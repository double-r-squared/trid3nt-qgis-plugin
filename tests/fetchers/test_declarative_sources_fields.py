"""The declarative sources keep every field their service returns under the service's own names.

USGS earthquakes, NWS event and alerts, FIRMS, the NHD water surface, the NSI, the NWI wetlands and TIGERweb read recorded
answers (tests/fixtures/fetcher_fields/README.md); FIRMS answers nothing without a key, so its VIIRS header is the
documented one stated by hand. No source carries a column map, and a request that can ask every field does."""

from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from pathlib import Path

import pyogrio
import pytest
import yaml

from trid3nt_server.tools.fetchers import _router
from trid3nt_server.tools.fetchers._router import registration
from trid3nt_server.tools.fetchers._router.executors import http_json, vector_fgb, vector_ogr
from trid3nt_server.tools.fetchers._router.executors.vector_fgb import features_to_fgb_bytes

_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "fetcher_fields"
_ROOT = Path(_router.__file__).resolve().parents[1]


def _spec(name):
    return registration.get_spec(name)


def _json(name):
    return json.loads((_FIX / name).read_text())


def _written(spec, features, params=None):
    data = features_to_fgb_bytes(features, spec, params)
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as handle:
        handle.write(data)
    try:
        return pyogrio.read_dataframe(handle.name)
    finally:
        os.unlink(handle.name)


def _no_case_twins(frame):
    counts = Counter(str(c).lower() for c in frame.columns)
    assert [c for c, n in counts.items() if n > 1] == []


def _walked(spec, body):
    return http_json._features(spec, {}, [json.dumps(body).encode()])


def _ingests():
    return {p.parent.name: yaml.safe_load(p.read_text()).get("ingest") or {} for p in _ROOT.rglob("source.yaml")}


def test_no_source_states_a_column_map_and_the_projection_is_gone():
    assert not hasattr(vector_fgb, "apply_column_map")
    assert [name for name, ingest in _ingests().items() if "column_map" in ingest] == []


def test_an_earthquake_keeps_every_usgs_property_and_produces_only_what_it_makes():
    body = _json("usgs_earthquakes_nc_excerpt.json")
    spec = _spec("fetch_usgs_earthquakes")
    frame = _written(spec, _walked(spec, body))
    first = body["features"][0]
    assert set(first["properties"]) <= set(frame.columns)
    row = frame.iloc[0]
    assert row["time"] == first["properties"]["time"] and row["magType"] == first["properties"]["magType"]
    assert row["id"] == first["id"] and row["depth_km"] == first["geometry"]["coordinates"][2]
    assert row["time_utc"] == "2024-12-05T23:30:30Z" and row["updated_utc"].endswith("Z")
    assert not {"mag_type", "event_type"} & set(frame.columns)
    _no_case_twins(frame)


def test_nws_event_keeps_every_alert_property_the_service_returns():
    body = _json("nws_alerts_fl_excerpt.json")
    spec = _spec("fetch_nws_event")
    frame = _written(spec, _walked(spec, body))
    assert len(frame) == 2
    flat = vector_fgb.flatten_record(body["features"][0]["properties"])
    assert set(flat) <= set(frame.columns)
    assert {"geocode.UGC", "affectedZones", "scope", "language"} <= set(frame.columns)
    assert set(frame["id"]) == {f["properties"]["id"] for f in body["features"] if f["geometry"]}
    _no_case_twins(frame)


def test_the_conus_alerts_keep_every_property_and_the_alert_without_a_polygon():
    from trid3nt_server.tools.fetchers.weather.fetch_nws_alerts_conus import hooks

    body = _json("nws_alerts_fl_excerpt.json")
    body["features"].append({**body["features"][0], "geometry": None})
    spec = _spec("fetch_nws_alerts_conus")
    found = hooks.parse_response(spec, {}, [json.dumps(body).encode()])
    frame = _written(spec, found)
    assert len(frame) == 4 and frame["geometry"].isna().sum() == 2
    assert {"senderName", "geocode.SAME", "sent", "scope", "language"} <= set(frame.columns)


_VIIRS = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_ti5,frp,daynight\n"
    "37.5,-120.5,330.1,0.39,0.36,2024-10-01,0613,N,VIIRS,n,2.0NRT,290.2,1.4,D\n"
    "37.6,-120.4,341.8,0.4,0.37,2024-10-01,0613,N,VIIRS,h,2.0NRT,291.0,5.2,D\n"
)


def test_firms_keeps_every_column_the_viirs_answer_carries():
    spec = _spec("fetch_firms_active_fire")
    found = http_json._features(spec, {"bbox": [-124, 32, -114, 42]}, [_VIIRS.encode()])
    frame = _written(spec, found)
    assert {"bright_ti4", "bright_ti5", "latitude", "longitude", "acq_time"} <= set(frame.columns)
    assert not {"brightness", "bright_t31"} & set(frame.columns)
    assert set(frame["acq_time"]) == {"0613"} and set(frame["confidence"]) == {"n", "h"}


def test_the_nhd_water_surface_asks_every_field_and_keeps_every_one():
    spec = _spec("fetch_nhd_water_surface")
    assert spec.endpoints["data"].query["outFields"] == "*"
    body = _json("nhd_area_hr_all_fields.json")
    frame = _written(spec, _walked(spec, body))
    assert set(body["features"][0]["properties"]) <= set(frame.columns) and "ftype" in frame.columns
    assert frame.iloc[0]["permanent_identifier"] == body["features"][0]["properties"]["permanent_identifier"]
    _no_case_twins(frame)


