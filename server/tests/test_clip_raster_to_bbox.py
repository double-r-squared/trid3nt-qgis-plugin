"""Unit tests for ``clip_raster_to_bbox`` atomic tool (job-0085, FR-CE-8, FR-DC).

Coverage:
1. ``test_clip_raster_to_bbox_registered`` — tool appears in TOOL_REGISTRY with
   correct metadata (cacheable=True, ttl_class="static-30d",
   source_class="clip_raster").
2. ``test_clip_quadrant_produces_smaller_raster`` — 256×256 synthetic raster +
   top-right quadrant bbox → output ~128×128 pixels.
3. ``test_clip_reproject_4326_to_3857`` — same raster + target_crs="EPSG:3857" →
   output CRS is EPSG:3857 (gdalwarp path exercised).
4. ``test_cache_miss_writes_and_hit_skips_gdal`` — first call (miss) invokes GDAL;
   second call with same args (hit) does NOT.
5. ``test_unknown_raster_uri_raises_typed_error`` — non-gs:// non-file URI raises
   ClipRasterError with error_code="UNKNOWN_RASTER_URI".
6. ``test_gdalwarp_path_when_crs_differs`` — bbox_crs differs from source CRS →
   gdalwarp is used even with target_crs=None.
7. ``test_returns_layer_uri_fields`` — LayerURI fields (layer_type, role) are correct.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.clip_raster_to_bbox import (
    ClipRasterError,
    clip_raster_to_bbox,
)

# ---------------------------------------------------------------------------
# Pinned timestamp for deterministic cache keys
# ---------------------------------------------------------------------------

PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# GDAL binary availability check
# ---------------------------------------------------------------------------

_GDAL_TRANSLATE_AVAILABLE = (
    os.path.isfile(os.path.expanduser("~/miniforge3/envs/grace2/bin/gdal_translate"))
    or bool(__import__("shutil").which("gdal_translate"))
    or (
        bool(os.environ.get("GRACE2_GDAL_TRANSLATE_BIN"))
        and os.path.isfile(os.environ.get("GRACE2_GDAL_TRANSLATE_BIN", ""))
    )
)
_GDALWARP_AVAILABLE = (
    os.path.isfile(os.path.expanduser("~/miniforge3/envs/grace2/bin/gdalwarp"))
    or bool(__import__("shutil").which("gdalwarp"))
    or (
        bool(os.environ.get("GRACE2_GDALWARP_BIN"))
        and os.path.isfile(os.environ.get("GRACE2_GDALWARP_BIN", ""))
    )
)

_SKIP_GDAL_TRANSLATE = pytest.mark.skipif(
    not _GDAL_TRANSLATE_AVAILABLE,
    reason="gdal_translate binary not available in this environment",
)
_SKIP_GDALWARP = pytest.mark.skipif(
    not _GDALWARP_AVAILABLE,
    reason="gdalwarp binary not available in this environment",
)

# ---------------------------------------------------------------------------
# Helpers: synthetic raster creation
# ---------------------------------------------------------------------------


def _write_synthetic_raster(
    path: str,
    width: int = 256,
    height: int = 256,
    crs: str = "EPSG:4326",
    west: float = -82.0,
    south: float = 26.0,
    east: float = -80.0,
    north: float = 28.0,
) -> None:
    """Write a synthetic GeoTIFF of given dimensions, filled with elevation values."""
    transform = from_bounds(west, south, east, north, width, height)
    data = np.arange(width * height, dtype=np.float32).reshape(height, width)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": width,
        "height": height,
        "count": 1,
        "crs": crs,
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def _make_fake_raster_bytes(
    width: int = 4,
    height: int = 4,
    crs: str = "EPSG:4326",
) -> bytes:
    """Return a minimal valid GeoTIFF payload."""
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        path = tmp.name
    try:
        _write_synthetic_raster(path, width=width, height=height, crs=crs)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# FakeBlob / FakeStorageClient for cache shim isolation
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
# Test 1 — registration check
# ---------------------------------------------------------------------------


def test_clip_raster_to_bbox_registered():
    """clip_raster_to_bbox is in TOOL_REGISTRY with expected metadata."""
    assert "clip_raster_to_bbox" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["clip_raster_to_bbox"]
    assert entry.metadata.cacheable is True
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "clip_raster"


# ---------------------------------------------------------------------------
# Test 2 — synthetic 256×256 raster, top-right quadrant bbox → ~128×128 clip
# ---------------------------------------------------------------------------


@_SKIP_GDAL_TRANSLATE
def test_clip_quadrant_produces_smaller_raster():
    """256×256 raster + top-right quadrant bbox → output ~128×128 pixels.

    Source raster: EPSG:4326, extent (-82, 26, -80, 28) → 2°×2° grid.
    Top-right quadrant bbox: west=-81, south=27, east=-80, north=28 (1°×1°).
    Expected output: ~128×128 pixels (within 10% tolerance for GDAL alignment).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "src.tif")
        _write_synthetic_raster(
            src_path,
            width=256,
            height=256,
            crs="EPSG:4326",
            west=-82.0,
            south=26.0,
            east=-80.0,
            north=28.0,
        )

        # Top-right quadrant: x ∈ [-81, -80], y ∈ [27, 28]
        clip_bbox = (-81.0, 27.0, -80.0, 28.0)
        out_path = os.path.join(tmpdir, "clip.tif")

        # Patch GCS download and cache so we use local files directly.
        fake_sc = FakeStorageClient()
        fake_raster_bytes = open(src_path, "rb").read()

        with patch(
            "grace2_agent.tools.clip_raster_to_bbox._download_raster_bytes",
            return_value=fake_raster_bytes,
        ), patch(
            "grace2_agent.tools.clip_raster_to_bbox._get_source_crs",
            return_value=CRS.from_epsg(4326),
        ):
            result = clip_raster_to_bbox(
                raster_uri="gs://bucket/raster/src.tif",
                bbox=clip_bbox,
                bbox_crs="EPSG:4326",
                target_crs=None,
                _bucket="test-bucket",
            )

        # Retrieve the clipped bytes from the fake cache store.
        assert len(fake_sc.store) == 1, "Expected exactly one object written to cache"
        clip_path_key = list(fake_sc.store.keys())[0]
        clip_bytes = fake_sc.store[clip_path_key]

        # Write clip bytes to a temp file so rasterio can inspect them.
        clip_tif = os.path.join(tmpdir, "clipped_output.tif")
        with open(clip_tif, "wb") as f:
            f.write(clip_bytes)

        with rasterio.open(clip_tif) as ds:
            w, h = ds.width, ds.height

        # Expect roughly 128×128 (within 10% = 13 pixels of 128).
        assert abs(w - 128) <= 13, f"Expected width ≈128, got {w}"
        assert abs(h - 128) <= 13, f"Expected height ≈128, got {h}"

        assert result.layer_type == "raster"
        assert result.uri.startswith("s3://")
        assert "clip_raster" in result.uri


