"""The MODFLOW groundwater-transport wrapper: its dictionary and what a solved run writes."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from ...runtime.module import Output
from .module import ModflowModule

__all__ = ["GWT", "MODULE_OUTPUT"]

# What the module writes, read off the concentration file. Units stay empty: mf6 carries no unit system.
MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({
    "CONCENTRATION": Output("concentration", "",
                            style={"kind": "mesh", "ramp": "viridis",
                                   "floor": 0}),
})

GWT = ModflowModule("gwt")
GWT.MODULE_OUTPUT = MODULE_OUTPUT
