"""tiled-mosaic transform (contract sec 2.4) -- HYBRID glue for esri_landcover.

A named transform WRAPPING the raster-cog executor: ``plan_tile_grid`` splits a
bbox >``tile_deg2`` into sub-tiles, each fetched via the raster-cog executor,
written to temp GTiffs, merged via ``rasterio.merge`` (``method="first"``,
categorical-safe), palette passthrough preserved. Single-tile bbox is the fast
path (one executor call). Hard ceiling ``gates.max_bbox_deg2`` raises the typed
bbox error redirecting to the sibling. This is a transform (composes the executor
N times), not a new executor -- keeping executors atomic per the
"analysis is composition" norm.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import bbox_error_suffix, router_empty_error, router_input_error
from ..executors import raster_cog

logger = logging.getLogger(
    "trid3nt_server.data.fetchers._router.transforms.tiled_mosaic"
)

__all__ = ["plan_tile_grid", "mosaic_tile_files", "array_to_tempfile", "execute"]


def plan_tile_grid(
    bbox: tuple[float, float, float, float],
    tile_deg2: float,
) -> list[tuple[float, float, float, float]]:
    """Split ``bbox`` into a grid of sub-bboxes each with area <= ``tile_deg2``.

    Border cells may be narrower; all cells cover the full bbox with no gaps.
    Total area already <= ``tile_deg2`` -> a single entry equal to ``bbox``.
    Lifted verbatim from the ``fetch_esri_landcover_10m._plan_tile_grid`` twin.
    """
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
    """Write a single-band array to a temp GTiff and return its path (caller unlinks)."""
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
    """Merge tile GTiffs into one COG via ``rasterio.merge`` (categorical-safe).

    ``method="first"`` + ``resampling=nearest`` keep class codes un-interpolated
    (the landcover categorical requirement). ``nodata`` + ``colormap`` carry the
    categorical mosaic's nodata + embedded palette through to the output COG.
    Returns COG bytes.
    """
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
    """Plan the tile grid, fetch each via the raster-cog executor, merge -> COG."""
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

    # Fast path: single tile == one executor call, no merge (categorical palette
    # COG for stac_search inherits from raster_cog.execute).
    if len(tiles) == 1:
        return raster_cog.execute(spec, {**params, "bbox": list(tiles[0])})

    # Categorical (esri_landcover): uint8 tiles carrying nodata + the embedded
    # palette merged first-non-nodata -> palette COG (twin _fetch_landcover_cog_bytes).
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
                    arr, transform, crs, tile_cmap = raster_cog.stac_to_mosaic(
                        spec, {**params, "bbox": list(tile)}
                    )
                    if colormap is None and tile_cmap is not None:
                        colormap = tile_cmap
                else:
                    arr, transform, crs = raster_cog.fetch_source_array(
                        spec, {**params, "bbox": list(tile)}
                    )
            except Exception as exc:  # noqa: BLE001
                # A tile with no coverage is skipped (first-non-nodata mosaic).
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
