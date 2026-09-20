"""The gauge fold: spec-driven, with the two blockers it resolved.

The readings and the expanded site record are parsed together - the readings
win and the site record decorates them with the zero a gage height is counted
from - an all-empty result answers an honest no-stations, and the window mode
switches the output schema between the instantaneous and hydrograph shapes.
Plus the spatial-selector and temporal-window edge matrix, offline over
synthetic payloads."""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.fetchers._router import hooks, registration
from trid3nt_server.tools.fetchers._router.executors import http_json
from trid3nt_server.tools.fetchers._router.transport import errors as terrors
from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_nwis_gauges import (
    hooks as nwis_hooks)


@pytest.fixture(scope="module")
def spec():
    s = registration.get_spec("fetch_usgs_nwis_gauges")
    assert s is not None, "fetch_usgs_nwis_gauges must be spec-served"
    return s


def _iv_series(site, name, lon, lat, param, val, dt):
    return {"sourceInfo": {"siteCode": [{"value": site}], "siteName": name,
            "geoLocation": {"geogLocation": {"latitude": lat, "longitude": lon}}},
            "variable": {"variableCode": [{"value": param}]},
            "values": [{"value": [{"value": str(val), "dateTime": dt}]}]}


def _iv_body():
    return json.dumps({"value": {"timeSeries": [
        _iv_series("01646500", "POTOMAC", -77.12, 38.95, "00060", "1200.0", "2024-01-01T12:00:00Z"),
        _iv_series("01646500", "POTOMAC", -77.12, 38.95, "00065", "3.4", "2024-01-01T12:00:00Z"),
        _iv_series("01638500", "SHENANDOAH", -77.80, 39.02, "00060", "800.0", "2024-01-01T12:00:00Z"),
    ]}}).encode()


def _window_series(param, samples):
    return {"sourceInfo": {"siteCode": [{"value": "01646500"}], "siteName": "POTOMAC",
                           "geoLocation": {"geogLocation": {"latitude": 38.95, "longitude": -77.12}}},
            "variable": {"variableCode": [{"value": param}]},
            "values": [{"value": [{"value": str(v), "dateTime": d} for d, v in samples]}]}


def _iv_window_body():
    flow = [("2024-01-01T00:00:00Z", 1000.0), ("2024-01-01T01:00:00Z", 1100.0), ("2024-01-01T02:00:00Z", 1200.0)]
    stage = [("2024-01-01T00:00:00Z", 3.1), ("2024-01-01T01:00:00Z", 3.4), ("2024-01-01T02:00:00Z", 3.9)]
    return json.dumps({"value": {"timeSeries": [
        _window_series("00060", flow), _window_series("00065", stage)]}}).encode()


_SITE_RDB = (
    "# comment\n"
    "agency_cd\tsite_no\tstation_nm\tdec_lat_va\tdec_long_va\talt_va\talt_datum_cd\n"
    "5s\t15s\t50s\t16s\t16s\t16s\t10s\n"
    "USGS\t01646500\tPOTOMAC RIVER\t38.95\t-77.12\t37.20\tNAVD88\n"
    "USGS\t01638500\tSHENANDOAH\t39.02\t-77.80\t\t\n"
).encode()




def test_nwis_registered_and_spec_served(spec):
    from trid3nt_server.tools import TOOL_REGISTRY

    assert "fetch_usgs_nwis_gauges" in TOOL_REGISTRY
    assert "_promoted" in TOOL_REGISTRY["fetch_usgs_nwis_gauges"].module
    assert spec.error_code_prefix == "NWIS_GAUGES"
    assert spec.empty_error_suffix == "NO_STATIONS"


def test_nwis_hooks_registered():
    for h in ("usgs_nwis.resolve", "usgs_nwis.build_request", "usgs_nwis.parse"):
        assert h in hooks.HOOK_REGISTRY




