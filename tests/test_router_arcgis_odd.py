"""Migrated coverage for the arcgis-odd fold wave (ADR 0066).

The twins (fema_nfhl_zones, usace_dams, epa_frs_facilities) were
DELETED and folded onto the EXISTING tier-3 hooks
(build_request / next_page / parse_response). Live twin-vs-router feature-set
value-identity was proven at fold time; this file migrates the value-bearing
UNIT coverage of the pure hook logic (offline, synthetic bodies): OBJECTID-cursor
paging + tolerate, server-side sfha/zone/IN() where, USPS/hazard
normalization, keyless-mirror endpoint selection, program-expansion union +
point-from-LAT/LON synthesis.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.tools.fetchers._router.errors import RouterInputError, RouterUpstreamError
from trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones import hooks as nfhl
from trid3nt_server.tools.fetchers.hazard.fetch_usace_dams import hooks as dams
from trid3nt_server.tools.fetchers.hazard.fetch_epa_frs_facilities import hooks as frs


def _spec(prefix: str, source_class: str) -> SourceSpec:
    return SourceSpec.model_validate({
        "schema_version": "v1", "name": "t", "source_class": source_class,
        "error_prefix": prefix, "input_error_suffix": "INPUT_INVALID", "shape": "vector-fgb",
        "endpoints": {"data": {"url": "https://x/query"}},
        "auth": {"mode": "none", "user_agent": "ua"},
        "output": {"layer_type": "vector", "ext": "fgb", "style": {"kind": "reference"}},
        "cache": {"ttl_class": "static-30d"},
        "payload_estimate": {"model": "per_feature"},
    })


def _fc(feats):
    return json.dumps({"type": "FeatureCollection", "features": feats}).encode()


def _poly(oid, zone="AE"):
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]},
            "properties": {"OBJECTID": oid, "FLD_ZONE": zone, "SFHA_TF": "T"}}


# ------------------------------- NFHL ------------------------------- #


class _FakeBase:
    """The service client the library wraps: the two knobs the hook sets."""

    def __init__(self):
        self.outformat = "json"
        self.max_nrecords = 2000
        self.n_missing = 0


class _FakeClient:
    """The object-id read, offline: one feature per id, tile by tile."""

    def __init__(self, base):
        self.client = base

    def oids_bygeom(self, bbox, geo_crs=4326, sql_clause=None):
        last = _FakeNFHL.last
        last |= {"bbox": bbox, "geo_crs": geo_crs, "sql_clause": sql_clause,
                 "outformat": self.client.outformat,
                 "max_nrecords": self.client.max_nrecords}
        last.setdefault("tiles", []).append(tuple(bbox))
        # id 3 is listed by every tile: the polygon that straddles the edges, and so
        # the case the merge has to de-duplicate.
        return iter([("1", "2"), ("3",)] if len(last["tiles"]) == 1 else [("3",)])

    def get_features(self, featureids, return_m=False, return_geom=True):
        batches = list(featureids)
        _FakeNFHL.last.setdefault("batches", []).append([len(b) for b in batches])
        if len(_FakeNFHL.last["tiles"]) in _FakeNFHL.refuse:
            raise RuntimeError("ServiceError: the tile was refused")
        props = dict.fromkeys(nfhl._PRESERVED_PROPERTIES, None)
        return [{"type": "FeatureCollection", "features": [
            {"type": "Feature",
             "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]},
             "properties": {**props, "FLD_ZONE": "AE", "SFHA_TF": "T", "OBJECTID": int(i)}}
            for i in ids]} for ids in batches]


class _FakeNFHL:
    """The library client, answering offline; records what it was asked for."""

    last: dict = {}
    #: 1-based tile numbers the service refuses to serve features for.
    refuse: set = set()

    def __init__(self, service, layer):
        _FakeNFHL.last = {"service": service, "layer": layer}
        self.client = _FakeClient(_FakeBase())


def _stub_nfhl(monkeypatch, cls=_FakeNFHL, refuse=()):
    import pygeohydro

    _FakeNFHL.refuse = set(refuse)
    monkeypatch.setattr(pygeohydro, "NFHL", cls)
    monkeypatch.setattr(nfhl.time, "sleep", lambda _s: None)
    return cls


#: One tile wide, so a test that is not about tiling reads exactly one.
_ONE_TILE = [0.0, 0.0, 0.1, 0.1]


def test_nfhl_sql_clause_sfha_and_zone_in(monkeypatch):
    _stub_nfhl(monkeypatch)
    s = _spec("FEMA_NFHL_ZONES", "fema_nfhl")
    nfhl.delegate(s, {"bbox": _ONE_TILE, "sfha_only": True, "zone_filter": ["ve", "V"]},
                  timeout_s=30.0)
    c = _FakeNFHL.last["sql_clause"]
    assert "SFHA_TF='T'" in c
    assert "FLD_ZONE IN ('V','VE')" in c  # uppercased + sorted
    assert _FakeNFHL.last["service"] == "NFHL" and _FakeNFHL.last["layer"] == "flood hazard zones"


def test_nfhl_asks_for_geojson_so_the_holes_survive(monkeypatch):
    """Esri JSON fills a zone polygon's holes; the format is named, not defaulted."""
    _stub_nfhl(monkeypatch)
    s = _spec("FEMA_NFHL_ZONES", "fema_nfhl")
    nfhl.delegate(s, {"bbox": _ONE_TILE}, timeout_s=30.0)
    assert _FakeNFHL.last["outformat"] == "geojson"


