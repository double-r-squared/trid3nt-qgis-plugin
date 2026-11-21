"""ASOS, RAWS, OpenAQ, AirNow, SNOTEL, the climate normals and Storm Events keep every field their source returns,
state the members a slot reads them by, and state no accuracy (none of them publishes one per reading, and the normals
are a climatology).

ASOS, RAWS, SNOTEL, the normals and Storm Events read recorded answers (tests/fixtures/fetcher_fields/README.md);
OpenAQ and AirNow are keyed with no key on this host, so their answers are the documented shapes stated by hand."""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pyogrio
import pytest

from trid3nt_server.errors import untabled_readings_note
from trid3nt_server.inputs import run_notes
from trid3nt_server.inputs.fields import FieldError, Measured, records
from trid3nt_server.inputs.observation import ObservationError
from trid3nt_server.inputs.weather import weather
from trid3nt_server.tools.fetchers._router import registration
from trid3nt_server.tools.fetchers._router.executors.vector_fgb import features_to_fgb_bytes
from trid3nt_server.tools.fetchers.climate.fetch_climate_normals import hooks as normals
from trid3nt_server.tools.fetchers.soil.fetch_snotel_snow import hooks as snotel
from trid3nt_server.tools.fetchers.weather.fetch_airnow_air_quality import hooks as airnow
from trid3nt_server.tools.fetchers.weather.fetch_asos_metar import hooks as asos
from trid3nt_server.tools.fetchers.weather.fetch_openaq_measurements import hooks as openaq
from trid3nt_server.tools.fetchers.weather.fetch_raws_weather import hooks as raws
from trid3nt_server.tools.fetchers.weather.fetch_storm_events_db import hooks as storms

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fetcher_fields"
_SOURCES = ("fetch_asos_metar", "fetch_raws_weather", "fetch_openaq_measurements", "fetch_airnow_air_quality",
            "fetch_snotel_snow", "fetch_climate_normals", "fetch_storm_events_db")


def _spec(name):
    return registration.get_spec(name)


def _layer(spec, features):
    """The features as the router writes them and a reader gets them back: every column of the FlatGeobuf kept."""
    data = features_to_fgb_bytes(features, spec)
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as handle:
        handle.write(data)
    try:
        frame = pyogrio.read_dataframe(handle.name)
    finally:
        os.unlink(handle.name)
    rows = json.loads(frame.drop(columns="geometry").to_json(orient="records"))
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [point.x, point.y]}, "properties": row}
        for row, point in zip(rows, frame.geometry)]}, frame


def _asos(text=None):
    spec = _spec("fetch_asos_metar")
    body = text if text is not None else (_FIXTURES / "asos_kpdx_kvuo_20241001.csv").read_text()
    return spec, asos.parse_response(spec, {}, [body.encode()])


def _raws_features(spec=None):
    spec = spec or _spec("fetch_raws_weather")
    station = json.loads((_FIXTURES / "raws_network_anec1.json").read_text())
    stations = [{"sid": station["id"], "lon": station["geometry"]["coordinates"][0],
                 "lat": station["geometry"]["coordinates"][1], "record": station["properties"]}]
    params = {"_stations": stations, "start_date": "2024-10-01", "end_date": "2024-10-01"}
    feats = raws.parse_response(spec, params, [])
    body = (_FIXTURES / "raws_obhistory_anec1_20241001.json").read_bytes()
    return spec, raws.enrich_merge(spec, params, feats, {"ANEC1:2024-10-01": SimpleNamespace(body=body)})


@pytest.mark.parametrize("name", _SOURCES)
def test_no_weather_source_states_an_accuracy(name):
    """Neither the services nor the instruments they relay state one per reading, and the normals are a climatology:
    none is invented, so a read of the member refuses by name."""
    assert "accuracy" not in _spec(name).fields


def test_asos_asks_the_service_for_every_variable_it_holds():
    plans = asos.build_request(_spec("fetch_asos_metar"), {
        "_station_ids": ["PDX"], "start_time": "2024-10-01T00:00:00Z", "end_time": "2024-10-01T03:00:00Z"})
    assert plans[0].params["data"] == ["all"]


