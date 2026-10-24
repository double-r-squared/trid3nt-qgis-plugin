"""The GeoClaw wrapper: its dictionary as the one module a run starts from, and what a solved run writes."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from ...runtime.module import Output
from .module import GeoclawModule, fault, finite_fault, fixed_grids, initial_surface, moment_tensor_fault, sift_fault, slab_fault, topography
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
GEO.composites(reads={"Topography": ("surface", "offset"), "SiftFault": ("unit_sources",),
                      "FiniteFault": ("finite_fault",), "SlabFault": ("slab",), "MomentTensorFault": ("moment_tensor", "slab"),
                      "FixedGrids": ("extent",), "InitialSurface": ("surface",)},
               Topography=topography, Fault=fault, SiftFault=sift_fault, FiniteFault=finite_fault, SlabFault=slab_fault, MomentTensorFault=moment_tensor_fault, FixedGrids=fixed_grids,
               InitialSurface=initial_surface)
GEO.MODULE_OUTPUT = MODULE_OUTPUT
GEO.reads(**PRIMITIVES)
# A stated point is the one gauge the run reads: the sea off the coast the question is about. The sea level is the
# offset the workflow measured, on the run's frame.
GEO.FILLED_BY = MappingProxyType({
    "seed": {"GAUGE_GAUGES": lambda point: [[1, point.lon, point.lat, 0.0, 1.0e10]]},
    "sea_level": {"GEO_SEA_LEVEL": lambda metres: float(metres)}})
