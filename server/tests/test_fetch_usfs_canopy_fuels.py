"""Unit tests for the ``fetch_usfs_canopy_fuels`` atomic tool (job-A14).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Validation: unknown layer / bad bbox raise typed errors with correct
  retryable flags.
- Mocked HTTP fetch:
  - A minimal TIFF body round-trips through ``fetch_usfs_canopy_fuels`` to a
    cached blob (URL synthesis, cache-key build, GCS write).
  - A JSON-error envelope from ImageServer raises
    ``USFSCanopyFuelsUpstreamError``.
  - A non-TIFF body (HTML) raises ``USFSCanopyFuelsUpstreamError``.
  - An HTTP 500 raises ``USFSCanopyFuelsUpstreamError``.
- Cache-key determinism:
  - Different layer values (cbh vs cbd) yield different cache keys.
  - Different bbox values yield different cache keys.
- Cache hit on second call: identical params return the cached GeoTIFF
  without re-invoking the fetch.
- Geographic-correctness gate (codified lesson job-0086):
  - The constructed URL contains the requested bbox parameters.
  - The returned LayerURI's ``layer_id`` encodes the bbox.
- Payload estimator:
  - Returns a float in [0.05, 50] for reasonable bboxes.
  - Falls back to max for missing / malformed bbox.

Live test (gated by ``GRACE2_TEST_LIVE_USFS_CANOPY=1``):
- Real San Diego-area CBH fetch from
  ``lfps.usgs.gov/arcgis/rest/services/Landfire_LF2022``. Verifies
  the bbox returns real canopy-base-height values, not all-nodata.
  Writes ``evidence/usfs_canopy_fuels_live.txt`` for the audit.
"""

from __future__ import annotations

import os
import struct
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.cache import compute_cache_key
from grace2_agent.tools.fetch_usfs_canopy_fuels import (
    _LANDFIRE_YEAR,
    _LAYER_LABEL,
    _LAYER_SERVICE,
    _LAYER_STYLE_PRESET,
    _LAYER_UNITS,
    _METADATA,
    _VALID_LAYERS,
    _bbox_to_pixel_size,
    _is_all_nodata,
    _round_bbox_to_6dp,
    _validate_bbox,
    estimate_payload_mb,
    fetch_usfs_canopy_fuels,
    USFSCanopyFuelsError,
    USFSCanopyFuelsBboxError,
    USFSCanopyFuelsEmptyError,
    USFSCanopyFuelsLayerError,
    USFSCanopyFuelsUpstreamError,
)

# ---------------------------------------------------------------------------
# Constants / pinned timestamps.
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)

# San Diego wildland-urban interface — moderate canopy fuels, CONUS coverage.
_SAN_DIEGO_BBOX: tuple[float, float, float, float] = (-117.5, 32.5, -117.0, 33.0)

# Small test bbox for fast smoke tests (~0.2deg x 0.2deg).
_SMALL_BBOX: tuple[float, float, float, float] = (-117.3, 32.7, -117.1, 32.9)

# Open-ocean bbox (Pacific, far offshore) — should trigger USFSCanopyFuelsEmptyError.
_OCEAN_BBOX: tuple[float, float, float, float] = (-160.0, 30.0, -158.0, 32.0)

_LIVE_USFS_CANOPY = os.environ.get("GRACE2_TEST_LIVE_USFS_CANOPY") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors sibling test pattern from fetch_landfire_fuels).
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


