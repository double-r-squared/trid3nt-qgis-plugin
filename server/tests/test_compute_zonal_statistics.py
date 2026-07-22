"""Unit tests for ``compute_zonal_statistics`` atomic tool (job-0083, FR-TA-2, FR-CE-8, FR-DC).

Coverage:
1.  ``test_compute_zonal_statistics_registered`` — tool in TOOL_REGISTRY with
    correct metadata (cacheable=True, ttl_class="dynamic-1h", source_class="zonal_statistics").
2.  ``test_raster_zone_ramp_quadrant`` — value raster = 1-100 ramp, zone = top-right
    quadrant mask → mean ≈ 75, sum = expected total.
3.  ``test_raster_zone_threshold`` — flood depth raster with threshold 0.5m → only
    pixels >= 0.5 are counted (simulates population-in-flood-zone query).
4.  ``test_vector_zone_single_polygon`` — value raster + single GeoJSON rectangle →
    per-polygon aggregate stats plus top-level aggregate.
5.  ``test_cache_hit_on_repeat_call`` — second call with identical args returns cached
    bytes (fetch_fn not invoked).
6.  ``test_all_statistics_computed`` — all 10 supported stats round-trip for raster zone.
7.  ``test_nodata_pixels_excluded`` — nodata pixels in value raster are excluded.
8.  ``test_empty_zone_returns_none_stats`` — zone that contains no value pixels returns
    None for all stats rather than raising.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.compute_zonal_statistics import (
    ZonalStatisticsError,
    _compute_stats,
    _detect_zone_type,
    compute_zonal_statistics,
)

# ---------------------------------------------------------------------------
# Pinned timestamp for deterministic cache keys
# ---------------------------------------------------------------------------

PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Synthetic raster helpers
# ---------------------------------------------------------------------------


def _write_ramp_raster(path: str, width: int = 10, height: int = 10) -> None:
    """Write a width×height GeoTIFF where pixel value = row * width + col + 1.

    With a 10×10 grid this creates values 1..100.
    Values are float32. CRS: EPSG:4326 (geographic; good enough for unit tests).
    """
    data = np.arange(1, width * height + 1, dtype=np.float32).reshape(height, width)
    transform = from_bounds(0.0, 0.0, 1.0, 1.0, width, height)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:4326",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def _write_zone_quadrant_mask(
    path: str,
    width: int = 10,
    height: int = 10,
    quadrant: str = "top_right",
) -> None:
    """Write a binary mask raster (0 / 1) covering the named quadrant.

    top_right  = upper half, right half  (rows 0..4, cols 5..9)
    bottom_left = lower half, left half  (rows 5..9, cols 0..4)
    """
    mask = np.zeros((height, width), dtype=np.uint8)
    half_h = height // 2
    half_w = width // 2
    if quadrant == "top_right":
        mask[:half_h, half_w:] = 1
    elif quadrant == "bottom_left":
        mask[half_h:, :half_w] = 1
    elif quadrant == "full":
        mask[:] = 1

    transform = from_bounds(0.0, 0.0, 1.0, 1.0, width, height)
    profile = {
        "driver": "GTiff",
        "dtype": "uint8",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:4326",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(mask, 1)


def _write_flood_depth_raster(path: str, width: int = 10, height: int = 10) -> None:
    """Write a flood depth raster: values range 0..0.9 in 0.1 increments (row-wise).

    Row 0: all 0.0; Row 1: all 0.1; ...; Row 9: all 0.9.
    So pixels with depth >= 0.5m are in rows 5..9 → 50 pixels total.
    """
    data = np.zeros((height, width), dtype=np.float32)
    for row in range(height):
        data[row, :] = row * 0.1
    transform = from_bounds(0.0, 0.0, 1.0, 1.0, width, height)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": width,
        "height": height,
        "count": 1,
        "crs": "EPSG:4326",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def _write_geojson_rect(path: str, minx: float, miny: float, maxx: float, maxy: float, zone_id: str = "poly0") -> None:
    """Write a single-feature GeoJSON file with a rectangular polygon."""
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [minx, miny],
                        [maxx, miny],
                        [maxx, maxy],
                        [minx, maxy],
                        [minx, miny],
                    ]],
                },
                "properties": {"id": zone_id},
            }
        ],
    }
    with open(path, "w") as f:
        json.dump(fc, f)


# ---------------------------------------------------------------------------
# FakeBlob / FakeBucket / FakeStorageClient
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
# Test 1 — registration
# ---------------------------------------------------------------------------


def test_compute_zonal_statistics_registered():
    """compute_zonal_statistics is in TOOL_REGISTRY with the expected metadata."""
    assert "compute_zonal_statistics" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["compute_zonal_statistics"]
    assert entry.metadata.cacheable is True
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "zonal_statistics"


# ---------------------------------------------------------------------------
# Test 2 — ramp raster + top-right quadrant mask
# ---------------------------------------------------------------------------


def test_raster_zone_ramp_quadrant():
    """Value raster 1-100, zone = top-right 5×5 quadrant → mean ≈ 75, sum correct.

    10×10 grid; values = row*10 + col + 1 (1..100).
    Top-right quadrant = rows 0..4, cols 5..9 (25 pixels).

    Expected pixels (row, col) → value:
        (0,5)=6  (0,6)=7  (0,7)=8  (0,8)=9  (0,9)=10
        (1,5)=16 (1,6)=17 (1,7)=18 (1,8)=19 (1,9)=20
        ...
        (4,5)=46 (4,6)=47 (4,7)=48 (4,8)=49 (4,9)=50

    Let's compute expected sum:
        Row r (0-indexed), cols 5..9: values = r*10 + 6, 7, 8, 9, 10
        Row sums = [6+7+8+9+10, 16+17+18+19+20, 26+27+28+29+30, 36+37+38+39+40, 46+47+48+49+50]
                 = [40, 90, 140, 190, 240]
        Total sum = 700
        Mean = 700 / 25 = 28.0

    Note: the ramp is row-major so the top-right quadrant (small row indices, large col indices)
    has LOWER values than the bottom-right quadrant. Mean should be 28.0, not 75.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        val_path = os.path.join(tmpdir, "value.tif")
        zone_path = os.path.join(tmpdir, "zone.tif")
        _write_ramp_raster(val_path, width=10, height=10)
        _write_zone_quadrant_mask(zone_path, width=10, height=10, quadrant="top_right")

        fake_sc = FakeStorageClient()
        result = compute_zonal_statistics(
            value_raster_uri=val_path,
            zone_input_uri=zone_path,
            statistics=["count", "sum", "mean", "max"],
            _bucket="test-bucket",
        )

    agg = result["aggregate"]
    assert agg["count"] == 25, f"Expected 25 pixels, got {agg['count']}"
    assert abs(agg["sum"] - 700.0) < 1.0, f"Expected sum≈700, got {agg['sum']}"
    assert abs(agg["mean"] - 28.0) < 0.1, f"Expected mean≈28.0, got {agg['mean']}"
    assert agg["max"] == 50.0, f"Expected max=50, got {agg['max']}"
    assert result["value_raster"] == val_path
    assert result["zone_input"] == zone_path
    assert "computed_at" in result


