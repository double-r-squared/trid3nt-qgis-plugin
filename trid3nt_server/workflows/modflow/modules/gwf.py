"""The MODFLOW groundwater-flow wrapper: its dictionary and what a solved run writes."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from ...runtime.module import Output
from .module import ModflowModule

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

GWF = ModflowModule("gwf")
GWF.MODULE_OUTPUT = MODULE_OUTPUT
