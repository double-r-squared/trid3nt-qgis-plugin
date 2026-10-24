"""The GeoClaw wrapper over the engine-neutral module surface.

Where the dictionary lives and the one body the engine exposes; the fill holds
this dictionary, and Clawpack's own writer judges what the authoring writes
(setrun's Data classes). One module word, "geoclaw": every rundata group is a
keyword prefix on the same body, and what a solved run writes is a later leg's
to pin.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from trid3nt_contracts.common import Origin
from trid3nt_contracts.payload_warning import ParamSheetRow

from ...runtime.module import Module
from ...runtime.sheet import Sheet
from ..errors import GeoclawError

__all__ = ["GeoclawModule", "fault", "finite_fault", "fixed_grids", "initial_surface", "module_input_dir", "sift_fault",
           "topography"]

#: What one Topography statement may carry: the surface, and the offset onto the run's frame.
_SURFACE = ("surface", "offset")

#: What one Fault statement carries: the subfaults and the dtopo grid they are summed on.
_FAULT = ("subfaults", "x", "y", "times")


def module_input_dir() -> Path:
    """Where the committed module inputs live."""
    return Path(__file__).resolve().parent / "module_input"


def _refuse(code: str, message: str) -> None:
    raise GeoclawError(message, error_code=f"GEOCLAW_{code}")


def topography(value: Any, *, run: Mapping[str, Any]
               ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The bed ladder as topofiles: Topography([{surface, offset}, ...]), coarse to fine.

    Each surface is a bed (a raster or a case layer) read on the run's ``vertical_frame``
    through the bed's own offset path, so a surface on another zero needs its measured
    ``offset`` or refuses. Every surface becomes one topotype-3 lon/lat topofile and one
    TOPO_TOPOFILES row; the engine takes the finest file at each point, so nothing is
    merged or filled."""
    from trid3nt_server.inputs.bed import bed
    from trid3nt_server.inputs.geometry import source_uri

    surfaces = list(value) if isinstance(value, (list, tuple)) else [value]
    if not surfaces:
        _refuse("TOPOGRAPHY_UNSTATED", "Topography states no surface; it takes the bed "
                "surfaces coarse to fine, each a raster or a case layer.")
    if run.get("TOPO_TOPOFILES") is not None:
        _refuse("TOPOGRAPHY_TOPOFILES_STATED",
                "TOPO_TOPOFILES is stated beside Topography: Topography states the "
                "topofile list, so remove the keyword or the composite.")
    frame = str(run.get("vertical_frame") or "").strip()
    if not frame:
        _refuse("TOPOGRAPHY_FRAME_UNSTATED",
                "the run states no vertical_frame, so no surface can be read on the "
                "run's zero: state the frame the run counts elevations from.")
    rows: list[list[Any]] = []
    files: dict[str, Any] = {}
    for number, item in enumerate(surfaces, 1):
        surface, offset = item, None
        if isinstance(item, Mapping):
            extra = sorted(set(item) - set(_SURFACE))
            if extra:
                _refuse("TOPOGRAPHY_UNSTATED",
                        f"Topography surface {number} states {', '.join(extra)}; a "
                        f"surface takes {', '.join(_SURFACE)}.")
            surface, offset = item.get("surface"), item.get("offset")
        if surface is None:
            _refuse("TOPOGRAPHY_UNSTATED",
                    f"Topography surface {number} states no surface to read.")
        read = bed(surface, frame=frame, offset=offset,
                   label=f"topography surface {number}")
        if read.is_depth:
            _refuse("TOPOGRAPHY_DEPTH",
                    f"Topography surface {number} is a depth in metres, which is no "
                    "grid a topofile holds: hand it the bed raster.")
        name = f"topo_{number:02d}.tt3"
        rows.append([3, name])
        files[name] = {"topofile": str(source_uri(read.source)), "frame": frame}
    return {"TOPO_TOPOFILES": rows}, files


