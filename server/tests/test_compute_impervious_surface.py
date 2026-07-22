"""Unit tests for ``compute_impervious_surface`` atomic tool (job-0095, FR-CE-8, FR-DC).

Coverage:
1. ``test_compute_impervious_surface_registered`` — tool appears in
   TOOL_REGISTRY with correct metadata (cacheable=True, ttl_class="static-30d",
   source_class="impervious").
2. ``test_compute_impervious_from_landcover_classes_22_23_24`` — synthetic
   landcover raster with developed classes 22, 23, 24 → output pixels equal
   0.3, 0.6, 0.9 respectively.
3. ``test_compute_impervious_from_impervious_product_scale_0_100`` — synthetic
   impervious product (values 0, 30, 60, 90, 100) → output is 0.0, 0.3, 0.6,
   0.9, 1.0 after 1/100 scaling.
4. ``test_compute_impervious_nodata_preserved_as_nan`` — input nodata pixels
   become NaN in the output.
5. ``test_compute_impervious_bbox_window`` — bbox window read returns a smaller
   raster covering only the requested AOI.
6. ``test_compute_impervious_cache_miss_writes`` + ``test_compute_impervious_cache_hit_skips_compute``
   — cache miss/hit behaviour (separate tests).
7. ``test_compute_impervious_returns_layer_uri`` — LayerURI shape correctness.
8. ``test_compute_impervious_raster_download_failure_raises`` — typed error.
9. ``test_compute_impervious_non_developed_classes_map_to_zero`` — water,
   forest, agriculture, wetlands → 0.0 (no spurious impervious values).
10. ``test_compute_impervious_unit_only_developed`` — only the four
    developed classes (21, 22, 23, 24) map to non-trivial fractions; verifies
    the canonical NLCD mapping.

The codified job-0086 lesson is honored:
- The tool propagates the input CRS / transform verbatim (no in-COG mirror).
- The unit tests place known classes at known PIXEL POSITIONS and assert the
  output value at THAT position, not just round-trip.
- The synthetic landcover test uses a non-square grid where each row holds a
  different class, so a Y-axis flip bug would be immediately visible.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.compute_impervious_surface import (
    DEVELOPED_CLASS_TO_IMPERVIOUS,
    ImperviousSurfaceError,
    _compute_impervious_bytes,
    _derive_impervious_from_landcover,
    _scale_impervious_product,
    compute_impervious_surface,
)

# ---------------------------------------------------------------------------
# Pinned timestamp for deterministic cache keys
# ---------------------------------------------------------------------------

PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Synthetic-raster helpers
# ---------------------------------------------------------------------------


def _write_synthetic_landcover(
    path: str,
    array: np.ndarray,
    nodata: int | None = 0,
    crs: str = "EPSG:5070",
) -> None:
    """Write a synthetic landcover GeoTIFF with the given array and nodata."""
    height, width = array.shape
    transform = from_bounds(0.0, 0.0, width * 30.0, height * 30.0, width, height)
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array.astype(np.uint8), 1)


def _write_synthetic_impervious_product(
    path: str,
    array: np.ndarray,
    nodata: int | None = 255,
    crs: str = "EPSG:5070",
) -> None:
    """Write a synthetic impervious-product GeoTIFF (values 0-100)."""
    height, width = array.shape
    transform = from_bounds(0.0, 0.0, width * 30.0, height * 30.0, width, height)
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": crs,
        "transform": transform,
        "nodata": nodata,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array.astype(np.uint8), 1)


def _read_output_array(output_bytes: bytes) -> tuple[np.ndarray, dict]:
    """Read output bytes and return (array, profile)."""
    with MemoryFile(output_bytes) as mf:
        with mf.open() as src:
            arr = src.read(1)
            profile = src.profile
    return arr, profile


# ---------------------------------------------------------------------------
# FakeBlob / FakeStorageClient
# ---------------------------------------------------------------------------


class _S3Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeStorageClient:
    """In-memory S3 double (GCP decommissioned). ``store`` keyed by object KEY.

    GCP is decommissioned: both the SOURCE raster read and the CACHE write now
    flow through boto3 S3 (the cache shim's only object store). ``source_blobs``
    is seeded into the same in-memory ``store`` so the tool's
    ``_download_raster_bytes`` can read an ``s3://`` landcover URI. Returns the
    per-test active instance installed by the autouse
    ``_route_cache_to_inmemory_s3`` fixture so the tool's read-through reads /
    writes the store the test inspects.
    """

    _active: "FakeStorageClient | None" = None

    def __new__(cls, source_blobs: dict[str, bytes] | None = None) -> "FakeStorageClient":
        if cls._active is not None:
            inst = cls._active
            if source_blobs:
                inst.store.update(source_blobs)
                inst.source_blobs.update(source_blobs)
            return inst
        return super().__new__(cls)

    def __init__(self, source_blobs: dict[str, bytes] | None = None) -> None:
        if getattr(self, "_init", False):
            return
        self._init = True
        self.store: dict[str, bytes] = dict(source_blobs or {})
        self.source_blobs: dict[str, bytes] = dict(source_blobs or {})
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

    @property
    def cache_writes(self) -> dict[str, bytes]:
        """Cache-bucket writes only (store minus pre-seeded source blobs)."""
        return {k: v for k, v in self.store.items() if k not in self.source_blobs}


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
# Test 1 — registration
# ---------------------------------------------------------------------------


def test_compute_impervious_surface_registered():
    """compute_impervious_surface is in TOOL_REGISTRY with expected metadata."""
    assert "compute_impervious_surface" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["compute_impervious_surface"]
    assert entry.metadata.cacheable is True
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "impervious"


# ---------------------------------------------------------------------------
# Tests 2 — landcover dev-class derivation
# ---------------------------------------------------------------------------


def test_compute_impervious_from_landcover_classes_22_23_24():
    """Developed classes 22, 23, 24 → output 0.3, 0.6, 0.9 exactly."""
    # 4×3 array: row 0 = class 22, row 1 = class 23, row 2 = class 24, row 3 = water (class 11).
    array = np.array(
        [
            [22, 22, 22],
            [23, 23, 23],
            [24, 24, 24],
            [11, 11, 11],
        ],
        dtype=np.uint8,
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, "landcover.tif")
        _write_synthetic_landcover(in_path, array, nodata=255)
        with open(in_path, "rb") as f:
            landcover_bytes = f.read()

        out_bytes = _compute_impervious_bytes(landcover_bytes, bbox=None)
        out_arr, profile = _read_output_array(out_bytes)

    assert out_arr.shape == array.shape
    assert profile["dtype"] == "float32"
    # Row 0 (class 22) → 0.3
    assert np.allclose(out_arr[0, :], 0.3), f"row 0: {out_arr[0]}"
    # Row 1 (class 23) → 0.6
    assert np.allclose(out_arr[1, :], 0.6), f"row 1: {out_arr[1]}"
    # Row 2 (class 24) → 0.9
    assert np.allclose(out_arr[2, :], 0.9), f"row 2: {out_arr[2]}"
    # Row 3 (water=11) → 0.0
    assert np.allclose(out_arr[3, :], 0.0), f"row 3 (water): {out_arr[3]}"

    # Codified job-0086 lesson: the spatial arrangement is preserved. The
    # developed-class GRADIENT runs north (top) to south (bottom). If a Y-axis
    # flip were silently introduced, row 0 would be 0.0 (water) not 0.3, and
    # row 3 would be 0.3 (class 22) not 0.0. The assertions above catch it.


# ---------------------------------------------------------------------------
# Test 3 — impervious-product scaling
# ---------------------------------------------------------------------------


def test_compute_impervious_from_impervious_product_scale_0_100():
    """Impervious-product values 0,30,60,90,100 → output 0.0,0.3,0.6,0.9,1.0."""
    array = np.array(
        [
            [0, 30, 60, 90, 100],
            [0, 30, 60, 90, 100],
        ],
        dtype=np.uint8,
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, "NLCD_2021_Impervious_L48.tif")
        _write_synthetic_impervious_product(in_path, array, nodata=255)
        with open(in_path, "rb") as f:
            input_bytes = f.read()

        # The filename does NOT carry through to _compute_impervious_bytes (it
        # only sees the bytes), so we pass force_impervious_product=True to
        # exercise the impervious-scaling path on the synthetic raster.
        out_bytes = _compute_impervious_bytes(
            input_bytes, bbox=None, force_impervious_product=True
        )
        out_arr, profile = _read_output_array(out_bytes)

    assert profile["dtype"] == "float32"
    expected = np.array(
        [
            [0.0, 0.3, 0.6, 0.9, 1.0],
            [0.0, 0.3, 0.6, 0.9, 1.0],
        ],
        dtype=np.float32,
    )
    assert np.allclose(out_arr, expected, atol=1e-5), (
        f"impervious scaling mismatch: {out_arr}"
    )


# ---------------------------------------------------------------------------
# Test 4 — nodata preservation
# ---------------------------------------------------------------------------


def test_compute_impervious_nodata_preserved_as_nan():
    """Input nodata pixels → NaN in the output."""
    array = np.array(
        [
            [22, 23, 0],  # 0 is nodata
            [24, 0, 11],
        ],
        dtype=np.uint8,
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, "landcover.tif")
        _write_synthetic_landcover(in_path, array, nodata=0)
        with open(in_path, "rb") as f:
            input_bytes = f.read()

        out_bytes = _compute_impervious_bytes(input_bytes, bbox=None)
        out_arr, profile = _read_output_array(out_bytes)

    # Output nodata convention is NaN.
    assert profile["nodata"] is not None
    assert np.isnan(profile["nodata"])

    # Class 22 → 0.3, class 23 → 0.6, class 24 → 0.9, water=11 → 0.0
    assert np.isclose(out_arr[0, 0], 0.3)
    assert np.isclose(out_arr[0, 1], 0.6)
    assert np.isnan(out_arr[0, 2])  # nodata
    assert np.isclose(out_arr[1, 0], 0.9)
    assert np.isnan(out_arr[1, 1])  # nodata
    assert np.isclose(out_arr[1, 2], 0.0)  # water


# ---------------------------------------------------------------------------
# Test 5 — bbox window
# ---------------------------------------------------------------------------


def test_compute_impervious_bbox_window_geographic_correctness():
    """bbox window reads a sub-extent; checks GEOGRAPHIC correctness (job-0086)."""
    # Build a 4×4 landcover raster in EPSG:4326 so we can pass a 4326 bbox.
    # Row 0 (north) = class 24 (high developed),
    # Row 1         = class 23,
    # Row 2         = class 22,
    # Row 3 (south) = class 11 (water).
    # Each pixel = 0.01° at EPSG:4326 around (lon=-82.0, lat=26.0).
    array = np.array(
        [
            [24, 24, 24, 24],
            [23, 23, 23, 23],
            [22, 22, 22, 22],
            [11, 11, 11, 11],
        ],
        dtype=np.uint8,
    )
    height, width = array.shape
    # Bounds: lon [-82.04, -82.00], lat [26.00, 26.04]. North-up orientation.
    transform = from_bounds(-82.04, 26.00, -82.00, 26.04, width, height)
    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, "landcover_4326.tif")
        profile = {
            "driver": "GTiff",
            "dtype": "uint8",
            "width": width,
            "height": height,
            "count": 1,
            "crs": "EPSG:4326",
            "transform": transform,
            "nodata": 255,
        }
        with rasterio.open(in_path, "w", **profile) as dst:
            dst.write(array, 1)
        with open(in_path, "rb") as f:
            input_bytes = f.read()

        # Bbox: just the top 2 rows (north half). lat range [26.02, 26.04].
        bbox = (-82.04, 26.02, -82.00, 26.04)
        out_bytes = _compute_impervious_bytes(input_bytes, bbox=bbox)
        out_arr, _ = _read_output_array(out_bytes)

    # Output should be 2 rows × 4 cols (the north half).
    assert out_arr.shape == (2, 4), f"expected (2, 4), got {out_arr.shape}"

    # Geographic correctness: the NORTH half contains the HIGH-developed
    # classes (24 → 0.9 and 23 → 0.6). If a Y-axis flip bug were present, the
    # output would contain rows from the SOUTH half (water + class 22) and
    # the mean would be near 0.15 instead of 0.75.
    assert np.allclose(out_arr[0, :], 0.9), f"north row: {out_arr[0]}"
    assert np.allclose(out_arr[1, :], 0.6), f"south-of-window row: {out_arr[1]}"
    # Mean of the window: (0.9 + 0.6) / 2 = 0.75 — distinctly high-developed,
    # not the (0.3 + 0.0) / 2 = 0.15 a flipped read would yield.
    assert abs(out_arr.mean() - 0.75) < 0.01


# ---------------------------------------------------------------------------
# Test 6a — cache miss writes
# ---------------------------------------------------------------------------


def test_compute_impervious_cache_miss_writes():
    """First call: bytes are computed and written to the cache bucket."""
    # 2×2 landcover: all class 22 → impervious 0.3.
    array = np.full((2, 2), 22, dtype=np.uint8)
    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, "lc.tif")
        _write_synthetic_landcover(in_path, array, nodata=255)
        with open(in_path, "rb") as f:
            input_bytes = f.read()

    storage = FakeStorageClient(
        source_blobs={"cache/static-30d/landcover/xyz.tif": input_bytes}
    )
    landcover_uri = "s3://test-source-bucket/cache/static-30d/landcover/xyz.tif"

    result = compute_impervious_surface(
        landcover_uri=landcover_uri,
        bbox=None,
        _bucket="test-bucket",
    )

    # Result should reference the cache bucket and the cache key path.
    assert result.uri.startswith("s3://test-bucket/")
    assert "/impervious/" in result.uri
    assert result.layer_type == "raster"
    assert result.role == "context"
    assert result.units is None
    # Cache bucket should now have exactly one fresh write (source excluded).
    assert len(storage.cache_writes) == 1
    # The cached bytes should be a valid GeoTIFF; verify it parses.
    cached_bytes = next(iter(storage.cache_writes.values()))
    out_arr, profile = _read_output_array(cached_bytes)
    assert np.allclose(out_arr, 0.3)


# ---------------------------------------------------------------------------
# Test 6b — cache hit
# ---------------------------------------------------------------------------


def test_compute_impervious_cache_hit_skips_compute():
    """Second call hits the cache; source download is NOT invoked."""
    from trid3nt_server.tools.cache import cache_path as make_cache_path
    from trid3nt_server.tools.cache import compute_cache_key

    # Pre-seed cache with a known impervious bytes.
    array = np.full((2, 2), 22, dtype=np.uint8)
    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, "lc.tif")
        _write_synthetic_landcover(in_path, array, nodata=255)
        with open(in_path, "rb") as f:
            landcover_bytes = f.read()
        cached_bytes = _compute_impervious_bytes(landcover_bytes, bbox=None)

    landcover_uri = "s3://test-source-bucket/cache/static-30d/landcover/xyz.tif"

    # Compute the expected cache path the way the tool does. The cache key
    # uses TTL-bucket vintage — for "static-30d" that's the current year-month.
    params = {"landcover_uri": landcover_uri}
    key = compute_cache_key("impervious", params, "static-30d")
    path = make_cache_path("impervious", "static-30d", key, "tif")

    storage = FakeStorageClient()
    storage.store[path] = cached_bytes
    # Note: NO source_blobs — confirms cache hit doesn't hit source.

    result = compute_impervious_surface(
        landcover_uri=landcover_uri,
        bbox=None,
        _bucket="test-bucket",
    )

    assert result.uri.endswith(f"{key}.tif"), result.uri
    # Cache store should be unchanged (only the pre-seeded entry).
    assert list(storage.store.keys()) == [path]


# ---------------------------------------------------------------------------
# Test 7 — LayerURI fields
# ---------------------------------------------------------------------------


def test_compute_impervious_returns_layer_uri_fields():
    """LayerURI returned by compute_impervious_surface has expected field values."""
    array = np.full((2, 2), 22, dtype=np.uint8)
    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, "lc.tif")
        _write_synthetic_landcover(in_path, array, nodata=255)
        with open(in_path, "rb") as f:
            input_bytes = f.read()

    storage = FakeStorageClient(
        source_blobs={"cache/static-30d/landcover/abc123.tif": input_bytes}
    )
    landcover_uri = "s3://test-source-bucket/cache/static-30d/landcover/abc123.tif"

    result = compute_impervious_surface(
        landcover_uri=landcover_uri,
        _bucket="test-bucket",
    )

    assert result.layer_type == "raster"
    assert result.role == "context"
    assert result.units is None
    assert "impervious-abc123" in result.layer_id


# ---------------------------------------------------------------------------
# Test 8 — download-failure typed-error path
# ---------------------------------------------------------------------------


def test_compute_impervious_raster_download_failure_raises():
    """S3 download failure raises ImperviousSurfaceError(error_code='RASTER_DOWNLOAD_FAILED')."""
    FakeStorageClient(source_blobs={})  # no source bytes seeded
    landcover_uri = "s3://test-source-bucket/missing.tif"

    with pytest.raises(ImperviousSurfaceError) as exc_info:
        compute_impervious_surface(
            landcover_uri=landcover_uri,
            _bucket="test-bucket",
        )
    assert exc_info.value.error_code == "RASTER_DOWNLOAD_FAILED"


# ---------------------------------------------------------------------------
# Test 9 — non-developed classes all map to zero
# ---------------------------------------------------------------------------


def test_compute_impervious_non_developed_classes_map_to_zero():
    """All non-developed NLCD classes (water/forest/ag/wetlands) → 0.0."""
    # Sample of canonical NLCD classes: 11 water, 41 deciduous forest, 81 hay,
    # 90 woody wetlands, 95 emergent wetlands. None are developed → all 0.0.
    array = np.array(
        [
            [11, 41, 81, 90, 95],
            [11, 41, 81, 90, 95],
        ],
        dtype=np.uint8,
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        in_path = os.path.join(tmpdir, "lc.tif")
        _write_synthetic_landcover(in_path, array, nodata=255)
        with open(in_path, "rb") as f:
            input_bytes = f.read()

        out_bytes = _compute_impervious_bytes(input_bytes, bbox=None)
        out_arr, _ = _read_output_array(out_bytes)

    assert np.allclose(out_arr, 0.0)


# ---------------------------------------------------------------------------
# Test 10 — Bbox shape validation
# ---------------------------------------------------------------------------


def test_compute_impervious_degenerate_bbox_raises():
    """Degenerate bbox (min >= max) raises ImperviousSurfaceError early."""
    FakeStorageClient(source_blobs={"x.tif": b""})
    with pytest.raises(ImperviousSurfaceError) as exc_info:
        compute_impervious_surface(
            landcover_uri="s3://test-source-bucket/x.tif",
            bbox=(-82.0, 26.0, -82.0, 26.0),  # degenerate
            _bucket="test-bucket",
        )
    assert exc_info.value.error_code == "BBOX_OUTSIDE_RASTER"


# ---------------------------------------------------------------------------
# Direct-helper unit tests (lowest-level)
# ---------------------------------------------------------------------------


def test_derive_helper_developed_class_lookup():
    """Direct test of the dev-class lookup helper."""
    arr = np.array(
        [
            [21, 22, 23, 24],
            [11, 41, 81, 95],
        ],
        dtype=np.uint8,
    )
    out = _derive_impervious_from_landcover(arr, nodata=255)
    assert out.dtype == np.float32
    assert np.isclose(out[0, 0], 0.0)  # 21 — open space, 0% impervious
    assert np.isclose(out[0, 1], 0.3)
    assert np.isclose(out[0, 2], 0.6)
    assert np.isclose(out[0, 3], 0.9)
    assert np.allclose(out[1, :], 0.0)  # non-developed
    # Mapping matches the constant.
    assert DEVELOPED_CLASS_TO_IMPERVIOUS == {21: 0.0, 22: 0.3, 23: 0.6, 24: 0.9}


def test_scale_helper_impervious_product_clipping():
    """Direct test of the impervious-product scaling helper, with clipping."""
    arr = np.array(
        [
            [0, 50, 100],
            [200, 255, 75],  # 200 is out of range — clipped to 1.0
        ],
        dtype=np.uint8,
    )
    out = _scale_impervious_product(arr, nodata=255)
    assert out.dtype == np.float32
    assert np.isclose(out[0, 0], 0.0)
    assert np.isclose(out[0, 1], 0.5)
    assert np.isclose(out[0, 2], 1.0)
    assert np.isclose(out[1, 0], 1.0)  # clipped from 2.0
    assert np.isnan(out[1, 1])  # nodata
    assert np.isclose(out[1, 2], 0.75)


# ---------------------------------------------------------------------------
# Live verification — env-guarded
# ---------------------------------------------------------------------------

# The kickoff requires ≥1 live test. The "live" path is: download a real NLCD
# landcover GeoTIFF and run the developed-class derivation against it.
# Guarded by TRID3NT_RUN_LIVE_NLCD=1 so CI without GCS / network skips it.


@pytest.mark.skipif(
    os.environ.get("TRID3NT_RUN_LIVE_NLCD") != "1",
    reason="live NLCD impervious-surface test requires TRID3NT_RUN_LIVE_NLCD=1 + GCP ADC",
)
def test_live_compute_impervious_against_fort_myers_landcover():
    """Live: derive impervious from a real NLCD landcover layer (Fort Myers AOI).

    Uses the existing job-0042 / job-0044 NLCD cache (Annual_NLCD_LndCov_2021).
    Asserts:
      - output has values in [0.0, 1.0] (excluding NaN);
      - some non-zero impervious fraction is present (urban Fort Myers has
        developed classes);
      - mean impervious fraction is < 1.0 (sanity: not all-developed).
    """
    from trid3nt_server.tools.data_fetch import fetch_landcover

    # Small Fort Myers AOI (~few hundred km²).
    bbox = (-82.10, 26.55, -81.80, 26.80)
    lc_result = fetch_landcover(bbox=bbox, dataset="nlcd_2021")
    landcover_layer = lc_result["layer"]
    assert landcover_layer.uri.startswith("gs://")

    # Run the impervious tool against the cached landcover URI.
    imp = compute_impervious_surface(landcover_uri=landcover_layer.uri)
    assert imp.uri.startswith("gs://")
    assert "/impervious/" in imp.uri
    assert imp.layer_type == "raster"

    # Download and inspect the output.
    from google.cloud import storage as gcs

    client = gcs.Client()
    rest = imp.uri[len("gs://"):]
    bucket_name, _, blob_name = rest.partition("/")
    out_bytes = client.bucket(bucket_name).blob(blob_name).download_as_bytes()
    out_arr, profile = _read_output_array(out_bytes)
    valid = out_arr[~np.isnan(out_arr)]
    assert valid.size > 0
    assert valid.min() >= 0.0
    assert valid.max() <= 1.0
    assert valid.max() > 0.0, "Fort Myers AOI should have some developed pixels"
    assert valid.mean() < 1.0
    print(
        f"live test: shape={out_arr.shape} min={valid.min():.3f} "
        f"max={valid.max():.3f} mean={valid.mean():.3f}"
    )