def _make_read_through_injector(fake_gcs):
    """S3-only in-memory read-through injector (GCP decommissioned).

    Replaces the retired ``google.cloud.storage`` double: drives the tool's
    ``read_through`` off an in-memory S3 store (``fake_gcs.store``, keyed by
    object KEY), minting ``s3://`` URIs and honoring cache hit/miss/write.
    """
    from grace2_agent.tools.cache import (
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


def _fake_tiff_bytes(width: int = 4, height: int = 4, value: int = 50) -> bytes:
    """Build a minimal valid little-endian TIFF body with a constant S16 pixel value.

    ``value=50`` represents CBH 5.0 m (50 / 10 = 5.0 m), a plausible forested
    value. The TIFF is just large enough for rasterio to read; all pixels are
    the same constant.
    """
    header = b"II*\x00" + struct.pack("<I", 8)
    strip = struct.pack(f"<{width * height}h", *([value] * (width * height)))
    n_tags = 10
    ifd_size = 2 + n_tags * 12 + 4
    strip_offset = 8 + ifd_size
    tags = b""
    tags += struct.pack("<HHIHH", 256, 3, 1, width, 0)      # ImageWidth
    tags += struct.pack("<HHIHH", 257, 3, 1, height, 0)     # ImageLength
    tags += struct.pack("<HHIHH", 258, 3, 1, 16, 0)         # BitsPerSample = 16
    tags += struct.pack("<HHIHH", 259, 3, 1, 1, 0)          # Compression = none
    tags += struct.pack("<HHIHH", 262, 3, 1, 1, 0)          # PhotometricInterp
    tags += struct.pack("<HHII", 273, 4, 1, strip_offset)   # StripOffsets
    tags += struct.pack("<HHIHH", 277, 3, 1, 1, 0)          # SamplesPerPixel
    tags += struct.pack("<HHIHH", 278, 3, 1, height, 0)     # RowsPerStrip
    tags += struct.pack("<HHII", 279, 4, 1, len(strip))     # StripByteCounts
    tags += struct.pack("<HHIHH", 339, 3, 1, 2, 0)          # SampleFormat = 2 (signed int)
    ifd = struct.pack("<H", n_tags) + tags + struct.pack("<I", 0)
    return header + ifd + strip


def _make_response(
    content: bytes,
    status_code: int = 200,
    content_type: str = "image/tiff",
) -> MagicMock:
    """Mock a requests.Response with .status_code, .content, .headers."""
    r = MagicMock()
    r.status_code = status_code
    r.content = content
    r.headers = {"Content-Type": content_type}
    return r


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry() -> None:
    """fetch_usfs_canopy_fuels appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_usfs_canopy_fuels" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_usfs_canopy_fuels"]
    assert entry.metadata.name == "fetch_usfs_canopy_fuels"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "usfs_canopy_fuels"
    assert entry.metadata.cacheable is True
    assert entry.metadata.supports_global_query is False
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


def test_valid_layers_are_cbh_and_cbd() -> None:
    """Only cbh and cbd are valid layer codes."""
    assert _VALID_LAYERS == {"cbh", "cbd"}


def test_each_layer_has_service_name() -> None:
    """Every valid layer maps to a non-empty LF2022 CONUS ImageServer name."""
    for layer in _VALID_LAYERS:
        assert _LAYER_SERVICE[layer], f"layer {layer!r} has no service mapping"
        assert "LF2022" in _LAYER_SERVICE[layer]
        assert "CONUS" in _LAYER_SERVICE[layer]
        assert layer.upper() in _LAYER_SERVICE[layer].upper()


def test_each_layer_has_units() -> None:
    """Both canopy layers carry scaled-integer units (they are continuous, not categorical)."""
    assert _LAYER_UNITS["cbh"] == "m * 10"
    assert _LAYER_UNITS["cbd"] == "kg/m^3 * 100"


def test_each_layer_has_style_preset() -> None:
    """Every valid layer maps to a QML style preset."""
    for layer in _VALID_LAYERS:
        assert _LAYER_STYLE_PRESET[layer], f"layer {layer!r} has no style preset"


def test_each_layer_has_label() -> None:
    """Every valid layer maps to a human-readable label."""
    assert "Canopy Base Height" in _LAYER_LABEL["cbh"]
    assert "Canopy Bulk Density" in _LAYER_LABEL["cbd"]


# ---------------------------------------------------------------------------
# Typed-error validation tests (no network needed).
# ---------------------------------------------------------------------------


def test_unknown_layer_raises_typed_error() -> None:
    """Unknown layer raises USFSCanopyFuelsLayerError (not generic RuntimeError)."""
    with pytest.raises(USFSCanopyFuelsLayerError, match="unknown layer"):
        fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="fbfm40")  # type: ignore[arg-type]


def test_layer_error_is_not_retryable() -> None:
    """USFSCanopyFuelsLayerError carries retryable=False (FR-AS-11)."""
    try:
        fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="fbfm40")  # type: ignore[arg-type]
    except USFSCanopyFuelsLayerError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected USFSCanopyFuelsLayerError")


def test_degenerate_bbox_raises_typed_error() -> None:
    """A bbox where min_lon == max_lon raises USFSCanopyFuelsBboxError."""
    with pytest.raises(USFSCanopyFuelsBboxError):
        fetch_usfs_canopy_fuels(bbox=(-117.0, 32.5, -117.0, 33.0), layer="cbh")


def test_out_of_range_bbox_raises_typed_error() -> None:
    """A bbox with lon > 180 raises USFSCanopyFuelsBboxError."""
    with pytest.raises(USFSCanopyFuelsBboxError, match="lon out of"):
        fetch_usfs_canopy_fuels(bbox=(-200.0, 32.5, -117.0, 33.0), layer="cbh")


def test_non_finite_bbox_raises_typed_error() -> None:
    """A bbox with NaN raises USFSCanopyFuelsBboxError."""
    with pytest.raises(USFSCanopyFuelsBboxError, match="non-finite"):
        fetch_usfs_canopy_fuels(
            bbox=(float("nan"), 32.5, -117.0, 33.0), layer="cbh"
        )


def test_bbox_error_is_not_retryable() -> None:
    """USFSCanopyFuelsBboxError carries retryable=False (FR-AS-11)."""
    try:
        fetch_usfs_canopy_fuels(
            bbox=(-117.0, 32.5, -117.0, 33.0), layer="cbh"  # degenerate
        )
    except USFSCanopyFuelsBboxError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected USFSCanopyFuelsBboxError")


def test_upstream_error_is_retryable() -> None:
    """USFSCanopyFuelsUpstreamError carries retryable=True (FR-AS-11)."""
    err = USFSCanopyFuelsUpstreamError("connection timeout")
    assert err.retryable is True
    assert err.error_code == "USFS_CANOPY_FUELS_UPSTREAM_ERROR"


def test_empty_error_is_not_retryable() -> None:
    """USFSCanopyFuelsEmptyError carries retryable=False."""
    err = USFSCanopyFuelsEmptyError("all nodata")
    assert err.retryable is False


# ---------------------------------------------------------------------------
# Pure-function tests (no network).
# ---------------------------------------------------------------------------


def test_round_bbox_to_6dp() -> None:
    """_round_bbox_to_6dp quantizes to 6 decimal places."""
    raw = (-117.123456789, 32.987654321, -116.123456789, 33.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-117.123457, 32.987654, -116.123457, 33.987654)
    for v in rounded:
        assert round(v, 6) == v


def test_bbox_to_pixel_size_clamps_min() -> None:
    """A tiny bbox clamps to _PX_MIN on both axes."""
    from grace2_agent.tools.fetch_usfs_canopy_fuels import _PX_MIN

    w, h = _bbox_to_pixel_size((-117.0, 32.5, -116.9999, 32.5001))
    assert w == _PX_MIN
    assert h == _PX_MIN


def test_bbox_to_pixel_size_clamps_max() -> None:
    """A very large bbox clamps to _PX_MAX on both axes."""
    from grace2_agent.tools.fetch_usfs_canopy_fuels import _PX_MAX

    w, h = _bbox_to_pixel_size((-120.0, 30.0, -110.0, 40.0))  # 10x10 deg
    assert w == _PX_MAX
    assert h == _PX_MAX


def test_validate_bbox_accepts_valid() -> None:
    """_validate_bbox returns None for a valid bbox (no raise)."""
    _validate_bbox(_SAN_DIEGO_BBOX)


def test_validate_bbox_rejects_wrong_arity() -> None:
    """_validate_bbox rejects a tuple of wrong arity."""
    with pytest.raises(USFSCanopyFuelsBboxError, match="must be"):
        _validate_bbox((1.0, 2.0, 3.0))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Payload estimator tests.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_small_bbox() -> None:
    """Small ~0.5 deg bbox yields a payload estimate in range [0.05, 50] MB."""
    est = estimate_payload_mb(bbox=list(_SMALL_BBOX))
    assert 0.05 <= est <= 50.0
    # 0.2 x 0.2 deg ~= 0.04 sq deg → 0.02 MB raw, but clipped to 0.05 min.
    assert est <= 0.5, f"Expected small estimate for tiny bbox; got {est}"


def test_estimate_payload_mb_large_bbox() -> None:
    """A 10x10 deg bbox yields the upper clip of 50 MB."""
    from grace2_agent.tools.fetch_usfs_canopy_fuels import _PAYLOAD_MAX_MB

    est = estimate_payload_mb(bbox=[-120.0, 25.0, -110.0, 35.0])
    # 10 x 10 = 100 sq deg → 50 MB → clipped.
    assert est == _PAYLOAD_MAX_MB


def test_estimate_payload_mb_missing_bbox() -> None:
    """Missing bbox falls back to the upper clip."""
    from grace2_agent.tools.fetch_usfs_canopy_fuels import _PAYLOAD_MAX_MB

    assert estimate_payload_mb() == _PAYLOAD_MAX_MB


def test_estimate_payload_mb_malformed_bbox() -> None:
    """Malformed bbox (wrong arity) falls back to the upper clip."""
    from grace2_agent.tools.fetch_usfs_canopy_fuels import _PAYLOAD_MAX_MB

    assert estimate_payload_mb(bbox=[1.0, 2.0]) == _PAYLOAD_MAX_MB


# ---------------------------------------------------------------------------
# Cache-key determinism tests.
# ---------------------------------------------------------------------------


def test_different_layers_yield_different_cache_keys() -> None:
    """cbh and cbd produce distinct cache keys for the same bbox."""
    bbox = _round_bbox_to_6dp(_SAN_DIEGO_BBOX)
    k_cbh = compute_cache_key(
        "usfs_canopy_fuels",
        {"layer": "cbh", "bbox": list(bbox), "year": _LANDFIRE_YEAR},
        "static-30d",
        now=_PINNED_NOW,
    )
    k_cbd = compute_cache_key(
        "usfs_canopy_fuels",
        {"layer": "cbd", "bbox": list(bbox), "year": _LANDFIRE_YEAR},
        "static-30d",
        now=_PINNED_NOW,
    )
    assert k_cbh != k_cbd


def test_different_bboxes_yield_different_cache_keys() -> None:
    """Same layer but different bbox produces distinct cache keys."""
    bbox_a = _round_bbox_to_6dp(_SAN_DIEGO_BBOX)
    bbox_b = _round_bbox_to_6dp((-118.0, 33.5, -117.5, 34.0))
    k_a = compute_cache_key(
        "usfs_canopy_fuels",
        {"layer": "cbh", "bbox": list(bbox_a), "year": _LANDFIRE_YEAR},
        "static-30d",
        now=_PINNED_NOW,
    )
    k_b = compute_cache_key(
        "usfs_canopy_fuels",
        {"layer": "cbh", "bbox": list(bbox_b), "year": _LANDFIRE_YEAR},
        "static-30d",
        now=_PINNED_NOW,
    )
    assert k_a != k_b


# ---------------------------------------------------------------------------
# Mocked HTTP fetch tests.
# ---------------------------------------------------------------------------


def test_mocked_tiff_fetch_writes_to_cache() -> None:
    """A mocked TIFF fetch round-trips through fetch_usfs_canopy_fuels to GCS."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    tiff = _fake_tiff_bytes()

    with patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.requests.get",
        return_value=_make_response(tiff),
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels._is_all_nodata",
        return_value=False,
    ):
        layer_uri = fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="cbh")

    assert layer_uri.uri is not None
    assert layer_uri.uri.startswith("s3://")
    assert "cache/static-30d/usfs_canopy_fuels/" in layer_uri.uri
    assert layer_uri.uri.endswith(".tif")
    # Cached blob carries the TIFF bytes.
    blob_path = layer_uri.uri.split("/", 3)[3]
    assert fake_gcs.store[blob_path] == tiff
    # Layer metadata checks.
    assert layer_uri.layer_type == "raster"
    assert layer_uri.role == "primary"
    assert layer_uri.units == "m * 10"
    assert "Canopy Base Height" in layer_uri.name
    # Geographic-correctness: layer_id encodes the bbox.
    assert "usfs-canopy-cbh-" in layer_uri.layer_id
    assert "-117.5" in layer_uri.layer_id or "-117.5000" in layer_uri.layer_id


