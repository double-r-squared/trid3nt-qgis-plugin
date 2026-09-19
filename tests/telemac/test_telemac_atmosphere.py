"""The weather over a domain: the file BIEF's reader scans, and the heat budget.

Offline: the atmospheric data file is TEXT the composite writes, so what is
proved here is the grammar that reader demands - the time column it refuses to
start without, the mnemonics it searches for, the units line it skips - the
columns each host's own source term reads, the carriage of a fetched station
record into those columns, and what the thermal process puts on the carrier's
result."""

from __future__ import annotations

import math

import pytest

from trid3nt_server.inputs import geometry as geometry_reader
from trid3nt_server.workflows.telemac.authoring.atmosphere import (
    ATMOSPHERE_FILENAME,
    TELEMAC2D_COLUMNS as _T2D_COLUMNS,
    Atmosphere,
    atmospheric_data_file,
)
from trid3nt_server.workflows.telemac.errors import TelemacError
from trid3nt_server.workflows.telemac.modules import T2D, T3D, WAQTEL, fill
from trid3nt_server.workflows.telemac.modules.module import SlotRefused

_HOUR = 3600.0

#: Three hours of weather, one value per instant for every series the file can
#: carry, so a test that drops one is dropping it deliberately.
_HOURS = [0.0, _HOUR, 2.0 * _HOUR]

_SEED = {"lon": -124.1, "lat": 40.48}


def _weather(**overrides):
    return Atmosphere(**{
        "times_s": _HOURS, "air_temp_c": [12.0, 15.0, 19.0],
        "vapour_pressure_pa": [900.0, 1000.0, 1100.0],
        "relative_humidity_pct": [86.0, 78.0, 66.0],
        "wind_speed_mps": [1.0, 2.0, 3.0], "wind_from_deg": [270.0, 280.0, 300.0],
        "cloud_octas": [8.0, 4.0, 0.0],
        "solar_radiation_wm2": [0.0, 300.0, 700.0],
        "pressure_pa": [101325.0, 101300.0, 101280.0],
        "rain_mm": [0.0, 0.0, 0.0], **overrides})


# -- the table the reader scans ---------------------------------------------- #

def test_the_file_opens_on_the_time_column_the_reader_refuses_to_start_without():
    lines = atmospheric_data_file(_weather()).splitlines()
    assert lines[0].startswith("#")  # the reader skips a line on its first character
    assert lines[1].split()[0] == "T"
    assert lines[2].split()[0] == "s"
    assert len(lines) == 3 + len(_HOURS)


def test_each_host_writes_the_columns_its_own_source_term_reads():
    """TELEMAC-2D's budget takes the vapour pressure and TELEMAC-3D's the
    relative humidity, and neither reads the other's."""
    for wrapper, humidity in ((T2D, "PVAP"), (T3D, "HREL")):
        header = fill(wrapper, coupling=[WAQTEL.thermal()],
                      atmosphere=_weather()
                      ).files[ATMOSPHERE_FILENAME].splitlines()[1].split()
        assert header == ["T", "TAIR", humidity, "WINDS", "CLDC", "RAY3", "PATM"]


@pytest.mark.parametrize("wrapper", [T2D, T3D])
def test_a_run_whose_own_terms_read_the_weather_writes_those_columns_alone(
        wrapper):
    """A host opens this file for its own terms - the wind that pushes the
    surface, the pressure that tilts it - and reads nothing else out of it."""
    header = fill(wrapper, WIND=True, atmosphere=_weather()
                  ).files[ATMOSPHERE_FILENAME].splitlines()[1].split()
    assert header == ["T", "WINDS", "WINDD"]


@pytest.mark.parametrize("wrapper", [T2D, T3D])
def test_weather_no_reader_of_this_run_reads_refuses_rather_than_riding_along(
        wrapper):
    with pytest.raises(TelemacError, match="read by nothing on it"):
        fill(wrapper, atmosphere=_weather())


def test_a_series_left_out_writes_no_column_and_shifts_none_of_the_others():
    text = atmospheric_data_file(_weather(cloud_octas=None, rain_mm=None))
    header = text.splitlines()[1].split()
    assert "CLDC" not in header and "RAINI" not in header
    assert header == ["T", "TAIR", "PVAP", "WINDS", "WINDD", "RAY3", "PATM"]
    assert [len(row.split()) for row in text.splitlines()[1:]] == [7] * 5


