"""A nodal field on an unstructured mesh, painted onto a regular EPSG:4326 grid.

The element fill is the solver's own P1 representation, so a cell inside an
element carries exactly what the solver would report there; the node halo exists
for a caller that has no element table. Neither invents a value: a cell no
element covers, or past the halo, is nodata."""

from __future__ import annotations

import math
from typing import Any

__all__ = [
    "MAX_PX_PER_SIDE",
    "MAX_TOTAL_CELLS",
    "MIN_PX_PER_SIDE",
    "TARGET_GROUND_RES_M",
    "grid_shape",
    "rasterize_elements",
    "rasterize_nodes",
]

#: Target GROUND resolution (m/px). A river channel is tens of metres wide, so
#: ~10 m/px keeps a field a smooth ribbon rather than chunky specks.
TARGET_GROUND_RES_M: float = 10.0
MIN_PX_PER_SIDE: int = 128
MAX_PX_PER_SIDE: int = 2500
MAX_TOTAL_CELLS: int = 5_000_000


def grid_shape(bbox: Any, res_m: float) -> tuple[int, int]:
    """``(nrows, ncols)`` for a lon/lat bbox at a ground resolution, floored and capped."""
    min_lon, min_lat, max_lon, max_lat = bbox
    mean_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(mean_lat)), 1e-6)
    h_m = (max_lat - min_lat) * m_per_deg_lat
    w_m = (max_lon - min_lon) * m_per_deg_lon
    res = max(res_m, 1e-6)
    nrows = min(max(int(round(h_m / res)), MIN_PX_PER_SIDE), MAX_PX_PER_SIDE)
    ncols = min(max(int(round(w_m / res)), MIN_PX_PER_SIDE), MAX_PX_PER_SIDE)
    if nrows * ncols > MAX_TOTAL_CELLS:
        s = math.sqrt(MAX_TOTAL_CELLS / float(nrows * ncols))
        nrows = max(MIN_PX_PER_SIDE, int(nrows * s))
        ncols = max(MIN_PX_PER_SIDE, int(ncols * s))
    return nrows, ncols


def rasterize_nodes(lon, lat, vals, bbox, out_shape, clip_dist_deg, wet_floor=0.0):
    """Linear-interpolate scattered node values onto a regular 4326 grid, clipped.

    A cell past ``clip_dist_deg`` from any node is NaN; row 0 is NORTH."""
    # Without the clip, griddata fills the whole convex hull and paints the field
    # across meander cut-offs that carry no mesh.
    import numpy as np
    from scipy.interpolate import griddata
    from scipy.spatial import cKDTree

    nrows, ncols = int(out_shape[0]), int(out_shape[1])
    min_lon, min_lat, max_lon, max_lat = bbox
    gdx = (max_lon - min_lon) / ncols
    gdy = (max_lat - min_lat) / nrows
    xc = min_lon + (np.arange(ncols) + 0.5) * gdx
    yc = max_lat - (np.arange(nrows) + 0.5) * gdy  # north->south
    gx, gy = np.meshgrid(xc, yc)

    pts = np.column_stack([lon, lat])
    grid = griddata(pts, vals, (gx, gy), method="linear")
    tree = cKDTree(pts)
    dist, _ = tree.query(np.column_stack([gx.ravel(), gy.ravel()]), k=1)
    dist = dist.reshape(nrows, ncols)
    grid = np.asarray(grid, dtype="float64")
    grid[dist > clip_dist_deg] = np.nan
    grid[~np.isfinite(grid)] = np.nan
    grid[grid < wet_floor] = np.nan
    return grid


def _triangles(ikle):
    """The element table as TRIANGLES (a quad element splits into two)."""
    import numpy as np

    ikle = np.asarray(ikle, dtype="int64")
    if ikle.ndim != 2 or ikle.shape[0] == 0:
        return np.empty((0, 3), dtype="int64")
    if ikle.shape[1] == 3:
        return ikle
    if ikle.shape[1] == 4:
        return np.vstack([ikle[:, [0, 1, 2]], ikle[:, [0, 2, 3]]])
    return ikle[:, :3]


