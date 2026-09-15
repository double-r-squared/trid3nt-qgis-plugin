"""The weather over the domain as ONE table: the value, and the file it becomes.

ONE writer for both hosts: the file is read by ``METEO_TELEMAC``, which sits in
BIEF rather than in either host, so TELEMAC-2D and TELEMAC-3D open the same
columns through the same scan - but each host writes only the columns its OWN
source term reads. A fetched station record is carried into the slots HERE,
because a unit conversion, a resampling and a station choice that make a record
fit a slot are that slot's own ingestion."""

from __future__ import annotations

import datetime as _dt
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from ..errors import TelemacError

__all__ = ["ATMOSPHERE_FILENAME", "COLUMNS", "TELEMAC2D_COLUMNS",
           "TELEMAC3D_COLUMNS", "Atmosphere", "atmospheric_data_file",
           "expand_for_telemac2d", "expand_for_telemac3d"]

#: The file a host's ASCII ATMOSPHERIC DATA FILE statement names.
ATMOSPHERE_FILENAME = "river_atmosphere.txt"

#: One row per slot the value carries: the mnemonic the reader searches the
#: header for, and the unit line's word for it. The reader matches a header
#: entry by INCLUSION, so no mnemonic may contain another.
#:
#: Pressures are written in PASCALS, which is the unit both the reader's own
#: constants and the thermal source term are in. The host's PRESSURE UNIT
#: keyword scales PATM and PVAP TOGETHER and scales them whether or not the file
#: supplied either, so stating it would multiply any constant standing in for an
#: absent column; Pa leaves that keyword on its engine default.
COLUMNS: Mapping[str, tuple[str, str]] = MappingProxyType({
    "air_temp_c": ("TAIR", "degC"),
    "vapour_pressure_pa": ("PVAP", "Pa"),
    "relative_humidity_pct": ("HREL", "%"),
    "wind_speed_mps": ("WINDS", "m/s"),
    "wind_from_deg": ("WINDD", "degree"),
    "cloud_octas": ("CLDC", "octa"),
    "solar_radiation_wm2": ("RAY3", "W/m2"),
    "pressure_pa": ("PATM", "Pa"),
    "rain_mm": ("RAINI", "mm"),
})

#: What each host's own heat budget reads. TELEMAC-2D's source term takes the
#: VAPOUR PRESSURE and TELEMAC-3D's takes the RELATIVE HUMIDITY; neither reads
#: the other's, so neither host writes a column it would only scan past.
TELEMAC2D_COLUMNS = tuple(n for n in COLUMNS if n != "relative_humidity_pct")
TELEMAC3D_COLUMNS = tuple(n for n in COLUMNS if n != "vapour_pressure_pa")

#: The reader's own name for the time column, which it refuses to start without.
_TIME = "T"

#: What the leading comment says when no record named what drove the run.
_PLAIN = "atmospheric data on the run's own clock"

#: One knot in metres per second, and one inch in millimetres.
_KNOT_MPS = 0.514444
_INCH_MM = 25.4

#: Magnus over water at the WMO's coefficients: saturation vapour pressure in
#: hectopascals from a temperature in degrees Celsius. Applied to the DEW POINT
#: it is the actual vapour pressure, which is what the 2D budget reads - the
#: engine computes the saturation value at the water's own temperature itself.
_MAGNUS_A, _MAGNUS_B, _MAGNUS_C = 6.112, 17.62, 243.12

#: The shortest record a table the engine interpolates between two rows can be,
#: and the longest gap between two instants that still describes a diurnal cycle.
_MIN_INSTANTS = 4
_MAX_GAP_S = 6.0 * 3600.0

#: What a station network reports, by the slot each column fills and the factor
#: that carries the network's unit into the slot's. Temperature and humidity are
#: read beside these, because neither is one multiplication.
_REPORTED: Mapping[str, tuple[str, float]] = MappingProxyType({
    "sknt": ("wind_speed_mps", _KNOT_MPS),
    "drct": ("wind_from_deg", 1.0),
    "solar_rad": ("solar_radiation_wm2", 1.0),
    "precip_in": ("rain_mm", _INCH_MM),
})


