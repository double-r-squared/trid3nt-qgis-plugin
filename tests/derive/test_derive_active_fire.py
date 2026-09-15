"""The fire discriminator as a derive over a layer.

Covered: the published defaults; that the split gate and then the contextual tests
against the cloud-free background - not the absolute floor - reject ground hot in
both windows; that cold cloud in a neighbourhood is excluded from the background
rather than allowed to lower it; the per-detection properties and the cell-centre
geometry; the refusals for a layer without the bands the tests need, for a crop
where nothing is hot, for a crop where nothing stands out, for a crop with no
background at all, and for a negative contrast; and the band-description contract
the raw ABI fetcher writes and this derive reads back. No network. ASCII only."""

from __future__ import annotations

import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.derive.derive_active_fire import derive_active_fire  # noqa: F401
from trid3nt_server.tools.derive.derive_active_fire.derive_active_fire import (
    ActiveFireError,
)
from trid3nt_server.tools.fetchers.imagery.fetch_goes_abi.hooks import band_description

_CELL = 0.02
_WEST, _NORTH = -108.0, 45.9

#: Cloud-free values for the three bands the cloud test reads: dark ground under a
#: warm 12.3 um window, which the published test leaves alone.
_CLEAR_RED = 0.15
_CLEAR_NIR = 0.20
_CLEAR_DIRTY_K = 294.0


def _abi_layer(tmp_path, bands: dict[int, np.ndarray], name: str = "abi.tif") -> str:
    """A raw-ABI-shaped layer: float32 bands, NaN nodata, each band described the way
    the raw ABI fetcher describes it."""
    numbers = sorted(bands)
    height, width = bands[numbers[0]].shape
    path = tmp_path / name
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": len(numbers),
        "height": height, "width": width, "crs": "EPSG:4326",
        "transform": from_origin(_WEST, _NORTH, _CELL, _CELL),
        "nodata": float("nan"),
    }
    with rasterio.open(path, "w", **profile) as dst:
        for position, number in enumerate(numbers, start=1):
            dst.write(bands[number].astype("float32"), position)
            dst.set_band_description(position, band_description(number))
    return str(path)


def _windows(tmp_path, shortwave, longwave, name, cloud=None):
    """A layer carrying the two window bands plus cloud-test bands that call every
    cell clear, except where ``cloud`` says otherwise."""
    shape = shortwave.shape
    red = np.full(shape, _CLEAR_RED, dtype="float32")
    nir = np.full(shape, _CLEAR_NIR, dtype="float32")
    dirty = np.full(shape, _CLEAR_DIRTY_K, dtype="float32")
    if cloud is not None:
        red[cloud] = 0.55
        nir[cloud] = 0.60
        dirty[cloud] = 250.0
    return _abi_layer(
        tmp_path, {2: red, 3: nir, 7: shortwave, 14: longwave, 15: dirty}, name)


@pytest.fixture()
def scene(tmp_path):
    """Cool ground, a patch of sunlit ground hot in BOTH windows, two burning cells
    and a no-data corner."""
    shortwave = np.full((6, 6), 300.0, dtype="float32")
    longwave = np.full((6, 6), 295.0, dtype="float32")
    shortwave[1:3, 1:4] = 315.0   # hot ground: above the absolute floor
    longwave[1:3, 1:4] = 308.0    # ... and hot in the longwave too, so split = 7
    shortwave[4, 4] = 335.0
    longwave[4, 4] = 292.0        # a flame: the longwave stays near ambient
    shortwave[4, 5] = 322.0
    longwave[4, 5] = 293.0
    shortwave[0, 0] = np.nan
    longwave[0, 0] = np.nan
    return _windows(tmp_path, shortwave, longwave, "scene.tif")


def _features(uri: str) -> list[dict]:
    with open(uri) as handle:
        return json.load(handle)["features"]


def test_registered_as_a_derive():
    assert "derive_active_fire" in TOOL_REGISTRY
    assert TOOL_REGISTRY["derive_active_fire"].metadata.cacheable is False