def test_parse_iv_instantaneous_carries_the_gauge_s_own_zero(spec):
    feats = hooks.HOOK_REGISTRY["usgs_nwis.parse"](
        spec, {"_mode": "instantaneous"}, [_iv_body(), _SITE_RDB])
    assert len(feats) == 2  # two distinct sites merged over discharge + gage
    assert set(feats[0]["properties"]) == {
        "site_no", "site_name", "discharge_cfs", "gage_height_ft", "water_temp_c",
        "reading_dt", "gauge_datum_ft", "vertical_datum"}
    by = {f["properties"]["site_no"]: f["properties"] for f in feats}
    assert by["01646500"]["discharge_cfs"] == 1200.0 and by["01646500"]["gage_height_ft"] == 3.4
    assert by["01646500"]["gauge_datum_ft"] == 37.20
    assert by["01646500"]["vertical_datum"] == "NAVD88"
    # a site that publishes no zero states none rather than a guessed one
    assert by["01638500"]["gauge_datum_ft"] is None


def test_parse_iv_window_publishes_the_stage_series_beside_the_discharge(spec):
    feats = hooks.HOOK_REGISTRY["usgs_nwis.parse"](
        spec, {"_mode": "hydrograph"}, [_iv_window_body(), _SITE_RDB])
    p = feats[0]["properties"]
    assert set(p) == {"site_no", "site_name", "discharge_cfs", "gage_height_ft", "water_temp_c",
                      "reading_dt", "time_series_csv", "stage_series_csv", "temp_series_csv",
                      "time_start", "time_end", "n_timesteps", "discharge_min_cfs",
                      "discharge_max_cfs", "discharge_mean_cfs",
                      "gauge_datum_ft", "vertical_datum"}
    assert p["n_timesteps"] == 3 and p["discharge_min_cfs"] == 1000.0 and p["discharge_max_cfs"] == 1200.0
    assert p["time_series_csv"].startswith("2024-01-01T00:00:00Z,1000.000000")
    assert p["stage_series_csv"].startswith("2024-01-01T00:00:00Z,3.100000")
    assert p["gauge_datum_ft"] == 37.20 and p["vertical_datum"] == "NAVD88"


def test_the_temperature_code_is_read_into_its_own_column(spec):
    """The third coverage row is asked by the parameter code NWIS answers to,
    and the reading lands in the column that row names rather than in the
    discharge one it is not measured in."""
    body = json.dumps({"value": {"timeSeries": [
        _iv_series("14211720", "WILLAMETTE", -122.67, 45.51, "00010", "18.4",
                   "2026-09-20T03:30:00Z")]}}).encode()
    p = hooks.HOOK_REGISTRY["usgs_nwis.parse"](spec, {"_mode": "instantaneous"},
                                               [body])[0]["properties"]
    assert p["water_temp_c"] == 18.4
    assert p["discharge_cfs"] is None and p["gage_height_ft"] is None
    assert p["reading_dt"] == "2026-09-20T03:30:00Z"


def test_the_temperature_window_is_its_own_series_column(spec):
    samples = [("2026-09-17T00:00:00Z", 18.7), ("2026-09-17T01:00:00Z", 18.6)]
    body = json.dumps({"value": {"timeSeries": [
        _window_series("00010", samples)]}}).encode()
    p = hooks.HOOK_REGISTRY["usgs_nwis.parse"](spec, {"_mode": "hydrograph"},
                                               [body])[0]["properties"]
    assert p["temp_series_csv"].startswith("2026-09-17T00:00:00Z,18.700000")
    assert p["water_temp_c"] == 18.6
    assert p["time_series_csv"] == "" and p["n_timesteps"] == 0


def test_a_parameter_this_source_reads_into_no_column_refuses(spec):
    assert _resolve_err(spec, {"bbox": [-122.7, 45.4, -122.6, 45.6],
                               "parameter": "00095"}) == "NWIS_GAUGES_INPUT_ERROR"


