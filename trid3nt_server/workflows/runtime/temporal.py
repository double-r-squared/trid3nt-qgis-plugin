"""Declared TEMPORAL TRANSFORMS - ``.resample(...)`` and ``.normalize(units=...)``.

pandas does the arithmetic; a transform that ran leaves a provenance stamp, and a
payload with no ``.resample()`` is never realigned behind the consumer's back.
:class:`Series` is the value a slot holds where the engine takes a file of
instants rather than one number.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Sequence

from .errors import DeclarativeError, ModifierIllegalError, PlanValidationError

__all__ = [
    "CATEGORICAL",
    "RATE",
    "Series",
    "STATE",
    "ResampleSpec",
    "TemporalGapError",
    "TemporalShapeError",
    "TemporalSpec",
    "TemporalUnitsError",
    "Transformed",
    "UnitsSpec",
    "convert_units",
    "transform_series",
    "transform_value",
]


# --- quantity classes: what a number MEANS decides how it may be moved ------ #

#: A per-time quantity (mm/day, m3/s). Resampling must preserve the total.
RATE = "rate"
#: An instantaneous level or condition (water level, temperature). Linear.
STATE = "state"
#: A class label (land cover, alert level, flow regime). Nearest only.
CATEGORICAL = "categorical"

# The QUANTITY CLASS picks the default method: a RATE resamples CONSERVATIVELY
# (mass-preserving), a STATE interpolates LINEARLY, a CATEGORICAL value moves by
# NEAREST and by nothing else. A caller may override the first two.
_DEFAULT_METHOD = {RATE: "conservative", STATE: "linear", CATEGORICAL: "nearest"}
_METHODS = ("conservative", "linear", "nearest")


class TemporalGapError(DeclarativeError):
    """The record has a hole wider than the declared ``max_gap``.
    Never bridged: a smooth line drawn over missing hours is a fabricated forcing."""

    error_code = "TEMPORAL_GAP_UNBRIDGED"


class TemporalUnitsError(DeclarativeError):
    error_code = "TEMPORAL_UNITS_INCOMPATIBLE"


class TemporalShapeError(DeclarativeError):
    """A ``.resample()`` was declared against a payload that has no time axis."""

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
                     start_s: float = 0.0) -> "Series":
        """Stamped readings -> this series on a clock that opens at ``start_s``.

        t = ``start_s`` is the FIRST sample, so the run opens on an instant
        somebody measured rather than on a midnight nobody did."""
        import pandas as pd

        stamps = pd.to_datetime([stamp for stamp, _v in samples], utc=True)
        order = sorted(range(len(stamps)), key=lambda i: stamps[i])
        first = stamps[order[0]]
        return cls([float(start_s) + (stamps[i] - first).total_seconds()
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

    def at_step(self, step_s: float) -> "Series":
        """This series at the engine's own time step.

        The engine reads the table between the two rows that bracket an instant,
        so a record COARSER than the step is already everything the engine
        reads and stays as it was measured; a record finer than the step carries
        rows between two instants the engine never asks at, and those resample
        onto the step."""
        step = float(step_s)
        native = min(b - a for a, b in zip(self.times_s, self.times_s[1:]))
        if step <= 0.0 or step <= native:
            return self
        edge = self.times_s[0]
        instants: list[float] = []
        while edge < self.times_s[-1]:
            instants.append(edge)
            edge += step
        instants.append(self.times_s[-1])
        return Series(instants, [self.at(t) for t in instants], units=self.units)

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


@dataclass(frozen=True, slots=True)
class ResampleSpec:
    """``to`` is a pandas offset alias (``"1h"``, ``"6h"``, ``"1D"``, ``"60s"``)."""

    to: str
    method: str | None = None
    max_gap: str = "native*3"

    def __post_init__(self) -> None:
        target = _interval(self.to)
        if self.method is not None and self.method not in _METHODS:
            raise PlanValidationError(
                f".resample(method={self.method!r}) is not one of {_METHODS}."
            )
        _max_gap(self.max_gap, target)  # refuse a malformed bound at DECLARATION


@dataclass(frozen=True, slots=True)
class UnitsSpec:
    units: str

    def __post_init__(self) -> None:
        _unit(self.units)


@dataclass(frozen=True, slots=True)
class TemporalSpec:
    """What a ``Data`` declaration asks of a payload's time axis and units."""

    resample: ResampleSpec | None = None
    units: UnitsSpec | None = None


