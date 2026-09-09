"""The USER-INPUT species: one normalizer per shape, and BOTH routes through it.

A value the user hands us arrives DRAWN (the draw gate's reply) or TYPED (a wire
coercion). These pin the shapes, the typed refusals, and the property that makes
the seam worth having: the two routes produce the SAME value for the same
geometry.

Offline - the normalizers are pure and the draw route is exercised against a stub
response object, never a live card.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from trid3nt_server.workflows.runtime.user_input import (
    UserInputError,
    bbox,
    bearing,
    bearing_deg,
    lonlat_bbox,
    lonlat_point,
    point,
    polygon_ring,
    polyline_coords,
)


# --- points ------------------------------------------------------------------ #
def test_a_point_arrives_as_a_lon_lat_pair_of_floats():
    assert lonlat_point(["-124.1", "40.5"]) == (-124.1, 40.5)
    assert lonlat_point((-124.1, 40.5)) == (-124.1, 40.5)


def test_an_absent_point_is_absence_not_a_refusal():
    """Absence is a legal answer; a malformed value is not."""
    assert lonlat_point(None) is None


@pytest.mark.parametrize("bad", ["somewhere", [1.0], [1.0, 2.0, 3.0], {"lon": 1},
                                 ["a", "b"]])
def test_a_malformed_point_refuses_typed_it_never_degrades(bad):
    """Degrading a bad point to a derived location is the silent-swallow class:
    the run models somewhere else and says nothing."""
    with pytest.raises(UserInputError) as exc:
        lonlat_point(bad, label="the outfall")
    assert "the outfall" in str(exc.value)


def test_a_point_off_the_earth_refuses_and_says_which_order_it_reads():
    with pytest.raises(UserInputError, match="longitude first"):
        lonlat_point([40.5, -124.1])          # lat/lon, the classic swap


def test_a_refusal_carries_the_callers_own_error_code():
    """A refusal reads to the model as THAT engine's refusal, not a library's."""
    with pytest.raises(UserInputError) as exc:
        lonlat_point("nonsense", code="TELEMAC_PARAMS_INVALID")
    assert exc.value.error_code == "TELEMAC_PARAMS_INVALID"


# --- bearings WRAP, they do not clamp ---------------------------------------- #
@pytest.mark.parametrize("given,expected", [
    (370, 10.0), (-90, 270.0), (0, 0.0), (359.5, 359.5), (720, 0.0), (-450, 270.0),
])
def test_a_bearing_wraps_rather_than_clamping(given, expected):
    """A bearing is cyclic, so 370 is 10 and -90 is 270 - clamping either one to a
    declared bound would turn a legal direction into a DIFFERENT legal direction."""
    assert bearing_deg(given) == pytest.approx(expected)


def test_a_non_numeric_bearing_refuses():
    with pytest.raises(UserInputError, match="degrees"):
        bearing_deg("north-ish")


# --- polylines --------------------------------------------------------------- #
def test_a_polyline_arrives_as_its_vertices():
    line = [[-124.2, 40.4], [-124.0, 40.6], [-123.9, 40.7]]
    assert polyline_coords(line) == line


def test_a_one_vertex_polyline_refuses_it_is_not_a_line():
    with pytest.raises(UserInputError, match="at least two vertices"):
        polyline_coords([[-124.2, 40.4]])


def test_a_polyline_vertex_off_the_earth_refuses():
    with pytest.raises(UserInputError, match="longitude first"):
        polyline_coords([[-124.2, 40.4], [-124.0, 95.0]])


# --- polygons arrive OPEN, whichever way they were given ---------------------- #
_OPEN = [[-124.2, 40.4], [-124.0, 40.4], [-124.0, 40.6]]
_CLOSED = _OPEN + [[-124.2, 40.4]]


@pytest.mark.parametrize("given", [_OPEN, _CLOSED])
def test_a_polygon_ring_arrives_open_whether_it_was_closed_or_not(given):
    """One representation, chosen because the two producers disagree: the canvas
    closes its ring and a typed list usually does not."""
    assert polygon_ring(given) == _OPEN


def test_a_two_vertex_polygon_refuses():
    with pytest.raises(UserInputError, match="at least three vertices"):
        polygon_ring([[-124.2, 40.4], [-124.0, 40.4]])


def test_a_polygon_given_as_a_geojson_geometry_is_read_out_of_it():
    ring = polygon_ring({"type": "Polygon", "coordinates": [_CLOSED]})
    assert ring == _OPEN


# --- bboxes come back ORDERED ------------------------------------------------- #
def test_a_bbox_dragged_corner_reversed_comes_back_ordered():
    """A box dragged right-to-left arrives with its corners the other way round; a
    consumer that subtracted them would get a negative extent and clip to nothing."""
    assert lonlat_bbox([-84.9, 29.8, -85.02, 29.69]) == (-85.02, 29.69, -84.9, 29.8)


def test_a_bbox_already_ordered_is_unchanged():
    assert lonlat_bbox([-85.02, 29.69, -84.9, 29.8]) == (-85.02, 29.69, -84.9, 29.8)


def test_a_bbox_reads_from_a_comma_string_too():
    assert lonlat_bbox("-85.02, 29.69, -84.9, 29.8") == (-85.02, 29.69, -84.9, 29.8)


@pytest.mark.parametrize("bad", [[1.0, 2.0, 3.0], [1.0, 2.0, 3.0, 4.0, 5.0],
                                 ["a", "b", "c", "d"], 7])
def test_a_malformed_bbox_refuses(bad):
    with pytest.raises(UserInputError):
        lonlat_bbox(bad, label="the extent")


# --- the coercion factories: the WIRE route into the same normalizers --------- #
def test_the_point_coercion_reads_one_wire_field():
    assert point("outfall_coords")({"outfall_coords": ["-124.1", "40.5"]}) == {
        "outfall_coords": (-124.1, 40.5)}


def test_the_bbox_coercion_orders_what_the_wire_sent():
    assert bbox("extent")({"extent": [-84.9, 29.8, -85.02, 29.69]}) == {
        "extent": (-85.02, 29.69, -84.9, 29.8)}


def test_the_bearing_coercion_wraps_what_the_wire_sent():
    assert bearing("wind_dir_deg")({"wind_dir_deg": 370}) == {"wind_dir_deg": 10.0}


def test_a_coercion_labels_its_refusal_with_the_param_it_reads():
    with pytest.raises(UserInputError, match="outfall_coords"):
        point("outfall_coords")({"outfall_coords": "somewhere"})


# --- ONE SEAM: the drawn route and the typed route agree ---------------------- #
def _drawn(geometry: str, response) -> object:
    from trid3nt_server.gates.draw_input import _value_from

    return _value_from(response, geometry)


def test_a_drawn_point_equals_the_typed_point():
    reply = SimpleNamespace(coordinates=[-124.1, 40.5], features=None,
                            cancelled=False)
    assert _drawn("point", reply) == lonlat_point([-124.1, 40.5])


def test_a_drawn_rectangle_equals_the_typed_bbox_reversed_corners_included():
    coords = [-84.9, 29.8, -85.02, 29.69]
    reply = SimpleNamespace(coordinates=coords, features=None, cancelled=False)
    assert _drawn("rectangle", reply) == lonlat_bbox(coords)


def test_a_drawn_polyline_equals_the_typed_polyline():
    line = [[-124.2, 40.4], [-124.0, 40.6]]
    reply = SimpleNamespace(coordinates=None, cancelled=False, features={
        "type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {"role": "line"},
            "geometry": {"type": "LineString", "coordinates": line}}]})
    assert _drawn("polyline", reply) == polyline_coords(line)


def test_a_drawn_polygon_equals_the_typed_polygon():
    """The canvas hands back a CLOSED ring and a typed list usually does not; both
    routes read the same normalizer, so they cannot answer differently."""
    reply = SimpleNamespace(coordinates=None, cancelled=False, features={
        "type": "FeatureCollection", "features": [{
            "type": "Feature", "properties": {"role": "aoi"},
            "geometry": {"type": "Polygon", "coordinates": [_CLOSED]}}]})
    assert _drawn("polygon", reply) == polygon_ring(_CLOSED) == polygon_ring(_OPEN)


def test_a_drawn_point_off_the_earth_refuses_through_the_same_normalizer():
    reply = SimpleNamespace(coordinates=[40.5, -124.1], features=None,
                            cancelled=False)
    with pytest.raises(UserInputError, match="longitude first"):
        _drawn("point", reply)
