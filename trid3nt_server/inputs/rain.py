"""RAIN over a place: the depth that fell in each interval, on the run's clock.

A record states its instants and the depth that fell from each; the cadence is the
record's own, never assumed. What file or keyword a storm becomes is the engine's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .observation import ObservationError, _median_gap, convert
from .temporal import Series, TemporalShapeError

__all__ = ["Rain", "rain"]

_CODE = "RAIN_INVALID"

#: How many cadences a gap between two stamps spans before it is a HOLE: at one and a
#: half a report is missing, whatever jitter the stamps carry.
_HOLE = 1.5


@dataclass(frozen=True, slots=True)
class Rain:
    """Gross rain as depths per interval: each instant OPENS the interval its depth fell over."""

    #: Millimetres per interval, on a clock whose zero is the moment the run opens at.
    series: Series
    #: The length of one interval, in seconds.
    interval_s: float

    @classmethod
    def every(cls, interval_s: float, depths_mm: Sequence[float]) -> "Rain":
        """Depths stated at a cadence with no stamps: the first interval opens at zero."""
        return cls(Series([n * float(interval_s) for n in range(len(depths_mm))],
                          depths_mm, units="mm"), float(interval_s))

    @property
    def peak_mm_per_s(self) -> float:
        return max(self.series.values) / self.interval_s


def rain(source: Any, *, series_field: str = "precip_mm", column_units: Any = None,
         at: Any = None, label: str = "rain", code: str = _CODE) -> Rain | None:
    """THE ingestion: a fetched rainfall record -> its depths per interval, in millimetres.

    The record states its instants under ``times`` and its depths under ``series_field``,
    in the unit the source's row states for that column; ``at`` is the run's zero."""
    if source is None:
        return None
    if not isinstance(source, Mapping) or not series_field \
            or source.get(series_field) is None or source.get("times") is None:
        raise ObservationError(
            code, f"{label} is no rainfall record: one states its instants under "
            "'times' and the depth that fell from each under a series column, and "
            "this states " + (f"{', '.join(map(str, source)) or 'nothing'}."
                              if isinstance(source, Mapping)
                              else f"a {type(source).__name__}."))
    units = dict(column_units or {}).get(series_field) or source.get("units")
    try:
        series = Series.from_samples(
            list(zip(source["times"], (convert(depth, units, "mm")
                                       for depth in source[series_field]),
                     strict=True)), units="mm", at=at)
    except (ValueError, TemporalShapeError) as why:
        raise ObservationError(code, f"{label} states no hyetograph: {why}") from why
    import pandas as pd

    cadence = _median_gap(series.times_s)
    stamps = sorted(pd.to_datetime(list(source["times"]), utc=True))
    for before, after in zip(stamps, stamps[1:]):
        if (after - before).total_seconds() >= _HOLE * cadence:
            raise ObservationError(
                code, f"{label} has a hole: it reports every {cadence:g} s and "
                f"states nothing between {before:%Y-%m-%d %H:%M} and "
                f"{after:%Y-%m-%d %H:%M} UTC. A depth nobody reported is not a "
                "dry interval; narrow the window to one side of the hole or "
                "pick a source that covers it.")
    return Rain(series, cadence)
