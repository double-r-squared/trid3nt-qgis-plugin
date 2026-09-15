"""The daytime composite as a derive over a layer.

Covered: the air taken off before the mix, the stated green synthesis under the
display gamma, the refusals for a layer without the visible bands, for a layer
that states no observation geometry and for a scene in darkness, and the band-
description contract the raw ABI fetcher writes and this derive reads back. No
network. ASCII only."""

from __future__ import annotations

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.derive.derive_true_color import derive_true_color  # noqa: F401
from trid3nt_server.tools.derive.derive_true_color.derive_true_color import (
    TrueColorError,
)
from trid3nt_server.tools.fetchers.imagery.fetch_goes_abi.hooks import band_description

_CELL = 0.02

#: Big Horn County, Montana in the early afternoon, seen from the GOES-West
#: subpoint: a sunlit scene at a real geostationary geometry.
_DAY = "2026-09-14T20:41:17.0Z"
_NIGHT = "2026-09-15T08:41:17.0Z"
_SUBPOINT = "-137.0"


def _abi_layer(tmp_path, bands: dict[int, np.ndarray], name: str,
               tags: dict[str, str] | None = None) -> str:
    numbers = sorted(bands)
    height, width = bands[numbers[0]].shape
    path = tmp_path / name
    profile = {
        "driver": "GTiff", "dtype": "float32", "count": len(numbers),
        "height": height, "width": width, "crs": "EPSG:4326",
        "transform": from_origin(-108.0, 45.9, _CELL, _CELL),
        "nodata": float("nan"),
    }
    with rasterio.open(path, "w", **profile) as dst:
        for position, number in enumerate(numbers, start=1):
            dst.write(bands[number].astype("float32"), position)
            dst.set_band_description(position, band_description(number))
        if tags is not None:
            dst.update_tags(**tags)
    return str(path)


def _uniform(tmp_path, blue, red, veggie, name="visible.tif", when=_DAY):
    tags = None if when is None else {
        "scan_time_utc": when, "satellite_subpoint_lon": _SUBPOINT}
    return _abi_layer(tmp_path, {
        1: np.full((3, 3), blue), 2: np.full((3, 3), red),
        3: np.full((3, 3), veggie)}, name, tags)


def _channels(layer) -> tuple[float, float, float]:
    """The three channels back in reflectance, with the display gamma undone."""
    with rasterio.open(layer.uri) as src:
        assert src.count == 3
        rgb = src.read()[:, 0, 0]
    return tuple(float((v / 255.0) ** 2.2) for v in rgb)


def test_registered_as_a_derive():
    assert "derive_true_color" in TOOL_REGISTRY
    assert TOOL_REGISTRY["derive_true_color"].metadata.cacheable is False


def test_the_air_is_taken_off_before_the_mix(tmp_path):
    """Ground that is tan reaches the instrument blue-heavy, and has to leave the
    composite red-heavy: air scatters the blue several times as hard as the red,
    and taking it off is what turns the order of the two channels around."""
    layer = TOOL_REGISTRY["derive_true_color"].fn(
        layer=_uniform(tmp_path, 0.187, 0.168, 0.29), _output_dir=str(tmp_path))
    red, _green, blue = _channels(layer)
    assert blue < red
    assert layer.source_bands == [1, 2, 3]
    assert layer.layer_type == "raster"


def test_the_green_channel_is_the_stated_mixture(tmp_path):
    """Green is synthesised from the other three, over the corrected values."""
    layer = TOOL_REGISTRY["derive_true_color"].fn(
        layer=_uniform(tmp_path, 0.187, 0.168, 0.29), _output_dir=str(tmp_path))
    red, green, blue = _channels(layer)
    # The near-infrared band leaves no channel of its own, so it is recovered from
    # the mixture the other two channels pin down.
    veggie = (green - 0.45 * red - 0.45 * blue) / 0.10
    assert 0.0 < veggie < 1.0
    assert green == pytest.approx(
        0.45 * red + 0.10 * veggie + 0.45 * blue, abs=2e-3)


def test_a_fully_lit_crop_reports_full_daylight(tmp_path):
    layer = TOOL_REGISTRY["derive_true_color"].fn(
        layer=_uniform(tmp_path, 0.187, 0.168, 0.29), _output_dir=str(tmp_path))
    assert layer.daylight_fraction == 1.0


def test_a_scene_in_darkness_is_refused(tmp_path):
    with pytest.raises(TrueColorError) as caught:
        TOOL_REGISTRY["derive_true_color"].fn(
            layer=_uniform(tmp_path, 0.0, 0.0, 0.0, "night.tif", when=_NIGHT),
            _output_dir=str(tmp_path))
    assert caught.value.error_code == "TRUE_COLOR_NO_DAYLIGHT"


def test_a_layer_that_states_no_geometry_is_refused(tmp_path):
    """Without the instant and the subpoint there is no angle to take the air off
    along, and a guess would be a picture of somewhere else's air."""
    with pytest.raises(TrueColorError) as caught:
        TOOL_REGISTRY["derive_true_color"].fn(
            layer=_uniform(tmp_path, 0.187, 0.168, 0.29, "bare.tif", when=None),
            _output_dir=str(tmp_path))
    assert caught.value.error_code == "TRUE_COLOR_GEOMETRY_MISSING"


def test_a_layer_without_the_visible_bands_is_refused(tmp_path):
    layer = _abi_layer(
        tmp_path, {7: np.full((3, 3), 300.0), 14: np.full((3, 3), 295.0)},
        "thermal.tif", {"scan_time_utc": _DAY, "satellite_subpoint_lon": _SUBPOINT})
    with pytest.raises(TrueColorError) as caught:
        TOOL_REGISTRY["derive_true_color"].fn(
            layer=layer, _output_dir=str(tmp_path))
    assert caught.value.error_code == "TRUE_COLOR_LAYER_UNREADABLE"
    assert "[7, 14]" in str(caught.value)
