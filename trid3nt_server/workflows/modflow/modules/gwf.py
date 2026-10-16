"""The MODFLOW groundwater-flow wrapper: its dictionary and what a solved run writes."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from ...runtime.module import MOMENT, Output
from ..errors import ModflowError
from .module import ModflowModule, _start_date_time, periods
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


def _stress(name: str, package: str, dimensions: Mapping[str, str]):
    columns = tuple(dimensions)

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
            "dimensions": dict(dimensions), "layer": layer}}, {}

    expand.__doc__ = (
        f"The {package} package's cells a geometry places: {name}(geometry, layer, "
        f"{', '.join(columns)}); a geometry reading nothing states nothing.")
    return expand


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
GWF.reads(**PRIMITIVES)
