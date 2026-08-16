"""Offline tests for the tier-3 hook contract (ADR 0056), no live calls.

Covers the registry (resolve / duplicate / spec-load validation), each source's
build_request URL construction + bespoke input validation, each parse_response
field extraction + honest-empty / too-large typed errors, and the http_json
executor end-to-end (multi-request join + paging) with the transport
monkeypatched. Migrates the value-bearing coverage from the three deleted twins'
test files (parse-field, window/year validation, honest-empty, join, paging).
"""

from __future__ import annotations

import json

import geopandas as gpd
import pytest

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.data.fetchers._router import registration as reg
from trid3nt_server.data.fetchers._router.errors import (
    RouterEmptyError,
    RouterInputError,
    RouterUpstreamError,
)
from trid3nt_server.data.fetchers._router import router as _router_mod
from trid3nt_server.data.fetchers._router.executors import http_json, vector_fgb
from trid3nt_server.data.fetchers._router.hooks import (
    HOOK_REGISTRY,
    HookResolutionError,
    RequestPlan,
    has_hook,
    register_hook,
    resolve_hook,
)
from trid3nt_server.data.fetchers._router.spec import compose_specs_from_tree

_SPECS = compose_specs_from_tree()


def _spec(name: str) -> SourceSpec:
    return _SPECS[name]


def _fgb_records(feats, spec) -> gpd.GeoDataFrame:
    import os
    import tempfile

    data = vector_fgb.features_to_fgb_bytes(feats, spec, {})
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(data)
        p = f.name
    try:
        return gpd.read_file(p)
    finally:
        os.unlink(p)


# --------------------------------------------------------------------------- #
# Registry.
# --------------------------------------------------------------------------- #


def test_all_hooks_registered():
    for name in (
        "usgs_earthquakes.build_request", "usgs_earthquakes.parse_response",
        "ncei_tsunami.build_request", "ncei_tsunami.parse_response",
        "usgs_volcano.build_request", "usgs_volcano.parse_response",
        "nws_event.build_request", "nws_event.parse_response",
        "usace_nsi.build_request", "usace_nsi.parse_response",
    ):
        assert has_hook(name) and callable(resolve_hook(name))


def test_resolve_unknown_raises():
    with pytest.raises(HookResolutionError):
        resolve_hook("nope.nope")


def test_duplicate_registration_raises():
    with pytest.raises(HookResolutionError):
        register_hook("usgs_earthquakes.build_request")(lambda *a: None)


def test_register_spec_rejects_unknown_hook():
    bad = _spec("fetch_usgs_earthquakes").model_copy(
        update={"hooks": _spec("fetch_usgs_earthquakes").hooks.model_copy(update={"build_request": "ghost.hook"})}
    )
    with pytest.raises(HookResolutionError):
        reg._validate_hooks(bad)


# --------------------------------------------------------------------------- #
# earthquakes.
# --------------------------------------------------------------------------- #


def test_eq_build_request_url():
    spec = _spec("fetch_usgs_earthquakes")
    br = resolve_hook(spec.hooks.build_request)
    plans = br(spec, {"bbox": [-122.5, 37.0, -120.0, 39.0], "start_date": "2019-07-04", "end_date": "2019-07-07", "min_magnitude": 4.5})
    assert isinstance(plans, list) and len(plans) == 1
    u = plans[0].url
    assert "format=geojson" in u and "limit=20000" in u and "minmagnitude=4.5" in u
    assert "minlongitude=-122.5" in u and "maxlatitude=39.0" in u
    # A bare ISO date parses via datetime.fromisoformat to 00:00:00 (py3.11+) --
    # the exact twin behavior (its except-branch end-of-day path is unreached).
    assert "starttime=2019-07-04T00%3A00%3A00" in u and "endtime=2019-07-07T00%3A00%3A00" in u


def test_eq_build_request_default_window_and_global():
    spec = _spec("fetch_usgs_earthquakes")
    br = resolve_hook(spec.hooks.build_request)
    plans = br(spec, {"min_magnitude": 2.5})  # no bbox, no dates
    u = plans[0].url
    assert "minlongitude" not in u and "starttime=" in u and "endtime=" in u


