"""Offline tests for ``section``: the generic polygon cut.

Everything here is shapely and pyproj on geometry authored in the test, so there
is no world read to stub: the tool is handed inline GeoJSON and writes into a
tmp dir. What is checked is the CUT - that the two end faces are square to the
line joining the points, that a disconnected piece the line misses is dropped
and SAID to be dropped, and that every way the ask can be wrong refuses by code
rather than returning a shape nobody asked for.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing.section.section import SectionError, section

#: A 0.2 x 0.02 degree band running east along lat 35 - a stand-in for the mapped
#: banks of a river, wide enough that a section of it has real area.
_BANK = json.dumps({"type": "Polygon", "coordinates": [[
    [-83.50, 34.99], [-83.30, 34.99], [-83.30, 35.01], [-83.50, 35.01],
    [-83.50, 34.99]]]})

_UPSTREAM = (-83.45, 35.0)
_DOWNSTREAM = (-83.40, 35.0)


def test_section_is_registered_under_its_own_name():
    assert "section" in TOOL_REGISTRY
    assert TOOL_REGISTRY["section"].fn.__name__ == "section"


def test_the_ribbon_producer_is_gone_from_the_registry():
    # A buffered flowline is not a domain: the tool that made one does not exist,
    # so no chain can reach for it.
    assert "corridor_of" not in TOOL_REGISTRY


def test_the_cut_between_two_points_keeps_only_what_lies_between_them(tmp_path):
    cut = section(_BANK, between=[_UPSTREAM, _DOWNSTREAM],
                  _output_dir=str(tmp_path))
    # The band is 0.20 deg long and the section 0.05 deg of it: a quarter.
    assert cut.source_area_km2 == pytest.approx(cut.area_km2 * 4.0, rel=0.02)
    assert cut.parts_kept == 1 and cut.parts_dropped == 0
    assert cut.length_m == pytest.approx(4565.0, rel=0.01)
    assert cut.utm_epsg == 32617
    assert cut.layer_type == "vector"
    assert cut.style == {"kind": "reference", "geometry": "polygon"}


def test_the_two_end_faces_stand_where_the_points_were_put(tmp_path):
    cut = section(_BANK, between=[_UPSTREAM, _DOWNSTREAM],
                  _output_dir=str(tmp_path))
    min_lon, _, max_lon, _ = cut.bbox
    assert min_lon == pytest.approx(_UPSTREAM[0], abs=1e-4)
    assert max_lon == pytest.approx(_DOWNSTREAM[0], abs=1e-4)


def test_each_end_face_spans_the_polygon_across_the_reach(tmp_path):
    """The face is the whole transect the cut left, not the part of it a probe
    line happened to catch: the cut edge is exactly collinear with such a line,
    and over a domain-sized probe the collinear intersection comes back whole at
    one end and EMPTY at the other, which is what left a reach with no outflow."""
    cut = section(_BANK, between=[_UPSTREAM, _DOWNSTREAM],
                  _output_dir=str(tmp_path))
    for face, lon in ((cut.face_start, _UPSTREAM[0]),
                      (cut.face_end, _DOWNSTREAM[0])):
        assert len(face) == 2
        assert all(point[0] == pytest.approx(lon, abs=1e-4) for point in face)
        # the band is 0.02 deg tall and the face crosses all of it
        assert abs(face[0][1] - face[1][1]) == pytest.approx(0.02, abs=1e-6)


def test_an_end_the_cut_never_reached_refuses_by_naming_the_geometry(tmp_path):
    """A downstream point PAST the end of the mapped band: the cut there falls
    off the polygon, so the section stops on its own edge and there is no
    transect at that end to prescribe an outflow across."""
    with pytest.raises(SectionError) as excinfo:
        section(_BANK, between=[_UPSTREAM, (-83.25, 35.0)],
                _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "SECTION_END_FACE_UNMEASURED"
    assert "boundary vertices stand on the cut plane" in str(excinfo.value)
    assert "downstream" in str(excinfo.value)


def test_the_cut_is_square_to_the_line_not_to_the_meridian(tmp_path):
    # A section between two points at the SAME latitude is cut on meridians; one
    # between points on a diagonal is not, and its corners must move with the
    # line rather than stay north-south.
    straight = section(_BANK, between=[_UPSTREAM, _DOWNSTREAM],
                       _output_dir=str(tmp_path))
    slanted = section(_BANK, between=[(-83.45, 34.995), (-83.40, 35.005)],
                      _output_dir=str(tmp_path))
    assert slanted.bbox[0] < straight.bbox[0]
    assert slanted.bbox[2] > straight.bbox[2]


def test_a_piece_the_line_never_touches_is_dropped_and_said_to_be(tmp_path):
    # Two parallel bands: the line runs down the southern one only, so the
    # northern piece falls inside the end cuts without being the reach.
    two = json.dumps({"type": "MultiPolygon", "coordinates": [
        [[[-83.50, 34.99], [-83.30, 34.99], [-83.30, 35.01], [-83.50, 35.01],
          [-83.50, 34.99]]],
        [[[-83.50, 35.20], [-83.30, 35.20], [-83.30, 35.22], [-83.50, 35.22],
          [-83.50, 35.20]]]]})
    cut = section(two, between=[_UPSTREAM, _DOWNSTREAM], _output_dir=str(tmp_path))
    assert cut.parts_kept == 1 and cut.parts_dropped == 1
    assert any("do not touch the line" in note for note in cut.notes)


def test_the_extent_cut_is_the_ordinary_clip(tmp_path):
    cut = section(_BANK, within=(-83.45, 34.0, -83.40, 36.0),
                  _output_dir=str(tmp_path))
    assert cut.source_area_km2 == pytest.approx(cut.area_km2 * 4.0, rel=0.02)
    assert cut.length_m == 0.0 and cut.utm_epsg == 0
    assert cut.bbox[0] == pytest.approx(-83.45) and cut.bbox[2] == pytest.approx(-83.40)


def test_the_notes_state_what_went_in_and_what_came_out(tmp_path):
    cut = section(_BANK, between=[_UPSTREAM, _DOWNSTREAM],
                  _output_dir=str(tmp_path))
    assert any("km^2 kept of the" in note for note in cut.notes)
    assert any("EPSG:32617" in note for note in cut.notes)


def test_the_artifact_holds_the_measurements_the_layer_reports(tmp_path):
    cut = section(_BANK, between=[_UPSTREAM, _DOWNSTREAM],
                  _output_dir=str(tmp_path))
    doc = json.loads(open(cut.uri).read())
    props = doc["features"][0]["properties"]
    assert props["area_km2"] == cut.area_km2
    assert props["length_m"] == cut.length_m
    assert props["parts_kept"] == 1 and props["parts_dropped"] == 0
    assert doc["features"][0]["geometry"]["type"] == "Polygon"


# --------------------------------------------------------------------------- #
# The refusals: nothing here invents a shape.
# --------------------------------------------------------------------------- #
def test_no_cut_at_all_refuses(tmp_path):
    with pytest.raises(SectionError) as excinfo:
        section(_BANK, _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "SECTION_INPUT_INVALID"


def test_both_cuts_at_once_refuses(tmp_path):
    with pytest.raises(SectionError) as excinfo:
        section(_BANK, between=[_UPSTREAM, _DOWNSTREAM],
                within=(-83.45, 34.0, -83.40, 36.0), _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "SECTION_INPUT_INVALID"


def test_a_line_layer_carries_no_polygon_and_refuses_by_naming_the_supply(tmp_path):
    # THE ruling: a flowline is not banks. Nothing widens it here.
    flowline = json.dumps({"type": "LineString",
                           "coordinates": [[-83.50, 35.0], [-83.30, 35.0]]})
    with pytest.raises(SectionError) as excinfo:
        section(flowline, between=[_UPSTREAM, _DOWNSTREAM],
                _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "SECTION_NO_POLYGON"
    message = str(excinfo.value)
    assert "draw the polygon" in message and "case layer" in message


def test_two_points_off_the_polygon_refuse_rather_than_reach_for_it(tmp_path):
    with pytest.raises(SectionError) as excinfo:
        section(_BANK, between=[(-90.0, 35.0), (-89.9, 35.0)],
                _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "SECTION_CUT_EMPTY"


def test_an_extent_the_polygon_does_not_reach_refuses(tmp_path):
    with pytest.raises(SectionError) as excinfo:
        section(_BANK, within=(-70.0, 35.0, -69.0, 36.0), _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "SECTION_CUT_EMPTY"


def test_one_point_twice_has_no_direction_to_be_square_to(tmp_path):
    with pytest.raises(SectionError) as excinfo:
        section(_BANK, between=[_UPSTREAM, _UPSTREAM], _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "SECTION_INPUT_INVALID"
    assert "one point" in str(excinfo.value)


@pytest.mark.parametrize("bad", [
    [(-83.45, 35.0)],                       # one point
    [(-83.45, 35.0), (-83.40, 35.0), (0, 0)],  # three
    [(-83.45, 35.0), ("x", 35.0)],          # not a number
    [(-83.45, 35.0), (-200.0, 35.0)],       # off the globe
])
def test_a_malformed_between_refuses_by_code(bad, tmp_path):
    with pytest.raises(SectionError) as excinfo:
        section(_BANK, between=bad, _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "SECTION_INPUT_INVALID"


@pytest.mark.parametrize("bad", [
    (-83.45, 34.0, -83.40),                 # three
    (-83.40, 34.0, -83.45, 36.0),           # inverted lon
    (-83.45, 34.0, -83.45, 36.0),           # no width
])
def test_a_malformed_within_refuses_by_code(bad, tmp_path):
    with pytest.raises(SectionError) as excinfo:
        section(_BANK, within=bad, _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "SECTION_INPUT_INVALID"


def test_a_source_that_cannot_be_read_refuses(tmp_path):
    with pytest.raises(SectionError) as excinfo:
        section(str(tmp_path / "nothing.fgb"), between=[_UPSTREAM, _DOWNSTREAM],
                _output_dir=str(tmp_path))
    assert excinfo.value.error_code == "SECTION_SOURCE_UNREADABLE"


def test_invented_keyword_arguments_are_absorbed(tmp_path):
    cut = section(_BANK, between=[_UPSTREAM, _DOWNSTREAM],
                  buffer_m=50, style="blue", _output_dir=str(tmp_path))
    assert cut.parts_kept == 1
