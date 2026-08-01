"""Offline tests for the chained-resolution mode (ADR 0063), no live calls.

Covers the mode primitives (resolve pre-step, offset paging, deduped/bounded/best-effort
detail enrichment) and each folded source's build/parse/resolve/enrich hooks with the
transport monkeypatched. Migrates the value-bearing coverage from the four deleted twins'
test files (parse-field, resolve gate, paging stop conditions, honest-empty / no-gauges /
too-large errors, zone-union enrichment, threshold/series enrichment).
"""

from __future__ import annotations

import json
import os
import tempfile

import geopandas as gpd
import pytest

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.agent.tools.fetchers._router import router as R
from trid3nt_server.agent.tools.fetchers._router.errors import RouterInputError, RouterUpstreamError
from trid3nt_server.agent.tools.fetchers._router.executors import chained_resolution as C
from trid3nt_server.agent.tools.fetchers._router.executors.chained_resolution import DetailResult
from trid3nt_server.agent.tools.fetchers._router.hooks import RequestPlan
from trid3nt_server.agent.tools.fetchers._router.spec import compose_specs_from_tree

_SPECS = compose_specs_from_tree()


def _spec(name: str) -> SourceSpec:
    return _SPECS[name]


def _run(name: str, raw: dict, url_map) -> gpd.GeoDataFrame:
    """Route the spec end-to-end with _get serving canned bytes by URL substring."""
    spec = _spec(name)

    def fake_get(_spec, plan):
        for key, body in url_map.items():
            if key in plan.url:
                if isinstance(body, Exception):
                    raise body
                return body if isinstance(body, bytes) else json.dumps(body).encode()
        raise AssertionError(f"no canned body for {plan.url}")

    orig = C._get
    C._get = fake_get
    try:
        params = R.validate_params(spec, dict(raw))
        if spec.hooks and spec.hooks.resolve_build:
            params = C.pre_resolve(spec, params)
        data = C.execute(spec, params)
    finally:
        C._get = orig
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(data)
        p = f.name
    try:
        return gpd.read_file(p)
    finally:
        os.unlink(p)


def _err(name: str, raw: dict, url_map):
    with pytest.raises(Exception) as ei:
        _run(name, raw, url_map)
    return ei.value


# --------------------------------------------------------------------------- #
# All four specs load + register their hooks.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", [
    "fetch_gbif_occurrences", "fetch_inaturalist_observations",
    "fetch_nws_alerts_conus", "fetch_nws_river_forecast",
])
def test_spec_loads_and_hooks_resolve(name):
    from trid3nt_server.agent.tools.fetchers._router.hooks import has_hook
    spec = _spec(name)
    assert spec.hooks is not None
    for pt in ("resolve_build", "resolve_parse", "build_request", "next_page", "enrich_plan", "enrich_merge"):
        hn = getattr(spec.hooks, pt)
        if hn:
            assert has_hook(hn), (name, pt, hn)


# --------------------------------------------------------------------------- #
# Mode primitive: deduped / bounded / best-effort detail fetch.
# --------------------------------------------------------------------------- #


def test_fetch_detail_set_dedup_cap_besteffort():
    spec = _spec("fetch_nws_alerts_conus")
    calls = []

    def fake_get(_spec, plan):
        calls.append(plan.url)
        if "boom" in plan.url:
            raise RouterUpstreamError("upstream")
        return b"{}"

    orig = C._get
    C._get = fake_get
    try:
        plans = [
            ("a", RequestPlan(url="http://x/a")),
            ("a", RequestPlan(url="http://x/a")),   # dup -> one fetch
            ("boom", RequestPlan(url="http://x/boom")),  # best-effort error, not raised
            ("b", RequestPlan(url="http://x/b")),
            ("c", RequestPlan(url="http://x/c")),   # over cap
        ]
        res = C.fetch_detail_set(spec, plans, cap=3)
    finally:
        C._get = orig
    assert calls.count("http://x/a") == 1              # deduped
    assert res["a"].body == b"{}" and res["a"].error is None
    assert res["boom"].error is not None                # honest error, kept
    assert res["c"].error == "detail-fetch cap reached"  # capped, not silent-dropped
    assert set(res) == {"a", "boom", "b", "c"}


# --------------------------------------------------------------------------- #
# GBIF: resolve gate + offset paging + bbox-clip.
# --------------------------------------------------------------------------- #

def _gbif_page(results, end):
    return {"results": results, "endOfRecords": end}

def _occ(lon, lat, gid=1):
    return {"decimalLongitude": lon, "decimalLatitude": lat, "gbifID": gid,
            "species": "Puma concolor", "eventDate": "2020", "basisOfRecord": "HUMAN_OBSERVATION"}


def test_gbif_taxonkey_fastpath_bbox_clip():
    gdf = _run("fetch_gbif_occurrences",
               {"species_key": "212", "bbox": [-82.0, 26.0, -81.0, 27.0], "max_records": 300},
               {"occurrence/search": _gbif_page([_occ(-81.5, 26.5, 1), _occ(-90.0, 26.5, 2)], True)})
    assert len(gdf) == 1  # the out-of-bbox point is clipped
    assert set(gdf.columns) >= {"gbifID", "species", "eventDate", "coordinateUncertaintyInMeters", "basisOfRecord"}


