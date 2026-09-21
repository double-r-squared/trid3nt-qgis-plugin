"""Many georeferenced tiles onto ONE AOI grid: the shared raster-row mechanic.

Every tiled elevation row - the nearshore tiles, the regional fine tiles, the
global relief, the national bathymetric surface - reads the same way: pick the
tiles, open each over /vsicurl, window-read the AOI, reproject onto one shared
bbox-clipped grid, last source wins. Only the tile PICK is a row's own."""

# Raw heterogeneous tiles are NEVER merged directly - a mixed-CRS merge raises on
# orientation - so each source is reprojected from its OWN CRS onto the shared
# grid and the LAST one painted wins, which makes the caller's source order the
# precedence. A source drops out silently where it is unreadable, empty or does
# not intersect the AOI, so ``painted`` is what a caller reconciles a promised
# footprint against, never the input list.
#
# The grid is built at the finest cell the sources carry, floored at the asked
# ``resolution_m``, and a grid past the pixel budget REFUSES naming the spacing
# that fits: nothing here coarsens on its own behalf.

from __future__ import annotations

import logging
import math
from typing import Any

from ._fetch_common import FetchError, PixelBudgetExceededError, UpstreamAPIError

logger = logging.getLogger(__name__)

__all__ = [
    "VSICURL_ENV",
    "MosaicEmpty",
    "MAX_GRID_PX",
    "target_grid",
    "mosaic",
    "painted_fraction",
]


class MosaicEmpty(FetchError):
    """No tile painted a cell of the AOI. A row translates it into its own
    coverage refusal: the merge itself cannot know whether an empty AOI means the
    programme publishes nothing here or that the pick was wrong."""

    error_code = "MOSAIC_EMPTY"
    retryable = False


#: GDAL no-sign-request env for anonymous public-S3 /vsicurl/ reads. The MULTIRANGE
#: + MERGE + HTTP/2 options batch the many small per-block (256x256) range requests
#: a bbox-windowed read issues into few multiplexed transfers - without them each
#: tile block is a separate latency-bound round trip and a native-resolution AOI
#: read crawls.
VSICURL_ENV = dict(
    AWS_NO_SIGN_REQUEST="YES",
    GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR",
    CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif,.tiff,.vrt",
    VSI_CACHE=True,
    VSI_CACHE_SIZE="104857600",
    GDAL_HTTP_MULTIRANGE="YES",
    GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES",
    GDAL_HTTP_VERSION="2",
    GDAL_BAND_BLOCK_CACHE="HASHSET",
)

#: Absolute physical cap (metres) on an elevation; any |z| at or above this is an
#: UNFLAGGED fill/nodata sentinel leak, masked to NaN.
SENTINEL_ABS = 9000.0

#: Pixel budget per axis: past it the request refuses rather than coarsening, and
#: the caller re-asks with a coarser resolution_m or a smaller AOI.
MAX_GRID_PX = 12000


def target_grid(sources: list[str], target_crs: str,
                bbox: tuple[float, float, float, float],
                resolution_m: float | None = None, *, source: str = "") -> Any:
    """The common bbox-aligned grid as ``(transform, width, height)``."""
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.warp import transform_bounds

    west, south, east, north = bbox
    t_west, t_south, t_east, t_north = transform_bounds(
        "EPSG:4326", target_crs, west, south, east, north, densify_pts=21)
    if not (t_east > t_west and t_north > t_south):
        raise UpstreamAPIError(
            f"degenerate AOI bounds in {target_crs}: "
            f"({t_west}, {t_south}, {t_east}, {t_north})")

    finest: float | None = None
    with rasterio.Env(**VSICURL_ENV):
        for src in sources:
            try:
                with rasterio.open(src) as ds:
                    px_w, px_h = abs(ds.transform.a), abs(ds.transform.e)
                    cx = (ds.bounds.left + ds.bounds.right) / 2.0
                    cy = (ds.bounds.bottom + ds.bounds.top) / 2.0
                    rt_w, rt_s, rt_e, rt_n = transform_bounds(
                        ds.crs, target_crs, cx, cy, cx + px_w, cy + px_h,
                        densify_pts=2)
                    cell = min(abs(rt_e - rt_w), abs(rt_n - rt_s))
            except Exception as exc:  # noqa: BLE001 - skip an unreadable source
                logger.warning("tile mosaic: could not probe the cell of %s: %s",
                               src, exc)
                continue
            if cell and cell > 0 and (finest is None or cell < finest):
                finest = cell

    if finest is None or finest <= 0:
        raise MosaicEmpty(
            f"no tile of {len(sources)} stated a readable cell over {bbox!r}")
    if resolution_m is not None and finest < float(resolution_m):
        finest = float(resolution_m)

    # The budget is measured on THAT grid - the projected extent's pixel count -
    # so the ceiling holds where the projected extent runs past the geodesic bbox
    # at a UTM zone edge.
    long_axis_m = max(t_east - t_west, t_north - t_south)
    fits = int(math.ceil(long_axis_m / MAX_GRID_PX))
    if finest < fits:
        raise PixelBudgetExceededError(
            f"{source or 'this source'}: bbox={tuple(bbox)} at "
            f"resolution_m={finest:g} needs {int(math.ceil(long_axis_m / finest))} "
            f"px on its long axis in {target_crs}, past the {MAX_GRID_PX} px/axis "
            f"budget. Nothing was coarsened: re-ask at resolution_m={fits} m (the "
            "finest spacing that fits this bbox) or coarser, or ask for a smaller "
            "bbox.")
    width = max(1, int(math.ceil((t_east - t_west) / finest)))
    height = max(1, int(math.ceil((t_north - t_south) / finest)))
    return from_origin(t_west, t_north, finest, finest), width, height


