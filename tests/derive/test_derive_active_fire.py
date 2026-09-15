"""The split-window discriminator as a derive over a layer.

Covered: the published defaults; that the split gate - not the absolute one - is
what rejects sunlit ground hot in both windows; the per-detection properties and
the cell-centre geometry; the refusals for a layer without the window bands and
for a crop where nothing burns; and the band-description contract the raw ABI
fetcher writes and this derive reads back. No network. ASCII only."""

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
    return _abi_layer(tmp_path, {7: shortwave, 14: longwave})


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


def test_only_the_burning_cells_are_flagged(scene, tmp_path):
    layer = TOOL_REGISTRY["derive_active_fire"].fn(
        layer=scene, _output_dir=str(tmp_path))
    assert layer.fire_pixel_count == 2
    assert layer.max_bt_shortwave_k == 335.0
    assert layer.max_bt_split_k == 43.0
    assert layer.layer_type == "vector"
    assert layer.style == {"kind": "reference", "geometry": "point"}


def test_the_split_gate_is_what_rejects_hot_ground(scene, tmp_path):
    """The absolute floor alone takes the sunlit patch with the fire; the split
    floor is what tells the two apart."""
    absolute_only = TOOL_REGISTRY["derive_active_fire"].fn(
        layer=scene, bt_split_min_k=0.0, _output_dir=str(tmp_path))
    assert absolute_only.fire_pixel_count == 8
    both = TOOL_REGISTRY["derive_active_fire"].fn(
        layer=scene, _output_dir=str(tmp_path))
    assert both.fire_pixel_count == 2


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
    layer = _abi_layer(tmp_path, {7: shortwave, 14: longwave}, "cool.tif")
    with pytest.raises(ActiveFireError) as caught:
        TOOL_REGISTRY["derive_active_fire"].fn(
            layer=layer, _output_dir=str(tmp_path))
    assert caught.value.error_code == "ACTIVE_FIRE_NO_DETECTIONS"
    assert "305.0 K" in str(caught.value)
    assert "6.0 K" in str(caught.value)


def test_a_layer_without_the_longwave_band_is_refused(tmp_path):
    layer = _abi_layer(
        tmp_path, {7: np.full((3, 3), 340.0, dtype="float32")}, "one_band.tif")
    with pytest.raises(ActiveFireError) as caught:
        TOOL_REGISTRY["derive_active_fire"].fn(
            layer=layer, _output_dir=str(tmp_path))
    assert caught.value.error_code == "ACTIVE_FIRE_LAYER_UNREADABLE"
    assert "[14]" in str(caught.value) and "[7]" in str(caught.value)


def test_a_layer_with_no_uri_is_refused():
    with pytest.raises(ActiveFireError) as caught:
        TOOL_REGISTRY["derive_active_fire"].fn(layer={"name": "not a layer"})
    assert caught.value.error_code == "ACTIVE_FIRE_LAYER_UNREADABLE"


def test_a_non_finite_threshold_is_refused(scene):
    with pytest.raises(ActiveFireError) as caught:
        TOOL_REGISTRY["derive_active_fire"].fn(
            layer=scene, bt_shortwave_min_k=float("nan"))
    assert caught.value.error_code == "ACTIVE_FIRE_INPUT_INVALID"
