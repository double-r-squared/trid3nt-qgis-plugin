"""The MESH display faces MDAL opens: a mesh file, and a dataset beside one.

MDAL reads a ``.2dm`` directly and turns the node z column into its "Bed Elevation" dataset. The format carries no CRS,
so the layer row's ``crs_authid`` names the node coordinates. A per-node value no mesh file carries is written as an
SMS ASCII dataset and loaded onto the mesh it was measured over.

A mesh of polygon cells - Voronoi, quadtree, any vertex count a triangle or
quad tag cannot state - has no ``.2dm`` face: it is written as a CF-UGRID
netCDF file, carrying cell-valued dataset groups over time in the same file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

__all__ = ["MESH_ELEMENT_TAG", "MeshDisplayError", "mesh_display_path",
           "write_ascii_dataset", "write_2dm", "write_2dm_arrays",
           "write_ugrid"]

#: The SMS element tag for a cell of N nodes; any other arity has no display face and says so.
MESH_ELEMENT_TAG: Mapping[int, str] = {3: "E3T", 4: "E4Q"}


class MeshDisplayError(RuntimeError):
    """A mesh that cannot be given a display face, with the reason as a code."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def mesh_display_path(mesh: Any) -> str | None:
    """The display file the MESHER wrote itself, or ``None`` to write a ``.2dm``.

    A mesh whose cells an engine re-realizes carries no connectivity ``.2dm`` holds.
    """
    declared = dict(getattr(mesh, "meta", None) or {}).get("files") or {}
    path = declared.get("display_uri")
    return str(path) if path else None


def write_2dm(mesh: Any) -> str:
    """Write a built mesh as the ``.2dm`` text MDAL opens as a mesh layer.
    A bed-less mesh writes a zero node column because the format requires one;
    ``has_bathymetry`` is what says whether an elevation was ever sampled.
    """
    points = np.asarray(mesh.points, dtype=float)
    bed = (np.zeros(points.shape[0], dtype=float) if mesh.bed is None
           else mesh.bed)
    return write_2dm_arrays(points, mesh.cells, bed)


def write_2dm_arrays(points: Any, cells: Any, z: Any) -> str:
    """Write ``(points, cells, z)`` arrays as ``.2dm`` text - nodes and cells 1-based.

    Coordinates are written in the units they arrive in.
    """
    pts = np.asarray(points, dtype=float)
    cel = np.asarray(cells, dtype=np.int64)
    zz = np.asarray(z, dtype=float)
    nodes_per_cell = int(cel.shape[1]) if cel.ndim == 2 else 0
    tag = MESH_ELEMENT_TAG.get(nodes_per_cell)
    if tag is None:
        raise MeshDisplayError(
            "MESH_DISPLAY_UNSUPPORTED_CELL",
            f"a {nodes_per_cell}-node cell has no .2dm element tag "
            f"(supported: {sorted(MESH_ELEMENT_TAG)}).")
    lines = ["MESH2D"]
    for i, row in enumerate(cel + 1, start=1):
        lines.append(f"{tag} {i} " + " ".join(str(int(v)) for v in row) + " 1")
    for i, (x, y) in enumerate(pts, start=1):
        zi = float(zz[i - 1]) if i - 1 < zz.size else 0.0
        lines.append(f"ND {i} {x:.6f} {y:.6f} {zi:.6f}")
    return "\n".join(lines) + "\n"


def write_ascii_dataset(values: Any, *, name: str, nodes: int, cells: int,
                        t: float = 0.0) -> str:
    """One value per node as the SMS ASCII dataset text MDAL loads onto a mesh.

    ``nodes`` and ``cells`` are the counts of the mesh this is measured over;
    MDAL refuses the file against a mesh of any other size.
    """
    import numpy as np

    column = np.asarray(values, dtype="float64").ravel()
    if column.size != int(nodes):
        raise MeshDisplayError(
            "MESH_DATASET_NODE_COUNT",
            f"{name!r} carries {column.size} values over a mesh of "
            f"{int(nodes)} nodes; MDAL binds a dataset node for node.")
    rows = "\n".join(f"{float(v):.6g}" for v in column)
    return (f'DATASET\nOBJTYPE "mesh2d"\nBEGSCL\nND {int(nodes)}\n'
            f'NC {int(cells)}\nNAME "{name}"\nTS 0 {float(t):.6f}\n'
            f"{rows}\nENDDS\n")