# ---------------------------------------------------------------------------
# Test 3 — reproject 4326→3857 (gdalwarp path)
# ---------------------------------------------------------------------------


@_SKIP_GDALWARP
def test_clip_reproject_4326_to_3857():
    """source EPSG:4326 + target_crs='EPSG:3857' → output CRS is EPSG:3857."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "src4326.tif")
        _write_synthetic_raster(
            src_path,
            width=256,
            height=256,
            crs="EPSG:4326",
            west=-82.0,
            south=26.0,
            east=-80.0,
            north=28.0,
        )
        fake_raster_bytes = open(src_path, "rb").read()
        fake_sc = FakeStorageClient()

        with patch(
            "grace2_agent.tools.clip_raster_to_bbox._download_raster_bytes",
            return_value=fake_raster_bytes,
        ), patch(
            "grace2_agent.tools.clip_raster_to_bbox._get_source_crs",
            return_value=CRS.from_epsg(4326),
        ):
            result = clip_raster_to_bbox(
                raster_uri="gs://bucket/raster/src4326.tif",
                bbox=(-81.5, 26.5, -80.5, 27.5),
                bbox_crs="EPSG:4326",
                target_crs="EPSG:3857",
                _bucket="test-bucket",
            )

        assert len(fake_sc.store) == 1, "Expected one cached clip"
        clip_bytes = list(fake_sc.store.values())[0]

        clip_tif = os.path.join(tmpdir, "clipped_3857.tif")
        with open(clip_tif, "wb") as f:
            f.write(clip_bytes)

        with rasterio.open(clip_tif) as ds:
            out_crs = ds.crs

        assert out_crs.to_epsg() == 3857, (
            f"Expected EPSG:3857 output CRS, got {out_crs}"
        )
        # URI should reference the target CRS in layer_id.
        assert "3857" in result.layer_id, f"Expected '3857' in layer_id, got {result.layer_id!r}"


# ---------------------------------------------------------------------------
# Test 4 — cache miss/hit
# ---------------------------------------------------------------------------


def test_cache_miss_writes_and_hit_skips_gdal():
    """First call (cache miss) invokes GDAL; second call (cache hit) does NOT."""
    fake_raster_bytes = _make_fake_raster_bytes()
    fake_clip_bytes = _make_fake_raster_bytes(width=2, height=2)
    fake_sc = FakeStorageClient()

    gdal_call_count: list[int] = [0]

    def _fake_run_gdal_translate(*args, **kwargs):
        gdal_call_count[0] += 1
        # Write fake clipped bytes to the output path argument.
        out_path = args[1]
        with open(out_path, "wb") as f:
            f.write(fake_clip_bytes)

    with patch(
        "grace2_agent.tools.clip_raster_to_bbox._download_raster_bytes",
        return_value=fake_raster_bytes,
    ), patch(
        "grace2_agent.tools.clip_raster_to_bbox._get_source_crs",
        return_value=CRS.from_epsg(4326),
    ), patch(
        "grace2_agent.tools.clip_raster_to_bbox._run_gdal_translate_clip_with_srs",
        side_effect=_fake_run_gdal_translate,
    ):
        # First call — should be a cache miss.
        result1 = clip_raster_to_bbox(
            raster_uri="gs://bucket/raster/abc.tif",
            bbox=(-81.0, 27.0, -80.0, 28.0),
            bbox_crs="EPSG:4326",
            target_crs=None,
            _bucket="test-bucket",
        )
        assert gdal_call_count[0] == 1, "Expected GDAL to be called on cache miss"

        # Second call — same params, should be a cache HIT (GDAL not called again).
        result2 = clip_raster_to_bbox(
            raster_uri="gs://bucket/raster/abc.tif",
            bbox=(-81.0, 27.0, -80.0, 28.0),
            bbox_crs="EPSG:4326",
            target_crs=None,
            _bucket="test-bucket",
        )
        assert gdal_call_count[0] == 1, (
            f"Expected GDAL NOT to be called on cache hit; got {gdal_call_count[0]} calls"
        )

    assert result1.uri == result2.uri, "Both calls should return the same cached URI"


# ---------------------------------------------------------------------------
# Test 5 — unknown raster_uri raises typed ClipRasterError
# ---------------------------------------------------------------------------


def test_unknown_raster_uri_raises_typed_error():
    """Non-gs:// non-file URI raises ClipRasterError(UNKNOWN_RASTER_URI)."""
    from grace2_agent.tools.clip_raster_to_bbox import _get_source_crs

    with pytest.raises(ClipRasterError) as exc_info:
        _get_source_crs("/nonexistent/path/that/does/not/exist.tif")

    assert exc_info.value.error_code == "UNKNOWN_RASTER_URI", (
        f"Expected UNKNOWN_RASTER_URI, got {exc_info.value.error_code!r}"
    )