def fault(value: Any, *, run: Mapping[str, Any]
          ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The seafloor rupture as a dtopo file: Fault({subfaults, x, y, times}), every value stated.

    ``subfaults`` is a list of mappings carrying Clawpack's own SubFault names (longitude,
    latitude, depth, strike, dip, rake, slip, length, width, coordinate_specification) in
    degrees, metres and the run's lon/lat; ``x`` and ``y`` are ``[first, last, nodes]``
    of the dtopo grid and ``times`` its one or two instants. Clawpack's Okada model sums
    the subfaults into one topotype-3 dtopo file and one DTOPO_DTOPOFILES row; the
    magnitude is computed from the slip, never stated, and nothing is scaled or defaulted."""
    if not isinstance(value, Mapping):
        _refuse("FAULT_UNSTATED", "Fault takes a mapping {subfaults, x, y, times}: the "
                "subfaults and the dtopo grid they are summed on are all stated.")
    extra = sorted(set(value) - set(_FAULT))
    if extra:
        _refuse("FAULT_UNSTATED", f"Fault states {', '.join(extra)}; it takes {', '.join(_FAULT)}.")
    absent = [key for key in _FAULT if value.get(key) is None]
    if absent:
        _refuse("FAULT_UNSTATED", f"Fault states no {', '.join(absent)}: the rupture and the "
                "grid it is written on are stated, the engine has no value for them.")
    if run.get("DTOPO_DTOPOFILES") is not None:
        _refuse("FAULT_DTOPOFILES_STATED",
                "DTOPO_DTOPOFILES is stated beside Fault: Fault states the dtopo file "
                "list, so remove the keyword or the composite.")
    if run.get("GEO_COORDINATE_SYSTEM") != 2:
        _refuse("FAULT_COORDINATES", "Fault writes a lon/lat dtopo, so GEO_COORDINATE_SYSTEM "
                "must be stated 2 beside it.")
    from ..authoring.dtopo import SUBFAULT_FIELDS

    subfaults = value["subfaults"]
    if not isinstance(subfaults, (list, tuple)) or not subfaults:
        _refuse("FAULT_UNSTATED", "Fault states no subfault; it takes a list of them.")
    stated = []
    for number, item in enumerate(subfaults, 1):
        if not isinstance(item, Mapping):
            _refuse("FAULT_SUBFAULT", f"Fault subfault {number} is not a mapping.")
        extra = sorted(set(item) - set(SUBFAULT_FIELDS))
        if extra:
            _refuse("FAULT_SUBFAULT", f"Fault subfault {number} states {', '.join(extra)}; a "
                    f"subfault takes {', '.join(SUBFAULT_FIELDS)}.")
        absent = [f for f in SUBFAULT_FIELDS if item.get(f) is None]
        if absent:
            _refuse("FAULT_SUBFAULT", f"Fault subfault {number} states no {', '.join(absent)}: "
                    "every value of the rupture is stated or refused, none is scaled or defaulted.")
        for field in SUBFAULT_FIELDS[:-1]:
            if isinstance(item[field], bool) or not isinstance(item[field], (int, float)) \
                    or not math.isfinite(item[field]):
                _refuse("FAULT_SUBFAULT", f"Fault subfault {number} states {field} "
                        f"{item[field]!r}, which is no finite number.")
        if not isinstance(item["coordinate_specification"], str):
            _refuse("FAULT_SUBFAULT", f"Fault subfault {number} states a coordinate_specification "
                    "that is no string.")
        stated.append({field: item[field] for field in SUBFAULT_FIELDS})
    grid = {}
    for axis in ("x", "y"):
        lo, hi, nodes = _triple(value[axis], axis)
        grid[axis] = [lo, hi, nodes]
    times = value["times"]
    if not isinstance(times, (list, tuple)) or not times or not all(
            isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t)
            for t in times):
        _refuse("FAULT_GRID", "Fault times is a list of the instants the dtopo is written at.")
    name = "dtopo_01.tt3"
    return ({"DTOPO_DTOPOFILES": [[3, name]]},
            {name: {"dtopo": {"subfaults": stated, **grid, "times": [float(t) for t in times]}}})


#: What one SiftFault statement carries: the unit sources read, the slip on each, the dtopo grid's spacing and margin in degrees, and its instants.
_SIFT = ("unit_sources", "slips", "spacing", "buffer", "times")


def sift_fault(value: Any, *, run: Mapping[str, Any]
               ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The rupture of a SIFT scenario as a dtopo file: SiftFault({unit_sources, slips, spacing, buffer, times}).

    ``unit_sources`` is the record ``fetch_sift_unit_sources`` returns for the unit sources the scenario
    names, ``slips`` the metres each slips by name, ``spacing`` and ``buffer`` the dtopo node spacing and
    the margin around the rupture in degrees, ``times`` the instants it is written at. Each unit source
    becomes a Fault subfault and the grid is the one Clawpack's ``create_dtopo_xy`` lays around them.
    No slip anywhere is no rupture: empty ``slips`` writes no dtopo."""
    if not isinstance(value, Mapping):
        _refuse("SIFT_UNSTATED", "SiftFault takes a mapping {unit_sources, slips, spacing, buffer, times}.")
    extra = sorted(set(value) - set(_SIFT))
    if extra:
        _refuse("SIFT_UNSTATED", f"SiftFault states {', '.join(extra)}; it takes {', '.join(_SIFT)}.")
    if isinstance(value.get("slips"), Mapping) and not value["slips"]:
        return {}, {}
    absent = [key for key in _SIFT if value.get(key) is None]
    if absent:
        _refuse("SIFT_UNSTATED", f"SiftFault states no {', '.join(absent)}: the unit sources read, the slip "
                "on each and the dtopo grid are all stated, the engine has no value for them.")
    table = value["unit_sources"]
    table = table.get("unit_sources") if isinstance(table, Mapping) else None
    slips = value["slips"]
    if not isinstance(table, Mapping) or not isinstance(slips, Mapping) or not slips:
        _refuse("SIFT_UNSTATED", "SiftFault takes the record fetch_sift_unit_sources returns and a mapping "
                "of unit-source name to slip in metres.")
    from ..authoring.dtopo import dtopo_axes, sift_subfaults

    subfaults = sift_subfaults(table, slips)
    x, y = dtopo_axes(subfaults, float(value["spacing"]), float(value["buffer"]))
    return fault({"subfaults": subfaults, "x": x, "y": y, "times": value["times"]}, run=run)


