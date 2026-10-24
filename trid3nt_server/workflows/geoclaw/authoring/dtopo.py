"""The dtopo file a sheet's Fault composite names, written as topotype 3 by Clawpack.

Each stated subfault becomes a ``dtopotools.SubFault`` and the Okada vertical displacement
is summed by ``Fault.create_dtopography`` over the stated lon/lat grid. The file is
written by ``DTopography.write``. Every subfault must stand inside the sheet's level-1
box, and when the sheet states topofiles at least one node of the grid under the
subfaults' surface projection must stand on water.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Mapping

from ..errors import GeoclawError

logger = logging.getLogger("trid3nt_server.workflows.geoclaw.authoring.dtopo")

__all__ = ["DTOPO", "KINEMATIC_FIELDS", "PLACEMENTS", "SUBFAULT_FIELDS", "dtopo_axes", "finite_fault_subfaults", "moment_tensor_subfaults", "sift_subfaults", "write_dtopofiles"]

#: The key a composite's file entry carries its fault under.
DTOPO = "dtopo"

#: The ten values Okada needs; Clawpack's own attribute names.
SUBFAULT_FIELDS = ("longitude", "latitude", "depth", "strike", "dip", "rake", "slip",
                   "length", "width", "coordinate_specification")

#: What a kinematic rupture adds to each subfault: when it begins slipping and how long the slip takes, in seconds.
KINEMATIC_FIELDS = ("rupture_time", "rise_time")


def _refuse(code: str, message: str) -> None:
    raise GeoclawError(message, error_code=f"GEOCLAW_{code}")


def sift_subfaults(unit_sources: Mapping[str, Mapping[str, float]],
                   slips: Mapping[str, float]) -> list[dict[str, Any]]:
    """SIFT unit sources slipped by ``slips`` (metres, by unit-source name) -> subfault statements in Clawpack's own SubFault names.

    Kilometres become metres and NOAA's east longitude becomes west-negative, as
    ``dtopotools.SiftFault`` reads the same table; the rigidity is left to Clawpack."""
    missing = [name for name in slips if name not in unit_sources]
    if missing:
        _refuse("SIFT_UNSTATED", f"the slips name {', '.join(map(repr, missing))}, which the unit "
                f"sources read ({', '.join(sorted(unit_sources))}) do not hold.")
    return [{"longitude": (lon - 360.0 if (lon := row["longitude"]) > 180.0 else lon),
             "latitude": row["latitude"], "depth": row["depth_km"] * 1000.0, "strike": row["strike"],
             "dip": row["dip"], "rake": row["rake"], "slip": float(slip),
             "length": row["length_km"] * 1000.0, "width": row["width_km"] * 1000.0,
             "coordinate_specification": "noaa sift"}
            for name, slip in slips.items() for row in [unit_sources[name]]]


def finite_fault_subfaults(record: Mapping[str, Any], kinematic: bool = False) -> list[dict[str, Any]]:
    """The record ``fetch_finite_fault`` returns -> subfault statements in Clawpack's own SubFault names.

    Each published row is a subfault centre, so the specification is ``centroid``; kilometres
    become metres and the slip is the inversion's, with no rigidity or scaling applied.
    ``kinematic`` adds each row's published rupture and rise times, in seconds."""
    rows = record.get("subfaults") if isinstance(record, Mapping) else None
    if not isinstance(rows, (list, tuple)) or not rows:
        _refuse("FINITE_FAULT_UNSTATED", "the finite-fault record holds no subfault.")
    try:
        return [{"longitude": row["longitude"], "latitude": row["latitude"], "depth": row["depth_km"] * 1000.0,
                 "strike": row["strike"], "dip": row["dip"], "rake": row["rake"], "slip": float(row["slip_m"]),
                 "length": row["length_km"] * 1000.0, "width": row["width_km"] * 1000.0,
                 "coordinate_specification": "centroid",
                 **({"rupture_time": float(row["rupture_time_s"]), "rise_time": float(row["rise_time_s"])}
                    if kinematic else {})} for row in rows]
    except (KeyError, TypeError) as exc:
        _refuse("FINITE_FAULT_UNSTATED", f"a finite-fault subfault states no usable {exc}.")


#: Where a moment-tensor rupture sits: the solution's published centroid, or on the Slab2 interface at that centroid.
PLACEMENTS = ("centroid", "slab")


