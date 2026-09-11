"""``derive_transect``: one line through a shape's centroid along a bearing.

Offline: every shape is geometry authored in the test. What is checked is the
line - centred on the centroid, the stated length on the ground, the direction
in the convention it was stated in - and that every wrong ask refuses by code."""

from __future__ import annotations

import json
import math

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.derive.derive_transect.derive_transect import (
    TransectError,
    derive_transect,
)

#: A breakwater outline near Point Judith, as a closed line.
_BREAKWATER = json.dumps({"type": "LineString", "coordinates": [
    [-71.515, 41.370], [-71.516, 41.372], [-71.514, 41.373], [-71.513, 41.371],
    [-71.515, 41.370]]})


def _ends(layer, tmp_path):
    doc = json.loads((tmp_path / layer.uri.rsplit("/", 1)[-1]).read_text())
    return doc["features"][0]["geometry"]["coordinates"]


def test_the_tool_is_registered_under_its_own_name():
    assert TOOL_REGISTRY["derive_transect"].fn.__name__ == "derive_transect"


def test_the_line_is_centred_on_the_centroid_and_the_stated_length(tmp_path):
    from pyproj import Geod

    line = derive_transect(_BREAKWATER, bearing_deg=0.0, length_m=1000.0,
                           _output_dir=str(tmp_path))
    start, end = _ends(line, tmp_path)
    assert line.centre == pytest.approx((-71.5145, 41.3715), abs=2e-3)
    assert line.length_m == 1000.0 and line.utm_epsg == 32619
    assert Geod(ellps="WGS84").inv(*start, *end)[2] == pytest.approx(1000.0, rel=1e-3)
    assert (start[0] + end[0]) / 2.0 == pytest.approx(line.centre[0], abs=1e-6)
    assert (start[1] + end[1]) / 2.0 == pytest.approx(line.centre[1], abs=1e-6)
    assert line.layer_type == "vector" and line.style["geometry"] == "line"


@pytest.mark.parametrize("bearing,convention,heads", [
    (0.0, "compass", "north"), (90.0, "compass", "east"),
    (0.0, "trig", "east"), (90.0, "trig", "north"), (180.0, "trig", "west"),
])
def test_the_bearing_runs_from_the_first_vertex_to_the_last(
        tmp_path, bearing, convention, heads):
    line = derive_transect(_BREAKWATER, bearing_deg=bearing, length_m=500.0,
                           convention=convention, _output_dir=str(tmp_path))
    start, end = _ends(line, tmp_path)
    dlon, dlat = end[0] - start[0], end[1] - start[1]
    axis = {"north": dlat > abs(dlon), "east": dlon > abs(dlat),
            "west": -dlon > abs(dlat)}
    assert axis[heads], (heads, dlon, dlat)
    assert line.convention == convention and line.bearing_deg == bearing


def test_a_wrong_length_convention_or_shape_refuses_by_code(tmp_path):
    with pytest.raises(TransectError) as ei:
        derive_transect(_BREAKWATER, bearing_deg=0.0, length_m=0.0,
                        _output_dir=str(tmp_path))
    assert ei.value.error_code == "TRANSECT_INPUT_INVALID"
    with pytest.raises(TransectError) as ei:
        derive_transect(_BREAKWATER, bearing_deg=0.0, length_m=10.0,
                        convention="radians", _output_dir=str(tmp_path))
    assert ei.value.error_code == "TRANSECT_INPUT_INVALID"
    empty = json.dumps({"type": "FeatureCollection", "features": []})
    with pytest.raises(TransectError) as ei:
        derive_transect(empty, bearing_deg=0.0, length_m=10.0,
                        _output_dir=str(tmp_path))
    assert ei.value.error_code == "TRANSECT_NO_SHAPE"
    with pytest.raises(TransectError) as ei:
        derive_transect(str(tmp_path / "nothing.geojson"), bearing_deg=0.0,
                        length_m=10.0, _output_dir=str(tmp_path))
    assert ei.value.error_code == "TRANSECT_SOURCE_UNREADABLE"


def test_a_typed_polyline_set_is_a_shape_too(tmp_path):
    """The structure slot hands the tool what it was handed: a list of lines."""
    lines = [[[-71.515, 41.370], [-71.516, 41.372]],
             [[-71.514, 41.373], [-71.513, 41.371]]]
    line = derive_transect(lines, bearing_deg=45.0, length_m=200.0,
                           _output_dir=str(tmp_path))
    assert math.isclose(line.centre[0], -71.5145, abs_tol=1e-3)
