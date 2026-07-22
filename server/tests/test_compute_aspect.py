"""Unit tests for ``compute_aspect`` atomic tool (job-0082, FR-CE-8, FR-DC).

Coverage:
1. ``test_compute_aspect_registered`` — tool appears in TOOL_REGISTRY with
   correct metadata (cacheable=True, ttl_class="static-30d",
   source_class="aspect").
2. ``test_compute_aspect_south_facing_slope`` — 32×32 synthetic DEM with a
   known south-facing slope (elevation increases northward → faces south)
   → aspect ≈ 180° (within ±10° tolerance, interior pixels).
3. ``test_compute_aspect_horn_vs_zeventhorne_both_succeed`` — both algorithm
   choices run without error on the synthetic DEM and return near-180°.
4. ``test_compute_aspect_zero_for_flat_true`` — flat DEM with zero_for_flat=True
   → output pixels are 0 (not -9999).
5. ``test_compute_aspect_zero_for_flat_false`` — flat DEM with zero_for_flat=False
   → output pixels are -9999 (or no-data) not 0.
6. ``test_compute_aspect_cache_hit_skips_fetch`` — second call with identical
   args hits the cache (fetch_fn not invoked).
7. ``test_compute_aspect_cache_miss_writes`` — first call (empty cache) invokes
   gdaldem and writes to cache bucket.
8. ``test_compute_aspect_returns_layer_uri`` — LayerURI fields correct (layer_type,
   role, units match).
9. ``test_compute_aspect_gdaldem_failure_raises_aspect_compute_error`` — non-zero
   gdaldem exit raises AspectComputeError(error_code="GDALDEM_FAILED").
10. ``test_compute_aspect_dem_download_failure_raises_aspect_compute_error`` — GCS
    download failure raises AspectComputeError(error_code="DEM_DOWNLOAD_FAILED").
11. ``test_cache_keys_vary_across_combos`` — 4 (algorithm × zero_for_flat) combos
    produce 4 distinct cache keys.
"""

from __future__ import annotations

import math
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.compute_aspect import (
    AspectComputeError,
    _run_gdaldem_aspect,
    compute_aspect,
)

# ---------------------------------------------------------------------------
# Pinned timestamp for deterministic cache keys
# ---------------------------------------------------------------------------

PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Helpers: synthetic DEM creation
# ---------------------------------------------------------------------------


def _write_synthetic_dem_south_facing(
    path: str,
    slope_deg: float = 10.0,
    size: int = 32,
    dx_m: float = 10.0,
) -> None:
    """Write a 32×32 GeoTIFF DEM with a known south-facing slope.

    A south-facing slope has elevation increasing from south to north (row index
    increases → elevation increases). GDAL's raster convention: row 0 is the
    northernmost row, row (size-1) is the southernmost row.

    So elevation increases as row index increases (going south → lower elevation
    in south, higher in north means row 0 = highest, row n-1 = lowest —
    that's a NORTH-facing slope).

    For a SOUTH-facing slope: row 0 (north) = lowest elevation, row n-1
    (south) = highest elevation. The gradient descends northward → aspect ≈ 180°
    (facing south, downslope toward north).

    Wait — aspect is the direction the slope *faces* (i.e., the direction of the
    downslope direction). A slope that descends toward the north faces NORTH
    (aspect ≈ 0°/360°). A slope that descends toward the south faces SOUTH
    (aspect ≈ 180°).

    For aspect ≈ 180° (south-facing):
    - The terrain descends toward the south.
    - Row 0 (north edge) has HIGH elevation; row n-1 (south edge) has LOW elevation.
    - This matches the same layout as a "downslope toward south" gradient.

    GDAL's raster row ordering: row 0 = top (north in a north-up raster).
    Transform: north edge = max_y, south edge = min_y.
    For descent southward: elevation[row] decreases as row increases.
    → row 0 = highest (north), row n-1 = lowest (south).
    → downslope direction = south → aspect = 180°.
    """
    dz = dx_m * math.tan(math.radians(slope_deg))
    # Row 0 = highest (north); elevation decreases southward → south-facing slope.
    elevations = np.zeros((size, size), dtype=np.float32)
    for row in range(size):
        elevations[row, :] = (size - row) * dz

    # Projected CRS in meters so GDAL interprets pixel spacing correctly.
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


def _write_flat_dem(
    path: str,
    size: int = 32,
    dx_m: float = 10.0,
    elevation: float = 100.0,
) -> None:
    """Write a 32×32 perfectly flat GeoTIFF DEM."""
    elevations = np.full((size, size), elevation, dtype=np.float32)
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


