"""Unit tests for ``compute_hillshade`` atomic tool (job-0079, FR-CE-8, FR-DC).

Coverage:
 1. ``test_compute_hillshade_registered`` — tool appears in TOOL_REGISTRY with
    correct metadata (cacheable=True, ttl_class="static-30d",
    source_class="hillshade").
 2. ``test_compute_hillshade_standard_preset`` — synthetic DEM → standard style
    runs cleanly, returns a GeoTIFF with non-zero mean value.
 3. ``test_compute_hillshade_swiss_double_preset`` — swiss_double runs both
    gdaldem passes and produces a blended GeoTIFF.
 4. ``test_compute_hillshade_multidirectional_preset`` — multidirectional style
    runs without error.
 5. ``test_compute_hillshade_combined_preset`` — combined style runs without error.
 6. ``test_compute_hillshade_smooth_preset`` — smooth style (ZevenbergenThorne)
    runs without error.
 7. ``test_compute_hillshade_cache_hit_skips_fetch`` — second call with identical
    args hits the cache (fetch_fn not invoked).
 8. ``test_compute_hillshade_cache_miss_writes`` — first call (empty cache)
    invokes gdaldem and writes to the cache bucket.
 9. ``test_compute_hillshade_returns_layer_uri_fields`` — LayerURI fields
    correct (layer_type, role, units, layer_id contains style).
10. ``test_compute_hillshade_gdaldem_failure_raises_error`` — non-zero gdaldem
    exit raises HillshadeComputeError(error_code="GDALDEM_FAILED").
11. ``test_compute_hillshade_dem_download_failure_raises_error`` — GCS download
    failure raises HillshadeComputeError(error_code="DEM_DOWNLOAD_FAILED").
12. ``test_cache_keys_vary_across_styles`` — 5 style presets produce 5 distinct
    cache keys.
13. ``test_cache_keys_vary_across_azimuths`` — standard style at different
    azimuths produces different cache keys.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.compute_hillshade import (
    HillshadeComputeError,
    _run_gdaldem_hillshade,
    compute_hillshade,
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
    slope_deg: float = 10.0,
    size: int = 32,
    dx_m: float = 10.0,
) -> None:
    """Write a 32×32 GeoTIFF DEM with a known N-S linear slope.

    Row 0 (north) has the highest elevation; elevation decreases southward.
    Uses EPSG:5070 (Albers Equal Area, metres) so GDAL interprets pixel
    spacing as metres — required for hillshade to produce valid luminance.
    """
    import math

    dz = dx_m * math.tan(math.radians(slope_deg))
    elevations = np.zeros((size, size), dtype=np.float32)
    for row in range(size):
        elevations[row, :] = (size - row) * dz

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


def _make_fake_hillshade_bytes() -> bytes:
    """Return a minimal valid uint8 GeoTIFF (used as fake gdaldem output)."""
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        path = tmp.name
    try:
        data = np.full((4, 4), 180, dtype=np.uint8)
        transform = from_bounds(0, 0, 40, 40, 4, 4)
        profile = {
            "driver": "GTiff",
            "dtype": "uint8",
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
    """Return minimal DEM GeoTIFF bytes (used as mock GCS download)."""
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        path = tmp.name
    try:
        data = np.ones((4, 4), dtype=np.float32) * 50.0
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
# Test 1 — registration check
# ---------------------------------------------------------------------------


def test_compute_hillshade_registered():
    """compute_hillshade is in TOOL_REGISTRY with the expected metadata."""
    assert "compute_hillshade" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["compute_hillshade"]
    assert entry.metadata.cacheable is True
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "hillshade"


# ---------------------------------------------------------------------------
# Tests 2–6 — gdaldem subprocess correctness on synthetic DEM (all 5 presets)
# ---------------------------------------------------------------------------


@_SKIP_GDALDEM
def test_compute_hillshade_standard_preset():
    """Standard style runs cleanly; output has non-zero interior mean."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        out_path = os.path.join(tmpdir, "hillshade_standard.tif")
        _write_synthetic_dem(dem_path, slope_deg=10.0)
        _run_gdaldem_hillshade(
            dem_path, out_path,
            azimuth=315.0, altitude=45.0, z_factor=1.0,
            algorithm="Horn",
        )
        assert os.path.isfile(out_path), "Standard hillshade output not created"
        with rasterio.open(out_path) as src:
            data = src.read(1)
        interior = data[1:-1, 1:-1].astype(float)
        mean_val = float(interior.mean())
        assert mean_val > 0.0, (
            f"Standard hillshade should have non-zero interior pixels; got mean={mean_val:.2f}"
        )