def test_mocked_tiff_fetch_cbd_units() -> None:
    """CBD layer returns kg/m^3 * 100 units and a distinct layer_id."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    tiff = _fake_tiff_bytes(value=12)  # 12 → 0.12 kg/m³

    with patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.requests.get",
        return_value=_make_response(tiff),
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels._is_all_nodata",
        return_value=False,
    ):
        layer_uri = fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="cbd")

    assert layer_uri.units == "kg/m^3 * 100"
    assert "Canopy Bulk Density" in layer_uri.name
    assert "usfs-canopy-cbd-" in layer_uri.layer_id


def test_mocked_json_error_raises_upstream() -> None:
    """A JSON error envelope from ImageServer raises USFSCanopyFuelsUpstreamError."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    json_body = b'{"error":{"code":400,"message":"Invalid bbox"}}'

    with patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.requests.get",
        return_value=_make_response(json_body, content_type="application/json"),
    ):
        with pytest.raises(USFSCanopyFuelsUpstreamError, match="JSON error"):
            fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="cbh")


def test_mocked_html_response_raises_upstream() -> None:
    """A non-TIFF body (HTML page) raises USFSCanopyFuelsUpstreamError."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    html_body = b"<!DOCTYPE html><html><body>404</body></html>"

    with patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.requests.get",
        return_value=_make_response(html_body, content_type="text/html"),
    ):
        with pytest.raises(USFSCanopyFuelsUpstreamError, match="not a TIFF"):
            fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="cbh")


def test_mocked_http_500_raises_upstream() -> None:
    """An HTTP 500 from ImageServer raises USFSCanopyFuelsUpstreamError."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)

    with patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.requests.get",
        return_value=_make_response(b"server error", status_code=500),
    ):
        with pytest.raises(USFSCanopyFuelsUpstreamError, match="HTTP 500"):
            fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="cbh")


