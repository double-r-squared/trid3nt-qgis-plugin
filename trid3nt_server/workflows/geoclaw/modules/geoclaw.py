"""The GeoClaw wrapper: its dictionary as the one module a run starts from, and what a solved run writes."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from ...runtime.module import Output
from .module import GeoclawModule, fault, topography
from .outputs import PRIMITIVES

__all__ = ["GEO", "MODULE_OUTPUT"]

# A depth reads from zero up and a speed likewise; a surface above or below the sea level diverges about it.
_DEPTH = {"kind": "continuous", "ramp": "ylgnbu", "floor": 0}
_SPEED = {"kind": "continuous", "ramp": "viridis", "floor": 0}
_SURFACE = {"kind": "continuous", "ramp": "rdbu", "center": 0.0}
_ARRIVAL = {"kind": "continuous", "ramp": "plasma"}

# What the module writes, by token, and the keyword whose records make the engine write it: FGMAX_FGMAX_GRIDS (the
# MAX_ and ARRIVAL rows, a speed under FGMAX_NUM_FGMAX_VAL 2 or 5), FGOUT_FGOUT_GRIDS (the frame rows, the overland ones
# from frame 0) and GAUGE_GAUGES. Depth is TOTAL depth; an OVERLAND row drops the cells wet in frame 0.
MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({
    "MAX_DEPTH": Output("maximum water depth", "m", style=_DEPTH, varies=False),
    "MAX_SPEED": Output("maximum speed", "m/s", style=_SPEED, varies=False),
    "MAX_SURFACE": Output("surface elevation at the maximum depth", "m", style=_SURFACE, varies=False),
    "ARRIVAL_TIME": Output("arrival time", "s", style=_ARRIVAL, varies=False),
    "MAX_OVERLAND_DEPTH": Output("maximum overland water depth", "m", style=_DEPTH, varies=False),
    "DEPTH": Output("water depth", "m", style=_DEPTH),
    "SURFACE": Output("surface elevation", "m", style=_SURFACE),
    "OVERLAND_DEPTH": Output("overland water depth", "m", style=_DEPTH),
    "GAUGE_SURFACE": Output("surface elevation", "m", style=_SURFACE),
    "GAUGE_DEPTH": Output("water depth", "m", style=_DEPTH),
})

GEO = GeoclawModule("geoclaw")
GEO.composites(reads={"Topography": ("surface", "offset")}, Topography=topography,
               Fault=fault)
GEO.MODULE_OUTPUT = MODULE_OUTPUT
GEO.reads(**PRIMITIVES)
