"""Router NWIS fold (ADR 0085): the last flood-seam twin, spec-driven.

Twin (fetch_usgs_nwis_gauges.py) DELETED. Covers the two blockers the fold resolved:
the IV WaterML-JSON -> Site-RDB parse_fallback chain (honest NO_STATIONS on all-empty)
and the window-mode output-schema switch (5-col instantaneous vs 12-col hydrograph),
plus the spatial-selector + temporal-window resolution edge matrix -- all offline with
synthetic payloads. (Live end-to-end site-set + schema parity vs the twin was verified
against real USGS at fold time; this suite is the offline regression surface.)
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.fetchers._router import hooks, registration
from trid3nt_server.tools.fetchers._router.executors import http_json


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


def _iv_window_body():
    samples = [("2024-01-01T00:00:00Z", 1000.0), ("2024-01-01T01:00:00Z", 1100.0), ("2024-01-01T02:00:00Z", 1200.0)]
    return json.dumps({"value": {"timeSeries": [{
        "sourceInfo": {"siteCode": [{"value": "01646500"}], "siteName": "POTOMAC",
                       "geoLocation": {"geogLocation": {"latitude": 38.95, "longitude": -77.12}}},
        "variable": {"variableCode": [{"value": "00060"}]},
        "values": [{"value": [{"value": str(v), "dateTime": d} for d, v in samples]}]}]}}).encode()


_SITE_RDB = (
    "# comment\n"
    "agency_cd\tsite_no\tstation_nm\tdec_lat_va\tdec_long_va\n"
    "5s\t15s\t50s\t16s\t16s\n"
    "USGS\t01646500\tPOTOMAC RIVER\t38.95\t-77.12\n"
    "USGS\t01638500\tSHENANDOAH\t39.02\t-77.80\n"
).encode()


# --------------------------------------------------------------------------- #
# Registration + hooks.
# --------------------------------------------------------------------------- #


def test_nwis_registered_and_spec_served(spec):
    from trid3nt_server.tools import TOOL_REGISTRY

    assert "fetch_usgs_nwis_gauges" in TOOL_REGISTRY
    assert "_promoted" in TOOL_REGISTRY["fetch_usgs_nwis_gauges"].module
    assert spec.error_code_prefix == "NWIS_GAUGES"
    assert spec.empty_error_suffix == "NO_STATIONS"


def test_nwis_hooks_registered():
    for h in ("usgs_nwis.resolve", "usgs_nwis.build_request", "usgs_nwis.parse"):
        assert h in hooks.HOOK_REGISTRY


# --------------------------------------------------------------------------- #
# Parse: self-detecting IV / IV-window / Site + per-mode schema.
# --------------------------------------------------------------------------- #


def test_parse_iv_instantaneous_5col(spec):
    feats = hooks.HOOK_REGISTRY["usgs_nwis.parse"](spec, {"_mode": "instantaneous"}, [_iv_body()])
    assert len(feats) == 2  # two distinct sites merged over discharge + gage
    assert set(feats[0]["properties"]) == {"site_no", "site_name", "discharge_cfs", "gage_height_ft", "reading_dt"}
    by = {f["properties"]["site_no"]: f["properties"] for f in feats}
    assert by["01646500"]["discharge_cfs"] == 1200.0 and by["01646500"]["gage_height_ft"] == 3.4


def test_parse_iv_window_12col(spec):
    feats = hooks.HOOK_REGISTRY["usgs_nwis.parse"](spec, {"_mode": "hydrograph"}, [_iv_window_body()])
    p = feats[0]["properties"]
    assert set(p) == {"site_no", "site_name", "discharge_cfs", "gage_height_ft", "reading_dt", "time_series_csv",
                      "time_start", "time_end", "n_timesteps", "discharge_min_cfs", "discharge_max_cfs", "discharge_mean_cfs"}
    assert p["n_timesteps"] == 3 and p["discharge_min_cfs"] == 1000.0 and p["discharge_max_cfs"] == 1200.0
    assert p["time_series_csv"].startswith("2024-01-01T00:00:00Z,1000.000000")


def test_parse_site_rdb(spec):
    feats = hooks.HOOK_REGISTRY["usgs_nwis.parse"](spec, {"_mode": "instantaneous"}, [_SITE_RDB])
    assert {f["properties"]["site_no"] for f in feats} == {"01646500", "01638500"}
    assert all(f["properties"]["discharge_cfs"] is None for f in feats)  # locations only


def test_parse_empty_body_returns_empty(spec):
    assert hooks.HOOK_REGISTRY["usgs_nwis.parse"](spec, {"_mode": "instantaneous"}, [b""]) == []


# --------------------------------------------------------------------------- #
# resolve(): selector + window edge matrix.
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# build_request plan ordering.
# --------------------------------------------------------------------------- #


def test_build_instantaneous_iv_then_site(spec):
    p = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, {"bbox": [-82.4, 26.3, -81.6, 26.9]})
    plans = hooks.HOOK_REGISTRY["usgs_nwis.build_request"](spec, {"bbox": [-82.4, 26.3, -81.6, 26.9], **p})
    assert len(plans) == 2 and plans[0].url.endswith("/iv/") and plans[1].url.endswith("/site/")
    assert plans[0].params["bBox"] == "-82.4,26.3,-81.6,26.9"


def test_build_hydrograph_iv_only(spec):
    p = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, {"bbox": [-82.4, 26.3, -81.6, 26.9], "period": "P7D"})
    plans = hooks.HOOK_REGISTRY["usgs_nwis.build_request"](spec, {"bbox": [-82.4, 26.3, -81.6, 26.9], **p})
    assert len(plans) == 1 and plans[0].params.get("period") == "P7D"


def test_build_state_selector(spec):
    p = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, {"state_code": "WA"})
    plans = hooks.HOOK_REGISTRY["usgs_nwis.build_request"](spec, {"state_code": "WA", **p})
    assert plans[0].params.get("stateCd") == "WA" and "bBox" not in plans[0].params


# --------------------------------------------------------------------------- #
# parse_fallback executor: IV-empty -> Site fallback; all-empty -> NO_STATIONS.
# --------------------------------------------------------------------------- #


def test_parse_fallback_iv_empty_uses_site(spec, monkeypatch):
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


def test_parse_fallback_all_empty_raises_no_stations(spec, monkeypatch):
    params = {"bbox": [-77.9, 38.9, -77.0, 39.1]}
    params.update(hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, dict(params)))
    monkeypatch.setattr(http_json, "_get", lambda _s, _p: b"")
    with pytest.raises(Exception) as ei:
        http_json.execute(spec, params)
    assert ei.value.error_code == "NWIS_GAUGES_NO_STATIONS"
    assert ei.value.retryable is False