def test_the_asked_parameter_is_what_both_services_are_called_with(spec):
    params = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](
        spec, {"bbox": [-122.7, 45.4, -122.6, 45.6], "parameter": "00010"})
    assert params["parameter"] == "00010"
    plans = hooks.HOOK_REGISTRY["usgs_nwis.build_request"](spec, params)
    assert [pl.params["parameterCd"] for pl in plans] == ["00010", "00010"]
    pair = hooks.HOOK_REGISTRY["usgs_nwis.build_request"](
        spec, hooks.HOOK_REGISTRY["usgs_nwis.resolve"](
            spec, {"bbox": [-122.7, 45.4, -122.6, 45.6]}))
    assert pair[0].params["parameterCd"] == "00060,00065"


def test_parse_site_rdb(spec):
    feats = hooks.HOOK_REGISTRY["usgs_nwis.parse"](spec, {"_mode": "instantaneous"}, [_SITE_RDB])
    assert {f["properties"]["site_no"] for f in feats} == {"01646500", "01638500"}
    assert all(f["properties"]["discharge_cfs"] is None for f in feats)  # locations only


def test_parse_with_no_body_at_all_refuses_rather_than_returning_nothing(spec):
    with pytest.raises(Exception) as caught:
        hooks.HOOK_REGISTRY["usgs_nwis.parse"](spec, {"_mode": "instantaneous"}, [b"", b""])
    assert caught.value.error_code == "NWIS_GAUGES_NO_STATIONS"




def _resolve_err(spec, params):
    with pytest.raises(Exception) as ei:
        hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, params)
    return ei.value.error_code


def test_resolve_no_selector_input_error(spec):
    assert _resolve_err(spec, {}) == "NWIS_GAUGES_INPUT_ERROR"


def test_resolve_bad_state_input_error(spec):
    assert _resolve_err(spec, {"state_code": "ZZ"}) == "NWIS_GAUGES_INPUT_ERROR"


def test_resolve_state_uppercased_instantaneous(spec):
    out = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, {"state_code": "wa"})
    assert out["state_code"] == "WA" and out["_mode"] == "instantaneous" and out["bbox"] is None


def test_resolve_bbox_too_large_no_state(spec):
    assert _resolve_err(spec, {"bbox": [-125, 25, -115, 45]}) == "NWIS_GAUGES_BBOX_TOO_LARGE"


def test_resolve_bbox_too_large_with_state_ok(spec):
    out = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, {"bbox": [-125, 25, -115, 45], "state_code": "WA"})
    assert out["state_code"] == "WA"  # state wins, no area error


def test_resolve_period_wins_hydrograph(spec):
    out = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, {"bbox": [-82.4, 26.3, -81.6, 26.9], "period": "P7D"})
    assert out["_mode"] == "hydrograph" and out["window"] == "P7D"


def test_resolve_bad_period_input_error(spec):
    assert _resolve_err(spec, {"bbox": [-82.4, 26.3, -81.6, 26.9], "period": "7 days"}) == "NWIS_GAUGES_INPUT_ERROR"


def test_resolve_one_date_input_error(spec):
    assert _resolve_err(spec, {"bbox": [-82.4, 26.3, -81.6, 26.9], "start_date": "2024-01-01"}) == "NWIS_GAUGES_INPUT_ERROR"


def test_resolve_both_dates_window(spec):
    out = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, {"bbox": [-82.4, 26.3, -81.6, 26.9], "start_date": "2024-01-01", "end_date": "2024-01-05"})
    assert out["_mode"] == "hydrograph" and out["window"] == ["2024-01-01", "2024-01-05"]


def test_resolve_over_120d_input_error(spec):
    assert _resolve_err(spec, {"bbox": [-82.4, 26.3, -81.6, 26.9], "start_date": "2024-01-01", "end_date": "2024-06-01"}) == "NWIS_GAUGES_INPUT_ERROR"




