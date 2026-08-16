"""Migrated coverage for the station-siblings fold (ADR 0065).

The five deferred station-sibling twins (asos_metar / raws_weather / snotel_snow /
airnow_air_quality / openaq_measurements) fold onto the EXISTING router phases with
zero new machinery. This file carries the value-bearing offline coverage that the
deleted twin test files held: registration parity, the composition primitives
(multi-state discovery, station x day enrich expansion, batched null-tolerant merge,
sensor->parameter join), and the keyed missing-key credential parity. Live
edge-matrix parity vs the twins was proven at fold time (drivers, ADR 0065); those
are network gates, not offline tests.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import pytest

from trid3nt_server.data import TOOL_REGISTRY
from trid3nt_server.data.fetchers._router import router
from trid3nt_server.data.fetchers._router.executors import (
    chained_resolution as cr,
)
from trid3nt_server.data.fetchers._router.executors import http_json
from trid3nt_server.data.fetchers._router.registration import _SPEC_REGISTRY
from trid3nt_server.data.fetchers._router.spec import load_spec_from_path
from trid3nt_server.credentials.credential_registry import is_credential_shaped_error

_FETCHERS = Path("trid3nt_server/data/fetchers")
_SPECS = {
    "fetch_asos_metar": _FETCHERS / "weather/fetch_asos_metar/source.yaml",
    "fetch_raws_weather": _FETCHERS / "weather/fetch_raws_weather/source.yaml",
    "fetch_snotel_snow": _FETCHERS / "soil/fetch_snotel_snow/source.yaml",
    "fetch_airnow_air_quality": _FETCHERS / "weather/fetch_airnow_air_quality/source.yaml",
    "fetch_openaq_measurements": _FETCHERS / "weather/fetch_openaq_measurements/source.yaml",
}


def _spec(name: str):
    return load_spec_from_path(_SPECS[name])


def _gdf(fgb: bytes):
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb)
        p = f.name
    return gpd.read_file(p, engine="pyogrio")


# --------------------------------------------------------------------------- #
# Registration parity: all five spec-served under the twin name.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", list(_SPECS))
def test_spec_served_under_twin_name(name):
    assert name in TOOL_REGISTRY
    assert name in _SPEC_REGISTRY
    assert "_router._promoted" in TOOL_REGISTRY[name].fn.__module__


# --------------------------------------------------------------------------- #
# asos_metar: multi-state discovery (resolve) + bulk-CSV main fetch.
# --------------------------------------------------------------------------- #


def test_asos_resolve_discovers_and_parses_obs():
    spec = _spec("fetch_asos_metar")
    geo = json.dumps({"features": [
        {"id": "RSW", "geometry": {"coordinates": [-81.75, 26.53]}, "properties": {"sname": "FT MYERS"}},
        {"id": "APF", "geometry": {"coordinates": [-81.77, 26.15]}, "properties": {"sname": "NAPLES"}},
    ]}).encode()
    csv = (b"station,valid,lon,lat,elevation,tmpf,dwpf,sknt,drct,gust,alti,mslp,vsby,wxcodes,skyc1,skyl1\n"
           b"RSW,2024-09-26 00:00,-81.75,26.53,9,86,75,6,290,null,29.99,1014.8,10,null,CLR,null\n"
           b"APF,2024-09-26 00:00,-81.77,26.15,8,84,74,5,300,null,29.98,1014.5,10,null,CLR,null\n")

    def fake_get(spec_, plan):
        return csv if plan.url.endswith("asos.py") else geo

    raw = {"bbox": [-82.5, 25.8, -81.0, 27.5], "start_time": "2024-09-26", "end_time": "2024-09-27"}
    params = router.validate_params(spec, raw)
    with patch.object(cr, "_get", fake_get):
        params = cr.pre_resolve(spec, params)
        assert sorted(params["_station_ids"]) == ["APF", "RSW"]
    with patch.object(http_json, "_get", fake_get):
        g = _gdf(http_json.execute(spec, params))
    assert len(g) == 2
    assert sorted(g["station"].tolist()) == ["APF", "RSW"]


def test_asos_no_stations_raises_empty():
    spec = _spec("fetch_asos_metar")
    empty = json.dumps({"features": []}).encode()
    raw = {"bbox": [-30.0, 10.0, -20.0, 20.0], "start_time": "2024-09-26", "end_time": "2024-09-27"}
    params = router.validate_params(spec, raw)
    with patch.object(cr, "_get", lambda s, p: empty):
        with pytest.raises(Exception) as exc:
            cr.pre_resolve(spec, params)
    assert exc.value.error_code == "ASOS_METAR_EMPTY"


def test_asos_future_start_rejected():
    spec = _spec("fetch_asos_metar")
    raw = {"bbox": [-82.5, 25.8, -81.0, 27.5], "start_time": "2099-01-01"}
    params = router.validate_params(spec, raw)
    with patch.object(cr, "_get", lambda s, p: b"{}"):
        with pytest.raises(Exception) as exc:
            cr.pre_resolve(spec, params)
    assert exc.value.error_code == "ASOS_METAR_INPUT_ERROR"


# --------------------------------------------------------------------------- #
# raws_weather: enrich EXPANDS station features into per-obs rows (best-effort).
# --------------------------------------------------------------------------- #


def test_raws_enrich_expands_and_best_effort_survives():
    spec = _spec("fetch_raws_weather")
    geo = json.dumps({"features": [
        {"id": "CISC1", "geometry": {"coordinates": [-120.3, 38.9]},
         "properties": {"sname": "CALDOR RAWS", "elevation": 1800}},
    ]}).encode()
    day = json.dumps({"data": [
        {"utc_valid": "2024-09-01T00:00Z", "tmpf": "70", "URHRGZZ": "30", "sknt": "5", "drct": "280",
         "VBIRGZZ": "9", "XRIRGZZ": "600", "PCIRGZZ": "0"},
        {"utc_valid": "2024-09-01T01:00Z", "tmpf": "68", "URHRGZZ": "32", "sknt": "4", "drct": "290",
         "VBIRGZZ": "8", "XRIRGZZ": "0", "PCIRGZZ": "0"},
    ]}).encode()

    def fake_get(spec_, plan):
        if "obhistory" in plan.url:
            # 2024-09-01 succeeds; 2024-09-02 raises (best-effort skip)
            if plan.params.get("date") == "2024-09-02":
                from trid3nt_server.data.fetchers._router.errors import router_upstream_error
                raise router_upstream_error(spec.error_code_prefix, "boom")
            return day
        return geo

    raw = {"bbox": [-121.0, 38.5, -119.5, 39.5], "start_time": "2024-09-01", "end_time": "2024-09-02"}
    params = router.validate_params(spec, raw)
    with patch.object(cr, "_get", fake_get):
        params = cr.pre_resolve(spec, params)
        g = _gdf(cr.execute(spec, params))
    # 1 station x 1 good day x 2 obs = 2 rows (the failed 2nd day is skipped, station survives)
    assert len(g) == 2
    assert set(g["station"]) == {"CISC1"}
    assert list(g.columns[:4]) == ["station", "station_name", "state", "utc_valid"]


def test_raws_no_stations_raises_empty():
    spec = _spec("fetch_raws_weather")
    geo = json.dumps({"features": [
        {"id": "NOTRAWS", "geometry": {"coordinates": [-120.3, 38.9]}, "properties": {"sname": "AIRPORT"}},
    ]}).encode()
    raw = {"bbox": [-121.0, 38.5, -119.5, 39.5], "start_time": "2024-09-01", "end_time": "2024-09-01"}
    params = router.validate_params(spec, raw)
    with patch.object(cr, "_get", lambda s, p: geo):
        with pytest.raises(Exception) as exc:
            cr.pre_resolve(spec, params)
    assert exc.value.error_code == "RAWS_WEATHER_EMPTY"


# --------------------------------------------------------------------------- #
# snotel_snow: catalog main-fetch + batched enrich; degrade-to-locations + NO_STATIONS.
# --------------------------------------------------------------------------- #


def _snotel_catalog():
    return json.dumps([
        {"stationTriplet": "335:CO:SNTL", "networkCode": "SNTL", "name": "A", "stateCode": "CO",
         "elevation": 10000, "latitude": 39.5, "longitude": -106.0},
        {"stationTriplet": "999:CA:SCAN", "networkCode": "SCAN", "name": "B", "stateCode": "CA",
         "elevation": 8000, "latitude": 38.0, "longitude": -120.0},  # outside bbox
    ]).encode()


def test_snotel_merge_and_degrade_to_locations():
    spec = _spec("fetch_snotel_snow")
    data = json.dumps([
        {"stationTriplet": "335:CO:SNTL", "data": [
            {"stationElement": {"elementCode": "WTEQ"}, "values": [{"value": "0.0", "date": "2026-01-10"}, {"value": "12.5", "date": "2026-01-11"}]},
            {"stationElement": {"elementCode": "SNWD"}, "values": [{"value": "48", "date": "2026-01-11"}]},
        ]},
    ]).encode()

    def fake_get(spec_, plan):
        return _snotel_catalog() if plan.url.endswith("/stations") or "/stations?" in plan.url else data

    raw = {"bbox": [-106.5, 39.0, -105.5, 40.0]}
    params = router.validate_params(spec, raw)
    with patch.object(cr, "_get", fake_get):
        g = _gdf(cr.execute(spec, params))
    assert len(g) == 1  # only the in-bbox station
    row = g.iloc[0]
    assert row["triplet"] == "335:CO:SNTL"
    assert float(row["swe_in"]) == 12.5  # latest non-null (off-season 0.0 preserved earlier, latest wins)
    assert float(row["snow_depth_in"]) == 48.0

    # DATA failure -> degrade to locations (station survives with null readings).
    def fail_data(spec_, plan):
        if plan.url.endswith("/stations") or "/stations?" in plan.url:
            return _snotel_catalog()
        from trid3nt_server.data.fetchers._router.errors import router_upstream_error
        raise router_upstream_error(spec.error_code_prefix, "data down")

    with patch.object(cr, "_get", fail_data):
        g2 = _gdf(cr.execute(spec, params))
    assert len(g2) == 1
    assert g2.iloc[0]["swe_in"] is None or str(g2.iloc[0]["swe_in"]) in ("nan", "None")


def test_snotel_no_stations_raises():
    spec = _spec("fetch_snotel_snow")
    raw = {"bbox": [-80.0, 25.0, -79.0, 26.0]}  # Florida: no SNOTEL
    params = router.validate_params(spec, raw)
    with patch.object(cr, "_get", lambda s, p: _snotel_catalog()):
        with pytest.raises(Exception) as exc:
            cr.execute(spec, params)
    assert exc.value.error_code == "SNOTEL_NO_STATIONS"


# --------------------------------------------------------------------------- #
# openaq: paging + per-location latest + sensor->parameter join (expanding).
# --------------------------------------------------------------------------- #


def test_openaq_paging_and_sensor_join():
    spec = _spec("fetch_openaq_measurements")
    loc = json.dumps({"results": [
        {"id": 101, "name": "A", "country": {"code": "IN"}, "coordinates": {"longitude": 77.1, "latitude": 28.6},
         "sensors": [{"id": 9001, "parameter": {"name": "pm25", "displayName": "PM2.5", "units": "ug/m3"}}]},
    ]}).encode()
    latest = json.dumps({"results": [
        {"sensorsId": 9001, "value": 88.5, "coordinates": {"longitude": 77.1, "latitude": 28.6},
         "datetime": {"utc": "2026-08-01T00:00:00Z", "local": "x"}},
    ]}).encode()

    def fake_get(spec_, plan):
        if plan.url.endswith("/locations"):
            return loc
        return latest

    raw = {"bbox": [76.8, 28.4, 77.4, 28.9], "parameters": ["pm25"], "api_key": "DUMMY"}
    params = router.validate_params(spec, raw)
    with patch.object(cr, "_get", fake_get):
        g = _gdf(cr.execute(spec, params))
    assert len(g) == 1
    assert g.iloc[0]["parameter"] == "pm25"
    assert float(g.iloc[0]["value"]) == 88.5
    assert g.iloc[0]["unit"] == "ug/m3"


# --------------------------------------------------------------------------- #
# Keyed missing-key credential parity (byte-identical typed error, pre-network).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,env,code",
    [
        ("fetch_airnow_air_quality", "TRID3NT_AIRNOW_API_KEY", "AIRNOW_MISSING_KEY"),
        ("fetch_openaq_measurements", "TRID3NT_OPENAQ_API_KEY", "OPENAQ_KEY_REQUIRED"),
    ],
)
def test_keyed_missing_key_is_credential_shaped(name, env, code, monkeypatch):
    monkeypatch.delenv(env, raising=False)
    spec = _spec(name)
    with pytest.raises(Exception) as exc:
        router.route(spec, {"bbox": [76.8, 28.4, 77.4, 28.9]})
    assert exc.value.error_code == code
    assert exc.value.retryable is False
    assert is_credential_shaped_error(name, exc.value) is True


@pytest.mark.parametrize(
    "name,code",
    [("fetch_airnow_air_quality", "AIRNOW_INPUT_ERROR"), ("fetch_openaq_measurements", "OPENAQ_INPUT_INVALID")],
)
def test_keyed_bad_bbox_input_error(name, code):
    spec = _spec(name)
    with pytest.raises(Exception) as exc:
        router.route(spec, {"bbox": [-81.0, 26.0, -81.0, 27.0]})  # degenerate
    assert exc.value.error_code == code
