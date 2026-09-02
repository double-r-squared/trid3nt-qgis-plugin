"""What a mesh's NODES carry: their projection, a sampled field, a slope, a read.

Array primitives every mesher and every consumer of an accepted mesh needs, and
none of them belongs to one mesher: a solve works in METRES, a field is a raster
sampled AT the nodes, a terrain slope is read off the mesh's own piecewise-linear
surface rather than off a finer grid the run does not resolve, a boundary walk is
the contours the cells describe, an authored ``.2dm`` is parsed back into the same
three arrays - once from a path and once from an accepted mesh's own display face
- and the channel a chain measures along is read ONCE into one head-to-tail order.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from trid3nt_server.tools.processing._geometry_common import utm_epsg_for

__all__ = [
    "MeshNodeError",
    "boundary_contours",
    "node_slopes_from_mesh",
    "read_2dm_mesh",
    "read_accepted_mesh_nodes",
    "read_centerline_utm",
    "reproject_nodes_to_utm",
    "sample_raster_at_nodes",
    "tin_formats",
]

#: How far inside a raster's edge a node is sampled, in pixels: past the rim
#: there is no cell at all, and the rim cell itself is resampled from partial
#: source coverage. One and a half puts every sample on a whole cell.
_RIM_PIXELS = 1.5

#: Where the repo's shared TIN format writers live. They are the one place the
#: boundary walk and the orient/clean pass are written, and they are IMPORTED
#: from here rather than reimplemented beside every caller.
_TIN_FORMATS = "scripts/sandbox/oceanmesh"


def tin_formats() -> Any:
    """The repo's shared TIN format module, importable from the agent venv."""
    path = str(Path(__file__).resolve().parents[4] / _TIN_FORMATS)
    if path not in sys.path:
        sys.path.insert(0, path)
    import mesh_formats  # type: ignore

    return mesh_formats


def boundary_contours(cells: Any) -> list[list[int]]:
    """The mesh's boundary walks, each a closed run of node ids in walk order."""
    import numpy as np

    return tin_formats().extract_boundary_loops(np.asarray(cells, dtype=np.int64))


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