def _read_aspect_interior_values(path: str) -> np.ndarray:
    """Read interior (non-border) pixels from an aspect GeoTIFF."""
    with rasterio.open(path) as src:
        data = src.read(1)
        nodata = src.nodata
    interior = data[1:-1, 1:-1].flatten()
    if nodata is not None:
        interior = interior[interior != nodata]
    return interior


# ---------------------------------------------------------------------------
# FakeBlob / FakeStorageClient for cache shim tests
# ---------------------------------------------------------------------------


class _S3Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeStorageClient:
    """In-memory S3 double (GCP decommissioned). ``store`` keyed by object KEY.

    Returns the per-test active instance installed by the autouse
    ``_route_cache_to_inmemory_s3`` fixture so the tool's real S3 read-through
    (boto3) reads/writes the same store the test inspects.
    """

    _active: "FakeStorageClient | None" = None

    def __new__(cls) -> "FakeStorageClient":
        if cls._active is not None:
            return cls._active
        return super().__new__(cls)

    def __init__(self) -> None:
        if getattr(self, "_init", False):
            return
        self._init = True
        self.store: dict[str, bytes] = {}
        self.last_put: dict | None = None

    def get_object(self, *, Bucket, Key):
        from botocore.exceptions import ClientError

        try:
            data = self.store[Key]
        except KeyError:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            )
        return {"Body": _S3Body(data)}

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        data = Body.read() if hasattr(Body, "read") else Body
        self.store[Key] = data
        self.last_put = {"Bucket": Bucket, "Key": Key, "ContentType": ContentType}
        return {}


@pytest.fixture(autouse=True)
def _route_cache_to_inmemory_s3(monkeypatch):
    """Route boto3 S3 (the cache shim's only object store) to an in-memory double."""
    import boto3

    FakeStorageClient._active = None
    client = FakeStorageClient()
    FakeStorageClient._active = client

    def _factory(service_name, *a, **k):
        assert service_name == "s3"
        return client

    monkeypatch.setattr(boto3, "client", _factory)
    try:
        yield client
    finally:
        FakeStorageClient._active = None


# ---------------------------------------------------------------------------
# gdaldem availability check
# ---------------------------------------------------------------------------

_GDALDEM_AVAILABLE = (
    os.path.isfile(os.path.expanduser("~/miniforge3/envs/grace2/bin/gdaldem"))
    or bool(__import__("shutil").which("gdaldem"))
    or (
        bool(os.environ.get("GRACE2_GDALDEM_BIN"))
        and os.path.isfile(os.environ.get("GRACE2_GDALDEM_BIN", ""))
    )
)
_SKIP_GDALDEM = pytest.mark.skipif(
    not _GDALDEM_AVAILABLE,
    reason="gdaldem binary not available in this environment",
)

# ---------------------------------------------------------------------------
# Helper: fake GeoTIFF bytes (used as mock gdaldem output / mock DEM bytes)
# ---------------------------------------------------------------------------


def _make_fake_aspect_bytes() -> bytes:
    """Return a minimal valid GeoTIFF payload (used as fake gdaldem output)."""
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        path = tmp.name
    try:
        data = np.ones((4, 4), dtype=np.float32) * 180.0
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
    """Return minimal DEM GeoTIFF bytes (used as fake GCS download)."""
    return _make_fake_aspect_bytes()


# ---------------------------------------------------------------------------
# Test 1 — registration check
# ---------------------------------------------------------------------------


def test_compute_aspect_registered():
    """compute_aspect is in TOOL_REGISTRY with the expected metadata."""
    assert "compute_aspect" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["compute_aspect"]
    assert entry.metadata.cacheable is True
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "aspect"


# ---------------------------------------------------------------------------
# Tests 2–5 — gdaldem subprocess correctness on synthetic DEMs
# ---------------------------------------------------------------------------


@_SKIP_GDALDEM
def test_compute_aspect_south_facing_slope():
    """32×32 DEM with south-facing slope (descends southward) → aspect ≈ 180° (±10°)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        out_path = os.path.join(tmpdir, "aspect.tif")
        _write_synthetic_dem_south_facing(dem_path, slope_deg=10.0)
        _run_gdaldem_aspect(dem_path, out_path, algorithm="Horn", zero_for_flat=True)
        values = _read_aspect_interior_values(out_path)
        assert len(values) > 0, "No valid interior pixels in aspect output"
        mean_aspect = float(np.mean(values))
        assert abs(mean_aspect - 180.0) < 10.0, (
            f"Expected mean aspect ≈ 180° for south-facing slope, got {mean_aspect:.2f}°"
        )


@_SKIP_GDALDEM
def test_compute_aspect_horn_vs_zeventhorne_both_succeed():
    """Both algorithm choices produce output without error and return near-180°."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        _write_synthetic_dem_south_facing(dem_path, slope_deg=10.0)

        for algo in ("Horn", "ZevenbergenThorne"):
            out_path = os.path.join(tmpdir, f"aspect_{algo}.tif")
            _run_gdaldem_aspect(dem_path, out_path, algorithm=algo, zero_for_flat=True)
            assert os.path.isfile(out_path), f"Output not created for algorithm={algo}"
            values = _read_aspect_interior_values(out_path)
            assert len(values) > 0
            mean_aspect = float(np.mean(values))
            assert abs(mean_aspect - 180.0) < 10.0, (
                f"algorithm={algo}: expected ≈180°, got {mean_aspect:.2f}°"
            )