@dataclass(frozen=True, slots=True)
class Transformed:
    """The transformed payload plus the stamp that says what was done to it."""

    values: Any
    note: str


# --- the mechanism --------------------------------------------------------- #


def transform_series(series: Any, spec: TemporalSpec | None, *,
                     quantity: str, units: str | None = None) -> Transformed:
    """Resample and unit-normalize a time-indexed series against ``spec``.
    ``series`` is a pandas ``Series`` on a ``DatetimeIndex`` or a sequence of
    ``(timestamp, value)`` pairs; ``note`` is the provenance stamp for the record."""
    import pandas as pd

    s = _as_series(series, pd)
    notes: list[str] = []
    if spec is not None and spec.units is not None and units:
        s = s.astype("float64").map(
            lambda v: convert_units(v, units, spec.units.units))
        notes.append(_units_note(units, spec.units.units))
    native = _native(s, pd)
    if spec is None or spec.resample is None:
        notes.insert(0, f"native {_fmt(native)} {quantity}, no resample declared")
        return Transformed(values=s, note="; ".join(notes))

    method = _method(spec.resample.method, quantity)
    target = _interval(spec.resample.to)
    _refuse_gaps(s, _max_gap(spec.resample.max_gap, native), native, pd)
    if target == native:
        notes.insert(0, f"native {_fmt(native)} matches the declared "
                        f"{_fmt(target)} {quantity}, no resample")
        return Transformed(values=s, note="; ".join(notes))
    out = _resample(s, target, native, method, spec.resample.to, pd)
    notes.insert(0, f"resampled {_fmt(native)}->{_fmt(target)} {method}")
    return Transformed(values=out, note="; ".join(notes))


def transform_value(value: float, spec: TemporalSpec | None, *,
                    quantity: str, units: str | None = None,
                    native: str | None = None) -> Transformed:
    """The single-value path: unit normalization, and a resample that must be a no-op.
    ``native`` is the interval the value already represents; a ``.resample()`` to
    anything else REFUSES, one number having no time axis to redistribute."""
    notes: list[str] = []
    out = float(value)
    if spec is not None and spec.units is not None and units:
        out = convert_units(out, units, spec.units.units)
        notes.append(_units_note(units, spec.units.units))
    if spec is None or spec.resample is None:
        notes.insert(0, f"native {native or 'unstated'} {quantity}, "
                        "no resample declared")
        return Transformed(values=out, note="; ".join(notes))

    target = _interval(spec.resample.to)
    if native is None or _interval(native) != target:
        raise TemporalShapeError(
            f"a single {quantity} value (native {native or 'unstated'}) cannot be "
            f"resampled to {spec.resample.to}: there is no time axis to "
            "redistribute, and inventing one would fabricate the series. Declare "
            "a series-shaped source, or declare .resample(to=) at the source's "
            "own cadence."
        )
    notes.insert(0, f"native {_fmt(target)} matches the declared "
                    f"{_fmt(target)} {quantity}, no resample")
    return Transformed(values=out, note="; ".join(notes))


def _resample(s: Any, target: Any, native: Any, method: str, freq: str, pd: Any) -> Any:
    """The ~10 lines of pandas the doctrine above wraps."""
    if method == "nearest":
        idx = pd.date_range(s.index[0], s.index[-1], freq=freq)
        return s.reindex(idx, method="nearest")
    if target >= native:  # DOWNSAMPLE: the interval mean preserves a rate's total
        return s.resample(freq).mean().dropna()
    idx = pd.date_range(s.index[0], s.index[-1], freq=freq)
    dense = s.reindex(s.index.union(idx))
    # A RATE is piecewise-constant across the interval it was reported for;
    # holding it is what keeps the total unchanged. A STATE moves in between.
    dense = dense.ffill() if method == "conservative" else dense.interpolate(method="time")
    return dense.reindex(idx)


