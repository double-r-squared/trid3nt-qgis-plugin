"""The typed SLOT inputs: a domain, a bed and boundary runs, read once.

Offline: every form a user's value arrives in is stated here, and what is proved
is that the typed value reads the same whichever form it came from.
"""

from __future__ import annotations

import pytest

from trid3nt_server.inputs import Point
from trid3nt_server.inputs.bed import DEPTH, POINTS, RASTER, bed
from trid3nt_server.inputs.boundary import (
    BoundaryRun,
    boundary_runs,
    roles_from_runs,
)
from trid3nt_server.inputs.domain import domain, domain_ring
from trid3nt_server.inputs.slots import DRAW_PURPOSES, ingest_slot
from trid3nt_server.inputs.user_input import UserInputError
from trid3nt_server.workflows.runtime.data import BED, DOMAIN, EXTENT, LINE, RUNS

_RING = [[-123.0, 45.0], [-122.9, 45.0], [-122.9, 45.1], [-123.0, 45.1]]


def test_a_drawn_ring_a_geojson_polygon_and_a_collection_read_the_same():
    """Three ways the same pond arrives; one value afterwards."""
    drawn = domain(_RING)
    stated = domain({"type": "Polygon",
                     "coordinates": [[*_RING, _RING[0]]]})
    collected = domain({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Polygon", "coordinates": [[*_RING, _RING[0]]]}}]})
    assert drawn.geometry == stated.geometry == collected.geometry
    assert drawn.bbox == (-123.0, 45.0, -122.9, 45.1)
    assert domain_ring(drawn) == _RING


def test_several_polygons_are_one_domain_with_parts():
    """A lake with an island and a two-basin harbour are ONE domain: picking the
    largest would silently drop the rest of what the user drew."""
    second = [[c[0] + 1.0, c[1]] for c in _RING]
    both = domain({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Polygon", "coordinates": [[*_RING, _RING[0]]]}},
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Polygon", "coordinates": [[*second, second[0]]]}}]})
    assert both.geometry["type"] == "MultiPolygon"
    assert len(both.geometry["coordinates"]) == 2


def test_a_shape_that_is_not_closed_refuses_rather_than_being_squared_off():
    with pytest.raises(UserInputError, match="no polygon geometry"):
        domain({"type": "LineString", "coordinates": [[0.0, 0.0], [1.0, 1.0]]})
    with pytest.raises(UserInputError, match="at least three vertices"):
        domain([[0.0, 0.0], [1.0, 1.0]])


def test_a_run_is_two_points_on_the_edge_and_a_type():
    runs = boundary_runs([{"start": [-123.0, 45.0], "end": [-122.9, 45.0],
                           "type": "inflow"},
                          {"start": [-123.0, 45.1], "end": [-122.9, 45.1],
                           "type": "outflow", "name": "the mouth"}])
    assert [r.type for r in runs] == ["inflow", "outflow"]
    assert runs[0].start == Point(-123.0, 45.0)
    assert runs[1].name == "the mouth"


def test_a_drawn_line_is_the_run_between_its_two_ends():
    """A drawing has vertices in between; a run is the stretch between its ends."""
    runs = boundary_runs({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"type": "open"},
         "geometry": {"type": "LineString",
                      "coordinates": [[0.0, 0.0], [0.5, 0.2], [1.0, 0.0]]}}]})
    assert runs[0].start == Point(0.0, 0.0) and runs[0].end == Point(1.0, 0.0)


def test_a_closed_body_states_no_run_and_prescribes_nothing():
    """Stating none is an ANSWER: the edge is solid wall wherever nothing names
    it, so a wall run prescribes nothing either."""
    assert boundary_runs(None) == ()
    assert roles_from_runs(()) == {}
    walls = (BoundaryRun(Point(0.0, 0.0), Point(1.0, 0.0)),)
    assert roles_from_runs(walls) == {}


def test_a_run_carries_one_of_the_declared_types():
    with pytest.raises(UserInputError, match="carries one of"):
        BoundaryRun(Point(0.0, 0.0), Point(1.0, 0.0), type="spillway")


def test_the_bed_slot_says_which_shape_it_was_handed():
    assert bed(2.0).kind == DEPTH and bed(2.0).depth_m == 2.0
    assert bed("2.5").depth_m == 2.5
    assert bed("s3://b/k/survey.geojson").kind == POINTS
    assert bed({"type": "FeatureCollection", "features": []}).kind == POINTS
    assert bed("s3://b/k/dem.tif").kind == RASTER
    # a fetcher NAME is a surface the mesh op resolves, not a depth
    assert bed("fetch_topobathy").kind == RASTER
    assert bed(None) is None


def test_a_depth_no_water_body_holds_refuses_rather_than_being_meshed():
    with pytest.raises(UserInputError, match="outside 0.0-12000.0 m"):
        bed(-3.0)


def test_each_slot_reads_through_the_ingestion_its_role_names():
    assert ingest_slot(DOMAIN, _RING).bbox == (-123.0, 45.0, -122.9, 45.1)
    assert ingest_slot(BED, 2.0).kind == DEPTH
    assert ingest_slot(RUNS, None) == ()
    # a role nothing here reads leaves the value as it came
    assert ingest_slot("", "x") == "x"


def test_an_extent_slot_reads_a_box_however_the_caller_named_it():
    """A rectangle drawn on the canvas arrives as four numbers, and what reads
    the slot afterwards reads ``.bbox`` - never the form it came in."""
    drawn = ingest_slot(EXTENT, (-71.55, 41.33, -71.44, 41.40))
    stated = ingest_slot(EXTENT, {"bbox": [-71.55, 41.33, -71.44, 41.40],
                                  "name": "the harbour window"})
    assert drawn.bbox == stated.bbox == (-71.55, 41.33, -71.44, 41.40)
    assert stated.name == "the harbour window"


def test_a_domain_says_its_own_name_when_it_is_read_as_text():
    """A mesh session, a layer title and a run's own name are all written from
    the domain, so a domain that read as its geometry named every one of them
    after its coordinates."""
    from trid3nt_server.workflows.mesh.step import _session_name

    named = domain({"type": "FeatureCollection", "name": "Willamette River",
                    "features": [{"type": "Feature", "properties": {},
                                  "geometry": {"type": "Polygon",
                                               "coordinates": [[*_RING,
                                                                _RING[0]]]}}]})
    assert str(named) == "Willamette River"
    assert _session_name(named, "om2d") == "Willamette River mesh"
    # A drawn outline has no name and is not its coordinates either.
    drawn = domain(_RING)
    assert str(drawn) == "domain"
    assert _session_name(drawn, "om2d") == "domain mesh"


def test_only_the_slots_a_user_can_draw_are_offered_on_the_canvas():
    """A bed is a survey or a number, so there is nothing to draw for it."""
    assert set(DRAW_PURPOSES) == {DOMAIN, RUNS, EXTENT, LINE}
    assert DRAW_PURPOSES[DOMAIN][:2] == ("polygon", "domain")
    assert DRAW_PURPOSES[RUNS][:2] == ("polyline", "boundary run")
    assert DRAW_PURPOSES[LINE][:2] == ("polyline", "line")
    # a box rides the pick mode, which offers no vector tool to choose between
    assert DRAW_PURPOSES[EXTENT][:2] == ("rectangle", "")


def test_a_domain_producer_hands_over_the_runs_it_cut_the_polygon_between():
    """A reach fetcher returns the polygon AND the two faces it was cut between
    as rows of one artifact. The slot reads the producer's own word for which
    row is which; a row that prescribes nothing is the wall the edge already
    is, and a drawn outline states no run at all."""
    from trid3nt_server.inputs.domain import domain

    reach = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"part": "reach"},
         "geometry": {"type": "Polygon",
                      "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}},
        {"type": "Feature", "properties": {"part": "centerline"},
         "geometry": {"type": "LineString", "coordinates": [[0.1, 0.5], [0.9, 0.5]]}},
        {"type": "Feature", "properties": {"part": "inflow"},
         "geometry": {"type": "LineString", "coordinates": [[0, 0], [0, 1]]}},
        {"type": "Feature", "properties": {"part": "outflow"},
         "geometry": {"type": "LineString", "coordinates": [[1, 0], [1, 1]]}},
    ]}
    cut = domain(reach)
    assert cut.geometry["type"] == "Polygon"
    assert [run.type for run in cut.runs] == ["inflow", "outflow"]
    assert (cut.runs[0].start.lon, cut.runs[0].end.lat) == (0.0, 1.0)
    assert domain([[0, 0], [1, 0], [1, 1]]).runs == ()


