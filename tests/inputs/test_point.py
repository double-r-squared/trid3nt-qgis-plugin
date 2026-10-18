"""A Point is ingested once, from every form a user names one in, and read typed.

The pick, the pair, the "lat,lon" string and the point layer all land on the same
value and a place name refuses; where a point is allowed to be - inside the domain,
on the river, in water - is answered here for any slot that holds one."""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server.inputs.point import (
    Point,
    PointNotViableError,
    point,
    point_arg,
    require_viable,
)
from trid3nt_server.inputs.user_input import UserInputError


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


def test_a_place_name_refuses_and_names_the_geocoder():
    with pytest.raises(UserInputError) as ei:
        _ingest("Twin Falls, Idaho", label="release",
                code="TELEMAC_PARAMS_INVALID")
    assert ei.value.error_code == "TELEMAC_PARAMS_INVALID"
    assert ei.value.retryable is True
    assert "geocode_location(query='Twin Falls, Idaho')" in str(ei.value)


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
    from trid3nt_server.inputs.gate import draw_input

    async def _never(**_kw):
        raise AssertionError("auto mode asks nobody")

    monkeypatch.setattr(draw_input, "gate_draw_input", _never)
    coerce = point_arg("release", tool="t", prompt="click")
    out = asyncio.run(coerce({"release": [-114.31, 42.58], "input_mode": "auto"}))
    assert out == {"release": Point(-114.31, 42.58)}
    assert asyncio.run(coerce({"input_mode": "auto"})) == {"release": None}


def test_the_coercion_asks_the_canvas_only_when_gated_live_and_empty(monkeypatch):
    from trid3nt_server.render import pipeline_emitter
    from trid3nt_server.inputs.gate import draw_input

    asked: list[tuple[str, str]] = []

    async def _gate(*, tool_name, param, geometry, prompt, **_kw):
        asked.append((tool_name, param))
        assert geometry == "point" and prompt == "click"
        return draw_input.DrawOutcome(value=Point(-114.31, 42.58, "point-1"))

    monkeypatch.setattr(draw_input, "gate_draw_input", _gate)
    monkeypatch.setattr(pipeline_emitter, "current_emitter", lambda: object())
    coerce = point_arg("release", tool="telemac_dye_release", prompt="click")
    out = asyncio.run(coerce({"input_mode": "user_gated"}))
    assert out == {"release": Point(-114.31, 42.58, "point-1")}
    assert asked == [("telemac_dye_release", "release")]
    # a value on the wire is never second-guessed by a card
    asyncio.run(coerce({"release": [-1.0, 1.0], "input_mode": "user_gated"}))
    assert len(asked) == 1


def test_a_point_and_a_drawn_slot_ask_through_the_one_canvas_ask(monkeypatch):
    from trid3nt_server.inputs import slots

    asked: list[tuple[str, str]] = []

    async def _draw(geometry, *, tool, param, **_kw):
        asked.append((geometry, param))
        return None

    monkeypatch.setattr(slots, "draw_on_canvas", _draw)
    coerce = point_arg("release", tool="t", prompt="click")
    assert asyncio.run(coerce({"input_mode": "user_gated"})) == {"release": None}
    assert asyncio.run(slots.ask_on_canvas(slots.DOMAIN, tool="t", param="domain")) is None
    assert asked == [("point", "release"), ("polygon", "domain")]


def test_a_point_with_no_prompt_is_read_and_never_asked_for(monkeypatch):
    from trid3nt_server.inputs import slots

    async def _draw(geometry, *, tool, param, **_kw):
        raise AssertionError("a point the caller may leave unstated is never asked for")

    monkeypatch.setattr(slots, "draw_on_canvas", _draw)
    coerce = point_arg("well", tool="t", prompt=None)
    assert asyncio.run(coerce({"input_mode": "user_gated"})) == {"well": None}
    assert asyncio.run(coerce({"well": [-1.0, 1.0], "input_mode": "user_gated"})) \
        == {"well": Point(-1.0, 1.0)}


def test_a_declined_pick_leaves_the_slot_empty(monkeypatch):
    from trid3nt_server.render import pipeline_emitter
    from trid3nt_server.inputs.gate import draw_input

    async def _gate(**_kw):
        return draw_input.DrawOutcome(reason="the drawing was cancelled")

    monkeypatch.setattr(draw_input, "gate_draw_input", _gate)
    monkeypatch.setattr(pipeline_emitter, "current_emitter", lambda: object())
    coerce = point_arg("release", tool="t", prompt="click")
    assert asyncio.run(coerce({"input_mode": "user_gated"})) == {"release": None}


# -- the shared viability check: a point is checked, never moved ------------------ #

#: Four sites 100 m apart along a line in a projected zone (UTM 11N near Twin
#: Falls, Idaho). The engine reads a point at the site nearest it.
_EPSG = 32611
_SITES = [[700000.0, 4716000.0], [700100.0, 4716000.0], [700200.0, 4716000.0],
          [700300.0, 4716000.0]]


def _lonlat(x, y):
    from pyproj import Transformer

    lon, lat = Transformer.from_crs(_EPSG, 4326, always_xy=True).transform(x, y)
    return Point(lon, lat, "release")


def _nearest(xy):
    import numpy as np

    sites = np.asarray(_SITES)
    return int(np.argmin(np.hypot(sites[:, 0] - xy[0], sites[:, 1] - xy[1])))


def _check(pt, viable, region=None):
    return require_viable(pt, label="release", utm_epsg=_EPSG, sites_xy=_SITES,
                          viable=viable, resolve=_nearest, region_m=region,
                          missed="dry at t0")


def test_a_viable_point_returns_the_site_the_engine_reads_and_is_not_moved():
    pt = _lonlat(700110.0, 4716000.0)
    assert _check(pt, [True] * 4) == 1


def test_a_point_on_a_dry_site_refuses_with_the_nearest_viable_site_as_a_hint():
    with pytest.raises(PointNotViableError) as caught:
        _check(_lonlat(700110.0, 4716000.0), [True, False, False, True])
    err = caught.value
    assert err.error_code == "POINT_NOT_VIABLE" and err.retryable is True
    assert "dry at t0 (site 1)" in str(err) and "was not moved" in str(err)
    assert err.hint["distance_m"] == pytest.approx(110.0, abs=0.5)


def test_a_point_outside_the_region_refuses_before_any_site_is_read():
    from shapely.geometry import box

    region = box(700000.0, 4715900.0, 700300.0, 4716100.0)
    with pytest.raises(PointNotViableError) as caught:
        _check(_lonlat(701000.0, 4716000.0), [True] * 4, region=region)
    assert "outside the domain" in str(caught.value)
    assert caught.value.hint["distance_m"] == pytest.approx(700.0, abs=0.5)


def test_with_no_viable_site_anywhere_the_refusal_carries_no_hint():
    with pytest.raises(PointNotViableError) as caught:
        _check(_lonlat(700110.0, 4716000.0), [False] * 4)
    assert caught.value.hint is None