@_SKIP_GDALDEM
def test_compute_aspect_zero_for_flat_true():
    """Flat DEM with zero_for_flat=True → aspect values are 0 (not -9999)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "flat.tif")
        out_path = os.path.join(tmpdir, "aspect_flat.tif")
        _write_flat_dem(dem_path)
        _run_gdaldem_aspect(dem_path, out_path, algorithm="Horn", zero_for_flat=True)
        with rasterio.open(out_path) as src:
            data = src.read(1)
            nodata = src.nodata
        # With -zero_for_flat: flat pixels should be 0
        interior = data[1:-1, 1:-1].flatten()
        if nodata is not None:
            valid = interior[interior != nodata]
        else:
            valid = interior
        # Interior pixels of a perfectly flat DEM should all be 0.
        assert np.all(valid == 0.0), (
            f"Expected all flat interior pixels = 0 with zero_for_flat=True; "
            f"got unique values: {np.unique(valid)}"
        )


@_SKIP_GDALDEM
def test_compute_aspect_zero_for_flat_false():
    """Flat DEM with zero_for_flat=False → flat pixels are nodata (-9999), not 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "flat.tif")
        out_path = os.path.join(tmpdir, "aspect_flat_nozff.tif")
        _write_flat_dem(dem_path)
        _run_gdaldem_aspect(dem_path, out_path, algorithm="Horn", zero_for_flat=False)
        with rasterio.open(out_path) as src:
            data = src.read(1)
            nodata = src.nodata
        interior = data[1:-1, 1:-1].flatten()
        # Without -zero_for_flat: flat pixels should be -9999 (gdaldem default).
        # Some GDAL builds use nodata= -9999; others leave raw -9999 in the array.
        sentinel = nodata if nodata is not None else -9999.0
        valid = interior[interior != sentinel]
        # All interior flat pixels should be flagged (no valid 0 values).
        assert len(valid) == 0, (
            f"Expected all flat interior pixels to be nodata ({sentinel}) with "
            f"zero_for_flat=False; got {len(valid)} non-nodata values"
        )


# ---------------------------------------------------------------------------
# Tests 6–8 — cache shim integration (mocked GCS + mocked gdaldem)
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_storage():
    """Provide a FakeStorageClient for cache shim isolation."""
    return FakeStorageClient()


def test_compute_aspect_cache_miss_writes(fake_storage):
    """On cache miss: gdaldem is called and bytes are written to the cache bucket."""
    fake_dem = _fake_dem_bytes()

    with patch(
        "grace2_agent.tools.compute_aspect._download_dem_bytes",
        return_value=fake_dem,
    ) as mock_download, patch(
        "grace2_agent.tools.compute_aspect._run_gdaldem_aspect",
        side_effect=lambda inp, out, algo, zff: open(out, "wb").write(_make_fake_aspect_bytes()) or None,
    ) as mock_gdaldem, patch(
        "grace2_agent.tools.cache.CACHE_BUCKET", "test-bucket"
    ):
        result = compute_aspect(
            dem_uri="gs://test-bucket/cache/static-30d/dem/abc123.tif",
            algorithm="Horn",
            zero_for_flat=True,
            _bucket="test-bucket",
        )

    assert result.layer_type == "raster"
    assert result.role == "context"
    assert result.units == "degrees"
    assert result.uri.startswith("s3://")
    assert "/aspect/" in result.uri
    mock_download.assert_called_once()
    mock_gdaldem.assert_called_once()


