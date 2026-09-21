"""The gauge network read through dataretrieval, offline over synthetic frames.

The readings and the expanded site record are joined by the delegate - the
readings win and the site record decorates them with the zero a gage height is
counted from - an all-empty result answers an honest no-stations, and the window
mode switches the output schema between the instantaneous and hydrograph shapes.
Plus the spatial-selector and temporal-window edge matrix."""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest
from dataretrieval.exceptions import HTTPError, ServiceUnavailable

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


def _stamp(hour):
    return dt.datetime(2024, 1, 1, hour, tzinfo=dt.timezone.utc)


def _iv_frame(rows):
    """One long IV frame: a datetime index, ``site_no`` and one column per code."""
    index = [_stamp(hour) for _site, hour, _vals in rows]
    data = {"site_no": [site for site, _hour, _vals in rows]}
    for code in ("00060", "00065", "00010"):
        if any(code in vals for _s, _h, vals in rows):
            data[code] = [vals.get(code) for _s, _h, vals in rows]
    return pd.DataFrame(data, index=pd.DatetimeIndex(index))


_SITE_FRAME = pd.DataFrame([
    {"site_no": "01646500", "station_nm": "POTOMAC RIVER", "dec_lat_va": 38.95,
     "dec_long_va": -77.12, "alt_va": 37.20, "alt_datum_cd": "NAVD88"},
    {"site_no": "01638500", "station_nm": "SHENANDOAH", "dec_lat_va": 39.02,
     "dec_long_va": -77.80, "alt_va": None, "alt_datum_cd": None},
])


@pytest.fixture
def nwis_calls(monkeypatch):
    """Stand in for both dataretrieval calls, recording the kwargs each got."""
    import dataretrieval.nwis as nwis

    calls = {"iv": None, "info": None, "iv_df": _iv_frame([]), "site_df": _SITE_FRAME}

    def _get_iv(**kwargs):
        calls["iv"] = kwargs
        return calls["iv_df"], None

    def _get_info(**kwargs):
        calls["info"] = kwargs
        return calls["site_df"], None

    monkeypatch.setattr(nwis, "get_iv", _get_iv)
    monkeypatch.setattr(nwis, "get_info", _get_info)
    return calls


def _read(spec, calls, **params):
    return nwis_hooks.read(spec, {"_mode": "instantaneous", **params}, timeout_s=5.0)


def test_nwis_registered_and_spec_served(spec):
    from trid3nt_server.tools import TOOL_REGISTRY

    assert "fetch_usgs_nwis_gauges" in TOOL_REGISTRY
    assert "_promoted" in TOOL_REGISTRY["fetch_usgs_nwis_gauges"].module
    assert spec.error_code_prefix == "NWIS_GAUGES"
    assert spec.empty_error_suffix == "NO_STATIONS"


def test_nwis_hooks_registered():
    for h in ("usgs_nwis.resolve", "usgs_nwis.read"):
        assert h in hooks.HOOK_REGISTRY


def test_the_read_carries_the_gauge_s_own_zero(spec, nwis_calls):
    nwis_calls["iv_df"] = _iv_frame([
        ("01646500", 12, {"00060": 1200.0, "00065": 3.4}),
        ("01638500", 12, {"00060": 800.0, "00065": None}),
    ])
    feats = _read(spec, nwis_calls, bbox=[-77.9, 38.9, -77.0, 39.1])
    assert len(feats) == 2
    assert set(feats[0]["properties"]) == {
        "site_no", "site_name", "discharge_cfs", "gage_height_ft", "water_temp_c",
        "reading_dt", "gauge_datum_ft", "vertical_datum"}
    by = {f["properties"]["site_no"]: f["properties"] for f in feats}
    assert by["01646500"]["discharge_cfs"] == 1200.0 and by["01646500"]["gage_height_ft"] == 3.4
    assert by["01646500"]["gauge_datum_ft"] == 37.20
    assert by["01646500"]["vertical_datum"] == "NAVD88"
    # a site that publishes no zero states none rather than a guessed one
    assert by["01638500"]["gauge_datum_ft"] is None


