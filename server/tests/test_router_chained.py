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


# --------------------------------------------------------------------------- #
# openfema_disasters (ADR 0064): offset paging + attribute<-boundary FIPS enrich.
# --------------------------------------------------------------------------- #

from trid3nt_server.agent.tools.fetchers._router.executors import http_json as _HJ
from trid3nt_server.agent.tools.fetchers._router.hooks import openfema_disasters as _OF
from trid3nt_server.agent.tools.fetchers._router.hooks import storm_events_db as _SE


def _decl(fips_state, fips_county, dnum, itype, dtype="DR", date="2020-06-01T00:00:00.000Z",
          area="Area", ia=True, pa=False):
    return {"fipsStateCode": fips_state, "fipsCountyCode": fips_county, "disasterNumber": dnum,
            "incidentType": itype, "declarationType": dtype, "declarationDate": date,
            "designatedArea": area, "iaProgramDeclared": ia, "paProgramDeclared": pa}


def _fema_page(records):
    return {"DisasterDeclarationsSummaries": records}


def _tiger(features):
    return {"type": "FeatureCollection", "features": features}


def _county_poly(geoid, name, ring):
    return {"type": "Feature", "properties": {"GEOID": geoid, "NAME": name, "STATE": geoid[:2],
            "COUNTY": geoid[2:]}, "geometry": {"type": "Polygon", "coordinates": [ring]}}


_RI_RING = [[-71.4, 41.6], [-71.3, 41.6], [-71.3, 41.7], [-71.4, 41.7], [-71.4, 41.6]]
_RI2_RING = [[-71.6, 41.8], [-71.5, 41.8], [-71.5, 41.9], [-71.6, 41.9], [-71.6, 41.8]]


def test_openfema_aggregate_and_fips_join():
    """Two declarations for one county aggregate; each county joins its TIGER polygon."""
    fema = _fema_page([
        _decl("44", "001", 4001, "Flood", date="2019-01-01T00:00:00.000Z"),
        _decl("44", "001", 4002, "Hurricane", date="2021-05-01T00:00:00.000Z", pa=True),
        _decl("44", "003", 4003, "Severe Storm"),
    ])
    tiger = _tiger([_county_poly("44001", "Bristol", _RI_RING),
                    _county_poly("44003", "Kent", _RI2_RING)])
    gdf = _run("fetch_openfema_disasters", {"state_code": "RI"},
               {"fema.gov": fema, "tigerweb": tiger})
    rows = {r["county_fips"]: r for _, r in gdf.iterrows()}
    assert set(rows) == {"44001", "44003"}
    assert rows["44001"]["n_declarations"] == 2
    assert rows["44001"]["disaster_numbers"] == "4001,4002"
    assert rows["44001"]["incident_types"] == "Flood,Hurricane"
    assert rows["44001"]["latest_declaration"] == "2021-05-01T00:00:00.000Z"
    assert bool(rows["44001"]["pa_program"]) is True
    assert rows["44001"]["county_name"] == "Bristol"


def test_openfema_statewide_excluded_and_no_declarations():
    """fipsCountyCode 000 (statewide) rows never join; nothing left -> NO_DECLARATIONS."""
    fema = _fema_page([_decl("44", "000", 5000, "Drought")])
    exc = _err("fetch_openfema_disasters", {"state_code": "RI"},
               {"fema.gov": fema, "tigerweb": _tiger([])})
    assert exc.error_code == "OPENFEMA_NO_DECLARATIONS"
    assert exc.retryable is False


def test_openfema_bbox_clip_drops_outside_county():
    """A county whose polygon falls outside the bbox is dropped (selector clip path)."""
    fema = _fema_page([_decl("44", "001", 4001, "Flood"), _decl("44", "003", 4003, "Fire")])
    tiger = _tiger([_county_poly("44001", "Bristol", _RI_RING),
                    _county_poly("44003", "Kent", _RI2_RING)])
    # bbox covers only the 44001 ring (~ -71.4..-71.3), not 44003 (~ -71.6..-71.5).
    gdf = _run("fetch_openfema_disasters", {"bbox": [-71.45, 41.55, -71.28, 41.72]},
               {"fema.gov": fema, "tigerweb": tiger})
    assert set(gdf["county_fips"]) == {"44001"}


def test_openfema_next_page_offset_stops_on_short_page():
    """The reused offset-paging primitive: a full page continues, a short page stops."""
    spec = _spec("fetch_openfema_disasters")
    full = json.dumps(_fema_page([_decl("44", "001", i, "Flood") for i in range(_OF._PAGE_SIZE)])).encode()
    short = json.dumps(_fema_page([_decl("44", "001", 1, "Flood")])).encode()
    raw = R.validate_params(spec, {"state_code": "RI"})
    nxt_full = _OF.next_page(spec, raw, [full])
    assert nxt_full is not None and "$skip" in str(nxt_full.params) and nxt_full.params["$skip"] == "1000"
    assert _OF.next_page(spec, raw, [full, short]) is None      # short page -> stop
    assert _OF.next_page(spec, raw, [short]) is None            # first page short -> stop


def test_openfema_input_errors():
    spec = _spec("fetch_openfema_disasters")
    for raw in ({}, {"state_code": "ZZ"}, {"state_code": "RI", "incident_type": "Frogs"},
                {"state_code": "RI", "start_year": 1800}):
        with pytest.raises(RouterInputError) as ei:
            _OF.build_request(spec, R.validate_params(spec, raw))
        assert ei.value.error_code == "OPENFEMA_INPUT_ERROR"


# --------------------------------------------------------------------------- #
# storm_events_db (ADR 0064): directory-index resolve -> bulk gzip-CSV decode.
# --------------------------------------------------------------------------- #

import gzip as _gzip


