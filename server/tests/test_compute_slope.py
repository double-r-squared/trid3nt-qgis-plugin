"""Unit tests for ``compute_slope`` atomic tool (job-0081, FR-CE-8, FR-DC).

Coverage:
1. ``test_compute_slope_registered`` — tool appears in TOOL_REGISTRY with
   correct metadata (cacheable=True, ttl_class="static-30d",
   source_class="slope").
2. ``test_compute_slope_degrees_known_gradient`` — 32×32 synthetic DEM with a
   known 1° linear N-S slope → computed degrees ≈ 1° (within ±0.1° tolerance).
3. ``test_compute_slope_percent_conversion`` — same DEM → percent output
   verifies tan(1°) × 100 ≈ 1.745% (within ±0.1% tolerance).
4. ``test_compute_slope_horn_vs_zeventhorne_both_succeed`` — both algorithm
   choices run without error on the synthetic DEM.
5. ``test_compute_slope_cache_hit_skips_fetch`` — second call with identical
   args hits the cache (fetch_fn not invoked).
6. ``test_compute_slope_cache_miss_writes`` — first call (empty cache) invokes
   gdaldem and writes to cache bucket.
7. ``test_compute_slope_returns_layer_uri`` — LayerURI fields correct (layer_type,
   role, units match).
8. ``test_compute_slope_gdaldem_failure_raises_slope_compute_error`` — non-zero
   gdaldem exit raises SlopeComputeError(error_code="GDALDEM_FAILED").
9. ``test_compute_slope_dem_download_failure_raises_slope_compute_error`` — GCS
   download failure raises SlopeComputeError(error_code="DEM_DOWNLOAD_FAILED").
"""

from __future__ import annotations

import math
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing.compute_slope import (
    SlopeComputeError,
    _run_gdaldem_slope,
    compute_slope,
)

# ---------------------------------------------------------------------------
# Pinned timestamp for deterministic cache keys
# ---------------------------------------------------------------------------

PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Helpers: synthetic DEM creation
# ---------------------------------------------------------------------------


def _write_synthetic_dem(
    path: str,
    slope_deg: float = 1.0,
    size: int = 32,
    dx_m: float = 10.0,
) -> None:
    """Write a 32×32 GeoTIFF DEM with a known N-S linear slope.

    The DEM has elevation values chosen so that the rise/run over each cell in
    the N-S direction equals ``tan(slope_deg)`` when grid spacing is ``dx_m``
    meters. We use a simple Albers-like projected CRS (EPSG:5070) so that GDAL
    interprets pixel spacing correctly (not geographic degrees).

    The N-S gradient per pixel (rise per row) is:
        dz = dx_m * tan(slope_deg * π / 180)

    Row 0 has the highest elevation; elevation decreases southward (row index
    increases → elevation decreases), giving a uniform downslope toward south.
    """
    dz = dx_m * math.tan(math.radians(slope_deg))
    # Build elevation grid: row 0 = max elevation, row n-1 = min.
    elevations = np.zeros((size, size), dtype=np.float32)
    for row in range(size):
        elevations[row, :] = (size - row) * dz  # elevation decreases southward

    # A simple projected transform in EPSG:5070 (meters) so GDAL picks up the
    # correct pixel size and uses it for gradient computation.
    # Bounds: a small patch near Fort Myers equivalent in EPSG:5070.
    west, south, east, north = 0.0, 0.0, size * dx_m, size * dx_m
    transform = from_bounds(west, south, east, north, size, size)

    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": size,
        "height": size,
        "count": 1,
        "crs": "EPSG:5070",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(elevations, 1)


def _read_slope_mean(path: str) -> float:
    """Read a slope GeoTIFF and return the mean of valid (non-NaN, non-nodata) pixels."""
    with rasterio.open(path) as src:
        data = src.read(1, masked=True)
        # GDAL gdaldem outputs nodata at edges (border pixels use a 3x3 window);
        # take the interior to avoid border artefacts.
        interior = data[1:-1, 1:-1]
        return float(np.ma.filled(interior, np.nan).flatten()[~np.isnan(np.ma.filled(interior, np.nan).flatten())].mean())


# ---------------------------------------------------------------------------
# Cache shim tests run against the shared in-memory S3 double (``fake_s3``
# fixture in conftest.py). GCP is decommissioned: the read-through writes /
# reads via boto3 S3, so artifact URIs are ``s3://`` and the cache store is
# keyed by object key.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test 1 — registration check
# ---------------------------------------------------------------------------


def test_compute_slope_registered():
    """compute_slope is in TOOL_REGISTRY with the expected metadata."""
    assert "compute_slope" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["compute_slope"]
    assert entry.metadata.cacheable is True
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "slope"


# ---------------------------------------------------------------------------
# Tests 2–4 — gdaldem subprocess correctness on synthetic DEM
# ---------------------------------------------------------------------------

# These tests invoke gdaldem directly, bypassing the cache shim, to verify
# the GDAL command construction is correct. They are skipped when gdaldem is
# not available in the test environment.