def test_the_window_read_publishes_the_stage_series_beside_the_discharge(spec, nwis_calls):
    nwis_calls["iv_df"] = _iv_frame([
        ("01646500", 0, {"00060": 1000.0, "00065": 3.1}),
        ("01646500", 1, {"00060": 1100.0, "00065": 3.4}),
        ("01646500", 2, {"00060": 1200.0, "00065": 3.9}),
    ])
    feats = _read(spec, nwis_calls, bbox=[-77.9, 38.9, -77.0, 39.1], _mode="hydrograph")
    p = feats[0]["properties"]
    assert set(p) == {"site_no", "site_name", "discharge_cfs", "gage_height_ft", "water_temp_c",
                      "reading_dt", "time_series_csv", "stage_series_csv", "temp_series_csv",
                      "time_start", "time_end", "n_timesteps", "discharge_min_cfs",
                      "discharge_max_cfs", "discharge_mean_cfs",
                      "gauge_datum_ft", "vertical_datum"}
    assert p["n_timesteps"] == 3 and p["discharge_min_cfs"] == 1000.0 and p["discharge_max_cfs"] == 1200.0
    assert p["time_series_csv"].startswith("2024-01-01T00:00:00+0000,1000.000000")
    assert p["stage_series_csv"].startswith("2024-01-01T00:00:00+0000,3.100000")
    assert p["gauge_datum_ft"] == 37.20 and p["vertical_datum"] == "NAVD88"


def test_the_temperature_code_is_read_into_its_own_column(spec, nwis_calls):
    """The third coverage row is asked by the parameter code NWIS answers to,
    and the reading lands in the column that row names rather than in the
    discharge one it is not measured in."""
    nwis_calls["iv_df"] = _iv_frame([("01646500", 3, {"00010": 18.4})])
    p = _read(spec, nwis_calls, bbox=[-77.9, 38.9, -77.0, 39.1],
              parameter="00010")[0]["properties"]
    assert p["water_temp_c"] == 18.4
    assert p["discharge_cfs"] is None and p["gage_height_ft"] is None
    assert p["reading_dt"] == "2024-01-01T03:00:00+0000"


def test_the_temperature_window_is_its_own_series_column(spec, nwis_calls):
    nwis_calls["iv_df"] = _iv_frame([
        ("01646500", 0, {"00010": 18.7}), ("01646500", 1, {"00010": 18.6})])
    p = _read(spec, nwis_calls, bbox=[-77.9, 38.9, -77.0, 39.1],
              parameter="00010", _mode="hydrograph")[0]["properties"]
    assert p["temp_series_csv"].startswith("2024-01-01T00:00:00+0000,18.700000")
    assert p["water_temp_c"] == 18.6
    assert p["time_series_csv"] == "" and p["n_timesteps"] == 0


def test_a_parameter_this_source_reads_into_no_column_refuses(spec):
    assert _resolve_err(spec, {"bbox": [-122.7, 45.4, -122.6, 45.6],
                               "parameter": "00095"}) == "NWIS_GAUGES_INPUT_ERROR"


def test_the_asked_parameter_is_what_both_services_are_called_with(spec, nwis_calls):
    params = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](
        spec, {"bbox": [-122.7, 45.4, -122.6, 45.6], "parameter": "00010"})
    assert params["parameter"] == "00010"
    _read(spec, nwis_calls, **params)
    assert nwis_calls["iv"]["parameterCd"] == "00010"
    assert nwis_calls["info"]["parameterCd"] == "00010"
    assert nwis_calls["info"]["siteOutput"] == "expanded"
    pair = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](
        spec, {"bbox": [-122.7, 45.4, -122.6, 45.6]})
    _read(spec, nwis_calls, **pair)
    assert nwis_calls["iv"]["parameterCd"] == "00060,00065"


def test_the_bbox_selector_is_the_one_the_library_is_called_with(spec, nwis_calls):
    params = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, {"bbox": [-82.4, 26.3, -81.6, 26.9]})
    _read(spec, nwis_calls, **params)
    assert nwis_calls["iv"]["bBox"] == "-82.4,26.3,-81.6,26.9"
    assert "stateCd" not in nwis_calls["iv"]


