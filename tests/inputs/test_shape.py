"""A Shape is ingested once, from the draw or a selected layer, and asked for its
lines or its polygons in lon/lat."""

from __future__ import annotations

import json

import pytest

from trid3nt_server.inputs.shape import Shape, polygons, polylines, shape
from trid3nt_server.workflows.runtime.user_input import UserInputError

_DRAWN = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"role": "line"},
     "geometry": {"type": "LineString",
                  "coordinates": [[-124.2, 40.4], [-124.0, 40.6]]}},
    {"type": "Feature", "properties": {"role": "aoi"},
     "geometry": {"type": "Polygon",
                  "coordinates": [[[-124.2, 40.4], [-124.0, 40.4],
                                   [-124.0, 40.6], [-124.2, 40.4]]]}}]}


def test_the_draw_is_taken_as_it_is_and_answers_lines_and_polygons():
    got = shape(_DRAWN)
    assert isinstance(got, Shape) and len(got.features["features"]) == 2
    assert polylines(got) == [[[-124.2, 40.4], [-124.0, 40.6]]]
    assert [g["type"] for g in polygons(got)] == ["Polygon"]


def test_a_selected_layer_is_read_into_the_same_collection(tmp_path):
    path = tmp_path / "breakwater.geojson"
    path.write_text(json.dumps(_DRAWN))
    assert polylines(shape(str(path))) == [[[-124.2, 40.4], [-124.0, 40.6]]]

    class _Layer:
        uri = str(path)

    assert polylines(shape(_Layer())) == [[[-124.2, 40.4], [-124.0, 40.6]]]


def test_a_bare_geometry_and_typed_vertices_wrap_into_features():
    one = shape({"type": "LineString", "coordinates": [[0.0, 0.0], [1.0, 1.0]]})
    assert polylines(one) == [[[0.0, 0.0], [1.0, 1.0]]]
    typed = shape([[0.0, 0.0], [1.0, 1.0], [2.0, 0.0]])
    assert polylines(typed) == [[[0.0, 0.0], [1.0, 1.0], [2.0, 0.0]]]
    assert shape(None) is None


def test_a_shape_with_no_line_refuses_under_the_callers_code():
    polygon_only = shape({"type": "Polygon", "coordinates": [
        [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]})
    with pytest.raises(UserInputError) as ei:
        polylines(polygon_only, label="structure", code="ARTEMIS_STRUCTURE_INVALID")
    assert ei.value.error_code == "ARTEMIS_STRUCTURE_INVALID"