def moment_tensor_subfaults(record: Mapping[str, Any], *, solution: str, plane: int, relation: str, placement: str,
                            slab: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """The record ``fetch_moment_tensor`` returns -> the one subfault its named solution, plane and scaling relation make.

    Length and width come from ``relation`` at the solution's Mw (Clawpack's definition of the scalar moment), the slip
    is M0 / (mu L W) with Clawpack's own SubFault rigidity, and strike, dip and rake are the stated plane's. ``placement``
    'slab' takes the depth from the ``slab`` record at the published centroid instead of the tensor's."""
    from clawpack.geoclaw import dtopotools

    from ..helpers.scaling import rupture_dimensions, slip_for_moment

    held = record.get("solutions") if isinstance(record, Mapping) else None
    if not isinstance(held, (list, tuple)) or not held:
        _refuse("MOMENT_TENSOR_UNSTATED", "the moment-tensor record holds no solution.")
    chosen = [s for s in held if s.get("code") == solution]
    if not chosen:
        _refuse("MOMENT_TENSOR_SOLUTION", f"solution {solution!r} is none the event holds: {', '.join(str(s.get('code')) for s in held)}.")
    sol = chosen[0]
    if plane not in (1, 2):
        _refuse("MOMENT_TENSOR_PLANE", f"plane {plane!r} is neither 1 nor 2: the tensor publishes both nodal planes and does not say which ruptured.")
    if placement not in PLACEMENTS:
        _refuse("MOMENT_TENSOR_PLACEMENT", f"placement {placement!r} is none of {' or '.join(PLACEMENTS)}.")
    centroid = sol.get("centroid")
    if not centroid:
        _refuse("MOMENT_TENSOR_CENTROID", f"solution {solution!r} publishes no centroid, and the hypocentre is not a stand-in for it.")
    moment, nodal = float(sol["scalar_moment_nm"]), sol["planes"][plane - 1]
    mw = float(dtopotools.Mw(moment))
    length, width = rupture_dimensions(relation, mw)
    depth_km = float(centroid["depth_km"])
    if placement == "slab":
        from trid3nt_server.tools.fetchers import _slab2_grids as slab2

        if not isinstance(slab, Mapping):
            _refuse("MOMENT_TENSOR_PLACEMENT", "placement 'slab' reads the interface from the record fetch_slab_geometry returns: state slab.")
        try:
            depth_km = float(slab2.sample(slab2.blocks_from_record(slab), centroid["longitude"], centroid["latitude"])["depth_km"])
        except slab2.SlabError as exc:
            _refuse("MOMENT_TENSOR_SLAB", f"the published centroid is not on the slab record: {exc}")
    top_km = depth_km - width / 2000.0 * math.sin(math.radians(nodal["dip"]))
    if top_km <= 0.0:
        _refuse("MOMENT_TENSOR_SURFACE", f"the {width / 1000:.1f} km wide rupture centred at {depth_km:.1f} km on a {nodal['dip']:g} degree plane "
                f"reaches {top_km:.1f} km, above the surface an Okada subfault takes.")
    mu = float(dtopotools.SubFault().mu)
    return [{"longitude": centroid["longitude"], "latitude": centroid["latitude"], "depth": depth_km * 1000.0,
             "strike": nodal["strike"], "dip": nodal["dip"], "rake": nodal["rake"],
             "slip": slip_for_moment(moment, mu, length, width), "length": length, "width": width,
             "coordinate_specification": "centroid"}]


def dtopo_axes(subfaults: list[Mapping[str, Any]], spacing: float, buffer: float
               ) -> tuple[list[float], list[float]]:
    """The ``[first, last, nodes]`` of the lon and lat axes Clawpack's own ``Fault.create_dtopo_xy`` lays around these subfaults.

    ``spacing`` is the node spacing in degrees and ``buffer`` the margin in degrees around the rupture."""
    from clawpack.geoclaw import dtopotools

    subs = []
    for stated in subfaults:
        sub = dtopotools.SubFault()
        for field in SUBFAULT_FIELDS:
            setattr(sub, field, stated[field])
        sub.calculate_geometry()
        subs.append(sub)
    fault = dtopotools.Fault()
    fault.subfaults = subs
    x, y = fault.create_dtopo_xy(dx=spacing, buffer_size=buffer)
    return [float(x[0]), float(x[-1]), len(x)], [float(y[0]), float(y[-1]), len(y)]


def write_dtopofiles(sheet: Any, work: Path) -> dict[str, dict[str, Any]]:
    """Write every dtopo the sheet's composite named into ``work`` -> file name to its facts.

    The facts are the grid (nodes in x and y, times), the moment magnitude Clawpack
    computes from the subfaults and the rigidity it used for it. A kinematic rupture is
    written at each of its instants with every subfault's share of the slip at that instant."""
    facts: dict[str, dict[str, Any]] = {}
    for name, content in sheet.files.items():
        if isinstance(content, Mapping) and DTOPO in content:
            facts[name] = _written(content[DTOPO], sheet, work, name)
    return facts


def _written(spec: Mapping[str, Any], sheet: Any, work: Path, name: str) -> dict[str, Any]:
    import numpy as np
    from clawpack.geoclaw import dtopotools

    kinematic = spec.get("rupture_type") == "kinematic"
    faults = []
    for number, stated in enumerate(spec["subfaults"], 1):
        sub = dtopotools.SubFault()
        for field in SUBFAULT_FIELDS:
            setattr(sub, field, stated[field])
        if kinematic:
            for field in KINEMATIC_FIELDS:
                setattr(sub, field, stated[field])
            sub.rise_shape = spec["rise_shape"]
        try:
            sub.calculate_geometry()
        except ValueError as exc:
            _refuse("FAULT_SUBFAULT", f"subfault {number} has no geometry Clawpack takes: {exc}")
        faults.append(sub)
    footprints = _inside(sheet, faults)
    x = np.linspace(*spec["x"][:2], int(spec["x"][2]))
    y = np.linspace(*spec["y"][:2], int(spec["y"][2]))
    _wet(sheet, work, footprints, x, y)
    fault = dtopotools.Fault()
    fault.subfaults = faults
    if kinematic:
        fault.rupture_type = "kinematic"
    try:
        dtopo = fault.create_dtopography(x, y, list(spec["times"]))
    except ValueError as exc:
        _refuse("FAULT_OKADA", f"Clawpack refused the rupture: {exc}")
    dtopo.write(str(work / name), dtopo_type=3)
    mw = float(fault.Mw())
    logger.info("geoclaw dtopo %s: Mw %.3f from %d subfault(s), rigidity %s Pa", name, mw,
                len(faults), {s.mu for s in faults})
    return {"nx": len(x), "ny": len(y), "times": list(spec["times"]), "Mw": mw,
            "rigidity": sorted({float(s.mu) for s in faults})}


def _inside(sheet: Any, faults: list[Any]) -> list[Any]:
    """Each subfault's surface projection as a polygon, after checking it lies in the level-1 box."""
    from shapely.geometry import Polygon

    lower, upper = (sheet.filled.get(k) for k in ("CLAW_LOWER", "CLAW_UPPER"))
    if lower is None or upper is None:
        _refuse("FAULT_DOMAIN", "CLAW_LOWER and CLAW_UPPER are stated by nothing, so no "
                "subfault can be placed in the domain: state the level-1 box first.")
    west, south = lower.value[:2]
    east, north = upper.value[:2]
    polygons = []
    for number, sub in enumerate(faults, 1):
        lons = [c[0] for c in sub.corners]
        lats = [c[1] for c in sub.corners]
        if min(lons) < west or max(lons) > east or min(lats) < south or max(lats) > north:
            _refuse("FAULT_DOMAIN",
                    f"subfault {number} projects onto lon {min(lons):.3f}..{max(lons):.3f}, "
                    f"lat {min(lats):.3f}..{max(lats):.3f}, outside the level-1 box "
                    f"({west:g}, {south:g}, {east:g}, {north:g}): a rupture the domain does "
                    "not hold moves no water the run solves. Move the subfault or widen the box.")
        polygons.append(Polygon([(c[0], c[1]) for c in sub.corners]))
    return polygons


def _wet(sheet: Any, work: Path, polygons: list[Any], x: Any, y: Any) -> None:
    """Refuse a grid with no wet node under the subfaults, read off the sheet's topofiles.

    The finest file listed last wins at a node, as the engine takes it; a file not in the
    run directory is skipped here and left to the staged check. A sheet stating no
    topofile has nothing to read water from, so only the domain check runs."""
    import numpy as np
    import shapely
    from clawpack.geoclaw.topotools import Topography

    under = shapely.union_all(polygons)
    X, Y = np.meshgrid(x, y)
    inside = shapely.contains_xy(under, X, Y)
    if not inside.any():
        _refuse("FAULT_GRID", f"the dtopo grid ({len(x)} x {len(y)} nodes) holds no node "
                "under the subfaults' surface projection, so the rupture would write "
                "nothing: grid it finer or over the subfaults.")
    row = sheet.filled.get("TOPO_TOPOFILES")
    if row is None or not row.value:
        return
    level = sheet.filled.get("GEO_SEA_LEVEL")
    sea = float(level.value) if level is not None else float(
        sheet.body.MODULE_INPUT["GEO_SEA_LEVEL"].default)
    z = np.full(X.shape, np.nan)
    read = 0
    for topo_type, fname in row.value:
        path = work / fname
        if not path.exists():
            continue
        topo = Topography()
        topo.read(str(path), int(topo_type))
        read += 1
        tx, ty = np.asarray(topo.x), np.asarray(topo.y)
        dx, dy = (tx[1] - tx[0]), (ty[1] - ty[0])
        ix = np.rint((X - tx[0]) / dx).astype(int)
        iy = np.rint((Y - ty[0]) / dy).astype(int)
        have = (ix >= 0) & (ix < len(tx)) & (iy >= 0) & (iy < len(ty))
        z[have] = np.asarray(topo.Z)[iy[have], ix[have]]
    if not read:
        return
    if not (inside & (z < sea)).any():
        _refuse("FAULT_DRY",
                f"no node of the dtopo grid under the subfaults stands on water in the "
                f"run's topofiles ({int(inside.sum())} nodes under the footprint, "
                f"{int((inside & ~np.isnan(z)).sum())} of them covered, none below "
                f"{sea:g} m): the rupture lies over land or beyond the bed, so it "
                "would deform nothing the run floods. Move the fault or state the bed over it.")