def test_gbif_name_resolve_exact_then_search():
    gdf = _run("fetch_gbif_occurrences",
               {"species_key": "Puma concolor", "bbox": [-82.0, 26.0, -81.0, 27.0], "max_records": 300},
               {"species/match": {"usageKey": 2435099, "matchType": "EXACT", "scientificName": "Puma concolor"},
                "occurrence/search": _gbif_page([_occ(-81.5, 26.5)], True)})
    assert len(gdf) == 1


def test_gbif_fuzzy_match_rejected():
    e = _err("fetch_gbif_occurrences",
             {"species_key": "Puma concoler", "bbox": [-82.0, 26.0, -81.0, 27.0]},
             {"species/match": {"usageKey": 2435099, "matchType": "FUZZY", "confidence": 95,
                                "scientificName": "Puma concolor", "rank": "SPECIES"}})
    assert isinstance(e, RouterInputError) and e.error_code == "GBIF_INPUT_ERROR"


def test_gbif_unknown_name_rejected():
    e = _err("fetch_gbif_occurrences",
             {"species_key": "Zxqwv notaname", "bbox": [-82.0, 26.0, -81.0, 27.0]},
             {"species/match": {"matchType": "NONE"}})
    assert isinstance(e, RouterInputError) and e.error_code == "GBIF_INPUT_ERROR"


def test_gbif_offset_paging_walks_to_endofrecords():
    # page 1 full (300) not endOfRecords -> page 2 fetched; page 2 endOfRecords -> stop.
    p1 = _gbif_page([_occ(-81.5, 26.5, i) for i in range(300)], False)
    p2 = _gbif_page([_occ(-81.6, 26.6, 999)], True)
    spec = _spec("fetch_gbif_occurrences")
    seen = []

    def fake_get(_spec, plan):
        off = plan.params.get("offset")
        seen.append(off)
        return json.dumps(p1 if off == 0 else p2).encode()

    orig = C._get
    C._get = fake_get
    try:
        params = R.validate_params(spec, {"species_key": "212", "bbox": [-82.0, 26.0, -81.0, 27.0], "max_records": 5000})
        C.execute(spec, params)
    finally:
        C._get = orig
    assert seen == [0, 300]  # exactly two pages, then endOfRecords stops


def test_gbif_empty_header_only():
    gdf = _run("fetch_gbif_occurrences",
               {"species_key": "212", "bbox": [-82.0, 26.0, -81.0, 27.0]},
               {"occurrence/search": _gbif_page([], True)})
    assert len(gdf) == 0
    assert set(gdf.columns) >= {"gbifID", "species", "basisOfRecord"}


# --------------------------------------------------------------------------- #
# iNat: resolve + page-number paging + extraction.
# --------------------------------------------------------------------------- #

def _inat_obs(lon, lat, oid=1):
    return {"geojson": {"coordinates": [lon, lat]}, "id": oid, "observed_on": "2021-01-01",
            "user": {"login": "obs"}, "photos": [{"url": "u"}], "species_guess": "gator", "place_guess": "FL"}


def test_inat_digit_id_fastpath():
    gdf = _run("fetch_inaturalist_observations",
               {"taxon_id": "43584", "bbox": [-82.0, 26.0, -81.0, 27.0], "max_records": 200},
               {"observations": {"results": [_inat_obs(-81.5, 26.5)], "total_results": 1}})
    assert len(gdf) == 1
    assert set(gdf.columns) >= {"id", "observed_on", "user_login", "photo_url", "species_guess", "place_guess"}


def test_inat_name_resolve_then_fetch():
    gdf = _run("fetch_inaturalist_observations",
               {"taxon_id": "American alligator", "bbox": [-82.0, 26.0, -81.0, 27.0], "max_records": 200},
               {"/taxa": {"results": [{"id": 26039}]},
                "observations": {"results": [_inat_obs(-81.5, 26.5)], "total_results": 1}})
    assert len(gdf) == 1


def test_inat_unknown_name_input_invalid():
    e = _err("fetch_inaturalist_observations",
             {"taxon_id": "Zxqwv", "bbox": [-82.0, 26.0, -81.0, 27.0]},
             {"/taxa": {"results": []}})
    assert isinstance(e, RouterInputError) and e.error_code == "INAT_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# NWS alerts: event filter + preserved props + zone enrichment + keep-null.
# --------------------------------------------------------------------------- #

def _alert(event, geom=None, zones=None, ugc=None):
    props = {"event": event, "headline": "H", "severity": "Severe", "id": event}
    if zones is not None:
        props["affectedZones"] = zones
    if ugc is not None:
        props["geocode"] = {"UGC": ugc}
    return {"type": "Feature", "geometry": geom, "properties": props}


