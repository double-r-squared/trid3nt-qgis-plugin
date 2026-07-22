"""Unit tests for the ``fetch_landfire_fuels`` atomic tool (job-0111).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Validation: unknown layer / bad bbox raise typed errors.
- Mocked HTTP fetch:
  - A minimal TIFF body round-trips through ``fetch_landfire_fuels`` to a
    cached blob (URL synthesis, cache-key build, GCS write).
  - A JSON-error envelope from ImageServer raises LandfireFuelsUpstreamError.
  - A non-TIFF body (HTML, plain text) raises LandfireFuelsUpstreamError.
- Cache-key determinism:
  - Different ``layer`` values yield different cache keys.
  - Different ``bbox`` values yield different cache keys.
- Cache hit on second call: identical params return the cached GeoTIFF
  without re-invoking the fetch.
- Geographic-correctness gate (codified lesson job-0086):
  - The constructed URL contains the requested bbox parameters (the
    server-side clip is bbox-driven, so URL integrity == geographic
    correctness for this passthrough-style fetcher).
  - The returned LayerURI's ``layer_id`` encodes the bbox so distinct
    bboxes produce distinct layer ids.

Live test (gated by ``GRACE2_TEST_LIVE_LANDFIRE=1``):
- Real California Sierra Nevada bbox FBFM40 fetch from
  ``lfps.usgs.gov/arcgis/rest/services/Landfire_LF2022``. Writes
  ``evidence/landfire_live.txt`` with a value-distribution summary so the
  audit can verify the bbox returns real fuel-model classes, not nodata.
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
from grace2_agent.tools.fetch_landfire_fuels import (
    _LANDFIRE_YEAR,
    _LAYER_SERVICE,
    _LAYER_STYLE_PRESET,
    _LAYER_UNITS,
    _METADATA,
    _VALID_LAYERS,
    _bbox_to_pixel_size,
    _build_metadata,
    _is_all_nodata,
    _round_bbox_to_6dp,
    _validate_bbox,
    LandfireFuelsBboxError,
    LandfireFuelsEmptyError,
    LandfireFuelsError,
    LandfireFuelsLayerError,
    LandfireFuelsUpstreamError,
    fetch_landfire_fuels,
)

# ---------------------------------------------------------------------------
# Constants / pinned timestamps.
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# California Sierra Nevada bbox — covers FBFM40-rich forests + mixed canopy.
# The kickoff's live-verification target.
_CA_SIERRA_BBOX: tuple[float, float, float, float] = (-122.0, 38.0, -119.0, 40.0)

# Open-ocean bbox (Pacific, far offshore) — used to verify the
# all-nodata gate raises LandfireFuelsEmptyError.
_OCEAN_BBOX: tuple[float, float, float, float] = (-160.0, 30.0, -158.0, 32.0)

_LIVE_LANDFIRE = os.environ.get("GRACE2_TEST_LIVE_LANDFIRE") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors sibling test pattern).
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


def _fake_tiff_bytes(width: int = 4, height: int = 4, value: int = 121) -> bytes:
    """Build a minimal but valid little-endian TIFF body with a constant pixel value.

    Produces a tiny single-strip ``S16`` GeoTIFF-shaped blob. Used in mock
    tests to avoid the real ImageServer and rasterio.
    """
    # Minimal valid TIFF: header + IFD with the absolute minimum tags.
    # 'II*\x00' marks little-endian; offset to IFD = 8.
    header = b"II*\x00" + struct.pack("<I", 8)
    # Strip bytes: width*height pixels at int16
    strip = struct.pack(f"<{width*height}h", *([value] * (width * height)))
    # We tack the strip onto the end and reference it by offset.
    # IFD: count(2) + 8 tags(12 bytes each) + next-IFD-offset(4)
    # We'll write a minimal tag set sufficient for libtiff to parse:
    # 256 ImageWidth, 257 ImageLength, 258 BitsPerSample, 259 Compression,
    # 262 PhotometricInterpretation, 273 StripOffsets, 277 SamplesPerPixel,
    # 278 RowsPerStrip, 279 StripByteCounts, 339 SampleFormat
    n_tags = 10
    ifd_size = 2 + n_tags * 12 + 4
    strip_offset = 8 + ifd_size
    tags = b""
    # ImageWidth (256), SHORT(3), count 1
    tags += struct.pack("<HHIHH", 256, 3, 1, width, 0)
    # ImageLength (257)
    tags += struct.pack("<HHIHH", 257, 3, 1, height, 0)
    # BitsPerSample (258) - 16
    tags += struct.pack("<HHIHH", 258, 3, 1, 16, 0)
    # Compression (259) - 1 (none)
    tags += struct.pack("<HHIHH", 259, 3, 1, 1, 0)
    # PhotometricInterpretation (262) - 1 (BlackIsZero)
    tags += struct.pack("<HHIHH", 262, 3, 1, 1, 0)
    # StripOffsets (273)
    tags += struct.pack("<HHII", 273, 4, 1, strip_offset)
    # SamplesPerPixel (277) - 1
    tags += struct.pack("<HHIHH", 277, 3, 1, 1, 0)
    # RowsPerStrip (278)
    tags += struct.pack("<HHIHH", 278, 3, 1, height, 0)
    # StripByteCounts (279)
    tags += struct.pack("<HHII", 279, 4, 1, len(strip))
    # SampleFormat (339) - 2 (signed int)
    tags += struct.pack("<HHIHH", 339, 3, 1, 2, 0)
    ifd = struct.pack("<H", n_tags) + tags + struct.pack("<I", 0)
    return header + ifd + strip


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry() -> None:
    """fetch_landfire_fuels appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_landfire_fuels" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_landfire_fuels"]
    assert entry.metadata.name == "fetch_landfire_fuels"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "landfire_fuels"
    assert entry.metadata.cacheable is True