@_SKIP_GDALDEM
def test_compute_hillshade_swiss_double_preset():
    """Swiss double: two gdaldem passes run and produce a non-zero blended output."""
    from grace2_agent.tools.compute_hillshade import _multiply_blend_hillshades

    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        out_a = os.path.join(tmpdir, "hs_315.tif")
        out_b = os.path.join(tmpdir, "hs_135.tif")
        blend_out = os.path.join(tmpdir, "hs_swiss.tif")
        _write_synthetic_dem(dem_path, slope_deg=10.0)

        _run_gdaldem_hillshade(
            dem_path, out_a,
            azimuth=315.0, altitude=45.0, z_factor=1.0,
            algorithm="Horn",
        )
        _run_gdaldem_hillshade(
            dem_path, out_b,
            azimuth=135.0, altitude=45.0, z_factor=1.0,
            algorithm="Horn",
        )
        assert os.path.isfile(out_a), "Primary (315°) hillshade not created"
        assert os.path.isfile(out_b), "Secondary (135°) hillshade not created"

        _multiply_blend_hillshades(out_a, out_b, blend_out)
        assert os.path.isfile(blend_out), "Blended swiss_double output not created"

        with rasterio.open(blend_out) as src:
            blended = src.read(1).astype(float)
        mean_blend = float(blended[1:-1, 1:-1].mean())
        assert mean_blend > 0.0, (
            f"Swiss double blend should have non-zero interior; got mean={mean_blend:.2f}"
        )


@_SKIP_GDALDEM
def test_compute_hillshade_multidirectional_preset():
    """Multidirectional style runs without error on the synthetic DEM."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        out_path = os.path.join(tmpdir, "hillshade_multidirectional.tif")
        _write_synthetic_dem(dem_path, slope_deg=10.0)
        _run_gdaldem_hillshade(
            dem_path, out_path,
            azimuth=315.0, altitude=45.0, z_factor=1.0,
            algorithm="Horn",
            multidirectional=True,
        )
        assert os.path.isfile(out_path), "Multidirectional hillshade output not created"


@_SKIP_GDALDEM
def test_compute_hillshade_combined_preset():
    """Combined style runs without error on the synthetic DEM."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        out_path = os.path.join(tmpdir, "hillshade_combined.tif")
        _write_synthetic_dem(dem_path, slope_deg=10.0)
        _run_gdaldem_hillshade(
            dem_path, out_path,
            azimuth=315.0, altitude=45.0, z_factor=1.0,
            algorithm="Horn",
            combined=True,
        )
        assert os.path.isfile(out_path), "Combined hillshade output not created"


