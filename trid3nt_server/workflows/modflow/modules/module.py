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

from ...runtime.module import Module, SlotRefused

__all__ = ["ModflowModule", "module_input_dir", "periods"]


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