def test_published_defaults_are_the_thresholds(scene, tmp_path):
    layer = TOOL_REGISTRY["derive_active_fire"].fn(
        layer=scene, _output_dir=str(tmp_path))
    assert layer.bt_shortwave_min_k == 310.0
    assert layer.bt_split_min_k == 10.0
    assert layer.background_sigma == 3.0


def test_only_the_burning_cells_are_flagged(scene, tmp_path):
    layer = TOOL_REGISTRY["derive_active_fire"].fn(
        layer=scene, _output_dir=str(tmp_path))
    assert layer.fire_pixel_count == 2
    assert layer.candidate_cell_count == 2
    assert layer.unclassifiable_cell_count == 0
    assert layer.cloud_fraction == 0.0
    assert layer.max_bt_shortwave_k == 335.0
    assert layer.max_bt_split_k == 43.0
    assert layer.layer_type == "vector"
    assert layer.style == {"kind": "reference", "geometry": "point"}


def test_the_contextual_tests_reject_hot_ground_the_floors_admit(scene, tmp_path):
    """With the split floor off, the sunlit patch clears both window tests; the
    contrast against its own neighbourhood is what tells it from the flame."""
    absolute_only = TOOL_REGISTRY["derive_active_fire"].fn(
        layer=scene, bt_split_min_k=0.0, _output_dir=str(tmp_path))
    assert absolute_only.candidate_cell_count == 8
    assert absolute_only.fire_pixel_count == 2


def test_a_hot_cell_on_a_warm_uniform_background_is_detected(tmp_path):
    shortwave = np.full((30, 30), 305.0, dtype="float32")
    longwave = np.full((30, 30), 298.0, dtype="float32")
    shortwave[15, 15] = 340.0
    longwave[15, 15] = 299.0
    layer = _windows(tmp_path, shortwave, longwave, "uniform.tif")
    result = TOOL_REGISTRY["derive_active_fire"].fn(
        layer=layer, _output_dir=str(tmp_path))
    assert result.fire_pixel_count == 1
    assert result.cloud_fraction == 0.0


def test_cold_cloud_in_the_window_is_excluded_from_the_background(tmp_path):
    """The same cell and the same background, with a cold cloud bank across a third
    of the neighbourhood: the cloud is neither candidate nor background, so it
    neither lowers the floor the flame has to clear nor becomes a detection."""
    shortwave = np.full((30, 30), 305.0, dtype="float32")
    longwave = np.full((30, 30), 298.0, dtype="float32")
    shortwave[15, 15] = 340.0
    longwave[15, 15] = 299.0
    cloud = np.zeros((30, 30), dtype=bool)
    cloud[:10, :] = True
    shortwave[cloud] = 312.0      # sunlit cloud top: warm in the shortwave
    longwave[cloud] = 250.0       # ... and cold in the longwave, so the split is 62
    layer = _windows(tmp_path, shortwave, longwave, "cloud.tif", cloud=cloud)
    result = TOOL_REGISTRY["derive_active_fire"].fn(
        layer=layer, _output_dir=str(tmp_path))
    assert result.fire_pixel_count == 1
    assert result.candidate_cell_count == 1
    assert result.cloud_fraction == pytest.approx(1.0 / 3.0, abs=1e-3)
    lon, lat = _features(result.uri)[0]["geometry"]["coordinates"]
    assert lon == pytest.approx(_WEST + 15.5 * _CELL)
    assert lat == pytest.approx(_NORTH - 15.5 * _CELL)


def test_each_detection_carries_its_temperatures_at_its_cell_centre(scene, tmp_path):
    layer = TOOL_REGISTRY["derive_active_fire"].fn(
        layer=scene, _output_dir=str(tmp_path))
    hottest = max(_features(layer.uri),
                  key=lambda f: f["properties"]["bt_shortwave_k"])
    assert hottest["properties"] == {
        "bt_shortwave_k": 335.0, "bt_longwave_k": 292.0, "bt_split_k": 43.0}
    lon, lat = hottest["geometry"]["coordinates"]
    assert lon == pytest.approx(_WEST + 4.5 * _CELL)
    assert lat == pytest.approx(_NORTH - 4.5 * _CELL)