def test_asos_keeps_every_column_the_service_answers():
    header = next(csv.reader(io.StringIO((_FIXTURES / "asos_kpdx_kvuo_20241001.csv").read_text())))
    _, feats = _asos()
    assert len(feats) == 6 and len(header) == 33
    assert set(feats[0]["properties"]) == set(header)
    props = feats[0]["properties"]
    assert props["station"] == "PDX" and props["tmpf"] == 75.0 and props["relh"] == 20.63
    assert props["metar"].startswith("KPDX 010053Z") and props["skyc1"] == "SCT" and props["feel"] == 75.0
    assert props["snowdepth"] is None and props["wxcodes"] is None


def test_asos_reads_a_trace_of_rain_as_no_depth_and_every_other_reading_as_a_number():
    text = (_FIXTURES / "asos_kpdx_kvuo_20241001.csv").read_text().splitlines()
    cols = text[0].split(",")
    first = text[1].split(",")
    first[cols.index("p01i")] = "T"
    _, feats = _asos("\n".join([text[0], ",".join(first), *text[2:]]))
    assert feats[0]["properties"]["p01i"] is None and feats[1]["properties"]["p01i"] == 0.0


def test_asos_weather_is_read_through_the_members_its_source_states():
    spec, feats = _asos()
    layer, _ = _layer(spec, feats)
    found = weather(layer, fields=spec.fields, near={"lon": -122.6, "lat": 45.59})
    assert found.site_id == "PDX"
    assert set(found.columns) == {"air_temp_c", "dew_point_c", "vapour_pressure_pa", "relative_humidity_pct",
                                  "wind_speed_mps", "wind_from_deg", "cloud_octas", "pressure_pa", "rain_mm"}
    assert found.short == {} and "solar_radiation_wm2" not in found.columns
    assert found.columns["air_temp_c"].values[0] == pytest.approx((75.0 - 32.0) / 1.8, abs=1e-3)
    assert found.columns["wind_speed_mps"].values[0] == pytest.approx(10.0 * 0.514444, abs=1e-3)
    assert found.columns["pressure_pa"].values[0] == pytest.approx(102400.0)
    assert found.columns["cloud_octas"].values[0] == 3.0 and found.columns["rain_mm"].values[0] == 0.0


def test_a_sky_cover_hour_the_table_does_not_hold_gives_no_cloud_that_hour_and_the_record_keeps_the_rest():
    spec, feats = _asos()
    feats[2]["properties"]["skyc1"] = "VV"
    layer, _ = _layer(spec, feats)
    token = run_notes.bind_notes()
    try:
        found = weather(layer, fields=spec.fields, near={"lon": -122.6, "lat": 45.59})
    finally:
        notes = run_notes.drain_notes(token)
    assert found.site_id == "PDX" and "air_temp_c" in found.columns and "rain_mm" in found.columns
    assert len(found.columns["cloud_octas"].values) == len(found.columns["air_temp_c"].values) - 1
    assert untabled_readings_note({"cloud_cover": 1}) in notes and "VV" not in notes[-1]


def test_a_station_whose_every_sky_cover_is_untabled_keeps_its_weather_and_is_short_of_cloud():
    spec, feats = _asos()
    for feat in feats:
        feat["properties"]["skyc1"] = "ZZZ"
    layer, _ = _layer(spec, feats)
    found = weather(layer, fields=spec.fields, near={"lon": -122.6, "lat": 45.59})
    assert "cloud_octas" not in found.columns and "cloud_octas" in found.short and "air_temp_c" in found.columns


def test_the_sky_cover_table_cites_where_its_codes_come_from():
    cover = _spec("fetch_asos_metar").fields["cloud_cover"]
    assert cover.unit == "octa" and "ASOS User's Guide" in cover.cite and "Table 3" in cover.cite


