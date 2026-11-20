"""vector-fgb serializer: the shape every vector row ends in.

GeoJSON features become FlatGeobuf, after the declarative frame normalizer every
vector executor runs first. ALWAYS emits a valid FGB: an empty result is a
header-only FGB carrying the declared schema, never a fabricated error."""

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
    "trid3nt_server.tools.fetchers._router.executors.vector_fgb"
)

__all__ = [
    "features_to_fgb_bytes",
    "apply_ingest_transforms",
    "build_where",
    "flatten_record",
    "resolve_endpoints",
]


# ``ingest.where_clauses``: a rule's ``str.format(**params)`` clause joins (AND) only when every
# ``require`` param is present and non-None; none declared falls back to a literal ``where`` else "1=1".


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


def _passes_geometry_filter(geom: Any, gf: dict[str, Any]) -> bool:
    """Point/finite-geometry filter: ``geom_types`` allowlist; ``require_finite`` drops a missing or non-finite leading (x, y)."""
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


def apply_ingest_transforms(
    features: list[dict[str, Any]],
    spec: SourceSpec,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """``geometry_filter``, then every record flattened, with what the fetch produced beside it."""
    gf = (spec.ingest or {}).get("geometry_filter")
    out: list[dict[str, Any]] = []
    for feat in features:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        if gf and not _passes_geometry_filter(geom, gf):
            continue
        out.append({"type": "Feature", "geometry": geom,
                    "properties": _with_produced(flatten_record(feat.get("properties") or {}),
                                                 feat.get("produced") or {}, spec)})
    return out


def flatten_record(record: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """A source's record as one flat row under its own names: a nested object as dotted keys, a list as JSON text."""
    flat: dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            flat.update(flatten_record(value, f"{name}."))
        elif isinstance(value, (dict, list, tuple)):
            flat[name] = json.dumps(value, default=str)
        else:
            flat[name] = value
    return flat


def _with_produced(record: dict[str, Any], produced: dict[str, Any], spec: SourceSpec) -> dict[str, Any]:
    """The record with what the fetch made of it; a made field named like one the source returned refuses."""
    clash = sorted(set(produced) & set(record))
    if clash:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"the fetch produces {', '.join(clash)}, a name the source's own record already "
            "carries; the source's field keeps its name, so the produced one is renamed in the hook",
            retryable=False, suffix="FIELD_COLLISION")
    return {**record, **produced}


def _out_columns(spec: SourceSpec, features: list[dict[str, Any]]) -> list[str]:
    declared = (spec.ingest or {}).get("properties")
    if declared:
        cols = [str(c) for c in declared]
    else:
        cols = []
        for feat in features:
            for k in (feat.get("properties") or {}).keys():
                if k not in cols:
                    cols.append(str(k))
    return cols


def features_to_fgb_bytes(
    features: list[dict[str, Any]],
    spec: SourceSpec,
    params: dict[str, Any] | None = None,
) -> bytes:
    """Serialize GeoJSON features to FlatGeobuf bytes after the ingest transforms; an empty
    list yields a header-only FGB carrying the declared schema."""
    try:
        import geopandas as gpd
        import pandas as pd
    except ImportError as exc:  # pragma: no cover
        raise router_upstream_error(spec.error_code_prefix, f"geopandas unavailable: {exc}")

    features = apply_ingest_transforms(features, spec, params)

    crs = spec.normalize.crs
    # keep_null_geometry preserves attribute-only rows (an alert whose zone did not resolve).
    keep_null = bool(getattr(spec.output, "keep_null_geometry", False))
    if keep_null:
        rows = [f for f in features if isinstance(f, dict)]
    else:
        rows = [
            f for f in features
            if isinstance(f, dict) and f.get("geometry") is not None
        ]
    valid = [f for f in rows if f.get("geometry") is not None]
    cols = _out_columns(spec, features)

    if not rows:
        empty_df = pd.DataFrame(columns=cols)
        gdf = gpd.GeoDataFrame(empty_df, geometry=[], crs=crs)
    elif keep_null:
        # Column order is pinned to the declared schema.
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

    # pyogrio rejects a spatial index over any NULL geometry.
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
    """Ordered endpoint chain: the primary picked by ``ingest.endpoint_select`` (default ``data``), then SAME-DATA mirrors."""
    endpoints = spec.endpoints
    ingest = spec.ingest or {}
    sel = ingest.get("endpoint_select")
    by_enum = ingest.get("endpoint_by_param")
    if isinstance(by_enum, dict):
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
