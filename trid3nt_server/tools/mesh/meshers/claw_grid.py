"""The GeoClaw grid recipe: a level-1 box, refinement ratios and flagregions.

GeoClaw has no mesh; its grid is the level-1 lattice plus the ratios each finer
level divides it by and the regions that force or allow those levels. The built
Mesh is the level-1 lattice, and the recipe's facts ride in its probes."""

from __future__ import annotations

import dataclasses
import math
from typing import Any, Sequence

from trid3nt_server.tools.mesh.grid_geometry import M_PER_DEG_LAT, RegularGrid
from trid3nt_server.tools.mesh.meshers import (
    POST,
    Mesh,
    MeshOp,
    MeshToolError,
    OpNamespace,
    bind_ops,
    register_mesher,
)
from trid3nt_server.tools.mesh.meshers.reg_grid import lattice

__all__ = ["CLAW_GRID", "build", "flagregion"]

#: The probes key the grid facts travel under to the accepted artifact.
PROBE_KEY = "claw_grid"

#: Arc-seconds per degree.
_ARCSEC = 3600.0


def flagregion(mesh: Mesh, name: str, minlevel: int, maxlevel: int, t1: float,
               t2: float, extent: Sequence[float]) -> Mesh:
    """One Clawpack FlagRegion over a lon/lat rectangle -> the mesh carrying it.

    Levels minlevel..maxlevel are forced to at least minlevel and allowed up to
    maxlevel inside ``extent`` (min_lon, min_lat, max_lon, max_lat) between the
    run times t1 and t2, in seconds."""
    levels = int(mesh.meta["levels"])
    lo, hi = int(minlevel), int(maxlevel)
    if not 1 <= lo <= hi:
        _refuse("MESH_FLAGREGION_LEVELS",
                f"flagregion {name!r}: minlevel {minlevel!r} and maxlevel "
                f"{maxlevel!r} must satisfy 1 <= minlevel <= maxlevel.")
    if hi > levels:
        _refuse("MESH_FLAGREGION_LEVELS",
                f"flagregion {name!r} asks for maxlevel {hi}, above the "
                f"{levels} level(s) the refinement ratios "
                f"{list(mesh.meta['ratios'])} give; state one more ratio or "
                f"lower the maxlevel.")
    if not float(t1) <= float(t2):
        _refuse("MESH_FLAGREGION_TIMES",
                f"flagregion {name!r}: t1 {t1!r} is after t2 {t2!r}.")
    x1, y1, x2, y2 = (float(v) for v in extent)
    if not (x2 > x1 and y2 > y1):
        _refuse("MESH_FLAGREGION_EXTENT",
                f"flagregion {name!r}: extent {tuple(extent)!r} is not "
                "(min_lon, min_lat, max_lon, max_lat) with max above min.")
    record = {"name": str(name), "minlevel": lo, "maxlevel": hi,
              "t1": float(t1), "t2": float(t2), "extent": (x1, y1, x2, y2)}
    carried = [*mesh.meta.get("flagregions", ()), record]
    return dataclasses.replace(mesh, meta={**dict(mesh.meta),
                                           "flagregions": carried})