_GDALDEM_AVAILABLE = (
    os.path.isfile(os.path.expanduser("~/miniforge3/envs/grace2/bin/gdaldem"))
    or bool(__import__("shutil").which("gdaldem"))
    or (
        bool(os.environ.get("TRID3NT_GDALDEM_BIN"))
        and os.path.isfile(os.environ.get("TRID3NT_GDALDEM_BIN", ""))
    )
)
_SKIP_GDALDEM = pytest.mark.skipif(
    not _GDALDEM_AVAILABLE,
    reason="gdaldem binary not available in this environment",
)


@_SKIP_GDALDEM
def test_compute_slope_degrees_known_gradient():
    """32×32 DEM with 1° N-S gradient → mean slope ≈ 1° (±0.1°)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        out_path = os.path.join(tmpdir, "slope.tif")
        _write_synthetic_dem(dem_path, slope_deg=1.0)
        _run_gdaldem_slope(dem_path, out_path, output_unit="degrees", algorithm="Horn")
        mean_slope = _read_slope_mean(out_path)
        assert abs(mean_slope - 1.0) < 0.1, (
            f"Expected mean slope ≈ 1.0°, got {mean_slope:.4f}°"
        )


@_SKIP_GDALDEM
def test_compute_slope_percent_conversion():
    """Same 1° gradient → percent output ≈ tan(1°)*100 ≈ 1.745% (±0.1%)."""
    expected_pct = math.tan(math.radians(1.0)) * 100.0  # ≈ 1.7455
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        out_path = os.path.join(tmpdir, "slope_pct.tif")
        _write_synthetic_dem(dem_path, slope_deg=1.0)
        _run_gdaldem_slope(dem_path, out_path, output_unit="percent", algorithm="Horn")
        mean_slope = _read_slope_mean(out_path)
        assert abs(mean_slope - expected_pct) < 0.1, (
            f"Expected mean slope ≈ {expected_pct:.4f}%, got {mean_slope:.4f}%"
        )


@_SKIP_GDALDEM
def test_compute_slope_horn_vs_zeventhorne_both_succeed():
    """Both algorithm choices produce output without error on the synthetic DEM."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        _write_synthetic_dem(dem_path, slope_deg=5.0)

        for algo in ("Horn", "ZevenbergenThorne"):
            out_path = os.path.join(tmpdir, f"slope_{algo}.tif")
            # Should not raise
            _run_gdaldem_slope(dem_path, out_path, output_unit="degrees", algorithm=algo)
            assert os.path.isfile(out_path), f"Output not created for algorithm={algo}"
            mean_slope = _read_slope_mean(out_path)
            # Sanity: both algorithms should give ≈ 5° for a clean synthetic DEM.
            assert abs(mean_slope - 5.0) < 0.5, (
                f"algorithm={algo}: expected ≈5°, got {mean_slope:.4f}°"
            )


# ---------------------------------------------------------------------------
# Tests 5–7 — cache shim integration (mocked GCS + mocked gdaldem)
# ---------------------------------------------------------------------------


def _make_fake_slope_bytes() -> bytes:
    """Return a minimal valid GeoTIFF payload (used as fake gdaldem output)."""
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        path = tmp.name
    try:
        data = np.ones((4, 4), dtype=np.float32)
        transform = from_bounds(0, 0, 40, 40, 4, 4)
        profile = {
            "driver": "GTiff",
            "dtype": "float32",
            "width": 4,
            "height": 4,
            "count": 1,
            "crs": "EPSG:5070",
            "transform": transform,
        }
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(data, 1)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _fake_dem_bytes() -> bytes:
    """Return minimal DEM GeoTIFF bytes (used as fake DEM download)."""
    return _make_fake_slope_bytes()


def test_compute_slope_cache_miss_writes(fake_s3):
    """On cache miss: gdaldem is called and bytes are written to the cache bucket."""
    fake_dem = _fake_dem_bytes()

    # Pre-seed the DEM bytes as the download return value by patching
    # _download_dem_bytes. The cache write routes through the boto3 S3 double.
    with patch(
        "trid3nt_server.tools.processing.compute_slope._download_dem_bytes",
        return_value=fake_dem,
    ) as mock_download, patch(
        "trid3nt_server.tools.processing.compute_slope._run_gdaldem_slope",
        side_effect=lambda inp, out, unit, algo: open(out, "wb").write(_make_fake_slope_bytes()) or None,
    ) as mock_gdaldem, patch(
        "trid3nt_server.tools.cache.CACHE_BUCKET", "test-bucket"
    ):
        result = compute_slope(
            dem_uri="s3://test-bucket/cache/static-30d/dem/abc123.tif",
            output_unit="degrees",
            algorithm="Horn",
            _bucket="test-bucket",
        )

    assert result.layer_type == "raster"
    assert result.role == "context"
    assert result.units == "degrees"
    assert result.uri.startswith("s3://")
    assert "/slope/" in result.uri
    mock_download.assert_called_once()
    mock_gdaldem.assert_called_once()


