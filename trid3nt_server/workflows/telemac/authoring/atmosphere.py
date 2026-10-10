"""The weather over the domain as one table: the value, and the file it becomes.

One writer for both hosts: ``METEO_TELEMAC`` (in BIEF) reads the file, but each host writes only
the columns its own source term reads. A fetched station record is carried into the slots here,
because unit conversion, resampling and station choice are the slot's own ingestion.
"""

from __future__ import annotations

import bisect
import datetime as _dt
import inspect
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from trid3nt_server.inputs.series import align
from trid3nt_server.workflows.runtime.temporal import (
    Series,
    TemporalGapError,
)

from ..errors import TelemacError

__all__ = ["ATMOSPHERE_FILENAME", "ATMOSPHERE_READS", "COLUMNS", "DISPUTED", "READS",
           "TELEMAC2D_COLUMNS", "TELEMAC3D_COLUMNS", "Atmosphere",
           "atmospheric_data_file", "expand_atmosphere", "write_atmosphere"]

# The file a host's ASCII ATMOSPHERIC DATA FILE statement names.
ATMOSPHERE_FILENAME = "river_atmosphere.txt"

# One row per slot: the mnemonic the reader searches the header for, and the unit line's word.
# The reader matches a header entry by inclusion, so no mnemonic may contain another. Pressures are
# in pascals: the host's PRESSURE UNIT keyword scales PATM and PVAP together even for an absent
# column, so it stays on its engine default.
COLUMNS: Mapping[str, tuple[str, str]] = MappingProxyType({
    "air_temp_c": ("TAIR", "degC"),
    # The dew point in Celsius, which KHIONE's thermal budget adds 273.16 to (``thermal_khione.f`` TDK); neither host reads it, it rides the file for the coupled module.
    "dew_point_c": ("TDEW", "degC"),
    "vapour_pressure_pa": ("PVAP", "Pa"),
    "relative_humidity_pct": ("HREL", "%"),
    "wind_speed_mps": ("WINDS", "m/s"),
    "wind_from_deg": ("WINDD", "degree"),
    "cloud_octas": ("CLDC", "octa"),
    "solar_radiation_wm2": ("RAY3", "W/m2"),
    "pressure_pa": ("PATM", "Pa"),
    "rain_mm": ("RAINI", "mm"),
})

# What each host's heat budget reads: 2D takes vapour pressure, 3D relative humidity. The dew point is in both for a coupled module.
TELEMAC2D_COLUMNS = tuple(n for n in COLUMNS if n != "relative_humidity_pct")
TELEMAC3D_COLUMNS = tuple(n for n in COLUMNS if n != "vapour_pressure_pa")

# The columns each module's source terms read, by the keyword it reads them under (empty where
# always). A run writes the union of its readers' columns only: requiring an unread column would
# discard rows that could drive the run.
READS: Mapping[str, Mapping[str, str]] = MappingProxyType({
    "telemac2d": MappingProxyType({
        "wind_speed_mps": "WIND", "wind_from_deg": "WIND",
        "pressure_pa": "AIR_PRESSURE", "rain_mm": "RAIN_OR_EVAPORATION"}),
    "telemac3d": MappingProxyType({
        "wind_speed_mps": "WIND", "wind_from_deg": "WIND",
        "pressure_pa": "AIR_PRESSURE", "rain_mm": "RAIN_OR_EVAPORATION"}),
    # ``waqtel/calcs2d_thermic.f``: the surface budget divides shortwave by depth and takes air, vapour, cloud, wind, pressure; the 3D branch takes relative humidity.
    "waqtel": MappingProxyType({
        "air_temp_c": "", "vapour_pressure_pa": "", "relative_humidity_pct": "",
        "cloud_octas": "", "solar_radiation_wm2": "", "wind_speed_mps": "",
        "pressure_pa": ""}),
    # ``khione/source_thermal.f`` takes TAIR, TDEW, CLDC, WINDS, RAINFALL and computes shortwave itself from cloud and longitude, so an ice run needs no solar column.
    "khione": MappingProxyType({
        "air_temp_c": "", "dew_point_c": "", "cloud_octas": "",
        "wind_speed_mps": "", "rain_mm": ""}),
})

