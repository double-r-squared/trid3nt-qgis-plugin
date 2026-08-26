"""vector-fgb executor (contract sec 2.2), incl. the ArcGIS paging mode.

Query -> GeoJSON/esri-json -> FlatGeobuf via ``geopandas ... driver="FlatGeobuf",
engine="pyogrio"``. ``pagination.mode`` selects ``result_offset`` (hifld/census)
or ``exceeded_transfer_limit``; the loop mirrors ``_fetch_features_paginated``
with the ``max_features`` cap. ALWAYS emits a valid FGB -- an empty result is a
header-only FGB (honest-empty, never a fabricated error), matching the twins.

The pure serializer ``features_to_fgb_bytes`` is offline-testable with synthetic
GeoJSON features; the network path routes through ``fetch_features`` which tests
monkeypatch (or drive against a fake ``_fetch_one_page``).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import tempfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_upstream_error
from ..shape_classifier import classify_response
from ..transport import TransportError, get_bytes, get_client, is_staged_uri

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.vector_fgb"
)

__all__ = [
    "features_to_fgb_bytes",
    "apply_ingest_transforms",
    "apply_column_map",
    "build_where",
    "resolve_endpoints",
    "build_query_params",
    "fetch_features",
    "execute",
]


# --------------------------------------------------------------------------- #
# Declarative WHERE-clause builder (phase-2 wave-2 ArcGIS family).
#
# `ingest.where_clauses` is an ordered list of {template, require:[params]}
# rules: a rule contributes its `str.format(**params)` clause ONLY when every
# `require` param is present + non-None in the validated params; the surviving
# clauses are AND-joined. Absent/none-declared -> falls back to a literal
# `where` param else "1=1". This carries the hifld VOLTAGE floor, the mtbs YEAR
# range, and the drought period= filters as spec data, no source hardcode.
# --------------------------------------------------------------------------- #


def build_where(spec: SourceSpec, params: dict[str, Any]) -> str:
    ingest = spec.ingest or {}
    clauses_spec = ingest.get("where_clauses")
    if not clauses_spec:
        return str(params.get("where", "1=1"))
    parts: list[str] = []
    for rule in clauses_spec:
        if not isinstance(rule, dict):
            continue
        require = rule.get("require") or []
        if any(params.get(p) is None for p in require):
            continue
        template = rule.get("template", "")
        try:
            parts.append(template.format(**params))
        except (KeyError, IndexError, ValueError):
            continue
    return " AND ".join(parts) if parts else "1=1"


# --------------------------------------------------------------------------- #
# Declarative column normalizer (phase-2 wave-2 ArcGIS family).
#
# `ingest.column_map` is an ORDERED map out_col -> rule reproducing a twin's
# raw-property -> output-column projection/rename/normalization WITHOUT a source
# hardcode. Rule fields:
#   from            source property key (case-insensitive when column_map_ci)
#   kind            passthrough(default) | int | float | str | lookup | epoch_ms_iso
#   null_below      numeric: value <= this -> None (the -999 SVI sentinel)
#   on_error        null(default) | skip_feature (drop the whole feature)
#   key_from        lookup: an already-computed out_col to key the table on
#   table           lookup: {key -> label}
#   default         value when the source key is absent / lookup miss
#   default_template  lookup miss: str formatted with {key} (drought "D{key}")
# When column_map is present the executor emits EXACTLY the mapped columns (a
# projection), then derived_columns / json_coerce / geometry_filter layer on top.
# --------------------------------------------------------------------------- #


class _SkipFeature(Exception):
    """Internal sentinel: a column_map rule with on_error=skip_feature failed."""


def _num(raw: Any) -> float:
    return float(raw)


def _norm_env(v: Any, kind: str) -> float | None:
    """EPA-EJScreen sentinel normalizers (percentile [0,100] / fraction [0,1] /

    raw). ``<= -999`` (EJSCREEN_NULL_SENTINELS[0]) or non-finite -> None; percentile
    /fraction clamp to their range and reject out-of-tolerance sentinels; raw
    passes any finite non-sentinel float. Byte-identical to the twin's three
    ``_normalize_*`` helpers.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= -999.0:
        return None
    if kind == "raw":
        return f
    hi = 100.0 if kind == "percentile" else 1.0
    if f < -0.001 or f > hi + 0.001:
        return None
    return max(0.0, min(hi, f))