# ---------------------------------------------------------------------------
# Test 3 — flood depth threshold (raster zone with zone_threshold)
# ---------------------------------------------------------------------------


def test_raster_zone_threshold():
    """Flood depth raster (0..0.9m) + same as zone with threshold=0.5 → 50 pixels.

    Value and zone are the same flood_depth raster. With threshold=0.5, only
    rows 5..9 qualify (depth >= 0.5). That's 5 rows × 10 cols = 50 pixels.
    Values in those rows: row 5 = 0.5, row 6 = 0.6, ..., row 9 = 0.9.
    Expected mean = (0.5*10 + 0.6*10 + 0.7*10 + 0.8*10 + 0.9*10) / 50
                  = (5 + 6 + 7 + 8 + 9) / 50 = 35 / 50 = 0.7
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        flood_path = os.path.join(tmpdir, "flood.tif")
        _write_flood_depth_raster(flood_path, width=10, height=10)

        fake_sc = FakeStorageClient()
        result = compute_zonal_statistics(
            value_raster_uri=flood_path,
            zone_input_uri=flood_path,
            statistics=["count", "mean", "min", "max"],
            zone_threshold=0.5,
            _bucket="test-bucket",
        )

    agg = result["aggregate"]
    assert agg["count"] == 50, f"Expected 50 pixels, got {agg['count']}"
    assert abs(agg["mean"] - 0.7) < 0.01, f"Expected mean≈0.7, got {agg['mean']}"
    assert abs(agg["min"] - 0.5) < 0.01, f"Expected min≈0.5, got {agg['min']}"
    assert abs(agg["max"] - 0.9) < 0.01, f"Expected max≈0.9, got {agg['max']}"


# ---------------------------------------------------------------------------
# Test 4 — vector zone input (single GeoJSON polygon)
# ---------------------------------------------------------------------------


def test_vector_zone_single_polygon():
    """Value raster + single GeoJSON rectangle → by_zone populated + aggregate.

    The ramp raster has values 1..100 across a 1°×1° extent (0,0)→(1,1).
    The polygon covers the bottom half (y=0..0.5) → rows 5..9.
    Row 5 values: 51,52,53,54,55,56,57,58,59,60 (cols 0..9).
    Row 6: 61..70; Row 7: 71..80; Row 8: 81..90; Row 9: 91..100.
    Total 50 pixels, sum = (51+52+...+100) = sum(51..100) = 50*(51+100)/2 = 50*151/2 = 3775.
    Mean = 3775 / 50 = 75.5.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        val_path = os.path.join(tmpdir, "value.tif")
        zone_path = os.path.join(tmpdir, "zone.geojson")
        _write_ramp_raster(val_path, width=10, height=10)
        # Bottom half of the raster extent (y=0..0.5, x=0..1).
        # rasterio uses the convention (west, south, east, north) for transform,
        # so row 0 in the raster corresponds to the northernmost pixels (y close to 1.0).
        # The ramp data row 0 = values 1..10 (northernmost).
        # Bottom half = y ∈ [0, 0.5] = rows 5..9 (southernmost in rasterio convention).
        _write_geojson_rect(zone_path, 0.0, 0.0, 1.0, 0.5, zone_id="bottom_half")

        fake_sc = FakeStorageClient()
        result = compute_zonal_statistics(
            value_raster_uri=val_path,
            zone_input_uri=zone_path,
            statistics=["count", "sum", "mean"],
            _bucket="test-bucket",
        )

    # by_zone should have one entry keyed by the feature's "id" property.
    assert "bottom_half" in result["by_zone"], (
        f"by_zone keys: {list(result['by_zone'].keys())}"
    )
    zone_stats = result["by_zone"]["bottom_half"]
    assert zone_stats["count"] == 50, f"Expected 50 pixels, got {zone_stats['count']}"
    assert abs(zone_stats["sum"] - 3775.0) < 1.0, (
        f"Expected sum≈3775, got {zone_stats['sum']}"
    )
    assert abs(zone_stats["mean"] - 75.5) < 0.1, (
        f"Expected mean≈75.5, got {zone_stats['mean']}"
    )

    # Aggregate should match the single zone (only one zone).
    agg = result["aggregate"]
    assert agg["count"] == 50, f"Expected aggregate count=50, got {agg['count']}"