def test_alerts_event_filter_and_inline_geometry():
    fc = {"type": "FeatureCollection", "features": [
        _alert("Flood Warning", geom={"type": "Point", "coordinates": [-80.0, 26.0]}),
        _alert("Heat Advisory", geom={"type": "Point", "coordinates": [-80.0, 26.0]}),
    ]}
    gdf = _run("fetch_nws_alerts_conus", {"event_types": ["Flood Warning"], "status": "actual"},
               {"/alerts/active": fc})
    assert len(gdf) == 1 and gdf.iloc[0]["event"] == "Flood Warning"


def test_alerts_zone_enrichment_union_and_keep_null():
    zone_geom = {"type": "Polygon", "coordinates": [[[-80, 26], [-79, 26], [-79, 27], [-80, 27], [-80, 26]]]}
    fc = {"type": "FeatureCollection", "features": [
        _alert("Flood Warning", geom=None, zones=["https://api.weather.gov/zones/forecast/FLZ001"]),
        _alert("Flood Warning", geom=None, zones=["https://api.weather.gov/zones/forecast/FLZ999"]),  # unresolved
    ]}
    gdf = _run("fetch_nws_alerts_conus", {"event_types": ["Flood Warning"]},
               {"/alerts/active": fc,
                "zones/forecast/FLZ001": {"geometry": zone_geom},
                "zones/forecast/FLZ999": RouterUpstreamError("404")})
    # Both rows kept (never silent-drop); one has resolved geometry, one NULL.
    assert len(gdf) == 2
    assert int(gdf.geometry.notna().sum()) == 1
    assert int(gdf.geometry.isna().sum()) == 1


def test_alerts_bad_status_input_invalid():
    e = _err("fetch_nws_alerts_conus", {"status": "bogus"}, {"/alerts/active": {"type": "FeatureCollection", "features": []}})
    # status is a router enum -> validate_params rejects before the hook.
    assert isinstance(e, RouterInputError) and e.error_code == "NWS_CONUS_INPUT_INVALID"


def test_alerts_bad_area_input_invalid():
    e = _err("fetch_nws_alerts_conus", {"area": "Atlantis"}, {"/alerts/active": {"type": "FeatureCollection", "features": []}})
    assert isinstance(e, RouterInputError) and e.error_code == "NWS_CONUS_INPUT_INVALID"


# --------------------------------------------------------------------------- #
# NWS river: gates + no-gauges + threshold enrichment.
# --------------------------------------------------------------------------- #

def _gauge(lid, lon=-91.0, lat=30.5):
    return {"lid": lid, "name": lid, "latitude": lat, "longitude": lon,
            "status": {"observed": {"primary": 10.0, "floodCategory": "no_flooding"}, "forecast": {}}}


def test_river_bbox_no_gauges_typed_error():
    e = _err("fetch_nws_river_forecast", {"bbox": [-80.0, 24.0, -79.9, 24.1]},
             {"nwps/v1/gauges": {"gauges": []}})
    assert isinstance(e, RouterInputError) and e.error_code == "NWS_RIVER_FORECAST_NO_GAUGES"


def test_river_bbox_too_large():
    e = _err("fetch_nws_river_forecast", {"bbox": [-170.0, -80.0, 170.0, 80.0]},
             {"nwps/v1/gauges": {"gauges": []}})
    assert isinstance(e, RouterInputError) and e.error_code == "NWS_RIVER_FORECAST_BBOX_TOO_LARGE"


def test_river_missing_selector_input_error():
    e = _err("fetch_nws_river_forecast", {}, {"nwps": {"gauges": []}})
    assert isinstance(e, RouterInputError) and e.error_code == "NWS_RIVER_FORECAST_INPUT_ERROR"


def test_river_threshold_enrichment_joins_stages():
    detail = {"flood": {"categories": {"action": {"stage": 5.0}, "minor": {"stage": 8.0},
                                        "moderate": {"stage": 10.0}, "major": {"stage": 12.0}}}}
    gdf = _run("fetch_nws_river_forecast",
               {"bbox": [-91.5, 30.0, -90.0, 31.0], "include_thresholds": True},
               {"gauges/ABCI1": detail,  # detail (more specific) checked first
                "v1/gauges": {"gauges": [_gauge("ABCI1")]}})
    row = gdf.iloc[0]
    assert row["action_stage_ft"] == 5.0 and row["major_stage_ft"] == 12.0


def test_river_gauge_id_detail_mode():
    detail = {"lid": "CIDI4", "name": "gauge", "latitude": 30.5, "longitude": -91.0,
              "status": {"observed": {"primary": 3.0, "floodCategory": "action"}, "forecast": {}},
              "flood": {"categories": {"action": {"stage": 2.5}}}}
    gdf = _run("fetch_nws_river_forecast", {"gauge_id": "CIDI4"},
               {"nwps/v1/gauges/CIDI4": detail})
    assert len(gdf) == 1
    assert gdf.iloc[0]["lid"] == "CIDI4" and gdf.iloc[0]["action_stage_ft"] == 2.5