@pytest.mark.parametrize("kw,code", [
    ({"start_date": "2020-05-01", "end_date": "2020-01-01"}, "USGS_EARTHQUAKES_INPUT_ERROR"),
    ({"start_date": "2019-01-01", "end_date": "2021-01-01"}, "USGS_EARTHQUAKES_INPUT_ERROR"),  # >366d
    ({"start_date": "not-a-date"}, "USGS_EARTHQUAKES_INPUT_ERROR"),
    ({"min_magnitude": 99.0}, "USGS_EARTHQUAKES_INPUT_ERROR"),
])
def test_eq_build_request_input_errors(kw, code):
    spec = _spec("fetch_usgs_earthquakes")
    br = resolve_hook(spec.hooks.build_request)
    with pytest.raises(RouterInputError) as ei:
        br(spec, kw)
    assert ei.value.error_code == code


def test_eq_parse_extracts_fields_and_serializes():
    spec = _spec("fetch_usgs_earthquakes")
    pr = resolve_hook(spec.hooks.parse_response)
    body = json.dumps({"type": "FeatureCollection", "metadata": {"count": 1}, "features": [
        {"id": "nc1", "geometry": {"type": "Point", "coordinates": [-121.0, 38.0, 5.2]},
         "properties": {"mag": 4.1, "magType": "mw", "place": "X", "time": 1600000000000,
                        "tsunami": 0, "felt": 3, "sig": 100, "net": "nc", "status": "reviewed",
                        "type": "earthquake", "url": "http://u"}},
    ]}).encode()
    feats = pr(spec, {}, [body])
    p = feats[0]["properties"]
    assert feats[0]["geometry"]["coordinates"] == [-121.0, 38.0]
    assert p["depth_km"] == 5.2 and p["id"] == "nc1" and p["time"] == "2020-09-13T12:26:40Z"
    g = _fgb_records(feats, spec)
    assert list(g.columns)[:-1] == spec.ingest["properties"]


def test_eq_parse_empty_and_too_large():
    spec = _spec("fetch_usgs_earthquakes")
    pr = resolve_hook(spec.hooks.parse_response)
    with pytest.raises(RouterEmptyError) as ee:
        pr(spec, {}, [json.dumps({"type": "FeatureCollection", "features": []}).encode()])
    assert ee.value.error_code == "USGS_EARTHQUAKES_NO_EVENTS"
    with pytest.raises(RouterInputError) as te:
        pr(spec, {}, [json.dumps({"type": "FeatureCollection", "metadata": {"count": 20001}, "features": []}).encode()])
    assert te.value.error_code == "USGS_EARTHQUAKES_RESULT_TOO_LARGE"


def test_eq_parse_rejects_non_featurecollection():
    spec = _spec("fetch_usgs_earthquakes")
    pr = resolve_hook(spec.hooks.parse_response)
    with pytest.raises(RouterUpstreamError):
        pr(spec, {}, [json.dumps({"type": "NotAFC"}).encode()])


# --------------------------------------------------------------------------- #
# tsunami.
# --------------------------------------------------------------------------- #


def test_tsu_build_request_mode_page_bbox():
    spec = _spec("fetch_tsunami_events")
    br = resolve_hook(spec.hooks.build_request)
    plans = br(spec, {"bbox": [135, 30, 150, 45], "min_year": 1900, "max_year": 2024, "observation_type": "runups", "page": 3})
    u = plans[0].url
    assert "/runups?" in u and "page=3" in u and "minLatitude=30" in u and "itemsPerPage=200" in u


@pytest.mark.parametrize("kw,code", [
    ({"min_year": 2020, "max_year": 1990}, "TSUNAMI_EVENTS_INPUT_ERROR"),
    ({"min_year": 9999}, "TSUNAMI_EVENTS_INPUT_ERROR"),
    ({"observation_type": "garbage"}, "TSUNAMI_EVENTS_INPUT_ERROR"),
])
def test_tsu_build_request_input_errors(kw, code):
    spec = _spec("fetch_tsunami_events")
    br = resolve_hook(spec.hooks.build_request)
    with pytest.raises(RouterInputError) as ei:
        br(spec, kw)
    assert ei.value.error_code == code


