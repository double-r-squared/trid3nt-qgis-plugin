"""The MODFLOW groundwater-flow wrapper: its dictionary and what a solved run writes."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from ...runtime.module import MOMENT, Output, SlotRefused
from ..errors import ModflowError
from .module import ModflowModule, _start_date_time, _stress, periods
from .outputs import PRIMITIVES

__all__ = ["GWF", "MODULE_OUTPUT"]

# What the module writes, read off the head and budget files. A discharge component reads about zero, so its ramp diverges; saturation is a fraction.
_LEVEL = {"kind": "mesh", "ramp": "blues"}
_SATURATION = {"kind": "mesh", "ramp": "ylgnbu", "floor": 0}
_SIGNED = {"kind": "mesh", "ramp": "rdbu", "center": 0.0}

# Units stay empty: mf6 carries no unit system, and a run's lengths and rates are the units the modeller declares.
MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({
    "HEAD": Output("head", "", style=_LEVEL),
    "SATURATION": Output("saturation", "", style=_SATURATION),
    "SPECIFIC_DISCHARGE": Output("specific discharge", "", style=_SIGNED),
})

# Each stress composite: its package and the arguments it takes with their dimensions, in the package's own column order after the cell id.
_STRESS = {
    "Drains": ("DRN", (("elevation", "L"), ("conductance", "L2/T"))),
    "GeneralHead": ("GHB", (("head", "L"), ("conductance", "L2/T"))),
    "ConstantHead": ("CHD", (("head", "L"),)),
    "Wells": ("WEL", (("rate", "L3/T"),)),
    "Recharge": ("RCH", (("rate", "L/T"),)),
}


_AQUIFER = ("thickness", "saturated_thickness", "water_table_depth", "conductivity",
            "transmissivity")


def _aquifer(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """A one-layer aquifer's cells: Aquifer(thickness | saturated_thickness, water_table_depth, conductivity | transmissivity).

    ``thickness`` runs down from the land surface, ``saturated_thickness`` down from the water table; the
    bottom lies under whichever, the starting head a water-table depth below the top, and the conductivity
    is stated or is the transmissivity over the thickness."""
    if not isinstance(value, Mapping):
        raise ModflowError(
            f"Aquifer takes a mapping of {', '.join(_AQUIFER)}; got {value!r}.",
            error_code="MODFLOW_AQUIFER_UNSTATED")
    unknown = sorted(set(value) - set(_AQUIFER))
    if unknown:
        raise ModflowError(
            f"Aquifer takes {', '.join(_AQUIFER)}; {', '.join(unknown)} is none of them.",
            error_code="MODFLOW_AQUIFER_UNSTATED")
    whole, saturated = value.get("thickness"), value.get("saturated_thickness")
    if (whole is None) == (saturated is None):
        raise ModflowError(
            "Aquifer states "
            + ("both thickness and saturated_thickness; state one" if whole is not None
               else "neither thickness nor saturated_thickness; state one")
            + " - from the land surface, or down from the water table.",
            error_code="MODFLOW_AQUIFER_UNSTATED")
    for argument in ("water_table_depth",):
        if value.get(argument) is None:
            raise ModflowError(
                f"Aquifer states no {argument}; the engine has no value for it, "
                "so state it from a measurement or a named source.",
                error_code="MODFLOW_AQUIFER_UNSTATED")
    conductivity, transmissivity = value.get("conductivity"), value.get("transmissivity")
    if (conductivity is None) == (transmissivity is None):
        raise ModflowError(
            "Aquifer states "
            + ("both conductivity and transmissivity; state one" if conductivity is not None
               else "neither conductivity nor transmissivity; state one")
            + ", the other follows from the thickness.",
            error_code="MODFLOW_AQUIFER_UNSTATED")
    depth = value["water_table_depth"]
    stated = ({"cells": "raster", "of": conductivity} if conductivity is not None
              else {"cells": "quotient", "of": [transmissivity, saturated if whole is None else whole]})
    return {"DISV_NLAY": 1, "NPF_K": stated,
            "DISV_BOTM": {"cells": "below_top",
                          "of": whole if saturated is None else [depth, saturated]},
            "IC_STRT": {"cells": "below_top", "of": depth}}, {}


# The transport stress a Transport body states, by its package word; each is the leg 7 factory on the GWT wrapper.
_TRANSPORT_STRESS = {"CNC": "ConstantConcentration", "SRC": "MassSource"}

# The simulation's own clock and grid: one TDIS and one staged DISV describe the whole simulation, and the carrier states them.
_SIMULATION_OWNED = ("TDIS", "DISV")


def _transport(value: Any, *, run: Mapping[str, Any]
               ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The gwt body this run's flow couples: its module word, slots and given inputs.

    ``given`` names the inputs that carry the body; a body whose given inputs all hold
    nothing states nothing. Each slot is a GWT dictionary identifier - an unknown one
    refuses by name - and a stress statement is the GWT wrapper's own composite's shape."""
    from . import wrapper_for
    from .gwt import GWT

    if not isinstance(value, Mapping):
        raise SlotRefused(
            "Transport takes a mapping with 'given' naming the input(s) that carry "
            "the transport body and 'slots' stating its GWT keywords; got "
            f"{type(value).__name__}.")
    given = value.get("given")
    if isinstance(given, str):
        given = (given,)
    given = tuple(given or ())
    if given and not any((run.get(name) if isinstance(name, str) else name)
                         is not None for name in given):
        return ({}, {})
    stated = value.get("slots")
    if stated is not None and not isinstance(stated, Mapping):
        raise SlotRefused(
            f"Transport slots takes a mapping of GWT identifiers to values; got "
            f"{type(stated).__name__}.")
    wrapper = wrapper_for("gwt")
    slots: dict[str, Any] = {}
    for name, item in (stated or {}).items():
        prefix = str(name).split("_", 1)[0]
        if prefix in _SIMULATION_OWNED:
            raise SlotRefused(
                f"{name} states the simulation's own "
                f"{'clock' if prefix == 'TDIS' else 'grid'}, which the carrier's deck "
                "owns: one TDIS and the staged DISV describe the simulation, and the "
                "transport body describes its own packages.")
        if item is None:
            continue
        slots[str(name)] = wrapper.slot(name).check(item)
    for name, item in list(slots.items()):
        if not name.endswith("_STRESS_PERIOD_DATA") or not isinstance(item, Mapping) \
                or "stress" in item:
            continue
        composite = _TRANSPORT_STRESS.get(name.split("_", 1)[0])
        if composite is not None:
            slots.update(GWT.COMPOSITES[composite].apply(item, run)[0])
    return ({}, {"gwt": {"module": "gwt", "slots": slots, "given": list(given)}})


GWF = ModflowModule("gwf")
GWF.MODULE_OUTPUT = MODULE_OUTPUT
# The simulation's own clock on every body: Periods spells the marching, the moment the instant it opens at.
GWF.composites(Periods=periods)
GWF.FILLED_BY = MappingProxyType({MOMENT: {"TDIS_START_DATE_TIME": _start_date_time}})
GWF.composites(
    reads={name: ("geometry", *(column for column, _ in columns))
           for name, (_, columns) in _STRESS.items()},
    **{name: _stress(name, package, dict(columns))
       for name, (package, columns) in _STRESS.items()})
GWF.composites(reads={"Aquifer": _AQUIFER}, Aquifer=_aquifer)
GWF.composites(Transport=_transport)
GWF.reads(**PRIMITIVES)