def test_a_stricter_shortwave_floor_narrows_the_detection(scene, tmp_path):
    layer = TOOL_REGISTRY["derive_active_fire"].fn(
        layer=scene, bt_shortwave_min_k=330.0, _output_dir=str(tmp_path))
    assert layer.fire_pixel_count == 1


def test_nothing_burning_is_refused_with_the_strongest_signal_named(tmp_path):
    shortwave = np.full((4, 4), 305.0, dtype="float32")
    longwave = np.full((4, 4), 299.0, dtype="float32")
    layer = _windows(tmp_path, shortwave, longwave, "cool.tif")
    with pytest.raises(ActiveFireError) as caught:
        TOOL_REGISTRY["derive_active_fire"].fn(
            layer=layer, _output_dir=str(tmp_path))
    assert caught.value.error_code == "ACTIVE_FIRE_NO_DETECTIONS"
    assert "305.0 K" in str(caught.value)
    assert "6.0 K" in str(caught.value)


def test_a_warm_desert_with_no_contrast_detects_nothing(tmp_path):
    """Ground the sun has heated, varying a little: every cell clears neither the
    contrast nor, for most of them, the background-size test."""
    rng = np.random.default_rng(7)
    shortwave = (322.0 + rng.normal(0.0, 1.5, (40, 40))).astype("float32")
    longwave = (300.0 + rng.normal(0.0, 1.0, (40, 40))).astype("float32")
    layer = _windows(tmp_path, shortwave, longwave, "desert.tif")
    with pytest.raises(ActiveFireError) as caught:
        TOOL_REGISTRY["derive_active_fire"].fn(
            layer=layer, _output_dir=str(tmp_path))
    assert caught.value.error_code in (
        "ACTIVE_FIRE_NO_DETECTIONS", "ACTIVE_FIRE_UNCLASSIFIABLE")


def test_a_crop_with_no_background_left_is_unclassifiable_not_clear(tmp_path):
    """The control the two floors cannot answer: every cell clears both of them, so
    no cell has a background - which is not the same answer as "nothing is burning"
    and does not use the same code."""
    shortwave = np.full((30, 30), 330.0, dtype="float32")
    longwave = np.full((30, 30), 310.0, dtype="float32")
    layer = _windows(tmp_path, shortwave, longwave, "hot_ground.tif")
    with pytest.raises(ActiveFireError) as caught:
        TOOL_REGISTRY["derive_active_fire"].fn(
            layer=layer, _output_dir=str(tmp_path))
    assert caught.value.error_code == "ACTIVE_FIRE_UNCLASSIFIABLE"
    assert "900 cells pass both window tests" in str(caught.value)
    assert "UNKNOWN" in str(caught.value)


def test_an_unjudged_candidate_is_counted_beside_the_detections(tmp_path):
    """A fire on open ground, and hot ground walled in by cloud: the detection
    stands, and the cells whose neighbourhoods hold no background are reported as
    unjudged rather than folded into the clear ones."""
    shortwave = np.full((40, 40), 300.0, dtype="float32")
    longwave = np.full((40, 40), 295.0, dtype="float32")
    cloud = np.zeros((40, 40), dtype=bool)
    cloud[10:30, 10:30] = True
    cloud[16:24, 16:24] = False
    shortwave[cloud] = 312.0
    longwave[cloud] = 250.0
    shortwave[16:24, 16:24] = 330.0
    longwave[16:24, 16:24] = 310.0
    shortwave[2, 2] = 340.0
    longwave[2, 2] = 296.0
    layer = _windows(tmp_path, shortwave, longwave, "walled.tif", cloud=cloud)
    result = TOOL_REGISTRY["derive_active_fire"].fn(
        layer=layer, _output_dir=str(tmp_path))
    assert result.fire_pixel_count == 1
    assert result.candidate_cell_count == 65
    assert result.unclassifiable_cell_count == 64


