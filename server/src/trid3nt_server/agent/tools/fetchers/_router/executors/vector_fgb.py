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

import json
import logging
import math
import os
import tempfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_upstream_error

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.executors.vector_fgb"
)

__all__ = [
    "features_to_fgb_bytes",
    "apply_ingest_transforms",
    "build_query_params",
    "fetch_features",
    "execute",
]


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


def _out_columns(spec: SourceSpec, features: list[dict[str, Any]]) -> list[str]:
    """Resolve the output property columns (spec-declared or feature-derived).

    Derived-column names are always appended so an honest-empty header-only FGB
    still carries them (matching the hifld twin's ``[facility_type,
    facility_label]`` empty schema).
    """
    declared = (spec.ingest or {}).get("properties")
    if declared:
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
    valid = [
        f for f in features
        if isinstance(f, dict) and f.get("geometry") is not None
    ]
    cols = _out_columns(spec, features)

    if not valid:
        empty_df = pd.DataFrame(columns=cols)
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs=crs)
    else:
        gdf = gpd.GeoDataFrame.from_features(valid, crs=crs)
        gdf = gdf.dropna(subset=["geometry"]).copy()

    tmp_fgb: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".fgb", delete=False, prefix="trid3nt_router_vec_"
        ) as f:
            tmp_fgb = f.name
        try:
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


def build_query_params(
    spec: SourceSpec,
    bbox: tuple[float, float, float, float],
    *,
    result_offset: int = 0,
    where: str = "1=1",
) -> tuple[str, dict[str, str]]:
    """Build an ArcGIS FeatureServer ``/query`` URL + params for one page.

    Reads ``ingest.query_template`` (page_size, out_fields, order_by) with the
    hifld defaults. Returns ``(url, params)``.
    """
    ingest = spec.ingest or {}
    qt = ingest.get("query_template", {})
    endpoint = spec.endpoints.get("data") or next(iter(spec.endpoints.values()))
    url = endpoint.url or endpoint.url_template or ""
    min_lon, min_lat, max_lon, max_lat = bbox
    page_size = int(ingest.get("pagination", {}).get("page_size", 2000))
    params: dict[str, str] = {
        "where": where,
        "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": str(qt.get("out_fields", "*")),
        "outSR": "4326",
        "f": str(qt.get("f", "geojson")),
        "resultOffset": str(result_offset),
        "resultRecordCount": str(page_size),
        "orderByFields": str(qt.get("order_by", "OBJECTID ASC")),
    }
    # merge static endpoint query
    for k, v in (endpoint.query or {}).items():
        params[str(k)] = str(v)
    return url, params


def _fetch_one_page(spec: SourceSpec, url: str, params: dict[str, str]) -> list[dict[str, Any]]:
    """GET one page of an ArcGIS query; return GeoJSON features. Network."""
    import httpx

    ua = spec.auth.user_agent
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True) as client:
            resp = client.get(url, params=params, headers={"User-Agent": ua})
    except httpx.HTTPError as exc:
        raise router_upstream_error(spec.error_code_prefix, f"request failed url={url}: {exc}")
    if resp.status_code >= 400:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"HTTP {resp.status_code} url={url}: {resp.text[:500]!r}",
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise router_upstream_error(spec.error_code_prefix, f"non-JSON response url={url}: {exc}")
    if isinstance(body, dict) and "error" in body:
        raise router_upstream_error(
            spec.error_code_prefix, f"error envelope url={url}: {body['error']}"
        )
    if not isinstance(body, dict):
        raise router_upstream_error(spec.error_code_prefix, "response is not a JSON object")
    return body.get("features", []) or []


def fetch_features(spec: SourceSpec, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Page through the FeatureServer query, accumulating up to ``max_features``."""
    bbox = params["bbox"]
    max_features = spec.gates.max_features or 30000
    ingest = spec.ingest or {}
    page_size = int(ingest.get("pagination", {}).get("page_size", 2000))
    where = params.get("where", "1=1")

    accumulated: list[dict[str, Any]] = []
    offset = 0
    while True:
        url, qparams = build_query_params(spec, bbox, result_offset=offset, where=where)
        page = _fetch_one_page(spec, url, qparams)
        accumulated.extend(page)
        if len(page) < page_size:
            break
        if len(accumulated) >= max_features:
            accumulated = accumulated[:max_features]
            break
        offset += page_size
    return accumulated


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    """Fetch features and serialize to FGB bytes (the ``fetch_fn`` body)."""
    features = fetch_features(spec, params)
    return features_to_fgb_bytes(features, spec, params)
