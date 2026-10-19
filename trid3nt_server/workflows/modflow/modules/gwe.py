"""The MODFLOW groundwater-energy wrapper: its dictionary and what a solved run writes."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from ...runtime.module import MOMENT, Output
from .module import ModflowModule, _start_date_time, _stress, periods
from .outputs import PRIMITIVES

__all__ = ["GWE", "MODULE_OUTPUT"]

# What the module writes, read off the temperature file. Units stay empty: mf6 carries no unit system.
MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({
    "TEMPERATURE": Output("temperature", "",
                          style={"kind": "mesh", "ramp": "magma"}),
})

# Each energy stress composite: its package and the arguments it takes with their dimensions, in the package's own column order after the cell id.
_STRESS = {
    "ConstantTemperature": ("CTP", (("temperature", "THETA"),)),
    "EnergySource": ("ESL", (("rate", "E/T"),)),
}

GWE = ModflowModule("gwe")
GWE.MODULE_OUTPUT = MODULE_OUTPUT
# The simulation's own clock on every body: Periods spells the marching, the moment the instant it opens at.
GWE.composites(reads={"Periods": ("until", "steps")}, Periods=periods)
GWE.FILLED_BY = MappingProxyType({MOMENT: {"TDIS_START_DATE_TIME": _start_date_time}})
GWE.composites(
    reads={name: ("geometry", *(column for column, _ in columns))
           for name, (_, columns) in _STRESS.items()},
    **{name: _stress(name, package, dict(columns))
       for name, (package, columns) in _STRESS.items()})
# The read of what a solved run writes: the same primitives read off this body's own files.
GWE.reads(**PRIMITIVES)