def test_the_rows_are_blank_separated_because_the_reader_names_tabs_as_a_cause():
    assert "\t" not in atmospheric_data_file(_weather())


def test_a_table_of_one_instant_refuses_because_the_engine_interpolates_two():
    one = {name: value[:1] if isinstance(value, list) else value
           for name, value in dict(_weather()).items()}
    with pytest.raises(TelemacError, match="at least two instants"):
        atmospheric_data_file(Atmosphere(**one))


def test_a_clock_that_does_not_advance_refuses():
    with pytest.raises(TelemacError, match="strictly"):
        atmospheric_data_file(_weather(times_s=[0.0, _HOUR, _HOUR]))


def test_a_series_off_its_own_clock_refuses_by_name():
    with pytest.raises(TelemacError, match="air_temp_c carries 2 values"):
        atmospheric_data_file(_weather(air_temp_c=[12.0, 15.0]))


def test_an_atmosphere_with_no_series_in_it_refuses_rather_than_writing_a_clock():
    with pytest.raises(TelemacError, match="states nothing"):
        atmospheric_data_file(Atmosphere(times_s=_HOURS))


@pytest.mark.parametrize("wrapper", [T2D, T3D])
def test_both_hosts_name_the_same_file_and_state_no_unit_of_their_own(wrapper):
    sheet = fill(wrapper, coupling=[WAQTEL.thermal()], atmosphere=_weather())
    assert dict(sheet.resolved())["ASCII ATMOSPHERIC DATA FILE"] == (
        ATMOSPHERE_FILENAME)
    assert ATMOSPHERE_FILENAME in sheet.files


@pytest.mark.parametrize("wrapper", [T2D, T3D])
def test_a_host_handed_no_weather_states_no_file(wrapper):
    assert dict(fill(wrapper, atmosphere=Atmosphere()).resolved()) == {}


@pytest.mark.parametrize("wrapper", [T2D, T3D])
def test_a_series_with_no_clock_refuses_rather_than_being_dropped(wrapper):
    with pytest.raises(TelemacError) as refusal:
        fill(wrapper, atmosphere=Atmosphere(air_temp_c=[12.0, 14.0])).resolved()
    assert "air_temp_c" in str(refusal.value)


# -- what a fetched station record becomes ----------------------------------- #

def _observation(hour: int, **overrides):
    """One RAWS row as the network reports it, in the network's own units."""
    return {"station": "SCOC1", "utc_valid": f"2026-09-01T{hour:02d}:00:00Z",
            "tmpf": 68.0, "dwpf": 50.0, "relh": 51.0, "sknt": 5.0, "drct": 270.0,
            "solar_rad": 400.0, "precip_in": 0.0, **overrides}


