"""The weather over the domain as one table: the value, and the file it becomes.

One writer for both hosts: ``METEO_TELEMAC`` (in BIEF) reads the file, but each host writes only
the columns its own source term reads. A station's record arrives as a series per quantity; what is
done here is putting the ones this run reads on one clock and under the reader's mnemonics.
"""

from __future__ import annotations

import inspect
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from trid3nt_server.inputs.series import align
from trid3nt_server.inputs.temporal import CATEGORICAL, Series, TemporalGapError
from trid3nt_server.inputs.weather import UNITS, Weather

from ..errors import TelemacError

__all__ = ["ATMOSPHERE_FILENAME", "ATMOSPHERE_READS", "COLUMNS", "DISPUTED", "READS",
           "TELEMAC2D_COLUMNS", "TELEMAC3D_COLUMNS", "Atmosphere",
           "atmospheric_data_file", "expand_atmosphere", "write_atmosphere"]

# The file a host's ASCII ATMOSPHERIC DATA FILE statement names.
ATMOSPHERE_FILENAME = "river_atmosphere.txt"

# One row per quantity a weather record states: the mnemonic the reader searches the header for, and
# the unit line's word, which is the unit the record carries it in.
# The reader matches a header entry by inclusion, so no mnemonic may contain another. Pressures are
# in pascals: the host's PRESSURE UNIT keyword scales PATM and PVAP together even for an absent
# column, so it stays on its engine default.
COLUMNS: Mapping[str, tuple[str, str]] = MappingProxyType({
    name: (mnemonic, UNITS[name]) for name, mnemonic in {
        "air_temp_c": "TAIR",
        # The dew point in Celsius, which KHIONE's thermal budget adds 273.16 to (``thermal_khione.f`` TDK); neither host reads it, it rides the file for the coupled module.
        "dew_point_c": "TDEW",
        "vapour_pressure_pa": "PVAP",
        "relative_humidity_pct": "HREL",
        "wind_speed_mps": "WINDS",
        "wind_from_deg": "WINDD",
        "cloud_octas": "CLDC",
        "solar_radiation_wm2": "RAY3",
        "pressure_pa": "PATM",
        "rain_mm": "RAINI",
    }.items()})

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

# The longest gap in one column still describing a diurnal cycle.
_MAX_GAP_S = 6.0 * 3600.0


def Atmosphere(*, times_s: Any = None, air_temp_c: Any = None,  # noqa: N802
               dew_point_c: Any = None,
               vapour_pressure_pa: Any = None, relative_humidity_pct: Any = None,
               wind_speed_mps: Any = None, wind_from_deg: Any = None,
               cloud_octas: Any = None, solar_radiation_wm2: Any = None,
               pressure_pa: Any = None, rain_mm: Any = None,
               observed: Any = None) -> Mapping[str, Any]:
    """The weather over the domain as time series on the run's own clock.

    ``observed`` is the station record the run ingested; a series stated here stands over it, and
    one left out writes no column.
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
        "observed": observed})


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
                "times_s beside the series, or hand the run a station record.",
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
    times, reported, note = _from_record(value["observed"], wanted)
    return times, {name: stated[name] if stated[name] is not None
                   else reported.get(name) for name in COLUMNS}, note


def _from_record(observed: Weather, columns: Sequence[str]
                 ) -> tuple[list[float], dict[str, Any], str]:
    """One station's record -> the columns this run reads, on one clock from the run's zero.

    A column the station's network reports and its record is short of refuses by name; one the
    network does not report is absent, and the engine reads its own constant for it.
    """
    short = [f"{COLUMNS[name][0]} ({observed.short[name]})" for name in columns
             if name in observed.short]
    if short:
        raise TelemacError(
            f"the weather station {observed.site_id} is the nearest whose record "
            "brackets this run, and it cannot drive a column the run reads: "
            + "; ".join(short) + ". State that series on the atmosphere, or "
            "hand the run another station's record.",
            error_code="TELEMAC_WEATHER_INCOMPLETE")
    series: dict[str, Series] = {}
    for name in columns:
        if name not in observed.columns:
            continue
        try:
            series[name] = align(observed.columns[name],
                                 max_gap_s=_MAX_GAP_S).series
        except TemporalGapError as why:
            raise TelemacError(
                f"{COLUMNS[name][0]} at the weather station {observed.site_id}: "
                f"{why}", error_code="TELEMAC_WEATHER_INCOMPLETE") from why
    if not series:
        raise TelemacError(
            f"the weather station {observed.site_id} reports none of the columns "
            f"this run reads ({', '.join(COLUMNS[name][0] for name in columns)}).",
            error_code="TELEMAC_WEATHER_EMPTY")
    # Each series brackets the run on its own readings, so the span every one covers does too.
    first = max(found.times_s[0] for found in series.values())
    last = min(found.times_s[-1] for found in series.values())
    times = sorted({t for found in series.values() for t in found.times_s
                    if first <= t <= last})
    stamp = observed.opens.strftime("%Y-%m-%d %H:%M")
    absent = [COLUMNS[name][0] for name in columns if name not in series]
    how_far = ("" if observed.distance_km is None
               else f", {observed.distance_km:.0f} km from the reach")
    return times, {name: _on(times, name, found)
                   for name, found in series.items()}, (
        f"the weather is the station {observed.site_id}{how_far}: {len(times)} "
        f"observations bracketing the run, which opens at {stamp} UTC and is "
        "this table's t = 0."
        + (f" This network reports no {', '.join(absent)}, so the engine "
           "reads its own constant for it." if absent else ""))


def _on(clock: Sequence[float], name: str, series: Series) -> list[float]:
    """One column read at every instant of the table's clock."""
    if name == "wind_from_deg":
        # A bearing wraps, so between two readings it has no value but the nearer one.
        return list(align(series, onto=clock, quantity=CATEGORICAL,
                          max_gap_s=_MAX_GAP_S).series.values)
    return [series.at(t) for t in clock]