def _cell_m(ds: Any) -> float:
    """A source's native cell in METRES (a geographic grid scaled at mid-latitude)."""
    cell = abs(ds.transform.a)
    try:
        if ds.crs is not None and ds.crs.is_geographic:
            lat = (ds.bounds.bottom + ds.bounds.top) / 2.0
            return cell * 111320.0 * max(math.cos(math.radians(lat)), 0.1)
    except Exception:  # noqa: BLE001
        pass
    return cell


def _windowed(ds: Any, target_cell_m: float,
              bbox: tuple[float, float, float, float]) -> tuple[Any, Any]:
    """One band CLIPPED to the AOI and decimated to about the target cell, or
    ``(None, None)`` where the AOI does not intersect this source."""

    # Reading a full native tile only to resample it onto a coarse bbox-clipped
    # grid is the dominant fetch cost. These COGs carry no overviews but ARE
    # internally tiled, so a bbox WINDOW read pulls only the AOI-overlapping
    # blocks. The read is oversampled about 2x against the target cell so the
    # bilinear reprojection stays clean; a source already at or coarser than the
    # target is read natively.
    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window, from_bounds

    west, south, east, north = bbox
    full = Window(0, 0, ds.width, ds.height)
    try:
        sw, ss, se, sn = transform_bounds("EPSG:4326", ds.crs, west, south,
                                          east, north, densify_pts=21)
        win = from_bounds(sw, ss, se, sn, ds.transform)
        win = win.round_offsets().round_lengths().intersection(full)
    except Exception:  # noqa: BLE001 - fall back to the whole tile
        win = full
    if win.width < 1 or win.height < 1:
        return None, None

    native = _cell_m(ds)
    factor = 1
    if target_cell_m and native and native > 0:
        factor = max(1, int(target_cell_m / native / 2.0))
    out_h = max(1, int(win.height) // factor)
    out_w = max(1, int(win.width) // factor)
    arr = ds.read(1, window=win, out_shape=(out_h, out_w),
                  masked=True).astype("float32")
    transform = ds.window_transform(win) * rasterio.Affine.scale(
        int(win.width) / out_w, int(win.height) / out_h)
    return arr.filled(np.nan).astype("float32"), transform


def mosaic(sources: list[str], target_crs: str,
           bbox: tuple[float, float, float, float],
           resolution_m: float | None = None, *, source: str = ""
           ) -> tuple[Any, Any, str, list[bool]]:
    """Warp every source onto one grid, last-wins -> ``(array, transform, crs,
    painted)``, where ``painted`` flags per input source, in order, the ones that
    contributed at least one cell."""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    if not sources:
        raise MosaicEmpty(f"{source or 'this source'}: no tile to merge")

    dst_transform, width, height = target_grid(
        sources, target_crs, bbox, resolution_m=resolution_m, source=source)
    grid = np.full((height, width), np.nan, dtype="float32")
    painted = [False] * len(sources)
    target_cell_m = abs(dst_transform.a)

    with rasterio.Env(**VSICURL_ENV):
        for index, src in enumerate(sources):
            try:
                with rasterio.open(src) as ds:
                    values, src_transform = _windowed(ds, target_cell_m, bbox)
                    if values is None:
                        continue
                    values = np.where(
                        np.abs(values) >= np.float32(SENTINEL_ABS),
                        np.float32("nan"), values)
                    src_crs = ds.crs
            except Exception as exc:  # noqa: BLE001 - skip an unreadable source
                logger.warning("tile mosaic: skipping unreadable source %s: %s",
                               src, exc)
                continue

            warped = np.full((height, width), np.nan, dtype="float32")
            reproject(source=values, destination=warped,
                      src_transform=src_transform, src_crs=src_crs,
                      dst_transform=dst_transform, dst_crs=target_crs,
                      src_nodata=np.nan, dst_nodata=np.nan,
                      resampling=Resampling.bilinear)
            valid = ~np.isnan(warped)
            if valid.any():
                grid[valid] = warped[valid]
                painted[index] = True

    if not any(painted):
        raise MosaicEmpty(
            f"{source or 'this source'}: every one of {len(sources)} tiles was "
            f"empty, unreadable or outside {bbox!r}")
    return grid, dst_transform, target_crs, painted


def painted_fraction(array: Any) -> float:
    """The share of the AOI grid carrying a real value: the mosaic sits on a
    bbox-clipped grid, so every cell is an AOI cell and a NaN is a cell nobody
    painted. A measure of what was PAINTED, not of a delivered footprint."""
    import numpy as np

    grid = np.asarray(array, dtype="float64")
    if grid.size == 0:
        return 0.0
    return float(np.count_nonzero(np.isfinite(grid)) / grid.size)
