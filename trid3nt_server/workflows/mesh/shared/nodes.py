"""What a mesh's NODES carry: their projection, a sampled field, a slope, a read.

Four array primitives every mesher and every consumer of an accepted mesh needs,
and none of them belongs to one mesher: a solve works in METRES, a bed is a
raster sampled AT the nodes, a terrain slope is read off the mesh's own
piecewise-linear surface rather than off a finer grid the run does not resolve,
and an authored ``.2dm`` is parsed back into the same three arrays.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trid3nt_server.tools.processing._geometry_common import utm_epsg_for

__all__ = [
    "MeshNodeError",
    "fit_downstream_bed",
    "node_slopes_from_mesh",
    "read_2dm_mesh",
    "reproject_nodes_to_utm",
    "sample_raster_at_nodes",
]

#: How far inside a raster's edge a node is sampled, in pixels: past the rim
#: there is no cell at all, and the rim cell itself is resampled from partial
#: source coverage. One and a half puts every sample on a whole cell.
_RIM_PIXELS = 1.5


class MeshNodeError(RuntimeError):
    """A node array could not be read; carries an open-set ``error_code``."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def reproject_nodes_to_utm(points_lonlat: Any) -> tuple[Any, int]:
    """Project (N,2) lon/lat nodes to the local UTM zone -> ``(points_m, epsg)``.

    A shallow-water solver works in METRES - the momentum equations, the friction
    law, the CFL time step and a normal-depth outlet boundary all are - while a
    mesher's output may be degrees, so the solve mesh MUST be projected. The zone
    is the domain centroid's.
    """
    import numpy as np
    from pyproj import Transformer

    pts = np.asarray(points_lonlat, dtype=float)
    epsg = utm_epsg_for(float(np.mean(pts[:, 0])), float(np.mean(pts[:, 1])))
    tr = Transformer.from_crs(4326, epsg, always_xy=True)
    x, y = tr.transform(pts[:, 0], pts[:, 1])
    return np.column_stack([x, y]).astype(float), int(epsg)


def sample_raster_at_nodes(raster_path: Any, points_lonlat: Any) -> Any:
    """Sample a raster at (N,2) lon/lat nodes -> (N,) values, holes filled.

    Nodata becomes the finite mean rather than NaN: a bed with holes in it is not
    a bed a solver can start from, and a hole at one node would propagate a NaN
    through the whole free surface.

    A node on the raster's own RIM reads the nearest whole pixel instead. A mesh
    cut from an AOI puts nodes exactly on that AOI's corner coordinates, and the
    grid fetched for that AOI has nothing whole there: one row and one column past
    it the sample is the untagged zero, and the rim row and column themselves are
    resampled from partial source coverage, so both report sea level along two
    entire sides of a domain that is 18 m deep two pixels in. Neither reads as
    missing anywhere downstream - they read as real water, or real land.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import transform as warp_transform

    pts = np.asarray(points_lonlat, dtype=float)
    with rasterio.open(raster_path) as src:
        xs, ys = warp_transform(
            "EPSG:4326", src.crs, pts[:, 0].tolist(), pts[:, 1].tolist())
        left, bottom, right, top = src.bounds
        dx, dy = (abs(v) for v in src.res)
        xs = np.clip(np.asarray(xs, dtype=float),
                     left + _RIM_PIXELS * dx, right - _RIM_PIXELS * dx)
        ys = np.clip(np.asarray(ys, dtype=float),
                     bottom + _RIM_PIXELS * dy, top - _RIM_PIXELS * dy)
        vals = np.array(list(src.sample(list(zip(xs, ys)))), dtype=float)[:, 0]
        nodata = src.nodata
    if nodata is not None:
        vals[vals == nodata] = np.nan
    if np.isnan(vals).any():
        finite = vals[np.isfinite(vals)]
        vals[np.isnan(vals)] = float(finite.mean()) if finite.size else 0.0
    return vals


def fit_downstream_bed(points_utm: Any, centerline_utm: Any, sampled_z: Any, *,
                       min_slope: float, max_slope: float
                       ) -> tuple[Any, dict[str, Any]]:
    """A monotone downstream bed fitted to a sampled DEM -> ``(bed, stats)``.

    A real canyon DEM is the SURFACE - rim, vegetation and water together - and
    along a thalweg it is noisy enough to run uphill between adjacent nodes. A
    shallow-water solve on a bed that runs uphill ponds instead of flowing, so the
    fit is what the solve gets: project every node onto the centerline for an
    along-channel distance, fit ``z ~ z0 - slope * s`` over the nodes that sampled,
    clamp the slope into the stated band, and lay a clean plane from a robust
    upstream level.

    The clamp is the enforcement, and both numbers travel: ``measured_slope`` is
    what the terrain said and ``enforced_slope`` is what the solve got, so a bed
    that was overruled says so rather than presenting the overrule as the DEM.
    """
    import numpy as np

    z_raw = np.asarray(sampled_z, dtype=float)
    s_node = _along_channel_distance(points_utm, centerline_utm)
    valid = np.isfinite(z_raw)
    if not valid.any():
        raise MeshNodeError(
            "MESH_BED_UNSAMPLED",
            f"the bed raster covers none of the {z_raw.size} mesh nodes (every "
            "sample is nodata), so no downstream bed can be fitted from it.")
    design = np.column_stack([np.ones(int(valid.sum())), s_node[valid]])
    coef, *_ = np.linalg.lstsq(design, z_raw[valid], rcond=None)
    measured = float(-coef[1])  # positive = downhill
    slope = float(np.clip(measured, min_slope, max_slope))
    top = float(np.nanpercentile(z_raw[valid], 20))
    bed = top - slope * s_node
    return bed, {
        "dem_min": float(np.nanmin(z_raw)), "dem_max": float(np.nanmax(z_raw)),
        "n_dem_nan": int((~valid).sum()),
        "measured_slope": measured, "enforced_slope": slope,
        "bed_top_m": top, "bed_drop_m": float(slope * s_node.max()),
        "reach_len_m": float(s_node.max()),
    }


def _along_channel_distance(points_utm: Any, centerline_utm: Any) -> Any:
    """Distance along ``centerline_utm`` of each node's nearest point on it."""
    import numpy as np

    pts = np.asarray(points_utm, dtype=float)
    line = np.asarray(centerline_utm, dtype=float)
    a, b = line[:-1], line[1:]
    seg = b - a
    length2 = np.maximum((seg ** 2).sum(axis=1), 1e-12)
    cum = np.concatenate([[0.0], np.cumsum(np.sqrt(length2))])
    # (n_nodes, n_segments) projection parameter, clamped to the segment.
    delta = pts[:, None, :] - a[None, :, :]
    t = np.clip((delta * seg[None, :, :]).sum(axis=2) / length2[None, :], 0.0, 1.0)
    foot = a[None, :, :] + t[:, :, None] * seg[None, :, :]
    nearest = np.argmin(((pts[:, None, :] - foot) ** 2).sum(axis=2), axis=1)
    rows = np.arange(pts.shape[0])
    return cum[nearest] + t[rows, nearest] * np.sqrt(length2[nearest])