def build(recipe: Any) -> Mesh:
    """Build the level-1 lattice this recipe states and annotate it with the grid facts."""
    extent = recipe.extent
    if not isinstance(extent, (tuple, list)) or len(extent) != 4:
        _refuse("MESH_POLYGON_DOMAIN_UNSUPPORTED",
                "mesher 'claw_grid' takes a rectangular lon/lat extent "
                "(min_lon, min_lat, max_lon, max_lat): the level-1 grid is a box.")
    if recipe.resolution_m in (None, ""):
        _refuse("MESH_EDGE_UNSTATED",
                "mesher 'claw_grid' needs resolution_m, the level-1 cell edge "
                "in metres along a meridian; GeoClaw has no value for it.")
    ratios = _ratios(recipe.build.get("refinement_ratios"))
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in extent)
    if not (max_lon > min_lon and max_lat > min_lat):
        _refuse("MESH_EXTENT_DEGENERATE",
                f"extent {tuple(extent)!r} needs max_lon > min_lon and "
                "max_lat > min_lat.")
    step = float(recipe.resolution_m) / M_PER_DEG_LAT
    ncol = max(1, round((max_lon - min_lon) / step))
    nrow = max(1, round((max_lat - min_lat) / step))
    centre_lat = 0.5 * (min_lat + max_lat)
    m_lon = M_PER_DEG_LAT * max(0.01, math.cos(math.radians(centre_lat)))
    grid = RegularGrid(
        min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat,
        resolution_m=float(recipe.resolution_m), ncol=ncol, nrow=nrow,
        dlon=(max_lon - min_lon) / ncol, dlat=(max_lat - min_lat) / nrow,
        span_x_m=(max_lon - min_lon) * m_lon,
        span_y_m=(max_lat - min_lat) * M_PER_DEG_LAT,
        m_per_deg_lon=m_lon, m_per_deg_lat=M_PER_DEG_LAT)
    level1 = lattice(grid)
    mesh = Mesh(points=level1.points, cells=level1.cells,
                crs_authid=level1.crs_authid, bed=None,
                meta={**dict(level1.meta), "ratios": tuple(ratios),
                      "levels": len(ratios) + 1})
    for op in bind_ops(CLAW_GRID, recipe.ops):
        mesh = _run(op.op, mesh)
    return _with_facts(mesh, grid, ratios, recipe.resolution_m)


def _run(op: MeshOp, mesh: Mesh) -> Mesh:
    if op.fn != "flagregion":
        _refuse("MESH_OP_UNSUPPORTED",
                f"mesh_op({op.fn!r}) has no meaning on a GeoClaw grid recipe: "
                "its ops are flagregion entries, and the bed is the topofiles'.")
    try:
        return flagregion(mesh, **dict(op.kwargs))
    except TypeError as exc:
        raise MeshToolError(
            "MESH_OP_BAD_KWARGS",
            f"mesh_op('flagregion', ...): {exc}. It takes name, minlevel, "
            "maxlevel, t1, t2 and extent.") from None


def _ratios(value: Any) -> list[int]:
    if not isinstance(value, (list, tuple)):
        _refuse("MESH_RATIOS_UNSTATED",
                "mesher 'claw_grid' needs build={'refinement_ratios': [...]}: "
                "one integer per finer level, applied to x, y and t; GeoClaw "
                "has no value for it.")
    bad = [r for r in value if isinstance(r, bool) or not isinstance(r, int) or r < 1]
    if bad:
        _refuse("MESH_RATIOS_INVALID",
                f"refinement_ratios {list(value)!r}: {bad[0]!r} is not an "
                "integer of at least 1.")
    return [int(r) for r in value]