def test_compute_slope_cache_hit_skips_fetch(fake_s3):
    """On cache hit: gdaldem is NOT invoked; cached bytes are returned."""
    fake_slope = _make_fake_slope_bytes()

    # Pre-seed the fake slope bytes in the cache store.
    # We need to know the cache path first — compute it the same way cache.py does.
    from trid3nt_server.tools.cache import compute_cache_key, cache_path as make_cache_path

    params = {
        "dem_uri": "s3://test-bucket/cache/static-30d/dem/abc123.tif",
        "output_unit": "degrees",
        "algorithm": "Horn",
    }
    key = compute_cache_key("slope", params, "static-30d", now=PINNED_NOW)
    path = make_cache_path("slope", "static-30d", key, "tif")
    fake_s3.store[path] = fake_slope

    gdaldem_called = []

    def _no_gdaldem(*args, **kwargs):
        gdaldem_called.append(args)

    with patch(
        "trid3nt_server.tools.processing.compute_slope._run_gdaldem_slope",
        side_effect=_no_gdaldem,
    ), patch(
        "trid3nt_server.tools.processing.compute_slope._download_dem_bytes",
        return_value=b"",
    ), patch(
        "trid3nt_server.tools.cache.CACHE_BUCKET", "test-bucket"
    ):
        result = compute_slope(
            dem_uri="s3://test-bucket/cache/static-30d/dem/abc123.tif",
            output_unit="degrees",
            algorithm="Horn",
            _bucket="test-bucket",
        )

    # gdaldem must NOT have been called (cache hit).
    assert len(gdaldem_called) == 0, "gdaldem was called on cache hit — should not be"
    assert result.uri.endswith(f"{key}.tif"), (
        f"URI should reference the cache key; got {result.uri!r}"
    )


def test_compute_slope_returns_layer_uri_fields(fake_s3):
    """LayerURI returned by compute_slope has the expected field values."""
    fake_dem = _fake_dem_bytes()

    with patch(
        "trid3nt_server.tools.processing.compute_slope._download_dem_bytes",
        return_value=fake_dem,
    ), patch(
        "trid3nt_server.tools.processing.compute_slope._run_gdaldem_slope",
        side_effect=lambda inp, out, unit, algo: open(out, "wb").write(_make_fake_slope_bytes()) or None,
    ):
        result = compute_slope(
            dem_uri="s3://bucket/cache/static-30d/dem/deadbeef.tif",
            output_unit="percent",
            algorithm="ZevenbergenThorne",
            _bucket="test-bucket",
        )

    assert result.layer_type == "raster"
    assert result.role == "context"
    assert result.units == "percent"
    assert "slope" in result.layer_id
    assert "percent" in result.layer_id
    assert "ZevenbergenThorne" in result.layer_id
    # name includes unit symbol
    assert "%" in result.name


# ---------------------------------------------------------------------------
# Tests 8–9 — error path coverage
# ---------------------------------------------------------------------------


def test_compute_slope_gdaldem_failure_raises_slope_compute_error(fake_s3):
    """Non-zero gdaldem exit → SlopeComputeError with error_code='GDALDEM_FAILED'."""
    fake_dem = _fake_dem_bytes()

    with patch(
        "trid3nt_server.tools.processing.compute_slope._download_dem_bytes",
        return_value=fake_dem,
    ), patch(
        "trid3nt_server.tools.processing.compute_slope._get_gdaldem_bin",
        return_value="/bin/false",  # always exits 1
    ):
        with pytest.raises(SlopeComputeError) as exc_info:
            compute_slope(
                dem_uri="s3://bucket/dem/abc.tif",
                _bucket="test-bucket",
            )
    assert exc_info.value.error_code == "GDALDEM_FAILED"


def test_compute_slope_dem_download_failure_raises_slope_compute_error(fake_s3):
    """DEM download failure → SlopeComputeError with error_code='DEM_DOWNLOAD_FAILED'."""
    with patch(
        "trid3nt_server.tools.processing.compute_slope._download_dem_bytes",
        side_effect=SlopeComputeError("DEM_DOWNLOAD_FAILED", "S3 download failed"),
    ):
        with pytest.raises(SlopeComputeError) as exc_info:
            compute_slope(
                dem_uri="s3://bucket/dem/missing.tif",
                _bucket="test-bucket",
            )
    assert exc_info.value.error_code == "DEM_DOWNLOAD_FAILED"


# ---------------------------------------------------------------------------
# Test — cache key varies across all 4 parameter combos
# ---------------------------------------------------------------------------


def test_cache_keys_vary_across_combos():
    """Cache keys differ for each (output_unit, algorithm) combination."""
    from trid3nt_server.tools.cache import compute_cache_key

    dem_uri = "gs://bucket/cache/static-30d/dem/somekey.tif"
    combos = [
        ("degrees", "Horn"),
        ("degrees", "ZevenbergenThorne"),
        ("percent", "Horn"),
        ("percent", "ZevenbergenThorne"),
    ]
    keys = set()
    for unit, algo in combos:
        params = {"dem_uri": dem_uri, "output_unit": unit, "algorithm": algo}
        key = compute_cache_key("slope", params, "static-30d", now=PINNED_NOW)
        keys.add(key)

    assert len(keys) == 4, (
        f"Expected 4 distinct cache keys for 4 combos; got {len(keys)} unique: {keys}"
    )
