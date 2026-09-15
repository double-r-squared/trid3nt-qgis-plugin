"""``fetch_nhd_waterbody_at_point``: one seed in, the ONE body it names out.

The envelope is the seed grown past the allowed distance so a refusal can say what was
there; the pick is the body the seed stands in, else the nearest inside the allowance.
Covered with that, the two mirrors' differing column case and the refusals."""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError, RouterInputError
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.hydrology.fetch_nhd_waterbody_at_point import hooks as wb

#: Walden Pond, Concord MA - a small named body the seed can stand in or beside.
_SEED = [-71.3395, 42.4380]


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_nhd_waterbody_at_point"]


def _params(**over):
    return {"seed_point": list(_SEED), "search_distance_km": 2.0, **over}


def _body(*features: dict) -> bytes:
    return json.dumps({"type": "FeatureCollection",
                       "features": list(features)}).encode()


def _pond(lon: float, lat: float, half: float, properties: dict) -> dict:
    return {"type": "Feature", "properties": properties, "geometry": {
        "type": "Polygon", "coordinates": [[
            [lon - half, lat - half], [lon + half, lat - half],
            [lon + half, lat + half], [lon - half, lat + half],
            [lon - half, lat - half]]]}}


_WALDEN = {"gnis_name": "Walden Pond", "gnis_id": "00611885", "ftype": 390,
           "fcode": 39004, "reachcode": "01070006000000",
           "permanent_identifier": "abc123", "areasqkm": 0.255}


def test_the_spec_declares_the_two_mirrors_of_one_dataset(spec):
    assert set(spec.endpoints) == {"data", "medium"}
    assert spec.endpoint_fallback == ["medium"]
    # The mirror chain is first-success, so only one body reaches the parse.
    assert spec.ingest["http_source"]["endpoint_fallback"] is True
    assert spec.output.layer_type == "vector"


def test_a_malformed_seed_refuses_before_the_network(spec):
    with pytest.raises(RouterInputError) as excinfo:
        wb.build_request(spec, _params(seed_point="the pond"))
    assert excinfo.value.error_code == "NHD_WATERBODY_INPUT_INVALID"


def test_the_two_plans_are_the_two_mirrors_high_resolution_first(spec):
    primary, mirror = wb.build_request(spec, _params())
    assert "NHDPlus_HR" in primary.url and "/nhd/MapServer/12/" in mirror.url
    west, south, east, north = (float(v) for v in primary.params["geometry"].split(","))
    assert west < _SEED[0] < east and south < _SEED[1] < north
    # The box looks PAST the allowance, so a refusal can name what stood there.
    assert (north - south) / 2.0 == pytest.approx(
        wb._LOOK_FACTOR * 2.0 / 111.0, rel=1e-6)


def test_a_zero_distance_still_asks_for_a_box_with_area(spec):
    primary, _mirror = wb.build_request(spec, _params(search_distance_km=0.0))
    west, south, east, north = (float(v) for v in primary.params["geometry"].split(","))
    assert (north - south) / 2.0 == pytest.approx(wb._MIN_LOOK_KM / 111.0, rel=1e-6)


def test_a_seed_inside_a_body_takes_it_at_zero_distance(spec):
    rows = wb.parse_response(spec, _params(), [_body(_pond(*_SEED, 0.004, _WALDEN))])
    assert len(rows) == 1
    properties = rows[0]["properties"]
    assert properties["part"] == "waterbody"
    assert properties["gnis_name"] == "Walden Pond"
    assert properties["gnis_id"] == "00611885"
    assert properties["seed_distance_km"] == 0.0


def test_a_seed_inside_nested_outlines_takes_the_smallest(spec):
    rows = wb.parse_response(spec, _params(), [_body(
        _pond(*_SEED, 0.02, {**_WALDEN, "gnis_name": "The reservoir around it"}),
        _pond(*_SEED, 0.004, _WALDEN))])
    assert rows[0]["properties"]["gnis_name"] == "Walden Pond"


def test_a_seed_on_land_takes_the_nearest_body_inside_the_allowance(spec):
    lon, lat = _SEED[0] - 0.01, _SEED[1]
    rows = wb.parse_response(spec, _params(seed_point=[lon, lat]),
                             [_body(_pond(*_SEED, 0.004, _WALDEN))])
    distance = rows[0]["properties"]["seed_distance_km"]
    assert 0.0 < distance < 2.0


def test_a_body_beyond_the_allowance_refuses_naming_it_and_how_far(spec):
    lon, lat = _SEED[0] - 0.01, _SEED[1]
    with pytest.raises(RouterEmptyError) as excinfo:
        wb.parse_response(spec, _params(seed_point=[lon, lat], search_distance_km=0.1),
                          [_body(_pond(*_SEED, 0.004, _WALDEN))])
    message = str(excinfo.value)
    assert excinfo.value.error_code == "NHD_WATERBODY_NO_WATERBODY"
    assert "Walden Pond" in message and "km, beyond the 0.1 km" in message


def test_nothing_mapped_near_the_seed_refuses_by_name(spec):
    with pytest.raises(RouterEmptyError) as excinfo:
        wb.parse_response(spec, _params(), [_body()])
    assert excinfo.value.error_code == "NHD_WATERBODY_NO_WATERBODY"
    assert "no waterbody at all" in str(excinfo.value)


def test_the_mirrors_upper_case_columns_read_the_same(spec):
    upper = {"GNIS_NAME": "Walden Pond", "GNIS_ID": "00611885", "FTYPE": 390,
             "FCODE": 39004, "REACHCODE": "01070006000000",
             "PERMANENT_IDENTIFIER": "abc123", "AREASQKM": 0.255}
    rows = wb.parse_response(spec, _params(), [_body(_pond(*_SEED, 0.004, upper))])
    properties = rows[0]["properties"]
    assert properties["gnis_name"] == "Walden Pond"
    assert properties["permanent_identifier"] == "abc123"
    assert properties["areasqkm"] == 0.255


def test_the_waterbody_surfaces_from_its_own_corpus_phrasings():
    from pathlib import Path

    import yaml

    from trid3nt_server.tools.search.search_tools import search_tools as dd
    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    dd._get_index()
    here = Path(wb.__file__).resolve().parent
    queries = (yaml.safe_load((here / "corpus.yaml").read_text()) or {})[
        "fetch_nhd_waterbody_at_point"]
    assert queries
    assert any("fetch_nhd_waterbody_at_point" in retrieve_visible_tools(q, None, 8)
               for q in queries), (
        "fetch_nhd_waterbody_at_point surfaces in NO top-8 for any of its corpus queries")