def _feature(properties, lon=-124.2, lat=40.5):
    return {"type": "Feature", "properties": properties,
            "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def _rows(hours=range(0, 24), **overrides):
    return [_feature(_observation(hour, **overrides)) for hour in hours]


def _observed(monkeypatch, rows, *, duration_s=20.0 * _HOUR, **overrides):
    """The fetched record answers with ``rows`` and nothing reaches the network."""
    monkeypatch.setattr(geometry_reader, "read_geometry_doc",
                        lambda layer: {"features": rows})
    return atmospheric_data_file(Atmosphere(
        observed="s3://cache/raws.fgb", at=_SEED, duration_s=duration_s,
        **overrides))


def _column(text: str, mnemonic: str) -> list[float]:
    lines = text.splitlines()
    index = lines[1].split().index(mnemonic)
    return [float(row.split()[index]) for row in lines[3:]]


def test_the_record_opens_the_run_clock_at_its_first_observation(monkeypatch):
    text = _observed(monkeypatch, _rows())
    assert _column(text, "T") == [hour * _HOUR for hour in range(24)]
    assert "SCOC1" in text.splitlines()[0] and "2026-09-01 00:00" in text.splitlines()[0]


def test_every_reported_unit_is_carried_into_the_unit_its_slot_names(monkeypatch):
    text = _observed(monkeypatch, _rows())
    assert _column(text, "TAIR")[0] == pytest.approx(20.0, abs=1e-3)
    assert _column(text, "WINDS")[0] == pytest.approx(5.0 * 0.514444, abs=1e-3)
    assert _column(text, "WINDD")[0] == 270.0
    assert _column(text, "RAY3")[0] == 400.0


def test_the_fire_network_fills_no_rain_column_because_its_own_is_a_total(
        monkeypatch):
    """Its precipitation column accumulates over the season rather than
    reporting the depth that fell in the interval, so no rain rides from it."""
    assert "RAINI" not in _observed(monkeypatch, _rows()).splitlines()[1]


def test_the_vapour_pressure_is_magnus_over_the_reported_dew_point(monkeypatch):
    dew_c = (50.0 - 32.0) / 1.8
    expected = 100.0 * 6.112 * math.exp(17.62 * dew_c / (243.12 + dew_c))
    assert _column(_observed(monkeypatch, _rows()), "PVAP")[0] == pytest.approx(
        expected, abs=0.1)


def test_one_reported_humidity_column_states_both_slots(monkeypatch):
    """The 2D budget reads the vapour pressure and the 3D one the relative
    humidity, so a record carrying either drives either host."""
    from trid3nt_server.workflows.telemac.authoring.atmosphere import (
        TELEMAC3D_COLUMNS,
    )

    monkeypatch.setattr(geometry_reader, "read_geometry_doc",
                        lambda layer: {"features": _rows()})
    text = atmospheric_data_file(
        Atmosphere(observed="s3://cache/raws.fgb", at=_SEED,
                   duration_s=20.0 * _HOUR), TELEMAC3D_COLUMNS)
    # 10 C dew point under 20 C air is about 52% relative humidity, which is the
    # same measurement the vapour pressure is.
    assert _column(text, "HREL")[0] == pytest.approx(52.0, abs=1.0)


def test_a_row_missing_one_column_is_not_an_instant_at_all(monkeypatch):
    """The file is ONE table: a gap in any column is a gap in the row."""
    rows = _rows(range(0, 6)) + [_feature(_observation(hour, solar_rad=None))
                                 for hour in range(6, 24)]
    with pytest.raises(TelemacError, match="no weather station"):
        _observed(monkeypatch, rows)


def test_a_record_shorter_than_the_run_refuses_rather_than_extrapolating(monkeypatch):
    with pytest.raises(TelemacError, match="the record runs"):
        _observed(monkeypatch, _rows(range(0, 12)))


def test_a_gap_too_long_to_describe_a_day_refuses(monkeypatch):
    with pytest.raises(TelemacError, match="hole ending at"):
        _observed(monkeypatch, _rows([0, 1, 2, 20, 21, 22, 23]),
                  duration_s=22.0 * _HOUR)


def test_the_nearest_station_that_can_drive_the_run_is_the_one_taken(monkeypatch):
    near = [_feature({**_observation(hour), "station": "NEAR"}, lon=-124.11,
                     lat=40.48) for hour in range(0, 6)]
    far = [_feature({**_observation(hour), "station": "FAR"}, lon=-124.5,
                    lat=40.8) for hour in range(0, 24)]
    assert "FAR" in _observed(monkeypatch, near + far).splitlines()[0]


def test_a_series_stated_beside_a_record_stands_over_what_the_record_reports(
        monkeypatch):
    text = _observed(monkeypatch, _rows(), cloud_octas=[2.0] * 24)
    # The network reports no cloud at all, so the stated column is the only one
    # there is - and the run reads a cloud rather than the engine's constant.
    assert _column(text, "CLDC") == [2.0] * 24


def _airport(hour: int, **overrides):
    """One ASOS row as the airport network reports it, in its own units."""
    return {"station": "PDX", "valid": f"2024-01-14T{hour:02d}:53:00Z",
            "tmpf": 21.0, "dwpf": 12.0, "sknt": 9.0, "drct": 80.0,
            "mslp": 1024.3, "skyc1": "OVC", "p01i": 0.01, **overrides}


def _ice_columns():
    """The columns a run coupling KHIONE alone under TELEMAC-2D reads."""
    from trid3nt_server.workflows.telemac.authoring.atmosphere import READS

    return tuple(name for name in _T2D_COLUMNS if name in READS["khione"])


def test_the_airport_record_carries_every_column_an_ice_run_reads(monkeypatch):
    """The dew point is measured rather than inverted out of a humidity, the sky
    cover is an observer's code, and neither is a column the fire network has."""
    rows = [_feature(_airport(hour)) for hour in range(0, 24)]
    monkeypatch.setattr(geometry_reader, "read_geometry_doc",
                        lambda layer: {"features": rows})
    text = atmospheric_data_file(
        Atmosphere(observed="s3://cache/asos.fgb", at=_SEED,
                   duration_s=20.0 * _HOUR), _ice_columns())
    assert text.splitlines()[1].split() == ["T", "TAIR", "TDEW", "WINDS",
                                            "CLDC", "RAINI"]
    assert _column(text, "TAIR")[0] == pytest.approx(-6.111, abs=1e-3)
    assert _column(text, "TDEW")[0] == pytest.approx(-11.111, abs=0.05)
    assert _column(text, "CLDC")[0] == 8.0
    assert _column(text, "RAINI")[0] == pytest.approx(0.254, abs=1e-3)


def test_an_airport_row_short_of_a_column_the_run_does_not_read_is_an_instant(
        monkeypatch):
    """A column no reader of this run takes is not asked of an observation, so
    a record with no radiation in it still drives a module computing its own."""
    rows = [_feature(_airport(hour, mslp=None)) for hour in range(0, 24)]
    monkeypatch.setattr(geometry_reader, "read_geometry_doc",
                        lambda layer: {"features": rows})
    text = atmospheric_data_file(
        Atmosphere(observed="s3://cache/asos.fgb", at=_SEED,
                   duration_s=20.0 * _HOUR), _ice_columns())
    assert len(text.splitlines()) == 3 + 24


def test_a_record_holding_no_station_refuses_by_name(monkeypatch):
    with pytest.raises(TelemacError, match="no station observation"):
        _observed(monkeypatch, [])


# -- the heat budget --------------------------------------------------------- #

def test_the_thermal_body_states_only_the_keywords_it_was_given():
    sheet = fill(T2D, coupling=[WAQTEL.thermal(
        COEFFICIENT_OF_CLOUDING_RATE=0.2,
        COEFFICIENTS_FOR_CALIBRATING_ATMOSPHERIC_RADIATION=0.85)])
    (body,) = sheet.coupled
    assert body["process"] == 11
    assert dict(body["slots"]) == {
        "COEFFICIENT_OF_CLOUDING_RATE": 0.2,
        "COEFFICIENTS_FOR_CALIBRATING_ATMOSPHERIC_RADIATION": 0.85}
    assert dict(sheet.resolved())["WATER QUALITY PROCESS"] == 11


def test_a_keyword_waqtel_does_not_have_refuses_where_the_body_states_it():
    with pytest.raises(SlotRefused, match="CLOUDING"):
        WAQTEL.thermal(COEFFICIENT_OF_CLOUDING=0.2)


def test_the_thermal_process_appends_its_temperature_to_a_carrier_with_no_tracer():
    sheet = fill(T2D, coupling=[WAQTEL.thermal()])
    (row,) = sheet.tracers
    assert (row.name, row.unit) == ("TEMPERATURE", "oC")
    assert row.style["ramp"] == "rdylbu_r"
    assert sheet.printouts()["VARIABLES FOR GRAPHIC PRINTOUTS"].endswith(",T1")


def test_a_temperature_the_carrier_declares_is_adopted_rather_than_appended():
    sheet = fill(T2D, NUMBER_OF_TRACERS=1,
                 NAMES_OF_TRACERS=["TEMPERATURE     DEG"],
                 coupling=[WAQTEL.thermal()])
    (row,) = sheet.tracers
    # The name and the unit are the carrier's, because they are what the result
    # file carries; the STYLE is the process's, because the carrier's is the
    # generic tracer row and says nothing about a temperature.
    assert (row.name, row.unit) == ("TEMPERATURE", "DEG")
    assert row.style["ramp"] == "rdylbu_r"


def test_the_thermal_rows_are_declared_data_on_the_wrapper():
    rows = dict(WAQTEL.APPENDABLE)["process 11"]
    assert [row.name for row in rows] == ["TEMPERATURE"]
    assert all(row.style for row in rows)
