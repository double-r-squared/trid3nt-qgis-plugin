"""``derive_water_polygon``: a box and a coastline -> the water it leaves.

A coastline is a line and a mesh is cut from a polygon; this is the step between
them. What is proved is the classification - OSM draws land on the LEFT of the
way's direction - and the two refusals that keep a guess out of a domain.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools.derive.derive_water_polygon.derive_water_polygon import (
    WaterPolygonError,
    derive_water_polygon,
)

_BOX = [-70.0, 42.0, -69.0, 43.0]

#: A north-south coast through the middle of the box. Drawn SOUTH to NORTH, so
#: the land is on its LEFT - the WEST half - and the water is the east half.
_WEST_IS_LAND = {"type": "LineString",
                 "coordinates": [[-69.5, 41.9], [-69.5, 43.1]]}
#: The same coast drawn the other way: the land flips with it.
_EAST_IS_LAND = {"type": "LineString",
                 "coordinates": [[-69.5, 43.1], [-69.5, 41.9]]}


def _water(coastline, tmp_path, extent=None):
    return derive_water_polygon(coastline, extent or _BOX,
                                _output_dir=str(tmp_path))


def _geometry(layer):
    return json.load(open(layer.uri))["features"][0]["geometry"]


def _closed(geometry):
    rings = (geometry["coordinates"] if geometry["type"] == "Polygon"
             else [r for part in geometry["coordinates"] for r in part])
    return all(ring[0] == ring[-1] for ring in rings)


def test_the_water_is_the_side_the_way_does_not_have_its_land_on(tmp_path):
    layer = _water(_WEST_IS_LAND, tmp_path)
    minx, _miny, maxx, _maxy = layer.bbox
    assert (minx, maxx) == pytest.approx((-69.5, -69.0))
    assert layer.water_fraction == pytest.approx(0.5, abs=0.01)
    assert layer.n_parts == 1 and layer.n_ways == 1


def test_reversing_the_way_moves_the_water_to_the_other_side(tmp_path):
    """The DIRECTION is the datum: a reader that reordered the vertices would
    swap land for water without saying anything."""
    layer = _water(_EAST_IS_LAND, tmp_path)
    minx, _miny, maxx, _maxy = layer.bbox
    assert (minx, maxx) == pytest.approx((-70.0, -69.5))


def test_the_cut_comes_back_as_a_polygon_a_mesh_can_be_built_from(tmp_path):
    geometry = _geometry(_water(_WEST_IS_LAND, tmp_path))
    assert geometry["type"] in ("Polygon", "MultiPolygon")
    assert _closed(geometry)


def test_the_water_polygon_fills_the_domain_slot_as_it_stands(tmp_path):
    """The whole point of the cut: what leaves is a DOMAIN, ingested like any
    other - drawn, fetched, or produced."""
    from trid3nt_server.inputs.domain import domain

    ingested = domain(_water(_WEST_IS_LAND, tmp_path).uri)
    assert ingested.geometry["type"] in ("Polygon", "MultiPolygon")
    assert ingested.runs == ()


def test_a_way_that_ends_inside_the_box_divides_nothing_and_refuses(tmp_path):
    stub = {"type": "LineString", "coordinates": [[-69.5, 41.9], [-69.5, 42.5]]}
    with pytest.raises(WaterPolygonError) as exc:
        _water(stub, tmp_path)
    assert exc.value.error_code == "WATER_POLYGON_DOES_NOT_CLOSE"


def test_no_coastline_over_this_box_is_named_rather_than_guessed(tmp_path):
    with pytest.raises(WaterPolygonError) as exc:
        _water({"type": "FeatureCollection", "features": []}, tmp_path)
    assert exc.value.error_code == "WATER_POLYGON_NO_COASTLINE"


def test_a_coastline_that_misses_the_box_leaves_it_uncut(tmp_path):
    away = {"type": "LineString", "coordinates": [[-60.0, 41.9], [-60.0, 43.1]]}
    with pytest.raises(WaterPolygonError) as exc:
        _water(away, tmp_path)
    assert exc.value.error_code == "WATER_POLYGON_NO_COASTLINE"


def test_the_extent_can_be_the_rectangle_the_user_drew(tmp_path):
    """A drawn box is a shape, not four numbers, and its bounds are the box."""
    drawn = {"type": "Polygon", "coordinates": [[
        [-70.0, 42.0], [-69.0, 42.0], [-69.0, 43.0], [-70.0, 43.0],
        [-70.0, 42.0]]]}
    layer = _water(_WEST_IS_LAND, tmp_path, extent=drawn)
    assert layer.water_fraction == pytest.approx(0.5, abs=0.01)


def test_an_extent_that_encloses_no_area_refuses(tmp_path):
    with pytest.raises(WaterPolygonError) as exc:
        _water(_WEST_IS_LAND, tmp_path, extent=[-70.0, 42.0, -70.0, 42.0])
    assert exc.value.error_code == "WATER_POLYGON_NO_BOX"