# ---------------------------------------------------------------------------
# Test 6 — gdalwarp path used when CRS differs (even without target_crs)
# ---------------------------------------------------------------------------


def test_gdalwarp_path_when_crs_differs():
    """When bbox_crs differs from source CRS, gdalwarp is used even without target_crs."""
    fake_raster_bytes = _make_fake_raster_bytes(crs="EPSG:3857")
    fake_clip_bytes = _make_fake_raster_bytes()
    fake_sc = FakeStorageClient()

    gdalwarp_called: list[int] = [0]
    gdal_translate_called: list[int] = [0]

    def _fake_gdalwarp(*args, **kwargs):
        gdalwarp_called[0] += 1
        out_path = args[1]
        with open(out_path, "wb") as f:
            f.write(fake_clip_bytes)

    def _fake_translate(*args, **kwargs):
        gdal_translate_called[0] += 1
        out_path = args[1]
        with open(out_path, "wb") as f:
            f.write(fake_clip_bytes)

    with patch(
        "grace2_agent.tools.clip_raster_to_bbox._download_raster_bytes",
        return_value=fake_raster_bytes,
    ), patch(
        # Source raster is EPSG:3857, bbox_crs will be "EPSG:4326" → CRS mismatch
        "grace2_agent.tools.clip_raster_to_bbox._get_source_crs",
        return_value=CRS.from_epsg(3857),
    ), patch(
        "grace2_agent.tools.clip_raster_to_bbox._run_gdalwarp_clip",
        side_effect=_fake_gdalwarp,
    ), patch(
        "grace2_agent.tools.clip_raster_to_bbox._run_gdal_translate_clip_with_srs",
        side_effect=_fake_translate,
    ):
        clip_raster_to_bbox(
            raster_uri="gs://bucket/raster/3857.tif",
            bbox=(-81.0, 27.0, -80.0, 28.0),
            bbox_crs="EPSG:4326",  # differs from source EPSG:3857
            target_crs=None,
            _bucket="test-bucket",
        )

    assert gdalwarp_called[0] == 1, "Expected gdalwarp to be called for CRS mismatch"
    assert gdal_translate_called[0] == 0, (
        "Expected gdal_translate NOT to be called when CRS differs"
    )