# The column set each host can carry at all, by module.
_HOST_COLUMNS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "telemac2d": TELEMAC2D_COLUMNS, "telemac3d": TELEMAC3D_COLUMNS})

# The unit a column is written in where its module takes another than the row states: factor off
# the canonical value and the unit word. Applied only where that module is the column's only reader;
# two readers in two units refuse.
_IN_ITS_UNIT: Mapping[tuple[str, str], tuple[float, str]] = MappingProxyType({
    ("khione", "cloud_octas"): (1.25, "tenth"),
    ("khione", "rain_mm"): (1.0, "mm/h"),
})

# The reader's name for the time column, which it refuses to start without.
_TIME = "T"

# What the leading comment says when no record named what drove the run.
_PLAIN = "atmospheric data on the run's own clock"

# One knot in m/s, one inch in mm.
_KNOT_MPS = 0.514444
_INCH_MM = 25.4

# Magnus over water (WMO coefficients): saturation vapour pressure in hPa from Celsius. On the dew point it is the actual vapour pressure the 2D budget reads.
_MAGNUS_A, _MAGNUS_B, _MAGNUS_C = 6.112, 17.62, 243.12

# Shortest record and longest gap still describing a diurnal cycle. A record must bracket the run; a floor above the two rows the engine reads between would refuse an hour-long run.
_MIN_INSTANTS = 2
_MAX_GAP_S = 6.0 * 3600.0

# The three slots one humidity column states, read together: each is the others over the saturation value at the air temperature.
_HUMIDITY = ("vapour_pressure_pa", "relative_humidity_pct", "dew_point_c")

# A METAR sky-cover code as octas: clear, 1-2, 3-4, 5-7, 8, and an obscured sky as fully covered.
_SKY_OCTAS: Mapping[str, float] = MappingProxyType({
    "CLR": 0.0, "SKC": 0.0, "NCD": 0.0, "NSC": 0.0,
    "FEW": 2.0, "SCT": 4.0, "BKN": 6.0, "OVC": 8.0, "VV": 8.0})


def _celsius(value: Any) -> float:
    return round((float(value) - 32.0) / 1.8, 3)


def _knots(value: Any) -> float:
    return round(max(0.0, float(value)) * _KNOT_MPS, 3)


def _bearing(value: Any) -> float:
    """A wind direction wraps; it is an angle."""
    return round(float(value) % 360.0, 1)


def _millimetres(value: Any) -> float:
    return round(max(0.0, float(value)) * _INCH_MM, 3)


def _pascals(value: Any) -> float:
    return round(float(value) * 100.0, 1)


def _watts(value: Any) -> float:
    return round(max(0.0, float(value)), 3)


def _octas(value: Any) -> float | None:
    """A METAR sky-cover code as octas; anything else reports no cloud."""
    return _SKY_OCTAS.get(str(value).strip().upper()[:3])


# What each station network reports, by slot: the network's column name and what carries its unit.
# A slot no row names is absent from the file and the engine reads its constant keyword.
# The fire-weather network is the one hourly record carrying solar radiation; its precipitation is a
# season accumulator, so no rain slot is filled from it.
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


def Atmosphere(*, times_s: Any = None, air_temp_c: Any = None,  # noqa: N802
               dew_point_c: Any = None,
               vapour_pressure_pa: Any = None, relative_humidity_pct: Any = None,
               wind_speed_mps: Any = None, wind_from_deg: Any = None,
               cloud_octas: Any = None, solar_radiation_wm2: Any = None,
               pressure_pa: Any = None, rain_mm: Any = None,
               observed: Any = None, at: Any = None,
               duration_s: Any = None,
               event_time: Any = None) -> Mapping[str, Any]:
    """The weather over the domain as time series on the run's own clock.

    ``observed`` is a fetched station record; a series stated here stands over it, and one left out
    writes no column. ``event_time`` is the table's t = 0.
    """
    # One clock: the file is one table and the engine interpolates every column between the same two rows.
    return MappingProxyType({
        "times_s": times_s, "air_temp_c": air_temp_c,
        "dew_point_c": dew_point_c,
        "vapour_pressure_pa": vapour_pressure_pa,
        "relative_humidity_pct": relative_humidity_pct,
        "wind_speed_mps": wind_speed_mps, "wind_from_deg": wind_from_deg,
        "cloud_octas": cloud_octas, "solar_radiation_wm2": solar_radiation_wm2,
        "pressure_pa": pressure_pa, "rain_mm": rain_mm,
        "observed": observed, "at": at, "duration_s": duration_s,
        "event_time": event_time})


