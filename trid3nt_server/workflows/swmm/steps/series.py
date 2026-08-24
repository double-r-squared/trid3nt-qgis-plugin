"""Time-series helpers every SWMM deck writer and metrics step shares.

A SWMM deck is mostly a clock and a ``[TIMESERIES]`` block, and every template
that authors one was rendering both by hand - three spellings of the same
``H:MM`` formatting, three coercions of the same ``[["H:MM", value], ...]``
argument, three copies of argmax. These are those, once.

The rendering precision is a per-deck ARGUMENT rather than a house rule: SWMM
reads the numbers it is given, and a deck that changed precision would change
its own answer.
"""

from __future__ import annotations

from typing import Any, Sequence

from .errors import SwmmDeckError

__all__ = ["clock", "coerce_series", "peak", "timeseries_block"]


def clock(minutes: float) -> str:
    """SWMM's ``H:MM`` time-series clock, counted from the simulation start.

    Hours run past 24 rather than wrapping - a SWMM time series is indexed by
    elapsed time from ``START_DATE``, not by time of day.
    """
    total = int(minutes)
    return f"{total // 60}:{total % 60:02d}"


def coerce_series(series: Any, *, what: str) -> list[tuple[str, float]] | None:
    """``[["H:MM", value], ...]`` as clock/value pairs; ``None`` when not supplied.

    A MALFORMED series refuses typed rather than falling back to the declared
    forcing: silently modelling a different storm than the caller asked for is
    the swallow class this library exists to remove.
    """
    if not series:
        return None
    try:
        return [(str(when), float(value)) for when, value in series]
    except (TypeError, ValueError) as exc:
        raise SwmmDeckError(
            f"{what} is not a list of [\"H:MM\", value] pairs: {exc}"
        ) from exc


def timeseries_block(name: str, rows: Sequence[tuple[str, float]], *,
                     precision: int = 4) -> str:
    """One ``[TIMESERIES]`` object's lines: ``<name> <clock> <value>`` per row."""
    return "\n".join(f"{name} {when} {value:.{precision}f}" for when, value in rows)


def peak(series: Sequence[float]) -> tuple[float, int]:
    """``(maximum, its index)``; ``(0.0, 0)`` for an empty series.

    The INDEX is the point: a peak flow without its timing cannot answer "when",
    and every hydrograph question here is partly a timing question.
    """
    if not series:
        return 0.0, 0
    index = max(range(len(series)), key=lambda k: series[k])
    return series[index], index
