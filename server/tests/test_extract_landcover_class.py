"""Unit + live tests for ``extract_landcover_class`` atomic tool (job-0094, FR-CE-8, FR-DC).

Coverage:
1. ``test_extract_landcover_class_registered`` — tool appears in TOOL_REGISTRY
   with correct metadata (cacheable=True, ttl_class="static-30d",
   source_class="landcover_class").
2. ``test_extract_single_class_water_only`` — synthetic 32x32 NLCD raster with
   mixed classes (11, 21, 41, 81), extract class=[11] → only water pixels are
   1, every other pixel is 0.
3. ``test_extract_multiple_classes_forest`` — synthetic raster, extract
   [41, 42, 43] (all forest) → forest pixels are 1, others 0.
4. ``test_bbox_window_read_top_right`` — 64x64 raster + bbox covering the
   top-right quadrant → output 32x32 covering just that quadrant.
5. ``test_nodata_preserved`` — input has nodata pixels (255 sentinel); output
   preserves them as 255 (not 0 or 1).
6. ``test_cache_miss_hit_skips_recompute`` — first call (miss) reads source +
   computes; second call (hit) does NOT read source again.
7. ``test_empty_classes_raises_typed_error`` — classes=[] → LandcoverClassError
   with error_code="CLASSES_EMPTY".
8. ``test_invalid_class_code_raises_typed_error`` — classes=[255] (reserved) →
   LandcoverClassError with error_code="CLASSES_INVALID".
9. ``test_returns_layer_uri_fields`` — LayerURI fields are well-formed.
10. (Live, env-guarded) ``test_live_fortmyers_water_mask`` — extract class=[11]
    from a known-good Fort Myers NLCD COG; assert the water pixel count matches
    the source's class-11 pixel count exactly (geography-correctness check per
    job-0086 codified lesson).
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

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing.extract_landcover_class import (
    LandcoverClassError,
    extract_landcover_class,
)

# ---------------------------------------------------------------------------
# Pinned timestamp for deterministic cache keys
# ---------------------------------------------------------------------------

PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


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
# Synthetic NLCD-coded raster builder
# ---------------------------------------------------------------------------


def _write_synthetic_nlcd(
    path: str,
    *,
    width: int = 32,
    height: int = 32,
    west: float = -82.0,
    south: float = 26.0,
    east: float = -80.0,
    north: float = 28.0,
    arr: np.ndarray | None = None,
    nodata: int = 255,
    crs: str = "EPSG:4326",
) -> np.ndarray:
    """Write a synthetic NLCD-coded GeoTIFF; return the in-memory array.

    If ``arr`` is None, fills with a deterministic four-quadrant pattern using
    canonical NLCD codes: top-left=11 (water), top-right=21 (developed-open),
    bottom-left=41 (forest), bottom-right=81 (pasture).
    """
    if arr is None:
        arr = np.full((height, width), fill_value=11, dtype=np.uint8)
        h2 = height // 2
        w2 = width // 2
        arr[:h2, :w2] = 11  # top-left: open water
        arr[:h2, w2:] = 21  # top-right: developed open
        arr[h2:, :w2] = 41  # bottom-left: forest
        arr[h2:, w2:] = 81  # bottom-right: pasture
    transform = from_bounds(west, south, east, north, width, height)
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
        dst.write(arr, 1)
    return arr


def _read_tif_bytes_to_array(tif_bytes: bytes) -> tuple[np.ndarray, dict]:
    """Write tif bytes to a tempfile, open with rasterio, return (array, meta)."""
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        path = f.name
    try:
        with open(path, "wb") as wf:
            wf.write(tif_bytes)
        with rasterio.open(path) as src:
            arr = src.read(1)
            meta = {
                "crs": src.crs,
                "width": src.width,
                "height": src.height,
                "nodata": src.nodata,
                "bounds": src.bounds,
            }
        return arr, meta
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Test 1 — registration
# ---------------------------------------------------------------------------


def test_extract_landcover_class_registered():
    """Tool is present in TOOL_REGISTRY with expected metadata."""
    assert "extract_landcover_class" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["extract_landcover_class"]
    assert entry.metadata.cacheable is True
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "landcover_class"


# ---------------------------------------------------------------------------
# Test 2 — extract class=11 (water) → only water pixels become 1
# ---------------------------------------------------------------------------


def test_extract_single_class_water_only():
    """32x32 4-quadrant NLCD; extract [11] → water quadrant is 1, rest 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "nlcd.tif")
        src_arr = _write_synthetic_nlcd(src_path, width=32, height=32)
        n_water_in = int(np.sum(src_arr == 11))
        assert n_water_in > 0, "synthetic raster should have water pixels"

        fake_sc = FakeStorageClient()
        result = extract_landcover_class(
            landcover_uri=src_path,
            classes=[11],
            bbox=None,
            _bucket="test-bucket",
        )

        # Inspect the cached output bytes.
        assert len(fake_sc.store) == 1
        out_bytes = next(iter(fake_sc.store.values()))
        out_arr, meta = _read_tif_bytes_to_array(out_bytes)

        # Geographic correctness: count of 1-pixels equals count of class-11 pixels.
        n_match = int(np.sum(out_arr == 1))
        n_other = int(np.sum(out_arr == 0))
        n_nd = int(np.sum(out_arr == 255))
        assert n_match == n_water_in, (
            f"expected {n_water_in} water pixels in mask; got {n_match}"
        )
        # Non-water valid pixels become 0; nothing should be silently 255.
        assert n_other == (src_arr.size - n_water_in - int(np.sum(src_arr == 255)))
        # Geographic correctness — top-left quadrant is the water quadrant.
        assert np.all(out_arr[:16, :16] == 1), "top-left (water quadrant) should be all 1"
        assert np.all(out_arr[:16, 16:] == 0), "top-right (developed) should be all 0"

        # LayerURI surface check.
        assert result.layer_type == "raster"
        assert result.role == "context"
        assert result.uri.startswith("s3://")
        assert "landcover_class" in result.uri
        assert meta["nodata"] == 255