def _with_facts(mesh: Mesh, grid: RegularGrid, ratios: list[int],
                stated_m: float) -> Mesh:
    """The mesh with the grid facts the gate card quotes and authoring reads."""
    factor = math.prod(ratios)
    finest_deg = (grid.dlon / factor, grid.dlat / factor)
    regions = list(mesh.meta.get("flagregions", ()))
    counts = _level_counts(grid, ratios, regions)
    finest_m = (finest_deg[1] * M_PER_DEG_LAT,
                finest_deg[0] * grid.m_per_deg_lon)
    facts = {
        "lower": [grid.min_lon, grid.min_lat],
        "upper": [grid.max_lon, grid.max_lat],
        "num_cells": [grid.ncol, grid.nrow],
        "ratios": ratios,
        "levels": len(ratios) + 1,
        "level1_cell_deg": [grid.dlon, grid.dlat],
        "level1_cell_m": [grid.dlat * M_PER_DEG_LAT, grid.dlon * grid.m_per_deg_lon],
        "finest_cell_deg": list(finest_deg),
        "finest_cell_m": list(finest_m),
        "finest_cell_arcsec": [finest_deg[1] * _ARCSEC, finest_deg[0] * _ARCSEC],
        "cells_per_level": counts,
        "flagregions": regions,
    }
    finest = (f"{finest_m[0]:.1f} m meridian x {finest_m[1]:.1f} m parallel = "
              f"{finest_deg[1] * _ARCSEC:.2f} arc-seconds")
    notes = [f"finest cell {finest}"]
    rows = [
        ("grid", f"level-1 grid {grid.ncol} x {grid.nrow} cells of "
                 f"{facts['level1_cell_m'][0]:.0f} m meridian x "
                 f"{facts['level1_cell_m'][1]:.0f} m parallel; "
                 f"{len(ratios) + 1} level(s), ratios {ratios}"),
        ("finest_cell", finest),
        *[(f"level[{c['level']}]", f"{c['forced']} cells forced, up to "
           f"{c['allowed']} allowed") for c in counts[1:]],
    ]
    if abs(grid.dlat * M_PER_DEG_LAT - float(stated_m)) > 1e-6 * float(stated_m):
        notes.append(f"the stated {float(stated_m):g} m was fitted to the "
                     "extent as a whole number of cells")
    return dataclasses.replace(mesh, meta={
        **dict(mesh.meta), "edge_notes": notes, "card_rows": rows,
        "probes": {**dict(mesh.meta.get("probes") or {}), PROBE_KEY: facts}})


def _level_counts(grid: RegularGrid, ratios: list[int],
                  regions: list[dict[str, Any]]) -> list[dict[str, int]]:
    """Per level: cells the regions force and cells they allow, by area.

    Level 1 is the whole box. A region forces every level up to its minlevel
    and allows every level up to its maxlevel."""
    domain = (grid.min_lon, grid.min_lat, grid.max_lon, grid.max_lat)
    out = [{"level": 1, "forced": grid.ncol * grid.nrow,
            "allowed": grid.ncol * grid.nrow}]
    divisor = 1
    for level in range(2, len(ratios) + 2):
        divisor *= ratios[level - 2]
        cell = (grid.dlon / divisor) * (grid.dlat / divisor)
        forced = [r["extent"] for r in regions if r["minlevel"] >= level]
        allowed = [r["extent"] for r in regions if r["maxlevel"] >= level]
        out.append({
            "level": level,
            "forced": math.ceil(_union_area(forced, domain) / cell - 1e-9),
            "allowed": math.ceil(_union_area(allowed, domain) / cell - 1e-9)})
    return out


def _union_area(boxes: list[tuple[float, float, float, float]],
                domain: tuple[float, float, float, float]) -> float:
    """Area of the union of ``boxes`` clipped to ``domain``, in square degrees."""
    clipped = []
    for x1, y1, x2, y2 in boxes:
        c = (max(x1, domain[0]), max(y1, domain[1]),
             min(x2, domain[2]), min(y2, domain[3]))
        if c[2] > c[0] and c[3] > c[1]:
            clipped.append(c)
    xs = sorted({x for c in clipped for x in (c[0], c[2])})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        spans = sorted((c[1], c[3]) for c in clipped
                       if c[0] <= left and c[2] >= right)
        top = None
        for lo, hi in spans:
            if top is None or lo > top:
                area += (right - left) * (hi - lo)
                top = hi
            elif hi > top:
                area += (right - left) * (hi - top)
                top = hi
    return area


def _refuse(code: str, message: str) -> None:
    raise MeshToolError(code, message)


CLAW_GRID = register_mesher(
    "claw_grid",
    build,
    kinds=("amr_grid",),
    namespaces=(OpNamespace(origin="clawpack", phase=POST,
                            names=("flagregion",)),),
    build_params=("refinement_ratios",),
    default_ops=(),
)
