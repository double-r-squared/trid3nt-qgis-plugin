"""``fetch_raws_weather``'s discovery radius: the request bbox grown by search_radius_km.

A RAWS sits on a ridge tens of kilometres from the water a question is about, so the
box stations are LOOKED FOR in is wider than the box asked about, and the stations are
ordered by their distance from the box asked about before the station cap."""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.fetchers._router.errors import RouterEmptyError
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.weather.fetch_raws_weather import hooks as raws

#: The Willamette reach at Portland - a bbox with no RAWS in it at all.
_REACH_BBOX = [-122.68697, 45.515863, -122.66549, 45.539677]


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_raws_weather"]


def _params(**over):
    """Params as the router hands them to a hook: spec defaults already resolved."""
    return {"bbox": list(_REACH_BBOX), "start_time": "2026-09-01",
            "end_time": "2026-09-02", "search_radius_km": 60.0, **over}


def _network(*stations: tuple[str, float, float]) -> bytes:
    return json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": sid,
         "geometry": {"type": "Point", "coordinates": [lon, lat]},
         "properties": {"sname": f"{sid} RAWS", "elevation": 100.0}}
        for sid, lon, lat in stations]}).encode()


def test_the_spec_declares_the_radius_with_a_tens_of_kilometres_default(spec):
    radius = spec.params["search_radius_km"]
    assert radius.default == 60.0
    assert radius.min == 0.0 and radius.max == 200.0


def test_the_discovery_box_is_the_request_bbox_grown_by_the_radius():
    west, south, east, north = raws._discovery_box(_params())
    asked_w, asked_s, asked_e, asked_n = _REACH_BBOX
    assert west < asked_w and east > asked_e
    assert south < asked_s and north > asked_n
    assert (north - asked_n) == pytest.approx(60.0 / 111.0, rel=1e-6)


def test_a_zero_radius_leaves_the_box_as_asked():
    assert raws._discovery_box(_params(search_radius_km=0.0)) == pytest.approx(
        tuple(_REACH_BBOX))


def test_the_radius_widens_which_states_are_asked_at_all(spec):
    # A bbox 20 km inside Oregon: the Washington network only comes into view once
    # the box grows past the state line.
    inland = [-122.7, 45.30, -122.65, 45.32]
    narrow = {p.url for p in raws.resolve_build(
        spec, _params(bbox=inland, search_radius_km=0.0))}
    wide = {p.url for p in raws.resolve_build(spec, _params(bbox=inland))}
    assert narrow < wide
    assert any("WA_DCP" in url for url in wide - narrow)


def test_a_station_outside_the_bbox_but_inside_the_radius_is_kept(spec):
    # Half a degree north of the reach: about 55 km, nothing a 0 km radius sees.
    body = _network(("RIDGE1", _REACH_BBOX[0], _REACH_BBOX[3] + 0.5))
    merged = raws.resolve_parse(spec, _params(), [body])
    assert [st["sid"] for st in merged["_stations"]] == ["RIDGE1"]
    assert merged["_stations"][0]["distance_km"] == pytest.approx(55.5, rel=0.02)


def test_a_station_past_the_radius_is_not_in_view(spec):
    body = _network(("FARAWAY", _REACH_BBOX[0], _REACH_BBOX[3] + 1.5))
    with pytest.raises(RouterEmptyError) as excinfo:
        raws.resolve_parse(spec, _params(), [body])
    # The refusal names the lever that would bring one into view.
    assert "search_radius_km" in str(excinfo.value)
    assert excinfo.value.error_code == "RAWS_WEATHER_EMPTY"


def test_the_stations_are_ordered_by_distance_from_the_bbox_asked_about(spec):
    body = _network(
        ("FAR", _REACH_BBOX[0], _REACH_BBOX[3] + 0.5),
        ("NEAR", _REACH_BBOX[0], _REACH_BBOX[3] + 0.05),
        ("INSIDE", 0.5 * (_REACH_BBOX[0] + _REACH_BBOX[2]),
         0.5 * (_REACH_BBOX[1] + _REACH_BBOX[3])),
    )
    merged = raws.resolve_parse(spec, _params(), [body])
    assert [st["sid"] for st in merged["_stations"]] == ["INSIDE", "NEAR", "FAR"]
    assert merged["_stations"][0]["distance_km"] == 0.0


def test_the_cap_keeps_the_nearest_stations(spec, monkeypatch):
    monkeypatch.setattr(raws, "_MAX_STATIONS", 2)
    body = _network(
        ("FAR", _REACH_BBOX[0], _REACH_BBOX[3] + 0.5),
        ("NEAR", _REACH_BBOX[0], _REACH_BBOX[3] + 0.05),
        ("INSIDE", 0.5 * (_REACH_BBOX[0] + _REACH_BBOX[2]),
         0.5 * (_REACH_BBOX[1] + _REACH_BBOX[3])),
    )
    merged = raws.resolve_parse(spec, _params(), [body])
    assert [st["sid"] for st in merged["_stations"]] == ["INSIDE", "NEAR"]