# Within-cadence interpolation is refinement; bridging a hole in the record is
# invention. The declared bound defaults to three native intervals.
def _refuse_gaps(s: Any, bound: Any, native: Any, pd: Any) -> None:
    gaps = s.index.to_series().diff().dropna()
    worst = gaps.max() if len(gaps) else pd.Timedelta(0)
    if worst > bound:
        at = gaps.idxmax()
        raise TemporalGapError(
            f"the record has a {_fmt(worst)} hole ending {at.isoformat()}, wider "
            f"than the declared max_gap of {_fmt(bound)} (native cadence "
            f"{_fmt(native)}). Bridging it would invent the missing interval; "
            "narrow the window, pick a source that covers it, or declare a "
            "max_gap that admits the hole on purpose."
        )


def _method(declared: str | None, quantity: str) -> str:
    if quantity not in _DEFAULT_METHOD:
        raise ModifierIllegalError(
            f"quantity class {quantity!r} is not one of "
            f"{tuple(_DEFAULT_METHOD)} - the class is what picks the method."
        )
    if declared is None:
        return _DEFAULT_METHOD[quantity]
    if quantity == CATEGORICAL and declared != "nearest":
        raise ModifierIllegalError(
            f".resample(method={declared!r}) is illegal on a CATEGORICAL quantity: "
            "class labels have no average and no slope, so nearest is the only "
            "honest move."
        )
    return declared


def _as_series(series: Any, pd: Any) -> Any:
    s = series if isinstance(series, pd.Series) else pd.Series(
        [v for _t, v in series], index=pd.to_datetime([t for t, _v in series]))
    if not isinstance(s.index, pd.DatetimeIndex):
        raise TemporalShapeError(
            "a resampled series must be indexed by time; got an index of "
            f"{type(s.index).__name__}."
        )
    s = s.sort_index()
    if len(s) < 2:
        raise TemporalShapeError(
            f"a series of {len(s)} point(s) has no cadence to resample from.")
    return s


def _native(s: Any, pd: Any) -> Any:
    """The source's own cadence: the LOWER-median sample spacing.
    Robust to a hole, and never reports a spacing the record does not contain -
    an interpolating median would turn 6h and 12h into an unsampled 9h."""
    diffs = s.index.to_series().diff().dropna()
    return pd.Timedelta(diffs.quantile(0.5, interpolation="lower"))


def _interval(text: str) -> Any:
    import pandas as pd
    from pandas.tseries.frequencies import to_offset

    try:
        return pd.Timedelta(to_offset(str(text)))
    except Exception as exc:  # noqa: BLE001 - a bad alias is a declaration fault
        raise PlanValidationError(
            f"{text!r} is not a fixed time interval (use a pandas offset alias "
            f"like '15min', '1h', '6h', '1D'): {exc}"
        ) from exc


def _max_gap(text: str, native: Any) -> Any:
    """``"native*3"`` (the default bound) or an explicit interval like ``"6h"``."""
    raw = str(text).strip()
    if raw.startswith("native"):
        tail = raw[len("native"):].strip()
        if not tail:
            return native
        if not tail.startswith("*"):
            raise PlanValidationError(
                f"max_gap={text!r} must be 'native', 'native*<k>' or an interval.")
        try:
            return native * float(tail[1:])
        except ValueError as exc:
            raise PlanValidationError(
                f"max_gap={text!r} has a non-numeric multiplier: {exc}") from exc
    return _interval(raw)


def _units_note(source: str, target: str) -> str:
    return (f"units {target} (declared, unchanged)" if source == target
            else f"converted {source}->{target}")


def _fmt(delta: Any) -> str:
    seconds = delta.total_seconds()
    for unit, size in (("D", 86400.0), ("h", 3600.0), ("min", 60.0)):
        if seconds >= size and seconds % size == 0:
            return f"{int(seconds // size)}{unit}"
    return f"{seconds:g}s"


def spec_from(to: str | None, method: str | None, max_gap: str,
              units: str | None, existing: TemporalSpec | None) -> TemporalSpec:
    """Fold one declared modifier into a producer's spec, keeping the other half."""
    base = existing or TemporalSpec()
    if to is not None:
        return TemporalSpec(resample=ResampleSpec(to=to, method=method,
                                                  max_gap=max_gap),
                            units=base.units)
    return TemporalSpec(resample=base.resample,
                        units=UnitsSpec(units=units) if units else base.units)
