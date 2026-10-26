"""The GeoClaw grid recipe: a level-1 box, refinement ratios and flagregions.

GeoClaw has no mesh; its grid is the level-1 lattice plus the ratios each finer
level divides it by and the regions that force or allow those levels. The built
Mesh is the level-1 lattice, and the recipe's facts ride in its probes."""

from __future__ import annotations

import dataclasses
import math
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

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

#: The fields a ruled rectangle states, Clawpack's RuledRectangle names plus the file it is written to.
_RULED_FIELDS = ("ixy", "s", "lower", "upper", "method", "filename")

#: Arc-seconds per degree.
_ARCSEC = 3600.0

# Wall seconds per simulated second = _WALL_S_PER_WORK * W / level-1 cell (m), where W sums, per level, the
# cells the regions allow times the product of the ratios down to that level (the steps that level takes
# per level-1 step). Measured on this box through the image with the 7-level Copalis recipe: 244.9 s for
# 2400 s at an 8 arcmin level 1 gives 2.93e-4, about 3000 s for 3600 s at 4 arcmin gives 3.04e-4. A run of a
# few hundred cells is dominated by fixed costs (chile2010: 41 s measured, 5 s here), so this reads low there.
_WALL_S_PER_WORK = 3.0e-4

#: The basis both cards state beside the estimate.
WALL_BASIS = ("counting every cell the regions allow; scaled from the 7-level Copalis runs (2400 s in 245 s at "
              "8 arcmin, 3600 s in about 50 min at 4 arcmin) - a guide to the cost of the finer levels, not a limit")


def flagregion(mesh: Mesh, name: str, minlevel: int, maxlevel: int, t1: float,
               t2: float, extent: Sequence[float] | None = None, with_rupture: bool = False,
               ruled: Mapping[str, Any] | None = None) -> Mesh:
    """One Clawpack FlagRegion over a lon/lat rectangle or a ruled rectangle -> the mesh carrying it.

    Levels minlevel..maxlevel are forced to at least minlevel and allowed up to
    maxlevel inside ``extent`` (min_lon, min_lat, max_lon, max_lat), or inside ``ruled``, Clawpack's
    RuledRectangle stated by its own fields: ixy (1 or 'x': s is lon and lower/upper are lat; 2 or 'y':
    the reverse), s (strictly increasing), lower and upper (one per s, lower at most upper), method
    (0 piecewise constant, 1 piecewise linear) and filename (the file the run directory holds it in).
    The window is the run times t1 to t2, in seconds. ``with_rupture`` lengthens the window to the end of the run's
    stated rupture when that is later than t2; the run's keywords state the result."""
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
    if (extent is None) == (ruled is None):
        _refuse("MESH_FLAGREGION_EXTENT",
                f"flagregion {name!r} states exactly one of extent and ruled.")
    record = {"name": str(name), "minlevel": lo, "maxlevel": hi,
              "t1": float(t1), "t2": float(t2), "with_rupture": bool(with_rupture)}
    if ruled is not None:
        record.update(_ruled(str(name), ruled))
    else:
        x1, y1, x2, y2 = (float(v) for v in extent)
        if not (x2 > x1 and y2 > y1):
            _refuse("MESH_FLAGREGION_EXTENT",
                    f"flagregion {name!r}: extent {tuple(extent)!r} is not "
                    "(min_lon, min_lat, max_lon, max_lat) with max above min.")
        record["extent"] = (x1, y1, x2, y2)
    carried = [*mesh.meta.get("flagregions", ()), record]
    return dataclasses.replace(mesh, meta={**dict(mesh.meta),
                                           "flagregions": carried})