# ---------------------------------------------------------------------------
# Test 5 — cache hit on repeat call
# ---------------------------------------------------------------------------


def test_cache_hit_on_repeat_call():
    """Second call with identical args returns cached result; fetch_fn NOT called twice.

    We use the real read_through shim with a FakeStorageClient. The first call
    misses (store is empty) and writes to the fake store. The second call finds
    the key in the fake store and returns the cached result without invoking
    the compute fetch_fn again.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        val_path = os.path.join(tmpdir, "value.tif")
        zone_path = os.path.join(tmpdir, "zone.tif")
        _write_ramp_raster(val_path)
        _write_zone_quadrant_mask(zone_path, quadrant="full")

        fake_sc = FakeStorageClient()

        # Track how many times the _zonal_stats_raster_zone (the actual compute)
        # is invoked. A cache hit should skip it on the second call.
        compute_call_count = []
        from grace2_agent.tools.compute_zonal_statistics import (
            _zonal_stats_raster_zone as _orig_rz,
        )

        def counting_rz(*args, **kwargs):
            compute_call_count.append(1)
            return _orig_rz(*args, **kwargs)

        with patch(
            "grace2_agent.tools.compute_zonal_statistics._zonal_stats_raster_zone",
            side_effect=counting_rz,
        ):
            result1 = compute_zonal_statistics(
                value_raster_uri=val_path,
                zone_input_uri=zone_path,
                statistics=["count", "mean"],
                _bucket="test-bucket",
            )
            result2 = compute_zonal_statistics(
                value_raster_uri=val_path,
                zone_input_uri=zone_path,
                statistics=["count", "mean"],
                _bucket="test-bucket",
            )

    # compute should have been called only once (first call = miss; second = hit from store).
    assert len(compute_call_count) == 1, (
        f"Expected compute called once (cache hit on 2nd call), got {len(compute_call_count)}"
    )
    # Both results should have count = 100 (full mask).
    assert result1["aggregate"]["count"] == 100
    assert result2["aggregate"]["count"] == 100


# ---------------------------------------------------------------------------
# Test 6 — all 10 supported statistics compute correctly
# ---------------------------------------------------------------------------


def test_all_statistics_computed():
    """All 10 supported statistics round-trip for a known ramp + full-zone mask."""
    with tempfile.TemporaryDirectory() as tmpdir:
        val_path = os.path.join(tmpdir, "value.tif")
        zone_path = os.path.join(tmpdir, "zone.tif")
        _write_ramp_raster(val_path, width=10, height=10)
        _write_zone_quadrant_mask(zone_path, width=10, height=10, quadrant="full")

        fake_sc = FakeStorageClient()
        result = compute_zonal_statistics(
            value_raster_uri=val_path,
            zone_input_uri=zone_path,
            statistics=[
                "count", "sum", "mean", "min", "max", "std",
                "median", "percentile_25", "percentile_75", "percentile_95",
            ],
            _bucket="test-bucket",
        )

    agg = result["aggregate"]
    # All 10 stats should be present and non-None.
    for stat in ["count", "sum", "mean", "min", "max", "std",
                 "median", "percentile_25", "percentile_75", "percentile_95"]:
        assert stat in agg, f"Stat {stat!r} missing from aggregate"
        assert agg[stat] is not None, f"Stat {stat!r} is None (should have value)"

    # Spot-check known values for 1..100 ramp.
    assert agg["count"] == 100
    assert abs(agg["sum"] - 5050.0) < 1.0,  f"sum: {agg['sum']}"
    assert abs(agg["mean"] - 50.5) < 0.1,   f"mean: {agg['mean']}"
    assert agg["min"] == 1.0,               f"min: {agg['min']}"
    assert agg["max"] == 100.0,             f"max: {agg['max']}"
    assert abs(agg["median"] - 50.5) < 0.5, f"median: {agg['median']}"


# ---------------------------------------------------------------------------
# Test 7 — nodata pixels excluded
# ---------------------------------------------------------------------------


def test_nodata_pixels_excluded():
    """Nodata pixels in the value raster are excluded from statistics."""
    with tempfile.TemporaryDirectory() as tmpdir:
        val_path = os.path.join(tmpdir, "value_nodata.tif")
        zone_path = os.path.join(tmpdir, "zone_full.tif")

        # Write ramp raster with nodata = -9999 and replace first row with nodata.
        data = np.arange(1, 101, dtype=np.float32).reshape(10, 10)
        data[0, :] = -9999.0  # first row is nodata
        transform = from_bounds(0.0, 0.0, 1.0, 1.0, 10, 10)
        profile = {
            "driver": "GTiff",
            "dtype": "float32",
            "width": 10,
            "height": 10,
            "count": 1,
            "crs": "EPSG:4326",
            "transform": transform,
            "nodata": -9999.0,
        }
        with rasterio.open(val_path, "w", **profile) as dst:
            dst.write(data, 1)

        _write_zone_quadrant_mask(zone_path, quadrant="full")

        fake_sc = FakeStorageClient()
        result = compute_zonal_statistics(
            value_raster_uri=val_path,
            zone_input_uri=zone_path,
            statistics=["count", "min"],
            _bucket="test-bucket",
        )

    agg = result["aggregate"]
    # First row (10 pixels) should be excluded → count = 90.
    assert agg["count"] == 90, f"Expected 90 valid pixels, got {agg['count']}"
    # Min should be 11 (first valid row starts at row 1, values 11..20).
    assert agg["min"] == 11.0, f"Expected min=11.0, got {agg['min']}"


# ---------------------------------------------------------------------------
# Test 8 — empty zone returns None stats
# ---------------------------------------------------------------------------


def test_empty_zone_returns_none_stats():
    """Zone that contains no value pixels returns None for all stats (no raise)."""
    # Build a ramp raster but a zone that covers NO pixels (zero mask).
    with tempfile.TemporaryDirectory() as tmpdir:
        val_path = os.path.join(tmpdir, "value.tif")
        zone_path = os.path.join(tmpdir, "zone_empty.tif")
        _write_ramp_raster(val_path)

        # Write all-zeros mask (no pixels in zone).
        data = np.zeros((10, 10), dtype=np.uint8)
        transform = from_bounds(0.0, 0.0, 1.0, 1.0, 10, 10)
        profile = {
            "driver": "GTiff",
            "dtype": "uint8",
            "width": 10,
            "height": 10,
            "count": 1,
            "crs": "EPSG:4326",
            "transform": transform,
        }
        with rasterio.open(zone_path, "w", **profile) as dst:
            dst.write(data, 1)

        fake_sc = FakeStorageClient()
        result = compute_zonal_statistics(
            value_raster_uri=val_path,
            zone_input_uri=zone_path,
            statistics=["count", "mean", "max"],
            _bucket="test-bucket",
        )

    agg = result["aggregate"]
    # All stats should be None (no valid pixels in zone).
    for stat in ["count", "mean", "max"]:
        assert agg[stat] is None, f"Expected None for {stat} on empty zone, got {agg[stat]}"


# ---------------------------------------------------------------------------
# Unit test for _compute_stats helper
# ---------------------------------------------------------------------------


def test_compute_stats_helper_known_values():
    """_compute_stats returns correct values for a known array."""
    vals = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    stats = _compute_stats(
        vals,
        ["count", "sum", "mean", "min", "max", "std", "median",
         "percentile_25", "percentile_75", "percentile_95"],
    )
    assert stats["count"] == 5
    assert abs(stats["sum"] - 15.0) < 1e-6
    assert abs(stats["mean"] - 3.0) < 1e-6
    assert stats["min"] == 1.0
    assert stats["max"] == 5.0
    assert abs(stats["median"] - 3.0) < 1e-6


def test_compute_stats_empty_array():
    """_compute_stats on empty array returns all None."""
    stats = _compute_stats(np.array([]), ["count", "mean", "sum"])
    assert all(v is None for v in stats.values())


# ---------------------------------------------------------------------------
# Unit test for zone-type detection
# ---------------------------------------------------------------------------


def test_detect_zone_type_by_extension():
    assert _detect_zone_type("path/to/file.tif") == "raster"
    assert _detect_zone_type("path/to/file.tiff") == "raster"
    assert _detect_zone_type("path/to/file.fgb") == "vector"
    assert _detect_zone_type("path/to/file.geojson") == "vector"
    assert _detect_zone_type("path/to/file.gpkg") == "vector"