def _resolve_column(
    rule: dict[str, Any], src_props: dict[str, Any], out_row: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> Any:
    kind = rule.get("kind", "passthrough")
    on_error = rule.get("on_error", "null")

    # A param-echo column (ejscreen `indicator` = the validated request param).
    if kind == "param":
        return (params or {}).get(rule.get("param"))
    # from_param: the SOURCE field is chosen by a request param through a map
    # (ejscreen `value` <- INDICATORS[indicator]); resolve then fall through to
    # the declared kind over that field.
    if "from_param" in rule:
        fp = rule.get("from_param") or {}
        field = (fp.get("map") or {}).get((params or {}).get(fp.get("param")))
        rule = {**rule, "from": field}
    if kind in ("percentile", "fraction", "raw"):
        present = rule.get("from") in src_props
        raw = src_props.get(rule.get("from")) if present else rule.get("default")
        return _norm_env(raw, kind)

    if kind == "lookup":
        table = rule.get("table") or {}
        key = out_row.get(rule["key_from"]) if "key_from" in rule else src_props.get(rule.get("from"))
        if key is None:
            return rule.get("default")
        # YAML int-keyed tables load as int keys; coerce the lookup key to int
        # when the table is int-keyed so a float/str code still resolves.
        if table and all(isinstance(k, int) for k in table):
            try:
                key = int(key)
            except (TypeError, ValueError):
                return rule.get("default")
        if key in table:
            return table[key]
        if "default_template" in rule:
            return str(rule["default_template"]).format(key=key)
        return rule.get("default")

    present = rule.get("from") in src_props
    raw = src_props.get(rule.get("from")) if present else rule.get("default")

    if kind == "passthrough":
        return raw
    if kind == "str":
        return str(raw)
    if kind == "epoch_ms_iso":
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            try:
                return _dt.datetime.fromtimestamp(raw / 1000.0, tz=_dt.timezone.utc).date().isoformat()
            except (OverflowError, OSError, ValueError):
                return rule.get("default", "")
        return rule.get("default", "")
    if kind in ("int", "float"):
        if raw is None:
            if on_error == "skip_feature":
                raise _SkipFeature
            return None
        try:
            f = _num(raw)
        except (TypeError, ValueError):
            if on_error == "skip_feature":
                raise _SkipFeature
            return None
        null_below = rule.get("null_below")
        if null_below is not None and f <= null_below:
            return None
        if kind == "int":
            try:
                return int(raw)
            except (TypeError, ValueError):
                if on_error == "skip_feature":
                    raise _SkipFeature
                return None
        return f
    return raw


def _resolve_column_map(spec: SourceSpec, params: dict[str, Any] | None) -> dict[str, Any] | None:
    """The effective column_map for this call (static, or per-enum synthesized).

    ``ingest.properties_by_param`` (phase-2 wave-6) reproduces a twin whose kept
    property SET varies by an enum param (usace_levees ``layer`` -> a different
    ``_LAYER_PROPERTIES`` tuple per sub-layer): it synthesizes a passthrough
    column_map over the resolved per-value column list, so the projection +
    honest-empty header schema both follow the selected value. No-op (returns the
    static ``ingest.column_map``, possibly None) for every prior spec.
    """
    ingest = spec.ingest or {}
    pbp = ingest.get("properties_by_param")
    if isinstance(pbp, dict) and params is not None:
        pval = params.get(pbp.get("param"))
        cols = (pbp.get("map") or {}).get(pval)
        if cols:
            return {str(c): {"from": str(c), "kind": "passthrough"} for c in cols}
    return ingest.get("column_map")


def apply_column_map(
    features: list[dict[str, Any]], spec: SourceSpec, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Project each feature's props to the declared ``ingest.column_map`` columns."""
    ingest = spec.ingest or {}
    cmap = _resolve_column_map(spec, params)
    if not cmap:
        return features
    ci = bool(ingest.get("column_map_ci"))
    out: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        raw_props = dict(feat.get("properties") or {})
        src = {str(k).lower(): v for k, v in raw_props.items()} if ci else raw_props
        row: dict[str, Any] = {}
        try:
            for out_col, rule in cmap.items():
                r = dict(rule)
                if ci and "from" in r:
                    r["from"] = str(r["from"]).lower()
                row[str(out_col)] = _resolve_column(r, src, row, params)
        except _SkipFeature:
            continue
        out.append({"type": "Feature", "geometry": feat.get("geometry"), "properties": row})
    return out


# --------------------------------------------------------------------------- #
# Declarative ingest transforms (contract sec 2.2 -- the hifld twin's parity
# gap: derived/constant columns, nested-property -> JSON coercion, and the
# Point/finite-geometry filter). All three are opt-in ``ingest.*`` directives
# (no source hardcodes); a spec without them is a strict no-op (census/demo).
# --------------------------------------------------------------------------- #


def _derived_column_names(spec: SourceSpec) -> list[str]:
    return [str(c) for c in ((spec.ingest or {}).get("derived_columns") or {})]


def _passes_geometry_filter(geom: Any, gf: dict[str, Any]) -> bool:
    """Point/finite-geometry filter (hifld twin ``_features_to_flatgeobuf``).

    ``geom_types`` restricts to a geometry-type allowlist; ``require_finite``
    drops features whose leading (x, y) coordinate pair is missing/non-finite.
    """
    if geom is None:
        return False
    geom_types = gf.get("geom_types")
    if geom_types and geom.get("type") not in geom_types:
        return False
    if gf.get("require_finite"):
        coords = geom.get("coordinates")
        if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
            return False
        x, y = coords[0], coords[1]
        if not (
            isinstance(x, (int, float)) and isinstance(y, (int, float))
            and math.isfinite(x) and math.isfinite(y)
        ):
            return False
    return True


def _resolve_derived(dc_spec: dict[str, Any], spec: SourceSpec, params: dict[str, Any] | None) -> Any:
    """Resolve one derived-column value from a declarative source descriptor.

    ``source: const``   -> ``value`` (a constant).
    ``source: param``   -> ``params[param]`` (e.g. facility_type == the request).
    ``source: routing`` -> ``ingest.routing[params[key_param]][field]`` (e.g.
    facility_label == the routing table's label for the requested facility_type).
    """
    src = dc_spec.get("source")
    if src == "const":
        return dc_spec.get("value")
    if src == "param":
        return (params or {}).get(dc_spec.get("param"))
    if src == "routing":
        routing = (spec.ingest or {}).get("routing") or {}
        key = (params or {}).get(dc_spec.get("key_param"))
        entry = routing.get(key) if isinstance(routing, dict) else None
        if isinstance(entry, dict):
            return entry.get(dc_spec.get("field"))
    return None


def apply_ingest_transforms(
    features: list[dict[str, Any]],
    spec: SourceSpec,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apply declarative ``geometry_filter`` / ``json_coerce_nested`` /
    ``derived_columns`` to raw features (no-op when none are declared)."""
    ingest = spec.ingest or {}
    # Column-map projection (rename/normalize) runs FIRST so geometry_filter /
    # json_coerce / derived_columns see the normalized output props.
    features = apply_column_map(features, spec, params)
    gf = ingest.get("geometry_filter")
    json_coerce = bool(ingest.get("json_coerce_nested"))
    derived = ingest.get("derived_columns") or {}
    if not (gf or json_coerce or derived):
        return features
    out: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if gf and not _passes_geometry_filter(geom, gf):
            continue
        props = dict(feat.get("properties") or {})
        if json_coerce:
            for k, v in list(props.items()):
                if isinstance(v, (dict, list)):
                    props[k] = json.dumps(v)
        for col, dc_spec in derived.items():
            props[str(col)] = _resolve_derived(dc_spec, spec, params)
        out.append({"type": "Feature", "geometry": geom, "properties": props})
    return out


def _out_columns(
    spec: SourceSpec, features: list[dict[str, Any]], params: dict[str, Any] | None = None
) -> list[str]:
    """Resolve the output property columns (spec-declared or feature-derived).

    Derived-column names are always appended so an honest-empty header-only FGB
    still carries them (matching the hifld twin's ``[facility_type,
    facility_label]`` empty schema).
    """
    ingest = spec.ingest or {}
    cmap = _resolve_column_map(spec, params)
    declared = ingest.get("properties")
    if cmap:
        # column_map is the authoritative projected schema (honest-empty header
        # still carries every mapped column, matching the twins' empty FGB).
        cols = [str(c) for c in cmap]
    elif declared:
        cols = [str(c) for c in declared]
    else:
        cols = []
        for feat in features:
            for k in (feat.get("properties") or {}).keys():
                if k not in cols:
                    cols.append(str(k))
    for dc in _derived_column_names(spec):
        if dc not in cols:
            cols.append(dc)
    return cols


def features_to_fgb_bytes(
    features: list[dict[str, Any]],
    spec: SourceSpec,
    params: dict[str, Any] | None = None,
) -> bytes:
    """Serialize GeoJSON features to FlatGeobuf bytes (pure, offline).

    Applies the declarative ingest transforms (geometry filter, JSON coercion,
    derived columns) first, then serializes. Always emits a valid FGB: an empty
    feature list yields a header-only FGB carrying the declared/derived column
    schema so downstream readers still parse (honest-empty, matching the
    hifld/census twins).
    """
    try:
        import geopandas as gpd
        import pandas as pd
    except ImportError as exc:  # pragma: no cover
        raise router_upstream_error(spec.error_code_prefix, f"geopandas unavailable: {exc}")

    features = apply_ingest_transforms(features, spec, params)

    crs = spec.normalize.crs
    # keep_null_geometry: preserve attribute-only rows (nws_alerts_conus
    # unresolvable-zone alerts) instead of dropping NULL-geometry features. Default
    # off = the byte-identical drop-null path for every prior spec.
    keep_null = bool(getattr(spec.output, "keep_null_geometry", False))
    if keep_null:
        rows = [f for f in features if isinstance(f, dict)]
    else:
        rows = [
            f for f in features
            if isinstance(f, dict) and f.get("geometry") is not None
        ]
    valid = [f for f in rows if f.get("geometry") is not None]
    cols = _out_columns(spec, features, params)

    if not rows:
        empty_df = pd.DataFrame(columns=cols)
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs=crs)
    elif keep_null:
        # Materialize every row (NULL geometry preserved); column order pinned to
        # the declared schema so the property table is stable.
        gdf = gpd.GeoDataFrame.from_features(
            [
                {
                    "type": "Feature",
                    "properties": {c: (f.get("properties") or {}).get(c) for c in cols},
                    "geometry": f.get("geometry"),
                }
                for f in rows
            ],
            crs=crs,
        )
    else:
        gdf = gpd.GeoDataFrame.from_features(rows, crs=crs)
        gdf = gdf.dropna(subset=["geometry"]).copy()

    # pyogrio rejects a spatial index over any NULL geometry; disable it when a
    # kept-null source emits (or could emit) attribute-only rows.
    has_null_geom = keep_null and bool(len(gdf)) and (len(valid) < len(rows))

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_router_vec_"
        ) as f:
            tmp_fgb = f.name
        try:
            if has_null_geom or (keep_null and len(gdf) == 0):
                gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio", SPATIAL_INDEX="NO")
            else:
                gdf.to_file(tmp_fgb, driver="FlatGeobuf", engine="pyogrio")
        except Exception as exc:  # noqa: BLE001
            raise router_upstream_error(
                spec.error_code_prefix, f"FlatGeobuf write failed for {len(gdf)} feature(s): {exc}"
            )
        with open(tmp_fgb, "rb") as f:
            fgb_bytes = f.read()
        logger.info(
            "router.vector_fgb: FlatGeobuf = %d bytes (%d feature(s), source=%s)",
            len(fgb_bytes), len(valid), spec.source_class,
        )
        return fgb_bytes
    finally:
        if tmp_fgb is not None:
            try:
                os.unlink(tmp_fgb)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# ArcGIS query build + paginated fetch (network).
# --------------------------------------------------------------------------- #


def _esri_geometry_to_geojson(g: Any) -> dict[str, Any] | None:
    """Decode one esri-json geometry to a GeoJSON geometry (rings/point/paths).

    Polygon ``rings`` keep ALL rings under a single Polygon (the standard
    ArcGIS->GeoJSON convention; pyogrio repairs winding on write); degenerate
    rings (< 4 vertices) are dropped -- byte-identical to the ejscreen twin's
    ``_esri_rings_to_geojson_geometry``.
    """
    if not isinstance(g, dict):
        return None
    if "rings" in g:
        good = [r for r in (g.get("rings") or []) if isinstance(r, list) and len(r) >= 4]
        return {"type": "Polygon", "coordinates": good} if good else None
    if "paths" in g:
        paths = [p for p in (g.get("paths") or []) if isinstance(p, list) and len(p) >= 2]
        if not paths:
            return None
        if len(paths) == 1:
            return {"type": "LineString", "coordinates": paths[0]}
        return {"type": "MultiLineString", "coordinates": paths}
    if "x" in g and "y" in g:
        x, y = g.get("x"), g.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            return {"type": "Point", "coordinates": [x, y]}
    return None


def _esri_feature_to_geojson(feat: dict[str, Any]) -> dict[str, Any]:
    """Esri ``{attributes, geometry}`` -> GeoJSON ``{properties, geometry}``."""
    return {
        "type": "Feature",
        "geometry": _esri_geometry_to_geojson(feat.get("geometry")),
        "properties": feat.get("attributes") or {},
    }


def resolve_endpoints(spec: SourceSpec, params: dict[str, Any]) -> list[Any]:
    """Ordered endpoint chain for the fetch: the SELECTED primary + fallbacks.

    ``ingest.endpoint_select`` chooses the primary by a param's presence (drought
    current layer /3 when ``date`` absent, archive layer /2 when present).
    Absent -> the ``data`` endpoint (else the first). ``spec.endpoint_fallback``
    names ordered SAME-DATA mirrors of this spec's own ``endpoints`` block, tried
    on the primary's upstream failure (nhd HR -> medium-res).
    """
    endpoints = spec.endpoints
    ingest = spec.ingest or {}
    sel = ingest.get("endpoint_select")
    by_enum = ingest.get("endpoint_by_param")
    if isinstance(by_enum, dict):
        # Per-enum sub-layer routing (usace_levees ``layer`` -> FeatureServer
        # sub-layer endpoint). No-op for every prior spec (none declare it).
        pval = params.get(by_enum.get("param"))
        primary = endpoints.get((by_enum.get("map") or {}).get(pval))
    elif isinstance(sel, dict):
        pname = sel.get("param")
        chosen = sel.get("present") if params.get(pname) is not None else sel.get("absent")
        primary = endpoints.get(chosen)
    else:
        primary = endpoints.get("data")
    if primary is None:
        primary = next(iter(endpoints.values()))
    chain = [primary]
    for fb in spec.endpoint_fallback:
        ep = endpoints.get(fb)
        if ep is not None and ep is not primary:
            chain.append(ep)
    return chain


def build_query_params(
    spec: SourceSpec,
    bbox: tuple[float, float, float, float] | None,
    *,
    result_offset: int = 0,
    where: str = "1=1",
    endpoint: Any | None = None,
) -> tuple[str, dict[str, str]]:
    """Build an ArcGIS FeatureServer ``/query`` URL + params for one page.

    Reads ``ingest.query_template`` (page_size, out_fields, order_by) with the
    hifld defaults. ``endpoint`` overrides the ``data`` endpoint (fallback /
    select chain). ``bbox=None`` omits the geometry envelope (the global-query
    sweep, supports_global_query sources: nifc). Returns ``(url, params)``.
    """
    ingest = spec.ingest or {}
    qt = ingest.get("query_template", {})
    if endpoint is None:
        endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    url = endpoint.url or endpoint.url_template or ""
    # A staged s3:// object needs the raster_cog direct_window resolution
    # (bucket/key -> the active endpoint); this executor hands its URL straight
    # to an ArcGIS FeatureServer query over httpx, which cannot serve one. Full
    # staged-vector support is future work -- this closes the trap with a typed
    # error instead of a raw httpx failure on an s3:// scheme.
    if is_staged_uri(url):
        raise router_upstream_error(
            spec.error_code_prefix,
            f"a staged s3:// uri is not readable by the vector query executor: {url!r}",
        )
    page_size = int(ingest.get("pagination", {}).get("page_size", 2000))
    params: dict[str, str] = {
        "where": where,
        "outFields": str(qt.get("out_fields", "*")),
        "outSR": "4326",
        "f": str(qt.get("f", "geojson")),
        "resultOffset": str(result_offset),
        "resultRecordCount": str(page_size),
    }
    # orderByFields only when the spec pins one: some hosted services (CDC onemap)
    # reject an orderByFields they do not support, so it is opt-in (the twins that
    # need stable paging set it; those that omit it, like the CDC SVI twin, do not).
    order_by = qt.get("order_by")
    if order_by:
        params["orderByFields"] = str(order_by)
    # esri-json sources (f=json layers that reject f=geojson, ejscreen) need the
    # geometry as a JSON envelope object + returnGeometry; a no-op for geojson.
    if bool(ingest.get("esri_json")):
        params["returnGeometry"] = "true"
    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
        if ingest.get("geometry_envelope") == "json":
            params["geometry"] = json.dumps({
                "xmin": min_lon, "ymin": min_lat, "xmax": max_lon, "ymax": max_lat,
                "spatialReference": {"wkid": 4326},
            })
        else:
            params["geometry"] = f"{min_lon},{min_lat},{max_lon},{max_lat}"
            params["inSR"] = "4326"
        params["geometryType"] = "esriGeometryEnvelope"
        params["spatialRel"] = "esriSpatialRelIntersects"
    # merge static endpoint query
    for k, v in (endpoint.query or {}).items():
        params[str(k)] = str(v)
    return url, params


def _fetch_one_page(spec: SourceSpec, url: str, params: dict[str, str]) -> list[dict[str, Any]]:
    """GET one page of an ArcGIS query; return GeoJSON features. Network.

    Routes through the shared transport (``..transport.get_bytes`` over the
    pooled ``get_client()``) -- the ONE retry authority (429/5xx/timeout backoff
    + Retry-After) instead of a bare per-call client. Any ``TransportError``
    (typed >=400, or retry-exhaustion) is converted to the existing
    ``router_upstream_error`` framing so callers see no shape change.

    Body-shape classification (JSON-parse / ArcGIS ``{"error": ...}`` envelope
    / not-a-JSON-object) runs through the shared ``classify_response`` (item 4
    of the observability/retention batch) -- same typed exception + same
    message wording as before the migration, just sourced from ONE shape
    classifier instead of ad hoc parsing here.
    """
    ua = spec.auth.user_agent
    try:
        body, _ct, _final_url = get_bytes(
            get_client(), url, headers={"User-Agent": ua}, params=params
        )
    except TransportError as exc:
        raise router_upstream_error(spec.error_code_prefix, f"request failed url={url}: {exc}")

    verdict = classify_response(body.decode("utf-8", "replace"))
    if verdict.kind == "unparseable":
        raise router_upstream_error(
            spec.error_code_prefix,
            f"non-JSON response url={url}: {verdict.excerpt!r}",
        )
    if verdict.kind == "error_envelope":
        raise router_upstream_error(
            spec.error_code_prefix, f"error envelope url={url}: {verdict.error_message}"
        )
    body = verdict.body
    if not isinstance(body, dict):
        raise router_upstream_error(spec.error_code_prefix, "response is not a JSON object")
    features = body.get("features", []) or []
    # esri-json (f=json) sources hand back {attributes, geometry:{rings/x,y/paths}};
    # decode to GeoJSON {properties, geometry} so the shared transforms + serializer
    # see the uniform shape (ejscreen). No-op for f=geojson features.
    if bool((spec.ingest or {}).get("esri_json")):
        return [_esri_feature_to_geojson(f) for f in features if isinstance(f, dict)]
    return features


def _fetch_from_endpoint(spec: SourceSpec, endpoint: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Page through ONE endpoint's FeatureServer query up to ``max_features``."""
    bbox = params.get("bbox")   # None -> global-query sweep (supports_global_query)
    max_features = spec.gates.max_features or 30000
    page_size = int((spec.ingest or {}).get("pagination", {}).get("page_size", 2000))
    # A caller may CAP the sweep at its own count, and that is a single-shot
    # request rather than a paging hint: page size and ceiling move together, so
    # the first page returns exactly the cap in server order and the loop stops.
    # It exists because a consumer reproducing a hand-written query has to
    # reproduce its resultRecordCount - two different counts against the same
    # layer are two different answers when the layer holds more rows than either.
    capped = params.get("max_records")
    if capped is not None:
        page_size = max_features = int(capped)
    where = build_where(spec, params)

    accumulated: list[dict[str, Any]] = []
    offset = 0
    while True:
        url, qparams = build_query_params(
            spec, bbox, result_offset=offset, where=where, endpoint=endpoint
        )
        page = _fetch_one_page(spec, url, qparams)
        accumulated.extend(page)
        if len(page) < page_size:
            break
        if len(accumulated) >= max_features:
            accumulated = accumulated[:max_features]
            break
        offset += page_size
    return accumulated


def fetch_features(spec: SourceSpec, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Fetch features across the resolved endpoint chain (primary -> fallback).

    The primary is the selected/`data` endpoint; on an upstream failure each
    ordered ``spec.endpoint_fallback`` mirror is tried before the error is
    surfaced (nhd HR -> medium-res). Every mirror publishes the SAME dataset, so
    the hop is silent by the loudness floor. A single endpoint (the common case)
    is one call with no mirror.
    """
    chain = resolve_endpoints(spec, params)
    last_exc: Exception | None = None
    for i, endpoint in enumerate(chain):
        try:
            return _fetch_from_endpoint(spec, endpoint, params)
        except Exception as exc:  # noqa: BLE001 -- try the next endpoint in the chain
            last_exc = exc
            if i < len(chain) - 1:
                logger.warning(
                    "router.vector_fgb: endpoint %d/%d failed (%s); trying fallback",
                    i + 1, len(chain), exc,
                )
    assert last_exc is not None
    if len(chain) > 1:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"all {len(chain)} endpoints failed; last error: {last_exc}",
        )
    raise last_exc


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Fetch features and serialize to FGB bytes (the ``fetch_fn`` body)."""
    features = fetch_features(spec, params)
    return features_to_fgb_bytes(features, spec, params)