def test_the_state_selector_replaces_the_box(spec, nwis_calls):
    params = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](spec, {"state_code": "WA"})
    _read(spec, nwis_calls, **params)
    assert nwis_calls["iv"]["stateCd"] == "WA" and "bBox" not in nwis_calls["iv"]


def test_the_window_reaches_the_library_as_the_form_it_was_asked_in(spec, nwis_calls):
    period = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](
        spec, {"bbox": [-82.4, 26.3, -81.6, 26.9], "period": "P7D"})
    _read(spec, nwis_calls, **period)
    assert nwis_calls["iv"]["period"] == "P7D"
    dates = hooks.HOOK_REGISTRY["usgs_nwis.resolve"](
        spec, {"bbox": [-82.4, 26.3, -81.6, 26.9],
               "start_date": "2024-01-01", "end_date": "2024-01-05"})
    _read(spec, nwis_calls, **dates)
    assert nwis_calls["iv"]["start"] == "2024-01-01" and nwis_calls["iv"]["end"] == "2024-01-05"


def test_empty_readings_degrade_to_the_station_locations(spec, nwis_calls):
    feats = _read(spec, nwis_calls, bbox=[-77.9, 38.9, -77.0, 39.1])
    assert {f["properties"]["site_no"] for f in feats} == {"01646500", "01638500"}
    assert all(f["properties"]["discharge_cfs"] is None for f in feats)


def test_both_services_empty_raises_no_stations(spec, nwis_calls):
    nwis_calls["site_df"] = _SITE_FRAME.iloc[0:0]
    with pytest.raises(Exception) as ei:
        _read(spec, nwis_calls, bbox=[-77.9, 38.9, -77.0, 39.1])
    assert ei.value.error_code == "NWIS_GAUGES_NO_STATIONS"
    assert ei.value.retryable is False


def test_a_reading_with_no_site_location_has_nowhere_to_be_a_point(spec, nwis_calls):
    nwis_calls["iv_df"] = _iv_frame([("09999999", 12, {"00060": 5.0})])
    with pytest.raises(Exception) as ei:
        _read(spec, nwis_calls, bbox=[-77.9, 38.9, -77.0, 39.1])
    assert ei.value.error_code == "NWIS_GAUGES_NO_STATIONS"


def test_a_rejected_request_is_an_input_error_not_an_upstream_one(spec, monkeypatch):
    import dataretrieval.nwis as nwis

    def _reject(**_kwargs):
        raise HTTPError("bad bBox", status_code=400)

    monkeypatch.setattr(nwis, "get_iv", _reject)
    with pytest.raises(Exception) as ei:
        nwis_hooks.read(spec, {"bbox": [-77.9, 38.9, -77.0, 39.1]}, timeout_s=5.0)
    assert ei.value.error_code == "NWIS_GAUGES_INPUT_ERROR"


def test_a_service_failure_stays_an_upstream_error(spec, monkeypatch):
    import dataretrieval.nwis as nwis

    def _down(**_kwargs):
        raise ServiceUnavailable("gateway down", status_code=503)

    monkeypatch.setattr(nwis, "get_iv", _down)
    with pytest.raises(Exception) as ei:
        nwis_hooks.read(spec, {"bbox": [-77.9, 38.9, -77.0, 39.1]}, timeout_s=5.0)
    assert ei.value.error_code == "NWIS_GAUGES_UPSTREAM_ERROR"


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



def test_a_transport_404_carries_its_own_non_retryable_verdict(spec, monkeypatch):
    def _raise(_plan):
        raise terrors.TransportNotFound("object not found (HTTP 404)", body=None)

    monkeypatch.setattr(http_json, "_get_raw", _raise)
    bare = registration.get_spec("fetch_noaa_coops_tides")
    with pytest.raises(Exception) as ei:
        http_json._get(bare, hooks.RequestPlan(url="https://example.invalid/x"))
    assert ei.value.error_code.endswith("_UPSTREAM_ERROR")
    assert ei.value.retryable is False