def sample_raster_at_nodes(raster_path: Any, points_lonlat: Any,
                           interp: str = "nearest") -> Any:
    """Sample a raster at (N,2) lon/lat nodes -> (N,) values, holes filled.

    Nodata becomes the finite mean rather than NaN: a field with holes in it is
    not a field a solver can start from, and a hole at one node would propagate a
    NaN through the whole free surface.

    A node on the raster's own RIM reads the nearest whole pixel instead. A mesh
    cut from an AOI puts nodes exactly on that AOI's corner coordinates, and the
    grid fetched for that AOI has nothing whole there: one row and one column past
    it the sample is the untagged zero, and the rim row and column themselves are
    resampled from partial source coverage, so both report sea level along two
    entire sides of a domain that is 18 m deep two pixels in. Neither reads as
    missing anywhere downstream - they read as real water, or real land.

    ``nearest`` returns a value the grid actually holds; ``bilinear`` reads
    between the four surrounding cell centres, which is what a mesh coarser than
    its raster wants.
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
        nodata = src.nodata
        if str(interp) == "bilinear":
            vals = _bilinear(src, xs, ys, nodata)
        else:
            vals = np.array(list(src.sample(list(zip(xs, ys)))),
                            dtype=float)[:, 0]
    if nodata is not None:
        vals[vals == nodata] = np.nan
    if np.isnan(vals).any():
        finite = vals[np.isfinite(vals)]
        vals[np.isnan(vals)] = float(finite.mean()) if finite.size else 0.0
    return vals


def _bilinear(src: Any, xs: Any, ys: Any, nodata: Any) -> Any:
    """The band read between its four surrounding cell centres, nodata held out.

    Nodata is lifted to NaN BEFORE the interpolation: averaging a fill value into
    its neighbours would spread a hole across four cells instead of leaving it to
    be filled once, honestly, by the caller.
    """
    import numpy as np
    from scipy.ndimage import map_coordinates

    band = src.read(1).astype(float)
    if nodata is not None:
        band[band == nodata] = np.nan
    inverse = ~src.transform
    cols, rows = inverse * (np.asarray(xs, dtype=float), np.asarray(ys, dtype=float))
    at = [rows - 0.5, cols - 0.5]
    valid = np.isfinite(band)
    value = map_coordinates(np.where(valid, band, 0.0), at, order=1,
                            mode="nearest")
    weight = map_coordinates(valid.astype(float), at, order=1, mode="nearest")
    # A stencil that touched a hole reports the hole rather than three quarters
    # of a value: the caller fills it once, honestly, with the rest.
    return np.where(weight > 0.999, value, np.nan)


def read_accepted_mesh_nodes(display_uri: str, *, utm_epsg: int | None = None
                             ) -> tuple[Any, Any, Any, Any]:
    """An accepted mesh's display face -> ``(points_utm, cells, bed, points_lonlat)``.

    The ``.2dm`` is the ONE readable record of the node numbering the geometry
    file carries, so a field written against these arrays lands on the nodes the
    solver has. ``points_lonlat`` is those same nodes projected back and is
    ``None`` unless a zone is named - a raster sampled at them is then sampled at
    this mesh's own nodes rather than at a second triangulation of the domain.
    """
    import tempfile

    import numpy as np

    from trid3nt_server.tools.cache import read_object_bytes_s3

    uri = str(display_uri)
    local = Path(tempfile.mkdtemp(prefix="mesh-nodes-")) / "mesh.2dm"
    local.write_bytes(read_object_bytes_s3(uri) if uri.startswith("s3://")
                      else Path(uri).read_bytes())
    points_utm, cells, bed = read_2dm_mesh(str(local))
    if utm_epsg is None:
        return points_utm, cells, bed, None
    from pyproj import Transformer

    lon, lat = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True).transform(
        points_utm[:, 0], points_utm[:, 1])
    return points_utm, cells, bed, np.column_stack([lon, lat])


def read_centerline_utm(source: Any, utm_epsg: int, *,
                        start_lonlat: Any = None) -> Any:
    """A channel source -> ONE head-to-tail (N,2) polyline in the mesh's metres.

    THE reading of a centerline. A navigated flowline arrives as many rows whose
    order in the document says nothing, so the parts are joined into one
    continuous line - the parts' own directions chain them, which is why a
    shuffled collection normalizes to the same line - and a document whose parts
    stay separate refuses rather than being read as a vertex heap.

    ``start_lonlat`` is the end the CHAIN knows is the head: the navigate seed the
    flowline was walked downstream from. With it the order is a fact rather than
    whichever way the merge came out; without it the merged direction stands.
    """
    import numpy as np
    from pyproj import Transformer
    from shapely.geometry import shape as _shape
    from shapely.ops import linemerge

    from trid3nt_server.tools.processing._geometry_common import (
        flatten_geometries, read_geometry_doc,
    )

    parts: list[Any] = []
    for geometry in flatten_geometries(read_geometry_doc(source)):
        if str(geometry.get("type") or "") not in ("LineString", "MultiLineString"):
            continue
        shape = _shape(geometry)
        parts += [p for p in getattr(shape, "geoms", [shape]) if not p.is_empty]
    if not parts:
        raise MeshNodeError(
            "MESH_CENTERLINE_NO_LINE",
            f"the channel source {source!r} carries no polyline, so there is no "
            "centerline to read.")
    merged = linemerge(parts) if len(parts) > 1 else parts[0]
    pieces = list(getattr(merged, "geoms", [merged]))
    if len(pieces) != 1:
        raise MeshNodeError(
            "MESH_CENTERLINE_NOT_CONTINUOUS",
            f"the {len(parts)} channel part(s) join into {len(pieces)} separate "
            "lines, so they describe a network rather than one reach and no "
            "head-to-tail order exists over them.")
    coords = np.asarray(pieces[0].coords, dtype=float)
    if start_lonlat is not None:
        head = np.asarray([float(start_lonlat[0]), float(start_lonlat[1])])
        if (np.hypot(*(coords[-1] - head)) < np.hypot(*(coords[0] - head))):
            coords = coords[::-1]
    tr = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    x, y = tr.transform(coords[:, 0], coords[:, 1])
    return np.column_stack([x, y]).astype(float)


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
