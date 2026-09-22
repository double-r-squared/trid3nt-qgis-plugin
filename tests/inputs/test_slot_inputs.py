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
from trid3nt_server.inputs.slots import SLOTS, ingest_slot, role_of
from trid3nt_server.inputs.user_input import UserInputError
from trid3nt_server.workflows.runtime.data import (
    BED, DOMAIN, EXTENT, LEVEL, LINE)

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
        domain({"type": "Point", "coordinates": [0.0, 0.0]})
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
    assert bed("fetch_cudem").kind == RASTER
    assert bed(None) is None


def test_the_bed_slot_takes_one_source_and_composes_nothing():
    """The measurement and the surface under it are composed by the slot's own
    ingestion, so neither the slot nor the row that declares it carries the
    surface underneath: the row states one class."""
    import inspect

    from trid3nt_server.workflows.runtime import Data

    assert "over" not in inspect.signature(bed).parameters
    assert not Data.need("bathymetry").coercion.get("near")


def test_a_depth_no_water_body_holds_refuses_rather_than_being_meshed():
    with pytest.raises(UserInputError, match="outside 0.0-12000.0 m"):
        bed(-3.0)


def test_each_slot_reads_through_the_ingestion_its_role_names():
    assert ingest_slot(DOMAIN, _RING).bbox == (-123.0, 45.0, -122.9, 45.1)
    assert ingest_slot(BED, 2.0).kind == DEPTH
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
    assert {name for name, slot in SLOTS.items() if slot.draw} == {
        DOMAIN, EXTENT, LINE}
    assert SLOTS[DOMAIN].draw[:2] == ("polygon", "domain")
    assert SLOTS[LINE].draw[:2] == ("polyline", "line")
    # a box rides the pick mode, which offers no vector tool to choose between
    assert SLOTS[EXTENT].draw[:2] == ("rectangle", "")


def test_a_rows_name_is_its_slot_and_every_other_name_is_a_plain_row():
    """The reserved names ARE this registry's keys: one list, so a row called
    ``bed`` is the bed slot however it was declared, and a row called anything
    else is read by whatever declared it."""
    assert role_of("bed") == "bed"
    assert role_of("discharge") == "discharge"
    assert role_of("observe") == "observe"
    assert role_of("carrier") == ""
    assert role_of("surveyed_bed") == ""


def test_a_reserved_name_states_which_classes_its_role_serves():
    """The bed reads a measurement or the ground; a level reads a level. The
    lint refuses a reserved name under a class its slot does not serve."""
    assert SLOTS[BED].classes == frozenset(
        {"bathymetry", "terrain", "channel survey"})
    assert SLOTS[LEVEL].classes == frozenset({"water level series"})
    assert "discharge series" not in SLOTS[BED].classes


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


def test_a_bed_the_runs_frame_cannot_reach_names_its_zero_once():
    """The datum check builds the refusal and names the surface, its zero and
    the run's; the slot re-raises that under its own code rather than saying the
    same three facts again in its own words."""
    from trid3nt_contracts.execution import LayerURI
    from trid3nt_server.inputs.bed import elevations

    charted = LayerURI(layer_id="x", name="a charted survey", layer_type="raster",
                       uri="s3://b/k/survey.tif", vertical_datum="CRD")
    with pytest.raises(UserInputError) as excinfo:
        elevations(charted, frame="NAVD88")
    said = str(excinfo.value)
    assert said.count("CRD") == 1 and said.count("NAVD88") == 1
    assert "a charted survey" in said


def _merged(share: float | None, alternatives: list[str] | None = None):
    """A merged bed stating the share of the water no row of it measured."""
    from trid3nt_server.inputs.bed import MergedRasterLayerURI

    return MergedRasterLayerURI(layer_id="m", name="merged bed surface",
                                layer_type="raster", uri="s3://b/k/merged.tif",
                                unmeasured_water_fraction=share,
                                water_alternatives=alternatives or [])


def test_water_no_row_measured_is_feedback_and_not_a_refusal():
    """NO refusing share exists in the system. The surface states what it
    covers before the solve and a node no value reaches is what refuses, so a
    hole the fill named no share for is a foot gun with feedback beside it."""
    assert bed(_merged(0.74, ["fetch_chs_nonna"]), label="bed").kind == RASTER


