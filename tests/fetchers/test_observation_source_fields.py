"""NWIS gauges and USGS groundwater wells keep every field their service returned and state the members a
slot reads them by: the site record with the gauge zero's accuracy, the IV qualifier, the whole field
measurement with its observing procedure, the well's constructed depth.

Read on recorded USGS answers (tests/fixtures/fetcher_fields/README.md)."""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyogrio
import pytest
from dataretrieval.nwis import _read_rdb

from trid3nt.inputs.fields import FieldError, Measured, records
from trid3nt.inputs.observation import observation
from trid3nt.tools.fetchers._router import registration
from trid3nt.tools.fetchers._router.executors.vector_fgb import (
    apply_ingest_transforms, features_to_fgb_bytes)
from trid3nt.tools.fetchers.hydrology.fetch_usgs_groundwater_levels import hooks as gw_hooks
from trid3nt.tools.fetchers.hydrology.fetch_usgs_nwis_gauges import hooks as nwis_hooks

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fetcher_fields"
_NWIS = "fetch_usgs_nwis_gauges"
_GW = "fetch_usgs_groundwater_levels"


def _row(source: str, data_class: str):
    return next(r for r in registration.get_spec(source).coverage if r.data_class == data_class)


@pytest.fixture
def nwis_calls(monkeypatch):
    import dataretrieval.nwis as nwis

    iv = _read_rdb((_FIXTURES / "nwis_iv_06610670.rdb").read_text())
    # The RDB names a series by its TS_ID ("271977_00065"); the JSON answer dataretrieval's get_iv reads names it by code.
    iv = iv.rename(columns={c: c.split("_", 1)[1] for c in iv.columns if c.startswith("271977_")})
    iv.index = pd.DatetimeIndex(pd.to_datetime(iv.index)).tz_localize(dt.timezone(dt.timedelta(hours=-6)))
    site = _read_rdb((_FIXTURES / "nwis_site_expanded.rdb").read_text())
    monkeypatch.setattr(nwis, "get_iv", lambda **kw: (iv, None))
    monkeypatch.setattr(nwis, "get_info", lambda **kw: (site, None))


def _nwis(mode="instantaneous"):
    spec = registration.get_spec(_NWIS)
    return nwis_hooks.read(spec, {"_mode": mode, "bbox": [-95.95, 41.1, -95.85, 41.3], "parameter": "00065"},
                           timeout_s=5.0)


def test_the_site_record_is_kept_under_its_own_names_beside_what_the_read_produced(nwis_calls):
    feats = {f["properties"]["site_no"]: f for f in _nwis()}
    hwy370 = feats["06610670"]
    record = hwy370["properties"]
    assert len(record) == 42 and record["alt_acy_va"] == 0.08 and record["alt_va"] == 936.34
    assert record["station_nm"] == "Missouri River at Hwy 370 at Bellevue, Nebr."
    assert set(feats) == {"06610670"} and hwy370["produced"]["gage_height_ft"] == 11.07
    assert not {"site_name", "gauge_datum_ft", "vertical_datum"} & (set(record) | set(hwy370["produced"]))


def test_a_site_with_no_reading_still_states_its_zero_and_its_accuracy(monkeypatch):
    import dataretrieval.nwis as nwis

    site = _read_rdb((_FIXTURES / "nwis_site_expanded.rdb").read_text())
    monkeypatch.setattr(nwis, "get_iv", lambda **kw: (pd.DataFrame(), None))
    monkeypatch.setattr(nwis, "get_info", lambda **kw: (site, None))
    feats = {f["properties"]["site_no"]: f for f in _nwis()}
    assert feats["06610000"]["properties"]["alt_acy_va"] == 0.16 and feats["06610000"]["properties"]["alt_va"] == 948.97
    assert feats["06610000"]["produced"]["gage_height_ft"] is None


def test_the_iv_qualifier_is_kept_for_the_latest_reading_and_on_every_row_of_a_window(nwis_calls):
    latest = {f["properties"]["site_no"]: f for f in _nwis()}["06610670"]["produced"]
    assert latest["gage_height_cd"] == "P"
    window = {f["properties"]["site_no"]: f for f in _nwis("hydrograph")}["06610670"]["produced"]
    readings = json.loads(window["stage_series"])
    assert readings[0] == {"datetime": "2026-11-17T00:00:00-0600", "value": 11.26, "cd": "P"}
    assert {r["cd"] for r in readings} == {"P"}


def test_the_whole_record_reaches_the_file_without_a_name_clash(nwis_calls):
    spec = registration.get_spec(_NWIS)
    data = features_to_fgb_bytes(_nwis(), spec, {"_mode": "instantaneous"})
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as handle:
        handle.write(data)
    try:
        frame = pyogrio.read_dataframe(handle.name)
    finally:
        os.unlink(handle.name)
    assert {"alt_acy_va", "alt_meth_cd", "drain_area_va", "huc_cd", "gage_height_cd", "gage_height_ft"} <= set(frame.columns)


def test_the_stage_row_reads_the_zero_its_accuracy_and_the_stage_standard(nwis_calls):
    spec = registration.get_spec(_NWIS)
    row = _row(_NWIS, "water level series")
    layer = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": f["geometry"], "properties": apply_ingest_transforms([f], spec, {})[0]["properties"]}
        for f in _nwis() if f["properties"]["site_no"] == "06610670"]}
    rec = records(layer, row.fields, source=_NWIS, site_fields=("site_no",))[0]
    assert rec.site == "06610670"
    assert rec.read("site") == "06610670" and rec.read("site_name").startswith("Missouri River at Hwy 370")
    assert rec.above == Measured(936.34, "ft") and rec.above_accuracy == Measured(0.08, "ft")
    assert rec.read("datum") == "NAVD88"
    # TM 3-A7: 0.01 ft or 0.2 percent of the effective stage, the gage height here, whichever is greater.
    assert rec.accuracy.unit == "ft" and rec.accuracy.value == pytest.approx(0.002 * rec.value.value)


