"""FTW/fiboa field-boundary delegate hooks: geopandas owns the pushdown socket.

fetch_field_boundaries reads PUBLISHED agricultural field-boundary GeoParquet from
Source Cooperative over an fsspec HTTPS handle, with CRS-aware GeoParquet 1.1 row-group
bbox PUSHDOWN happening INSIDE ``geopandas.read_parquet(fh, bbox=...)`` -- the parquet
reader issues its own HTTP range requests to prune row groups. That is a maintained
LIBRARY owning discovery + the socket (the pfdf / HRRR-Zarr pattern), so it folds onto
the VECTOR ``library_delegate`` mode: the "needs a new pushdown TRANSPORT"
concern is refuted -- the pushdown is not a router transport, it lives in geopandas, which
this delegate hook legitimately owns (the sanctioned impurity).

Two hooks:
  * ``field_boundaries.select`` (pre_resolve, PURE): bbox -> dataset key selection from
    the declared ``ingest.field_boundaries.datasets`` table (or an explicit ``dataset``
    param), merged into params before read_through so the resolved key enters the cache
    key. Raises the twin's FIELDS_NO_COVERAGE / FIELDS_INPUT_INVALID pre-cache.
  * ``field_boundaries.read`` (delegate): the geopandas GeoParquet pushdown read -> WGS84
    GeoJSON polygon features (crop_name property) for the shared vector_fgb serializer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import RouterUpstreamError, router_input_error
from . import register_hook

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.hooks.field_boundaries"
)

__all__ = ["select_dataset", "read_fields"]

_MAX_FEATURES = 50_000
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)


def _datasets(spec: SourceSpec) -> list[dict[str, Any]]:
    return ((spec.ingest or {}).get("field_boundaries") or {}).get("datasets") or []


def _bbox_intersects(a: Any, b: Any) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


# --------------------------------------------------------------------------- #
# pre_resolve: pure dataset selection (bbox -> key), pre-cache.
# --------------------------------------------------------------------------- #


@register_hook("field_boundaries.select")
def select_dataset(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Pick the FTW dataset covering ``bbox`` (or honor an explicit ``dataset`` key).

    Merges ``{"dataset": <key>}`` into params so the resolved key enters the cache key.
    Raises FIELDS_INPUT_INVALID (unknown key) / FIELDS_NO_COVERAGE (no overlap) -- the
    twin's ``_select_dataset``, byte-identical codes.
    """
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


# --------------------------------------------------------------------------- #
# delegate: geopandas GeoParquet pushdown read -> GeoJSON polygon features.
# --------------------------------------------------------------------------- #


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
    """Read field-boundary polygons for ``bbox`` from the selected dataset (twin ``_read_fields_gdf``).

    CRS-aware GeoParquet 1.1 row-group bbox pushdown over an fsspec HTTPS handle (the
    library owns the range reads), clip to the exact bbox, cap, normalize the crop label
    to ``crop_name`` -> WGS84 GeoJSON polygon features. A read failure -> FIELDS_UPSTREAM_ERROR.
    """
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
