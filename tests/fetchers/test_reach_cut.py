"""The reach cut: the mapped water surface sectioned square to the walked line.

Everything is geometry authored in the test, so there is no world read to stub.
What is checked is the CUT - that the two end faces are square to the line joining
the walk's ends, that a disconnected piece the line misses is dropped, and that a
cut with nothing to measure refuses rather than inventing a transect."""

from __future__ import annotations

import pytest

from trid3nt_server.tools.fetchers._router.errors import (
    RouterEmptyError, RouterUpstreamError)
from trid3nt_server.tools.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.tools.fetchers.hydrology.fetch_river_reach.hooks import _cut_reach

#: A 0.2 x 0.02 degree band running east along lat 35 - the mapped banks of a
#: river, wide enough that a section of it has real area.
_BANK = {"type": "Polygon", "coordinates": [[
    [-83.50, 34.99], [-83.30, 34.99], [-83.30, 35.01], [-83.50, 35.01],
    [-83.50, 34.99]]]}

_UPSTREAM = [-83.45, 35.0]
_DOWNSTREAM = [-83.40, 35.0]


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_river_reach"]


def test_the_cut_keeps_only_the_water_between_the_two_ends(spec):
    geom, props = _cut_reach(spec, _BANK, [_UPSTREAM, _DOWNSTREAM])
    # The band is 0.20 deg long and the reach 0.05 deg of it: a quarter.
    assert props["source_area_km2"] == pytest.approx(props["area_km2"] * 4.0, rel=0.02)
    assert props["utm_epsg"] == 32617
    assert geom["type"] == "Polygon"


def test_each_end_face_spans_the_water_across_the_reach(spec):
    """The face is the whole transect the cut left, not the part a probe line caught.

    The cut edge is exactly collinear with such a line, so over a domain-sized probe
    the intersection comes back whole at one end and EMPTY at the other."""
    _geom, props = _cut_reach(spec, _BANK, [_UPSTREAM, _DOWNSTREAM])
    for face, lon in ((props["face_start"], _UPSTREAM[0]),
                      (props["face_end"], _DOWNSTREAM[0])):
        assert len(face) == 2
        assert all(point[0] == pytest.approx(lon, abs=1e-4) for point in face)
        # the band is 0.02 deg tall and the face crosses all of it
        assert abs(face[0][1] - face[1][1]) == pytest.approx(0.02, abs=1e-6)


def test_the_cut_is_square_to_the_walk_not_to_the_meridian(spec):
    # A reach between two points at the SAME latitude is cut on meridians; one
    # between points on a diagonal is not, and its corners must move with the
    # line rather than stay north-south.
    from shapely.geometry import shape

    straight = shape(_cut_reach(spec, _BANK, [_UPSTREAM, _DOWNSTREAM])[0]).bounds
    slanted = shape(_cut_reach(
        spec, _BANK, [[-83.45, 34.995], [-83.40, 35.005]])[0]).bounds
    assert slanted[0] < straight[0]
    assert slanted[2] > straight[2]


def test_a_piece_the_walk_never_touches_is_left_out(spec):
    # Two parallel bands: the line runs down the southern one only, so the
    # northern piece falls inside the end cuts without being the reach.
    two = {"type": "MultiPolygon", "coordinates": [
        [[[-83.50, 34.99], [-83.30, 34.99], [-83.30, 35.01], [-83.50, 35.01],
          [-83.50, 34.99]]],
        [[[-83.50, 35.20], [-83.30, 35.20], [-83.30, 35.22], [-83.50, 35.22],
          [-83.50, 35.20]]]]}
    from shapely.geometry import shape

    geom, props = _cut_reach(spec, two, [_UPSTREAM, _DOWNSTREAM])
    _one, only = _cut_reach(spec, _BANK, [_UPSTREAM, _DOWNSTREAM])
    assert shape(geom).geom_type == "Polygon"
    assert props["area_km2"] == pytest.approx(only["area_km2"])


def test_an_end_the_cut_never_reached_refuses_rather_than_naming_a_bank(spec):
    """A downstream end PAST the mapped band: the cut there falls off the water,
    so the reach stops on its own edge and there is no transect at that end to
    prescribe an outflow across."""
    with pytest.raises(RouterEmptyError) as excinfo:
        _cut_reach(spec, _BANK, [_UPSTREAM, [-83.25, 35.0]])
    assert "outflow" in str(excinfo.value)


def test_a_walk_off_the_mapped_water_refuses(spec):
    with pytest.raises(RouterEmptyError):
        _cut_reach(spec, _BANK, [[-90.0, 35.0], [-89.9, 35.0]])


def test_one_point_twice_has_no_direction_to_be_square_to(spec):
    with pytest.raises(RouterEmptyError) as excinfo:
        _cut_reach(spec, _BANK, [_UPSTREAM, _UPSTREAM])
    assert "one point" in str(excinfo.value)


def test_a_centreline_only_reach_has_no_banks_to_solve_between(spec):
    # THE ruling: a flowline is not banks. Nothing widens it here.
    flowline = {"type": "LineString",
                "coordinates": [[-83.50, 35.0], [-83.30, 35.0]]}
    with pytest.raises(RouterEmptyError) as excinfo:
        _cut_reach(spec, flowline, [_UPSTREAM, _DOWNSTREAM])
    assert "no water SURFACE" in str(excinfo.value)


def test_water_that_cannot_be_read_is_an_upstream_fault(spec, tmp_path):
    with pytest.raises(RouterUpstreamError):
        _cut_reach(spec, str(tmp_path / "nothing.fgb"), [_UPSTREAM, _DOWNSTREAM])
