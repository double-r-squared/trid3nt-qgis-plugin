"""A Point is ingested once, from every form a user names one in, and read typed.

The pick, the pair, the "lat,lon" string, the point layer and the geocoded place
all land on the same value; where a point is allowed to be - inside the domain,
on the river, in water - is answered here for any slot that holds one."""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server.workflows.inputs import point as point_mod
from trid3nt_server.workflows.inputs.point import (
    Point,
    PointOutsideDomainError,
    contain,
    point,
    point_arg,
    snap_to_wet,
)
from trid3nt_server.workflows.runtime.user_input import UserInputError


def _ingest(value, **kw):
    return asyncio.run(point(value, **kw))


# -- the five sources ---------------------------------------------------------- #

def test_a_pick_carries_its_coordinates_and_its_name():
    got = _ingest({"coordinates": [-114.31, 42.58], "name": "outfall-a"})
    assert got == Point(-114.31, 42.58, "outfall-a")


def test_a_pair_is_lon_lat_with_no_name():
    assert _ingest([-114.31, 42.58]) == Point(-114.31, 42.58)
    assert _ingest((-114.31, 42.58)) == Point(-114.31, 42.58)


def test_a_typed_string_is_spoken_latitude_first():
    assert _ingest("42.58,-114.31") == Point(-114.31, 42.58)
    assert _ingest("42.58, -114.31") == Point(-114.31, 42.58)


def test_a_geojson_feature_reads_its_geometry_and_its_name_property():
    got = _ingest({"type": "Feature", "properties": {"name": "gauge 3"},
                   "geometry": {"type": "Point", "coordinates": [-114.31, 42.58]}})
    assert got == Point(-114.31, 42.58, "gauge 3")
    assert _ingest({"lon": -114.31, "lat": 42.58}) == Point(-114.31, 42.58)


def test_a_selected_point_layer_gives_its_first_point_and_its_name(tmp_path):
    path = tmp_path / "gauges.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "USGS 13090500"},
         "geometry": {"type": "Point", "coordinates": [-114.31, 42.58]}}]}))
    assert _ingest(str(path)) == Point(-114.31, 42.58, "USGS 13090500")


def test_a_layer_with_no_point_feature_refuses_by_name(tmp_path):
    path = tmp_path / "banks.geojson"
    path.write_text(json.dumps({"type": "LineString",
                                "coordinates": [[-114.34, 42.58], [-114.28, 42.58]]}))
    with pytest.raises(UserInputError) as ei:
        _ingest(str(path), label="release", code="TELEMAC_PARAMS_INVALID")
    assert ei.value.error_code == "TELEMAC_PARAMS_INVALID"
    assert "no point feature" in str(ei.value)


def test_a_place_name_is_geocoded_and_keeps_the_name(monkeypatch):
    from trid3nt_server.workflows.inputs import aoi

    async def _geo(name):
        assert name == "Twin Falls, Idaho"
        return (-114.4609, 42.5629)

    monkeypatch.setattr(aoi, "geocode_place", _geo)
    assert _ingest("Twin Falls, Idaho") == Point(-114.4609, 42.5629, "Twin Falls, Idaho")


def test_a_place_the_geocoder_does_not_know_refuses(monkeypatch):
    from trid3nt_server.workflows.inputs import aoi

    async def _geo(_name):
        return None

    monkeypatch.setattr(aoi, "geocode_place", _geo)
    with pytest.raises(UserInputError) as ei:
        _ingest("Nowhere In Particular", label="release")
    assert "could not be geocoded" in str(ei.value)


def test_nothing_and_a_point_pass_through():
    assert _ingest(None) is None
    assert _ingest(Point(1.0, 2.0, "x")) == Point(1.0, 2.0, "x")


@pytest.mark.parametrize("bad", [[200.0, 10.0], {"name": "no coords"}, ["a", "b"]])
def test_a_value_of_no_readable_shape_refuses_under_the_callers_code(bad):
    with pytest.raises(UserInputError) as ei:
        _ingest(bad, label="release", code="TELEMAC_PARAMS_INVALID")
    assert ei.value.error_code == "TELEMAC_PARAMS_INVALID"


# -- the pick-or-wire coercion ---------------------------------------------------- #

def test_the_coercion_reads_the_wire_value_and_never_asks_in_auto(monkeypatch):
    async def _never(**_kw):
        raise AssertionError("auto mode asks nobody")

    monkeypatch.setattr(point_mod, "_pick", _never)
    coerce = point_arg("release", tool="t", prompt="click")
    out = asyncio.run(coerce({"release": [-114.31, 42.58], "input_mode": "auto"}))
    assert out == {"release": Point(-114.31, 42.58)}
    assert asyncio.run(coerce({"input_mode": "auto"})) == {"release": None}


def test_the_coercion_asks_the_canvas_only_when_gated_live_and_empty(monkeypatch):
    from trid3nt_server.emission import pipeline_emitter
    from trid3nt_server.gates import draw_input

    asked: list[tuple[str, str]] = []

    async def _gate(*, tool_name, param, geometry, prompt, **_kw):
        asked.append((tool_name, param))
        assert geometry == "point" and prompt == "click"
        return draw_input.DrawOutcome(value=Point(-114.31, 42.58, "point-1"))

    monkeypatch.setattr(draw_input, "gate_draw_input", _gate)
    monkeypatch.setattr(pipeline_emitter, "current_emitter", lambda: object())
    coerce = point_arg("release", tool="telemac_river_dye", prompt="click")
    out = asyncio.run(coerce({"input_mode": "user_gated"}))
    assert out == {"release": Point(-114.31, 42.58, "point-1")}
    assert asked == [("telemac_river_dye", "release")]
    # a value on the wire is never second-guessed by a card
    asyncio.run(coerce({"release": [-1.0, 1.0], "input_mode": "user_gated"}))
    assert len(asked) == 1