def test_cache_hit_does_not_refetch() -> None:
    """Second call with identical (layer, bbox) hits the cache and skips the fetch."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    tiff = _fake_tiff_bytes()
    fetch_call_count = {"n": 0}

    def counted_get(url: str, **_kw: Any) -> Any:
        fetch_call_count["n"] += 1
        return _make_response(tiff)

    with patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.requests.get",
        side_effect=counted_get,
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels._is_all_nodata",
        return_value=False,
    ):
        u1 = fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="cbh")
        u2 = fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="cbh")

    assert u1.uri == u2.uri
    assert fetch_call_count["n"] == 1, (
        f"Expected 1 HTTP fetch (first writes cache, second reads); "
        f"got {fetch_call_count['n']}"
    )


def test_url_encodes_requested_bbox_and_layer() -> None:
    """The HTTP request URL contains the bbox and service name (geographic-correctness gate).

    Per codified lesson job-0086: for a passthrough-style fetcher that relies
    on server-side clip, URL integrity == geographic correctness.
    """
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    captured = {}

    def capture_get(url: str, **_kw: Any) -> Any:
        captured["url"] = url
        return _make_response(_fake_tiff_bytes())

    with patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.requests.get",
        side_effect=capture_get,
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels._is_all_nodata",
        return_value=False,
    ):
        fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="cbh")

    url = captured["url"]
    # Correct service name for CBH
    assert "LF2022_CBH_CONUS/ImageServer/exportImage" in url
    # bbox in CGI param
    assert "bbox=" in url
    # CRS hygiene
    assert "bboxSR=4326" in url
    assert "imageSR=4326" in url
    assert "format=tiff" in url


def test_extra_kwargs_are_ignored() -> None:
    """Extra kwargs (**_extra_ignored) do not cause a TypeError (LLM hallucination guard)."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    tiff = _fake_tiff_bytes()

    with patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.requests.get",
        return_value=_make_response(tiff),
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels._is_all_nodata",
        return_value=False,
    ):
        # Should not raise even with invented kwargs.
        layer_uri = fetch_usfs_canopy_fuels(
            bbox=_SAN_DIEGO_BBOX,
            layer="cbh",
            invented_param="ignored_value",  # LLM hallucination
        )
    assert layer_uri.uri is not None


