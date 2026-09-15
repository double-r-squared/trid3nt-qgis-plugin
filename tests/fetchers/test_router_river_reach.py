"""``fetch_river_reach``: two services and two derives, composed into one artifact.

The seed resolves to a COMID before the cache key, the navigate and the bank query
are built together, and the parse clips the centerline, cuts the banks square to it
and emits the four rows. Covered with that, the refusals: a seed off CONUS, a seed
that snaps to nothing, a walk with no flowline and a reach with no mapped water."""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError, RouterInputError
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.hydrology.fetch_river_reach import hooks as rr

#: On the Willamette at Portland, the seed the canary reach is built from.
_SEED = [-122.6735, 45.5175]


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_river_reach"]


def _params(**over):
    return {"seed_point": list(_SEED), "distance_km": 3.0, "direction": "DM",
            "comid": 23815040, **over}


def _flowline_body(lon: float, lat: float, km: float = 6.0) -> bytes:
    """A straight north-running flowline from (lon, lat), digitized downstream."""
    north = lat + km / 111.0
    return json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"nhdplus_comid": 23815040},
         "geometry": {"type": "LineString",
                      "coordinates": [[lon, lat], [lon, north]]}}]}).encode()


def _bank_body(lon: float, lat: float, km: float = 6.0, half_deg: float = 0.002) -> bytes:
    """A straight channel of mapped water around that flowline."""
    north = lat + km / 111.0
    return json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"ftype": 460}, "geometry": {
            "type": "Polygon", "coordinates": [[
                [lon - half_deg, lat - 0.002], [lon + half_deg, lat - 0.002],
                [lon + half_deg, north + 0.002], [lon - half_deg, north + 0.002],
                [lon - half_deg, lat - 0.002]]]}}]}).encode()


def test_the_spec_declares_the_two_services_and_the_water_it_keeps(spec):
    assert set(spec.endpoints) == {"position", "navigate", "banks"}
    # The domain is the WATER, not the structures NHD maps on it.
    assert spec.endpoints["banks"].query["where"] == "FType IN (460, 537)"
    assert spec.output.layer_type == "vector"


def test_a_seed_off_the_network_ground_refuses_by_name(spec):
    with pytest.raises(RouterInputError) as excinfo:
        rr.resolve_build(spec, _params(seed_point=[2.35, 48.85]))
    assert excinfo.value.error_code == "RIVER_REACH_INPUT_INVALID"
    assert "CONUS" in str(excinfo.value)


def test_a_malformed_seed_refuses_before_the_network(spec):
    with pytest.raises(RouterInputError):
        rr.resolve_build(spec, _params(seed_point="downtown"))


def test_the_snap_becomes_the_comid_the_walk_starts_from(spec):
    body = json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"identifier": "23815040"},
         "geometry": None}]}).encode()
    assert rr.resolve_parse(spec, _params(), [body]) == {"comid": 23815040}


def test_a_seed_that_snaps_to_nothing_refuses_by_name(spec):
    body = json.dumps({"type": "FeatureCollection", "features": []}).encode()
    with pytest.raises(RouterInputError) as excinfo:
        rr.resolve_parse(spec, _params(), [body])
    assert excinfo.value.error_code == "RIVER_REACH_INPUT_INVALID"


def test_the_two_plans_are_the_walk_and_the_banks_around_the_seed(spec):
    walk, banks = rr.build_request(spec, _params())
    assert "comid/23815040/navigation/DM/flowlines" in walk.url
    assert walk.params["distance"] == "3"
    west, south, east, north = (float(v) for v in banks.params["geometry"].split(","))
    assert west < _SEED[0] < east and south < _SEED[1] < north
    # The envelope has to CONTAIN a walk of the asked length, in both axes.
    assert (north - south) / 2.0 > 3.0 / 111.0
    assert banks.params["where"] == "FType IN (460, 537)"


def test_the_four_rows_are_the_domain_its_two_runs_and_the_centerline(spec):
    rows = rr.parse_response(spec, _params(), [
        _flowline_body(*_SEED), _bank_body(*_SEED)])
    by_part = {row["properties"]["part"]: row for row in rows}
    assert sorted(by_part) == ["centerline", "inflow", "outflow", "reach"]
    assert by_part["reach"]["geometry"]["type"] == "Polygon"
    for part in ("inflow", "outflow"):
        assert len(by_part[part]["geometry"]["coordinates"]) == 2
    assert by_part["reach"]["properties"]["reach_km"] == pytest.approx(3.0, abs=0.05)
    assert by_part["reach"]["properties"]["area_km2"] > 0.0
    # The runs are in FLOW order: going downstream, the inflow is at the seed.
    assert by_part["inflow"]["geometry"]["coordinates"][0][1] < \
        by_part["outflow"]["geometry"]["coordinates"][0][1]


def test_going_upstream_puts_the_outflow_at_the_seed(spec):
    lon, lat = _SEED
    # An upstream walk returns the channel ABOVE the seed; the seed is its foot.
    rows = rr.parse_response(spec, _params(direction="UM"), [
        _flowline_body(lon, lat - 6.0 / 111.0), _bank_body(lon, lat - 6.0 / 111.0)])
    by_part = {row["properties"]["part"]: row for row in rows}
    assert by_part["outflow"]["geometry"]["coordinates"][0][1] > \
        by_part["inflow"]["geometry"]["coordinates"][0][1]


def test_a_walk_with_no_flowline_refuses_by_name(spec):
    empty = json.dumps({"type": "FeatureCollection", "features": []}).encode()
    with pytest.raises(RouterEmptyError) as excinfo:
        rr.parse_response(spec, _params(), [empty, _bank_body(*_SEED)])
    assert excinfo.value.error_code == "RIVER_REACH_EMPTY"


def test_a_channel_nhd_maps_no_water_surface_for_refuses_by_name(spec):
    empty = json.dumps({"type": "FeatureCollection", "features": []}).encode()
    with pytest.raises(RouterEmptyError) as excinfo:
        rr.parse_response(spec, _params(), [_flowline_body(*_SEED), empty])
    assert "centreline only" in str(excinfo.value)


def test_a_reach_whose_banks_do_not_reach_its_ends_refuses_rather_than_guessing(spec):
    # The mapped water stops 1 km short of the walk's downstream end.
    rows_body = _bank_body(*_SEED, km=1.0)
    with pytest.raises(RouterEmptyError) as excinfo:
        rr.parse_response(spec, _params(), [_flowline_body(*_SEED), rows_body])
    assert excinfo.value.error_code == "RIVER_REACH_EMPTY"


def test_the_reach_surfaces_from_its_own_corpus_phrasings():
    from pathlib import Path

    import yaml

    from trid3nt_server.tools.search.search_tools import search_tools as dd
    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    dd._get_index()
    here = Path(rr.__file__).resolve().parent
    queries = (yaml.safe_load((here / "corpus.yaml").read_text()) or {})["fetch_river_reach"]
    assert queries
    assert any("fetch_river_reach" in retrieve_visible_tools(q, None, 8) for q in queries), (
        "fetch_river_reach surfaces in NO top-8 for any of its corpus queries")