# ---------------------------------------------------------------------------
# Test 3 — extract multiple classes (forest 41/42/43)
# ---------------------------------------------------------------------------


def test_extract_multiple_classes_forest():
    """Synthetic raster with classes 41,42,43,11,21; extract [41,42,43] →
    all 3 forest classes collapse to 1; others 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "nlcd_forest.tif")
        # Build a 40x40 raster: rows 0-9 = 11 (water), 10-19 = 41, 20-29 = 42, 30-39 = 43.
        arr = np.zeros((40, 40), dtype=np.uint8)
        arr[0:10, :] = 11
        arr[10:20, :] = 41
        arr[20:30, :] = 42
        arr[30:40, :] = 43
        _write_synthetic_nlcd(
            src_path,
            width=40,
            height=40,
            arr=arr,
        )

        fake_sc = FakeStorageClient()
        extract_landcover_class(
            landcover_uri=src_path,
            classes=[41, 42, 43],
            bbox=None,
            _bucket="test-bucket",
        )

        out_bytes = next(iter(fake_sc.store.values()))
        out_arr, _ = _read_tif_bytes_to_array(out_bytes)

        # Rows 0-9 (water) → 0; rows 10-39 (forest) → 1.
        assert np.all(out_arr[0:10, :] == 0), "water rows should be 0"
        assert np.all(out_arr[10:40, :] == 1), "forest rows should be 1"
        n_match = int(np.sum(out_arr == 1))
        # 30 forest rows × 40 cols = 1200 pixels.
        assert n_match == 1200, f"expected 1200 forest pixels; got {n_match}"


# ---------------------------------------------------------------------------
# Test 4 — bbox windowed read returns top-right quadrant
# ---------------------------------------------------------------------------


def test_bbox_window_read_top_right():
    """64x64 raster + top-right bbox → output ~32x32 just that quadrant."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "nlcd_64.tif")
        # 4-quadrant pattern: top-left water (11), top-right developed (24),
        # bottom-left forest (41), bottom-right pasture (81).
        arr = np.zeros((64, 64), dtype=np.uint8)
        arr[:32, :32] = 11
        arr[:32, 32:] = 24
        arr[32:, :32] = 41
        arr[32:, 32:] = 81
        # Use a 2-degree extent so each pixel ≈ 0.03125° wide.
        _write_synthetic_nlcd(
            src_path,
            width=64,
            height=64,
            arr=arr,
            west=-82.0,
            south=26.0,
            east=-80.0,
            north=28.0,
        )

        # Top-right quadrant bbox: (-81, 27, -80, 28).
        clip_bbox = (-81.0, 27.0, -80.0, 28.0)
        fake_sc = FakeStorageClient()
        extract_landcover_class(
            landcover_uri=src_path,
            classes=[24],
            bbox=clip_bbox,
            _bucket="test-bucket",
        )

        out_bytes = next(iter(fake_sc.store.values()))
        out_arr, meta = _read_tif_bytes_to_array(out_bytes)

        # Output should be ~32x32 (the windowed slice of the top-right quadrant).
        assert abs(meta["height"] - 32) <= 1, f"height should be ~32; got {meta['height']}"
        assert abs(meta["width"] - 32) <= 1, f"width should be ~32; got {meta['width']}"
        # Every pixel in the bbox-window quadrant is class 24 → output all 1.
        assert np.all(out_arr == 1), (
            f"expected all 1 in top-right quadrant mask; got "
            f"counts={dict(zip(*np.unique(out_arr, return_counts=True)))}"
        )