#: What one FiniteFault statement carries: the finite-fault record read, the dtopo grid's spacing and margin in degrees, and its instants.
_FINITE = ("finite_fault", "spacing", "buffer", "times")


def finite_fault(value: Any, *, run: Mapping[str, Any]
                 ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """A real earthquake's published slip as a dtopo file: FiniteFault({finite_fault, spacing, buffer, times}).

    ``finite_fault`` is the record ``fetch_finite_fault`` returns; each of its subfaults becomes a Fault subfault
    at the slip the inversion states, and ``spacing``, ``buffer`` and ``times`` are as SiftFault's. A rupture
    another composite already wrote refuses: one run has one rupture."""
    if not isinstance(value, Mapping):
        _refuse("FINITE_FAULT_UNSTATED", "FiniteFault takes a mapping {finite_fault, spacing, buffer, times}.")
    extra = sorted(set(value) - set(_FINITE))
    if extra:
        _refuse("FINITE_FAULT_UNSTATED", f"FiniteFault states {', '.join(extra)}; it takes {', '.join(_FINITE)}.")
    absent = [key for key in _FINITE if value.get(key) is None]
    if absent:
        _refuse("FINITE_FAULT_UNSTATED", f"FiniteFault states no {', '.join(absent)}: the record read and the "
                "dtopo grid are all stated, the engine has no value for them.")
    if run.get("DTOPO_DTOPOFILES") is not None:
        _refuse("FINITE_FAULT_RUPTURE_STATED", "DTOPO_DTOPOFILES is already stated (by Fault, SiftFault or the "
                "keyword): FiniteFault is the run's one rupture, so state only one of them.")
    from ..authoring.dtopo import dtopo_axes, finite_fault_subfaults

    subfaults = finite_fault_subfaults(value["finite_fault"])
    x, y = dtopo_axes(subfaults, float(value["spacing"]), float(value["buffer"]))
    return fault({"subfaults": subfaults, "x": x, "y": y, "times": value["times"]}, run=run)


#: What one FixedGrids statement carries: the extent the products cover and their cadence.
_GRIDS = ("extent", "interval", "tstart_max", "dt_check", "arrival_tol", "interp_method")


def fixed_grids(value: Any, *, run: Mapping[str, Any]
                ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The fixed grids the products are read from: FixedGrids({extent, interval, tstart_max, dt_check, arrival_tol, interp_method}).

    One fgmax grid and one fgout grid, both on the cells of the FINEST level of the stated grid recipe that
    cover ``extent`` whole, so each node reads one finest cell; the fgout frames run from t = 0 to
    CLAW_TFINAL every ``interval`` seconds, frame 0 being the initial sea the overland products are masked by.
    ``tstart_max`` starts the maxima, ``dt_check`` and ``arrival_tol`` are fgmax's own, and the monitored level is the finest."""
    if not isinstance(value, Mapping):
        _refuse("GRIDS_UNSTATED", "FixedGrids takes a mapping " + "{" + ", ".join(_GRIDS) + "}.")
    absent = [key for key in _GRIDS if value.get(key) is None]
    extra = sorted(set(value) - set(_GRIDS))
    if absent or extra:
        _refuse("GRIDS_UNSTATED", f"FixedGrids states {', '.join(extra) or 'nothing extra'} beyond and no "
                f"{', '.join(absent) or 'missing value'}: it takes {', '.join(_GRIDS)}, all stated.")
    for key in ("FGMAX_FGMAX_GRIDS", "FGOUT_FGOUT_GRIDS"):
        if run.get(key) is not None:
            _refuse("GRIDS_STATED", f"{key} is stated beside FixedGrids: FixedGrids states the grid lists, "
                    "so remove the keyword or the composite.")
    facts = ((getattr(run.get("mesh", {}).get("artifact"), "probes", None) or {}).get("claw_grid")
             if isinstance(run.get("mesh"), Mapping) else None)
    tfinal = run.get("CLAW_TFINAL")
    if facts is None or tfinal is None:
        _refuse("GRIDS_UNSTATED", "FixedGrids sits on the accepted grid recipe's finest level and the run's "
                "CLAW_TFINAL: the mesh is built and CLAW_TFINAL stated before the grids are.")
    west, south, east, north = (float(v) for v in getattr(value["extent"], "bbox", value["extent"]))
    (lx, ly), (dx, dy), levels = facts["lower"], facts["finest_cell_deg"], int(facts["levels"])
    x1, x2 = lx + math.floor((west - lx) / dx + 1e-9) * dx, lx + math.ceil((east - lx) / dx - 1e-9) * dx
    y1, y2 = ly + math.floor((south - ly) / dy + 1e-9) * dy, ly + math.ceil((north - ly) / dy - 1e-9) * dy
    nx, ny = round((x2 - x1) / dx), round((y2 - y1) / dy)
    interval = float(value["interval"])
    if interval <= 0 or float(tfinal) <= 0:
        _refuse("GRIDS_UNSTATED", f"FixedGrids interval {interval:g} s over CLAW_TFINAL {float(tfinal):g} s "
                "writes no frame.")
    nout = int(math.floor(float(tfinal) / interval + 1e-9)) + 1
    return ({"FGMAX_FGMAX_GRIDS": [[1, float(value["tstart_max"]), 1.0e10, float(value["dt_check"]), levels,
                                    float(value["arrival_tol"]), int(value["interp_method"]), 2,
                                    {"x1": x1 + dx / 2, "y1": y1 + dy / 2, "x2": x2 - dx / 2,
                                     "y2": y2 - dy / 2, "nx": nx, "ny": ny}]],
             "FGOUT_FGOUT_GRIDS": [[1, 1, nout, 0.0, (nout - 1) * interval, 2, "binary32",
                                    x1, x2, y1, y2, nx, ny]]}, {})


def initial_surface(value: Any, *, run: Mapping[str, Any]
                    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The water surface the run opens at, from a raster: InitialSurface({surface}).

    ``surface`` is a raster of surface elevation, read on the run's ``vertical_frame``; it is written as the
    one qinit file the engine reads and the water stands at that elevation wherever it is above the bed.
    A surface the run does not state states nothing."""
    from trid3nt_server.inputs.bed import bed
    from trid3nt_server.inputs.geometry import source_uri

    if not isinstance(value, Mapping) or set(value) != {"surface"}:
        _refuse("SURFACE_UNSTATED", "InitialSurface takes a mapping {surface}: the raster of the surface elevation.")
    if value["surface"] is None:
        return {}, {}
    if run.get("QINIT_QINIT_TYPE") is not None or run.get("QINIT_QINITFILES") is not None:
        _refuse("SURFACE_QINIT_STATED", "QINIT_QINIT_TYPE or QINIT_QINITFILES is stated beside InitialSurface: "
                "InitialSurface states the qinit file, so remove the keyword or the composite.")
    frame = str(run.get("vertical_frame") or "").strip()
    if not frame:
        _refuse("SURFACE_FRAME_UNSTATED", "the run states no vertical_frame, so the surface cannot be read on the "
                "run's zero: state the frame the run counts elevations from.")
    read = bed(value["surface"], frame=frame, label="initial surface")
    if read.is_depth:
        _refuse("SURFACE_DEPTH", "InitialSurface is a depth in metres, which is no grid a qinit file holds: hand it the raster.")
    name = "qinit_01.xyz"
    return ({"QINIT_QINIT_TYPE": 4, "QINIT_QINITFILES": [[name]]},
            {name: {"qinit": str(source_uri(read.source)), "frame": frame}})


def _triple(axis_value: Any, axis: str) -> tuple[float, float, int]:
    ok = isinstance(axis_value, (list, tuple)) and len(axis_value) == 3 and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        for v in axis_value)
    if not ok or axis_value[0] >= axis_value[1] or axis_value[2] < 2 \
            or axis_value[2] != int(axis_value[2]):
        _refuse("FAULT_GRID", f"Fault {axis} is [first, last, nodes] with first below last and "
                f"at least two nodes, not {axis_value!r}.")
    return float(axis_value[0]), float(axis_value[1]), int(axis_value[2])


@dataclass(frozen=True, slots=True)
class GeoclawSheet(Sheet):
    """A filled GeoClaw deck: the sheet, plus the card row stating the run's wall time."""

    def engine_rows(self) -> tuple[list[ParamSheetRow], set[str]]:
        """The card's engine section: the wall-time estimate of the whole run, from the accepted grid's rate and the stated CLAW_TFINAL.

        States nothing while the run holds no grid facts or CLAW_TFINAL is unstated."""
        from trid3nt_server.tools.mesh.meshers.claw_grid import PROBE_KEY, WALL_BASIS

        mesh = self.run.get("mesh")
        facts = (getattr(mesh.get("artifact"), "probes", None) or {}).get(PROBE_KEY) if isinstance(mesh, Mapping) else None
        tfinal = self.stated().get("CLAW_TFINAL")
        if not facts or tfinal is None:
            return [], set()
        minutes = facts["wall_s_per_sim_s"] * float(tfinal) / 60.0
        return [ParamSheetRow(
            name="wall_estimate", value=f"about {minutes:.0f} min of wall for CLAW_TFINAL {float(tfinal):g} s",
            desc="The run's wall time: the grid's wall seconds per simulated second times CLAW_TFINAL. Read-only, not enforced.",
            origin=Origin.DERIVED, editable=False, source_badge=WALL_BASIS, group="Grid")], set()


class GeoclawModule(Module):
    """The GeoClaw wrapper: its dictionary and the registry of the one exposed body."""

    @classmethod
    def dictionary_dir(cls) -> Path:
        return module_input_dir()

    @classmethod
    def wrapper_for(cls, module: str) -> type:
        from . import wrapper_for

        return wrapper_for(module)

    @classmethod
    def sheet(cls, filled: dict[str, Any], files: dict[str, Any],
              run: Mapping[str, Any]) -> Any:
        """The filled sheet a body's slots make: the generic sheet, held immutable.

        Nothing is armed or written into files here - the authoring that turns
        slots into setrun values and Clawpack's .data files is a later leg's."""
        return GeoclawSheet(body=cls, filled=MappingProxyType(filled),
                     files=MappingProxyType(files),
                     run=MappingProxyType(run))
