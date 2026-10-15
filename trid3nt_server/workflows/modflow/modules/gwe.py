"""The MODFLOW groundwater-energy wrapper: its dictionary and what a solved run writes."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from ...runtime.module import Output
from .module import ModflowModule

__all__ = ["GWE", "MODULE_OUTPUT"]

# What the module writes, read off the temperature file. Units stay empty: mf6 carries no unit system.
MODULE_OUTPUT: Mapping[str, Output] = MappingProxyType({
    "TEMPERATURE": Output("temperature", "",
                          style={"kind": "mesh", "ramp": "magma"}),
})

GWE = ModflowModule("gwe")
GWE.MODULE_OUTPUT = MODULE_OUTPUT