def test_raws_keeps_the_station_record_whole_and_every_observation_field_beside_it():
    spec, rows = _raws_features()
    assert len(rows) == 25
    assert rows[0]["properties"]["sid"] == "ANEC1" and rows[0]["properties"]["network"] == "CA_DCP"
    assert rows[0]["properties"]["attributes"] == {"HAS_PHOUR": "1"}
    observed = rows[10]["produced"]
    assert set(observed) == {"index", "utc_valid", "MMIRGZZ", "MTIRGZZ", "PCIRGZZ", "PPHRGZZ", "TAIRGNZ", "TAIRGXZ",
                             "TAIRGZZ", "UDIRGZZ", "UPHRGZZ", "URHRGZZ", "USIRGZZ", "VBIRGZZ", "XRIRGZZ", "tmpf",
                             "dwpf", "sknt", "drct", "local_valid"}
    assert observed["XRIRGZZ"] == 14.0 and observed["URHRGZZ"] == 287.0 and observed["VBIRGZZ"] == 13.1
    layer, frame = _layer(spec, rows)
    assert "attributes.HAS_PHOUR" in frame.columns and "elevation" in frame.columns


def test_raws_relative_humidity_is_the_SHEF_XR_element_and_no_reading_is_lost_to_a_peak_wind_direction():
    """XR is humidity, UR the direction of the peak wind and VB the battery's volts (SHEF Code Manual, physical
    elements): the station's 287 degrees at 09:52 local is no sensor fault."""
    _, rows = _raws_features()
    assert len(rows) == 25
    assert any(row["produced"]["URHRGZZ"] and row["produced"]["URHRGZZ"] > 100.0 for row in rows)


def test_raws_weather_reads_humidity_from_its_xr_element_and_reports_no_solar_radiation():
    spec, rows = _raws_features()
    layer, _ = _layer(spec, rows)
    found = weather(layer, fields=spec.fields, near={"lon": -116.67, "lat": 32.84})
    assert found.site_id == "ANEC1"
    assert set(found.columns) == {"air_temp_c", "dew_point_c", "vapour_pressure_pa", "relative_humidity_pct",
                                  "wind_speed_mps", "wind_from_deg"}
    assert found.short == {} and "solar_radiation_wm2" not in found.columns
    # 77 F at 24 percent, the first instant the station reports
    assert found.columns["air_temp_c"].values[0] == pytest.approx(25.0, abs=1e-3)
    assert found.columns["relative_humidity_pct"].values[0] == pytest.approx(24.0, abs=0.1)
    assert found.columns["wind_speed_mps"].values[0] == pytest.approx(0.86897624 * 0.514444, abs=1e-3)
    assert not {"solar_radiation", "pressure", "rain", "cloud_cover"} & set(spec.fields)


def test_a_member_in_a_unit_weather_does_not_carry_refuses_naming_it():
    spec, feats = _asos()
    layer, _ = _layer(spec, feats)
    fields = dict(spec.fields)
    fields["wind_speed"] = fields["wind_speed"].model_copy(update={"unit": "furlongs"})
    with pytest.raises(ObservationError, match="furlongs"):
        weather(layer, fields=fields, near={"lon": -122.6, "lat": 45.59})


def test_a_source_mapping_no_site_cannot_be_read_as_a_station():
    spec, feats = _asos()
    layer, _ = _layer(spec, feats)
    fields = {k: v for k, v in spec.fields.items() if k != "site"}
    with pytest.raises(ObservationError, match="maps no 'site'"):
        weather(layer, fields=fields, near={"lon": -122.6, "lat": 45.59})


_OPENAQ_LOCATION = {
    "id": 2178, "name": "Delhi - Anand Vihar", "locality": "Delhi", "timezone": "Asia/Kolkata",
    "country": {"id": 9, "code": "IN", "name": "India"}, "owner": {"id": 4, "name": "CPCB"},
    "provider": {"id": 119, "name": "CPCB"}, "isMobile": False, "isMonitor": True,
    "instruments": [{"id": 2, "name": "Government Monitor"}],
    "sensors": [{"id": 7440, "name": "pm25 ug/m3", "parameter": {
        "id": 2, "name": "pm25", "units": "ug/m3", "displayName": "PM2.5"}}],
    "coordinates": {"latitude": 28.6468, "longitude": 77.3153},
    "datetimeFirst": {"utc": "2016-11-24T07:00:00Z", "local": "2016-11-24T12:30:00+05:30"},
    "datetimeLast": {"utc": "2026-11-20T06:00:00Z", "local": "2026-11-20T11:30:00+05:30"}}