def test_a_line_reads_the_same_from_every_shape_it_arrives_in():
    """A drawn collection, a bare geometry and a typed vertex list are one line
    after the slot, so a profile down a reach and a profile across a lake are
    the same read."""
    from trid3nt_server.inputs.line import line

    drawn = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"role": "line"},
         "geometry": {"type": "LineString", "coordinates": [[0.0, 0.0],
                                                            [1.0, 1.0]]}}]}
    one = {"type": "LineString", "coordinates": [[0.0, 0.0], [1.0, 1.0]]}
    assert line(drawn) == one
    assert line(one) == one
    assert line([[0.0, 0.0], [1.0, 1.0]]) == one
    assert line(None) is None


def test_a_reach_mapped_in_two_pieces_stays_two_pieces():
    """Merging them would decide where the gap closes, which is a measurement
    nobody made."""
    from trid3nt_server.inputs.line import line

    two = {"type": "MultiLineString",
           "coordinates": [[[0.0, 0.0], [1.0, 0.0]], [[2.0, 0.0], [3.0, 0.0]]]}
    assert line(two) == two


def test_a_shape_that_carries_no_polyline_refuses_rather_than_reading_one():
    """An outline is not a line down it, and squaring one into the other would
    measure a profile along a shoreline."""
    from trid3nt_server.inputs.line import line
    from trid3nt_server.inputs.user_input import UserInputError

    for carries_none in ({"type": "Polygon",
                          "coordinates": [[[0.0, 0.0], [1.0, 0.0],
                                           [1.0, 1.0], [0.0, 0.0]]]},
                         {"type": "FeatureCollection", "features": []}):
        with pytest.raises(UserInputError) as excinfo:
            line(carries_none)
        assert excinfo.value.error_code == "LINE_INVALID"
