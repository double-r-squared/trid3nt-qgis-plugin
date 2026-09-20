"""The domain slot's own cut: a LAND-WATER EDGE arrives, a polygon leaves.

A coastline is a line and a domain is the closed outline the equations are
solved over, so the step between them is this slot's ingestion - the mesh
seam's cut, run where the line arrived. What is proved here is that the slot
runs it, that it reads the window the question was asked in, and that it
refuses typed where there is no window or the line divides nothing.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.inputs.domain import domain
from trid3nt_server.inputs.user_input import UserInputError

_BOX = (-70.0, 42.0, -69.0, 43.0)

#: A north-south coast through the middle of the box, drawn SOUTH to NORTH, so
#: OSM's convention puts the land on its LEFT - the west half - and the water
#: is the east half.
_COAST = {"type": "LineString", "coordinates": [[-69.5, 41.9], [-69.5, 43.1]]}


def _bounds(geometry):
    from shapely.geometry import shape

    return shape(geometry).bounds


def test_a_coastline_handed_to_the_domain_slot_is_cut_into_the_water_it_leaves():
    cut = domain(_COAST, extent=_BOX)
    minx, _miny, maxx, _maxy = _bounds(cut.geometry)
    assert cut.geometry["type"] in ("Polygon", "MultiPolygon")
    assert (minx, maxx) == pytest.approx((-69.5, -69.0))


def test_the_cut_runs_on_a_coastline_layer_the_match_fetched(tmp_path):
    """The match hands the slot a LAYER, not inline geometry, and the slot reads
    every form the same."""
    path = tmp_path / "coastline.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"natural": "coastline"},
         "geometry": _COAST}]}))
    cut = domain(str(path), extent=_BOX)
    minx, _miny, maxx, _maxy = _bounds(cut.geometry)
    assert (minx, maxx) == pytest.approx((-69.5, -69.0))


def test_the_window_is_what_the_water_is_cut_out_of():
    """Half the box is a different domain from a quarter of it: the slot cuts
    against the window it was told, never against the line's own span."""
    narrow = domain(_COAST, extent=(-69.8, 42.0, -69.2, 43.0))
    minx, _miny, maxx, _maxy = _bounds(narrow.geometry)
    assert (minx, maxx) == pytest.approx((-69.5, -69.2))


def test_a_line_with_no_window_refuses_rather_than_guessing_a_box():
    with pytest.raises(UserInputError, match="no window to cut them against"):
        domain(_COAST)


def test_a_cut_that_does_not_close_refuses_in_the_cuts_own_words():
    """A way that ENDS inside the box divides nothing; the slot carries the
    cut's own code rather than renaming the refusal."""
    stub = {"type": "LineString", "coordinates": [[-69.5, 41.9], [-69.5, 42.5]]}
    with pytest.raises(UserInputError) as exc:
        domain(stub, extent=_BOX)
    assert exc.value.error_code == "MESH_WATER_DOES_NOT_CLOSE"


def test_a_polygon_is_still_read_as_the_polygon_it_is():
    """The cut is the LINE's path only: an outline the user drew, or a fetched
    body, reads exactly as before and nothing is cut."""
    ring = [[-70.0, 42.0], [-69.0, 42.0], [-69.0, 43.0], [-70.0, 43.0]]
    drawn = domain({"type": "Polygon", "coordinates": [[*ring, ring[0]]]},
                   extent=(-69.8, 42.0, -69.2, 43.0))
    assert _bounds(drawn.geometry) == pytest.approx((-70.0, 42.0, -69.0, 43.0))


def test_the_lines_the_cut_consumed_are_not_companions_of_the_water():
    """A companion is a geometry the producer measured BESIDE the polygon; the
    edge a polygon was cut FROM is not one, and a mesher handed the domain reads
    the water alone."""
    cut = domain({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "Point Judith"},
         "geometry": _COAST}]}, extent=_BOX)
    assert cut.companions == {}
    parts = cut.as_feature_collection()["features"]
    assert [f["geometry"]["type"] for f in parts] == [cut.geometry["type"]]
