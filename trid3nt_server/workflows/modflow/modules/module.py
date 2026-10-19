"""The MODFLOW wrapper over the engine-neutral module surface.

Where the dictionary lives and which bodies the engine exposes; the fill holds
this dictionary, and mf6's own writer judges what the authoring writes. The one
composite every body shares is ``Periods``: the simulation's own clock, spelled
in TDIS.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from ..errors import ModflowError
from ...runtime.module import Module, SlotRefused

__all__ = ["ModflowModule", "module_input_dir", "periods"]


# A flow stress may carry the concentration of the water it moves: the package's AUXILIARY column the transport model's SSM reads.
_AUX = ("concentration", "CONCENTRATION", "M/L3")


def _stress(name: str, package: str, dimensions: Mapping[str, str], *, carries: bool = False):
    columns = tuple(dimensions)

    def expand(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        if isinstance(value, (list, tuple)) and value:
            expanded = [slots for item in value if (slots := expand(item)[0])]
            stated = [slots[f"{package}_STRESS_PERIOD_DATA"] for slots in expanded]
            carrying = [f"{package}_AUXILIARY" in slots for slots in expanded]
            if any(carrying) and not all(carrying):
                raise ModflowError(
                    f"{name} states {_AUX[0]} on some statements and not others; the "
                    f"column is on every cell of the package, so state it on each statement.",
                    error_code="MODFLOW_STRESS_UNSTATED")
            return ({f"{package}_STRESS_PERIOD_DATA": {"stress": package, "statements": stated},
                     **({f"{package}_AUXILIARY": [_AUX[1]]} if all(carrying) else {})}
                    if stated else {}), {}
        if not isinstance(value, Mapping):
            raise ModflowError(
                f"{name} takes a mapping of geometry, layer and {', '.join(columns)}, or a list of them; "
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
        aux = {_AUX[0]: _AUX[2]} if carries and value.get(_AUX[0]) is not None else {}
        taken = {**dimensions, **aux}
        return {f"{package}_STRESS_PERIOD_DATA": {
            "stress": package, "composite": name, "geometry": value["geometry"],
            "values": {column: value[column] for column in taken},
            "dimensions": dict(taken), "layer": layer},
            **({f"{package}_AUXILIARY": [_AUX[1]]} if aux else {})}, {}

    expand.__doc__ = (
        f"The {package} package's cells a geometry places: {name}(geometry, layer, "
        f"{', '.join(columns)}{f', [{_AUX[0]}]' if carries else ''}), or a list of such statements; a geometry reading nothing states nothing.")
    return expand


def module_input_dir() -> Path:
    """Where the committed module inputs live."""
    return Path(__file__).resolve().parent / "module_input"


def periods(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The TDIS clock a ``Periods`` value states: the count and one row per period.

    A row is (length, steps, multiplier) in the time units the deck spells - the
    engine's three numbers, with no step count or cadence guessed for it."""
    if not isinstance(value, (list, tuple)) or not value:
        raise SlotRefused(
            "Periods takes a non-empty list of (length, steps, multiplier) rows "
            f"the simulation marches; got {value!r}.")
    rows: list[list[float | int]] = []
    for rank, row in enumerate(value, start=1):
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise SlotRefused(
                f"Periods row {rank} is (length, steps, multiplier); got {row!r}.")
        length, steps, multiplier = row
        if isinstance(length, bool) or isinstance(multiplier, bool) \
                or not isinstance(length, (int, float)) \
                or not isinstance(multiplier, (int, float)) \
                or isinstance(steps, bool) or not isinstance(steps, int):
            raise SlotRefused(
                f"Periods row {rank} takes numbers (length and multiplier "
                f"real, steps an integer); got {row!r}.")
        if length <= 0 or steps < 1 or multiplier <= 0:
            raise SlotRefused(
                f"Periods row {rank} is ({length!r}, {steps!r}, "
                f"{multiplier!r}); a period runs a positive length in a "
                "positive number of steps with a positive multiplier, or the "
                "simulation advances nothing.")
        rows.append([float(length), int(steps), float(multiplier)])
    return {"TDIS_NPER": len(rows), "TDIS_PERIODDATA": rows}, {}


def _start_date_time(moment: Mapping[str, Any]) -> str:
    """The run's moment as mf6 reads a start: one ISO 8601 instant."""
    return moment["at"].isoformat()


class ModflowModule(Module):
    """The MODFLOW wrapper: its dictionary and the registry of exposed bodies."""

    @classmethod
    def dictionary_dir(cls) -> Path:
        return module_input_dir()

    @classmethod
    def wrapper_for(cls, module: str) -> type:
        from . import wrapper_for

        return wrapper_for(module)

    @classmethod
    def sheet(cls, filled: dict[str, Any], files: dict[str, Any],
              run: Mapping[str, Any]) -> Any:
        """The filled deck a body's slots make: the generic sheet, held immutable.

        Nothing is armed, coupled or written into files here - MODFLOW's one
        simulation directory is the authoring's to write."""
        from ...runtime.sheet import Sheet

        return Sheet(body=cls, filled=MappingProxyType(filled),
                     files=MappingProxyType(files),
                     run=MappingProxyType(run))
