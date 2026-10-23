"""The tile mosaic's pixel budget and resolution speak metres on a geographic target as on a projected one."""

from __future__ import annotations

import pytest

pytest.importorskip("rasterio")

from trid3nt_server.tools.fetchers import _tile_mosaic
from trid3nt_server.tools.fetchers._fetch_common import PixelBudgetExceededError

_BBOX = (-124.3, 47.0, -124.1, 47.2)


@pytest.fixture
def tile(tmp_path):
    import numpy
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / "tile.tif"
    cell = 1.0 / 3600.0
    width = int(0.4 / cell)
    with rasterio.open(path, "w", driver="GTiff", width=width, height=width, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_origin(-124.4, 47.3, cell, cell)) as ds:
        ds.write(numpy.full((1, width, width), -5.0, dtype="float32"))
    return str(path)


def test_a_geographic_target_keeps_the_sources_own_cell(tile) -> None:
    transform, width, height = _tile_mosaic.target_grid([tile], "EPSG:4326", _BBOX)
    assert transform.a == pytest.approx(1.0 / 3600.0)
    assert width == pytest.approx(0.2 * 3600, abs=1)


def test_resolution_m_is_metres_on_a_geographic_target(tile) -> None:
    transform, _w, _h = _tile_mosaic.target_grid([tile], "EPSG:4326", _BBOX, resolution_m=60.0)
    assert transform.a == pytest.approx(60.0 / _tile_mosaic.METRES_PER_DEGREE)


def test_a_geographic_ask_past_the_budget_refuses_naming_the_metres_that_fit(tmp_path) -> None:
    import numpy
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / "fine.tif"
    with rasterio.open(path, "w", driver="GTiff", width=100, height=100, count=1, dtype="float32",
                       crs="EPSG:4326", transform=from_origin(-124.3, 47.1, 1e-5, 1e-5)) as ds:
        ds.write(numpy.zeros((1, 100, 100), dtype="float32"))
    with pytest.raises(PixelBudgetExceededError) as caught:
        _tile_mosaic.target_grid([str(path)], "EPSG:4326", _BBOX)
    assert "re-ask at resolution_m=" in str(caught.value) and "EPSG:4326" in str(caught.value)
    fits = int(str(caught.value).split("re-ask at resolution_m=")[1].split(" ")[0])
    _transform, width, _h = _tile_mosaic.target_grid([str(path)], "EPSG:4326", _BBOX, resolution_m=float(fits))
    assert width <= _tile_mosaic.MAX_GRID_PX