_OPENAQ_LATEST = {"datetime": {"utc": "2026-11-20T06:00:00Z", "local": "2026-11-20T11:30:00+05:30"},
                  "value": 188.5, "coordinates": {"latitude": 28.6468, "longitude": 77.3153},
                  "sensorsId": 7440, "locationsId": 2178}


def test_openaq_keeps_the_location_the_latest_reading_and_its_sensor_whole_and_reads_members_off_them():
    """The documented OpenAQ v3 shapes, stated by hand: no key is stored on this host, so no answer is recorded."""
    spec = _spec("fetch_openaq_measurements")
    feat = {"type": "Feature", "geometry": None, "properties": {"_station": _OPENAQ_LOCATION}}
    body = json.dumps({"results": [_OPENAQ_LATEST]}).encode()
    rows = openaq.enrich_merge(spec, {"bbox": [77.0, 28.5, 77.6, 28.8]}, [feat], {"loc:2178": SimpleNamespace(body=body)})
    layer, frame = _layer(spec, rows)
    assert {"id", "name", "locality", "country.code", "owner.name", "isMobile", "coordinates.latitude",
            "datetimeFirst.utc", "instruments", "sensors", "latest.value", "latest.datetime.utc", "latest.sensorsId",
            "sensor.parameter.name", "sensor.parameter.units", "sensor.parameter.displayName"} <= set(frame.columns)
    (rec,) = records(layer, spec.fields, source="fetch_openaq_measurements")
    assert rec.value == Measured(188.5, "ug/m3") and rec.quantity == "pm25"
    assert rec.site_name == "Delhi - Anand Vihar" and rec.time == "2026-11-20T06:00:00Z"


_AIRNOW = {"Latitude": 34.0667, "Longitude": -118.2275, "UTC": "2026-11-20T18:00", "Parameter": "PM25",
           "Unit": "UG/M3", "Value": 14.2, "RawConcentration": 14.2, "AQI": 58, "Category": 2,
           "SiteName": "Los Angeles - N. Main Street", "AgencyName": "South Coast AQMD",
           "FullAQSCode": "060371103", "IntlAQSCode": "840060371103"}


def test_airnow_keeps_every_field_it_returns_and_states_the_names_of_its_codes_beside_them():
    """The documented AirNow aq/data verbose shape, stated by hand: no key is stored on this host."""
    spec = _spec("fetch_airnow_air_quality")
    older = {**_AIRNOW, "UTC": "2026-11-20T17:00", "Value": 9.0}
    rows = airnow.parse_response(spec, {}, [json.dumps([older, _AIRNOW]).encode()])
    assert len(rows) == 1 and rows[0]["properties"] == _AIRNOW
    assert rows[0]["produced"] == {"ParameterName": "PM2.5 (fine particulate matter)", "AQICategoryName": "Moderate"}
    layer, frame = _layer(spec, rows)
    assert set(_AIRNOW) | {"ParameterName", "AQICategoryName"} <= set(frame.columns)
    (rec,) = records(layer, spec.fields, source="fetch_airnow_air_quality")
    assert rec.value == Measured(14.2, "UG/M3") and rec.read("site") == "060371103" and rec.quantity == "PM25"


def test_snotel_keeps_the_catalog_record_and_each_element_s_latest_reading_and_record():
    spec = _spec("fetch_snotel_snow")
    catalog = (_FIXTURES / "snotel_stations_or_20240301.json").read_bytes()
    params = {"bbox": [-117.5, 45.0, -117.0, 45.5]}
    feats = snotel.parse_response(spec, params, [catalog])
    assert [f["properties"]["stationTriplet"] for f in feats] == ["302:OR:SNTL"]
    data = (_FIXTURES / "snotel_data_20240301_20240304.json").read_bytes()
    rows = snotel.enrich_merge(spec, params, feats, {"batch": SimpleNamespace(body=data)})
    layer, frame = _layer(spec, rows)
    props = layer["features"][0]["properties"]
    assert props["name"] == "Aneroid Lake #2" and props["elevation"] == 7430.0 and props["shefId"] == "ANRO3"
    assert json.loads(props["associatedHucs"])[0] == "170601020101"
    assert props["WTEQ.value"] == 13.3 and props["WTEQ.date"] == "2024-03-04"
    assert props["SNWD.value"] == 51.0 and props["SNWD.stationElement.storedUnitCode"] == "in"
    assert props["WTEQ.stationElement.dataPrecision"] == 1
    (rec,) = records(layer, spec.fields, source="fetch_snotel_snow")
    assert rec.read("site") == "302:OR:SNTL" and rec.site_name == "Aneroid Lake #2"


