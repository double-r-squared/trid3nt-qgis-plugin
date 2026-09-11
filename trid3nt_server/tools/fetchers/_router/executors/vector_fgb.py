"""vector-fgb serializer: the shape every vector row ends in.

GeoJSON features become FlatGeobuf, after the declarative frame normalizer every
vector executor runs first. ALWAYS emits a valid FGB: an empty result is a
header-only FGB carrying the declared schema, never a fabricated error."""

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

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.executors.vector_fgb"
)

__all__ = [
    "features_to_fgb_bytes",
    "apply_ingest_transforms",
    "apply_column_map",
    "build_where",
    "resolve_endpoints",
]


# Declarative WHERE-clause builder for the ArcGIS family.
#
# `ingest.where_clauses` is an ordered list of {template, require:[params]}
# rules: a rule contributes its `str.format(**params)` clause ONLY when every
# `require` param is present + non-None in the validated params; the surviving
# clauses are AND-joined. Absent/none-declared -> falls back to a literal
# `where` param else "1=1". A voltage floor, a year range and a period filter are
# all spec data this way, with no source hardcode.


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


# Declarative column normalizer for the ArcGIS family.
#
# `ingest.column_map` is an ORDERED map out_col -> rule, a raw-property to
# output-column projection, rename and normalization with no source hardcode.
# Rule fields:
#   from            source property key (case-insensitive when column_map_ci)
#   kind            passthrough(default) | int | float | str | lookup | date_iso
#   null_below      numeric: value <= this -> None (the -999 SVI sentinel)
#   on_error        null(default) | skip_feature (drop the whole feature)
#   key_from        lookup: an already-computed out_col to key the table on
#   table           lookup: {key -> label}
#   default         value when the source key is absent / lookup miss
#   default_template  lookup miss: str formatted with {key} (drought "D{key}")
# When column_map is present the executor emits EXACTLY the mapped columns (a
# projection), then derived_columns / json_coerce / geometry_filter layer on top.


class _SkipFeature(Exception):
    """Internal sentinel: a column_map rule with on_error=skip_feature failed."""


def _num(raw: Any) -> float:
    return float(raw)


def _norm_env(v: Any, kind: str) -> float | None:
    """Sentinel normalizer for an environmental indicator: ``<= -999`` or non-finite
    yields None, percentile and fraction clamp to their range and reject an
    out-of-tolerance sentinel, and raw passes any finite non-sentinel float."""
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

    # A param-echo column: the column value IS the validated request param.
    if kind == "param":
        return (params or {}).get(rule.get("param"))
    # from_param: the SOURCE field is chosen by a request param through a map
    # (the source field is looked up by a request param); resolve then fall
    # through to the declared kind over that field.
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
    if kind == "date_iso":
        # A date arrives typed where the driver read the service's own field type
        # and as epoch milliseconds where it did not; both are the same day.
        if isinstance(raw, (_dt.datetime, _dt.date)):
            return (raw.date() if isinstance(raw, _dt.datetime) else raw).isoformat()
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
    """The effective column_map for this call: the static ``ingest.column_map``, or a
    passthrough map synthesized from ``ingest.properties_by_param`` when the kept
    property SET varies by an enum param, so the empty header follows it too."""
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


# Declarative ingest transforms: derived and constant columns, nested-property to
# JSON coercion, and the Point/finite-geometry filter. All three are opt-in
# ``ingest.*`` directives; a spec declaring none of them is untouched.


def _derived_column_names(spec: SourceSpec) -> list[str]:
    return [str(c) for c in ((spec.ingest or {}).get("derived_columns") or {})]


def _passes_geometry_filter(geom: Any, gf: dict[str, Any]) -> bool:
    """Point/finite-geometry filter: ``geom_types`` is a geometry-type allowlist and
    ``require_finite`` drops a feature whose leading (x, y) pair is missing or
    non-finite."""
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
    """Resolve one derived-column value from its descriptor: ``const`` is a literal,
    ``param`` echoes a request param, and ``routing`` reads a field out of
    ``ingest.routing`` keyed by a request param's value."""
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
    """Resolve the output property columns, spec-declared or feature-derived. Derived
    names are always appended, so an honest-empty header-only FGB still carries
    them."""
    ingest = spec.ingest or {}
    cmap = _resolve_column_map(spec, params)
    declared = ingest.get("properties")
    if cmap:
        # column_map is the authoritative projected schema: the honest-empty header
        # still carries every mapped column.
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
    """Serialize GeoJSON features to FlatGeobuf bytes, applying the declarative ingest
    transforms first. Always emits a valid FGB: an empty feature list yields a
    header-only FGB carrying the declared and derived schema, so readers still parse."""
    try:
        import geopandas as gpd
        import pandas as pd
    except ImportError as exc:  # pragma: no cover
        raise router_upstream_error(spec.error_code_prefix, f"geopandas unavailable: {exc}")

    features = apply_ingest_transforms(features, spec, params)

    crs = spec.normalize.crs
    # keep_null_geometry preserves attribute-only rows -- an alert whose zone did not
    # resolve -- instead of dropping NULL-geometry features. Default off.
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


def resolve_endpoints(spec: SourceSpec, params: dict[str, Any]) -> list[Any]:
    """Ordered endpoint chain: the selected primary then its fallbacks.
    ``ingest.endpoint_select`` picks the primary by a param's presence, defaulting to
    the ``data`` endpoint; ``spec.endpoint_fallback`` names SAME-DATA mirrors."""
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