# Every argument takes a value or an input's name.
ATMOSPHERE_READS = tuple(inspect.signature(Atmosphere).parameters)


# Columns whose unit two readers disagree on, by mnemonic and the module reading it in the unit
# written here. The reader skips the unit line. KHIONE reads cloud cover in tenths (WAQTEL divides by
# eight) and rain as mm/h where METEO_TELEMAC reads metres accumulated over the step.
DISPUTED: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "cloud_octas": ("khione",),
    "rain_mm": ("khione",),
})


def write_atmosphere(body: Any, stated: Mapping[str, Any],
                     files: dict[str, Any]) -> None:
    """The weather table, written in the columns this run's readers read.

    Resolved here because the coupling and switches arrive in any order; where two enabled
    modules read one column in different units, it refuses.
    """
    value = files.get(ATMOSPHERE_FILENAME)
    if not isinstance(value, Mapping) or "slots" in value:
        return
    coupled = [str(content["module"]) for content in files.values()
               if isinstance(content, Mapping) and "slots" in content]
    readers = _readers(body, stated, coupled)
    columns = tuple(name for name in _HOST_COLUMNS.get(body.MODULE, ())
                    if name in readers)
    if not columns:
        raise TelemacError(
            "the weather this run was handed is read by nothing on it: "
            f"{body.MODULE} opens the atmospheric file for its own terms under "
            + ", ".join(sorted({switch for switch in
                                READS.get(body.MODULE, {}).values() if switch}))
            + ", this deck states none of them true, and no coupled module "
            "reads a column either; state the switch the column is read under, "
            "or drop the atmosphere.", error_code="TELEMAC_WEATHER_EMPTY")
    _refuse_disputed(readers)
    files[ATMOSPHERE_FILENAME] = atmospheric_data_file(
        value, columns,
        {name: _IN_ITS_UNIT[(modules[0], name)] for name, modules in
         readers.items()
         if len(modules) == 1 and (modules[0], name) in _IN_ITS_UNIT})


def _readers(body: Any, stated: Mapping[str, Any],
             coupled: Sequence[str]) -> dict[str, list[str]]:
    reading: dict[str, list[str]] = {}
    for name, switch in READS.get(body.MODULE, {}).items():
        if not switch or body.switched(switch, stated):
            reading.setdefault(name, []).append(body.MODULE)
    for module in coupled:
        for name in READS.get(module, ()):
            reading.setdefault(name, []).append(module)
    return reading


def _refuse_disputed(readers: Mapping[str, Sequence[str]]) -> None:
    """A column two readers take in two units refuses; one file carries one number per instant."""
    named = sorted(name for name, modules in DISPUTED.items()
                   if set(modules) & set(readers.get(name, ()))
                   and set(readers.get(name, ())) - set(modules))
    if not named:
        return
    reading = sorted({module for name in named for module in DISPUTED[name]
                      if module in readers[name]})
    other = sorted({module for name in named for module in readers[name]
                    if module not in DISPUTED[name]})
    raise TelemacError(
        f"{', '.join(COLUMNS[name][0] for name in named)} is read in one unit by "
        f"{', '.join(other)} and in another by {', '.join(reading)}, and one "
        "atmospheric data file carries one number per instant; drop "
        f"{', '.join(named)} from the atmosphere, or run the two apart.",
        error_code="TELEMAC_WEATHER_DISPUTED")