def test_six_layers_are_defined() -> None:
    """All six required layer codes are present (cc/ch added by FIRE-2)."""
    assert _VALID_LAYERS == {"fbfm40", "fbfm13", "cbh", "cbd", "cc", "ch"}


def test_each_layer_has_a_service_name() -> None:
    """Every valid layer maps to a non-empty ImageServer service name."""
    for layer in _VALID_LAYERS:
        assert _LAYER_SERVICE[layer], f"layer {layer!r} has no service mapping"
        assert "LF2022" in _LAYER_SERVICE[layer]
        assert "CONUS" in _LAYER_SERVICE[layer]


def test_each_layer_has_a_style_preset() -> None:
    """Every valid layer maps to a QML style preset."""
    for layer in _VALID_LAYERS:
        assert _LAYER_STYLE_PRESET[layer], (
            f"layer {layer!r} has no style preset"
        )


def test_units_set_for_continuous_layers_and_none_for_categorical() -> None:
    """Fuel-model layers are unitless; canopy layers carry scaled-int units."""
    assert _LAYER_UNITS["fbfm40"] is None
    assert _LAYER_UNITS["fbfm13"] is None
    assert _LAYER_UNITS["cbh"] is not None
    assert _LAYER_UNITS["cbd"] is not None
    assert _LAYER_UNITS["cc"] == "percent"
    assert _LAYER_UNITS["ch"] == "m * 10"


def test_build_metadata_defends_against_schema_extension() -> None:
    """_build_metadata tolerates AtomicToolMetadata without supports_global_query.

    This exercises the Wave 1.5 schema-defensive construction — the parallel
    job-0114-schema adds the field, but this tool registers cleanly either
    way.
    """
    meta = _build_metadata()
    assert meta.name == "fetch_landfire_fuels"
    assert meta.ttl_class == "static-30d"


# ---------------------------------------------------------------------------
# Typed-error tests (no network needed).
# ---------------------------------------------------------------------------


def test_unknown_layer_raises_typed_error() -> None:
    """Unknown layer raises LandfireFuelsLayerError (not generic RuntimeError)."""
    with pytest.raises(LandfireFuelsLayerError, match="unknown layer"):
        fetch_landfire_fuels(bbox=_CA_SIERRA_BBOX, layer="bogus")  # type: ignore[arg-type]


def test_layer_error_is_not_retryable() -> None:
    """LandfireFuelsLayerError carries retryable=False (FR-AS-11)."""
    try:
        fetch_landfire_fuels(bbox=_CA_SIERRA_BBOX, layer="bogus")  # type: ignore[arg-type]
    except LandfireFuelsLayerError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected LandfireFuelsLayerError")


def test_degenerate_bbox_raises_typed_error() -> None:
    """A bbox where min == max raises LandfireFuelsBboxError before any HTTP."""
    with pytest.raises(LandfireFuelsBboxError):
        fetch_landfire_fuels(bbox=(-122.0, 38.0, -122.0, 38.0), layer="fbfm40")