def test_nfhl_bad_zone_raises_input_invalid(monkeypatch):
    _stub_nfhl(monkeypatch)
    s = _spec("FEMA_NFHL_ZONES", "fema_nfhl")
    with pytest.raises(RouterInputError) as e:
        nfhl.delegate(s, {"bbox": _ONE_TILE, "zone_filter": ["ZZZ"]}, timeout_s=30.0)
    assert e.value.error_code == "FEMA_NFHL_ZONES_INPUT_INVALID"


def test_nfhl_projects_the_regulatory_fourteen(monkeypatch):
    _stub_nfhl(monkeypatch)
    s = _spec("FEMA_NFHL_ZONES", "fema_nfhl")
    feats = nfhl.delegate(s, {"bbox": _ONE_TILE}, timeout_s=30.0)
    assert len(feats) == 3  # one feature per object id, over the tile's two batches
    props = feats[0]["properties"]
    assert "OBJECTID" not in props
    assert set(props) == set(nfhl._PRESERVED_PROPERTIES)


def test_nfhl_reads_one_batch_at_a_time_under_the_deliverable_size(monkeypatch):
    """The service advertises 2000 and 500s on it; one batch per call carries the retry."""
    _stub_nfhl(monkeypatch)
    s = _spec("FEMA_NFHL_ZONES", "fema_nfhl")
    nfhl.delegate(s, {"bbox": _ONE_TILE}, timeout_s=30.0)
    assert _FakeNFHL.last["max_nrecords"] == nfhl._MAX_IDS_PER_REQUEST < 2000
    assert _FakeNFHL.last["batches"] == [[2], [1]]


def test_nfhl_unread_object_ids_are_never_a_partial_layer(monkeypatch):
    """The library warns and returns what it has; a regulatory layer may not."""

    class _Lossy(_FakeNFHL):
        def __init__(self, service, layer):
            super().__init__(service, layer)
            outer = self

            def get_features(featureids, return_m=False, return_geom=True):
                out = _FakeClient.get_features(outer.client, featureids)
                outer.client.client.n_missing = 4
                return out

            self.client.get_features = get_features

    _stub_nfhl(monkeypatch, _Lossy)
    s = _spec("FEMA_NFHL_ZONES", "fema_nfhl")
    with pytest.raises(RouterUpstreamError) as e:
        nfhl.delegate(s, {"bbox": _ONE_TILE}, timeout_s=30.0)
    assert "8 object id(s)" in str(e.value)  # 4 unread reported per batch


def test_nfhl_tiles_a_wide_aoi_and_merges_it_by_object_id(monkeypatch):
    """One client cannot read a metro AOI in one pass, so the AOI is read in tiles."""
    _stub_nfhl(monkeypatch)
    s = _spec("FEMA_NFHL_ZONES", "fema_nfhl")
    feats = nfhl.delegate(s, {"bbox": [0.0, 0.0, 0.3, 0.2]}, timeout_s=30.0)
    tiles = _FakeNFHL.last["tiles"]
    assert len(tiles) == 6  # 0.3 x 0.2 deg at a 0.1 deg tile
    assert all(x1 - x0 <= nfhl._TILE_DEG and y1 - y0 <= nfhl._TILE_DEG
               for x0, y0, x1, y1 in tiles)
    # id 3 is listed by all six tiles and survives once; ids 1 and 2 by the first.
    assert len(feats) == 3


def test_nfhl_pauses_between_tiles_at_the_measured_pace(monkeypatch):
    """The pause is the pace the service was measured to serve reads at."""
    _stub_nfhl(monkeypatch)
    waits: list[float] = []
    monkeypatch.setattr(nfhl.time, "sleep", waits.append)
    s = _spec("FEMA_NFHL_ZONES", "fema_nfhl")
    nfhl.delegate(s, {"bbox": [0.0, 0.0, 0.3, 0.1]}, timeout_s=30.0)
    assert waits == [nfhl._TILE_PAUSE_S] * 2  # between the three tiles, not before


def test_nfhl_a_refused_tile_is_named_with_what_it_would_have_carried(monkeypatch):
    """A tile the service still refuses is reported, never quietly dropped."""
    from trid3nt_server.workflows.runtime import journal

    _stub_nfhl(monkeypatch, refuse=(2,))
    s = _spec("FEMA_NFHL_ZONES", "fema_nfhl")
    token = journal.bind_notes()
    try:
        feats = nfhl.delegate(s, {"bbox": [0.0, 0.0, 0.3, 0.1]}, timeout_s=30.0)
        notes = journal.drain_notes(token)
    finally:
        journal._NOTES.set(None)
    assert len(feats) == 3  # tiles 1 and 3 still answered
    assert len(notes) == 1
    assert "1 of 3 tiles" in notes[0] and "at least 1 were not" in notes[0]
    assert "[0.1000,0.0000,0.2000,0.1000]" in notes[0]


def test_nfhl_an_aoi_no_tile_answered_is_an_upstream_error(monkeypatch):
    """Nothing read and something lost is a refusal, not an honest-empty layer."""
    _stub_nfhl(monkeypatch, refuse=(1, 2, 3))
    s = _spec("FEMA_NFHL_ZONES", "fema_nfhl")
    with pytest.raises(RouterUpstreamError) as e:
        nfhl.delegate(s, {"bbox": [0.0, 0.0, 0.3, 0.1]}, timeout_s=30.0)
    assert "3 of 3 tiles were refused" in str(e.value)


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
