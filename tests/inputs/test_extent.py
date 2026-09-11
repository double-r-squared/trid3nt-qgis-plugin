"""An Extent is ingested once, from a bbox pick, the canvas AOI, a place or a
layer's bounds, and read as one ordered box."""

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


def test_a_place_becomes_the_box_around_its_geocoded_centre(monkeypatch):
    from trid3nt_server.inputs import aoi

    async def _geo(name):
        return (-114.46, 42.56)

    monkeypatch.setattr(aoi, "geocode_place", _geo)
    got = _ingest("Twin Falls, Idaho", half_deg=0.1)
    assert got == Extent((-114.56, 42.46, -114.36, 42.66), "Twin Falls, Idaho")


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