def atmospheric_data_file(value: Mapping[str, Any],
                          columns: Sequence[str] = TELEMAC2D_COLUMNS,
                          in_unit: Mapping[str, tuple[float, str]]
                          = MappingProxyType({})) -> str:
    """The value -> the table the reader scans, or the refusal that says why not.

    A comment, the header, the units, then one row per instant, single-space separated. ``in_unit``
    writes a column in the unit its one reader takes.
    """
    times, stated, note = _resolved(value, columns)
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
        mnemonic, unit = COLUMNS[name]
        factor, unit = in_unit.get(name, (1.0, unit))
        series.append((mnemonic, unit,
                       column if factor == 1.0 else [v * factor for v in column]))
    if not series:
        raise TelemacError(
            "an atmosphere with no series this host reads states nothing; omit "
            "it instead.", error_code="TELEMAC_WEATHER_EMPTY")
    # No tabs: the reader's error path names them as the cause of an unsplittable header, and its row read prefers blanks.
    rows = [f"#{note or _PLAIN}",
            " ".join([_TIME] + [mnemo for mnemo, _, _ in series]),
            " ".join(["s"] + [unit for _, unit, _ in series])]
    for index, instant in enumerate(times):
        rows.append(" ".join([f"{instant:.3f}"]
                             + [f"{column[index]:.9g}"
                                for _, _, column in series]))
    return "\n".join(rows) + "\n"


def expand_atmosphere(value: Mapping[str, Any]) -> tuple[Mapping[str, Any],
                                                         Mapping[str, Any]]:
    """The atmosphere -> the file statement, and the value the table is written from.

    The host opens the file only with a reason to read weather (coupled water quality, wind, air
    pressure); the columns depend on every reader, known once coupling and switches are filled.
    """
    if value.get("times_s") is None and value.get("observed") is None:
        stated = [name for name in COLUMNS if value.get(name) is not None]
        if stated:
            raise TelemacError(
                f"{', '.join(stated)} came with no clock to read them on; state "
                "times_s beside the series, or hand the slot a fetched record.",
                error_code="TELEMAC_WEATHER_INCOMPLETE")
        return ({}, {})
    return ({"ASCII_ATMOSPHERIC_DATA_FILE": ATMOSPHERE_FILENAME},
            {ATMOSPHERE_FILENAME: value})


def _resolved(value: Mapping[str, Any], columns: Sequence[str]
              ) -> tuple[Any, dict[str, Any], str]:
    stated = {name: value.get(name) for name in COLUMNS}
    if value.get("observed") is None:
        return value["times_s"], stated, ""
    wanted = tuple(name for name in columns if stated.get(name) is None)
    times, reported, note = _from_record(value, wanted)
    return times, {name: stated[name] if stated[name] is not None
                   else reported.get(name) for name in COLUMNS}, note


def _from_record(value: Mapping[str, Any], columns: Sequence[str]
                 ) -> tuple[list[float], dict[str, Any], str]:
    """The fetched station record -> the series on the run's clock from zero.

    The nearest station that can drive the run end to end is taken; refused stations are named
    with reasons. Only the columns this run reads are asked of an observation.
    """
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
            times, series, opened = _series(observations, value.get("duration_s"),
                                            columns, value.get("event_time"))
        except ValueError as why:
            refusals.append(f"{station} ({distance_km:.0f} km): {why}")
            continue
        stamp = opened.strftime("%Y-%m-%d %H:%M")
        absent = [COLUMNS[name][0] for name in columns if name not in series]
        return times, series, (
            f"the weather is the station {station}, {distance_km:.0f} km from "
            f"the reach: {len(times)} observations bracketing the run, which "
            f"opens at {stamp} UTC and is this table's t = 0."
            + (f" This network reports no {', '.join(absent)}, so the engine "
               "reads its own constant for it." if absent else ""))
    raise TelemacError(
        "no weather station near the reach carries a record this run can be "
        "driven by: " + "; ".join(refusals),
        error_code="TELEMAC_WEATHER_INCOMPLETE")


def _lonlat(at: Any) -> tuple[float, float]:
    if isinstance(at, (list, tuple)):
        return float(at[0]), float(at[1])
    if isinstance(at, Mapping):
        return float(at["lon"]), float(at["lat"])
    return float(at.lon), float(at.lat)


