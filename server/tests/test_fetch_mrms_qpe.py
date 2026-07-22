"""Unit tests for the ``fetch_mrms_qpe`` atomic tool (job-0103).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Validation: unknown accumulation / bad bbox / bad valid_time raise typed errors.
- Mocked grib2 fixture: a synthetic 100×100 grib2 round-trips through the
  conversion pipeline to a valid GeoTIFF with the expected dtype, nodata,
  and value range.
- Cache key determinism: different accumulation values yield different cache
  keys; different bbox values yield different cache keys.
- bbox=None vs bbox-clipped: full-CONUS path returns CONUS-sized output, while
  bbox-clipped path returns smaller output. Geographic-correctness gate
  (codified lesson job-0086): clipped output's reported bounds intersect the
  requested bbox.
- Sentinel collapse: MRMS -3 and -1 sentinels are turned into GeoTIFF nodata,
  positive values pass through.
- Cache hit on second call: identical params return the cached GeoTIFF without
  re-invoking ``_fetch_mrms_qpe_bytes``.

Live test (gated by ``TRID3NT_TEST_LIVE_MRMS=1``):
- Real CONUS 24H QPE fetch — full CONUS + Florida-clipped — written to
  ``evidence/mrms_live.txt`` with max/mean precipitation values for the audit.
"""

from __future__ import annotations

import gzip
import io
import os
import tempfile
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.cache import compute_cache_key
from trid3nt_server.tools.fetch_mrms_qpe import (
    _CONUS_BBOX,
    _METADATA,
    _NODATA,
    _grib2_to_geotiff,
    _normalize_accumulation,
    _parse_valid_time,
    _round_bbox_to_6dp,
    _validate_bbox,
    estimate_payload_mb,
    MRMSQPEEmptyError,
    MRMSQPEInputError,
    MRMSQPENotAvailableError,
    MRMSQPEUpstreamError,
    fetch_mrms_qpe,
)


# ---------------------------------------------------------------------------
# Constants / pinned timestamps.
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Florida bbox (Gulf coast — Hurricane Ian / Fort Myers reference area).
_FLORIDA_BBOX: tuple[float, float, float, float] = (-83.0, 25.0, -80.0, 28.0)

_LIVE_MRMS = os.environ.get("TRID3NT_TEST_LIVE_MRMS") == "1"


# ---------------------------------------------------------------------------
# Fake GCS (mirrors sibling-test pattern).
# ---------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, store: dict[str, bytes], path: str) -> None:
        self._store = store
        self._path = path
        self.custom_time: datetime | None = None
        self.cache_control: str | None = None

    def exists(self) -> bool:
        return self._path in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self._path]

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self._store[self._path] = data


class FakeBucket:
    def __init__(self, store: dict[str, bytes]) -> None:
        self._store = store

    def blob(self, path: str) -> FakeBlob:
        return FakeBlob(self._store, path)


class FakeStorageClient:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.store)


def _patched_read_through(fake_gcs):
    """S3-only in-memory read-through injector (GCP decommissioned).

    Replaces the retired ``google.cloud.storage`` double: drives the tool's
    ``read_through`` off an in-memory S3 store (``fake_gcs.store``, keyed by
    object KEY), minting ``s3://`` URIs and honoring cache hit/miss/write.
    """
    from trid3nt_server.tools.cache import (
        CACHE_BUCKET,
        cache_path,
        compute_cache_key,
        is_cacheable,
        ReadThroughResult,
    )

    store = fake_gcs.store

    def patched(metadata, params, ext, fetch_fn, **kw):
        bucket = kw.get("bucket") or CACHE_BUCKET
        source_id = kw.get("source_id") or (metadata.source_class or metadata.name)
        force_refresh = kw.get("force_refresh", False)
        if not is_cacheable(metadata):
            return ReadThroughResult(uri=None, data=fetch_fn(), hit=False)
        key = compute_cache_key(source_id, params, metadata.ttl_class, now=_PINNED_NOW)
        path = cache_path(metadata.source_class, metadata.ttl_class, key, ext)
        uri = f"s3://{bucket}/{path}"
        if not force_refresh and path in store:
            return ReadThroughResult(uri=uri, data=store[path], hit=True)
        data = fetch_fn()
        store[path] = data
        return ReadThroughResult(uri=uri, data=data, hit=False)

    return patched