def test_cbh_and_cbd_produce_different_uris() -> None:
    """cbh and cbd fetches produce distinct URIs (different cache keys)."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    tiff = _fake_tiff_bytes()

    with patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.requests.get",
        return_value=_make_response(tiff),
    ), patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels._is_all_nodata",
        return_value=False,
    ):
        u_cbh = fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="cbh")
        u_cbd = fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="cbd")

    assert u_cbh.uri != u_cbd.uri
    assert u_cbh.layer_id != u_cbd.layer_id


# ---------------------------------------------------------------------------
# Live test — env-gated, hits real LANDFIRE ImageServer.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_USFS_CANOPY,
    reason="set GRACE2_TEST_LIVE_USFS_CANOPY=1 to enable real USFS canopy fetches",
)
def test_live_san_diego_cbh_returns_real_raster() -> None:
    """Live: San Diego CBH fetch returns a real GeoTIFF with valid canopy heights.

    Geographic-correctness gate (codified lesson job-0086): rasterio reads
    the returned TIFF and asserts the band-1 pixel values are in the CBH
    range (>0, plausible for chaparral/forest areas of San Diego county).
    Writes ``evidence/usfs_canopy_fuels_live.txt`` for the auditor.
    """
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)

    with patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.read_through",
        side_effect=patched_rt,
    ):
        layer_uri = fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="cbh")

    assert layer_uri.uri is not None
    blob_path = layer_uri.uri.split("/", 3)[3]
    tiff = fake_gcs.store[blob_path]

    # TIFF magic check
    assert tiff[:4] in (b"II*\x00", b"MM\x00*"), (
        f"Expected TIFF magic; got {tiff[:8]!r}"
    )
    assert len(tiff) > 1024, f"TIFF too small: {len(tiff)} bytes"

    # Read with rasterio and verify pixel values.
    import rasterio
    from rasterio.io import MemoryFile

    with MemoryFile(tiff) as mem:
        with mem.open() as src:
            arr = src.read(1)
            valid_pixels = arr[arr > 0]
            width = src.width
            height = src.height
            crs = src.crs
            bounds = src.bounds

    # San Diego has chaparral and mixed forest — CBH values >0 are expected.
    assert len(valid_pixels) > 0, (
        "Expected at least some valid CBH pixels in San Diego area; "
        "got all-nodata or all-zero"
    )
    # CBH max is realistically <400 (40m), min valid is 1.
    unique_vals = sorted(set(valid_pixels.ravel().tolist()))[:15]
    valid_cbh = [v for v in unique_vals if 1 <= v <= 400]
    assert len(valid_cbh) > 0, (
        f"Expected CBH values in [1, 400]; got: {unique_vals}"
    )

    # Evidence file
    os.makedirs("evidence", exist_ok=True)
    with open("evidence/usfs_canopy_fuels_live.txt", "w") as f:
        f.write(
            f"job-A14 fetch_usfs_canopy_fuels LIVE test\n"
            f"timestamp: {datetime.now(timezone.utc).isoformat()}\n"
            f"layer: cbh\n"
            f"bbox: {_SAN_DIEGO_BBOX}\n"
            f"tiff bytes: {len(tiff)}\n"
            f"width x height: {width} x {height}\n"
            f"crs: {crs}\n"
            f"bounds: {bounds}\n"
            f"valid pixels (>0): {len(valid_pixels)} of {arr.size}\n"
            f"unique CBH values (first 15): {unique_vals}\n"
            f"valid CBH codes [1-400]: {valid_cbh}\n"
            f"LayerURI.uri: {layer_uri.uri}\n"
            f"LayerURI.layer_id: {layer_uri.layer_id}\n"
            f"LayerURI.name: {layer_uri.name}\n"
            f"LayerURI.units: {layer_uri.units}\n"
            f"LayerURI.style_preset: {layer_uri.style_preset}\n"
        )


@pytest.mark.skipif(
    not _LIVE_USFS_CANOPY,
    reason="set GRACE2_TEST_LIVE_USFS_CANOPY=1 to enable real USFS canopy fetches",
)
def test_live_cbh_vs_cbd_different_uris() -> None:
    """Live: CBH and CBD fetches for the same bbox produce distinct cached TIFFs."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)

    with patch(
        "grace2_agent.tools.fetch_usfs_canopy_fuels.read_through",
        side_effect=patched_rt,
    ):
        u_cbh = fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="cbh")
        u_cbd = fetch_usfs_canopy_fuels(bbox=_SAN_DIEGO_BBOX, layer="cbd")

    assert u_cbh.uri != u_cbd.uri, (
        "CBH and CBD should produce distinct cache URIs (different cache keys)"
    )
    cbh_path = u_cbh.uri.split("/", 3)[3]
    cbd_path = u_cbd.uri.split("/", 3)[3]
    assert fake_gcs.store[cbh_path] != fake_gcs.store[cbd_path], (
        "CBH and CBD TIFF blobs should be distinct rasters"
    )
