"""Unit tests for ``derive_raster_mean``.

Covered: the mean over a whole grid, nodata cells not counted and never read as
zero, the mean inside a polygon, an all-nodata grid refusing, an area that does
not overlap, and the registration."""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.derive.derive_raster_mean.derive_raster_mean import (
    RasterMeanError,
    derive_raster_mean,
)

_NODATA = -9999.0


def _write(values: np.ndarray) -> str:
    fd, path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_raster_mean_")
    os.close(fd)
    with rasterio.open(
        path, "w", driver="GTiff", height=values.shape[0], width=values.shape[1],
        count=1, dtype="float32", crs="EPSG:4326", nodata=_NODATA,
        transform=from_origin(-122.70, 45.52, 0.01, 0.01),
    ) as dst:
        dst.write(values.astype("float32"), 1)
    return path


def test_registered_and_not_cacheable() -> None:
    assert "derive_raster_mean" in TOOL_REGISTRY
    assert TOOL_REGISTRY["derive_raster_mean"].metadata.cacheable is False


def test_the_mean_of_every_real_pixel() -> None:
    out = derive_raster_mean(layer=_write(np.array([[1.0, 2.0], [3.0, 4.0]])))
    assert out["mean"] == pytest.approx(2.5)
    assert out["count"] == 4
    assert (out["min"], out["max"]) == (1.0, 4.0)


def test_nodata_is_not_counted_and_is_never_a_zero() -> None:
    out = derive_raster_mean(layer=_write(np.array([[4.0, _NODATA], [_NODATA, 6.0]])))
    assert out["mean"] == pytest.approx(5.0)
    assert out["count"] == 2


def test_an_all_nodata_grid_refuses() -> None:
    with pytest.raises(RasterMeanError) as caught:
        derive_raster_mean(layer=_write(np.full((2, 2), _NODATA)))
    assert caught.value.error_code == "DERIVE_RASTER_MEAN_EMPTY"


def test_the_mean_inside_a_polygon() -> None:
    grid = _write(np.array([[10.0, 20.0], [30.0, 40.0]]))
    left = {"type": "Polygon", "coordinates": [[
        [-122.7005, 45.4995], [-122.6930, 45.4995],
        [-122.6930, 45.5205], [-122.7005, 45.5205], [-122.7005, 45.4995]]]}
    out = derive_raster_mean(layer=grid, within=left)
    assert out["mean"] == pytest.approx(20.0)
    assert out["count"] == 2


def test_an_area_that_does_not_overlap_refuses() -> None:
    grid = _write(np.array([[1.0, 2.0], [3.0, 4.0]]))
    far = {"type": "Polygon", "coordinates": [[
        [10.0, 10.0], [10.1, 10.0], [10.1, 10.1], [10.0, 10.1], [10.0, 10.0]]]}
    with pytest.raises(RasterMeanError) as caught:
        derive_raster_mean(layer=grid, within=far)
    assert caught.value.error_code == "DERIVE_RASTER_MEAN_NO_OVERLAP"


def test_an_area_with_no_polygon_refuses() -> None:
    grid = _write(np.array([[1.0, 2.0], [3.0, 4.0]]))
    with pytest.raises(RasterMeanError) as caught:
        derive_raster_mean(
            layer=grid,
            within={"type": "Point", "coordinates": [-122.7, 45.5]})
    assert caught.value.error_code == "DERIVE_RASTER_MEAN_NO_OVERLAP"