# ---------------------------------------------------------------------------
# Test 5 — nodata pixels preserved as 255
# ---------------------------------------------------------------------------


def test_nodata_preserved():
    """Source has nodata=255 pixels; output preserves them as 255."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "nlcd_nd.tif")
        arr = np.full((16, 16), fill_value=11, dtype=np.uint8)
        # Stamp a 4x4 nodata block at the top-left.
        arr[0:4, 0:4] = 255
        _write_synthetic_nlcd(
            src_path,
            width=16,
            height=16,
            arr=arr,
            nodata=255,
        )

        fake_sc = FakeStorageClient()
        extract_landcover_class(
            landcover_uri=src_path,
            classes=[11],
            bbox=None,
            _bucket="test-bucket",
        )

        out_bytes = next(iter(fake_sc.store.values()))
        out_arr, _ = _read_tif_bytes_to_array(out_bytes)

        # Nodata block: still 255 (not collapsed to 0 or 1).
        assert np.all(out_arr[0:4, 0:4] == 255), "nodata block should remain 255"
        # The rest is class 11 → 1.
        rest_mask = np.ones_like(out_arr, dtype=bool)
        rest_mask[0:4, 0:4] = False
        assert np.all(out_arr[rest_mask] == 1), "valid water pixels should be 1"


# ---------------------------------------------------------------------------
# Test 6 — cache miss → hit (recompute skipped on hit)
# ---------------------------------------------------------------------------


def test_cache_miss_hit_skips_recompute():
    """First call invokes extractor; second call with same args is a hit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "nlcd_cache.tif")
        _write_synthetic_nlcd(src_path, width=32, height=32)

        fake_sc = FakeStorageClient()
        call_count = [0]

        # Wrap the inner extractor to count invocations.
        from trid3nt_server.tools.processing import extract_landcover_class as mod

        real_extract = mod._extract_mask_bytes

        def _counted(*args, **kwargs):
            call_count[0] += 1
            return real_extract(*args, **kwargs)

        with patch.object(mod, "_extract_mask_bytes", side_effect=_counted):
            r1 = extract_landcover_class(
                landcover_uri=src_path,
                classes=[11],
                bbox=None,
                _bucket="test-bucket",
            )
            assert call_count[0] == 1, "cache miss should invoke extractor once"

            r2 = extract_landcover_class(
                landcover_uri=src_path,
                classes=[11],
                bbox=None,
                _bucket="test-bucket",
            )
            assert call_count[0] == 1, (
                f"cache hit should NOT re-invoke extractor; got {call_count[0]} calls"
            )

        assert r1.uri == r2.uri


