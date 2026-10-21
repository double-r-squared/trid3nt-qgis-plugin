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
from ...runtime.module import MOMENT, Module, SlotRefused

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
        if layer != "all" and (isinstance(layer, bool) or not isinstance(layer, int) or layer < 1):
            raise ModflowError(
                f"{name} layer is {layer!r}; it is an integer from 1, or \"all\".",
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


_STREAMS = {"width": "L", "slope": "1", "bed_top": "L", "bed_thickness": "L",
            "bed_conductivity": "L/T", "manning": "1"}


def _streams(value: Any) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    if isinstance(value, (list, tuple)) and value:
        stated = [item for each in value if (item := _streams(each)[0])]
        return ({"SFR_PACKAGEDATA": {"streams": [row["SFR_PACKAGEDATA"]["streams"][0]
                                                 for row in stated]}} if stated else {}), {}
    if not isinstance(value, Mapping):
        raise ModflowError(
            "Streams takes a mapping of geometry, "
            f"{', '.join(_STREAMS)} and an optional layer, or a list of them; got {value!r}.",
            error_code="MODFLOW_STRESS_UNSTATED")
    if value.get("geometry") is None:
        return {}, {}
    for argument in _STREAMS:
        if value.get(argument) is None:
            raise ModflowError(
                f"Streams states no {argument}; the engine has no value for it, "
                "so state it from a measurement or a named source.",
                error_code="MODFLOW_STRESS_UNSTATED")
    layer = value.get("layer")
    if layer is not None and (isinstance(layer, bool) or not isinstance(layer, int) or layer < 1):
        raise ModflowError(f"Streams layer is {layer!r}; it is an integer from 1.",
                           error_code="MODFLOW_STRESS_UNSTATED")
    manning = value["manning"]
    if isinstance(manning, bool) or not isinstance(manning, (int, float)):
        raise ModflowError(
            "Streams manning is a number in the units the SFR conversions state; a raster "
            "carries no unit this frame converts.", error_code="MODFLOW_STRESS_UNSTATED")
    return {"SFR_PACKAGEDATA": {"streams": [{
        "composite": "Streams", "geometry": value["geometry"], "layer": layer,
        "values": {column: value[column] for column in _STREAMS},
        "dimensions": dict(_STREAMS)}]}}, {}


_streams.__doc__ = (
    "The SFR reaches a line geometry places: Streams(geometry, width, slope, bed_top, "
    "bed_thickness, bed_conductivity, manning[, layer]), or a list of such statements.\n\n"
    "    The line is cut by the cells it crosses in its digitized order, downstream; each\n"
    "    reach lies on the cell's top solved layer unless layer states one.")


def module_input_dir() -> Path:
    """Where the committed module inputs live."""
    return Path(__file__).resolve().parent / "module_input"


def periods(value: Any, *, run: Mapping[str, Any] | None = None
            ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The TDIS clock a ``Periods`` value states: the count and one row per period.

    Two forms: a non-empty list of (length, steps, multiplier) rows in the time
    units the deck spells, or a mapping ``{"until": <instant>, "steps": <int>}``
    whose instant is a run input resolved by the fill - the clock then runs from
    the run's start (its moment, else its event_time) to that instant, in
    ``TDIS_TIME_UNITS``, over the stated number of equal steps."""
    if isinstance(value, Mapping):
        return _period_until(value, run or {})
    if not isinstance(value, (list, tuple)) or not value:
        raise SlotRefused(
            "Periods takes a non-empty list of (length, steps, multiplier) rows "
            f"the simulation marches, or a mapping of until and steps; got {value!r}.")
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


def _period_until(value: Mapping[str, Any], run: Mapping[str, Any]
                  ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The one period from the run's start to a stated ``until`` instant, over ``steps`` equal steps."""
    import datetime as dt

    from trid3nt_server.inputs.instant import instant

    from ..authoring.griddata import SECONDS

    unknown = sorted(set(value) - {"until", "steps"})
    if unknown:
        raise SlotRefused(
            f"Periods takes until and steps; {', '.join(unknown)} is none of them.")
    until, steps = value.get("until"), value.get("steps")
    if until is None:
        raise SlotRefused("Periods states no until; the period runs to an instant.")
    if steps is None:
        raise SlotRefused("Periods states no steps; the period marches a number of steps.")
    if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
        raise SlotRefused(
            f"Periods steps is {steps!r}; it is an integer from 1.")
    moment = run.get(MOMENT)
    start: Any = None
    if isinstance(moment, Mapping) and moment.get("at") is not None:
        start = moment["at"]
    elif run.get("event_time") is not None:
        start = run["event_time"]
    if isinstance(start, str):
        start = dt.datetime.fromisoformat(start)
    if start is None:
        raise SlotRefused(
            "Periods states until, but the run holds no event_time and no "
            "moment for the clock to start at.")
    try:
        end = dt.datetime.fromisoformat(
            instant(until, label="Periods.until", code="PERIODS_UNTIL_INVALID")
            or "")
    except ValueError as refused:
        raise SlotRefused(f"Periods until is not a parseable instant: {refused}") from None
    if end <= start:
        raise SlotRefused(
            f"Periods until {end.isoformat()} is not after the run's start "
            f"{start.isoformat()}; a period that advances nothing is no period.")
    units = str(run.get("TDIS_TIME_UNITS") or "unknown").strip().lower()
    if units not in SECONDS:
        raise SlotRefused(
            f"TDIS_TIME_UNITS is {units!r}, so the period's length in it has "
            "no length; state TDIS_TIME_UNITS as seconds, minutes, hours, "
            "days or years.")
    length = (end - start).total_seconds() / SECONDS[units]
    return {"TDIS_NPER": 1, "TDIS_PERIODDATA": [[length, int(steps), 1.0]]}, {}


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
