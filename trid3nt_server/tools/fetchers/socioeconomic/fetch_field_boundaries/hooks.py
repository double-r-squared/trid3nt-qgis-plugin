"""FTW/fiboa field-boundary delegate hooks: geopandas owns the pushdown socket.

The published GeoParquet is read over an fsspec handle with CRS-aware row-group bbox
PUSHDOWN happening INSIDE the parquet reader, which issues its own range requests to
prune row groups. That pushdown is the library's, not a router transport."""

# ``select`` is PURE: it resolves the bbox to a dataset key from the declared table, or
# takes an explicit ``dataset`` param, and merges the key into params before
# read_through so it enters the cache key, raising the no-coverage or invalid-input
# error pre-cache. ``read`` is the pushdown read itself, returning WGS84 polygon
# features for the shared serializer.

from __future__ import annotations

import json
import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import RouterUpstreamError, router_input_error
from ..._router.hooks import register_hook

logger = logging.getLogger(__name__)

__all__ = ["select_dataset", "read_fields"]

_MAX_FEATURES = 50_000
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)


def _datasets(spec: SourceSpec) -> list[dict[str, Any]]:
    return ((spec.ingest or {}).get("field_boundaries") or {}).get("datasets") or []


def _bbox_intersects(a: Any, b: Any) -> bool:
    """True iff the two WGS84 bboxes overlap; a touching edge counts."""
    from shapely.geometry import box

    return box(*a).intersects(box(*b))




@register_hook("field_boundaries.select")
def select_dataset(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Pick the dataset covering ``bbox``, or honour an explicit ``dataset`` key, merging
    the resolved key into params so it enters the cache key. An unknown key is an
    invalid input; no overlap is a no-coverage refusal."""
    sc = spec.error_code_prefix
    bbox = tuple(float(v) for v in params["bbox"])
    requested = params.get("dataset")
    datasets = _datasets(spec)

    if requested:
        for ds in datasets:
            if ds["key"] == requested:
                if not _bbox_intersects(bbox, ds["coverage"]):
                    raise router_input_error(
                        sc,
                        f"dataset {requested!r} (coverage {tuple(ds['coverage'])}) does not "
                        f"intersect bbox {bbox}; pick a bbox inside the dataset or omit "
                        f"`dataset` to auto-select.",
                        "NO_COVERAGE",
                    )
                return {"dataset": ds["key"]}
        raise router_input_error(
            sc, f"unknown dataset {requested!r}; valid keys: {[d['key'] for d in datasets]}",
            spec.input_error_suffix,
        )

    matches = [ds for ds in datasets if _bbox_intersects(bbox, ds["coverage"])]
    if not matches:
        labels = ", ".join(d["label"] for d in datasets)
        raise router_input_error(
            sc,
            f"no published Fields of The World / fiboa dataset covers bbox {bbox}. "
            f"Published coverage is regional (currently: {labels}). On-demand global "
            "field-boundary inference from satellite imagery is a separate future tool "
            "and is not available yet.",
            "NO_COVERAGE",
        )
    # Prefer a pushdown-capable dataset when several overlap (cheaper read).
    matches.sort(key=lambda d: (not bool(d.get("pushdown")),))
    return {"dataset": matches[0]["key"]}




def _file_crs(parquet_file: Any) -> Any:
    import pyproj

    geo_raw = parquet_file.schema_arrow.metadata.get(b"geo")
    if not geo_raw:
        return pyproj.CRS.from_user_input("OGC:CRS84")
    geo = json.loads(geo_raw)
    prim = geo.get("primary_column")
    col = (geo.get("columns") or {}).get(prim, {})
    crs = col.get("crs")
    if crs is None:
        return pyproj.CRS.from_user_input("OGC:CRS84")
    if isinstance(crs, dict):
        return pyproj.CRS.from_json_dict(crs)
    return pyproj.CRS.from_user_input(crs)


def _has_covering(parquet_file: Any) -> bool:
    geo_raw = parquet_file.schema_arrow.metadata.get(b"geo")
    if not geo_raw:
        return False
    geo = json.loads(geo_raw)
    prim = geo.get("primary_column")
    col = (geo.get("columns") or {}).get(prim, {})
    return bool(col.get("covering"))


@register_hook("field_boundaries.read")
def read_fields(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> list[dict[str, Any]]:
    """Read field-boundary polygons for ``bbox`` from the selected dataset: a row-group
    bbox pushdown, then a clip to the exact bbox, a cap, and a crop-label normalization
    to WGS84 polygon features. A read failure raises the typed upstream error."""
    import geopandas as gpd
    import pyarrow.parquet as pq
    import pyproj
    from shapely.geometry import box

    sc = spec.error_code_prefix
    bbox = tuple(float(v) for v in params["bbox"])
    key = params["dataset"]
    ds = next((d for d in _datasets(spec) if d["key"] == key), None)
    if ds is None:  # defensive: select_dataset already resolved it
        raise router_input_error(sc, f"unknown dataset {key!r}", spec.input_error_suffix)

    crop_field = ds.get("crop_field")
    columns = ["geometry"] + ([crop_field] if crop_field else [])

    import fsspec

    try:
        fh = fsspec.filesystem("https", headers={"User-Agent": _USER_AGENT}).open(ds["url"])
    except Exception as exc:  # noqa: BLE001
        raise RouterUpstreamError(f"could not open {ds['url']}: {exc}")

    try:
        try:
            pf = pq.ParquetFile(fh)
            file_crs = _file_crs(pf)
            covering = _has_covering(pf)
            try:
                fh.seek(0)
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            raise RouterUpstreamError(f"could not read GeoParquet metadata for {key}: {exc}")

        wgs84 = pyproj.CRS.from_epsg(4326)
        if file_crs.equals(wgs84) or file_crs.to_epsg() == 4326:
            pushdown_bbox = bbox
            same_crs = True
        else:
            same_crs = False
            qb = gpd.GeoSeries([box(*bbox)], crs="EPSG:4326").to_crs(file_crs)
            pushdown_bbox = tuple(float(v) for v in qb.total_bounds)

        read_kwargs: dict[str, Any] = {"columns": columns}
        if bool(ds.get("pushdown")) and covering:
            read_kwargs["bbox"] = pushdown_bbox
        try:
            gdf = gpd.read_parquet(fh, **read_kwargs)
        except Exception as exc:  # noqa: BLE001
            raise RouterUpstreamError(f"GeoParquet read failed for {key}: {exc}")
    finally:
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass

    if gdf.crs is None:
        gdf = gdf.set_crs(file_crs, allow_override=True)
    if not same_crs:
        gdf = gdf.to_crs("EPSG:4326")

    clip_geom = box(*bbox)
    gdf = gdf[gdf.geometry.intersects(clip_geom)].copy()
    if len(gdf) > 0:
        gdf["geometry"] = gdf.geometry.intersection(clip_geom)
        gdf = gdf[~gdf.geometry.is_empty].copy()
    if len(gdf) > _MAX_FEATURES:
        logger.warning("field_boundaries: %d fields exceeds cap %d; truncating", len(gdf), _MAX_FEATURES)
        gdf = gdf.iloc[:_MAX_FEATURES].copy()

    if crop_field and crop_field in gdf.columns:
        gdf = gdf.rename(columns={crop_field: "crop_name"})
    elif "crop_name" not in gdf.columns:
        gdf["crop_name"] = None
    gdf = gdf[[c for c in ("geometry", "crop_name") if c in gdf.columns]]

    if len(gdf) == 0:
        return []
    return json.loads(gdf.to_json(drop_id=True))["features"]
