"""The MODFLOW groundwater-transport wrapper: its dictionary and what a solved run writes."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from ...runtime.module import MOMENT, Output
from .module import ModflowModule, _observations, _series, _start_date_time, _stress, periods
from .outputs import PRIMITIVES

__all__ = ["GWT", "MODULE_OUTPUT"]

# What the module writes, read off the concentration file. Units stay empty: mf6 carries no unit system.
MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({
    "CONCENTRATION": Output("concentration", "",
                            style={"kind": "mesh", "ramp": "viridis",
                                   "floor": 0}),
})

# Each transport stress composite: its package and the arguments it takes with their dimensions, in the package's own column order after the cell id.
_STRESS = {
    "ConstantConcentration": ("CNC", (("concentration", "M/L3"),)),
    "MassSource": ("SRC", (("rate", "M/T"),)),
}

GWT = ModflowModule("gwt")
GWT.MODULE_OUTPUT = MODULE_OUTPUT
# The simulation's own clock on every body: Periods spells the marching, the moment the instant it opens at.
GWT.composites(reads={"Periods": ("until", "steps")}, Periods=periods)
GWT.composites(
    reads={name: ("geometry", *(column for column, _ in columns))
           for name, (_, columns) in _STRESS.items()},
    **{name: _stress(name, package, dict(columns))
       for name, (package, columns) in _STRESS.items()})
GWT.composites(reads={"TimeSeries": ("rows",)}, TimeSeries=_series)
GWT.composites(reads={"Observations": ("at", "to_at")}, Observations=_observations)
GWT.FILLED_BY = MappingProxyType({MOMENT: {"TDIS_START_DATE_TIME": _start_date_time}})
# The read of what a solved run writes: the same primitives read off this body's own files.
GWT.reads(**PRIMITIVES)