def test_out_of_range_bbox_raises_typed_error() -> None:
    """A bbox with lon > 180 raises LandfireFuelsBboxError."""
    with pytest.raises(LandfireFuelsBboxError, match="lon out of"):
        fetch_landfire_fuels(bbox=(-200.0, 38.0, -119.0, 40.0), layer="fbfm40")


def test_non_finite_bbox_raises_typed_error() -> None:
    """A bbox with NaN raises LandfireFuelsBboxError."""
    with pytest.raises(LandfireFuelsBboxError, match="non-finite"):
        fetch_landfire_fuels(
            bbox=(float("nan"), 38.0, -119.0, 40.0), layer="fbfm40"
        )


# ---------------------------------------------------------------------------
# Pure-function tests (no network).
# ---------------------------------------------------------------------------


def test_round_bbox_to_6dp() -> None:
    """_round_bbox_to_6dp quantizes to 6 decimal places."""
    raw = (-122.123456789, 38.987654321, -121.123456789, 39.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-122.123457, 38.987654, -121.123457, 39.987654)
    for v in rounded:
        assert round(v, 6) == v


def test_bbox_to_pixel_size_clamps_to_bounds() -> None:
    """A tiny bbox clamps to the _PX_MIN; a huge bbox clamps to _PX_MAX."""
    from grace2_agent.tools.fetch_landfire_fuels import _PX_MAX, _PX_MIN

    # Tiny bbox: ~0.0001° wide ≈ 11 m at mid-lat.
    w, h = _bbox_to_pixel_size((-122.0, 38.0, -121.9999, 38.0001))
    assert w == _PX_MIN
    assert h == _PX_MIN

    # Huge bbox: 10° × 10° ≈ 1100 km wide at 30 m → 37k px, clamped.
    w, h = _bbox_to_pixel_size((-122.0, 38.0, -112.0, 48.0))
    assert w == _PX_MAX
    assert h == _PX_MAX


def test_validate_bbox_accepts_valid_bbox() -> None:
    """_validate_bbox returns None for a valid bbox."""
    _validate_bbox(_CA_SIERRA_BBOX)  # should not raise


def test_validate_bbox_rejects_wrong_arity() -> None:
    """_validate_bbox rejects a tuple of wrong arity."""
    with pytest.raises(LandfireFuelsBboxError, match="must be"):
        _validate_bbox((1.0, 2.0, 3.0))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cache-key determinism tests.
# ---------------------------------------------------------------------------


def test_different_layers_yield_different_cache_keys() -> None:
    """Two calls with different layers should produce distinct cache keys."""
    bbox = _round_bbox_to_6dp(_CA_SIERRA_BBOX)
    k_fbfm40 = compute_cache_key(
        "landfire_fuels",
        {"layer": "fbfm40", "bbox": list(bbox), "year": _LANDFIRE_YEAR},
        "static-30d",
        now=_PINNED_NOW,
    )
    k_fbfm13 = compute_cache_key(
        "landfire_fuels",
        {"layer": "fbfm13", "bbox": list(bbox), "year": _LANDFIRE_YEAR},
        "static-30d",
        now=_PINNED_NOW,
    )
    k_cbh = compute_cache_key(
        "landfire_fuels",
        {"layer": "cbh", "bbox": list(bbox), "year": _LANDFIRE_YEAR},
        "static-30d",
        now=_PINNED_NOW,
    )
    assert k_fbfm40 != k_fbfm13
    assert k_fbfm40 != k_cbh
    assert k_fbfm13 != k_cbh


def test_different_bboxes_yield_different_cache_keys() -> None:
    """Two calls with the same layer but different bbox should yield distinct keys."""
    bbox_a = _round_bbox_to_6dp((-122.0, 38.0, -119.0, 40.0))
    bbox_b = _round_bbox_to_6dp((-122.5, 38.5, -119.5, 40.5))
    k_a = compute_cache_key(
        "landfire_fuels",
        {"layer": "fbfm40", "bbox": list(bbox_a), "year": _LANDFIRE_YEAR},
        "static-30d",
        now=_PINNED_NOW,
    )
    k_b = compute_cache_key(
        "landfire_fuels",
        {"layer": "fbfm40", "bbox": list(bbox_b), "year": _LANDFIRE_YEAR},
        "static-30d",
        now=_PINNED_NOW,
    )
    assert k_a != k_b