def test_a_fill_that_names_the_share_it_refuses_at_is_refused_above_it():
    """The remedy is the person's to state, so the refusal carries every half of
    it: how much of the water nothing measured, the share they said they would
    not stand on, which rows could cover it, and the ops that would lay them."""
    op = [{"name": "merge", "rows": ["fetch_gebco"], "refuse_above": 0.3}]
    with pytest.raises(UserInputError) as excinfo:
        bed(_merged(0.74, ["fetch_chs_nonna"]), label="bed", op=op)
    said = str(excinfo.value)
    assert "74.0%" in said and "30.0%" in said
    assert "fetch_chs_nonna" in said
    assert "'interpolated'" in said


def test_a_hole_inside_the_share_the_fill_named_stands():
    op = [{"name": "merge", "rows": ["fetch_gebco"], "refuse_above": 0.8}]
    assert bed(_merged(0.74), label="bed", op=op).kind == RASTER


def test_a_share_that_is_not_a_share_refuses_at_the_op():
    from trid3nt_server.inputs.bed import refuse_above

    with pytest.raises(UserInputError, match="refuse_above"):
        bed(_merged(0.1), op=[{"name": "merge", "rows": ["a"],
                               "refuse_above": 7.0}])
    # The STRICTEST of several merges answers: a run that stated two shares
    # meant the tighter of them.
    assert refuse_above([{"name": "merge", "rows": ["a"], "refuse_above": 0.6},
                         {"name": "merge", "rows": ["b"],
                          "refuse_above": 0.2}]) == 0.2
    assert refuse_above([{"name": "merge", "rows": ["a"]}]) is None


def test_every_merge_the_call_states_names_rows_that_are_laid():
    """A stated op that is not laid is impossible, not merely unlikely: a call
    naming two merges lays both, in the order it states them."""
    from trid3nt_server.inputs.bed import merge_rows

    assert merge_rows([{"name": "merge", "rows": ["a", "b"]},
                       "interpolated"]) == ["a", "b"]
    assert merge_rows([{"name": "merge", "rows": ["a", "b"]},
                       {"name": "merge", "rows": ["c"]},
                       "interpolated"]) == ["a", "b", "c"]


def test_a_bed_whose_water_is_measured_whole_needs_no_op():
    assert bed(_merged(0.0)).kind == RASTER
    assert bed(_merged(None)).kind == RASTER


def test_the_op_the_run_states_is_what_lets_the_unmeasured_water_through():
    assert bed(_merged(0.74), op="interpolated").kind == RASTER
    assert bed(_merged(0.74), op={"name": "interpolated"}).kind == RASTER
    assert bed(_merged(0.74), op=[{"name": "merge", "rows": ["a"]},
                                  "interpolated"]).kind == RASTER


def test_an_op_this_slot_does_not_know_refuses_by_name():
    """The op NAME is the slot's own to read, so the slot is where a name it
    has no move for - or an argument its move does not take - refuses."""
    with pytest.raises(UserInputError, match="'interpolated'"):
        bed(_merged(0.74), op="smoothed")
    with pytest.raises(UserInputError, match="'interpolated'"):
        bed(_merged(0.74), op={"name": "interpolated", "max_distance_m": 500.0})
    with pytest.raises(UserInputError, match="'merge'"):
        bed(_merged(0.74), op={"name": "merge"})


def test_an_empty_artifact_and_an_absent_producer_refuse_DIFFERENTLY(tmp_path):
    """An empty fetch and an absent fetch read identically otherwise, and the
    reader is told to draw a polygon a producer already went looking for."""
    import json

    with pytest.raises(UserInputError) as drawn:
        domain({"type": "Point", "coordinates": [0.0, 0.0]})
    assert "producer RAN" not in str(drawn.value)

    nothing = tmp_path / "water.geojson"
    nothing.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    with pytest.raises(UserInputError) as ran:
        domain(str(nothing))
    message = str(ran.value)
    assert "producer RAN and found nothing" in message
    assert str(nothing) in message

    gauges = tmp_path / "gauges.geojson"
    gauges.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {},
         "geometry": {"type": "Point", "coordinates": [0.0, 0.0]}}]}))
    with pytest.raises(UserInputError) as wrong_shape:
        domain(str(gauges))
    assert "1 geometries of kind Point" in str(wrong_shape.value)
    assert "producer RAN" not in str(wrong_shape.value)