def test_a_declined_pick_leaves_the_slot_empty(monkeypatch):
    from trid3nt_server.emission import pipeline_emitter
    from trid3nt_server.gates import draw_input

    async def _gate(**_kw):
        return draw_input.DrawOutcome(reason="the drawing was cancelled")

    monkeypatch.setattr(draw_input, "gate_draw_input", _gate)
    monkeypatch.setattr(pipeline_emitter, "current_emitter", lambda: object())
    coerce = point_arg("release", tool="t", prompt="click")
    assert asyncio.run(coerce({"input_mode": "user_gated"})) == {"release": None}


# -- containment in a domain, on the river ------------------------------------------ #

# A ~0.02 deg square of "water" near Twin Falls, Idaho, with a flowline running
# west-east through its middle. Inline GeoJSON so nothing is read or fetched.
_DOMAIN = json.dumps({
    "type": "FeatureCollection",
    "features": [{"type": "Feature", "properties": {}, "geometry": {
        "type": "Polygon",
        "coordinates": [[[-114.33, 42.57], [-114.29, 42.57],
                         [-114.29, 42.59], [-114.33, 42.59],
                         [-114.33, 42.57]]]}}]})

_FLOWLINE = json.dumps({
    "type": "FeatureCollection",
    "features": [{"type": "Feature", "properties": {}, "geometry": {
        "type": "LineString",
        "coordinates": [[-114.34, 42.58], [-114.31, 42.58], [-114.28, 42.58]]}}]})


def test_a_point_on_the_flowline_inside_the_domain_is_honored_unmoved():
    got, moved = contain(Point(-114.31, 42.58, "a"), domain=_DOMAIN,
                         flowline=_FLOWLINE)
    assert got.lon == pytest.approx(-114.31, abs=1e-5)
    assert got.lat == pytest.approx(42.58, abs=1e-5)
    assert got.name == "a" and moved < 1.0


def test_a_point_inside_the_domain_but_off_the_river_is_moved_onto_it():
    got, moved = contain(Point(-114.31, 42.585), domain=_DOMAIN, flowline=_FLOWLINE)
    assert got.lon == pytest.approx(-114.31, abs=1e-4)
    assert got.lat == pytest.approx(42.58, abs=1e-4)
    assert 400.0 < moved < 700.0  # ~0.005 deg of latitude


def test_a_point_outside_the_domain_is_refused_by_name_and_names_the_fix():
    with pytest.raises(PointOutsideDomainError) as ei:
        contain(Point(-114.26, 42.58), domain=_DOMAIN, flowline=_FLOWLINE,
                label="release")
    err = ei.value
    assert err.error_code == "POINT_OUTSIDE_DOMAIN"
    assert err.retryable is True and len(err.suggestions) == 3
    assert err.distance_m > 1000.0
    assert "release" in str(err) and "not inside the domain polygon" in str(err)


def test_the_move_never_leaves_the_domain_by_following_the_river_out_of_it():
    got, _ = contain(Point(-114.2905, 42.5895), domain=_DOMAIN, flowline=_FLOWLINE)
    assert -114.33 <= got.lon <= -114.29
    assert got.lat == pytest.approx(42.58, abs=1e-4)


def test_a_flowline_that_misses_the_domain_refuses_rather_than_reaching_for_it():
    away = json.dumps({"type": "LineString",
                       "coordinates": [[-114.20, 42.58], [-114.18, 42.58]]})
    with pytest.raises(UserInputError) as ei:
        contain(Point(-114.31, 42.58), domain=_DOMAIN, flowline=away)
    assert ei.value.error_code == "FLOWLINE_OUTSIDE_DOMAIN"


def test_a_domain_source_carrying_no_polygon_refuses():
    line_only = json.dumps({"type": "LineString",
                            "coordinates": [[-114.33, 42.58], [-114.29, 42.58]]})
    with pytest.raises(UserInputError) as ei:
        contain(Point(-114.31, 42.58), domain=line_only, flowline=_FLOWLINE)
    assert ei.value.error_code == "DOMAIN_NO_POLYGON"


# -- the move onto water --------------------------------------------------------- #

#: Four nodes 100 m apart along one bank line. The engine solves a source at the
#: node nearest it, so these are the only places a point can be.
_NODES = [[0.0, 0.0], [100.0, 0.0], [200.0, 0.0], [300.0, 0.0]]


def _snap(xy, wet):
    return snap_to_wet(xy, node_xy=_NODES, wet=wet, state="a stand-in initial state")


def test_a_point_landing_on_a_wet_node_is_left_exactly_where_it_was():
    where, moved, node = _snap((110.0, 0.0), [True] * 4)
    assert where == (110.0, 0.0) and moved == 0.0 and node == 1


def test_a_point_landing_on_a_dry_node_moves_to_the_nearest_wet_one():
    where, moved, node = _snap((110.0, 0.0), [True, False, False, True])
    assert node == 0 and where == (0.0, 0.0)
    assert moved == pytest.approx(110.0)


def test_an_initial_state_with_no_wet_node_anywhere_refuses_typed():
    with pytest.raises(UserInputError) as ei:
        _snap((110.0, 0.0), [False] * 4)
    assert ei.value.error_code == "POINT_NOWHERE_WET"
    assert "10 m away and DRY" in str(ei.value)
    assert "a stand-in initial state" in str(ei.value)