# ---------------------------------------------------------------------------
# Mocked HTTP fetch tests.
# ---------------------------------------------------------------------------


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


def test_mocked_tiff_fetch_writes_to_cache() -> None:
    """A mocked TIFF fetch round-trips through read_through into the fake GCS store."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    tiff = _fake_tiff_bytes()

    with patch(
        "grace2_agent.tools.fetch_landfire_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_landfire_fuels.requests.get",
        return_value=_make_response(tiff),
    ), patch(
        # The all-nodata gate uses rasterio; in the mocked path we want to
        # short-circuit it so the tiny fake TIFF doesn't fail rasterio's
        # validation.
        "grace2_agent.tools.fetch_landfire_fuels._is_all_nodata",
        return_value=False,
    ):
        layer_uri = fetch_landfire_fuels(bbox=_CA_SIERRA_BBOX, layer="fbfm40")

    assert layer_uri.uri is not None
    assert layer_uri.uri.startswith("s3://")
    assert "cache/static-30d/landfire_fuels/" in layer_uri.uri
    assert layer_uri.uri.endswith(".tif")
    # Cached blob carries the TIFF bytes we returned.
    blob_path = layer_uri.uri.split("/", 3)[3]  # strip gs://bucket/
    assert fake_gcs.store[blob_path] == tiff
    # Geographic-correctness gate: layer_id encodes the bbox.
    assert "landfire-fbfm40-" in layer_uri.layer_id
    assert "-122.0000" in layer_uri.layer_id
    assert "38.0000" in layer_uri.layer_id
    # role + layer_type per kickoff
    assert layer_uri.layer_type == "raster"
    assert layer_uri.role == "primary"
    # No scalar unit for fuel-model categories.
    assert layer_uri.units is None


def test_mocked_json_error_envelope_raises_upstream() -> None:
    """A JSON error body (ImageServer rejection) raises LandfireFuelsUpstreamError."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    json_body = b'{"error":{"code":400,"message":"Invalid bbox"}}'

    with patch(
        "grace2_agent.tools.fetch_landfire_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_landfire_fuels.requests.get",
        return_value=_make_response(
            json_body, content_type="application/json"
        ),
    ):
        with pytest.raises(LandfireFuelsUpstreamError, match="JSON error"):
            fetch_landfire_fuels(bbox=_CA_SIERRA_BBOX, layer="fbfm40")


def test_mocked_html_response_raises_upstream() -> None:
    """A non-TIFF body (HTML page) raises LandfireFuelsUpstreamError."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    html_body = b"<!DOCTYPE html><html><body>404</body></html>"

    with patch(
        "grace2_agent.tools.fetch_landfire_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_landfire_fuels.requests.get",
        return_value=_make_response(html_body, content_type="text/html"),
    ):
        with pytest.raises(LandfireFuelsUpstreamError, match="not a TIFF"):
            fetch_landfire_fuels(bbox=_CA_SIERRA_BBOX, layer="fbfm40")


def test_mocked_http_500_raises_upstream() -> None:
    """An HTTP 500 from ImageServer raises LandfireFuelsUpstreamError."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)

    with patch(
        "grace2_agent.tools.fetch_landfire_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_landfire_fuels.requests.get",
        return_value=_make_response(b"server error", status_code=500),
    ):
        with pytest.raises(LandfireFuelsUpstreamError, match="HTTP 500"):
            fetch_landfire_fuels(bbox=_CA_SIERRA_BBOX, layer="fbfm40")