def test_snotel_with_its_data_service_down_still_states_every_element_column_null():
    spec = _spec("fetch_snotel_snow")
    feats = snotel.parse_response(spec, {"bbox": [-117.5, 45.0, -117.0, 45.5]},
                                  [(_FIXTURES / "snotel_stations_or_20240301.json").read_bytes()])
    rows = snotel.enrich_merge(spec, {}, feats, {})
    _, frame = _layer(spec, rows)
    assert {"WTEQ.value", "WTEQ.date", "SNWD.value", "SNWD.date"} <= set(frame.columns)
    assert frame["WTEQ.value"].isna().all()


def _normals_rows():
    spec = _spec("fetch_climate_normals")
    inventory = (_FIXTURES / "normals_inventory_excerpt.txt").read_bytes()
    params = {"bbox": [-122.7, 45.5, -122.5, 45.7]}
    feats = normals.parse_response(spec, params, [inventory])
    body = (_FIXTURES / "normals_annual_USW00024229.csv").read_bytes()
    return spec, normals.enrich_merge(spec, params, feats, {"USW00024229": SimpleNamespace(body=body)})


def test_the_normals_keep_the_whole_annual_row_under_ncei_s_own_names():
    spec, rows = _normals_rows()
    header = next(csv.reader(io.StringIO((_FIXTURES / "normals_annual_USW00024229.csv").read_text())))
    props = rows[0]["properties"]
    assert set(props) == set(header) and len(props) > 600
    assert props["STATION"] == "USW00024229" and props["NAME"] == "PORTLAND INTL AP, OR US"
    assert props["ANN-TAVG-NORMAL"] == 55.1 and props["years_ANN-TAVG-NORMAL"] == 27.0
    assert props["ANN-TMIN-PRBFST-T32FP50"] == "11/19" and props["meas_flag_ANN-TAVG-NORMAL"] is None
    assert not any(value == -9999.0 for value in props.values())
    layer, frame = _layer(spec, rows)
    assert len(frame.columns) > 600
    (rec,) = records(layer, spec.fields, source="fetch_climate_normals")
    assert rec.read("site") == "USW00024229"


def test_a_gap_ncei_writes_as_minus_9999_is_null_rather_than_a_temperature():
    assert normals._cell("-9999") is None and normals._cell("   -9999.0") is None
    assert normals._cell(" 55.1") == 55.1 and normals._cell("12/08") == "12/08" and normals._cell("  ") is None


def _storm_rows(**params):
    spec = _spec("fetch_storm_events_db")
    blob = (_FIXTURES / "storm_events_oregon_2023.csv.gz").read_bytes()
    return spec, storms.parse_response(spec, {"year": 2023, **params}, [blob])


def test_storm_events_keep_every_column_of_the_details_file_as_ncei_publishes_it():
    spec, rows = _storm_rows()
    header = next(csv.reader(io.StringIO(gzip.decompress(
        (_FIXTURES / "storm_events_oregon_2023.csv.gz").read_bytes()).decode())))
    assert len(rows) == 25 and len(header) == 51
    props = rows[0]["properties"]
    assert set(props) == set(header)
    assert props["DAMAGE_PROPERTY"].endswith("K") and props["BEGIN_DATE_TIME"].count("-") == 2
    assert isinstance(props["BEGIN_LAT"], float) and props["EVENT_NARRATIVE"]
    layer, frame = _layer(spec, rows)
    assert len(frame.columns) >= 51
    assert {rec.read("site") for rec in records(layer, spec.fields, source="fetch_storm_events_db")} == {
        row["properties"]["EVENT_ID"] for row in rows}
