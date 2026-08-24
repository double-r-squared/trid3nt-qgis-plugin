"""Migrated coverage for the arcgis-odd fold wave (ADR 0066).

The five twins (fema_nfhl_zones, nwi_wetlands, wdpa_protected_areas, usace_dams,
epa_frs_facilities) were DELETED and folded onto the EXISTING tier-3 hooks
(build_request / next_page / parse_response). Live twin-vs-router feature-set
value-identity was proven at fold time; this file migrates the value-bearing
UNIT coverage of the pure hook logic (offline, synthetic bodies): OBJECTID-cursor
paging + tolerate, server-side sfha/zone/IN() where, prefix-strip normalizer,
raise-on-unknown alias + fail-loud, USPS/hazard normalization, keyless-mirror
endpoint selection, program-expansion union + point-from-LAT/LON synthesis.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.tools.fetchers._router.errors import RouterInputError, RouterUpstreamError
from trid3nt_server.tools.fetchers._router.hooks import (
    fema_nfhl_zones as nfhl,
    nwi_wetlands as nwi,
    wdpa_protected_areas as wdpa,
    usace_dams as dams,
    epa_frs_facilities as frs,
)


def _spec(prefix: str, source_class: str) -> SourceSpec:
    return SourceSpec.model_validate({
        "schema_version": "v1", "name": "t", "source_class": source_class,
        "error_prefix": prefix, "input_error_suffix": "INPUT_INVALID", "shape": "vector-fgb",
        "endpoints": {"data": {"url": "https://x/query"}},
        "auth": {"mode": "none", "user_agent": "ua"},
        "output": {"layer_type": "vector", "ext": "fgb", "style_preset": "s"},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "per_feature"},
    })


def _fc(feats):
    return json.dumps({"type": "FeatureCollection", "features": feats}).encode()


def _poly(oid, zone="AE"):
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]},
            "properties": {"OBJECTID": oid, "FLD_ZONE": zone, "SFHA_TF": "T"}}


# ------------------------------- NFHL ------------------------------- #

def test_nfhl_where_sfha_and_zone_in():
    s = _spec("FEMA_NFHL_ZONES", "fema_nfhl")
    plan = nfhl.build_request(s, {"bbox": [0, 0, 1, 1], "sfha_only": True, "zone_filter": ["ve", "V"]})[0]
    w = plan.params["where"]
    assert w.startswith("OBJECTID>0")
    assert "SFHA_TF='T'" in w
    assert "FLD_ZONE IN ('V','VE')" in w  # uppercased + sorted
    assert plan.params["orderByFields"] == "OBJECTID"


def test_nfhl_bad_zone_raises_input_invalid():
    s = _spec("FEMA_NFHL_ZONES", "fema_nfhl")
    with pytest.raises(RouterInputError) as e:
        nfhl.build_request(s, {"bbox": [0, 0, 1, 1], "zone_filter": ["ZZZ"]})
    assert e.value.error_code == "FEMA_NFHL_ZONES_INPUT_INVALID"


def test_nfhl_cursor_advances_and_stops_short():
    s = _spec("FEMA_NFHL_ZONES", "fema_nfhl")
    full = _fc([_poly(i) for i in range(1, nfhl._PAGE_SIZE + 1)])
    nxt = nfhl.next_page(s, {"bbox": [0, 0, 1, 1]}, [full])
    assert nxt is not None and "OBJECTID>1000" in nxt.params["where"]
    short = _fc([_poly(i) for i in range(1, 10)])
    assert nfhl.next_page(s, {"bbox": [0, 0, 1, 1]}, [full, short]) is None


def test_nfhl_parse_strips_objectid_projects_14():
    s = _spec("FEMA_NFHL_ZONES", "fema_nfhl")
    feats = nfhl.parse_response(s, {}, [_fc([_poly(7)])])
    assert len(feats) == 1
    props = feats[0]["properties"]
    assert "OBJECTID" not in props
    assert set(props) == set(nfhl._PRESERVED_PROPERTIES)


# ------------------------------- NWI ------------------------------- #

def test_nwi_prefix_strip_first_wins():
    props = {"Wetlands.ATTRIBUTE": "PFO1A", "NWI_Wetland_Codes.ATTRIBUTE": "LOOKUP",
             "Wetlands.WETLAND_TYPE": "Freshwater", "Wetlands.ACRES": 3.2, "Wetlands.OBJECTID": 1}
    out = nwi._normalize_props(props)
    assert out == {"attribute": "PFO1A", "wetland_type": "Freshwater", "acres": 3.2}


def test_nwi_waf_headers_on_plan():
    s = _spec("NWI_WETLANDS", "nwi_wetlands")
    plan = nwi.build_request(s, {"bbox": [0, 0, 1, 1]})[0]
    assert plan.headers.get("Referer", "").startswith("https://www.fws.gov")
    assert "Mozilla" in plan.headers.get("User-Agent", "")


def test_nwi_next_page_short_stops_full_continues():
    s = _spec("NWI_WETLANDS", "nwi_wetlands")
    short = json.dumps({"type": "FeatureCollection", "features": [{"a": 1}] * 10,
                        "exceededTransferLimit": False}).encode()
    assert nwi.next_page(s, {"bbox": [0, 0, 1, 1]}, [short]) is None
    full = json.dumps({"type": "FeatureCollection", "features": [{"a": 1}] * nwi._PAGE_SIZE,
                       "exceededTransferLimit": True}).encode()
    nxt = nwi.next_page(s, {"bbox": [0, 0, 1, 1]}, [full])
    assert nxt is not None and nxt.params["resultOffset"] == str(nwi._PAGE_SIZE)


# ------------------------------- WDPA ------------------------------- #

@pytest.mark.parametrize("raw,canon", [
    ("NP", "National Park"), ("national parks", "National Park"),
    ("N.P.", "National Park"), ("nwr", "National Wildlife Refuge"),
    ("ramsar", "Ramsar Site, Wetland of International Importance"),
])
def test_wdpa_alias_resolves(raw, canon):
    assert wdpa._normalize_one("WDPA", raw) == canon


def test_wdpa_unknown_raises_designation_invalid():
    with pytest.raises(RouterInputError) as e:
        wdpa._normalize_one("WDPA", "Narnia Park")
    assert e.value.error_code == "WDPA_DESIGNATION_INVALID"


def test_wdpa_fail_loud_when_filter_empties_nonempty():
    s = _spec("WDPA", "wdpa")
    body = _fc([{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]},
                "properties": {"desig_eng": "National Park"}}])
    with pytest.raises(RouterInputError) as e:
        wdpa.parse_response(s, {"designation_filter": ["Wilderness Area"]}, [body])
    assert e.value.error_code == "WDPA_DESIGNATION_INVALID"
    assert "National Park" in str(e.value)


def test_wdpa_casefold_filter_keeps_match():
    s = _spec("WDPA", "wdpa")
    body = _fc([
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]}, "properties": {"desig_eng": "national park"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 1]}, "properties": {"desig_eng": "State Park"}},
    ])
    out = wdpa.parse_response(s, {"designation_filter": ["NP"]}, [body])
    assert len(out) == 1 and out[0]["properties"]["desig_eng"] == "national park"


# ------------------------------- usace_dams ------------------------------- #

def test_dams_where_in_and_height_usps_expand():
    w = dams._where(dams._norm_hazard("USACE_DAMS", ["high", "High"]),
                    dams._norm_state("USACE_DAMS", ["nv", "north carolina"]),
                    dams._norm_min_height("USACE_DAMS", 50))
    assert "HAZARD_POTENTIAL IN ('High')" in w  # dedup + canonical case
    assert "STATE IN ('Nevada','North Carolina')" in w
    assert "DAM_HEIGHT >= 50" in w


def test_dams_bad_hazard_raises():
    with pytest.raises(RouterInputError):
        dams._norm_hazard("USACE_DAMS", "Extreme")


def test_dams_keyless_uses_mirror(monkeypatch):
    monkeypatch.delenv("TRID3NT_USACE_NID_TOKEN", raising=False)
    s = _spec("USACE_DAMS", "usace_nid_dams")
    plan = dams.build_request(s, {"bbox": [0, 0, 1, 1]})[0]
    assert plan.url == dams._NID_BASE and "token" not in (plan.params or {})


def test_dams_token_selects_authoritative():
    s = _spec("USACE_DAMS", "usace_nid_dams")
    plan = dams.build_request(s, {"bbox": [0, 0, 1, 1], "token": "T0K"})[0]
    assert plan.url == dams._NID_AUTHORITATIVE_BASE and plan.params["token"] == "T0K"


# ------------------------------- FRS ------------------------------- #

def test_frs_program_expansion():
    assert frs._programs("EPA_FRS", {"facility_program": "frs"}) == frs.FRS_UNION_PROGRAMS
    assert frs._programs("EPA_FRS", {"facility_program": "npl"}) == ["superfund"]
    assert frs._programs("EPA_FRS", {}) == frs.FRS_UNION_PROGRAMS


def test_frs_bad_program_raises():
    s = _spec("EPA_FRS", "epa_frs_facilities")
    with pytest.raises(RouterInputError):
        frs.build_request(s, {"bbox": [0, 0, 1, 1], "facility_program": "nonsense"})


def test_frs_union_order_and_stamp():
    s = _spec("EPA_FRS", "epa_frs_facilities")
    tri = _fc([{"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 2]},
                "properties": {"primary_name": "ACME"}}])
    water = _fc([{"type": "Feature", "geometry": {"type": "Point", "coordinates": [3, 4]},
                  "properties": {"primary_name": "H2O"}}])
    empty = _fc([])
    bodies = [tri, water, empty, empty, empty]  # order = FRS_UNION_PROGRAMS
    out = frs.parse_response(s, {"facility_program": "frs"}, bodies)
    assert [f["properties"]["program"] for f in out] == ["tri", "water"]
    assert out[0]["properties"]["facility_name"] == "ACME"


def test_frs_superfund_point_from_latlon():
    s = _spec("EPA_FRS", "epa_frs_facilities")
    body = json.dumps({"features": [{"attributes": {"LATITUDE": 30.1, "LONGITUDE": -95.2,
                        "Site_Name": "Dump", "EPA_ID": "TX123", "NPL_Status": "Final"}}]}).encode()
    out = frs.parse_response(s, {"facility_program": "superfund"}, [body])
    assert len(out) == 1
    assert out[0]["geometry"]["coordinates"] == [-95.2, 30.1]
    assert out[0]["properties"]["npl_status"] == "Final"


def test_frs_superfund_drops_bad_latlon():
    s = _spec("EPA_FRS", "epa_frs_facilities")
    body = json.dumps({"features": [{"attributes": {"LATITUDE": None, "LONGITUDE": -95.2}}]}).encode()
    assert frs.parse_response(s, {"facility_program": "superfund"}, [body]) == []


# ------------------------------- chained tolerate_page_error ------------------------------- #

def test_tolerate_page_error_returns_partial():
    from trid3nt_server.tools.fetchers._router.executors import chained_resolution as ch
    s = SourceSpec.model_validate({
        "schema_version": "v1", "name": "t", "source_class": "sc", "shape": "vector-fgb",
        "endpoints": {"data": {"url": "https://x"}}, "output": {"layer_type": "vector", "ext": "fgb", "style_preset": "s"},
        "cache": {"ttl_class": "static-30d"}, "payload_estimate": {"model": "per_feature"},
        "hooks": {"build_request": "fema_nfhl_zones.build_request", "next_page": "fema_nfhl_zones.next_page",
                  "parse_response": "fema_nfhl_zones.parse_response"},
        "ingest": {"chained": {"tolerate_page_error": True}},
    })
    calls = {"n": 0}

    def fake_get(spec, plan):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fc([_poly(i) for i in range(1, ch.__dict__.get("_PAGE", 0) or 1001)])
        raise RouterUpstreamError("boom")

    import trid3nt_server.tools.fetchers._router.executors.chained_resolution as CH
    orig = CH._get
    CH._get = fake_get
    try:
        bodies = CH._fetch_main(s, {"bbox": [0, 0, 1, 1]})
    finally:
        CH._get = orig
    assert len(bodies) == 1  # first page kept, later-page failure tolerated