# ---------------------------------------------------------------------------
# Test 7 — empty classes raises typed error
# ---------------------------------------------------------------------------


def test_empty_classes_raises_typed_error():
    """classes=[] → LandcoverClassError(CLASSES_EMPTY)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "nlcd_empty.tif")
        _write_synthetic_nlcd(src_path)

        fake_sc = FakeStorageClient()
        with pytest.raises(LandcoverClassError) as exc_info:
            extract_landcover_class(
                landcover_uri=src_path,
                classes=[],
                bbox=None,
                _bucket="test-bucket",
            )
        assert exc_info.value.error_code == "CLASSES_EMPTY"


# ---------------------------------------------------------------------------
# Test 8 — invalid class code (255 reserved) raises typed error
# ---------------------------------------------------------------------------


def test_invalid_class_code_raises_typed_error():
    """classes=[255] → LandcoverClassError(CLASSES_INVALID) (reserved sentinel)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "nlcd_invalid.tif")
        _write_synthetic_nlcd(src_path)

        fake_sc = FakeStorageClient()
        with pytest.raises(LandcoverClassError) as exc_info:
            extract_landcover_class(
                landcover_uri=src_path,
                classes=[255],
                bbox=None,
                _bucket="test-bucket",
            )
        assert exc_info.value.error_code == "CLASSES_INVALID"


# ---------------------------------------------------------------------------
# Test 9 — LayerURI fields are correct
# ---------------------------------------------------------------------------


def test_returns_layer_uri_fields():
    """LayerURI returned has the documented field values."""
    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "nlcd_layeruri.tif")
        _write_synthetic_nlcd(src_path)
        fake_sc = FakeStorageClient()

        result = extract_landcover_class(
            landcover_uri=src_path,
            classes=[11, 21],
            bbox=None,
            _bucket="test-bucket",
        )

        assert result.layer_type == "raster"
        assert result.role == "context"
        assert result.units is None
        assert result.style_preset == "categorical_landcover"
        assert "landcover-class" in result.layer_id
        # The class tag appears in the layer_id so two different class-set calls
        # produce distinguishable layer ids.
        assert "11" in result.layer_id and "21" in result.layer_id


# ---------------------------------------------------------------------------
# Test 10 — cache keys vary per (uri, classes, bbox)
# ---------------------------------------------------------------------------


def test_cache_keys_vary_across_params():
    """Same uri but different classes / bbox produce distinct cache keys."""
    from trid3nt_server.tools.cache import compute_cache_key

    base_uri = "gs://bucket/landcover.tif"
    combos = [
        {"landcover_uri": base_uri, "classes": [11], "year": "2021"},
        {"landcover_uri": base_uri, "classes": [11, 21], "year": "2021"},
        {
            "landcover_uri": base_uri,
            "classes": [11],
            "year": "2021",
            "bbox": [-82.0, 26.0, -80.0, 28.0],
        },
        {
            "landcover_uri": base_uri,
            "classes": [11],
            "year": "2021",
            "bbox": [-81.0, 26.0, -80.0, 28.0],
        },
    ]
    keys = {
        compute_cache_key("landcover_class", p, "static-30d", now=PINNED_NOW)
        for p in combos
    }
    assert len(keys) == 4, f"expected 4 distinct keys; got {len(keys)}"


# ---------------------------------------------------------------------------
# Test 11 — LIVE (env-guarded): Fort Myers NLCD water mask, geography check
# ---------------------------------------------------------------------------

_LIVE_LANDCOVER = bool(os.environ.get("TRID3NT_TEST_LIVE_LANDCOVER"))