def test_the_structure_inventory_keeps_every_field_and_derives_nothing():
    body = _json("nsi_structures_omaha_excerpt.json")
    spec = _spec("fetch_usace_nsi")
    frame = _written(spec, _walked(spec, body))
    assert set(body["features"][0]["properties"]) <= set(frame.columns)
    assert {"grnd_elv_m", "val_struct", "occtype", "bid", "ftprntid", "zone_sub"} <= set(frame.columns)
    assert not {"component_type", "replacement_value"} & set(frame.columns)
    first = body["features"][0]["properties"]
    assert frame.set_index("fd_id").loc[first["fd_id"], "val_struct"] == first["val_struct"]
    _no_case_twins(frame)


def test_the_wetlands_keep_every_field_the_map_service_returns():
    df = pyogrio.read_dataframe(_FIX / "nwi_wetlands_omaha_esrijson.json")
    feats = [{"type": "Feature", "geometry": g.__geo_interface__, "properties": p}
             for g, p in zip(df.geometry, df.drop(columns=[df.geometry.name]).to_dict(orient="records"))]
    frame = _written(_spec("fetch_nwi_wetlands"), feats)
    assert {"ATTRIBUTE", "WETLAND_TYPE", "QAQC_CODE", "ACRES", "GlobalID", "Shape_Area"} <= set(frame.columns)
    assert frame.iloc[0]["ATTRIBUTE"] == "PEM1A"
    _no_case_twins(frame)


@pytest.mark.parametrize("name", ["tigerweb_county_all_fields.json", "tigerweb_state_all_fields.json"])
def test_the_administrative_boundaries_keep_every_tigerweb_field(name):
    body = _json(name)
    feats = [{"type": "Feature", "geometry": f["geometry"], "properties": f["properties"]} for f in body["features"]]
    frame = _written(_spec("fetch_administrative_boundaries"), feats)
    assert set(body["features"][0]["properties"]) <= set(frame.columns)
    assert {"GEOID", "NAME", "INTPTLAT", "AREALAND"} <= set(frame.columns)
    _no_case_twins(frame)


@pytest.mark.parametrize("name", ["fetch_nhd_hr_waterbodies", "fetch_nhd_hr_river_areas", "fetch_nhdplus_hr_flowline_network"])
def test_the_ogr_nhd_rows_ask_the_service_for_every_field(name):
    spec = _spec(name)
    url = vector_ogr.build_query(spec, (-96.1, 41.2, -95.8, 41.5), where="1=1")
    assert "outFields=%2A" in url


def test_wfigs_and_ehydro_ask_for_every_field(monkeypatch):
    from trid3nt_server.tools.fetchers.hazard.fetch_wfigs_incident import hooks as wfigs
    from trid3nt_server.tools.fetchers.hydrology.fetch_ehydro_surveys import hooks as ehydro

    assert wfigs._build_wfigs_query("Ranger Road", None)["outFields"] == "*"
    asked = []

    def _get(_client, _url, params=None):
        asked.append(params)
        return b'{"features": []}', "application/json", _url

    monkeypatch.setattr(ehydro, "get_bytes", _get)
    ehydro._index(_spec("fetch_ehydro_surveys"), {"bbox": [-82.5, 42.9, -82.4, 43.0]})
    assert asked[0]["outFields"] == "*"


def test_the_declarative_enrichment_block_is_gone():
    from trid3nt_server.tools.fetchers._router import field_map

    assert not hasattr(field_map, "enrich_plans") and not hasattr(field_map, "enrich_merge")
    assert [name for name, ingest in _ingests().items() if "enrich" in ingest] == []


_SOILGRIDS_PROPERTIES = ["bdod", "cec", "cfvo", "clay", "nitrogen", "ocd", "ocs", "phh2o", "sand", "silt", "soc"]
_SOILGRIDS_STATISTICS = ["mean", "Q0.05", "Q0.5", "Q0.95", "uncertainty"]


def test_soilgrids_offers_every_property_and_statistic_the_service_serves():
    spec = _spec("fetch_soilgrids")
    assert spec.params["soil_property"].values == _SOILGRIDS_PROPERTIES
    assert spec.params["statistic"].values == _SOILGRIDS_STATISTICS
    assert "0-30cm" in spec.params["depth"].values
    units = spec.normalize.units_by_param
    scale = spec.ingest["projected_window"]["scale_by_param"]
    assert set(units["map"]) == set(scale["map"]) == set(_SOILGRIDS_PROPERTIES)
    assert scale["map"]["nitrogen"] == 100.0 and scale["map"]["cec"] == 10.0 and scale["map"]["ocs"] == 10.0
    assert units["map"]["ocd"] == "kg/m3" and units["map"]["nitrogen"] == "g/kg" and units["map"]["cfvo"] == "cm3/100cm3"
    assert scale["override"]["map"] == {"uncertainty": 10.0} and units["override"]["map"] == {"uncertainty": "ratio"}


def test_the_soilgrids_request_names_the_statistic_and_an_uncertainty_is_scaled_by_ten():
    from trid3nt_server.tools.fetchers._router.params import by_param

    spec = _spec("fetch_soilgrids")
    assert spec.endpoints["data"].url_template.endswith("{soil_property}_{depth}_{statistic}.vrt")
    scale = spec.ingest["projected_window"]["scale_by_param"]
    divisor = by_param
    assert divisor(scale, {"soil_property": "clay", "statistic": "mean"}) == 10.0
    assert divisor(scale, {"soil_property": "bdod", "statistic": "Q0.5"}) == 100.0
    assert divisor(scale, {"soil_property": "bdod", "statistic": "uncertainty"}) == 10.0


def test_statsgo_offers_the_two_fields_sciencebase_publishes_with_the_units_it_states():
    spec = _spec("fetch_statsgo_soils")
    assert spec.params["field"].values == ["KFFACT", "THICK"]
    assert spec.normalize.units_by_param["map"] == {"KFFACT": "inches per hour", "THICK": "inches"}
