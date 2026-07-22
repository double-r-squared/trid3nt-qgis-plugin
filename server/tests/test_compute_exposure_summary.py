"""Tests for ``compute_exposure_summary`` (hazard-footprint exposure).

No network: the WorldPop / buildings fetch seams
(``_fetch_population_layer`` / ``_fetch_buildings_layer``) are monkeypatched
to synthetic local artifacts; the hazard raster is a tiny local GeoTIFF.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from trid3nt_server.tools import compute_exposure_summary as mod
from trid3nt_server.tools.compute_exposure_summary import (
    ExposureEmptyFootprintError,
    ExposureInputError,
    compute_exposure_summary,
    get_session_exposure,
)

# 10x10 grid over a 0.1 x 0.1 deg box near Mexico Beach, FL.
_BBOX = (-85.5, 29.9, -85.4, 30.0)


def _write_raster(path: Path, data: np.ndarray, nodata: float | None = None) -> Path:
    import rasterio
    from rasterio.transform import from_bounds

    h, w = data.shape
    transform = from_bounds(*_BBOX, w, h)
    kwargs = dict(
        driver="GTiff",
        height=h,
        width=w,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    )
    if nodata is not None:
        kwargs["nodata"] = nodata
    with rasterio.open(path, "w", **kwargs) as ds:
        ds.write(data.astype("float32"), 1)
    return path


@pytest.fixture()
def hazard_path(tmp_path: Path) -> Path:
    """Flood depth: left half wet at 2.0 m, right half dry (0)."""
    data = np.zeros((10, 10), dtype="float32")
    data[:, :5] = 2.0
    return _write_raster(tmp_path / "depth.tif", data)


@pytest.fixture()
def population_layer(tmp_path: Path) -> SimpleNamespace:
    """WorldPop stand-in: 10 people per cell on the same grid (100 total)."""
    data = np.full((10, 10), 10.0, dtype="float32")
    path = _write_raster(tmp_path / "pop.tif", data)
    return SimpleNamespace(uri=str(path))


@pytest.fixture()
def buildings_layer(tmp_path: Path) -> SimpleNamespace:
    """3 buildings: two on the wet half, one on the dry half."""
    import geopandas as gpd
    from shapely.geometry import Point

    gdf = gpd.GeoDataFrame(
        {"name": ["wet_a", "wet_b", "dry_c"]},
        geometry=[
            Point(-85.48, 29.95),
            Point(-85.46, 29.92),
            Point(-85.42, 29.95),
        ],
        crs="EPSG:4326",
    )
    path = tmp_path / "buildings.geojson"
    gdf.to_file(path, driver="GeoJSON")
    return SimpleNamespace(uri=str(path))


@pytest.fixture()
def patched_fetchers(monkeypatch, population_layer, buildings_layer):
    monkeypatch.setattr(
        mod, "_fetch_population_layer", lambda bbox, dataset: population_layer
    )
    monkeypatch.setattr(mod, "_fetch_buildings_layer", lambda bbox: buildings_layer)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_exposure_happy_path(hazard_path: Path, patched_fetchers) -> None:
    result = compute_exposure_summary(hazard_layer_uri=str(hazard_path))

    # Wet half = 50 cells x 10 people/cell.
    assert result["population"] == 500
    # Two of the three buildings sit on the wet half.
    assert result["buildings"] == 2
    assert result["area_km2"] > 0.0
    assert result["threshold"] is None  # default: any wet cell
    assert result["errors"] == {}
    assert result["footprint_cell_count"] == 50
    assert result["hazard_layer_uri"] == str(hazard_path)
    assert result["computed_at"]
    # bbox is the raster bounds in EPSG:4326.
    assert result["bbox"] == pytest.approx(list(_BBOX), abs=1e-6)


def test_exposure_area_matches_geometry(hazard_path: Path, patched_fetchers) -> None:
    result = compute_exposure_summary(hazard_layer_uri=str(hazard_path))
    # Half of a ~0.1 x 0.1 deg box at ~30N: ~ (0.05 deg * 96.4 km/deg-lon)
    # x (0.1 deg * 110.5 km/deg-lat) ~= 53 km^2. Accept a generous band.
    assert 40.0 < result["area_km2"] < 70.0


def test_threshold_shrinks_footprint(
    tmp_path: Path, patched_fetchers, population_layer, buildings_layer
) -> None:
    # Depth gradient: only the leftmost 2 columns exceed 3.0.
    data = np.zeros((10, 10), dtype="float32")
    data[:, 0] = 4.0
    data[:, 1] = 3.5
    data[:, 2] = 1.0
    path = _write_raster(tmp_path / "grad.tif", data)

    result = compute_exposure_summary(hazard_layer_uri=str(path), threshold=3.0)
    assert result["threshold"] == 3.0
    assert result["footprint_cell_count"] == 20
    assert result["population"] == 200  # 20 cells x 10 people


def test_session_store_records_result(hazard_path: Path, patched_fetchers) -> None:
    result = compute_exposure_summary(hazard_layer_uri=str(hazard_path))
    stored = get_session_exposure(None)
    assert stored is not None
    assert stored["population"] == result["population"]
    assert stored["area_km2"] == result["area_km2"]


# --------------------------------------------------------------------------- #
# Per-component honest degrade
# --------------------------------------------------------------------------- #


def test_population_failure_degrades_per_component(
    hazard_path: Path, monkeypatch, buildings_layer
) -> None:
    def _boom(bbox, dataset):
        raise RuntimeError("WorldPop upstream 503")

    monkeypatch.setattr(mod, "_fetch_population_layer", _boom)
    monkeypatch.setattr(mod, "_fetch_buildings_layer", lambda bbox: buildings_layer)

    result = compute_exposure_summary(hazard_layer_uri=str(hazard_path))
    assert result["population"] is None
    assert "WorldPop upstream 503" in result["errors"]["population"]
    # Buildings + area still computed.
    assert result["buildings"] == 2
    assert result["area_km2"] > 0.0


def test_buildings_failure_degrades_per_component(
    hazard_path: Path, monkeypatch, population_layer
) -> None:
    monkeypatch.setattr(
        mod, "_fetch_population_layer", lambda bbox, dataset: population_layer
    )

    def _boom(bbox):
        raise RuntimeError("Overpass timeout")

    monkeypatch.setattr(mod, "_fetch_buildings_layer", _boom)

    result = compute_exposure_summary(hazard_layer_uri=str(hazard_path))
    assert result["buildings"] is None
    assert "Overpass timeout" in result["errors"]["buildings"]
    assert result["population"] == 500


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


def test_empty_footprint_raises_typed_error(tmp_path: Path, patched_fetchers) -> None:
    data = np.zeros((10, 10), dtype="float32")  # entirely dry
    path = _write_raster(tmp_path / "dry.tif", data)
    with pytest.raises(ExposureEmptyFootprintError):
        compute_exposure_summary(hazard_layer_uri=str(path))


def test_threshold_above_all_values_raises_empty(
    hazard_path: Path, patched_fetchers
) -> None:
    with pytest.raises(ExposureEmptyFootprintError):
        compute_exposure_summary(hazard_layer_uri=str(hazard_path), threshold=99.0)


def test_missing_uri_raises_input_error() -> None:
    with pytest.raises(ExposureInputError):
        compute_exposure_summary(hazard_layer_uri="/nonexistent/depth.tif")


def test_empty_uri_raises_input_error() -> None:
    with pytest.raises(ExposureInputError):
        compute_exposure_summary(hazard_layer_uri="")


def test_non_finite_threshold_raises_input_error(hazard_path: Path) -> None:
    with pytest.raises(ExposureInputError):
        compute_exposure_summary(
            hazard_layer_uri=str(hazard_path), threshold=float("nan")
        )


def test_nodata_cells_excluded_from_footprint(
    tmp_path: Path, patched_fetchers
) -> None:
    # Left half wet, right half nodata (-9999): only 50 wet cells, and the
    # nodata cells are neither wet nor valid.
    data = np.full((10, 10), -9999.0, dtype="float32")
    data[:, :5] = 1.0
    path = _write_raster(tmp_path / "nd.tif", data, nodata=-9999.0)
    result = compute_exposure_summary(hazard_layer_uri=str(path))
    assert result["footprint_cell_count"] == 50


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_registered_in_tool_registry() -> None:
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get("compute_exposure_summary")
    assert entry is not None
    assert entry.metadata.cacheable is False
    assert entry.metadata.ttl_class == "live-no-cache"
