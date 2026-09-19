"""THE ALIGNMENT: one measured record moved onto the clock a reader asks it on.

A series carries its own instants, its own unit and - off the coverage row that
fetched it - its own QUANTITY CLASS, and those three decide every move that may
be made on it: a rate is conserved, a state interpolates, a class label goes to
its nearest neighbour. A hole wider than the bound refuses rather than being
drawn over. What was done rides back as a stamp, so a record nobody realigned
says so too.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from typing import Any, Sequence

from trid3nt_server.workflows.runtime.temporal import (
    CATEGORICAL,
    RATE,
    STATE,
    Series,
    TemporalGapError,
)

__all__ = ["Aligned", "align", "quantity_class"]

#: How a DATA CLASS moves in time. Stated over the coverage vocabulary because
#: the row that fetched a record already says what it measured, and an author
#: asked to restate it would be stating it for a source they did not choose.
#: Rainfall and flow are per-time totals; a class label has no average and no
#: slope; everything else is a level or a condition read at an instant.
_QUANTITY: dict[str, str] = {
    "precipitation series": RATE,
    "discharge series": RATE,
    "land cover": CATEGORICAL,
}

#: The default bound on a hole, in native intervals. Within-cadence
#: interpolation is refinement; bridging three missed reports is invention.
_GAP_INTERVALS = 3.0


@dataclass(frozen=True, slots=True)
class Aligned:
    """The record on the clock asked for, and the stamp saying how it got there."""

    series: Series
    note: str


def quantity_class(data_class: Any) -> str:
    """How a source's own data class moves in time: rate, state or categorical."""
    return _QUANTITY.get(str(data_class or "").strip(), STATE)


def align(series: Series, *, onto: Any = None, quantity: str = STATE,
          units: str | None = None, max_gap_s: float | None = None,
          opening_at: float = 0.0) -> Aligned:
    """One record onto the clock a reader reads it on, in the unit that reader reads.

    ``onto`` is the engine's own time step, the instants a reader asks at, or
    nothing where the record's own clock is what gets written; ``opening_at``
    moves the whole record to where the reader's zero sits. A hole wider than
    ``max_gap_s`` - three native intervals unstated - refuses."""
    notes: list[str] = []
    native = min(b - a for a, b in zip(series.times_s, series.times_s[1:]))
    bound = float(max_gap_s) if max_gap_s is not None else native * _GAP_INTERVALS
    _refuse_gaps(series, bound, native)
    if units is not None and str(units) != series.units:
        notes.append(f"converted {series.units}->{units}")
        series = series.in_units(str(units))
    if onto is not None:
        instants = _instants(series, onto)
        moved = _moved(series, instants, quantity)
        notes.append(
            f"native {_fmt(native)} kept" if moved is series
            else f"{_fmt(native)}->{_fmt(_step(instants))} {_method(quantity)}")
        series = moved
    if opening_at:
        series = series.opening_at(float(opening_at))
        notes.append(f"opened at t = {float(opening_at):g} s")
    return Aligned(series=series,
                   note="; ".join(notes) or f"native {_fmt(native)} kept")


def _instants(series: Series, onto: Any) -> tuple[float, ...]:
    """The clock asked for: a step swept across the record, or the instants given."""
    if isinstance(onto, (int, float)):
        step = float(onto)
        if step <= 0.0:
            return series.times_s
        edge, instants = series.times_s[0], []
        while edge < series.times_s[-1]:
            instants.append(edge)
            edge += step
        instants.append(series.times_s[-1])
        return tuple(instants)
    return tuple(float(t) for t in onto)


def _moved(series: Series, instants: tuple[float, ...], quantity: str) -> Series:
    """The record read at each instant the way its quantity class may be read.

    A clock no COARSER than the record's own asks for nothing the record does
    not already answer between two rows, so the record stands as measured."""
    if instants == series.times_s:
        return series
    native = min(b - a for a, b in zip(series.times_s, series.times_s[1:]))
    if quantity == RATE and _step(instants) > native:
        values = [_interval_mean(series, instants, i) for i in range(len(instants))]
    elif quantity == CATEGORICAL:
        values = [_nearest(series, t) for t in instants]
    elif _step(instants) <= native:
        return series
    else:
        values = [series.at(t) for t in instants]
    return Series(instants, values, units=series.units)


def _interval_mean(series: Series, instants: tuple[float, ...], index: int) -> float:
    """A RATE over the interval this instant opens: the time-weighted mean.

    What a rate reports is a total over the interval it was reported for, so the
    mean over the target interval is the value that keeps that total."""
    start = instants[index]
    end = instants[index + 1] if index + 1 < len(instants) else instants[-1]
    if end <= start:
        return series.at(start)
    edges = sorted({start, end, *(t for t in series.times_s if start < t < end)})
    total = sum((b - a) * 0.5 * (series.at(a) + series.at(b))
                for a, b in zip(edges, edges[1:]))
    return total / (end - start)


def _nearest(series: Series, time_s: float) -> float:
    """The value of the sample closest to one instant - the only honest move on a
    class label, which has no average and no slope."""
    index = bisect_left(series.times_s, float(time_s))
    if index <= 0:
        return series.values[0]
    if index >= len(series.times_s):
        return series.values[-1]
    before, after = series.times_s[index - 1], series.times_s[index]
    return (series.values[index - 1]
            if (time_s - before) <= (after - time_s) else series.values[index])


def _refuse_gaps(series: Series, bound: float, native: float) -> None:
    gaps = [(b - a, b) for a, b in zip(series.times_s, series.times_s[1:])]
    worst, ends = max(gaps)
    if worst <= bound:
        return
    raise TemporalGapError(
        f"the record has a {_fmt(worst)} hole ending at t = {ends:g} s, wider "
        f"than the {_fmt(bound)} bound (native cadence {_fmt(native)}). Bridging "
        "it would invent the missing interval; narrow the window, pick a source "
        "that covers it, or state a bound that admits the hole on purpose.")


def _method(quantity: str) -> str:
    return {RATE: "conservative", CATEGORICAL: "nearest"}.get(quantity, "linear")


def _step(instants: Sequence[float]) -> float:
    """The clock's own spacing. The MEAN, not the shortest: a swept step leaves a
    ragged last interval, and the record is not read finely because of it."""
    return (instants[-1] - instants[0]) / float(len(instants) - 1)


def _fmt(seconds: float) -> str:
    for unit, size in (("d", 86400.0), ("h", 3600.0), ("min", 60.0)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{seconds:g}s"