def Atmosphere(*, times_s: Any = None, air_temp_c: Any = None,  # noqa: N802
               vapour_pressure_pa: Any = None, relative_humidity_pct: Any = None,
               wind_speed_mps: Any = None, wind_from_deg: Any = None,
               cloud_octas: Any = None, solar_radiation_wm2: Any = None,
               pressure_pa: Any = None, rain_mm: Any = None,
               observed: Any = None, at: Any = None,
               duration_s: Any = None) -> Mapping[str, Any]:
    """The weather over the domain as time series on the run's own clock: what a
    heat budget reads, plus the rain and the wind beside it.

    ``observed`` is a fetched station record the nearest usable station is taken
    from; a series stated here stands over what that record reports, and one
    left out writes no column, so the host reads its own constant keyword."""
    # A MAPPING, not an object: the sheet's one ref walk descends mappings. Every
    # series shares one clock, because the file is ONE table - the engine
    # interpolates every column between the same two rows.
    return MappingProxyType({
        "times_s": times_s, "air_temp_c": air_temp_c,
        "vapour_pressure_pa": vapour_pressure_pa,
        "relative_humidity_pct": relative_humidity_pct,
        "wind_speed_mps": wind_speed_mps, "wind_from_deg": wind_from_deg,
        "cloud_octas": cloud_octas, "solar_radiation_wm2": solar_radiation_wm2,
        "pressure_pa": pressure_pa, "rain_mm": rain_mm,
        "observed": observed, "at": at, "duration_s": duration_s})


def atmospheric_data_file(value: Mapping[str, Any],
                          columns: Sequence[str] = TELEMAC2D_COLUMNS) -> str:
    """The value -> the table the reader scans, or the refusal that says why not.

    A comment naming what drove the run, the header, the units, then one row per
    instant, separated by single spaces."""
    times, stated, note = _resolved(value)
    times = [float(t) for t in times]
    if len(times) < 2:
        raise TelemacError(
            "the atmospheric data file needs at least two instants - the engine "
            f"interpolates between two rows and refuses a shorter table; got "
            f"{len(times)}.", error_code="TELEMAC_WEATHER_INCOMPLETE")
    if any(b <= a for a, b in zip(times, times[1:])):
        raise TelemacError("the atmospheric series' instants must strictly "
                           "increase on the run's own clock.",
                           error_code="TELEMAC_WEATHER_INCOMPLETE")
    series: list[tuple[str, str, list[float]]] = []
    for name in columns:
        if stated.get(name) is None:
            continue
        column = [float(v) for v in stated[name]]
        if len(column) != len(times):
            raise TelemacError(
                f"{name} carries {len(column)} values against {len(times)} "
                "instants; every series in one atmosphere shares its clock.",
                error_code="TELEMAC_WEATHER_INCOMPLETE")
        series.append((*COLUMNS[name], column))
    if not series:
        raise TelemacError(
            "an atmosphere with no series this host reads states nothing; omit "
            "it instead.", error_code="TELEMAC_WEATHER_EMPTY")
    # TABS ARE NOT WRITTEN. The reader's own error path names them as the cause
    # of a header it cannot split, and its list-directed row read is happier on
    # blanks; single spaces are what both scans are written against.
    rows = [f"#{note or _PLAIN}",
            " ".join([_TIME] + [mnemo for mnemo, _, _ in series]),
            " ".join(["s"] + [unit for _, unit, _ in series])]
    for index, instant in enumerate(times):
        rows.append(" ".join([f"{instant:.3f}"]
                             + [f"{column[index]:.9g}"
                                for _, _, column in series]))
    return "\n".join(rows) + "\n"


def expand_for_telemac2d(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                            Mapping[str, Any]]:
    """The atmosphere -> the file statement and the file, in TELEMAC-2D's columns."""
    return _expand(value, TELEMAC2D_COLUMNS)