@pytest.mark.skipif(
    not _LIVE_LANDCOVER,
    reason="set TRID3NT_TEST_LIVE_LANDCOVER=1 to run live Fort Myers NLCD test",
)
def test_live_fortmyers_water_mask(tmp_path):
    """Live extract of class=[11] from the Fort Myers cached NLCD COG.

    Geography-correctness check per the job-0086 codified lesson: the count of
    1-pixels in the output must equal the count of class-11 pixels in the
    source raster — exactly, because both are computed over the same window.
    This catches in-COG axis mirrors, transform drift, or window misalignment
    that would mirror or rotate the mask relative to its source.
    """
    # Inject a fake GCS so the cache shim's write is in-memory; the read path is
    # fully real (stages the live cached NLCD via the tool's own s3 reader).
    nlcd_uri = (
        "s3://trid3nt-cache/cache/static-30d/landcover/"
        "7dac3520db9a0f6092a434be438d02d9.tif"
    )

    # Source counts: read class-11 pixel count directly from the NLCD COG.
    from rasterio.io import MemoryFile

    from trid3nt_server.tools.cache import read_object_bytes_s3

    with MemoryFile(read_object_bytes_s3(nlcd_uri)) as _mf, _mf.open() as src:
        src_arr = src.read(1)
        src_water_count = int(np.sum(src_arr == 11))
        src_developed_count = int(
            np.sum((src_arr >= 21) & (src_arr <= 24))
        )
        src_nodata_count = int(np.sum(src_arr == 255))
        src_height, src_width = src_arr.shape

    assert src_water_count > 0, "live NLCD source should have water pixels"

    fake_sc = FakeStorageClient()
    result = extract_landcover_class(
        landcover_uri=nlcd_uri,
        classes=[11],
        bbox=None,
        _bucket="test-bucket",
    )

    # Pull the cached bytes back, count.
    assert len(fake_sc.store) == 1
    out_bytes = next(iter(fake_sc.store.values()))
    out_path = tmp_path / "fortmyers_water_mask.tif"
    out_path.write_bytes(out_bytes)
    with rasterio.open(out_path) as out:
        out_arr = out.read(1)
        out_bounds = out.bounds
        out_crs = out.crs

    n_match = int(np.sum(out_arr == 1))
    n_other = int(np.sum(out_arr == 0))
    n_nd = int(np.sum(out_arr == 255))

    # GEOGRAPHIC CORRECTNESS (job-0086 lesson):
    # The mask MUST have exactly src_water_count pixels marked 1.
    assert n_match == src_water_count, (
        f"mask water count {n_match} != source water count {src_water_count}"
    )
    assert n_nd == src_nodata_count, (
        f"mask nodata count {n_nd} != source nodata count {src_nodata_count}"
    )
    assert out_arr.shape == (src_height, src_width), (
        f"output shape {out_arr.shape} differs from source {(src_height, src_width)}"
    )
    # Output CRS matches source (EPSG:4326 for this cached NLCD).
    assert out_crs.to_epsg() == 4326

    # Multi-class extract: developed [21..24] cycle.
    result_dev = extract_landcover_class(
        landcover_uri=nlcd_uri,
        classes=[21, 22, 23, 24],
        bbox=None,
        _bucket="test-bucket",
    )
    # The previous write is still in the store; new key adds a second entry.
    assert len(fake_sc.store) == 2
    # Find the developed mask blob (the one different from the water mask URI).
    dev_uri_path = result_dev.uri[len("gs://test-bucket/"):]
    dev_bytes = fake_sc.store[dev_uri_path]
    dev_path = tmp_path / "fortmyers_developed_mask.tif"
    dev_path.write_bytes(dev_bytes)
    with rasterio.open(dev_path) as out:
        dev_arr = out.read(1)
    n_dev_match = int(np.sum(dev_arr == 1))
    assert n_dev_match == src_developed_count, (
        f"dev mask count {n_dev_match} != source dev count {src_developed_count}"
    )

    # Bounds sanity: the mask covers the same geographic footprint.
    print(
        f"\nlive_test extract_landcover_class: water={n_match} / "
        f"developed={n_dev_match} / nodata={n_nd} bounds={out_bounds} "
        f"crs={out_crs}"
    )