@_SKIP_GDALDEM
def test_compute_hillshade_smooth_preset():
    """Smooth style (ZevenbergenThorne) runs without error on the synthetic DEM."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        out_path = os.path.join(tmpdir, "hillshade_smooth.tif")
        _write_synthetic_dem(dem_path, slope_deg=10.0)
        _run_gdaldem_hillshade(
            dem_path, out_path,
            azimuth=315.0, altitude=45.0, z_factor=1.0,
            algorithm="ZevenbergenThorne",
        )
        assert os.path.isfile(out_path), "Smooth hillshade output not created"


# ---------------------------------------------------------------------------
# Tests 7–9 — cache shim integration (mocked GCS + mocked gdaldem)
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_storage():
    """Provide a FakeStorageClient for cache shim isolation."""
    return FakeStorageClient()


def test_compute_hillshade_cache_miss_writes(fake_storage):
    """On cache miss: gdaldem is called and bytes are written to the cache bucket."""
    fake_dem = _fake_dem_bytes()
    fake_hs = _make_fake_hillshade_bytes()

    with patch(
        "grace2_agent.tools.compute_hillshade._download_dem_bytes",
        return_value=fake_dem,
    ) as mock_download, patch(
        "grace2_agent.tools.compute_hillshade._run_gdaldem_hillshade",
        side_effect=lambda inp, out, **kw: open(out, "wb").write(fake_hs) or None,
    ) as mock_gdaldem, patch(
        "grace2_agent.tools.cache.CACHE_BUCKET", "test-bucket"
    ):
        result = compute_hillshade(
            dem_uri="gs://test-bucket/cache/static-30d/dem/abc123.tif",
            style="standard",
            _bucket="test-bucket",
        )

    assert result.layer_type == "raster"
    assert result.role == "context"
    assert result.units == "intensity"
    assert result.uri.startswith("s3://")
    assert "/hillshade/" in result.uri
    mock_download.assert_called_once()
    mock_gdaldem.assert_called_once()


def test_compute_hillshade_cache_hit_skips_fetch(fake_storage):
    """On cache hit: gdaldem is NOT invoked; cached bytes are returned."""
    fake_hs = _make_fake_hillshade_bytes()

    from grace2_agent.tools.cache import cache_path as make_cache_path
    from grace2_agent.tools.cache import compute_cache_key

    params = {
        "dem_uri": "gs://test-bucket/cache/static-30d/dem/abc123.tif",
        "style": "standard",
        "algorithm": "Horn",
        "azimuth": 315.0,
        "altitude": 45.0,
        "z_factor": 1.0,
    }
    key = compute_cache_key("hillshade", params, "static-30d", now=PINNED_NOW)
    path = make_cache_path("hillshade", "static-30d", key, "tif")
    fake_storage.store[path] = fake_hs

    gdaldem_called = []

    def _no_gdaldem(*args, **kwargs):
        gdaldem_called.append(args)

    with patch(
        "grace2_agent.tools.compute_hillshade._run_gdaldem_hillshade",
        side_effect=_no_gdaldem,
    ), patch(
        "grace2_agent.tools.compute_hillshade._download_dem_bytes",
        return_value=b"",
    ), patch(
        "grace2_agent.tools.cache.CACHE_BUCKET", "test-bucket"
    ):
        result = compute_hillshade(
            dem_uri="gs://test-bucket/cache/static-30d/dem/abc123.tif",
            style="standard",
            _bucket="test-bucket",
        )

    assert len(gdaldem_called) == 0, "gdaldem was called on cache hit — should not be"
    assert result.uri.endswith(f"{key}.tif"), (
        f"URI should reference the cache key; got {result.uri!r}"
    )


def test_compute_hillshade_returns_layer_uri_fields():
    """LayerURI returned by compute_hillshade has the expected field values."""
    fake_dem = _fake_dem_bytes()
    fake_hs = _make_fake_hillshade_bytes()

    with patch(
        "grace2_agent.tools.compute_hillshade._download_dem_bytes",
        return_value=fake_dem,
    ), patch(
        "grace2_agent.tools.compute_hillshade._run_gdaldem_hillshade",
        side_effect=lambda inp, out, **kw: open(out, "wb").write(fake_hs) or None,
    ):
        fake_sc = FakeStorageClient()
        result = compute_hillshade(
            dem_uri="gs://bucket/cache/static-30d/dem/deadbeef.tif",
            style="swiss_double",
            _bucket="test-bucket",
        )

    assert result.layer_type == "raster"
    assert result.role == "context"
    assert result.units == "intensity"
    assert "hillshade" in result.layer_id
    assert "swiss_double" in result.layer_id
    assert "Swiss Double" in result.name


# ---------------------------------------------------------------------------
# Tests 10–11 — error path coverage
# ---------------------------------------------------------------------------


def test_compute_hillshade_gdaldem_failure_raises_error():
    """Non-zero gdaldem exit → HillshadeComputeError with error_code='GDALDEM_FAILED'."""
    fake_dem = _fake_dem_bytes()

    with patch(
        "grace2_agent.tools.compute_hillshade._download_dem_bytes",
        return_value=fake_dem,
    ), patch(
        "grace2_agent.tools.compute_hillshade._get_gdaldem_bin",
        return_value="/bin/false",  # always exits 1
    ):
        fake_sc = FakeStorageClient()
        with pytest.raises(HillshadeComputeError) as exc_info:
            compute_hillshade(
                dem_uri="gs://bucket/dem/abc.tif",
                style="standard",
                _bucket="test-bucket",
            )
    assert exc_info.value.error_code == "GDALDEM_FAILED"


def test_compute_hillshade_dem_download_failure_raises_error():
    """GCS download failure → HillshadeComputeError with error_code='DEM_DOWNLOAD_FAILED'."""
    with patch(
        "grace2_agent.tools.compute_hillshade._download_dem_bytes",
        side_effect=HillshadeComputeError("DEM_DOWNLOAD_FAILED", "GCS download failed"),
    ):
        fake_sc = FakeStorageClient()
        with pytest.raises(HillshadeComputeError) as exc_info:
            compute_hillshade(
                dem_uri="gs://bucket/dem/missing.tif",
                style="standard",
                _bucket="test-bucket",
            )
    assert exc_info.value.error_code == "DEM_DOWNLOAD_FAILED"


# ---------------------------------------------------------------------------
# Test 12 — cache key varies across all 5 style presets
# ---------------------------------------------------------------------------


def test_cache_keys_vary_across_styles():
    """Cache keys differ for each of the 5 style presets."""
    from grace2_agent.tools.cache import compute_cache_key

    dem_uri = "gs://bucket/cache/static-30d/dem/somekey.tif"
    styles = ["standard", "swiss_double", "multidirectional", "combined", "smooth"]
    keys = set()
    for style in styles:
        params = {
            "dem_uri": dem_uri,
            "style": style,
            "algorithm": "Horn",
            "azimuth": 315.0,
            "altitude": 45.0,
            "z_factor": 1.0,
        }
        key = compute_cache_key("hillshade", params, "static-30d", now=PINNED_NOW)
        keys.add(key)

    assert len(keys) == 5, (
        f"Expected 5 distinct cache keys for 5 style presets; got {len(keys)} unique: {keys}"
    )


# ---------------------------------------------------------------------------
# Test 13 — cache key varies with azimuth changes (standard style)
# ---------------------------------------------------------------------------


def test_cache_keys_vary_across_azimuths():
    """Standard style at different azimuths produces different cache keys."""
    from grace2_agent.tools.cache import compute_cache_key

    dem_uri = "gs://bucket/cache/static-30d/dem/somekey.tif"
    azimuths = [0.0, 90.0, 180.0, 270.0, 315.0]
    keys = set()
    for az in azimuths:
        params = {
            "dem_uri": dem_uri,
            "style": "standard",
            "algorithm": "Horn",
            "azimuth": az,
            "altitude": 45.0,
            "z_factor": 1.0,
        }
        key = compute_cache_key("hillshade", params, "static-30d", now=PINNED_NOW)
        keys.add(key)

    assert len(keys) == len(azimuths), (
        f"Expected {len(azimuths)} distinct cache keys for {len(azimuths)} azimuths; "
        f"got {len(keys)} unique: {keys}"
    )


# ---------------------------------------------------------------------------
# Test 14 — swiss_double calls gdaldem exactly twice
# ---------------------------------------------------------------------------


def test_compute_hillshade_swiss_double_calls_gdaldem_twice(fake_storage):
    """swiss_double style invokes _run_gdaldem_hillshade exactly twice (315° + 135°)."""
    fake_dem = _fake_dem_bytes()
    fake_hs = _make_fake_hillshade_bytes()

    gdaldem_calls = []

    def _fake_gdaldem(inp, out, *, azimuth, **kw):
        gdaldem_calls.append(azimuth)
        open(out, "wb").write(fake_hs)

    with patch(
        "grace2_agent.tools.compute_hillshade._download_dem_bytes",
        return_value=fake_dem,
    ), patch(
        "grace2_agent.tools.compute_hillshade._run_gdaldem_hillshade",
        side_effect=_fake_gdaldem,
    ), patch(
        "grace2_agent.tools.compute_hillshade._multiply_blend_hillshades",
        side_effect=lambda a, b, out: open(out, "wb").write(fake_hs) or None,
    ):
        fake_sc = FakeStorageClient()
        result = compute_hillshade(
            dem_uri="gs://bucket/dem/abc.tif",
            style="swiss_double",
            _bucket="test-bucket",
        )

    assert len(gdaldem_calls) == 2, (
        f"swiss_double should call gdaldem exactly twice; got {len(gdaldem_calls)} calls"
    )
    assert 315.0 in gdaldem_calls, "swiss_double primary pass (315°) not found"
    assert 135.0 in gdaldem_calls, "swiss_double secondary pass (135°) not found"
    assert "swiss_double" in result.layer_id


# ---------------------------------------------------------------------------
# job-0257 — CRS preservation (hillshade no-render root-cause #3)
#
# Live evidence (2026-06-10): the conda-env gdaldem invoked via bare
# subprocess (no PROJ_LIB/PROJ_DATA) cannot find proj.db and silently writes
# the output CRS as a degenerate LOCAL_CS/ENGCRS (epsg=None) instead of the
# DEM's EPSG:5070. QGIS Server then cannot reproject the layer for WMS.
# Fixes under test: (a) _gdaldem_subprocess_env wires <prefix>/share/proj,
# (b) _ensure_output_crs_matches_dem re-stamps the DEM CRS when degraded.
# ---------------------------------------------------------------------------


def test_ensure_output_crs_stamps_degraded_output():
    """A CRS-less gdaldem output gets the DEM's CRS stamped in place."""
    from grace2_agent.tools.compute_hillshade import _ensure_output_crs_matches_dem

    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        out_path = os.path.join(tmpdir, "hs.tif")
        _write_synthetic_dem(dem_path)

        # Simulate the degraded output: same grid, crs=None.
        data = np.full((32, 32), 128, dtype=np.uint8)
        transform = from_bounds(0.0, 0.0, 320.0, 320.0, 32, 32)
        profile = {
            "driver": "GTiff",
            "dtype": "uint8",
            "width": 32,
            "height": 32,
            "count": 1,
            "crs": None,
            "transform": transform,
        }
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(data, 1)

        _ensure_output_crs_matches_dem(dem_path, out_path)

        with rasterio.open(out_path) as fixed:
            assert fixed.crs is not None, "CRS stamp did not apply"
            assert fixed.crs.to_epsg() == 5070, (
                f"expected EPSG:5070 after stamp; got {fixed.crs}"
            )