def _run_http(name: str, raw: dict, url_map) -> gpd.GeoDataFrame:
    """Route an http_json + resolve-phase spec, serving canned bytes by URL substring."""
    spec = _spec(name)

    def fake_get(_spec, plan):
        for key, body in url_map.items():
            if key in plan.url:
                if isinstance(body, Exception):
                    raise body
                return body if isinstance(body, bytes) else json.dumps(body).encode()
        raise AssertionError(f"no canned body for {plan.url}")

    o1, o2 = C._get, _HJ._get
    C._get = fake_get
    _HJ._get = fake_get
    try:
        params = R.validate_params(spec, dict(raw))
        params = C.pre_resolve(spec, params)
        data = _HJ.execute(spec, params)
    finally:
        C._get = o1
        _HJ._get = o2
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(data)
        p = f.name
    try:
        return gpd.read_file(p)
    finally:
        os.unlink(p)


_INDEX_HTML = (
    '<a href="StormEvents_details-ftp_v1.0_d2022_c20230101.csv.gz">f</a>'
    '<a href="StormEvents_details-ftp_v1.0_d2022_c20240517.csv.gz">f</a>'
    '<a href="StormEvents_details-ftp_v1.0_d2021_c20230101.csv.gz">f</a>'
).encode()

_CSV = (
    "EVENT_ID,EVENT_TYPE,STATE,BEGIN_LAT,BEGIN_LON,BEGIN_DATE_TIME,END_DATE_TIME,"
    "INJURIES_DIRECT,DEATHS_DIRECT,DEATHS_INDIRECT,DAMAGE_PROPERTY,MAGNITUDE,EPISODE_NARRATIVE\n"
    "1,Tornado,OKLAHOMA,35.5,-97.5,28-SEP-22 14:00:00,28-SEP-22 14:30:00,0,0,0,10.00K,,narr1\n"
    "2,Flood,FLORIDA,27.5,-81.5,29-SEP-22 06:00:00,29-SEP-22 12:00:00,1,0,0,5.00K,,narr2\n"
    "3,Hail,OKLAHOMA,36.1,-95.9,01-MAY-22 20:00:00,01-MAY-22 20:15:00,0,0,0,1.00K,1.75,narr3\n"
)


def _csv_gz(text=_CSV):
    return _gzip.compress(text.encode())


def test_storm_resolve_picks_newest_processed_date():
    spec = _spec("fetch_storm_events_db")

    def fake_get(_spec, plan):
        return _INDEX_HTML

    o = C._get
    C._get = fake_get
    try:
        merged = C.pre_resolve(spec, R.validate_params(spec, {"year": 2022}))
    finally:
        C._get = o
    urls = merged["_csv_urls"]
    assert len(urls) == 1 and urls[0].endswith("d2022_c20240517.csv.gz")  # newest c-date wins


def test_storm_resolve_missing_year_upstream():
    spec = _spec("fetch_storm_events_db")

    def fake_get(_spec, plan):
        return _INDEX_HTML

    o = C._get
    C._get = fake_get
    try:
        params = R.validate_params(spec, {"year": 1999})
        with pytest.raises(RouterUpstreamError) as ei:
            C.pre_resolve(spec, params)
    finally:
        C._get = o
    assert ei.value.error_code == "STORM_EVENTS_UPSTREAM_ERROR"


def test_storm_state_and_event_filter():
    gdf = _run_http("fetch_storm_events_db", {"year": 2022, "state": "OK", "event_types": ["Tornado"]},
                    {"d2022_c20240517": _csv_gz(), "csvfiles/": _INDEX_HTML})
    assert set(gdf["EVENT_ID"].astype(str)) == {"1"}
    assert gdf.iloc[0]["EVENT_TYPE"] == "Tornado"


def test_storm_bbox_filter():
    gdf = _run_http("fetch_storm_events_db", {"year": 2022, "bbox": [-98.0, 34.0, -95.0, 37.0]},
                    {"d2022_c20240517": _csv_gz(), "csvfiles/": _INDEX_HTML})
    # OK points (35.5,-97.5) + (36.1,-95.9) fall in bbox; FL point does not.
    assert set(gdf["EVENT_ID"].astype(str)) == {"1", "3"}


def test_storm_empty_after_filter():
    exc = _err_http("fetch_storm_events_db", {"year": 2022, "state": "RI"},
                    {"d2022_c20240517": _csv_gz(), "csvfiles/": _INDEX_HTML})
    assert exc.error_code == "STORM_EVENTS_EMPTY" and exc.retryable is False


def test_storm_corrupt_gzip_upstream():
    exc = _err_http("fetch_storm_events_db", {"year": 2022, "state": "OK"},
                    {"d2022_c20240517": b"not-a-gzip", "csvfiles/": _INDEX_HTML})
    assert exc.error_code == "STORM_EVENTS_UPSTREAM_ERROR"


def test_storm_input_errors():
    spec = _spec("fetch_storm_events_db")
    # year out of range -> declarative ARG_INVALID.
    with pytest.raises(RouterInputError) as ei:
        R.validate_params(spec, {"year": 1800})
    assert ei.value.error_code == "STORM_EVENTS_ARG_INVALID"
    # bad state / bad window -> resolve_build validation.
    for raw in ({"year": 2022, "state": "Atlantis"},
                {"year": 2022, "begin_date": "2022-05-05", "end_date": "2022-01-01"}):
        with pytest.raises(RouterInputError) as ei:
            _SE.resolve_build(spec, R.validate_params(spec, raw))
        assert ei.value.error_code == "STORM_EVENTS_ARG_INVALID"


def _err_http(name, raw, url_map):
    with pytest.raises(Exception) as ei:
        _run_http(name, raw, url_map)
    return ei.value