def test_tsu_parse_events_and_runups():
    spec = _spec("fetch_tsunami_events")
    pr = resolve_hook(spec.hooks.parse_response)
    ev = json.dumps({"items": [{"id": 7, "latitude": 38.3, "longitude": 142.4, "year": 2011,
        "causeCode": 1, "eqMagnitude": 9.1, "maxWaterHeight": 38.9, "deathsTotal": 18000, "numRunups": 5}]}).encode()
    feats = pr(spec, {"observation_type": "events"}, [ev])
    p = feats[0]["properties"]
    assert p["cause"] == "Earthquake" and p["max_water_height"] == 38.9 and p["deaths"] == 18000 and p["observation_type"] == "event"
    ru = json.dumps({"items": [{"id": 8, "latitude": 38.3, "longitude": 142.4, "year": 2011,
        "sourceCauseCode": 1, "sourceEqMagnitude": 9.1, "runupHt": 9.3, "distFromSource": 120.0}]}).encode()
    fr = pr(spec, {"observation_type": "runups"}, [ru])[0]["properties"]
    assert fr["max_water_height"] == 9.3 and fr["dist_from_source_km"] == 120.0 and fr["observation_type"] == "runup"


def test_tsu_parse_empty_raises_no_events():
    spec = _spec("fetch_tsunami_events")
    pr = resolve_hook(spec.hooks.parse_response)
    with pytest.raises(RouterEmptyError) as ee:
        pr(spec, {"observation_type": "events"}, [json.dumps({"items": []}).encode()])
    assert ee.value.error_code == "TSUNAMI_EVENTS_NO_EVENTS"


def test_tsu_paging_loop_and_too_large(monkeypatch):
    spec = _spec("fetch_tsunami_events")
    # 2-page response; page 1 declares totalPages=2. Executor concatenates.
    pages = {
        1: json.dumps({"totalItems": 2, "totalPages": 2, "items": [{"id": 1, "latitude": 38.0, "longitude": 142.0, "year": 2011, "causeCode": 1}]}).encode(),
        2: json.dumps({"totalItems": 2, "totalPages": 2, "items": [{"id": 2, "latitude": 38.1, "longitude": 142.1, "year": 2011, "causeCode": 1}]}).encode(),
    }
    def fake_get(spec_, plan):
        pg = int(dict(x.split("=") for x in plan.url.split("?")[1].split("&"))["page"])
        return pages[pg]
    monkeypatch.setattr(http_json, "_get", fake_get)
    bodies = http_json.fetch_bodies(spec, {"observation_type": "events", "min_year": 2011, "max_year": 2011})
    assert len(bodies) == 2
    feats = resolve_hook(spec.hooks.parse_response)(spec, {"observation_type": "events"}, bodies)
    assert len(feats) == 2
    # too-large: page 1 reports totalPages beyond the cap.
    big = json.dumps({"totalItems": 99999, "totalPages": 99, "items": [{"id": 1, "latitude": 1, "longitude": 1, "year": 2011, "causeCode": 1}]}).encode()
    monkeypatch.setattr(http_json, "_get", lambda s, p: big)
    with pytest.raises(RouterInputError) as te:
        http_json.fetch_bodies(spec, {"observation_type": "events"})
    assert te.value.error_code == "TSUNAMI_EVENTS_RESULT_TOO_LARGE"


# --------------------------------------------------------------------------- #
# volcano (multi-request join via the executor).
# --------------------------------------------------------------------------- #


def test_vol_build_request_two_endpoints():
    spec = _spec("fetch_usgs_volcano_alerts")
    plans = resolve_hook(spec.hooks.build_request)(spec, {})
    assert len(plans) == 2 and "getMonitoredVolcanoes" in plans[0].url and "getUSVolcanoes" in plans[1].url


def test_vol_execute_joins_and_serializes(monkeypatch):
    spec = _spec("fetch_usgs_volcano_alerts")
    alerts = json.dumps([{"vnum": "332010", "volcano_name": "Kilauea", "alert_level": "watch", "color_code": "orange", "obs_abbr": "hvo"}]).encode()
    coords = json.dumps([{"vnum": "332010", "volcano_name": "Kilauea", "latitude": 19.42, "longitude": -155.29, "elevation_meters": 1222, "region": "Hawaii"}]).encode()
    seq = iter([alerts, coords])
    monkeypatch.setattr(http_json, "_get", lambda s, p: next(seq))
    data = http_json.execute(spec, {})
    import os
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(data)
        p = f.name
    try:
        g = gpd.read_file(p)
    finally:
        os.unlink(p)
    assert len(g) == 1 and g.iloc[0]["alert_level"] == "WATCH" and int(g.iloc[0]["alert_rank"]) == 2


