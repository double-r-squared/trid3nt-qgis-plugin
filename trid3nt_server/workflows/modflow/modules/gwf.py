"""The MODFLOW groundwater-flow wrapper: its dictionary and what a solved run writes."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from ...runtime.module import MOMENT, Output
from ..errors import ModflowError
from .module import ModflowModule, _start_date_time, periods

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

# Each stress composite: its package and the arguments it takes, in the package's own column order after the cell id.
_STRESS = {
    "Drains": ("DRN", ("elevation", "conductance")),
    "GeneralHead": ("GHB", ("head", "conductance")),
    "ConstantHead": ("CHD", ("head",)),
    "Wells": ("WEL", ("rate",)),
    "Recharge": ("RCH", ("rate",)),
}


def _stress(name: str, package: str, columns: tuple[str, ...]):
    def expand(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        if not isinstance(value, Mapping):
            raise ModflowError(
                f"{name} takes a mapping of geometry, layer and {', '.join(columns)}; "
                f"got {value!r}.", error_code="MODFLOW_STRESS_UNSTATED")
        if value.get("geometry") is None:
            return {}, {}
        for argument in ("layer", *columns):
            if value.get(argument) is None:
                raise ModflowError(
                    f"{name} states no {argument}; the engine has no value for it, "
                    "so state it from a measurement or a named source.",
                    error_code="MODFLOW_STRESS_UNSTATED")
        layer = value["layer"]
        if isinstance(layer, bool) or not isinstance(layer, int) or layer < 1:
            raise ModflowError(
                f"{name} layer is {layer!r}; it is an integer from 1.",
                error_code="MODFLOW_STRESS_UNSTATED")
        return {f"{package}_STRESS_PERIOD_DATA": {
            "stress": package, "composite": name, "geometry": value["geometry"],
            "values": {column: value[column] for column in columns},
            "layer": layer}}, {}

    expand.__doc__ = (
        f"The {package} package's cells a geometry places: {name}(geometry, layer, "
        f"{', '.join(columns)}); a geometry reading nothing states nothing.")
    return expand


GWF = ModflowModule("gwf")
GWF.MODULE_OUTPUT = MODULE_OUTPUT
# The simulation's own clock on every body: Periods spells the marching, the moment the instant it opens at.
GWF.composites(Periods=periods)
GWF.FILLED_BY = MappingProxyType({MOMENT: {"TDIS_START_DATE_TIME": _start_date_time}})
GWF.composites(
    reads={name: ("geometry", *columns) for name, (_, columns) in _STRESS.items()},
    **{name: _stress(name, package, columns)
       for name, (package, columns) in _STRESS.items()})
