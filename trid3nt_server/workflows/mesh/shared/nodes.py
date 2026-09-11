"""What a mesh's NODES carry: their projection, a sampled field, a slope, a read.

The array primitives every mesher and every consumer of an accepted mesh needs,
none of them belonging to one mesher: a solve works in METRES, a field is a
raster sampled AT the nodes, a slope is read off the mesh's own surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from trid3nt_server.inputs.geometry import utm_epsg_for

__all__ = [
    "MeshNodeError",
    "accepted_mesh_nodes",
    "boundary_contours",
    "node_slopes_from_mesh",
    "read_2dm_mesh",
    "read_accepted_mesh_nodes",
    "read_centerline_utm",
    "reproject_nodes_to_utm",
    "sample_layer_at_nodes",
    "sample_raster_at_nodes",
    "tin_formats",
]

#: How far inside a raster's edge a node is sampled, in pixels: past the rim
#: there is no cell at all, and the rim cell itself is resampled from partial
#: source coverage. One and a half puts every sample on a whole cell.
_RIM_PIXELS = 1.5


def tin_formats() -> Any:
    """The shared TIN writers - the one home of the boundary walk and orient pass."""
    from trid3nt_server.workflows.mesh.shared.formats import mesh_formats

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

    The zone is the domain centroid's; a solve mesh is always in metres."""
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

    ``nearest`` returns a value the grid holds; ``bilinear`` interpolates."""
    import numpy as np
    import rasterio
    from rasterio.warp import transform as warp_transform

    pts = np.asarray(points_lonlat, dtype=float)
    with rasterio.open(raster_path) as src:
        xs, ys = warp_transform(
            "EPSG:4326", src.crs, pts[:, 0].tolist(), pts[:, 1].tolist())
        left, bottom, right, top = src.bounds
        dx, dy = (abs(v) for v in src.res)
        # A mesh cut from an AOI puts nodes exactly on that AOI's corner
        # coordinates, where the fetched grid has nothing whole: one row and one
        # column past it the sample is the untagged zero, and the rim row and
        # column themselves are resampled from partial source coverage, so both
        # report sea level along two entire sides of a domain that is metres deep
        # two pixels in - and neither reads as missing anywhere downstream.
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
        # Nodata becomes the finite mean rather than NaN: a field with holes in
        # it is not a field a solver can start from, and a hole at one node
        # would propagate a NaN through the whole free surface.
        finite = vals[np.isfinite(vals)]
        vals[np.isnan(vals)] = float(finite.mean()) if finite.size else 0.0
    return vals


def sample_layer_at_nodes(layer: Any, points_lonlat: Any,
                          interp: str = "nearest") -> Any:
    """A fetched raster layer sampled at (N,2) lon/lat nodes -> (N,) values.

    The layer's ``uri`` names the raster, in the store or on disk."""
    import tempfile

    from trid3nt_server.tools.cache import read_object_bytes_s3
    from trid3nt_server.inputs.layer_fields import layer_field

    uri = str(layer_field(layer, "uri") or "")
    if not uri:
        raise MeshNodeError(
            "MESH_LAYER_NO_RASTER",
            f"the layer {layer!r} names no raster uri to sample at the nodes.")
    local = Path(tempfile.mkdtemp(prefix="mesh-sample-")) / "layer.tif"
    local.write_bytes(read_object_bytes_s3(uri) if uri.startswith("s3://")
                      else Path(uri).read_bytes())
    try:
        return sample_raster_at_nodes(local, points_lonlat, interp=interp)
    finally:
        local.unlink(missing_ok=True)


def _bilinear(src: Any, xs: Any, ys: Any, nodata: Any) -> Any:
    """The band read between its four surrounding cell centres, nodata held out.

    Nodata is lifted to NaN BEFORE the interpolation, never averaged in."""
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

    ``points_lonlat`` is ``None`` unless ``utm_epsg`` names a zone."""
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


def accepted_mesh_nodes(mesh: Any) -> tuple[Any, Any, Any, Any]:
    """The mesh step's record -> ``(points_utm, cells, bed, points_lonlat)``.

    Read off the display face, the one readable record of the file's numbering;
    a record with no display face or no projected zone refuses by name."""
    utm_epsg = int(getattr(mesh.get("artifact"), "utm_epsg", 0) or 0)
    uri = str(mesh.get("display_uri") or "")
    if not uri or not utm_epsg:
        raise MeshNodeError(
            "MESH_NOT_ACCEPTED",
            "the accepted mesh carries no display face or no projected zone, so "
            "its nodes cannot be read; a mesh ask builds both.")
    return read_accepted_mesh_nodes(uri, utm_epsg=utm_epsg)


def read_centerline_utm(source: Any, utm_epsg: int, *,
                        start_lonlat: Any = None) -> Any:
    """A channel source -> ONE head-to-tail (N,2) polyline in the mesh's metres.

    Parts that stay separate refuse rather than read as a vertex heap."""
    import numpy as np
    from pyproj import Transformer
    from shapely.geometry import shape as _shape
    from shapely.ops import linemerge

    from trid3nt_server.inputs.geometry import (
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
    # The end the CHAIN knows is the head - the navigate seed the flowline was
    # walked downstream from - makes the order a fact rather than whichever way
    # the merge came out; without it the merged direction stands.
    if start_lonlat is not None:
        head = np.asarray([float(start_lonlat[0]), float(start_lonlat[1])])
        if (np.hypot(*(coords[-1] - head)) < np.hypot(*(coords[0] - head))):
            coords = coords[::-1]
    tr = Transformer.from_crs(4326, int(utm_epsg), always_xy=True)
    x, y = tr.transform(coords[:, 0], coords[:, 1])
    return np.column_stack([x, y]).astype(float)


def node_slopes_from_mesh(points_utm: Any, cells: Any, bed_elev: Any) -> Any:
    """Per-node terrain slope (m/m) from the mesh's OWN piecewise-linear bed.

    A node's slope is the mean of the exact per-triangle gradients."""
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

    ``ND`` and ``E3T`` rows, 1-based; nodes come back in id order, in metres."""
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