# ---------------------------------------------------------------------------
# Test 7 — LayerURI field correctness
# ---------------------------------------------------------------------------


def test_returns_layer_uri_fields():
    """LayerURI returned by clip_raster_to_bbox has correct field values."""
    fake_raster_bytes = _make_fake_raster_bytes()
    fake_clip_bytes = _make_fake_raster_bytes(width=2, height=2)
    fake_sc = FakeStorageClient()

    def _fake_run_translate(*args, **kwargs):
        out_path = args[1]
        with open(out_path, "wb") as f:
            f.write(fake_clip_bytes)

    with patch(
        "grace2_agent.tools.clip_raster_to_bbox._download_raster_bytes",
        return_value=fake_raster_bytes,
    ), patch(
        "grace2_agent.tools.clip_raster_to_bbox._get_source_crs",
        return_value=CRS.from_epsg(4326),
    ), patch(
        "grace2_agent.tools.clip_raster_to_bbox._run_gdal_translate_clip_with_srs",
        side_effect=_fake_run_translate,
    ):
        result = clip_raster_to_bbox(
            raster_uri="gs://bucket/raster/mydem.tif",
            bbox=(-81.0, 27.0, -80.0, 28.0),
            bbox_crs="EPSG:4326",
            target_crs=None,
            _bucket="test-bucket",
        )

    assert result.layer_type == "raster"
    assert result.role == "context"
    assert result.uri.startswith("s3://")
    assert "clip_raster" in result.uri
    # layer_id should include something from the raster URI
    assert "mydem" in result.layer_id or "clip" in result.layer_id


# ---------------------------------------------------------------------------
# Test — cache keys vary across parameter combinations
# ---------------------------------------------------------------------------


def test_cache_keys_vary_across_params():
    """Cache keys differ for distinct (raster_uri, bbox, bbox_crs, target_crs) combos."""
    from grace2_agent.tools.cache import compute_cache_key

    base_uri = "gs://bucket/raster/somekey.tif"
    combos = [
        {"raster_uri": base_uri, "bbox": [-82.0, 26.0, -80.0, 28.0], "bbox_crs": "EPSG:4326"},
        {"raster_uri": base_uri, "bbox": [-81.0, 26.0, -80.0, 28.0], "bbox_crs": "EPSG:4326"},
        {"raster_uri": base_uri, "bbox": [-82.0, 26.0, -80.0, 28.0], "bbox_crs": "EPSG:3857"},
        {
            "raster_uri": base_uri,
            "bbox": [-82.0, 26.0, -80.0, 28.0],
            "bbox_crs": "EPSG:4326",
            "target_crs": "EPSG:3857",
        },
    ]
    keys = set()
    for params in combos:
        key = compute_cache_key("clip_raster", params, "static-30d", now=PINNED_NOW)
        keys.add(key)

    assert len(keys) == 4, (
        f"Expected 4 distinct cache keys for 4 combos; got {len(keys)}: {keys}"
    )