def rasterize_elements(lon, lat, ikle, vals, bbox, out_shape, wet_floor=0.0):
    """P1 (barycentric) interpolation of a nodal FEM field onto a regular grid.

    An element with ANY non-finite vertex is SKIPPED; row 0 is NORTH."""
    # The solution IS piecewise-linear over its own elements, so evaluating each
    # element's barycentric shape functions at the covered cell centres
    # reproduces the solver's representation exactly. For an open-water mesh
    # whose nodes stand kilometres apart, a nearest-node halo sized for a
    # channel publishes a lattice of isolated pixels instead of a field.
    import numpy as np

    nrows, ncols = int(out_shape[0]), int(out_shape[1])
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    gdx = (max_lon - min_lon) / ncols
    gdy = (max_lat - min_lat) / nrows
    xc = min_lon + (np.arange(ncols) + 0.5) * gdx
    yc = max_lat - (np.arange(nrows) + 0.5) * gdy      # north -> south

    x = np.asarray(lon, dtype="float64")
    y = np.asarray(lat, dtype="float64")
    v = np.asarray(vals, dtype="float64")
    tri = _triangles(ikle)
    if tri.size == 0 or tri.max() >= x.size:
        raise ValueError(
            f"element table does not index the {x.size} mesh nodes "
            f"(nelem={tri.shape[0]}, max index={tri.max() if tri.size else -1})")

    grid = np.full((nrows, ncols), np.nan, dtype="float64")
    x0, x1, x2 = x[tri[:, 0]], x[tri[:, 1]], x[tri[:, 2]]
    y0, y1, y2 = y[tri[:, 0]], y[tri[:, 1]], y[tri[:, 2]]
    v0, v1, v2 = v[tri[:, 0]], v[tri[:, 1]], v[tri[:, 2]]
    det = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)

    keep = np.isfinite(v0) & np.isfinite(v1) & np.isfinite(v2) & (np.abs(det) > 0.0)
    # cell-index window per element (half-cell offset: xc[j] = min_lon+(j+0.5)*gdx)
    txmin = np.minimum(np.minimum(x0, x1), x2)
    txmax = np.maximum(np.maximum(x0, x1), x2)
    tymin = np.minimum(np.minimum(y0, y1), y2)
    tymax = np.maximum(np.maximum(y0, y1), y2)
    j_lo = np.ceil((txmin - min_lon) / gdx - 0.5).astype("int64")
    j_hi = np.floor((txmax - min_lon) / gdx - 0.5).astype("int64")
    i_lo = np.ceil((max_lat - tymax) / gdy - 0.5).astype("int64")
    i_hi = np.floor((max_lat - tymin) / gdy - 0.5).astype("int64")
    np.clip(j_lo, 0, ncols - 1, out=j_lo)
    np.clip(j_hi, 0, ncols - 1, out=j_hi)
    np.clip(i_lo, 0, nrows - 1, out=i_lo)
    np.clip(i_hi, 0, nrows - 1, out=i_hi)

    eps = -1e-9
    for k in np.flatnonzero(keep):
        jl, jh, il, ih = int(j_lo[k]), int(j_hi[k]), int(i_lo[k]), int(i_hi[k])
        if jh < jl or ih < il:
            continue
        gx = xc[jl:jh + 1][None, :]
        gy = yc[il:ih + 1][:, None]
        d = det[k]
        l0 = ((y1[k] - y2[k]) * (gx - x2[k]) + (x2[k] - x1[k]) * (gy - y2[k])) / d
        l1 = ((y2[k] - y0[k]) * (gx - x2[k]) + (x0[k] - x2[k]) * (gy - y2[k])) / d
        l2 = 1.0 - l0 - l1
        inside = (l0 >= eps) & (l1 >= eps) & (l2 >= eps)
        if not inside.any():
            continue
        block = grid[il:ih + 1, jl:jh + 1]
        interp = l0 * v0[k] + l1 * v1[k] + l2 * v2[k]
        np.copyto(block, np.broadcast_to(interp, block.shape), where=inside)

    grid[~np.isfinite(grid)] = np.nan
    grid[grid < wet_floor] = np.nan
    return grid
