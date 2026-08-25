"""Declared TEMPORAL TRANSFORMS - ``.resample(...)`` and ``.normalize(units=...)``.

pandas does the arithmetic; this module is the DOCTRINE around it.

Three rules decide every call, and none of them is a per-source constant:

* the QUANTITY CLASS picks the default method - a RATE resamples
  CONSERVATIVELY (mass-preserving), a STATE interpolates LINEARLY, a
  CATEGORICAL value moves by NEAREST and by nothing else. A caller may
  override the first two; asking to average class labels is refused.
* INTERPOLATION IS DECLARED. A transform that ran leaves a provenance stamp
  ("resampled 6h->1h linear"), so a manufactured value is never mistaken for
  an observed one, and a payload with no ``.resample()`` is never realigned
  behind the consumer's back.
* A HOLE WIDER THAN ``max_gap`` REFUSES. Within-cadence interpolation is
  refinement; bridging a hole in the record is invention, and this library
  does not invent the world. The default bound is three native intervals.

Unit conversion rides an EXPLICIT table (below), not a units engine: a
conversion nobody declared is a conversion nobody can check, and a
cross-dimension request refuses rather than guessing at intent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .errors import DeclarativeError, ModifierIllegalError, PlanValidationError

__all__ = [
    "CATEGORICAL",
    "RATE",
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

_DEFAULT_METHOD = {RATE: "conservative", STATE: "linear", CATEGORICAL: "nearest"}
_METHODS = ("conservative", "linear", "nearest")


class TemporalGapError(DeclarativeError):
    """The record has a hole wider than the declared ``max_gap``.

    Never bridged: the consumer asked for a cadence the source cannot honestly
    supply across this window, and a smooth line drawn over missing hours is a
    fabricated forcing.
    """

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

    ``series`` is a pandas ``Series`` on a ``DatetimeIndex``, or any sequence of
    ``(timestamp, value)`` pairs. The returned ``values`` is a pandas ``Series``;
    the returned ``note`` is the provenance stamp for the run's record.
    """
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

    ``native`` is the interval the value already represents. A ``.resample()`` to
    anything else REFUSES: one number carries no time axis to redistribute, so
    honoring the request would mean manufacturing the series it was asked for.
    """
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

    Robust to a hole (unlike the mean) and never reports a spacing the record
    does not actually contain (unlike an interpolating median, which turns 6h
    and 12h into a 9h cadence nothing was ever sampled at).
    """
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