def test_compute_aspect_cache_hit_skips_fetch(fake_storage):
    """On cache hit: gdaldem is NOT invoked; cached bytes are returned."""
    fake_aspect = _make_fake_aspect_bytes()

    from grace2_agent.tools.cache import cache_path as make_cache_path
    from grace2_agent.tools.cache import compute_cache_key

    params = {
        "dem_uri": "gs://test-bucket/cache/static-30d/dem/abc123.tif",
        "algorithm": "Horn",
        "zero_for_flat": True,
    }
    key = compute_cache_key("aspect", params, "static-30d", now=PINNED_NOW)
    path = make_cache_path("aspect", "static-30d", key, "tif")
    fake_storage.store[path] = fake_aspect

    gdaldem_called = []

    def _no_gdaldem(*args, **kwargs):
        gdaldem_called.append(args)

    with patch(
        "grace2_agent.tools.compute_aspect._run_gdaldem_aspect",
        side_effect=_no_gdaldem,
    ), patch(
        "grace2_agent.tools.compute_aspect._download_dem_bytes",
        return_value=b"",
    ), patch(
        "grace2_agent.tools.cache.CACHE_BUCKET", "test-bucket"
    ):
        result = compute_aspect(
            dem_uri="gs://test-bucket/cache/static-30d/dem/abc123.tif",
            algorithm="Horn",
            zero_for_flat=True,
            _bucket="test-bucket",
        )

    assert len(gdaldem_called) == 0, "gdaldem was called on cache hit — should not be"
    assert result.uri.endswith(f"{key}.tif"), (
        f"URI should reference the cache key; got {result.uri!r}"
    )


def test_compute_aspect_returns_layer_uri_fields():
    """LayerURI returned by compute_aspect has the expected field values."""
    fake_dem = _fake_dem_bytes()

    with patch(
        "grace2_agent.tools.compute_aspect._download_dem_bytes",
        return_value=fake_dem,
    ), patch(
        "grace2_agent.tools.compute_aspect._run_gdaldem_aspect",
        side_effect=lambda inp, out, algo, zff: open(out, "wb").write(_make_fake_aspect_bytes()) or None,
    ):
        fake_sc = FakeStorageClient()
        result = compute_aspect(
            dem_uri="gs://bucket/cache/static-30d/dem/deadbeef.tif",
            algorithm="ZevenbergenThorne",
            zero_for_flat=False,
            _bucket="test-bucket",
        )

    assert result.layer_type == "raster"
    assert result.role == "context"
    assert result.units == "degrees"
    assert "aspect" in result.layer_id
    assert "ZevenbergenThorne" in result.layer_id
    assert "nozff" in result.layer_id
    assert "ZevenbergenThorne" in result.name


# ---------------------------------------------------------------------------
# Tests 9–10 — error path coverage
# ---------------------------------------------------------------------------


def test_compute_aspect_gdaldem_failure_raises_aspect_compute_error():
    """Non-zero gdaldem exit → AspectComputeError with error_code='GDALDEM_FAILED'."""
    fake_dem = _fake_dem_bytes()

    with patch(
        "grace2_agent.tools.compute_aspect._download_dem_bytes",
        return_value=fake_dem,
    ), patch(
        "grace2_agent.tools.compute_aspect._get_gdaldem_bin",
        return_value="/bin/false",  # always exits 1
    ):
        fake_sc = FakeStorageClient()
        with pytest.raises(AspectComputeError) as exc_info:
            compute_aspect(
                dem_uri="gs://bucket/dem/abc.tif",
                _bucket="test-bucket",
            )
    assert exc_info.value.error_code == "GDALDEM_FAILED"


def test_compute_aspect_dem_download_failure_raises_aspect_compute_error():
    """GCS download failure → AspectComputeError with error_code='DEM_DOWNLOAD_FAILED'."""
    with patch(
        "grace2_agent.tools.compute_aspect._download_dem_bytes",
        side_effect=AspectComputeError("DEM_DOWNLOAD_FAILED", "GCS download failed"),
    ):
        fake_sc = FakeStorageClient()
        with pytest.raises(AspectComputeError) as exc_info:
            compute_aspect(
                dem_uri="gs://bucket/dem/missing.tif",
                _bucket="test-bucket",
            )
    assert exc_info.value.error_code == "DEM_DOWNLOAD_FAILED"


# ---------------------------------------------------------------------------
# Test 11 — cache key varies across all 4 parameter combos
# ---------------------------------------------------------------------------


def test_cache_keys_vary_across_combos():
    """Cache keys differ for each (algorithm, zero_for_flat) combination."""
    from grace2_agent.tools.cache import compute_cache_key

    dem_uri = "gs://bucket/cache/static-30d/dem/somekey.tif"
    combos = [
        ("Horn", True),
        ("Horn", False),
        ("ZevenbergenThorne", True),
        ("ZevenbergenThorne", False),
    ]
    keys = set()
    for algo, zff in combos:
        params = {"dem_uri": dem_uri, "algorithm": algo, "zero_for_flat": zff}
        key = compute_cache_key("aspect", params, "static-30d", now=PINNED_NOW)
        keys.add(key)

    assert len(keys) == 4, (
        f"Expected 4 distinct cache keys for 4 combos; got {len(keys)} unique: {keys}"
    )