def node_slopes_from_mesh(points_utm: Any, cells: Any, bed_elev: Any) -> Any:
    """Per-node terrain slope (m/m) from the mesh's OWN piecewise-linear bed.

    The bed is linear over each triangle, so its gradient is exact per element;
    a node's slope is the mean over the elements that touch it. Read off the mesh
    rather than re-sampled from a raster because the mesh IS the discretization
    the solver sees - a slope taken at a finer scale would correct a curve number
    for terrain the run does not resolve.
    """
    import numpy as np

    pts = np.asarray(points_utm, dtype=float)
    tri = np.asarray(cells, dtype=np.int64)
    z = np.asarray(bed_elev, dtype=float)
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    x1, y1 = pts[a, 0], pts[a, 1]
    x2, y2 = pts[b, 0], pts[b, 1]
    x3, y3 = pts[c, 0], pts[c, 1]
    det = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
    # A zero-area triangle carries no gradient; it contributes nothing rather
    # than an infinity that would poison every node it touches.
    safe = np.where(np.abs(det) > 0.0, det, np.nan)
    dzdx = ((y2 - y3) * (z[a] - z[c]) + (y3 - y1) * (z[b] - z[c])) / safe
    dzdy = ((x3 - x2) * (z[a] - z[c]) + (x1 - x3) * (z[b] - z[c])) / safe
    grad = np.sqrt(dzdx ** 2 + dzdy ** 2)
    total = np.zeros(pts.shape[0], dtype=float)
    count = np.zeros(pts.shape[0], dtype=float)
    finite = np.isfinite(grad)
    for column in (a, b, c):
        np.add.at(total, column[finite], grad[finite])
        np.add.at(count, column[finite], 1.0)
    return np.where(count > 0.0, total / np.maximum(count, 1.0), 0.0)


def read_2dm_mesh(twodm_path: str) -> tuple[Any, Any, Any]:
    """Parse an SMS ``.2dm`` -> ``(points (N,2), cells (M,3) 0-based, z (N,))``.

    The inverse of the display face's ``.2dm`` writer: ``ND id x y z`` node rows and
    ``E3T id n1 n2 n3 mat`` triangle rows, both 1-based. Nodes come back in id
    order; coordinates are the mesh's native metres (the artifact's ``utm_epsg``
    names the CRS).
    """
    import numpy as np

    nodes: dict[int, tuple[float, float, float]] = {}
    tris: list[tuple[int, int, int]] = []
    for line in Path(twodm_path).read_text().splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "ND" and len(parts) >= 5:
            nodes[int(parts[1])] = (float(parts[2]), float(parts[3]), float(parts[4]))
        elif parts[0] in ("E3T", "E3L") and len(parts) >= 5:
            tris.append((int(parts[2]), int(parts[3]), int(parts[4])))
    if not nodes or not tris:
        raise MeshNodeError(
            "MESH_SUPPLIED_UNREADABLE",
            f"2dm mesh {twodm_path} parsed to {len(nodes)} nodes / {len(tris)} "
            "elements; expected a MESH2D ND/E3T body.")
    order = sorted(nodes)
    remap = {nid: i for i, nid in enumerate(order)}
    points = np.array([[nodes[n][0], nodes[n][1]] for n in order], dtype=float)
    z = np.array([nodes[n][2] for n in order], dtype=float)
    cells = np.array([[remap[a], remap[b], remap[c]] for a, b, c in tris],
                     dtype=np.int64)
    return points, cells, z
