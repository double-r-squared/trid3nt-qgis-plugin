"""Shared in-worker bed-COG writer for the lake-datum TELEMAC wave modules.

The ARTEMIS (agitation) and TOMAWAC (wave_field) pipelines sample a NOAA Great
Lakes lake-datum bed at their grid nodes INSIDE the solver container, so the bed
never touches the agent-side router seam. This writes the sampled bed the solve
ran on as a small 4326 COG next to the result; the composer rides that object
through ``publish_raster_input_cog`` (no re-upload) as a role=context input. It
mirrors the ``telemac_river_dye_build.write_bed_cog`` treatment but takes node
lon/lat/z directly (the wave grids carry lon/lat, not a channel Transformer).
"""

from __future__ import annotations

import math
import os
from typing import Any

#: filename the supervisor uploads (via the manifest ``outputs``) + the composer
#: keys off (recorded as ``bed_cog`` in telemac_metrics.json).
BED_COG_FILENAME: str = "bed_bathymetry.tif"
#: pixel budget: the bed COG is a spot-check backdrop, not an analysis raster.
BED_COG_MAX_PX_PER_SIDE: int = 512
BED_COG_MIN_PX_PER_SIDE: int = 16


def write_bed_cog_lonlat(lon: Any, lat: Any, z: Any, path: str) -> dict[str, Any]:
    """Rasterize node bed elevations ``z`` at ``lon``/``lat`` (EPSG:4326) to a COG.

    Linearly interpolates the per-node bed onto a modest regular 4326 grid clipped
    to the sampled footprint (a cell whose nearest node is far is nodata, so
    griddata never paints the convex hull), and writes a tiled COG carrying the
    bed the solve ran on. Non-finite nodes (land / off-lake) are dropped before
    interpolation. Returns a metrics dict (``bed_cog`` filename + ``bed_cog_min_m``
    / ``bed_cog_max_m`` / ``bed_cog_px``). Raises on any failure -- the caller
    wraps it best-effort so a bed-COG hiccup never voids a CORRECT END solve.
    """
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds
    from scipy.interpolate import griddata
    from scipy.spatial import cKDTree

    lon = np.asarray(lon, dtype=float).ravel()
    lat = np.asarray(lat, dtype=float).ravel()
    z = np.asarray(z, dtype=float).ravel()
    finite = np.isfinite(lon) & np.isfinite(lat) & np.isfinite(z)
    if finite.sum() < 3:
        raise RuntimeError("bed COG: fewer than 3 finite nodes to rasterize")
    lon, lat, z = lon[finite], lat[finite], z[finite]

    min_lon, max_lon = float(lon.min()), float(lon.max())
    min_lat, max_lat = float(lat.min()), float(lat.max())
    span_lon = max(max_lon - min_lon, 1e-9)
    span_lat = max(max_lat - min_lat, 1e-9)
    mean_lat = 0.5 * (min_lat + max_lat)
    w_m = span_lon * 111_320.0 * max(math.cos(math.radians(mean_lat)), 1e-6)
    h_m = span_lat * 111_320.0
    if w_m >= h_m:
        ncols = BED_COG_MAX_PX_PER_SIDE
        nrows = int(round(BED_COG_MAX_PX_PER_SIDE * h_m / max(w_m, 1e-9)))
    else:
        nrows = BED_COG_MAX_PX_PER_SIDE
        ncols = int(round(BED_COG_MAX_PX_PER_SIDE * w_m / max(h_m, 1e-9)))
    nrows = int(np.clip(nrows, BED_COG_MIN_PX_PER_SIDE, BED_COG_MAX_PX_PER_SIDE))
    ncols = int(np.clip(ncols, BED_COG_MIN_PX_PER_SIDE, BED_COG_MAX_PX_PER_SIDE))

    gdx, gdy = span_lon / ncols, span_lat / nrows
    xc = min_lon + (np.arange(ncols) + 0.5) * gdx
    yc = max_lat - (np.arange(nrows) + 0.5) * gdy  # north -> south (COG row 0 = N)
    gx, gy = np.meshgrid(xc, yc)
    pts = np.column_stack([lon, lat])
    grid = griddata(pts, z, (gx, gy), method="linear")
    grid = np.asarray(grid, dtype="float32")
    # clip to the sampled footprint: a cell whose nearest node is > ~1.5 mean-cell
    # away is outside the meshed lake -> nodata (never paint the convex hull).
    tree = cKDTree(pts)
    dist, _ = tree.query(np.column_stack([gx.ravel(), gy.ravel()]), k=1)
    clip = 1.5 * float(max(gdx, gdy))
    grid[(dist.reshape(nrows, ncols) > clip)] = np.nan
    grid[~np.isfinite(grid)] = np.nan
    if not np.isfinite(grid).any():
        raise RuntimeError("bed COG: grid is entirely nodata after clipping")

    nodata = -9999.0
    out = np.where(np.isfinite(grid), grid, nodata).astype("float32")
    transform = from_bounds(min_lon, min_lat, max_lon, max_lat, ncols, nrows)
    profile = dict(
        driver="COG", dtype="float32", count=1, height=nrows, width=ncols,
        crs="EPSG:4326", transform=transform, nodata=nodata,
        compress="deflate", blocksize=256,
    )
    if os.path.exists(path):
        os.remove(path)
    try:
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(out, 1)
    except Exception:  # noqa: BLE001 -- some rasterio builds lack the COG driver
        profile.update(driver="GTiff", tiled=True, blockxsize=256, blockysize=256)
        profile.pop("blocksize", None)
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(out, 1)

    finite_vals = grid[np.isfinite(grid)]
    return {
        "bed_cog": os.path.basename(path),
        "bed_cog_min_m": round(float(finite_vals.min()), 3),
        "bed_cog_max_m": round(float(finite_vals.max()), 3),
        "bed_cog_px": [int(nrows), int(ncols)],
    }
