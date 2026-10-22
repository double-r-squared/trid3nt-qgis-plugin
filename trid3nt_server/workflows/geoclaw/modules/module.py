"""The GeoClaw wrapper over the engine-neutral module surface.

Where the dictionary lives and the one body the engine exposes; the fill holds
this dictionary, and Clawpack's own writer judges what the authoring writes
(setrun's Data classes). One module word, "geoclaw": every rundata group is a
keyword prefix on the same body, and what a solved run writes is a later leg's
to pin.
"""

from __future__ import annotations

import math
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ...runtime.module import Module
from ..errors import GeoclawError

__all__ = ["GeoclawModule", "fault", "module_input_dir", "topography"]

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


def _triple(axis_value: Any, axis: str) -> tuple[float, float, int]:
    ok = isinstance(axis_value, (list, tuple)) and len(axis_value) == 3 and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        for v in axis_value)
    if not ok or axis_value[0] >= axis_value[1] or axis_value[2] < 2 \
            or axis_value[2] != int(axis_value[2]):
        _refuse("FAULT_GRID", f"Fault {axis} is [first, last, nodes] with first below last and "
                f"at least two nodes, not {axis_value!r}.")
    return float(axis_value[0]), float(axis_value[1]), int(axis_value[2])


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
        from ...runtime.sheet import Sheet

        return Sheet(body=cls, filled=MappingProxyType(filled),
                     files=MappingProxyType(files),
                     run=MappingProxyType(run))
