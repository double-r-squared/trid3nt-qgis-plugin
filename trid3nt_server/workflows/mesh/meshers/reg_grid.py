"""The regular-grid mesher: a geographic extent plus a cell size -> the lattice.

Wraps the repo's regular-grid domain math; what it returns is the node lattice
and its quad cells, which is the same geometry a structured deck writes as an
origin plus cell counts. Its edits are re-derivations of that math, so the recipe
replays exactly.

A regular grid carries no bed: elevations arrive from a sampled raster, and a
zero-filled bed would read to a solver as ground at sea level.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Mapping

import numpy as np

from trid3nt_server.workflows.mesh.grid_geometry import RegularGrid, regular_grid_from_bbox
from trid3nt_server.workflows.mesh.meshers import (
    EditAction,
    Mesh,
    MeshField,
    apply_layer_edits_action,
    contained_extent,
    register_mesher,
    staged_coverage,
)

__all__ = ["REG_GRID", "build"]

_FIELDS = (
    MeshField("kind", types=(str,), choices=("structured_grid",),
              default="structured_grid",
              doc="structured_grid - the one kind a uniform lattice is"),
    MeshField("extent", types=(tuple, list), required=True,
              doc="(min_lon, min_lat, max_lon, max_lat) in EPSG:4326"),
    MeshField("resolution_m", types=(int, float), required=True,
              doc="target uniform cell size, in metres"),
)


def build(spec: Mapping[str, Any]) -> Mesh:
    """Build the lattice an ``(extent, resolution_m)`` ask describes."""
    return _lattice(regular_grid_from_bbox(
        tuple(float(v) for v in spec["extent"]), float(spec["resolution_m"])))


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


def _over_the_same_coverage(before: Mesh, after: Mesh) -> Mesh:
    """``after``, stating the coverage ``before`` was staged over.

    A re-derivation stages nothing, so the ground the inputs were fetched for is
    still the ground they were fetched for. Without this the coverage would
    collapse onto whatever box the last edit left, and the next extent change
    would be judged against a crop rather than against what is actually staged.
    """
    coverage = staged_coverage(before)
    if coverage is None:
        return after
    return dataclasses.replace(
        after, meta={**dict(after.meta), "staged_coverage": coverage})


def _set_resolution(mesh: Mesh, *, resolution_m: float) -> Mesh:
    return _over_the_same_coverage(mesh, build(
        {"extent": mesh.meta["extent"], "resolution_m": float(resolution_m)}))


def _set_extent(mesh: Mesh, *, extent: Any) -> Mesh:
    """Re-derive the lattice over a CROP of the staged coverage."""
    return _over_the_same_coverage(mesh, build(
        {"extent": contained_extent(mesh, extent, edit="set_extent"),
         "resolution_m": mesh.meta["resolution_m"]}))


REG_GRID = register_mesher(
    "reg_grid",
    build,
    actions=(
        EditAction(
            name="set_resolution", apply=_set_resolution,
            inputs={"resolution_m": MeshField(
                "resolution_m", types=(int, float), required=True,
                doc="the new uniform cell size, in metres")},
            doc="Re-derive the lattice at a different cell size."),
        EditAction(
            name="set_extent", apply=_set_extent,
            inputs={"extent": MeshField(
                "extent", types=(tuple, list), required=True,
                doc="the new (min_lon, min_lat, max_lon, max_lat); it must sit "
                    "INSIDE the coverage the mesh was staged over")},
            doc="Crop the lattice to a smaller extent inside the staged coverage."),
        apply_layer_edits_action(),
    ),
    fields=_FIELDS,
)
