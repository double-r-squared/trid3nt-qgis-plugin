"""The regular-grid mesher: a geographic extent plus a cell size -> the lattice.

CONFORMS to the same surface every mesher does, with the smallest possible
registration: no library namespace of its own (the lattice IS the repo's own
regular-grid domain math, reached by the role adapter rather than named by an
op), and a near-empty default recipe. The shared primitives ride along, so a
lattice that wants a bed says ``mesh_op("set_bed", source=...)`` exactly as an
unstructured domain does.

What it returns is the node lattice and its quad cells, which is the same
geometry a structured deck writes as an origin plus cell counts. A regular grid
carries no bed of its own: elevations arrive from a sampled raster, and a
zero-filled bed would read to a solver as ground at sea level.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from trid3nt_server.workflows.mesh.grid_geometry import (
    RegularGrid, regular_grid_from_bbox,
)
from trid3nt_server.workflows.mesh.meshers import (
    Mesh,
    MeshToolError,
    register_mesher,
)

__all__ = ["REG_GRID", "build"]

#: The cell size an ask that declares none gets, in metres.
_DEFAULT_RESOLUTION_M = 100.0


def build(recipe: Any) -> Mesh:
    """Build the lattice this recipe's extent and resolution describe."""
    extent = recipe.extent
    if not isinstance(extent, (tuple, list)):
        # A lattice IS its origin, cell size and row/column counts - that is what
        # a structured deck writes and what every consumer of this mesh reads back
        # out of the meta. Dropping the cells outside a polygon would leave
        # something no longer describable that way, so the narrowing is refused
        # here and escalates to the mesher that meshes an interior.
        raise MeshToolError(
            "MESH_POLYGON_DOMAIN_UNSUPPORTED",
            "mesher 'reg_grid' takes a rectangular extent: a regular lattice is "
            "an origin plus cell counts, and masking it to a polygon leaves a "
            "geometry a structured deck cannot write. Mesh the polygon interior "
            "with mesher='om2d', or pass the polygon's bounding box.",
            escalation={"tool": "build_mesh",
                        "overrides": {"mesher": "om2d", "extent": extent}})
    mesh = _lattice(regular_grid_from_bbox(
        tuple(float(v) for v in extent),
        float(recipe.resolution_m or _DEFAULT_RESOLUTION_M)))
    return _with_ops(mesh, recipe)


def _with_ops(mesh: Mesh, recipe: Any) -> Mesh:
    """Run the recipe's ops over the lattice, in their declared order.

    Every op a reg_grid recipe can name is a shared primitive running on the
    host, so this is the whole of its execution: there is no library to shell.
    """
    from trid3nt_server.workflows.mesh.inputs import op_input
    from trid3nt_server.workflows.mesh.meshers import bind_ops

    for op in bind_ops(REG_GRID, recipe.ops):
        mesh = op.fn(mesh, **{name: op_input(value)
                              for name, value in op.kwargs.items()})
    return mesh


def _lattice(grid: RegularGrid) -> Mesh:
    lons = grid.min_lon + np.arange(grid.ncol + 1, dtype=float) * grid.dlon
    lats = grid.min_lat + np.arange(grid.nrow + 1, dtype=float) * grid.dlat
    xx, yy = np.meshgrid(lons, lats)
    points = np.column_stack([xx.ravel(), yy.ravel()])
    stride = grid.ncol + 1
    rows, cols = np.meshgrid(np.arange(grid.nrow), np.arange(grid.ncol),
                             indexing="ij")
    sw = (rows * stride + cols).ravel().astype(np.int64)
    # counter-clockwise from the south-west corner
    cells = np.column_stack([sw, sw + 1, sw + stride + 1, sw + stride])
    return Mesh(
        points=points, cells=cells, crs_authid="EPSG:4326", bed=None,
        meta={"extent": (grid.min_lon, grid.min_lat, grid.max_lon, grid.max_lat),
              "resolution_m": grid.resolution_m,
              "ncol": grid.ncol, "nrow": grid.nrow,
              "dlon": grid.dlon, "dlat": grid.dlat,
              "m_per_deg_lon": grid.m_per_deg_lon,
              "m_per_deg_lat": grid.m_per_deg_lat})


REG_GRID = register_mesher(
    "reg_grid",
    build,
    kinds=("structured_grid",),
    # No default ops: a lattice at the one size word is the whole of what an
    # undeclared ask asked for, and a bed it did not name is a bed it does not
    # have.
    default_ops=(),
)
