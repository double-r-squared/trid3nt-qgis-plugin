"""THE WEATHER over a place: one station's record, as a series per quantity.

The nearest station whose record brackets the run is the one read - nearest to the point
its row names, else to the middle of the run's domain - and every quantity its source maps
is kept in the unit its name states, on a clock whose zero is the moment the run
opens at. Which quantities a solve reads, and the file they become,
are its engine's.
"""

from __future__ import annotations

import bisect
import datetime as _dt
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from trid3nt_contracts.coverage import Member
from trid3nt.errors import untabled_readings_note

from .bound_domain import current_domain
from .fields import FieldError, Record
from .observation import ObservationError, _distance_km, _features, _moment, convert
from .run_notes import journal_note
from .temporal import Series, TemporalShapeError
from .temporal import spelling

__all__ = ["UNITS", "Weather", "weather"]

_CODE = "WEATHER_INVALID"

#: Every quantity a weather record states, and the unit its series is carried in.
UNITS: Mapping[str, str] = MappingProxyType({
    "air_temp_c": "degC",
    "dew_point_c": "degC",
    "vapour_pressure_pa": "Pa",
    "relative_humidity_pct": "%",
    "wind_speed_mps": "m/s",
    "wind_from_deg": "degree",
    "cloud_octas": "octa",
    "solar_radiation_wm2": "W/m2",
    "pressure_pa": "Pa",
    "rain_mm": "mm",
})

# The saturation vapour pressure over water (Magnus, WMO coefficients) in hPa from Celsius; on the dew point it is the actual vapour pressure.
_MAGNUS_A, _MAGNUS_B, _MAGNUS_C = 6.112, 17.62, 243.12

# The shortest record that states an interval at all.
_MIN_INSTANTS = 2

# The three quantities one humidity reading states, read together: each is the others over the saturation value at the air temperature.
_HUMIDITY = ("vapour_pressure_pa", "relative_humidity_pct", "dew_point_c")

#: The member a source states each quantity under; vapour pressure is closed from the dew point or the relative humidity, so no source states it.
_MEMBER: Mapping[str, str] = MappingProxyType({
    "air_temp_c": "air_temperature", "dew_point_c": "dew_point",
    "relative_humidity_pct": "relative_humidity", "wind_speed_mps": "wind_speed",
    "wind_from_deg": "wind_from", "cloud_octas": "cloud_cover",
    "solar_radiation_wm2": "solar_radiation", "pressure_pa": "pressure", "rain_mm": "rain"})

#: How a record no source states a field map for is read: the layer a user hands over names its station ``station``, its
#: instant ``time`` and each quantity under its name in :data:`UNITS`, in that unit.
_UNSOURCED: Mapping[str, Member] = {
    "site": Member(field="station"), "time": Member(field="time"),
    **{_MEMBER[name]: Member(field=name, unit=UNITS[name]) for name in _MEMBER}}

# Factors onto the unit each quantity is carried in, by the spelling of the unit a member states.
_TO_UNITS: Mapping[str, Mapping[str, float]] = MappingProxyType({
    "wind_speed_mps": {"kt": 0.514444, "kn": 0.514444, "knot": 0.514444, "knots": 0.514444, "m/s": 1.0},
    "pressure_pa": {"hpa": 100.0, "mb": 100.0, "mbar": 100.0, "pa": 1.0, "inhg": 3386.389},
    "wind_from_deg": {"degree": 1.0, "degrees": 1.0, "deg": 1.0, "degt": 1.0},
    "solar_radiation_wm2": {"w/m2": 1.0, "wm2": 1.0},
    "relative_humidity_pct": {"%": 1.0, "pct": 1.0, "percent": 1.0},
    "cloud_octas": {"octa": 1.0, "octas": 1.0}})


@dataclass(frozen=True, slots=True)
class Weather:
    """One station's record over the run's window, a series per quantity."""

    #: By quantity, in :data:`UNITS`; each brackets the window on its own readings.
    columns: Mapping[str, Series]
    #: Why a quantity this station's network reports carries no series here.
    short: Mapping[str, str]
    #: The UTC instant every series reads zero at.
    opens: _dt.datetime
    site_id: str
    distance_km: float | None = None