def _by_station(rows: Sequence[Mapping[str, Any]], lon: float, lat: float
                ) -> list[tuple[float, str, list[Mapping[str, Any]]]]:
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
        # Flat-earth distance suffices: the ranking only orders stations.
        east = (where[station][0] - lon) * 111.32 * math.cos(math.radians(lat))
        north = (where[station][1] - lat) * 110.57
        ranked.append((math.hypot(east, north), station, observations))
    return sorted(ranked)


def _series(observations: Sequence[Mapping[str, Any]], duration_s: Any,
            columns: Sequence[str], event_time: Any = None
            ) -> tuple[list[float], dict[str, Any], _dt.datetime]:
    """One station's observations -> the slots, on the run's clock from zero.

    t = 0 is the moment the run opens at, and the table is read between the last observation at or
    before it and the first at or after the close. With no moment, the first observation is the origin.
    """
    kept: dict[_dt.datetime, dict[str, float]] = {}
    for observation in observations:
        reported = _network(observation)
        if reported is None:
            continue
        when, network = reported
        stamp = _instant(observation.get(when))
        values = _values(observation, columns, network)
        if stamp is not None and values is not None:
            kept[stamp] = values
    stamps = sorted(kept)
    if len(stamps) < _MIN_INSTANTS:
        raise ValueError(f"{len(stamps)} complete observations in the window")
    opens = _instant(event_time) or stamps[0]
    closes = (opens + _dt.timedelta(seconds=float(duration_s))
              if duration_s is not None else stamps[-1])
    held = _bracketing(stamps, opens, closes)
    # Each slot is judged as every record is: the hole bound is the alignment's.
    try:
        series = {slot: align(Series.from_samples(
                      [(stamp, kept[stamp][slot]) for stamp in held],
                      units=COLUMNS[slot][1], at=opens),
                      max_gap_s=_MAX_GAP_S).series
                  for slot in kept[held[0]]}
    except TemporalGapError as why:
        raise ValueError(str(why)) from why
    times = list(next(iter(series.values())).times_s)
    return (times, {slot: list(found.values) for slot, found in series.items()},
            opens)


def _bracketing(stamps: Sequence[_dt.datetime], opens: _dt.datetime,
                closes: _dt.datetime) -> list[_dt.datetime]:
    """The observations the run is read between: last at or before the open, first at or after the close.

    The engine stops when an instant falls outside the table; the refusal names the missing end.
    """
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


def _instant(stamp: Any) -> _dt.datetime | None:
    if not stamp:
        return None
    try:
        read = _dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return read if read.tzinfo else read.replace(tzinfo=_dt.timezone.utc)


def _network(observation: Mapping[str, Any]
             ) -> tuple[str, Mapping[str, tuple[str, Any]]] | None:
    for when, network in _NETWORKS.items():
        if observation.get(when):
            return when, network
    return None


def _values(observation: Mapping[str, Any], columns: Sequence[str],
            network: Mapping[str, tuple[str, Any]]) -> dict[str, float] | None:
    """One observation -> the slot values it carries, or nothing where a column this run reads is absent.

    The file is one table, so an observation short of a written column is not an instant.
    """
    values: dict[str, float] = {}
    for name in columns:
        if name in _HUMIDITY or name not in network:
            continue
        column, carry = network[name]
        if observation.get(column) is None:
            return None
        try:
            read = carry(observation[column])
        except (TypeError, ValueError):
            return None
        if read is None:
            return None
        values[name] = read
    if not any(name in _HUMIDITY for name in columns):
        return values
    try:
        humidity = _humidity(_celsius(float(observation["tmpf"])),
                             observation.get("dwpf"), observation.get("relh"))
    except (KeyError, TypeError, ValueError):
        return None
    return None if humidity is None else {**values, **humidity}


def _humidity(air_c: float, dew_f: Any, relative_pct: Any
              ) -> dict[str, float] | None:
    """The two humidity slots, from whichever one the network reported.

    Each is the other over the saturation value at the air temperature, so one record drives a 2D
    vapour-pressure run and a 3D relative-humidity run.
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
    """The temperature this vapour pressure saturates at (Magnus inverted), so a network reporting relative humidity states this column too."""
    ratio = math.log(max(vapour_hpa, 1.0e-6) / _MAGNUS_A)
    return _MAGNUS_C * ratio / (_MAGNUS_B - ratio)