def test_the_flow_row_states_no_accuracy_and_no_zero(nwis_calls):
    row = _row(_NWIS, "discharge series")
    assert "accuracy" not in row.fields and "above" not in row.fields


# ---- groundwater wells

def _body(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


def _wells(period_average=False):
    spec = registration.get_spec(_GW)
    params = {"state_code": "IA"}
    if period_average:
        params.update(start_date="1998-01-01", end_date="2005-12-31", period_average=True)
    feats = gw_hooks.parse_response(spec, params, [_body("usgs_field_measurements_des_moines.json")])
    return gw_hooks.enrich_merge(spec, params, feats, {"locations": SimpleNamespace(
        body=_body("usgs_monitoring_locations_des_moines.json"))})


def _location(mlid):
    return next(f["properties"] for f in json.loads(_body("usgs_monitoring_locations_des_moines.json"))["features"]
                if f["id"] == mlid)


def _layer(feats):
    spec = registration.get_spec(_GW)
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": f["geometry"], "properties": f["properties"]}
        for f in apply_ingest_transforms(feats, spec, {})]}


def test_a_well_keeps_its_whole_measurement_and_its_whole_location_record():
    feats = _wells()
    assert len(feats) == 185
    props = _layer(feats)["features"][0]["properties"]
    assert len(json.loads(_body("usgs_field_measurements_des_moines.json"))["features"][0]["properties"]) == 21
    assert {"observing_procedure", "observing_procedure_code", "measurement_rated", "field_visit_id",
            "qualifier", "last_modified", "measuring_agency", "control_condition"} <= set(props)
    assert {"location.well_constructed_depth", "location.altitude_accuracy", "location.altitude_method_name",
            "location.national_aquifer_code", "location.monitoring_location_name"} <= set(props)
    assert not {"parameter_label", "quantity", "well_depth_ft", "unit", "site_name"} & set(props)


def test_a_well_reads_its_members_through_the_row():
    row = _row(_GW, "groundwater level")
    wells = records(_layer(_wells()), row.fields, source=_GW, site_fields=("site_no",))
    first = wells[0]
    assert first.value == Measured(428.0, "ft") and first.quantity == "water_table_depth"
    assert first.read("depth").unit == "ft" and first.read("datum") == "Local Assumed Datum"
    assert first.read("site_name") == first.raw["location.monitoring_location_name"]
    read, refused = [], []
    for rec in wells:
        try:
            read.append(rec.accuracy)
        except FieldError as exc:
            refused.append(exc.error_code)
    assert len(read) == 66 and set(read) == {Measured(0.01, "ft")}
    assert len(refused) == 119 and set(refused) == {"FIELD_UNTABLED"}


def test_the_observation_carries_the_wells_depth_to_the_layer_read():
    row = _row(_GW, "groundwater level")
    layer = _layer(_wells())
    found = observation(layer, near={"lon": -93.8849487852759, "lat": 41.6035996958724}, fields=row.fields,
                        producer=_GW, to_units="ft", caption="a groundwater level")
    assert found.site_id == "413613093530401" and found.quantity == "water_table_depth"
    assert found.depth == _location("USGS-413613093530401")["well_constructed_depth"]
    assert found.depth_units == "ft"


def test_a_period_average_keeps_only_what_every_reading_states_alike():
    feats = _wells(period_average=True)
    by_site = {}
    for f in feats:
        by_site.setdefault((f["properties"]["monitoring_location_id"], f["properties"]["parameter_code"]), []).append(f)
    assert all(len(v) == 1 for v in by_site.values())
    many = next(f for f in feats if f["produced"]["reading_count"] > 1)
    assert many["produced"]["water_level"] is not None and many["produced"]["datetime"] == "1998-01-01"
    measured = json.loads(_body("usgs_field_measurements_des_moines.json"))["features"]
    mine = [m for m in measured if m["properties"]["monitoring_location_id"] == many["properties"]["monitoring_location_id"]]
    procedures = {m["properties"]["observing_procedure"] for m in mine}
    kept = many["properties"]["observing_procedure"]
    assert (kept is None) == (len(procedures) > 1)


def test_the_executor_writes_a_wells_whole_record_and_the_location_join_to_the_file(monkeypatch):
    from trid3nt.tools.fetchers._router.executors import chained_resolution, http_json

    measured, located = _body("usgs_field_measurements_des_moines.json"), _body("usgs_monitoring_locations_des_moines.json")
    monkeypatch.setattr(http_json, "_get", lambda spec, plan: located if "monitoring-locations" in plan.url else measured)
    monkeypatch.setattr(chained_resolution, "_get", http_json._get)
    spec = registration.get_spec(_GW)
    data = chained_resolution.execute(spec, {"state_code": "IA"})
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as handle:
        handle.write(data)
    try:
        frame = pyogrio.read_dataframe(handle.name)
    finally:
        os.unlink(handle.name)
    assert len(frame) == 185 and {"observing_procedure", "water_level", "site_no", "location.well_constructed_depth",
                                  "location.altitude_accuracy"} <= set(frame.columns)
    # Two of the 100 wells are absent from the recorded locations answer: their readings stay, with no location.
    assert 0 < frame["location.well_constructed_depth"].isna().sum() < 10 and frame["location.well_constructed_depth"].notna().sum() > 175