def test_build_instantaneous_iv_then_site(spec):
    p = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, {"bbox": [-82.4, 26.3, -81.6, 26.9]})
    plans = hooks.HOOK_REGISTRY["usgs_nwis.build_request"](spec, {"bbox": [-82.4, 26.3, -81.6, 26.9], **p})
    assert len(plans) == 2 and plans[0].url.endswith("/iv/") and plans[1].url.endswith("/site/")
    assert plans[0].params["bBox"] == "-82.4,26.3,-81.6,26.9"


def test_build_hydrograph_asks_the_site_service_for_the_expanded_record(spec):
    p = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, {"bbox": [-82.4, 26.3, -81.6, 26.9], "period": "P7D"})
    plans = hooks.HOOK_REGISTRY["usgs_nwis.build_request"](spec, {"bbox": [-82.4, 26.3, -81.6, 26.9], **p})
    assert len(plans) == 2 and plans[0].params.get("period") == "P7D"
    assert plans[1].params.get("siteOutput") == "expanded"


def test_build_state_selector(spec):
    p = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, {"state_code": "WA"})
    plans = hooks.HOOK_REGISTRY["usgs_nwis.build_request"](spec, {"state_code": "WA", **p})
    assert plans[0].params.get("stateCd") == "WA" and "bBox" not in plans[0].params




def test_iv_empty_degrades_to_the_station_locations(spec, monkeypatch):
    params = {"bbox": [-77.9, 38.9, -77.0, 39.1]}
    params.update(hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, dict(params)))
    calls = {"n": 0}

    def _fake_get(_spec, plan):
        calls["n"] += 1
        return b"" if plan.url.endswith("/iv/") else _SITE_RDB  # IV empty -> Site

    monkeypatch.setattr(http_json, "_get", _fake_get)
    fgb = http_json.execute(spec, params)
    assert calls["n"] == 2  # tried IV then Site
    import os
    import tempfile

    import geopandas as gpd
    f = tempfile.NamedTemporaryFile(suffix=".fgb", delete=False)
    f.write(fgb)
    f.close()
    gdf = gpd.read_file(f.name)
    os.unlink(f.name)
    assert set(gdf["site_no"]) == {"01646500", "01638500"}


def test_both_services_empty_raises_no_stations(spec, monkeypatch):
    params = {"bbox": [-77.9, 38.9, -77.0, 39.1]}
    params.update(hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, dict(params)))
    monkeypatch.setattr(http_json, "_get", lambda _s, _p: b"")
    with pytest.raises(Exception) as ei:
        http_json.execute(spec, params)
    assert ei.value.error_code == "NWIS_GAUGES_NO_STATIONS"
    assert ei.value.retryable is False


@pytest.mark.parametrize("body", ["", "  ", "No sites found matching the bBox"])
def test_a_404_over_a_box_holding_no_gauge_is_an_empty_record(spec, body):
    typed = nwis_hooks.classify_status(spec, 404, body)
    assert typed.error_code == "NWIS_GAUGES_NO_STATIONS"
    assert typed.retryable is False


@pytest.mark.parametrize("status,body", [
    (503, ""), (500, "gateway down"), (None, None), (404, "<html>bad query</html>")])
def test_a_real_failure_stays_an_upstream_error(spec, status, body):
    assert nwis_hooks.classify_status(spec, status, body) is None


def test_a_transport_404_carries_its_own_non_retryable_verdict(spec, monkeypatch):
    def _raise(_plan):
        raise terrors.TransportNotFound("object not found (HTTP 404)", body=None)

    monkeypatch.setattr(http_json, "_get_raw", _raise)
    bare = registration.get_spec("fetch_noaa_coops_tides")
    with pytest.raises(Exception) as ei:
        http_json._get(bare, hooks.RequestPlan(url="https://example.invalid/x"))
    assert ei.value.error_code.endswith("_UPSTREAM_ERROR")
    assert ei.value.retryable is False
