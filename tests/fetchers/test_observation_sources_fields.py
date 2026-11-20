"""CO-OPS water level, Great Lakes water level, NWS forecast, NWM, GTSM, WQP, STN high-water marks and the Des Moines head
targets keep every field their source returns, state the members a slot reads them by, and cite an accuracy only where
the source publishes one; model output and predictions state none.

CO-OPS and Great Lakes read recorded mdapi and datagetter answers (tests/fixtures/fetcher_fields/README.md); the
NWS, NWM and GTSM layers are built the way their hooks build them, and the STN marks are the hand-made records of
tests/fetchers/test_router_envelope.py (the service answers 403 from this host)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pandas as pd
import pyogrio
import pytest

from trid3nt_contracts.coverage import Coverage, Member
from trid3nt_server.inputs.fields import FieldError, Measured, records
from trid3nt_server.inputs.observation import ObservationError, observation
from trid3nt_server.tools.fetchers._router import registration
from trid3nt_server.tools.fetchers._router.executors import station_timeseries as st

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fetcher_fields"


def _row(source, data_class, kind=None):
    return next(r for r in registration.get_spec(source).coverage
                if r.data_class == data_class and (kind is None or r.kind == kind))


def _stations(monkeypatch, source, catalog_id, answer):
    catalog = {"stations": [s for s in json.loads((_FIXTURES / "coops_stations_8518750_9063063.json").read_text())["stations"]
                            if s["id"] == catalog_id]}
    monkeypatch.setattr(st, "_get_json", lambda spec, url, params: catalog if "stations.json" in url
                        else json.loads((_FIXTURES / answer).read_text()))
    spec = registration.get_spec(source)
    params = {"bbox": [-180.0, -90.0, 180.0, 90.0], "start_date": "2024-10-01", "end_date": "2024-10-01"}
    records_ = st.fetch_station_records(spec, params)
    data = st.stations_to_point_fgb(records_, spec, product="water_level")
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as handle:
        handle.write(data)
    try:
        frame = pyogrio.read_dataframe(handle.name)
    finally:
        os.unlink(handle.name)
    props = json.loads(frame.drop(columns="geometry").to_json(orient="records"))[0]
    point = frame.geometry.iloc[0]
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [point.x, point.y]}, "properties": props}]}, frame


def test_a_coops_station_keeps_its_catalog_record_and_every_reading_object(monkeypatch):
    layer, frame = _stations(monkeypatch, "fetch_noaa_coops_tides", "8518750", "coops_wl_8518750_20241001.json")
    props = layer["features"][0]["properties"]
    assert props["id"] == "8518750" and props["name"] == "The Battery" and props["state"] == "NY"
    assert {"timezone", "shefcode", "details.self", "datums.self", "lng", "lat"} <= set(props)
    readings = json.loads(props["series"])
    assert readings[0] == {"t": "2024-10-01 00:00", "v": "1.812", "s": "0.031", "f": "0,0,0,0", "q": "v"}
    assert props["n_timesteps"] == len(readings) and props["product"] == "water_level"
    assert not {"station_id", "station_name", "time_series_csv"} & set(props)


def test_a_coops_reading_is_read_through_the_row_with_the_sensor_accuracy_and_no_accuracy_on_predictions(monkeypatch):
    layer, _ = _stations(monkeypatch, "fetch_noaa_coops_tides", "8518750", "coops_wl_8518750_20241001.json")
    row = _row("fetch_noaa_coops_tides", "water level series", "measured")
    found = observation(layer, fields=row.fields, producer="fetch_noaa_coops_tides", at="2024-10-01T00:30:00Z",
                        to_units="m", accuracy=True)
    assert found.value == pytest.approx(1.763) and found.sampled == "2024-10-01 00:30"
    assert found.site_id == "8518750" and found.site_name == "The Battery" and found.datum == "MLLW"
    assert found.accuracy == 0.02
    predicted = _row("fetch_noaa_coops_tides", "water level series", "predicted")
    with pytest.raises(ObservationError) as caught:
        observation(layer, fields=predicted.fields, producer="fetch_noaa_coops_tides", accuracy=True)
    assert caught.value.error_code == "FIELD_UNMAPPED"
    temperature = _row("fetch_noaa_coops_tides", "water quality sample")
    assert "accuracy" not in temperature.fields


def test_a_great_lakes_gauge_reads_on_the_lake_datum_with_the_primary_sensor_bound(monkeypatch):
    layer, _ = _stations(monkeypatch, "fetch_greatlakes_water_level", "9063063", "coops_greatlakes_9063063_20241001.json")
    row = _row("fetch_greatlakes_water_level", "water level series")
    found = observation(layer, fields=row.fields, producer="fetch_greatlakes_water_level",
                        at="2024-10-01T00:06:00Z", to_units="m", accuracy=True)
    assert found.site_name == "Cleveland" and found.value == pytest.approx(0.93) and found.accuracy == 0.02


def test_the_accuracy_the_sources_state_is_cited_and_the_model_output_states_none():
    for source in ("fetch_noaa_coops_tides", "fetch_greatlakes_water_level"):
        member = (_row(source, "water level series", "measured").fields["accuracy"])
        assert member.constant == 0.02 and member.unit == "m" and "CO-OPS" in member.cite
    for source in ("fetch_noaa_nwm_streamflow", "fetch_nws_river_forecast", "fetch_gtsm_tide_surge",
                   "fetch_usgs_model_head_targets"):
        spec = registration.get_spec(source)
        assert all("accuracy" not in row.fields for row in spec.coverage) and "accuracy" not in spec.fields
    assert all(r.kind != "measured" for r in registration.get_spec("fetch_nws_river_forecast").coverage)


def test_a_forecast_gauge_reads_its_stage_series_by_nwps_keys_and_refuses_an_accuracy():
    from trid3nt_server.tools.fetchers.hydrology.fetch_nws_river_forecast import hooks as nws

    gauge = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-91.0, 30.5]},
             "properties": {"lid": "CIDI4", "name": "Test gauge", "status": {
                 "observed": {"primary": -999, "secondary": -999, "validTime": "2026-11-09T19:00:00Z"},
                 "forecast": {"primary": 3.0, "secondary": 1.5, "validTime": "2026-11-09T20:00:00Z"}}}}
    stageflow = json.dumps({"observed": {"data": []}, "forecast": {"data": [
        {"validTime": "2026-11-09T20:00:00Z", "primary": 3.0, "secondary": 1.5},
        {"validTime": "2026-11-09T21:00:00Z", "primary": 3.5, "secondary": -999}]}}).encode()
    detail = json.dumps({"datums": {"vertical": {"value": [{"abbrev": "NAVD88", "value": 10.0}]}}}).encode()

    class _Got:
        def __init__(self, body):
            self.body = body

    merged = nws.enrich_merge(registration.get_spec("fetch_nws_river_forecast"), {}, [gauge],
                              {"zero:CIDI4": _Got(detail), "ser:CIDI4": _Got(stageflow)})[0]
    assert merged["produced"]["obs_stage_ft"] is None and merged["produced"]["fcst_stage_ft"] == 3.0
    layer = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": merged["geometry"], "properties": {**merged["properties"], **merged["produced"]}}]}
    row = _row("fetch_nws_river_forecast", "water level series")
    found = observation(layer, fields=row.fields, producer="fetch_nws_river_forecast", at="2026-11-09T21:00:00Z",
                        to_units="ft")
    assert found.value == pytest.approx(13.5) and found.site_id == "CIDI4" and found.datum == "NAVD88"
    with pytest.raises(ObservationError):
        observation(layer, fields=row.fields, producer="fetch_nws_river_forecast", accuracy=True)


def _hwm_layer(*qualities):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-84.5, 30.5]},
         "properties": {"hwm_id": i, "elev_ft": 12.3, "hwmQualityName": q, "verticalDatumName": "NAVD88",
                        "quantity": "water_surface_elevation"}} for i, q in enumerate(qualities, 1)]}


def test_a_high_water_mark_reads_the_accuracy_its_quality_rating_publishes_and_no_other():
    spec = registration.get_spec("fetch_high_water_marks")
    marks = records(_hwm_layer("Excellent", "Good", "Fair", "Poor", "Unknown/Historical", "Very poor"),
                    spec.fields, source="fetch_high_water_marks", site_fields=("hwm_id",))
    got = []
    for mark in marks:
        try:
            got.append(mark.accuracy.value)
        except FieldError as exc:
            got.append(exc.error_code)
    assert got == [0.05, 0.1, 0.2, 0.4, "FIELD_UNTABLED", "FIELD_UNTABLED"]
    assert marks[0].value == Measured(12.3, "ft") and marks[0].read("quantity") == "water_surface_elevation"


def test_a_wqp_site_keeps_the_station_record_and_the_latest_results_whole_row(monkeypatch):
    from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_water_quality import hooks

    header = next(__import__("csv").reader((_FIXTURES / "wqp_result_header.csv").open()))
    result = {name: None for name in header}
    result.update({"MonitoringLocationIdentifier": "USGS-413800070330001", "ResultMeasureValue": "4.2",
                   "ResultMeasure/MeasureUnitCode": "mg/l", "ActivityStartDate": "2024-01-10",
                   "CharacteristicName": "Boron", "DataQuality/PrecisionValue": "0.1"})
    sites = pd.DataFrame([{"MonitoringLocationIdentifier": "USGS-413800070330001", "MonitoringLocationName": "W 1",
                           "LongitudeMeasure": -70.55, "LatitudeMeasure": 41.63,
                           "WellDepthMeasure/MeasureValue": "75.0", "WellDepthMeasure/MeasureUnitCode": "ft"}])
    answers = iter([(sites, None), (pd.DataFrame([result]), None)])
    monkeypatch.setattr(hooks, "retrieve", lambda *a, **k: next(answers))
    spec = registration.get_spec("fetch_usgs_water_quality")
    feature = hooks.features(spec, {"bbox": [-70.6, 41.6, -70.5, 41.7], "characteristic": "Boron"}, timeout_s=5.0)[0]
    assert set(f"result.{name}" for name in header) <= set(feature["produced"])
    layer = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": feature["geometry"], "properties": {**feature["properties"], **feature["produced"]}}]}
    row = _row("fetch_usgs_water_quality", "water quality sample")
    found = observation(layer, fields=row.fields, producer="fetch_usgs_water_quality", at="2024-01-20T00:00:00Z")
    assert (found.value, found.units, found.site_id, found.site_name, found.depth, found.depth_units) == (
        4.2, "mg/l", "USGS-413800070330001", "W 1", 75.0, "ft")
    assert "accuracy" not in row.fields


def test_a_head_target_reads_its_value_quantity_and_frame_through_the_source_map_and_states_no_accuracy():
    from trid3nt_server.tools.fetchers._router import router
    from trid3nt_server.tools.fetchers._router.hooks import resolve_hook

    spec = registration.get_spec("fetch_usgs_model_head_targets")
    zipped = (Path(__file__).resolve().parents[1] / "fixtures" / "usgs_model_head_targets" / "dm_targets_excerpt.zip").read_bytes()
    target = resolve_hook("usgs_model_head_targets.parse_response")(
        spec, router.validate_params(spec, {"bbox": [-93.854, 41.5869, -93.5753, 41.8193]}), [zipped])[0]
    layer = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": target["geometry"], "properties": {**target["properties"], **target["produced"]}}]}
    found = observation(layer, fields=spec.fields, producer="fetch_usgs_model_head_targets", at=None, to_units="m")
    assert found.quantity == "groundwater_head" and found.datum == "NAVD88" and found.units == "m"
    assert found.site_id == target["produced"]["site_id"]
    assert "accuracy" not in spec.fields


def test_a_source_that_maps_no_site_names_no_site_rather_than_guessing_a_field():
    layer = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
         "properties": {"site_id": "A", "station_id": "B", "lid": "C", "site_no": "D", "value": 1.0, "unit": "m"}}]}
    only_value = {"value": Member(field="value", unit={"field": "unit"})}
    assert observation(layer, fields=only_value).site_id is None
    assert observation(layer, fields={**only_value, "site": Member(field="lid")}).site_id == "C"


def test_reading_keys_are_both_named_and_belong_to_a_window_member():
    base = {"data_class": "water level series", "kind": "measured", "extent": {"kind": "service"},
            "window": {"series": True}}
    ok = Coverage.model_validate({**base, "fields": {"series": {"field": "s", "time_key": "t", "value_key": "v", "unit": "m"}}})
    assert ok.fields["series"].time_key == "t"
    with pytest.raises(ValueError):
        Coverage.model_validate({**base, "fields": {"series": {"field": "s", "time_key": "t", "unit": "m"}}})
    with pytest.raises(ValueError):
        Coverage.model_validate({**base, "fields": {"value": {"field": "s", "time_key": "t", "value_key": "v", "unit": "m"}}})


def test_a_fetched_layer_holding_date_typed_columns_reads_back_with_their_instants_as_iso_text(monkeypatch, tmp_path):
    """B1's WQP layer carries the portal's ActivityStartDateTime as a date-typed column; every slot's reader must not
    fail on it nor drop it."""
    from trid3nt_server.inputs.geometry import read_geometry_doc
    from trid3nt_server.tools.fetchers._router.executors.vector_fgb import features_to_fgb_bytes
    from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_water_quality import hooks

    header = next(__import__("csv").reader((_FIXTURES / "wqp_result_header.csv").open()))
    result = {name: None for name in header}
    result.update({"MonitoringLocationIdentifier": "USGS-413800070330001", "ResultMeasureValue": "4.2",
                   "ActivityStartDate": "2024-01-10", "ActivityStartDateTime": pd.Timestamp("2024-01-10T15:30:00Z"),
                   "ActivityEndDateTime": pd.Timestamp("2024-01-10T15:45:00Z")})
    sites = pd.DataFrame([{"MonitoringLocationIdentifier": "USGS-413800070330001", "LongitudeMeasure": -70.55,
                           "LatitudeMeasure": 41.63}])
    answers = iter([(sites, None), (pd.DataFrame([result]), None)])
    monkeypatch.setattr(hooks, "retrieve", lambda *a, **k: next(answers))
    spec = registration.get_spec("fetch_usgs_water_quality")
    feats = hooks.features(spec, {"bbox": [-70.6, 41.6, -70.5, 41.7], "characteristic": "Boron"}, timeout_s=5.0)
    path = tmp_path / "wqp.fgb"
    path.write_bytes(features_to_fgb_bytes(feats, spec))
    props = read_geometry_doc(str(path))["features"][0]["properties"]
    assert props["result.ActivityStartDateTime"].startswith("2024-01-10T15:30:00")
    assert props["result.ActivityEndDateTime"].startswith("2024-01-10T15:45:00")
