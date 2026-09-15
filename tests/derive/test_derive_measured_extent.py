"""Unit tests for ``derive_measured_extent``.

Covered: the footprint read off the raster's own mask rather than a threshold on
values, a real zero staying inside it, the intersection with a polygon and the
covered fraction it reports, a survey that reaches nothing under the polygon
refusing, an all-nodata raster refusing, and the registration."""

from __future__ import annotations

import json
import os
import tempfile

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.derive.derive_measured_extent.derive_measured_extent import (
    MeasuredExtentError,
    derive_measured_extent,
)

_NODATA = -9999.0


def _write(values: np.ndarray) -> str:
    fd, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_measured_")
    os.close(fd)
    with rasterio.open(
        path, "w", driver="GTiff", height=values.shape[0], width=values.shape[1],
        count=1, dtype="float32", crs="EPSG:4326", nodata=_NODATA,
        transform=from_origin(-122.70, 45.52, 0.01, 0.01),
    ) as dst:
        dst.write(values.astype("float32"), 1)
    return path


def _read(uri: str) -> dict:
    with open(uri, encoding="utf-8") as handle:
        return json.load(handle)


def test_registered() -> None:
    assert "derive_measured_extent" in TOOL_REGISTRY


def test_the_footprint_is_the_mask_and_a_real_zero_stays_inside_it(
        tmp_path) -> None:
    # The west column is a measured 0.0; the east column was never sounded.
    grid = _write(np.array([[0.0, _NODATA], [0.0, _NODATA]]))
    layer = derive_measured_extent(raster=grid, _output_dir=str(tmp_path))
    assert layer.area_km2 > 0.0
    west, _south, east, _north = layer.bbox
    assert east == pytest.approx(-122.69, abs=1e-6)
    assert west == pytest.approx(-122.70, abs=1e-6)
    assert _read(layer.uri)["features"][0]["geometry"]["type"] in (
        "Polygon", "MultiPolygon")


def test_the_intersection_with_a_polygon_reports_what_it_kept(tmp_path) -> None:
    grid = _write(np.array([[1.0, _NODATA], [1.0, _NODATA]]))
    asked = {"type": "Polygon", "coordinates": [[
        [-122.70, 45.50], [-122.68, 45.50], [-122.68, 45.52],
        [-122.70, 45.52], [-122.70, 45.50]]]}
    layer = derive_measured_extent(raster=grid, within=asked,
                                   _output_dir=str(tmp_path))
    assert layer.covered_fraction == pytest.approx(0.5, abs=0.02)
    assert layer.within_area_km2 > layer.area_km2


def test_a_survey_that_reaches_nothing_under_the_polygon_refuses(tmp_path) -> None:
    grid = _write(np.array([[1.0, 1.0], [1.0, 1.0]]))
    elsewhere = {"type": "Polygon", "coordinates": [[
        [10.0, 10.0], [10.1, 10.0], [10.1, 10.1], [10.0, 10.1], [10.0, 10.0]]]}
    with pytest.raises(MeasuredExtentError) as caught:
        derive_measured_extent(raster=grid, within=elsewhere,
                               _output_dir=str(tmp_path))
    assert caught.value.error_code == "DERIVE_MEASURED_EXTENT_DISJOINT"


def test_an_all_nodata_raster_refuses(tmp_path) -> None:
    with pytest.raises(MeasuredExtentError) as caught:
        derive_measured_extent(raster=_write(np.full((2, 2), _NODATA)),
                               _output_dir=str(tmp_path))
    assert caught.value.error_code == "DERIVE_MEASURED_EXTENT_EMPTY"


def test_an_unreadable_source_refuses(tmp_path) -> None:
    with pytest.raises(MeasuredExtentError) as caught:
        derive_measured_extent(raster="", _output_dir=str(tmp_path))
    assert caught.value.error_code == "DERIVE_MEASURED_EXTENT_SOURCE_UNREADABLE"
