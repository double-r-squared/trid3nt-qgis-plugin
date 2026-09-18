"""An Extent is ingested once, from a bbox pick, the canvas AOI, four numbers or a
layer's bounds, and read as one ordered box; a place name refuses."""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server.inputs.extent import Extent, extent
from trid3nt_server.inputs.user_input import UserInputError


def _ingest(value, **kw):
    return asyncio.run(extent(value, **kw))


def test_a_bbox_pick_carries_its_box_and_its_name():
    got = _ingest({"coordinates": [-114.4, 42.5, -114.2, 42.7], "name": "reach"})
    assert got == Extent((-114.4, 42.5, -114.2, 42.7), "reach")
    assert _ingest({"bbox": [-114.4, 42.5, -114.2, 42.7]}).bbox == (
        -114.4, 42.5, -114.2, 42.7)


def test_the_canvas_aoi_is_four_numbers_and_is_ordered():
    assert _ingest((-114.2, 42.7, -114.4, 42.5)).bbox == (-114.4, 42.5, -114.2, 42.7)
    assert _ingest("-114.4,42.5,-114.2,42.7").bbox == (-114.4, 42.5, -114.2, 42.7)


def test_a_layer_gives_the_bounds_of_everything_it_holds(tmp_path):
    path = tmp_path / "water.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {
            "type": "Polygon", "coordinates": [[[-114.33, 42.57], [-114.29, 42.57],
                                                [-114.29, 42.59], [-114.33, 42.57]]]}},
        {"type": "Feature", "properties": {}, "geometry": {
            "type": "Point", "coordinates": [-114.20, 42.60]}}]}))
    assert _ingest(str(path)).bbox == (-114.33, 42.57, -114.20, 42.60)


def test_a_place_name_refuses_and_names_the_geocoder():
    with pytest.raises(UserInputError) as ei:
        _ingest("Twin Falls, Idaho", label="aoi")
    assert ei.value.retryable is True
    assert "geocode_location(query='Twin Falls, Idaho')" in str(ei.value)
    assert "west, south, east, north" in str(ei.value)


def test_geojson_inline_takes_bounds_and_nothing_passes_through():
    got = _ingest({"type": "LineString",
                   "coordinates": [[-114.34, 42.58], [-114.28, 42.59]]})
    assert got.bbox == (-114.34, 42.58, -114.28, 42.59)
    assert _ingest(None) is None


@pytest.mark.parametrize("bad", ["1,2", [200.0, 0.0, 201.0, 1.0], {"name": "x"}])
def test_a_value_of_no_readable_shape_refuses_under_the_callers_code(bad):
    with pytest.raises(UserInputError) as ei:
        _ingest(bad, label="aoi", code="TELEMAC_PARAMS_INVALID")
    assert ei.value.error_code == "TELEMAC_PARAMS_INVALID"


def test_two_boxes_are_the_same_extent_within_a_tenth_of_a_metre():
    from trid3nt_server.inputs.extent import bbox_equivalent

    box = [-114.4, 42.5, -114.2, 42.7]
    assert bbox_equivalent(box, list(box))
    assert bbox_equivalent(Extent(tuple(box)), box)
    assert bbox_equivalent(box, [-114.4000001, 42.5, -114.2, 42.7])
    # A real move of the area is never "the same extent".
    assert not bbox_equivalent(box, [-114.41, 42.5, -114.2, 42.7])
    # Nothing is equivalent to nothing, and an unreadable value never matches.
    assert not bbox_equivalent(None, box)
    assert not bbox_equivalent("x", box)
    assert not bbox_equivalent(box, [1, 2, 3])


def test_a_touching_edge_overlaps_here_and_in_every_fetcher_that_asks():
    """One shapely rule behind three call sites, with one documented semantics."""
    from trid3nt_server.inputs.extent import bbox_overlaps
    from trid3nt_server.tools.fetchers.socioeconomic.fetch_field_boundaries.hooks import (
        _bbox_intersects as fields_intersects,
    )
    from trid3nt_server.tools.fetchers.terrain.fetch_dem.hooks import (
        _bbox_intersects as dem_intersects,
    )

    cases = [
        (((0, 0, 1, 1), (2, 2, 3, 3)), False),   # disjoint
        (((0, 0, 2, 2), (1, 1, 3, 3)), True),    # partial overlap
        (((0, 0, 4, 4), (1, 1, 2, 2)), True),    # contained
        (((0, 0, 1, 1), (1, 0, 2, 1)), True),    # shared vertical edge
        (((0, 0, 1, 1), (0, 1, 1, 2)), True),    # shared horizontal edge
        (((0, 0, 1, 1), (1, 1, 2, 2)), True),    # single shared corner
        (((1, 1, 1, 1), (0, 0, 2, 2)), True),    # zero-area box inside
    ]
    for helper in (bbox_overlaps, dem_intersects, fields_intersects):
        for (a, b), expected in cases:
            assert helper(a, b) is expected, (helper.__module__, a, b)
    assert not bbox_overlaps(None, (0, 0, 1, 1))