@pytest.mark.parametrize("bodies,params", [
    ([b"[]", b"[]"], {}),  # empty monitored
    ([json.dumps([{"vnum": "1", "alert_level": "watch"}]).encode(), b"[]"], {}),  # no coord join
])
def test_vol_parse_honest_empty(bodies, params):
    spec = _spec("fetch_usgs_volcano_alerts")
    with pytest.raises(RouterEmptyError) as ee:
        resolve_hook(spec.hooks.parse_response)(spec, params, bodies)
    assert ee.value.error_code == "USGS_VOLCANO_ALERTS_NO_VOLCANOES"


def test_vol_parse_bbox_filter_excludes():
    spec = _spec("fetch_usgs_volcano_alerts")
    alerts = json.dumps([{"vnum": "332010", "alert_level": "watch", "color_code": "orange"}]).encode()
    coords = json.dumps([{"vnum": "332010", "latitude": 19.42, "longitude": -155.29}]).encode()
    with pytest.raises(RouterEmptyError):
        resolve_hook(spec.hooks.parse_response)(spec, {"bbox": [-100, 40, -90, 45]}, [alerts, coords])


# --------------------------------------------------------------------------- #
# nws_event (single-GET NWS /alerts/active; migrated from test_fetch_nws_event).
# --------------------------------------------------------------------------- #


def test_nws_build_request_url_state_and_events():
    spec = _spec("fetch_nws_event")
    br = resolve_hook(spec.hooks.build_request)
    vp = _router_mod.validate_params(spec, {"area": "FL", "event_types": ["Flood Warning", "Coastal Flood Watch"]})
    u = br(spec, vp)[0].url
    assert "/alerts/active?" in u and "area=FL" in u and "status=actual" in u and "message_type=alert" in u
    # str_list sorted + repeated &event= per entry.
    assert "event=Coastal%20Flood%20Watch" in u and "event=Flood%20Warning" in u


def test_nws_build_request_area_name_canonicalizes():
    spec = _spec("fetch_nws_event")
    br = resolve_hook(spec.hooks.build_request)
    vp = _router_mod.validate_params(spec, {"area": "Texas"})
    assert "area=TX" in br(spec, vp)[0].url


@pytest.mark.parametrize("kw,ok", [
    ({"area": "Nowhereland"}, False),   # unrecognized area -> INPUT_INVALID (build hook)
    ({"area": "12071"}, True),          # a FIPS builds a URL (NWS rejects it live -- twin defect)
])
def test_nws_build_request_area_validation(kw, ok):
    spec = _spec("fetch_nws_event")
    br = resolve_hook(spec.hooks.build_request)
    vp = _router_mod.validate_params(spec, kw)
    if ok:
        assert br(spec, vp)[0].url
    else:
        with pytest.raises(RouterInputError) as ei:
            br(spec, vp)
        assert ei.value.error_code == "NWS_EVENT_INPUT_INVALID"


def test_nws_bad_status_enum_input_invalid():
    spec = _spec("fetch_nws_event")
    with pytest.raises(RouterInputError) as ei:
        _router_mod.validate_params(spec, {"area": "FL", "status": "bogus"})
    assert ei.value.error_code == "NWS_EVENT_INPUT_INVALID"


def test_nws_parse_projects_props_and_drops_null_geometry():
    spec = _spec("fetch_nws_event")
    pr = resolve_hook(spec.hooks.parse_response)
    body = json.dumps({"type": "FeatureCollection", "features": [
        {"geometry": {"type": "Polygon", "coordinates": [[[-81.0, 26.0], [-80.0, 26.0], [-80.0, 27.0], [-81.0, 26.0]]]},
         "properties": {"event": "Flood Warning", "severity": "Severe", "id": "urn:x:1",
                        "geocode": {"UGC": ["FLZ048"]}}},
        {"geometry": None, "properties": {"event": "Winter Storm Watch", "id": "urn:x:2"}},  # zone-only -> dropped
    ]}).encode()
    feats = pr(spec, {}, [body])
    assert len(feats) == 1 and feats[0]["properties"]["event"] == "Flood Warning"
    # nested props JSON-coerced to a scalar string.
    assert isinstance(feats[0]["properties"]["id"], str)
    g = _fgb_records(feats, spec)
    assert list(g.columns)[:-1] == spec.ingest["properties"]