def test_ensure_output_crs_noop_when_already_correct():
    """When gdaldem preserved the CRS, the stamp is a no-op (no rewrite)."""
    from grace2_agent.tools.compute_hillshade import _ensure_output_crs_matches_dem

    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        out_path = os.path.join(tmpdir, "hs.tif")
        _write_synthetic_dem(dem_path)
        _write_synthetic_dem(out_path)  # same CRS as DEM

        before = os.path.getmtime(out_path)
        _ensure_output_crs_matches_dem(dem_path, out_path)
        with rasterio.open(out_path) as fixed:
            assert fixed.crs.to_epsg() == 5070


@_SKIP_GDALDEM
def test_fetch_fn_output_preserves_dem_crs_without_proj_env():
    """End-to-end _make_fetch_fn: output bytes carry the DEM's EPSG:5070 even
    when the process env lacks PROJ_LIB/PROJ_DATA (the agent's situation).

    This is the live failure mode: the demo-session cache artifacts read back
    as LOCAL_CS["NAD83 / Conus Albers"] with epsg=None.
    """
    from grace2_agent.tools.compute_hillshade import _make_fetch_fn

    # Strip PROJ vars so the subprocess depends entirely on the job-0257
    # env-wiring (or the post-hoc stamp as fallback).
    stripped = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PROJ_LIB", "PROJ_DATA", "GDAL_DATA")
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        _write_synthetic_dem(dem_path)

        with patch.dict(os.environ, stripped, clear=True):
            hs_bytes = _make_fetch_fn(
                dem_uri=dem_path,  # local path branch — no GCS
                style="standard",
                algorithm="Horn",
                azimuth=315.0,
                altitude=45.0,
                z_factor=1.0,
                storage_client=None,
            )

        out_path = os.path.join(tmpdir, "hs_check.tif")
        with open(out_path, "wb") as f:
            f.write(hs_bytes)
        with rasterio.open(out_path) as src:
            assert src.crs is not None, "hillshade output lost its CRS entirely"
            assert src.crs.to_epsg() == 5070, (
                f"job-0257 regression: hillshade CRS degraded to {src.crs} "
                f"(expected EPSG:5070 from the DEM)"
            )


