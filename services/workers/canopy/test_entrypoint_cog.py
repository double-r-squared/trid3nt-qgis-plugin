# Canopy worker -- COG-write + honesty-gate + helper tests.
#
# Cover for the inference-FREE parts of the canopy worker: the single-band
# float32 metres COG writer (georeferencing copied from the input RGB), the
# all-empty honesty gate, and the manifest/scheme helpers. The geoai/torch
# inference itself runs only in the image build-time smoke + the live E2E, never
# in CI -- these tests prove the COG-write contract with a SYNTHETIC height array.
#
# rasterio is required (installed in the worker image); the tests skip cleanly
# where it is absent (the agent venv DOES ship rasterio, so they run there).

from __future__ import annotations

from pathlib import Path

import pytest

rasterio = pytest.importorskip("rasterio")
import numpy as np  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402

from services.workers.canopy.entrypoint import (  # noqa: E402
    _output_scheme,
    _runs_uri,
    _split_object_uri,
    canopy_cog_is_nonempty,
    write_canopy_cog,
)


def _write_rgb(
    path: Path,
    *,
    nx: int = 64,
    ny: int = 48,
    crs: str = "EPSG:4326",
    x0: float = -85.30,
    y1: float = 29.95,
    res: float = 0.0001,  # ~10 m -- a small NAIP-ish tile
) -> None:
    """Write a tiny 3-band uint8 RGB GeoTIFF (the model INPUT to copy georef from)."""
    transform = from_origin(x0, y1, res, res)
    data = np.zeros((3, ny, nx), dtype="uint8")
    data[0] = 30  # arbitrary R/G/B -- the COG-write only reads CRS/transform/shape
    data[1] = 90
    data[2] = 40
    with rasterio.open(
        str(path),
        "w",
        driver="GTiff",
        height=ny,
        width=nx,
        count=3,
        dtype="uint8",
        crs=crs,
        transform=transform,
    ) as ds:
        ds.write(data)


# --------------------------------------------------------------------------- #
# COG-write
# --------------------------------------------------------------------------- #


def test_write_canopy_cog_copies_georef_and_is_single_band_float32(tmp_path: Path):
    rgb = tmp_path / "rgb.tif"
    _write_rgb(rgb, nx=64, ny=48)
    out = tmp_path / "canopy_height.tif"

    heights = np.full((48, 64), 12.5, dtype="float32")
    heights[0, 0] = 30.0  # a tall pixel
    write_canopy_cog(heights, rgb, out)

    assert out.exists()
    with rasterio.open(str(rgb)) as src, rasterio.open(str(out)) as dst:
        # Single-band float32 metres.
        assert dst.count == 1
        assert dst.dtypes[0] == "float32"
        # Georeferencing copied pixel-for-pixel from the RGB input.
        assert dst.crs == src.crs
        assert dst.transform == src.transform
        assert (dst.height, dst.width) == (src.height, src.width)
        band = dst.read(1)
    assert pytest.approx(band[0, 0], rel=1e-5) == 30.0
    assert pytest.approx(band[10, 10], rel=1e-5) == 12.5


def test_write_canopy_cog_clamps_negative_noise_to_zero(tmp_path: Path):
    rgb = tmp_path / "rgb.tif"
    _write_rgb(rgb, nx=32, ny=32)
    out = tmp_path / "canopy_height.tif"

    heights = np.full((32, 32), 5.0, dtype="float32")
    heights[1, 1] = -3.0  # model noise below 0 -> clamped to 0 (heights are >= 0)
    write_canopy_cog(heights, rgb, out)

    with rasterio.open(str(out)) as dst:
        band = dst.read(1)
    assert band[1, 1] == 0.0
    assert band.min() >= 0.0


def test_write_canopy_cog_is_tiled_cog(tmp_path: Path):
    rgb = tmp_path / "rgb.tif"
    _write_rgb(rgb, nx=64, ny=64)
    out = tmp_path / "canopy_height.tif"
    write_canopy_cog(np.full((64, 64), 8.0, dtype="float32"), rgb, out)

    with rasterio.open(str(out)) as dst:
        # Tiled (a COG requirement TiTiler relies on).
        assert dst.profile.get("tiled") is True
        assert dst.profile.get("compress", "").lower() == "lzw"


def test_write_canopy_cog_rejects_shape_mismatch(tmp_path: Path):
    rgb = tmp_path / "rgb.tif"
    _write_rgb(rgb, nx=64, ny=48)
    out = tmp_path / "canopy_height.tif"
    with pytest.raises(ValueError, match="shape"):
        write_canopy_cog(np.zeros((10, 10), dtype="float32"), rgb, out)


def test_write_canopy_cog_rejects_non_2d(tmp_path: Path):
    rgb = tmp_path / "rgb.tif"
    _write_rgb(rgb)
    out = tmp_path / "canopy_height.tif"
    with pytest.raises(ValueError, match="2-D"):
        write_canopy_cog(np.zeros((3, 48, 64), dtype="float32"), rgb, out)


# --------------------------------------------------------------------------- #
# Honesty gate (an empty / all-zero canopy raster is NOT a valid estimate)
# --------------------------------------------------------------------------- #


def test_canopy_cog_is_nonempty_true_for_real_heights(tmp_path: Path):
    rgb = tmp_path / "rgb.tif"
    _write_rgb(rgb, nx=32, ny=32)
    out = tmp_path / "canopy_height.tif"
    write_canopy_cog(np.full((32, 32), 7.0, dtype="float32"), rgb, out)
    assert canopy_cog_is_nonempty(out) is True


def test_canopy_cog_is_nonempty_false_for_all_zero(tmp_path: Path):
    rgb = tmp_path / "rgb.tif"
    _write_rgb(rgb, nx=32, ny=32)
    out = tmp_path / "canopy_height.tif"
    write_canopy_cog(np.zeros((32, 32), dtype="float32"), rgb, out)
    assert canopy_cog_is_nonempty(out) is False


def test_canopy_cog_is_nonempty_false_for_all_nan(tmp_path: Path):
    rgb = tmp_path / "rgb.tif"
    _write_rgb(rgb, nx=16, ny=16)
    out = tmp_path / "canopy_height.tif"
    nan_arr = np.full((16, 16), np.nan, dtype="float32")
    write_canopy_cog(nan_arr, rgb, out)
    assert canopy_cog_is_nonempty(out) is False


# --------------------------------------------------------------------------- #
# Manifest / scheme helpers (byte-identical to the openquake worker)
# --------------------------------------------------------------------------- #


def test_split_object_uri_s3_and_gs():
    assert _split_object_uri("s3://bucket/a/b.tif") == ("s3", "bucket", "a/b.tif")
    assert _split_object_uri("gs://bucket/a/b.tif") == ("gs", "bucket", "a/b.tif")


def test_split_object_uri_rejects_bad_scheme():
    with pytest.raises(ValueError, match="unsupported"):
        _split_object_uri("file:///tmp/x.tif")


def test_output_scheme_and_runs_uri(monkeypatch):
    monkeypatch.setenv("TRID3NT_OBJECT_STORE", "s3")
    monkeypatch.setattr("services.workers.canopy.entrypoint.RUNS_BUCKET", "runs-b")
    assert _output_scheme() == "s3"
    assert _runs_uri("RID", "canopy_height.tif") == "s3://runs-b/RID/canopy_height.tif"
