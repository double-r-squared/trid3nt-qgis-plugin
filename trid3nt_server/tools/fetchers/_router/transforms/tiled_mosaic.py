"""Tiled-mosaic transform over the raster-cog executor.

A bbox larger than ``tile_deg2`` splits into sub-tiles, each fetched through the
executor and merged first-non-nodata, categorical-safe and palette-preserving. A
single-tile bbox is one executor call; ``gates.max_bbox_deg2`` is the hard ceiling."""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import bbox_error_suffix, router_empty_error, router_input_error
from ..executors import raster_cog, stac_raster

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.transforms.tiled_mosaic"
)

__all__ = ["plan_tile_grid", "mosaic_tile_files", "array_to_tempfile", "execute"]


def _tile_source(spec: SourceSpec) -> Any:
    return stac_raster if (spec.ingest or {}).get("access") == "stac" else raster_cog


def plan_tile_grid(
    bbox: tuple[float, float, float, float],
    tile_deg2: float,
) -> list[tuple[float, float, float, float]]:
    """Split ``bbox`` into gapless sub-bboxes of area <= ``tile_deg2`` (border cells may be narrower); a small bbox returns itself."""
    min_lon, min_lat, max_lon, max_lat = bbox
    dlon = max_lon - min_lon
    dlat = max_lat - min_lat
    area = dlon * dlat
    if area <= tile_deg2:
        return [bbox]
    ratio = dlon / dlat if dlat > 0 else 1.0
    nrows = max(1, math.ceil(math.sqrt(area / tile_deg2 / ratio)))
    ncols = max(1, math.ceil(area / tile_deg2 / nrows))
    cell_dlon = dlon / ncols
    cell_dlat = dlat / nrows
    tiles: list[tuple[float, float, float, float]] = []
    for row in range(nrows):
        for col in range(ncols):
            tw = min_lon + col * cell_dlon
            ts = min_lat + row * cell_dlat
            te = min_lon + (col + 1) * cell_dlon if col < ncols - 1 else max_lon
            tn = min_lat + (row + 1) * cell_dlat if row < nrows - 1 else max_lat
            tiles.append((tw, ts, te, tn))
    return tiles


def array_to_tempfile(
    array: Any, transform: Any, crs: Any, *, dtype: str = "float32",
    nodata: float | None = None,
) -> str:
    import numpy as np
    import rasterio

    arr = np.asarray(array, dtype=dtype)
    if arr.ndim == 2:
        arr = arr[np.newaxis, :, :]
    _fd, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_router_tile_")
    os.close(_fd)
    profile: dict[str, Any] = dict(
        driver="GTiff", height=arr.shape[1], width=arr.shape[2],
        count=arr.shape[0], dtype=dtype, crs=crs, transform=transform,
    )
    if nodata is not None:
        profile["nodata"] = nodata
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr)
    return path


def mosaic_tile_files(
    paths: list[str],
    *,
    method: str = "first",
    resampling: str = "nearest",
    dtype: str = "float32",
    nodata: float | None = None,
    colormap: dict | None = None,
) -> bytes:
    """Merge tile GTiffs into one COG, first-non-nodata with nearest resampling so class codes stay un-interpolated."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.merge import merge as rio_merge

    merge_kw: dict[str, Any] = dict(method=method, resampling=Resampling[resampling])
    if nodata is not None:
        merge_kw["nodata"] = nodata
    srcs = [rasterio.open(p) for p in paths]
    try:
        mosaic, out_transform = rio_merge(srcs, **merge_kw)
        crs = srcs[0].crs
    finally:
        for s in srcs:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
    return raster_cog.array_to_cog_bytes(
        mosaic if mosaic.ndim == 2 else mosaic[0], out_transform, crs,
        nodata=(float("nan") if nodata is None else nodata), dtype=dtype, colormap=colormap,
    )


def execute(spec: SourceSpec, params: dict[str, Any]) -> bytes:
    ingest = spec.ingest or {}
    mosaic_cfg = ingest.get("mosaic", {})
    tile_deg2 = float(ingest.get("tile_deg2", 0.5))
    bbox = params["bbox"]

    max_deg2 = spec.gates.max_bbox_deg2
    dlon = bbox[2] - bbox[0]
    dlat = bbox[3] - bbox[1]
    if max_deg2 is not None and (dlon * dlat) > max_deg2:
        raise router_input_error(
            spec.error_code_prefix,
            f"bbox area {dlon * dlat:.2f} deg^2 exceeds max_bbox_deg2={max_deg2}; "
            f"use a coarser sibling source (e.g. NLCD) for a state-scale extent",
            bbox_error_suffix(spec),
        )

    tiles = plan_tile_grid(tuple(bbox), tile_deg2)  # type: ignore[arg-type]

    source = _tile_source(spec)
    if len(tiles) == 1:
        return source.execute(spec, {**params, "bbox": list(tiles[0])})

    categorical = ingest.get("palette") == "passthrough" or str(ingest.get("dtype")) == "uint8"
    nodata = int(mosaic_cfg.get("nodata", 0)) if categorical else None
    method = mosaic_cfg.get("method", "first")
    resampling = mosaic_cfg.get("resampling", "nearest")

    tile_paths: list[str] = []
    colormap: dict | None = None
    try:
        for tile in tiles:
            try:
                if categorical:
                    arr, transform, crs, tile_cmap = source.stac_to_mosaic(
                        spec, {**params, "bbox": list(tile)}
                    )
                    if colormap is None and tile_cmap is not None:
                        colormap = tile_cmap
                else:
                    arr, transform, crs = source.fetch_source_array(
                        spec, {**params, "bbox": list(tile)}
                    )
            except Exception as exc:  # noqa: BLE001
                if type(exc).__name__ == "RouterEmptyError":
                    continue
                raise
            if categorical:
                tile_paths.append(array_to_tempfile(arr, transform, crs, dtype="uint8", nodata=nodata))
            else:
                tile_paths.append(array_to_tempfile(arr, transform, crs))
        if not tile_paths:
            raise router_empty_error(spec.error_code_prefix, f"no tile carried data for bbox={bbox}", spec.empty_error_suffix)
        if categorical:
            return mosaic_tile_files(
                tile_paths, method=method, resampling=resampling,
                dtype="uint8", nodata=nodata, colormap=colormap,
            )
        return mosaic_tile_files(tile_paths, method=method, resampling=resampling)
    finally:
        for p in tile_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