def test_cache_hit_does_not_refetch() -> None:
    """Second call with same (layer, bbox) hits the cache and skips fetch."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    tiff = _fake_tiff_bytes()
    fetch_call_count = {"n": 0}

    def counted_get(url: str, **_kw: Any) -> Any:
        fetch_call_count["n"] += 1
        return _make_response(tiff)

    with patch(
        "grace2_agent.tools.fetch_landfire_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_landfire_fuels.requests.get",
        side_effect=counted_get,
    ), patch(
        "grace2_agent.tools.fetch_landfire_fuels._is_all_nodata",
        return_value=False,
    ):
        u1 = fetch_landfire_fuels(bbox=_CA_SIERRA_BBOX, layer="fbfm40")
        u2 = fetch_landfire_fuels(bbox=_CA_SIERRA_BBOX, layer="fbfm40")

    assert u1.uri == u2.uri
    assert fetch_call_count["n"] == 1, (
        f"Expected 1 fetch call (first call writes, second reads from cache); "
        f"got {fetch_call_count['n']}"
    )


def test_url_encodes_requested_bbox() -> None:
    """The HTTP request URL contains the bbox parameters (geographic-correctness gate).

    Per codified lesson job-0086: for a passthrough-style fetcher that
    relies on server-side clip, URL integrity == geographic correctness.
    We capture the URL the tool sends to the LANDFIRE ImageServer and
    assert it contains the bbox we asked for.
    """
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    captured = {}

    def capture_get(url: str, **_kw: Any) -> Any:
        captured["url"] = url
        return _make_response(_fake_tiff_bytes())

    with patch(
        "grace2_agent.tools.fetch_landfire_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_landfire_fuels.requests.get",
        side_effect=capture_get,
    ), patch(
        "grace2_agent.tools.fetch_landfire_fuels._is_all_nodata",
        return_value=False,
    ):
        fetch_landfire_fuels(bbox=_CA_SIERRA_BBOX, layer="fbfm40")

    url = captured["url"]
    # bbox in CGI param (URL-encoded comma is %2C)
    assert "bbox=" in url
    assert "-122.0%2C38.0%2C-119.0%2C40.0" in url or "-122.0,38.0,-119.0,40.0" in url
    # Correct service name
    assert "LF2022_FBFM40_CONUS/ImageServer/exportImage" in url
    # CRS hygiene
    assert "bboxSR=4326" in url
    assert "imageSR=4326" in url
    assert "format=tiff" in url


# ---------------------------------------------------------------------------
# FIRE-2: cc / ch canopy layers (ELMFIRE deck inputs) — mocked, no network.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("layer", "service", "label"),
    [
        ("cc", "LF2022_CC_CONUS", "Canopy Cover"),
        ("ch", "LF2022_CH_CONUS", "Canopy Height"),
    ],
)
def test_mocked_cc_ch_fetch_targets_correct_service(
    layer: str, service: str, label: str
) -> None:
    """cc/ch fetches hit their own ImageServer and return a well-formed LayerURI."""
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)
    captured = {}

    def capture_get(url: str, **_kw: Any) -> Any:
        captured["url"] = url
        return _make_response(_fake_tiff_bytes())

    with patch(
        "grace2_agent.tools.fetch_landfire_fuels.read_through",
        side_effect=patched_rt,
    ), patch(
        "grace2_agent.tools.fetch_landfire_fuels.requests.get",
        side_effect=capture_get,
    ), patch(
        "grace2_agent.tools.fetch_landfire_fuels._is_all_nodata",
        return_value=False,
    ):
        layer_uri = fetch_landfire_fuels(bbox=_CA_SIERRA_BBOX, layer=layer)  # type: ignore[arg-type]

    assert f"{service}/ImageServer/exportImage" in captured["url"]
    assert "bbox=" in captured["url"]
    assert layer_uri.uri is not None
    assert layer_uri.uri.startswith("s3://")
    assert f"landfire-{layer}-" in layer_uri.layer_id
    assert label in layer_uri.name
    assert layer_uri.layer_type == "raster"
    assert layer_uri.units is not None  # continuous canopy layer
    assert layer_uri.style_preset == _LAYER_STYLE_PRESET[layer]


def test_cc_ch_cache_keys_distinct_from_each_other_and_cbh() -> None:
    """cc / ch / cbh over the same bbox produce three distinct cache keys."""
    bbox = _round_bbox_to_6dp(_CA_SIERRA_BBOX)
    keys = {
        layer: compute_cache_key(
            "landfire_fuels",
            {"layer": layer, "bbox": list(bbox), "year": _LANDFIRE_YEAR},
            "static-30d",
            now=_PINNED_NOW,
        )
        for layer in ("cc", "ch", "cbh")
    }
    assert len(set(keys.values())) == 3


# ---------------------------------------------------------------------------
# Live test — env-gated, hits real LANDFIRE ImageServer.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_LANDFIRE,
    reason="set GRACE2_TEST_LIVE_LANDFIRE=1 to enable real LANDFIRE fetches",
)
def test_live_california_fbfm40_returns_real_raster() -> None:
    """Live: CA Sierra Nevada FBFM40 fetch returns a real GeoTIFF with class codes.

    Geographic-correctness gate (codified lesson job-0086): rasterio reads
    the returned TIFF and asserts the band-1 pixel values contain FBFM40
    fuel-model class codes (not all-nodata, not all-zero).
    Writes ``evidence/landfire_live.txt`` for the auditor.
    """
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)

    with patch(
        "grace2_agent.tools.fetch_landfire_fuels.read_through",
        side_effect=patched_rt,
    ):
        layer_uri = fetch_landfire_fuels(bbox=_CA_SIERRA_BBOX, layer="fbfm40")

    assert layer_uri.uri is not None
    blob_path = layer_uri.uri.split("/", 3)[3]
    tiff = fake_gcs.store[blob_path]
    # TIFF magic
    assert tiff[:4] in (b"II*\x00", b"MM\x00*"), (
        f"Expected TIFF magic; got {tiff[:8]!r}"
    )
    assert len(tiff) > 1024, f"TIFF too small: {len(tiff)} bytes"

    # Read with rasterio + check fuel-model class distribution.
    import rasterio
    from rasterio.io import MemoryFile

    with MemoryFile(tiff) as mem:
        with mem.open() as src:
            arr = src.read(1)
            unique_vals = sorted(set(arr.ravel().tolist()))[:20]
            width = src.width
            height = src.height
            crs = src.crs
            bounds = src.bounds

    # Geographic-correctness: the raster covers the requested bbox.
    # ImageServer returns the bbox we asked for (in imageSR=4326).
    assert bounds.left >= _CA_SIERRA_BBOX[0] - 0.5
    assert bounds.right <= _CA_SIERRA_BBOX[2] + 0.5
    assert bounds.bottom >= _CA_SIERRA_BBOX[1] - 0.5
    assert bounds.top <= _CA_SIERRA_BBOX[3] + 0.5

    # FBFM40 codes are 91-204 + nodata; we expect at least one valid code
    # (the CA Sierra Nevada is well-vegetated, no chance of all-nodata).
    valid_codes = [v for v in unique_vals if v >= 90 and v <= 210]
    assert len(valid_codes) > 0, (
        f"Expected at least one FBFM40 code in [90,210]; got unique vals: {unique_vals}"
    )

    # Evidence file
    os.makedirs("evidence", exist_ok=True)
    with open("evidence/landfire_live.txt", "w") as f:
        f.write(
            f"job-0111 fetch_landfire_fuels LIVE test\n"
            f"timestamp: {datetime.now(timezone.utc).isoformat()}\n"
            f"layer: fbfm40\n"
            f"bbox: {_CA_SIERRA_BBOX}\n"
            f"tiff bytes: {len(tiff)}\n"
            f"width x height: {width} x {height}\n"
            f"crs: {crs}\n"
            f"bounds: {bounds}\n"
            f"unique pixel values (first 20): {unique_vals}\n"
            f"valid FBFM40 codes detected: {valid_codes}\n"
            f"LayerURI.uri: {layer_uri.uri}\n"
            f"LayerURI.layer_id: {layer_uri.layer_id}\n"
            f"LayerURI.name: {layer_uri.name}\n"
            f"LayerURI.style_preset: {layer_uri.style_preset}\n"
        )


@pytest.mark.skipif(
    not _LIVE_LANDFIRE,
    reason="set GRACE2_TEST_LIVE_LANDFIRE=1 to enable real LANDFIRE fetches",
)
def test_live_layer_options_distinguish() -> None:
    """Live: fbfm13 vs fbfm40 produce distinct cached blobs (different cache keys).

    Verifies the kickoff acceptance: "layer='fbfm13' vs 'fbfm40' produce
    different cache keys".
    """
    fake_gcs = FakeStorageClient()
    patched_rt = _make_read_through_injector(fake_gcs)

    with patch(
        "grace2_agent.tools.fetch_landfire_fuels.read_through",
        side_effect=patched_rt,
    ):
        u40 = fetch_landfire_fuels(bbox=_CA_SIERRA_BBOX, layer="fbfm40")
        u13 = fetch_landfire_fuels(bbox=_CA_SIERRA_BBOX, layer="fbfm13")

    assert u40.uri != u13.uri
    # And the cache keys (the hex suffix between the source-class prefix
    # and the .tif extension) must differ.
    key40 = u40.uri.rsplit("/", 1)[-1].split(".")[0]
    key13 = u13.uri.rsplit("/", 1)[-1].split(".")[0]
    assert key40 != key13
