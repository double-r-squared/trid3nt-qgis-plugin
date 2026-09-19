"""``fetch_asos_metar``'s discovery radius: the request bbox grown by search_radius_km.

An airport stands where a runway fits rather than on the water a question is
about, so the box stations are LOOKED FOR in is wider than the box asked about.
Offline: the per-state network GeoJSON is stated and nothing reaches the network.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.weather.fetch_asos_metar import hooks as asos

#: The Willamette reach at Portland - a bbox with no ASOS station in it at all.
_REACH_BBOX = [-122.68697, 45.515863, -122.66549, 45.539677]

#: Portland International, 8 km northeast of that reach.
_PDX = ("PDX", -122.5975, 45.5887)


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_asos_metar"]


def _params(**over):
    return {"bbox": list(_REACH_BBOX), "start_time": "2024-01-11",
            "end_time": "2024-01-18", "search_radius_km": 60.0, **over}


def _network(*stations):
    return json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": sid,
         "geometry": {"type": "Point", "coordinates": [lon, lat]},
         "properties": {"sname": sid}}
        for sid, lon, lat in stations]}).encode()


def test_the_spec_declares_the_radius_with_a_tens_of_kilometres_default(spec):
    assert spec.params["search_radius_km"].default == 60.0


def test_a_station_outside_the_asked_box_is_found_inside_the_grown_one(spec):
    found = asos.resolve_parse(spec, _params(), [_network(_PDX)])
    assert found["_station_ids"] == ["PDX"]


def test_a_radius_of_zero_looks_only_in_the_box_asked_about(spec):
    with pytest.raises(RouterEmptyError):
        asos.resolve_parse(spec, _params(search_radius_km=0.0),
                           [_network(_PDX)])