def _checkered(tmp_path, hot_k: float, name: str) -> str:
    """A crop whose background really varies - alternating cells 10 K apart in BOTH
    windows, so the shortwave deviates by 5 K while the difference between the two
    windows does not vary at all - with one hotter cell at its centre."""
    cool = (np.indices((30, 30)).sum(axis=0) % 2) == 0
    shortwave = np.where(cool, 305.0, 295.0).astype("float32")
    longwave = np.where(cool, 292.0, 282.0).astype("float32")
    shortwave[15, 15] = hot_k
    longwave[15, 15] = 292.0
    return _windows(tmp_path, shortwave, longwave, name)


def test_a_candidate_inside_its_background_spread_is_not_a_detection(tmp_path):
    """The contrast is measured in the background's own deviations: at the default
    three, a cell 13 K above a background that deviates by 5 K is not fire."""
    layer = _checkered(tmp_path, 313.0, "spread.tif")
    with pytest.raises(ActiveFireError) as caught:
        TOOL_REGISTRY["derive_active_fire"].fn(
            layer=layer, _output_dir=str(tmp_path))
    assert caught.value.error_code == "ACTIVE_FIRE_NO_DETECTIONS"
    assert "1 cells pass both window tests" in str(caught.value)

    relaxed = TOOL_REGISTRY["derive_active_fire"].fn(
        layer=layer, background_sigma=1.0, _output_dir=str(tmp_path))
    assert relaxed.fire_pixel_count == 1
    assert relaxed.background_sigma == 1.0


def test_a_negative_contrast_is_refused(scene):
    with pytest.raises(ActiveFireError) as caught:
        TOOL_REGISTRY["derive_active_fire"].fn(
            layer=scene, background_sigma=-1.0)
    assert caught.value.error_code == "ACTIVE_FIRE_INPUT_INVALID"


def test_a_layer_without_the_cloud_bands_is_refused(tmp_path):
    shortwave = np.full((3, 3), 340.0, dtype="float32")
    longwave = np.full((3, 3), 295.0, dtype="float32")
    layer = _abi_layer(
        tmp_path, {7: shortwave, 14: longwave}, "windows_only.tif")
    with pytest.raises(ActiveFireError) as caught:
        TOOL_REGISTRY["derive_active_fire"].fn(
            layer=layer, _output_dir=str(tmp_path))
    assert caught.value.error_code == "ACTIVE_FIRE_LAYER_UNREADABLE"
    assert "[2, 3, 15]" in str(caught.value)


def test_a_layer_without_the_longwave_band_is_refused(tmp_path):
    layer = _abi_layer(
        tmp_path, {7: np.full((3, 3), 340.0, dtype="float32")}, "one_band.tif")
    with pytest.raises(ActiveFireError) as caught:
        TOOL_REGISTRY["derive_active_fire"].fn(
            layer=layer, _output_dir=str(tmp_path))
    assert caught.value.error_code == "ACTIVE_FIRE_LAYER_UNREADABLE"
    assert "14" in str(caught.value) and "[7]" in str(caught.value)


def test_a_layer_with_no_uri_is_refused():
    with pytest.raises(ActiveFireError) as caught:
        TOOL_REGISTRY["derive_active_fire"].fn(layer={"name": "not a layer"})
    assert caught.value.error_code == "ACTIVE_FIRE_LAYER_UNREADABLE"


def test_a_non_finite_threshold_is_refused(scene):
    with pytest.raises(ActiveFireError) as caught:
        TOOL_REGISTRY["derive_active_fire"].fn(
            layer=scene, bt_shortwave_min_k=float("nan"))
    assert caught.value.error_code == "ACTIVE_FIRE_INPUT_INVALID"