def expand_for_telemac3d(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                            Mapping[str, Any]]:
    """The atmosphere -> the file statement and the file, in TELEMAC-3D's columns."""
    return _expand(value, TELEMAC3D_COLUMNS)


def _expand(value: Mapping[str, Any], columns: Sequence[str]
            ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The file statement is the whole of what this composite states.

    The host opens that file only when it has a reason to read the weather - a
    coupled water-quality module, a wind, an air pressure."""
    if value.get("times_s") is None and value.get("observed") is None:
        # Neither a clock nor a record came, so no weather was resolved.
        return ({}, {})
    return ({"ASCII_ATMOSPHERIC_DATA_FILE": ATMOSPHERE_FILENAME},
            {ATMOSPHERE_FILENAME: atmospheric_data_file(value, columns)})


def _resolved(value: Mapping[str, Any]) -> tuple[Any, dict[str, Any], str]:
    """The clock, the series under every slot, and what drove them.

    A stated series stands over what a record reports: the record fills what the
    caller left open, and nothing more."""
    stated = {name: value.get(name) for name in COLUMNS}
    if value.get("observed") is None:
        return value["times_s"], stated, ""
    times, reported, note = _from_record(value)
    return times, {name: stated[name] if stated[name] is not None
                   else reported.get(name) for name in COLUMNS}, note


def _from_record(value: Mapping[str, Any]) -> tuple[list[float],
                                                    dict[str, Any], str]:
    """The fetched station record -> the series on the run's own clock from zero.

    The NEAREST station whose observations can drive the run end to end is the
    one taken; every station refused is named with its reason."""
    from trid3nt_server.inputs.geometry import read_geometry_doc

    lon, lat = _lonlat(value["at"])
    rows = read_geometry_doc(value["observed"]).get("features") or []
    stations = _by_station(rows, lon, lat)
    if not stations:
        raise TelemacError(
            "the fetched weather record holds no station observation near the "
            "reach; widen the window or state the series directly.",
            error_code="TELEMAC_WEATHER_EMPTY")
    refusals: list[str] = []
    for distance_km, station, observations in stations:
        try:
            times, series, opened = _series(observations, value.get("duration_s"))
        except ValueError as why:
            refusals.append(f"{station} ({distance_km:.0f} km): {why}")
            continue
        stamp = opened.strftime("%Y-%m-%d %H:%M")
        return times, series, (
            f"the weather is the station {station}, {distance_km:.0f} km from "
            f"the reach: {len(times)} observations opening at {stamp} UTC, which "
            "is this run t = 0. A column the network does not report is absent, "
            "and the engine reads its own constant for it.")
    raise TelemacError(
        "no weather station near the reach carries a record this run can be "
        "driven by: " + "; ".join(refusals),
        error_code="TELEMAC_WEATHER_INCOMPLETE")


def _lonlat(at: Any) -> tuple[float, float]:
    """Where the reach is, from a resolved pair, a seed mapping, or a Point."""
    if isinstance(at, (list, tuple)):
        return float(at[0]), float(at[1])
    if isinstance(at, Mapping):
        return float(at["lon"]), float(at["lat"])
    return float(at.lon), float(at.lat)


def _by_station(rows: Sequence[Mapping[str, Any]], lon: float, lat: float
                ) -> list[tuple[float, str, list[Mapping[str, Any]]]]:
    """The rows grouped by the site they were read at, nearest the reach first."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    where: dict[str, tuple[float, float]] = {}
    for row in rows:
        properties = row.get("properties") or {}
        station = str(properties.get("station") or "").strip()
        coordinates = (row.get("geometry") or {}).get("coordinates")
        if not station or not coordinates:
            continue
        grouped.setdefault(station, []).append(properties)
        where[station] = (float(coordinates[0]), float(coordinates[1]))
    ranked = []
    for station, observations in grouped.items():
        # Degrees to kilometres at this latitude: the ranking only has to ORDER
        # stations, so the local flat-earth distance is the whole of what it needs.
        east = (where[station][0] - lon) * 111.32 * math.cos(math.radians(lat))
        north = (where[station][1] - lat) * 110.57
        ranked.append((math.hypot(east, north), station, observations))
    return sorted(ranked)


def _series(observations: Sequence[Mapping[str, Any]], duration_s: Any
            ) -> tuple[list[float], dict[str, Any], _dt.datetime]:
    """One station's observations -> the slots, on the run's clock from zero.

    t = 0 is the station's FIRST complete observation, so the run opens on an
    instant somebody measured rather than on a midnight nobody did."""
    kept: dict[_dt.datetime, dict[str, float]] = {}
    for observation in observations:
        stamp, values = _instant(observation.get("utc_valid")), _values(observation)
        if stamp is not None and values is not None:
            kept[stamp] = values
    stamps = sorted(kept)
    if len(stamps) < _MIN_INSTANTS:
        raise ValueError(f"{len(stamps)} complete observations in the window")
    times = [(stamp - stamps[0]).total_seconds() for stamp in stamps]
    gaps = [b - a for a, b in zip(times, times[1:])]
    if max(gaps) > _MAX_GAP_S:
        raise ValueError(f"a {max(gaps) / 3600.0:.0f} h gap in the record")
    # The engine STOPS when a requested instant falls outside the table at either
    # end, so a run longer than the record it is driven by cannot be authored.
    if duration_s is not None and times[-1] < float(duration_s):
        raise ValueError(f"the record runs {times[-1] / 3600.0:.0f} h and the "
                         f"run is {float(duration_s) / 3600.0:.0f} h long")
    return (times,
            {slot: [kept[stamp][slot] for stamp in stamps]
             for slot in kept[stamps[0]]},
            stamps[0])


def _instant(stamp: Any) -> _dt.datetime | None:
    """One reported timestamp -> an aware UTC datetime, or nothing."""
    if not stamp:
        return None
    try:
        read = _dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return read if read.tzinfo else read.replace(tzinfo=_dt.timezone.utc)


def _values(observation: Mapping[str, Any]) -> dict[str, float] | None:
    """One observation -> the slot values it carries, or nothing if any is absent.

    The file is ONE table, so an observation short of a column is not an instant."""
    read = {name: observation.get(name) for name in _REPORTED}
    read["tmpf"] = observation.get("tmpf")
    if any(value is None for value in read.values()):
        return None
    try:
        numbers = {name: float(value) for name, value in read.items()}
        air_c = (numbers.pop("tmpf") - 32.0) / 1.8
        humidity = _humidity(air_c, observation.get("dwpf"),
                             observation.get("relh"))
    except (TypeError, ValueError):
        return None
    if humidity is None:
        return None
    values = {"air_temp_c": round(air_c, 3), **humidity}
    for name, number in numbers.items():
        slot, factor = _REPORTED[name]
        values[slot] = round(max(0.0, number) * factor, 3)
    # A direction is an angle rather than a magnitude, so it wraps instead of
    # being floored at zero.
    values["wind_from_deg"] = round(numbers["drct"] % 360.0, 1)
    return values


def _humidity(air_c: float, dew_f: Any, relative_pct: Any
              ) -> dict[str, float] | None:
    """The two humidity slots, from whichever one the network reported.

    Each is the other over the saturation value at the air's own temperature, so
    one reported column states both - which is what lets one record drive a 2D
    run reading the vapour pressure and a 3D run reading the relative humidity."""
    saturation = _saturation_hpa(air_c)
    if dew_f is not None:
        vapour = _saturation_hpa((float(dew_f) - 32.0) / 1.8)
    elif relative_pct is not None:
        vapour = saturation * float(relative_pct) / 100.0
    else:
        return None
    return {"vapour_pressure_pa": round(100.0 * vapour, 1),
            "relative_humidity_pct": round(100.0 * vapour / saturation, 2)}


def _saturation_hpa(temp_c: float) -> float:
    """Magnus over water at ``temp_c``, in hectopascals."""
    return _MAGNUS_A * math.exp(_MAGNUS_B * temp_c / (_MAGNUS_C + temp_c))