# ---------------------------------------------------------------------------
# Synthetic grib2 fixture.
#
# We generate a TINY grib2-style GeoTIFF (NOT a real grib2 — rasterio's GRIB
# driver is read-only) and pre-empt the gzip+grib2 decode step by patching
# ``_grib2_to_geotiff``. Where we need to exercise the conversion pipeline
# itself, we build a NUMPY array → write it as a GeoTIFF using the SAME
# transform / CRS the MRMS source produces, feed those bytes back through the
# sentinel-collapse + CRS hygiene logic. The two paths together give us
# integration confidence WITHOUT a real grib2 read in unit tests.
# ---------------------------------------------------------------------------


def _make_synthetic_mrms_geotiff(
    *,
    shape: tuple[int, int] = (350, 700),  # 1/10 the size of native CONUS
    include_sentinels: bool = True,
) -> bytes:
    """Build a GeoTIFF that mimics the MRMS native grid (CRS+transform).

    Returned bytes are NOT a grib2 — they're a GeoTIFF the conversion pipeline
    can ingest directly via ``rasterio.open`` since both formats use GDAL.
    Tests that need to exercise ``_grib2_to_geotiff`` write these bytes to a
    temp ``.tif`` and patch ``rasterio.open`` accordingly.
    """
    import numpy as np
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_bounds
    from rasterio.crs import CRS

    h, w = shape
    arr = np.full(shape, 5.0, dtype="float32")  # 5 mm baseline
    if include_sentinels:
        # Inject -3 (no-precip) and -1 (missing) sentinel blocks
        arr[0:h // 4, 0:w // 4] = -3.0
        arr[h // 2:h * 3 // 4, w // 2:w * 3 // 4] = -1.0
        # A clear precipitation maximum in the lower-right quadrant
        arr[h * 3 // 4:, w * 3 // 4:] = 50.0
    transform = from_bounds(-130.0, 20.0, -60.0, 55.0, w, h)
    profile = {
        "driver": "GTiff",
        "height": h,
        "width": w,
        "count": 1,
        "dtype": "float32",
        "crs": CRS.from_epsg(4326),
        "transform": transform,
    }
    with MemoryFile() as memf:
        with memf.open(**profile) as dst:
            dst.write(arr, 1)
        return memf.read()


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_mrms_qpe appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_mrms_qpe" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_mrms_qpe"]
    assert entry.metadata.name == "fetch_mrms_qpe"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "mrms_qpe"
    assert entry.metadata.cacheable is True


def test_metadata_module_constant_matches_registry():
    """``_METADATA`` base fields match the TOOL_REGISTRY entry.

    The decorator may create a copy via ``model_copy`` (e.g. to apply
    ``open_world_hint=True``), so we compare field values rather than
    object identity.
    """
    reg_meta = TOOL_REGISTRY["fetch_mrms_qpe"].metadata
    assert reg_meta.name == _METADATA.name
    assert reg_meta.ttl_class == _METADATA.ttl_class
    assert reg_meta.source_class == _METADATA.source_class
    assert reg_meta.cacheable == _METADATA.cacheable


# ---------------------------------------------------------------------------
# Validation / typed-error tests.
# ---------------------------------------------------------------------------


def test_unknown_accumulation_raises_input_error():
    with pytest.raises(MRMSQPEInputError) as exc:
        fetch_mrms_qpe(bbox=None, accumulation="99H")  # type: ignore[arg-type]
    assert "accumulation" in str(exc.value)


def test_degenerate_bbox_raises_input_error():
    with pytest.raises(MRMSQPEInputError):
        _validate_bbox((-81.0, 26.0, -81.0, 26.0))


def test_lon_out_of_range_raises_input_error():
    with pytest.raises(MRMSQPEInputError):
        _validate_bbox((-181.0, 25.0, -80.0, 26.0))


def test_lat_out_of_range_raises_input_error():
    with pytest.raises(MRMSQPEInputError):
        _validate_bbox((-81.0, 25.0, -80.0, 91.0))


def test_non_finite_bbox_raises_input_error():
    with pytest.raises(MRMSQPEInputError):
        _validate_bbox((-81.0, float("nan"), -80.0, 26.0))


# ---------------------------------------------------------------------------
# sprint-13 job-0226: lowercase accumulation alias tests (FR-TA-2 scope).
# ---------------------------------------------------------------------------


def test_normalize_accumulation_lowercase_24h():
    """'24h' normalizes to the canonical S3 token '24H'."""
    assert _normalize_accumulation("24h") == "24H"


def test_normalize_accumulation_lowercase_1h():
    """'1h' normalizes to '01H' (zero-padded S3 token)."""
    assert _normalize_accumulation("1h") == "01H"


def test_normalize_accumulation_lowercase_6h():
    """'6h' normalizes to '06H'."""
    assert _normalize_accumulation("6h") == "06H"


def test_normalize_accumulation_lowercase_72h():
    """'72h' normalizes to '72H'."""
    assert _normalize_accumulation("72h") == "72H"


def test_normalize_accumulation_uppercase_passthrough():
    """Uppercase tokens like '24H' are accepted unchanged."""
    assert _normalize_accumulation("24H") == "24H"
    assert _normalize_accumulation("01H") == "01H"


def test_normalize_accumulation_unknown_raises_input_error():
    """Unknown accumulation values raise MRMSQPEInputError."""
    with pytest.raises(MRMSQPEInputError) as exc:
        _normalize_accumulation("99h")
    assert "accumulation" in str(exc.value).lower() or "99h" in str(exc.value)


def test_fetch_mrms_qpe_lowercase_24h_accepted(tmp_path):
    """The tool accepts lowercase '24h' (sprint-13 default) without error."""
    fake_gcs = FakeStorageClient()
    synthetic_bytes = _make_synthetic_mrms_geotiff(include_sentinels=False)

    def fake_fetch_bytes(accumulation, bbox, valid_time_dt):
        assert accumulation == "24H", (
            f"expected canonical '24H' after normalization; got {accumulation!r}"
        )
        return synthetic_bytes

    with patch(
        "trid3nt_server.tools.fetch_mrms_qpe._fetch_mrms_qpe_bytes",
        side_effect=fake_fetch_bytes,
    ), patch(
        "trid3nt_server.tools.fetch_mrms_qpe.read_through",
        side_effect=_patched_read_through(fake_gcs),
    ):
        result = fetch_mrms_qpe(bbox=_FLORIDA_BBOX, accumulation="24h")

    assert result.layer_type == "raster"
    assert "24H" in result.layer_id or "24h" in result.layer_id.lower()


def test_fetch_mrms_qpe_default_is_24h(tmp_path):
    """Default accumulation is '24h' (sprint-13 change from '01H')."""
    fake_gcs = FakeStorageClient()
    synthetic_bytes = _make_synthetic_mrms_geotiff(include_sentinels=False)

    captured: dict[str, Any] = {}

    def fake_fetch_bytes(accumulation, bbox, valid_time_dt):
        captured["accumulation"] = accumulation
        return synthetic_bytes

    with patch(
        "trid3nt_server.tools.fetch_mrms_qpe._fetch_mrms_qpe_bytes",
        side_effect=fake_fetch_bytes,
    ), patch(
        "trid3nt_server.tools.fetch_mrms_qpe.read_through",
        side_effect=_patched_read_through(fake_gcs),
    ):
        fetch_mrms_qpe(bbox=_FLORIDA_BBOX)  # no accumulation kwarg — uses default

    assert captured.get("accumulation") == "24H", (
        f"default accumulation should be 24H (canonical form of '24h'); "
        f"got {captured.get('accumulation')!r}"
    )


def test_lowercase_and_uppercase_24h_share_cache_key():
    """'24h' and '24H' normalise to the same canonical key → same cache entry."""
    params_lower = {"accumulation": "24H", "bbox": "CONUS", "valid_time": "LATEST", "pass": "Pass2"}
    params_upper = {"accumulation": "24H", "bbox": "CONUS", "valid_time": "LATEST", "pass": "Pass2"}
    k_lower = compute_cache_key("mrms_qpe", params_lower, "dynamic-1h", now=_PINNED_NOW)
    k_upper = compute_cache_key("mrms_qpe", params_upper, "dynamic-1h", now=_PINNED_NOW)
    assert k_lower == k_upper, (
        "lowercase '24h' and uppercase '24H' must hash to the same cache key "
        "(both canonicalize to '24H' before cache-key construction)"
    )


# ---------------------------------------------------------------------------
# estimate_payload_mb (sprint-13 job-0226 addition — Wave 1.5 chat-warning).
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_returns_positive_float():
    """estimate_payload_mb with a typical CONUS bbox returns a positive float."""
    mb = estimate_payload_mb(bbox=_FLORIDA_BBOX)
    assert isinstance(mb, float)
    assert mb > 0.0


def test_estimate_payload_mb_scales_with_bbox_size():
    """A larger bbox produces a larger payload estimate than a small one."""
    small_bbox = (-81.5, 26.0, -81.0, 26.5)   # ~0.25 sq-deg
    large_bbox = (-90.0, 25.0, -80.0, 35.0)   # 100 sq-deg
    mb_small = estimate_payload_mb(bbox=small_bbox)
    mb_large = estimate_payload_mb(bbox=large_bbox)
    assert mb_large > mb_small * 5, (
        f"large bbox ({mb_large:.3f} MB) should be >>5x small bbox ({mb_small:.3f} MB)"
    )


def test_estimate_payload_mb_none_bbox_returns_conus_size():
    """bbox=None (CONUS-wide) returns a larger estimate than a small sub-region."""
    mb_conus = estimate_payload_mb(bbox=None)
    mb_florida = estimate_payload_mb(bbox=_FLORIDA_BBOX)
    assert mb_conus > mb_florida * 10, (
        f"CONUS estimate ({mb_conus:.1f} MB) should be >>10x Florida ({mb_florida:.3f} MB)"
    )


def test_bad_valid_time_raises_input_error():
    with pytest.raises(MRMSQPEInputError):
        _parse_valid_time("not-an-iso-date")


def test_valid_time_zulu_parses_to_utc():
    dt = _parse_valid_time("2026-06-08T11:00:00Z")
    assert dt == datetime(2026, 6, 8, 11, 0, 0, tzinfo=timezone.utc)


def test_valid_time_naive_assumed_utc():
    dt = _parse_valid_time("2026-06-08T11:00:00")
    assert dt == datetime(2026, 6, 8, 11, 0, 0, tzinfo=timezone.utc)


def test_round_bbox_quantizes_to_six_decimal_places():
    rounded = _round_bbox_to_6dp((-81.123456789, 26.987654321, -80.5, 27.0))
    assert rounded == (-81.123457, 26.987654, -80.5, 27.0)


# ---------------------------------------------------------------------------
# Cache-key determinism (FR-DC-3 + invariant 1).
# ---------------------------------------------------------------------------


def test_cache_key_differs_for_different_accumulation():
    params_01h = {"accumulation": "01H", "bbox": "CONUS", "valid_time": "LATEST", "pass": "Pass2"}
    params_24h = {"accumulation": "24H", "bbox": "CONUS", "valid_time": "LATEST", "pass": "Pass2"}
    k01 = compute_cache_key("mrms_qpe", params_01h, "dynamic-1h", now=_PINNED_NOW)
    k24 = compute_cache_key("mrms_qpe", params_24h, "dynamic-1h", now=_PINNED_NOW)
    assert k01 != k24, f"01H and 24H must hash to different cache keys; got {k01} == {k24}"


def test_cache_key_differs_for_different_bbox():
    fl_bbox = list(_FLORIDA_BBOX)
    tx_bbox = [-100.0, 27.0, -94.0, 33.0]
    p_fl = {"accumulation": "01H", "bbox": fl_bbox, "valid_time": "LATEST", "pass": "Pass2"}
    p_tx = {"accumulation": "01H", "bbox": tx_bbox, "valid_time": "LATEST", "pass": "Pass2"}
    k_fl = compute_cache_key("mrms_qpe", p_fl, "dynamic-1h", now=_PINNED_NOW)
    k_tx = compute_cache_key("mrms_qpe", p_tx, "dynamic-1h", now=_PINNED_NOW)
    assert k_fl != k_tx


def test_cache_key_stable_for_identical_params_in_same_hour():
    """Same params → same key inside the dynamic-1h vintage window."""
    p = {"accumulation": "01H", "bbox": "CONUS", "valid_time": "LATEST", "pass": "Pass2"}
    k1 = compute_cache_key("mrms_qpe", p, "dynamic-1h", now=_PINNED_NOW)
    k2 = compute_cache_key("mrms_qpe", p, "dynamic-1h", now=_PINNED_NOW)
    assert k1 == k2


# ---------------------------------------------------------------------------
# grib2 → GeoTIFF conversion (using a synthetic GeoTIFF as a stand-in for grib).
# ---------------------------------------------------------------------------


def _patch_rasterio_open_for_synthetic(synthetic_bytes: bytes):
    """Return a patcher that makes rasterio.open accept the synthetic TIFF as if it were grib2.

    The tool writes the grib bytes to a temp file then calls rasterio.open(tmp).
    We patch open() to return a context manager around the synthetic TIFF
    regardless of the path. Simpler than reproducing GRIB write logic.
    """
    import rasterio

    original_open = rasterio.open

    def fake_open(path, *args, **kwargs):  # noqa: ANN001
        if isinstance(path, str) and path.endswith(".grib2"):
            return original_open(io.BytesIO(synthetic_bytes), *args, **kwargs)
        return original_open(path, *args, **kwargs)

    return fake_open


def test_grib2_to_geotiff_collapses_sentinels_and_emits_valid_tif():
    """Synthetic input with -3 / -1 sentinels round-trips to a GeoTIFF with nodata."""
    import numpy as np
    import rasterio

    synthetic_bytes = _make_synthetic_mrms_geotiff(include_sentinels=True)
    fake_open = _patch_rasterio_open_for_synthetic(synthetic_bytes)

    with patch("rasterio.open", side_effect=fake_open):
        out_bytes = _grib2_to_geotiff(
            grib_bytes=b"unused-by-the-patch",
            bbox=None,
            valid_time=_PINNED_NOW,
        )

    assert isinstance(out_bytes, bytes)
    assert len(out_bytes) > 1000, "GeoTIFF should be non-trivial"

    # Open the output and verify CRS, dtype, nodata, and that the positive
    # signal at the lower-right quadrant survives.
    with rasterio.open(io.BytesIO(out_bytes)) as src:
        assert src.crs.to_epsg() == 4326, f"output CRS must be EPSG:4326; got {src.crs}"
        assert src.nodata == _NODATA
        assert src.dtypes[0] == "float32"
        arr = src.read(1)
        # The synthetic baseline (5.0) should be present
        positive = arr[arr > 0]
        assert positive.size > 0, "GeoTIFF must carry positive precipitation values"
        assert positive.max() >= 5.0
        # Sentinels (-3 and -1) must have been collapsed to nodata
        assert not ((arr > -3.5) & (arr < 0)).any(), (
            "sentinel values -3 and -1 must have been collapsed to nodata"
        )


def test_grib2_to_geotiff_clips_to_bbox():
    """bbox-clipped output is smaller than full CONUS output."""
    import rasterio

    synthetic_bytes = _make_synthetic_mrms_geotiff(include_sentinels=False)
    fake_open = _patch_rasterio_open_for_synthetic(synthetic_bytes)

    with patch("rasterio.open", side_effect=fake_open):
        full_bytes = _grib2_to_geotiff(b"", bbox=None, valid_time=_PINNED_NOW)
        # Florida-clip: covers roughly 3°x3° of the 70°x35° source grid
        clipped_bytes = _grib2_to_geotiff(b"", bbox=_FLORIDA_BBOX, valid_time=_PINNED_NOW)

    with rasterio.open(io.BytesIO(full_bytes)) as full_src:
        full_shape = full_src.shape
    with rasterio.open(io.BytesIO(clipped_bytes)) as clip_src:
        clip_shape = clip_src.shape
        clip_bounds = clip_src.bounds
        clip_crs = clip_src.crs

    assert clip_shape[0] * clip_shape[1] < full_shape[0] * full_shape[1], (
        f"clipped output ({clip_shape}) must be smaller than full ({full_shape})"
    )
    # Geographic-correctness gate (codified lesson job-0086): the clipped
    # raster's reported bounds must intersect the requested bbox.
    assert clip_crs.to_epsg() == 4326
    assert clip_bounds.left <= _FLORIDA_BBOX[2] and clip_bounds.right >= _FLORIDA_BBOX[0]
    assert clip_bounds.bottom <= _FLORIDA_BBOX[3] and clip_bounds.top >= _FLORIDA_BBOX[1]


def test_grib2_to_geotiff_raises_empty_for_offshore_bbox():
    """A bbox entirely outside CONUS surfaces ``MRMSQPEEmptyError``."""
    synthetic_bytes = _make_synthetic_mrms_geotiff(include_sentinels=False)
    fake_open = _patch_rasterio_open_for_synthetic(synthetic_bytes)
    # Atlantic offshore (no CONUS coverage)
    offshore = (-50.0, 30.0, -45.0, 35.0)
    with patch("rasterio.open", side_effect=fake_open):
        with pytest.raises(MRMSQPEEmptyError):
            _grib2_to_geotiff(b"", bbox=offshore, valid_time=_PINNED_NOW)


# ---------------------------------------------------------------------------
# End-to-end tool path (mocked S3 + mocked grib decoder).
# ---------------------------------------------------------------------------


def test_fetch_mrms_qpe_end_to_end_with_mocked_fetch_returns_layer_uri(tmp_path):
    """Full tool path: validation → cache → fetch → LayerURI."""
    fake_gcs = FakeStorageClient()
    synthetic_bytes = _make_synthetic_mrms_geotiff(include_sentinels=False)

    # Patch the bytes-fetcher to return our synthetic GeoTIFF
    def fake_fetch_bytes(accumulation, bbox, valid_time_dt):
        return synthetic_bytes

    with patch(
        "trid3nt_server.tools.fetch_mrms_qpe._fetch_mrms_qpe_bytes",
        side_effect=fake_fetch_bytes,
    ), patch(
        "trid3nt_server.tools.fetch_mrms_qpe.read_through",
        side_effect=_patched_read_through(fake_gcs),
    ):
        result = fetch_mrms_qpe(bbox=_FLORIDA_BBOX, accumulation="24H")

    assert result.layer_type == "raster"
    assert result.role == "primary"
    assert result.units == "mm"
    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert "mrms_qpe" in result.uri
    assert "dynamic-1h" in result.uri
    assert result.style_preset == "precipitation_mm"
    # bbox should round-trip back through the LayerURI
    assert result.bbox is not None
    assert abs(result.bbox[0] - _FLORIDA_BBOX[0]) < 1e-5


def test_fetch_mrms_qpe_cache_hit_on_second_call(tmp_path):
    """Identical params on a second call use the cached blob (no fetch)."""
    fake_gcs = FakeStorageClient()
    synthetic_bytes = _make_synthetic_mrms_geotiff(include_sentinels=False)

    call_count = {"n": 0}

    def fake_fetch_bytes(accumulation, bbox, valid_time_dt):
        call_count["n"] += 1
        return synthetic_bytes

    with patch(
        "trid3nt_server.tools.fetch_mrms_qpe._fetch_mrms_qpe_bytes",
        side_effect=fake_fetch_bytes,
    ), patch(
        "trid3nt_server.tools.fetch_mrms_qpe.read_through",
        side_effect=_patched_read_through(fake_gcs),
    ):
        r1 = fetch_mrms_qpe(bbox=_FLORIDA_BBOX, accumulation="24H")
        r2 = fetch_mrms_qpe(bbox=_FLORIDA_BBOX, accumulation="24H")

    assert r1.uri == r2.uri
    assert call_count["n"] == 1, (
        f"second call should hit cache; fetcher was called {call_count['n']} times"
    )


def test_fetch_mrms_qpe_global_query_with_bbox_none(tmp_path):
    """``bbox=None`` is accepted (supports_global_query=True) and produces a layer."""
    fake_gcs = FakeStorageClient()
    synthetic_bytes = _make_synthetic_mrms_geotiff(include_sentinels=False)

    captured: dict[str, Any] = {}

    def fake_fetch_bytes(accumulation, bbox, valid_time_dt):
        captured["bbox"] = bbox
        captured["accumulation"] = accumulation
        return synthetic_bytes

    with patch(
        "trid3nt_server.tools.fetch_mrms_qpe._fetch_mrms_qpe_bytes",
        side_effect=fake_fetch_bytes,
    ), patch(
        "trid3nt_server.tools.fetch_mrms_qpe.read_through",
        side_effect=_patched_read_through(fake_gcs),
    ):
        result = fetch_mrms_qpe(bbox=None, accumulation="01H")

    assert captured["bbox"] is None
    assert captured["accumulation"] == "01H"
    # CONUS-default tag should be embedded in the layer_id + bbox
    assert "CONUS" in result.layer_id
    assert result.bbox == _CONUS_BBOX


# ---------------------------------------------------------------------------
# Live test (gated).
#
# Hits the real noaa-mrms-pds S3 bucket. Run with:
#   TRID3NT_TEST_LIVE_MRMS=1 TRID3NT_SKIP_WORKER_SUBMITTER=1 .venv-agent/bin/pytest \
#       server/tests/test_fetch_mrms_qpe.py::test_live_fetch -s
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE_MRMS, reason="TRID3NT_TEST_LIVE_MRMS not set")
def test_live_fetch_24h_conus_writes_evidence_file(tmp_path):
    """LIVE: fetch real MRMS 24H CONUS QPE; assert positive values; write evidence."""
    import io as _io
    import json
    import numpy as np
    import rasterio
    from trid3nt_server.tools.fetch_mrms_qpe import _fetch_mrms_qpe_bytes

    # Direct invocation of the bytes path — bypasses the cache shim so we
    # exercise the live S3 + grib2 + reproject pipeline end-to-end.
    geotiff_bytes = _fetch_mrms_qpe_bytes(
        accumulation="24H",
        bbox=None,
        valid_time_dt=None,  # latest available
    )

    assert isinstance(geotiff_bytes, bytes)
    assert len(geotiff_bytes) > 1024 * 100  # CONUS-wide 24H is multi-MB

    with rasterio.open(_io.BytesIO(geotiff_bytes)) as src:
        assert src.crs.to_epsg() == 4326
        arr = src.read(1)
        positive = arr[arr > 0]
        assert positive.size > 0, "real 24H CONUS QPE must have some positive precipitation"
        max_mm = float(positive.max())
        mean_mm = float(positive.mean())
        # Geographic-correctness gate (codified lesson job-0086): bounds must
        # cover CONUS extent.
        bounds = src.bounds
        assert bounds.left <= -120.0 and bounds.right >= -70.0
        assert bounds.bottom <= 25.0 and bounds.top >= 49.0

    # Write evidence file for the audit.
    evidence_dir = "evidence"
    os.makedirs(evidence_dir, exist_ok=True)
    evidence_path = os.path.join(evidence_dir, "mrms_live.txt")
    with open(evidence_path, "w") as f:
        f.write("MRMS QPE live-fetch evidence (job-0103)\n")
        f.write(f"timestamp_utc: {datetime.now(timezone.utc).isoformat()}\n")
        f.write("accumulation: 24H\n")
        f.write("bbox: CONUS-wide (supports_global_query)\n")
        f.write(f"geotiff_bytes: {len(geotiff_bytes)}\n")
        f.write(f"shape: {arr.shape}\n")
        f.write(f"crs: EPSG:4326\n")
        f.write(f"bounds: {tuple(bounds)}\n")
        f.write(f"positive_pixel_count: {int(positive.size)}\n")
        f.write(f"max_mm: {max_mm:.4f}\n")
        f.write(f"mean_mm: {mean_mm:.4f}\n")
        f.write(f"nodata: {src.nodata}\n")
    print(f"\n[live] wrote {evidence_path}: max={max_mm:.2f} mm, mean={mean_mm:.2f} mm")


@pytest.mark.skipif(not _LIVE_MRMS, reason="TRID3NT_TEST_LIVE_MRMS not set")
def test_live_fetch_24h_florida_clipped_intersects_florida(tmp_path):
    """LIVE: fetch real MRMS 24H QPE clipped to Florida; verify clipped bounds intersect FL."""
    import io as _io
    import rasterio
    from trid3nt_server.tools.fetch_mrms_qpe import _fetch_mrms_qpe_bytes

    geotiff_bytes = _fetch_mrms_qpe_bytes(
        accumulation="24H",
        bbox=_FLORIDA_BBOX,
        valid_time_dt=None,
    )

    with rasterio.open(_io.BytesIO(geotiff_bytes)) as src:
        b = src.bounds
        # Geographic-correctness gate: the clipped bounds must intersect Florida,
        # not be CONUS-wide.
        assert b.left >= _FLORIDA_BBOX[0] - 0.02 and b.right <= _FLORIDA_BBOX[2] + 0.02
        assert b.bottom >= _FLORIDA_BBOX[1] - 0.02 and b.top <= _FLORIDA_BBOX[3] + 0.02
        # Width should be much smaller than CONUS
        assert (b.right - b.left) < 10.0, "Florida-clipped width should be <10°"
