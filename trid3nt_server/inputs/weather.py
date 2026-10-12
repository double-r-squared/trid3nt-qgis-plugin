"""THE WEATHER over a place: one station's record, as a series per quantity.

The nearest station whose record brackets the run is the one read - nearest to the point
its row names, else to the middle of the run's domain - and every quantity its network
reports is kept in the unit its name states, on a clock whose zero is the moment the run
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

from .bound_domain import current_domain
from .observation import ObservationError, _distance_km, _features, _moment
from .run_notes import journal_note
from .temporal import Series, TemporalShapeError, convert_units

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

# One knot in m/s.
_KNOT_MPS = 0.514444

# Magnus over water (WMO coefficients): saturation vapour pressure in hPa from Celsius. On the dew point it is the actual vapour pressure.
_MAGNUS_A, _MAGNUS_B, _MAGNUS_C = 6.112, 17.62, 243.12

# The shortest record that states an interval at all.
_MIN_INSTANTS = 2

# The three quantities one humidity column states, read together: each is the others over the saturation value at the air temperature.
_HUMIDITY = ("vapour_pressure_pa", "relative_humidity_pct", "dew_point_c")

# A METAR sky-cover code as octas: clear, 1-2, 3-4, 5-7, 8, and an obscured sky as fully covered.
_SKY_OCTAS: Mapping[str, float] = MappingProxyType({
    "CLR": 0.0, "SKC": 0.0, "NCD": 0.0, "NSC": 0.0,
    "FEW": 2.0, "SCT": 4.0, "BKN": 6.0, "OVC": 8.0, "VV": 8.0})


def _celsius(value: Any) -> float:
    return round(convert_units(float(value), "F", "degC"), 3)


def _knots(value: Any) -> float:
    return round(max(0.0, float(value)) * _KNOT_MPS, 3)


def _bearing(value: Any) -> float:
    """A wind direction wraps; it is an angle."""
    return round(float(value) % 360.0, 1)


def _millimetres(value: Any) -> float:
    return round(convert_units(max(0.0, float(value)), "in", "mm"), 3)


def _pascals(value: Any) -> float:
    return round(float(value) * 100.0, 1)


def _watts(value: Any) -> float:
    return round(max(0.0, float(value)), 3)


def _octas(value: Any) -> float | None:
    """A METAR sky-cover code as octas; anything else reports no cloud."""
    return _SKY_OCTAS.get(str(value).strip().upper()[:3])


# What each station network reports, by quantity: the network's column name and what carries its unit.
# The fire-weather network is the one hourly record carrying solar radiation; its precipitation is a
# season accumulator, so no rain is read from it.
_RAWS: Mapping[str, tuple[str, Any]] = MappingProxyType({
    "air_temp_c": ("tmpf", _celsius),
    "wind_speed_mps": ("sknt", _knots),
    "wind_from_deg": ("drct", _bearing),
    "solar_radiation_wm2": ("solar_rad", _watts),
})
# The airport hourly record: a real dew point, coded sky cover, pressure, past-hour precipitation; no radiation.
_ASOS: Mapping[str, tuple[str, Any]] = MappingProxyType({
    "air_temp_c": ("tmpf", _celsius),
    "wind_speed_mps": ("sknt", _knots),
    "wind_from_deg": ("drct", _bearing),
    "pressure_pa": ("mslp", _pascals),
    "cloud_octas": ("skyc1", _octas),
    "rain_mm": ("p01i", _millimetres),
})

# Which network a row came from, by the column its instant is stamped in; the two report different instruments.
_NETWORKS: Mapping[str, Mapping[str, tuple[str, Any]]] = MappingProxyType({
    "utc_valid": _RAWS, "valid": _ASOS})


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


def weather(source: Any, *, near: Any = None, at: Any = None,
            window_s: Any = None, label: str = "weather",
            code: str = _CODE) -> Weather | None:
    """THE ingestion: a fetched station record -> one station's series, every quantity kept.

    ``near`` ranks the stations, the domain's middle where the row names no point, and the
    nearest whose record brackets the window opening at ``at`` is taken; a station short of
    one quantity is still that station."""
    if source is None:
        return None
    dom = current_domain()
    if near is None and dom is not None and dom.bbox:
        west, south, east, north = dom.bbox
        near = ((west + east) / 2.0, (south + north) / 2.0)
    stations = _by_station(_features(source), near)
    if not stations:
        raise ObservationError(
            code, f"{label} holds no station observation near this place; widen "
            "the window or state the series directly.")
    refusals: list[str] = []
    for distance_km, station, observations in stations:
        how_far = (f"{station} ({distance_km:.0f} km)" if math.isfinite(distance_km)
                   else station)
        try:
            columns, short, opens = _series(observations, at, window_s)
        except ValueError as why:
            refusals.append(f"{how_far}: {why}")
            continue
        journal_note(
            f"{label} is the station {how_far}, read from "
            f"{opens:%Y-%m-%d %H:%M} UTC: it carries {', '.join(columns) or 'nothing'}"
            + (f" and is short of {', '.join(short)}." if short else "."))
        return Weather(columns=columns, short=short, opens=opens, site_id=station,
                       distance_km=distance_km if math.isfinite(distance_km)
                       else None)
    raise ObservationError(
        code, f"no station in {label} carries a record this run can be driven "
        "by: " + "; ".join(refusals))


def _by_station(rows: Sequence[Mapping[str, Any]], near: Any
                ) -> list[tuple[float, str, list[Mapping[str, Any]]]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    far: dict[str, float] = {}
    for row in rows:
        properties = row.get("properties") or {}
        station = str(properties.get("station") or "").strip()
        if not station or not (row.get("geometry") or {}).get("coordinates"):
            continue
        grouped.setdefault(station, []).append(properties)
        far[station] = _distance_km(row, near)
    return sorted((far[station], station, observations)
                  for station, observations in grouped.items())


def _series(observations: Sequence[Mapping[str, Any]], at: Any, window_s: Any
            ) -> tuple[dict[str, Series], dict[str, str], _dt.datetime]:
    """One station's observations -> a series per quantity, on the run's clock from zero.

    t = 0 is the moment the run opens at, and each series is read between its last reading at or
    before it and its first at or after the close. With no moment, the first observation is the origin.
    """
    kept: dict[str, dict[_dt.datetime, float]] = {}
    reports: set[str] = set()
    stamped: set[_dt.datetime] = set()
    for observation in observations:
        reported = _network(observation)
        if reported is None:
            continue
        when, network = reported
        stamp = _moment(observation.get(when))
        if stamp is None:
            continue
        stamped.add(stamp)
        reports |= {*network, *_HUMIDITY}
        for name, value in _values(observation, network).items():
            kept.setdefault(name, {})[stamp] = value
    stamps = sorted(stamped)
    if len(stamps) < _MIN_INSTANTS:
        raise ValueError(f"{len(stamps)} observations in the window")
    opens = _moment(at) or stamps[0]
    closes = (opens + _dt.timedelta(seconds=float(window_s))
              if window_s is not None else stamps[-1])
    _bracketing(stamps, opens, closes)
    columns: dict[str, Series] = {}
    short: dict[str, str] = {}
    for name in UNITS:
        if name not in reports:
            continue
        try:
            held = _bracketing(sorted(kept.get(name, ())), opens, closes)
            columns[name] = Series.from_samples(
                [(stamp, kept[name][stamp]) for stamp in held],
                units=UNITS[name], at=opens)
        except (ValueError, TemporalShapeError) as why:
            short[name] = str(why)
    return columns, short, opens


def _bracketing(stamps: Sequence[_dt.datetime], opens: _dt.datetime,
                closes: _dt.datetime) -> list[_dt.datetime]:
    """The readings the run is read between: last at or before the open, first at or after the close.

    A reader stops when an instant falls outside its table; the refusal names the missing end.
    """
    if not stamps:
        raise ValueError("it holds no reading at all")
    if stamps[0] > opens or stamps[-1] < closes:
        raise ValueError(
            f"the record runs {stamps[0]:%Y-%m-%d %H:%M} to "
            f"{stamps[-1]:%Y-%m-%d %H:%M} UTC "
            f"({(stamps[-1] - stamps[0]).total_seconds() / 3600.0:.0f} h) and "
            f"the run opens at {opens:%Y-%m-%d %H:%M} and closes at "
            f"{closes:%Y-%m-%d %H:%M} "
            f"({(closes - opens).total_seconds() / 3600.0:.0f} h): it holds no "
            "observation at or "
            + ("before the opening" if stamps[0] > opens else "after the close"))
    return list(stamps[bisect.bisect_right(stamps, opens) - 1:
                       bisect.bisect_left(stamps, closes) + 1])


def _network(observation: Mapping[str, Any]
             ) -> tuple[str, Mapping[str, tuple[str, Any]]] | None:
    for when, network in _NETWORKS.items():
        if observation.get(when):
            return when, network
    return None


def _values(observation: Mapping[str, Any],
            network: Mapping[str, tuple[str, Any]]) -> dict[str, float]:
    """One observation -> every quantity it carries a readable value of."""
    values: dict[str, float] = {}
    for name, (column, carry) in network.items():
        try:
            read = carry(observation[column])
        except (KeyError, TypeError, ValueError):
            continue
        if read is not None:
            values[name] = read
    try:
        humidity = _humidity(_celsius(float(observation["tmpf"])),
                             observation.get("dwpf"), observation.get("relh"))
    except (KeyError, TypeError, ValueError):
        return values
    return {**values, **(humidity or {})}


def _humidity(air_c: float, dew_f: Any, relative_pct: Any
              ) -> dict[str, float] | None:
    """The three humidity quantities, from whichever one the network reported.

    Each is the other over the saturation value at the air temperature, so one record drives a
    vapour-pressure reader and a relative-humidity reader.
    """
    saturation = _saturation_hpa(air_c)
    if dew_f is not None:
        vapour = _saturation_hpa((float(dew_f) - 32.0) / 1.8)
    elif relative_pct is not None:
        vapour = saturation * float(relative_pct) / 100.0
    else:
        return None
    return {"vapour_pressure_pa": round(100.0 * vapour, 1),
            "relative_humidity_pct": round(100.0 * vapour / saturation, 2),
            "dew_point_c": round(_dew_point_c(vapour), 3)}


def _saturation_hpa(temp_c: float) -> float:
    return _MAGNUS_A * math.exp(_MAGNUS_B * temp_c / (_MAGNUS_C + temp_c))


def _dew_point_c(vapour_hpa: float) -> float:
    """The temperature this vapour pressure saturates at (Magnus inverted), so a network reporting relative humidity states this quantity too."""
    ratio = math.log(max(vapour_hpa, 1.0e-6) / _MAGNUS_A)
    return _MAGNUS_C * ratio / (_MAGNUS_B - ratio)