def weather(source: Any, *, fields: Mapping[str, Member] | None = None, producer: str = "",
            near: Any = None, at: Any = None, window_s: Any = None, label: str = "weather",
            code: str = _CODE) -> Weather | None:
    """THE ingestion: a fetched station record -> one station's series, every quantity its source maps.

    ``fields`` is the source's map: its ``site``, ``time`` and one member per quantity it reports.
    ``near`` ranks the stations, the domain's middle where the row names no point, and the
    nearest whose record brackets the window opening at ``at`` is taken; a station short of
    one quantity is still that station."""
    if source is None:
        return None
    fields = dict(fields) if fields else dict(_UNSOURCED)
    for member in ("site", "time"):
        if member not in fields:
            raise ObservationError(
                code, f"{label} maps no {member!r} member, so its readings cannot be "
                "told by station and instant: state it in the source's fields.")
    dom = current_domain()
    if near is None and dom is not None and dom.bbox:
        west, south, east, north = dom.bbox
        near = ((west + east) / 2.0, (south + north) / 2.0)
    read = _Reader(fields, producer or label, label, code)
    stations = _by_station(_features(source), near, read)
    if not stations:
        raise ObservationError(
            code, f"{label} holds no station observation near this place; widen "
            "the window or state the series directly.")
    refusals: list[str] = []
    for distance_km, station, observations in stations:
        how_far = (f"{station} ({distance_km:.0f} km)" if math.isfinite(distance_km)
                   else station)
        try:
            columns, short, opens, untabled = _series(observations, read, at, window_s)
        except ValueError as why:
            refusals.append(f"{how_far}: {why}")
            continue
        journal_note(
            f"{label} is the station {how_far}, read from "
            f"{opens:%Y-%m-%d %H:%M} UTC: it carries {', '.join(columns) or 'nothing'}"
            + (f" and is short of {', '.join(short)}." if short else "."))
        if untabled:
            journal_note(untabled_readings_note(untabled))
        return Weather(columns=columns, short=short, opens=opens, site_id=station,
                       distance_km=distance_km if math.isfinite(distance_km)
                       else None)
    raise ObservationError(
        code, f"no station in {label} carries a record this run can be driven "
        "by: " + "; ".join(refusals))


class _Reader:
    """A source's map over its observations: which quantities it reports, and one observation read through it."""

    def __init__(self, fields: Mapping[str, Member], producer: str, label: str, code: str) -> None:
        self.fields, self.producer, self.label, self.code = fields, producer, label, code
        self.reports = {name for name, member in _MEMBER.items() if member in fields}
        if self.reports & {"dew_point_c", "relative_humidity_pct"}:
            self.reports |= set(_HUMIDITY)

    def record(self, props: Mapping[str, Any]) -> Record:
        return Record(props, self.fields, source=self.producer)

    def moment(self, rec: Record) -> _dt.datetime | None:
        return _moment(rec.stated("time"))

    def values(self, rec: Record, untabled: dict[str, int]) -> dict[str, float]:
        """One observation -> every quantity it carries a readable value of; a code its table lacks gives none, counted."""
        read: dict[str, float] = {}
        for name, member in _MEMBER.items():
            if member not in self.fields:
                continue
            try:
                found = rec.read(member)
            except FieldError as why:
                if why.error_code in ("FIELD_EMPTY", "FIELD_NOT_A_NUMBER"):
                    continue
                if why.error_code == "FIELD_UNTABLED":
                    untabled[member] = untabled.get(member, 0) + 1
                    continue
                raise
            read[name] = self._carry(name, found.value, found.unit)
        closed = _humidity(read.get("air_temp_c"), read.pop("dew_point_c", None),
                           read.pop("relative_humidity_pct", None))
        return {**read, **closed}

    def _carry(self, name: str, value: Any, unit: str | None) -> float:
        """One reading in the unit its quantity is carried in, or a refusal naming the unit its member states."""
        if name in ("air_temp_c", "dew_point_c"):
            return round(convert(value, unit, "degC"), 3)
        if name == "rain_mm":
            return round(convert(max(0.0, value), unit, "mm"), 3)
        factor = _TO_UNITS[name].get(spelling(unit))
        if factor is None:
            raise ObservationError(
                self.code, f"{self.label} states {_MEMBER[name]!r} in {unit or 'no unit'!r}, "
                f"which weather does not carry as {UNITS[name]}: its source states the unit "
                f"on the member, one of {', '.join(sorted(_TO_UNITS[name]))}.")
        read = value * factor
        if name == "wind_from_deg":
            return round(read % 360.0, 1)
        if name == "pressure_pa":
            return round(read, 1)
        if name == "relative_humidity_pct":
            return round(read, 2)
        return round(max(0.0, read), 3) if name != "cloud_octas" else read


