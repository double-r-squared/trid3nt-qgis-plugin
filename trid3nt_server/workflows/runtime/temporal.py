"""A MEASURED RECORD as the runtime holds it: instants, values and a unit.

:class:`Series` is the value a slot carries where the engine takes a file of
instants rather than one number, and it is frozen - what was measured does not
move once the record it was read off said what it measured. The QUANTITY CLASSES
say how a number may be moved in time and the unit table says what it may be
read as; moving one onto a reader's clock is ``inputs.series.align``.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Sequence

from .errors import DeclarativeError

__all__ = [
    "CATEGORICAL",
    "RATE",
    "STATE",
    "Series",
    "TemporalGapError",
    "TemporalShapeError",
    "TemporalUnitsError",
    "convert_units",
]


# --- quantity classes: what a number MEANS decides how it may be moved ------ #

#: A per-time quantity (mm/day, m3/s). Resampling must preserve the total.
RATE = "rate"
#: An instantaneous level or condition (water level, temperature). Linear.
STATE = "state"
#: A class label (land cover, alert level, flow regime). Nearest only.
CATEGORICAL = "categorical"

class TemporalGapError(DeclarativeError):
    """The record has a hole wider than the declared ``max_gap``.
    Never bridged: a smooth line drawn over missing hours is a fabricated forcing."""

    error_code = "TEMPORAL_GAP_UNBRIDGED"


class TemporalUnitsError(DeclarativeError):
    error_code = "TEMPORAL_UNITS_INCOMPATIBLE"


class TemporalShapeError(DeclarativeError):
    """The rows handed in do not describe a record: too few, or out of order."""

    error_code = "TEMPORAL_NOT_RESAMPLEABLE"


# --- the unit table: dimension, factor to the dimension's base, offset ------ #
#
# Small and explicit on purpose. An entry is a conversion someone declared and
# a reader can check; anything absent refuses by name rather than being guessed
# at. Base units are the first row of each block.
_UNITS: dict[str, tuple[str, float, float]] = {
    "m": ("length", 1.0, 0.0),
    "cm": ("length", 0.01, 0.0),
    "mm": ("length", 0.001, 0.0),
    "km": ("length", 1000.0, 0.0),
    "ft": ("length", 0.3048, 0.0),
    "in": ("length", 0.0254, 0.0),

    "m3/s": ("flow", 1.0, 0.0),
    "m^3/s": ("flow", 1.0, 0.0),
    "cms": ("flow", 1.0, 0.0),
    "ft3/s": ("flow", 0.028316846592, 0.0),
    "cfs": ("flow", 0.028316846592, 0.0),
    "L/s": ("flow", 0.001, 0.0),

    "mm/day": ("depth_rate", 1.0, 0.0),
    "mm/d": ("depth_rate", 1.0, 0.0),
    "mm/h": ("depth_rate", 24.0, 0.0),
    "mm/hr": ("depth_rate", 24.0, 0.0),
    "cm/day": ("depth_rate", 10.0, 0.0),
    "in/day": ("depth_rate", 25.4, 0.0),
    "in/h": ("depth_rate", 609.6, 0.0),
    "m/s": ("depth_rate", 86_400_000.0, 0.0),

    "degC": ("temperature", 1.0, 0.0),
    "C": ("temperature", 1.0, 0.0),
    "K": ("temperature", 1.0, -273.15),
    "degF": ("temperature", 5.0 / 9.0, -32.0 * 5.0 / 9.0),

    "mg/L": ("concentration", 1.0, 0.0),
    "g/m3": ("concentration", 1.0, 0.0),
    "ug/L": ("concentration", 0.001, 0.0),
}


def convert_units(value: float, source: str, target: str) -> float:
    """``value`` expressed in ``target`` units. Same unit in and out is exact."""
    if source == target:
        return float(value)
    src, dst = _unit(source), _unit(target)
    if src[0] != dst[0]:
        raise TemporalUnitsError(
            f"cannot normalize {source!r} ({src[0]}) to {target!r} ({dst[0]}): "
            "they measure different quantities, and a cross-dimension conversion "
            "would be an invented relationship."
        )
    base = float(value) * src[1] + src[2]
    return (base - dst[2]) / dst[1]


def _unit(name: str) -> tuple[str, float, float]:
    try:
        return _UNITS[str(name).strip()]
    except KeyError:
        raise TemporalUnitsError(
            f"unit {name!r} is not in the declared unit table "
            f"({', '.join(sorted(_UNITS))}). Add it there rather than converting "
            "inline - a conversion nobody declared is one nobody can check."
        ) from None


# --- the declaration ------------------------------------------------------- #


class Series:
    """A measured quantity over time, on the RUN's own clock: seconds and values.

    What a slot holds where a number would be a lumped stand-in for a record
    somebody measured. Not a dataclass: a card row and a run record both print
    it, and what a reader needs there is the shape, not a second copy of the
    points."""

    __slots__ = ("times_s", "values", "units")

    def __init__(self, times_s: Sequence[float], values: Sequence[float], *,
                 units: str) -> None:
        times = tuple(float(t) for t in times_s)
        found = tuple(float(v) for v in values)
        if len(times) != len(found):
            raise TemporalShapeError(
                f"a series carries {len(times)} instants against {len(found)} "
                "values; one instant holds one value.")
        if len(times) < 2:
            raise TemporalShapeError(
                f"a series of {len(times)} point(s) has no interval to read "
                "between two rows.")
        if any(b <= a for a, b in zip(times, times[1:])):
            raise TemporalShapeError(
                "a series' instants must strictly increase on the run's own "
                "clock; an engine that reads a table between two rows stops on "
                "a pair that does not.")
        object.__setattr__(self, "times_s", times)
        object.__setattr__(self, "values", found)
        object.__setattr__(self, "units", str(units))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            f"a Series is frozen: {name!r} cannot be moved once the record it "
            "was read off said what it measured.")

    def __len__(self) -> int:
        return len(self.times_s)

    def to_doc(self) -> dict[str, Any]:
        """The series as plain data, which is what a run RECORD carries.

        A record is read back off a JSON store, so what it holds is rows and not
        this object; the points are written whole because a summary in their
        place would be a measurement nobody could replay."""
        return {"times_s": list(self.times_s), "values": list(self.values),
                "units": self.units}

    def __str__(self) -> str:
        return f"series, {len(self.times_s)} points over the window"

    def __repr__(self) -> str:
        return (f"Series({len(self.times_s)} points, {self.times_s[0]:g}"
                f"..{self.times_s[-1]:g} s, {self.units})")

    def __eq__(self, other: Any) -> bool:
        return (isinstance(other, Series) and other.times_s == self.times_s
                and other.values == self.values and other.units == self.units)

    def __hash__(self) -> int:
        return hash((self.times_s, self.values, self.units))

    @classmethod
    def from_samples(cls, samples: Sequence[tuple[Any, float]], *, units: str,
                     start_s: float = 0.0, at: Any = None) -> "Series":
        """Stamped readings -> this series on a clock that opens at ``start_s``.

        ``at`` is the instant the RUN opens at, so t = ``start_s`` is that
        moment INSIDE the record and the readings before it carry negative
        times. Unstated, t = ``start_s`` is the record's first sample, which is
        the only honest origin when the run names no moment."""
        import pandas as pd

        stamps = pd.to_datetime([stamp for stamp, _v in samples], utc=True)
        order = sorted(range(len(stamps)), key=lambda i: stamps[i])
        origin = (pd.to_datetime(at, utc=True) if at is not None
                  else stamps[order[0]])
        return cls([float(start_s) + (stamps[i] - origin).total_seconds()
                    for i in order],
                   [float(samples[i][1]) for i in order], units=units)

    def in_units(self, units: str) -> "Series":
        """This series read in ``units``, or a refusal naming both."""
        if str(units) == self.units:
            return self
        return Series([*self.times_s],
                      [convert_units(v, self.units, units) for v in self.values],
                      units=str(units))

    def shifted(self, offset_m: float) -> "Series":
        """Every reading moved by the SAME offset the scalar rode onto its datum.

        Re-deriving the shift per point would put two readings of one gauge on
        two zeros."""
        if not offset_m:
            return self
        return Series([*self.times_s], [v + float(offset_m) for v in self.values],
                      units=self.units)

    def opening_at(self, start_s: float) -> "Series":
        """The same readings on a clock that reads ``start_s`` where this one
        reads zero.

        A record is opened at the run's own moment, at t = 0, and the readings
        before that moment keep their negative times; a run continuing another
        reads that same moment past zero on its own clock. Re-anchoring the
        FIRST sample here would open the run at the record's beginning."""
        offset = float(start_s)
        if not offset:
            return self
        return Series([t + offset for t in self.times_s], [*self.values],
                      units=self.units)

    def at(self, time_s: float) -> float:
        """The value at one instant, read between the two rows bracketing it -
        the engine's own reading of its own table."""
        t = float(time_s)
        if t <= self.times_s[0]:
            return self.values[0]
        if t >= self.times_s[-1]:
            return self.values[-1]
        index = bisect_right(self.times_s, t)
        left, right = self.times_s[index - 1], self.times_s[index]
        span = right - left
        low, high = self.values[index - 1], self.values[index]
        return low + (high - low) * (t - left) / span