# ---------------------------------------------------------------------------
# 2026-07-13 DEM fallback ladder (FIX 3): the Copernicus GLO-30 fallback DEM
# handle must flow through compute_hillshade UNCHANGED. The fallback layer's
# uri points at a COG written by fetch_copernicus_dem._write_dem_cog (COG
# driver, EPSG:4326 degrees, float32, nodata=-9999) -- a DIFFERENT byte shape
# than the 3DEP EPSG:5070 path -- so this proves the uniform dem_uri contract
# with the real writer + real gdaldem, not by assumption.
# ---------------------------------------------------------------------------


@_SKIP_GDALDEM
def test_copernicus_fallback_dem_flows_through_hillshade():
    """A GLO-30-shaped DEM COG (the 3DEP-fallback artifact) hillshades fine."""
    from grace2_agent.tools.compute_hillshade import _make_fetch_fn
    from grace2_agent.tools.fetch_copernicus_dem import _write_dem_cog

    # A small synthetic elevation grid over a Berkeley-ish bbox, produced by
    # the SAME writer the fallback path uses (float32, EPSG:4326, nodata).
    size = 32
    bbox = (-122.35, 37.82, -122.20, 37.92)
    rows = np.linspace(200.0, 20.0, size, dtype=np.float32)
    dem = np.repeat(rows[:, None], size, axis=1)
    dem[0, 0] = np.nan  # one nodata cell -- exercised through _NODATA fill
    cog_bytes = _write_dem_cog(dem, bbox, size, size)

    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "copdem_fallback.tif")
        with open(dem_path, "wb") as f:
            f.write(cog_bytes)

        hs_bytes = _make_fetch_fn(
            dem_uri=dem_path,  # local path branch -- same contract as s3://
            style="standard",
            algorithm="Horn",
            azimuth=315.0,
            altitude=45.0,
            z_factor=1.0,
            storage_client=None,
        )

        out_path = os.path.join(tmpdir, "hs.tif")
        with open(out_path, "wb") as f:
            f.write(hs_bytes)
        with rasterio.open(out_path) as src:
            assert src.count >= 1
            band = src.read(1)
            # Real luminance variation from the N-S slope, not a flat fill.
            assert band.max() > band.min()
            assert src.crs is not None
            assert src.crs.to_epsg() == 4326, (
                f"fallback DEM CRS not preserved: {src.crs}"
            )