def _by_station(rows: Sequence[Mapping[str, Any]], near: Any, read: _Reader
                ) -> list[tuple[float, str, list[Record]]]:
    grouped: dict[str, list[Record]] = {}
    far: dict[str, float] = {}
    for row in rows:
        rec = read.record(row.get("properties") or {})
        station = str(rec.stated("site") or "").strip()
        if not station or not (row.get("geometry") or {}).get("coordinates"):
            continue
        rec.site = station
        grouped.setdefault(station, []).append(rec)
        far[station] = _distance_km(row, near)
    return sorted((far[station], station, observations)
                  for station, observations in grouped.items())


def _series(observations: Sequence[Record], read: _Reader, at: Any, window_s: Any
            ) -> tuple[dict[str, Series], dict[str, str], _dt.datetime, dict[str, int]]:
    """One station's observations -> a series per quantity, on the run's clock from zero.

    t = 0 is the moment the run opens at, and each series is read between its last reading at or
    before it and its first after the close. With no moment, the first observation is the origin.
    """
    kept: dict[str, dict[_dt.datetime, float]] = {}
    untabled: dict[str, int] = {}
    stamped: set[_dt.datetime] = set()
    for rec in observations:
        stamp = read.moment(rec)
        if stamp is None:
            continue
        stamped.add(stamp)
        for name, value in read.values(rec, untabled).items():
            kept.setdefault(name, {})[stamp] = value
    stamps = sorted(stamped)
    if len(stamps) < _MIN_INSTANTS:
        raise ValueError(f"{len(stamps)} observations in the window")
    opens = _moment(at) or stamps[0]
    closes = (opens + _dt.timedelta(seconds=float(window_s))
              if window_s is not None else None)
    _bracketing(stamps, opens, closes)
    columns: dict[str, Series] = {}
    short: dict[str, str] = {}
    for name in UNITS:
        if name not in read.reports:
            continue
        try:
            held = _bracketing(sorted(kept.get(name, ())), opens, closes)
            columns[name] = Series.from_samples(
                [(stamp, kept[name][stamp]) for stamp in held],
                units=UNITS[name], at=opens)
        except (ValueError, TemporalShapeError) as why:
            short[name] = str(why)
    return columns, short, opens, untabled


def _bracketing(stamps: Sequence[_dt.datetime], opens: _dt.datetime,
                closes: _dt.datetime | None) -> list[_dt.datetime]:
    """The readings the run is read between: last at or before the open, first after the close.

    A reader stops when an instant falls outside its table, and a solve can step just past its
    close, so a record ending on the close is short; the refusal names the missing end. With no
    close stated the record is read to its own end.
    """
    if not stamps:
        raise ValueError("it holds no reading at all")
    late = closes is not None and stamps[-1] <= closes
    if closes is None:
        closes = stamps[-1]
    if stamps[0] > opens or late:
        raise ValueError(
            f"the record runs {stamps[0]:%Y-%m-%d %H:%M} to "
            f"{stamps[-1]:%Y-%m-%d %H:%M} UTC "
            f"({(stamps[-1] - stamps[0]).total_seconds() / 3600.0:.0f} h) and "
            f"the run opens at {opens:%Y-%m-%d %H:%M} and closes at "
            f"{closes:%Y-%m-%d %H:%M} "
            f"({(closes - opens).total_seconds() / 3600.0:.0f} h): it holds no "
            "observation "
            + ("at or before the opening" if stamps[0] > opens else "after the close"))
    return list(stamps[bisect.bisect_right(stamps, opens) - 1:
                       bisect.bisect_right(stamps, closes) + 1])


def _humidity(air_c: float | None, dew_c: float | None, relative_pct: float | None
              ) -> dict[str, float]:
    """The three humidity quantities, from whichever one the source reported, closed at the air temperature.

    Each is the other over the saturation value at the air temperature, so one record drives a
    vapour-pressure reader and a relative-humidity reader."""
    if air_c is None:
        return {}
    saturation = _saturation_hpa(air_c)
    if dew_c is not None:
        vapour = _saturation_hpa(dew_c)
    elif relative_pct is not None:
        vapour = saturation * relative_pct / 100.0
    else:
        return {}
    return {"vapour_pressure_pa": round(100.0 * vapour, 1),
            "relative_humidity_pct": round(100.0 * vapour / saturation, 2),
            "dew_point_c": round(_dew_point_c(vapour), 3)}


def _saturation_hpa(temp_c: float) -> float:
    return _MAGNUS_A * math.exp(_MAGNUS_B * temp_c / (_MAGNUS_C + temp_c))


def _dew_point_c(vapour_hpa: float) -> float:
    """The temperature this vapour pressure saturates at (Magnus inverted), so a network reporting relative humidity states this quantity too."""
    ratio = math.log(max(vapour_hpa, 1.0e-6) / _MAGNUS_A)
    return _MAGNUS_C * ratio / (_MAGNUS_B - ratio)