def test_nws_parse_empty_and_bad_body():
    spec = _spec("fetch_nws_event")
    pr = resolve_hook(spec.hooks.parse_response)
    assert pr(spec, {}, [json.dumps({"type": "FeatureCollection", "features": []}).encode()]) == []
    with pytest.raises(RouterUpstreamError) as ue:
        pr(spec, {}, [json.dumps({"type": "NotAFC"}).encode()])
    assert ue.value.error_code == "NWS_EVENT_UPSTREAM_ERROR"


# --------------------------------------------------------------------------- #
# usace_nsi (single-POST NSI structures; migrated from test_fetch_usace_nsi).
# --------------------------------------------------------------------------- #


def test_nsi_build_request_post_body():
    spec = _spec("fetch_usace_nsi")
    br = resolve_hook(spec.hooks.build_request)
    vp = _router_mod.validate_params(spec, {"bbox": [-81.88, 26.62, -81.86, 26.66]})
    plan = br(spec, vp)[0]
    assert plan.method == "POST" and plan.params == {"fmt": "fc"}
    assert plan.json_body["type"] == "FeatureCollection"
    ring = plan.json_body["features"][0]["geometry"]["coordinates"][0]
    assert ring[0] == [-81.88, 26.62] and ring[-1] == ring[0] and len(ring) == 5


def test_nsi_build_request_oversized_bbox_input_invalid():
    spec = _spec("fetch_usace_nsi")
    br = resolve_hook(spec.hooks.build_request)
    vp = _router_mod.validate_params(spec, {"bbox": [-100.0, 30.0, -97.0, 33.0]})  # >1 deg/axis
    with pytest.raises(RouterInputError) as ei:
        br(spec, vp)
    assert ei.value.error_code == "USACE_NSI_INPUT_INVALID"


def test_nsi_parse_projects_and_derives_pelicun_cols():
    spec = _spec("fetch_usace_nsi")
    pr = resolve_hook(spec.hooks.parse_response)
    body = json.dumps({"type": "FeatureCollection", "features": [
        {"geometry": {"type": "Point", "coordinates": [-81.87, 26.63]},
         "properties": {"fd_id": 7, "occtype": "RES1", "val_struct": 250000.0, "st_damcat": "RES"}},
        {"geometry": None, "properties": {"fd_id": 8, "occtype": "COM1"}},  # dropped
    ]}).encode()
    feats = pr(spec, {}, [body])
    assert len(feats) == 1
    p = feats[0]["properties"]
    assert p["component_type"] == "RES1" and p["replacement_value"] == 250000.0 and p["fd_id"] == 7
    g = _fgb_records(feats, spec)
    assert list(g.columns)[:-1] == spec.ingest["properties"]


def test_nsi_parse_message_error_and_empty():
    spec = _spec("fetch_usace_nsi")
    pr = resolve_hook(spec.hooks.parse_response)
    with pytest.raises(RouterUpstreamError) as ue:
        pr(spec, {}, [json.dumps({"message": "boom"}).encode()])
    assert ue.value.error_code == "USACE_NSI_UPSTREAM_ERROR"
    assert pr(spec, {}, [json.dumps({"type": "FeatureCollection", "features": []}).encode()]) == []


def test_nsi_post_transport_executor_end_to_end(monkeypatch):
    """The POST plan flows through http_json.execute (transport monkeypatched)."""
    spec = _spec("fetch_usace_nsi")
    fc = json.dumps({"type": "FeatureCollection", "features": [
        {"geometry": {"type": "Point", "coordinates": [-81.87, 26.63]},
         "properties": {"fd_id": 1, "occtype": "RES1", "val_struct": 100000.0}},
    ]}).encode()
    monkeypatch.setattr(http_json, "_get", lambda s, p: fc)
    data = http_json.execute(spec, _router_mod.validate_params(spec, {"bbox": [-81.88, 26.62, -81.86, 26.66]}))
    import os
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(data)
        pth = f.name
    try:
        gdf = gpd.read_file(pth)
    finally:
        os.unlink(pth)
    assert len(gdf) == 1 and gdf.iloc[0]["component_type"] == "RES1"