def write_ugrid(mesh: Any, path: str | Path, *,
                datasets: Mapping[str, Sequence[tuple[float, Sequence[float]]]
                                  ] | None = None,
                reference_time: str | None = None) -> str:
    """Write a polygon-cell mesh as a CF-UGRID netCDF file MDAL opens.

    Cells may have any vertex count: the connectivity pads shorter rows with
    the fill value. ``datasets`` are cell-valued groups over ONE time axis - a
    name to ``(seconds, values)`` pairs, one value per cell, every group at the
    same instants - counted from ``reference_time`` (ISO 8601), which a dataset
    requires. A painted bed becomes a node-centered ``bed_elevation`` variable,
    which MDAL surfaces as its Bed Elevation dataset. Returns the path written.
    """
    import netCDF4

    pts = np.asarray(mesh.points, dtype=float)
    rows = [np.asarray(row, dtype=np.int64) for row in mesh.cells]
    n_nodes, n_faces = int(pts.shape[0]), len(rows)
    max_n = max((int(r.size) for r in rows), default=0)
    if not rows or any(int(r.size) < 3 for r in rows):
        raise MeshDisplayError(
            "MESH_UGRID_EMPTY_CELL",
            "a UGRID face needs cells of at least three vertices; got "
            f"{[int(r.size) for r in rows[:5]]}.")
    geographic = str(mesh.crs_authid).upper() == "EPSG:4326"
    conn = np.full((n_faces, max_n), -1, dtype=np.int64)
    for face, row in enumerate(rows):
        conn[face, : int(row.size)] = row
    instants = _instants(datasets, reference_time) if datasets else None

    dest = netCDF4.Dataset(str(path), "w", format="NETCDF3_CLASSIC")
    try:
        dest.createDimension("nMesh2D_nodes", n_nodes)
        dest.createDimension("nMesh2D_faces", n_faces)
        dest.createDimension("max_nfaces", max_n)
        topo = dest.createVariable("Mesh2D", "i4")
        topo.setncattr("topology_dimension", 2)
        topo.setncattr("cf_role", "mesh_topology")
        topo.setncattr("node_coordinates", "Mesh2D_node_x Mesh2D_node_y")
        topo.setncattr("face_dimension", "nMesh2D_faces")
        topo.setncattr("face_node_connectivity", "Mesh2D_face_nodes")
        nx = dest.createVariable("Mesh2D_node_x", "f8", ("nMesh2D_nodes",))
        nx.setncattr("units", "degrees_east" if geographic else "m")
        if geographic:
            nx.setncattr("standard_name", "projection_x_coordinate")
        nx[:] = pts[:, 0]
        ny = dest.createVariable("Mesh2D_node_y", "f8", ("nMesh2D_nodes",))
        ny.setncattr("units", "degrees_north" if geographic else "m")
        if geographic:
            ny.setncattr("standard_name", "projection_y_coordinate")
        ny[:] = pts[:, 1]
        # MDAL reads the connectivity faces-first; the transposed order surfaces it as a dataset and drops every time step.
        fn = dest.createVariable("Mesh2D_face_nodes", "i4",
                                 ("nMesh2D_faces", "max_nfaces"),
                                 fill_value=-1)
        fn.setncattr("cf_role", "face_node_connectivity")
        fn.setncattr("start_index", 0)
        fn.setncattr("long_name", "vertex node indices of each face")
        fn[:] = conn
        if getattr(mesh, "has_bed", False) and mesh.bed is not None:
            bed = dest.createVariable("Mesh2D_bed", "f8", ("nMesh2D_nodes",))
            bed.setncattr("standard_name", "bed_elevation")
            bed.setncattr("long_name", "Bed Elevation")
            bed[:] = np.asarray(mesh.bed, dtype=float)
        if datasets:
            dest.createDimension("time", None)
            clock = dest.createVariable("time", "f8", ("time",))
            clock.setncattr("standard_name", "time")
            # MDAL reads only "<unit> since YYYY-MM-DD HH:MM:SS": bare seconds read as hours, an ISO "T...Z" anchor rescales.
            clock.setncattr("units", f"seconds since {instants[0]}")
            clock[:] = instants[1]
            for name, series in datasets.items():
                variable = dest.createVariable(
                    _identifier(name), "f8", ("time", "nMesh2D_faces"),
                    fill_value=np.float64("nan"))
                variable.setncattr("units", "1")
                variable.setncattr("mesh", "Mesh2D")
                variable.setncattr("location", "face")
                for step, (t, values) in enumerate(series):
                    column = np.asarray(values, dtype=float).ravel()
                    if column.size != n_faces:
                        raise MeshDisplayError(
                            "MESH_DATASET_FACE_COUNT",
                            f"{name!r} at t={float(t)} carries {column.size} "
                            f"values over a mesh of {n_faces} faces; MDAL binds "
                            "a dataset face for face.")
                    variable[step, :] = column
        dest.setncattr("Conventions", "CF-1.8, UGRID-1.0")
    finally:
        dest.close()
    return str(path)


def _instants(datasets: Mapping[str, Sequence[tuple[float, Sequence[float]]]],
              reference_time: str | None) -> tuple[str, list[float]]:
    """The anchor as MDAL reads it and the one set of instants every group shares."""
    from datetime import datetime, timezone

    if not reference_time:
        raise MeshDisplayError(
            "MESH_DATASET_UNANCHORED",
            "a dataset over time is counted from an instant, and none was "
            "given; state the run's moment or its solve's start.")
    at = datetime.fromisoformat(str(reference_time).replace("Z", "+00:00"))
    if at.tzinfo is not None:
        at = at.astimezone(timezone.utc).replace(tzinfo=None)
    axes = {name: [float(t) for t, _values in series]
            for name, series in datasets.items()}
    first = next(iter(axes.values()))
    if any(axis != first for axis in axes.values()):
        raise MeshDisplayError(
            "MESH_DATASET_TIMES",
            f"the groups {sorted(axes)} are at different instants; one file "
            "carries one time axis, so each axis is its own file.")
    return at.strftime("%Y-%m-%d %H:%M:%S"), first


def _identifier(name: str) -> str:
    """``name`` as a netCDF-safe variable name, or a refusal naming it."""
    text = str(name).strip().replace(" ", "_")
    if not text or not (text[0].isalpha() or text[0] == "_"):
        raise MeshDisplayError(
            "MESH_UGRID_DATASET_NAME",
            f"a dataset name must start with a letter or underscore; got "
            f"{name!r}.")
    if not all(ch.isalnum() or ch == "_" for ch in text):
        raise MeshDisplayError(
            "MESH_UGRID_DATASET_NAME",
            f"a dataset name may hold letters, digits and underscores only; "
            f"got {name!r}.")
    return text
