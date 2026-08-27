"""The MESH display face: a built mesh as the SMS ``.2dm`` MDAL opens.

Mesh is a data type on the one emission seam. The razor: geometry that feeds a
SOLVER is the mesh front's business and lives beside the mesher that built it;
geometry that feeds a SCREEN is emission's, and this is where it is written.

MDAL reads a ``.2dm`` directly as a mesh layer and turns the node z column into
its "Bed Elevation" dataset. The format carries no CRS, so the layer row's
``crs_authid`` is what names the coordinates the nodes are written in.
"""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np

__all__ = ["MESH_ELEMENT_TAG", "MeshDisplayError", "mesh_display_path", "write_2dm",
           "write_2dm_arrays"]

#: The SMS element tag for a cell of N nodes. A cell of any other arity has no
#: display face here and says so rather than being silently reshaped.
MESH_ELEMENT_TAG: Mapping[int, str] = {3: "E3T", 4: "E4Q"}


class MeshDisplayError(RuntimeError):
    """A mesh that cannot be given a display face, with the reason as a code."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def mesh_display_path(mesh: Any) -> str | None:
    """The display file the MESHER wrote itself, or ``None`` to write a ``.2dm``.

    A mesh whose cells an engine re-realizes carries no connectivity for the
    ``.2dm`` format to hold, so its mesher draws its own face - cell polygons -
    and names it here. Every node/cell mesh leaves this unset and takes the one
    writer above.
    """
    declared = dict(getattr(mesh, "meta", None) or {}).get("files") or {}
    path = declared.get("display_uri")
    return str(path) if path else None


def write_2dm(mesh: Any) -> str:
    """Write a built mesh as the ``.2dm`` text MDAL opens as a mesh layer.

    A bed-less mesh writes a zero node column because the format requires one;
    the artifact's ``has_bathymetry`` is what says whether an elevation was ever
    sampled.
    """
    points = np.asarray(mesh.points, dtype=float)
    bed = (np.zeros(points.shape[0], dtype=float) if mesh.bed is None
           else mesh.bed)
    return write_2dm_arrays(points, mesh.cells, bed)


def write_2dm_arrays(points: Any, cells: Any, z: Any) -> str:
    """Write ``(points, cells, z)`` arrays as ``.2dm`` text - nodes and cells 1-based.

    The array face, for a producer holding the geometry rather than a built mesh
    value. Coordinates are written in the units they arrive in.
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