def _ruled(name: str, spec: Mapping[str, Any]) -> dict[str, Any]:
    """A stated ruled rectangle -> its record: the fields as Clawpack's RuledRectangle holds them, its bounding extent and its polygon."""
    import numpy as np
    from clawpack.amrclaw.region_tools import RuledRectangle

    unknown = sorted(set(spec) - set(_RULED_FIELDS))
    missing = sorted(set(_RULED_FIELDS) - set(spec))
    if unknown or missing:
        _refuse("MESH_FLAGREGION_RULED",
                f"flagregion {name!r}: ruled states {list(_RULED_FIELDS)}; unknown {unknown}, missing {missing}.")
    ixy, method = spec["ixy"], spec["method"]
    if ixy not in (1, 2, "x", "y") or method not in (0, 1):
        _refuse("MESH_FLAGREGION_RULED",
                f"flagregion {name!r}: ixy is 1, 'x', 2 or 'y' and method is 0 or 1; got ixy {ixy!r}, method {method!r}.")
    s, lower, upper = (np.array([float(v) for v in spec[key]]) for key in ("s", "lower", "upper"))
    if not (len(s) >= 2 and len(s) == len(lower) == len(upper)):
        _refuse("MESH_FLAGREGION_RULED",
                f"flagregion {name!r}: s, lower and upper are equal lists of at least two values.")
    if np.diff(s).min() <= 0 or np.any(lower > upper):
        _refuse("MESH_FLAGREGION_RULED",
                f"flagregion {name!r}: s must increase strictly and lower must not exceed upper.")
    if not spec["filename"] or not isinstance(spec["filename"], str) or "/" in spec["filename"]:
        _refuse("MESH_FLAGREGION_RULED", f"flagregion {name!r}: filename is a bare file name, got {spec['filename']!r}.")
    rule = RuledRectangle()
    ixy = 1 if ixy in (1, "x") else 2
    rule.ixy, rule.method, rule.s, rule.lower, rule.upper = ixy, int(method), s, lower, upper
    x1, x2, y1, y2 = rule.bounding_box()
    xs, ys = rule.vertices()
    return {"extent": (float(x1), float(y1), float(x2), float(y2)),
            "ruled": {"ixy": ixy, "method": int(method), "s": s.tolist(), "lower": lower.tolist(),
                      "upper": upper.tolist(), "filename": spec["filename"]},
            "polygon": [(float(x), float(y)) for x, y in zip(xs, ys)]}


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
            "maxlevel, t1, t2, extent or ruled, and with_rupture.") from None


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
    work = sum(c["allowed"] * math.prod(ratios[:i]) for i, c in enumerate(counts))
    facts["wall_s_per_sim_s"] = _WALL_S_PER_WORK * work / facts["level1_cell_m"][0]
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
    rows.append(("wall_estimate", f"about {facts['wall_s_per_sim_s'] * 3600.0 / 60.0:.0f} min of wall per simulated hour "
                 f"on the box that measured it, {WALL_BASIS}"))
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
        forced = [_shape(r) for r in regions if r["minlevel"] >= level]
        allowed = [_shape(r) for r in regions if r["maxlevel"] >= level]
        out.append({
            "level": level,
            "forced": math.ceil(_union_area(forced, domain) / cell - 1e-9),
            "allowed": math.ceil(_union_area(allowed, domain) / cell - 1e-9)})
    return out


def _shape(region: Mapping[str, Any]) -> Any:
    """The ground a region covers: its extent box, or its ruled rectangle's polygon."""
    from shapely.geometry import Polygon, box

    return Polygon(region["polygon"]) if "polygon" in region else box(*region["extent"])


def _union_area(shapes: list[Any], domain: tuple[float, float, float, float]) -> float:
    """Area of the union of ``shapes`` clipped to ``domain``, in square degrees."""
    from shapely.geometry import box
    from shapely.ops import unary_union

    return unary_union(shapes).intersection(box(*domain)).area if shapes else 0.0


def _refuse(code: str, message: str) -> None:
    raise MeshToolError(code, message)


# The op's own signature is its schema: a literal word in `name` is a label, not the name of a run input.
_OPS = SimpleNamespace(__name__="claw_grid ops", __all__=("flagregion",), flagregion=flagregion)

CLAW_GRID = register_mesher(
    "claw_grid",
    build,
    kinds=("amr_grid",),
    namespaces=(OpNamespace(origin="clawpack", phase=POST, module=_OPS),),
    build_params=("refinement_ratios",),
    default_ops=(),
)
