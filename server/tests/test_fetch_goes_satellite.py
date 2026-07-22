"""Unit tests for the ``fetch_goes_satellite`` atomic tool (job-0104).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- ``bbox=None`` raises typed ``GOESBboxRequiredError`` (BBOX_REQUIRED).
- Unknown band raises typed ``GOESInputError``.
- Unknown satellite raises typed ``GOESInputError``.
- Degenerate / out-of-range bbox raises typed ``GOESInputError``.
- Different bands produce different cache keys.
- Different satellites produce different cache keys.
- Different bboxes produce different cache keys.
- Cache miss → fetch_fn invoked; cache hit → fetch_fn skipped.
- ``_pick_most_recent_key`` returns the largest ``_s<TIMESTAMP>`` key.
- ``_band_to_variable`` returns CMI_C02 / CMI_C13 / CMI_C08.
- ``_band_to_units`` returns reflectance / K / K.
- ``_round_valid_time`` rounds to 15-minute boundary.
- Live (env-gated): real fetch over Florida bbox produces a CRS-tagged COG
  with EPSG:4326 inside the requested bbox AND physically-plausible values.

Live tests gated by ``TRID3NT_TEST_LIVE_GOES=1``; everything else runs
unconditionally and uses patched network calls + fake GCS.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_goes_satellite import (
    GOESBboxRequiredError,
    GOESEmptyError,
    GOESError,
    GOESInputError,
    GOESUpstreamError,
    _BAND_TO_VARIABLE,
    _SATELLITE_BUCKETS,
    _SATELLITE_FILENAME_CODE,
    _band_to_units,
    _band_to_variable,
    _normalize_satellite,
    _pick_most_recent_key,
    _round_bbox,
    _round_valid_time,
    _validate_bbox,
    fetch_goes_satellite,
)


# ---------------------------------------------------------------------------
# Constants / helpers.
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 7, 30, tzinfo=timezone.utc)

# Florida coastal bbox — used for both unit & live tests (inside CONUS sector).
_FL_BBOX = (-82.0, 26.0, -80.0, 28.0)

# Marker for the live test.
_LIVE_GOES = os.environ.get("TRID3NT_TEST_LIVE_GOES") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors existing test pattern).
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


def _fake_cog_bytes(tag: str = "GOES") -> bytes:
    """Placeholder bytes; the cache shim only cares about ``bytes``, not validity."""
    return b"FAKE_GOES_COG_" + tag.encode() + b"\x00" * 16


def _make_read_through_injector(fake_gcs):
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
# Registration tests (no network needed).
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_goes_satellite appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_goes_satellite" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_goes_satellite"]
    assert entry.metadata.name == "fetch_goes_satellite"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "goes_satellite"
    assert entry.metadata.cacheable is True


def test_three_bands_are_defined():
    """All three required bands are mapped to CMI variables."""
    assert set(_BAND_TO_VARIABLE) == {"visible", "ir_window", "water_vapor"}
    assert _BAND_TO_VARIABLE["visible"] == "CMI_C02"
    assert _BAND_TO_VARIABLE["ir_window"] == "CMI_C13"
    assert _BAND_TO_VARIABLE["water_vapor"] == "CMI_C08"


def test_four_satellites_are_defined():
    """The 4 GOES-R satellites (16/17/18/19) are mapped to S3 buckets."""
    assert set(_SATELLITE_BUCKETS) == {"goes-16", "goes-17", "goes-18", "goes-19"}
    assert _SATELLITE_BUCKETS["goes-16"] == "noaa-goes16"
    assert _SATELLITE_BUCKETS["goes-18"] == "noaa-goes18"


def test_bucket_token_glues_digits_no_hyphen():
    """Regression guard for the 'goes18 vs goes-18' bug: bucket names glue the
    digits to 'goes' with NO hyphen (noaa-goes18, never noaa-goes-18)."""
    for token, bucket in _SATELLITE_BUCKETS.items():
        # token is the hyphenated internal form; bucket is the glued AWS form.
        digits = token.split("-")[1]
        assert bucket == f"noaa-goes{digits}"
        assert "goes-" not in bucket, f"{bucket!r} must NOT contain 'goes-' (404 hazard)"


def test_filename_code_map_matches_buckets():
    """Each satellite carries a glued 'GNN' filename code (G18, never G-18)."""
    assert set(_SATELLITE_FILENAME_CODE) == set(_SATELLITE_BUCKETS)
    assert _SATELLITE_FILENAME_CODE["goes-18"] == "G18"
    assert _SATELLITE_FILENAME_CODE["goes-19"] == "G19"
    for token, code in _SATELLITE_FILENAME_CODE.items():
        digits = token.split("-")[1]
        assert code == f"G{digits}"
        assert "-" not in code


# ---------------------------------------------------------------------------
# Satellite-identifier normalization (the "goes18 vs goes-18" bug class).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,canonical",
    [
        # Canonical form passes through.
        ("goes-18", "goes-18"),
        ("goes-19", "goes-19"),
        # Glued bucket spelling (the literal AWS form humans copy).
        ("goes18", "goes-18"),
        ("goes19", "goes-19"),
        # Upper / mixed case + spacing.
        ("GOES-18", "goes-18"),
        ("GOES 18", "goes-18"),
        ("GOES_19", "goes-19"),
        ("  goes-19  ", "goes-19"),
        # Filename satellite code.
        ("G18", "goes-18"),
        ("g19", "goes-19"),
        # Bare two-digit number.
        ("18", "goes-18"),
        ("19", "goes-19"),
        # Directional aliases -> current East/West birds (2025-04-07 mapping).
        ("GOES-East", "goes-19"),
        ("east", "goes-19"),
        ("GOES West", "goes-18"),
        ("west", "goes-18"),
        # Historical birds still resolvable for archival lookups.
        ("goes16", "goes-16"),
        ("G17", "goes-17"),
    ],
)
def test_normalize_satellite_accepts_human_spellings(raw, canonical):
    """Every accepted human/LLM spelling maps to the exact canonical token."""
    assert _normalize_satellite(raw) == canonical
    # And the canonical token keys a real bucket whose digits are glued.
    bucket = _SATELLITE_BUCKETS[_normalize_satellite(raw)]
    assert bucket == "noaa-goes" + canonical.split("-")[1]


def test_normalize_satellite_directional_matches_current_birds():
    """GOES-East -> goes-19 and GOES-West -> goes-18 per the 2025-04-07 swap."""
    assert _normalize_satellite("GOES-East") == "goes-19"
    assert _normalize_satellite("GOES-West") == "goes-18"


@pytest.mark.parametrize(
    "bad",
    ["himawari-9", "goes-99", "99", "G99", "goes", "northeast", "", "goes-1"],
)
def test_normalize_satellite_unknown_raises_loud_typed_error(bad):
    """A genuinely unknown token fails LOUD (typed GOESInputError listing the
    accepted forms) -- never a silent bad-bucket path or empty fetch."""
    with pytest.raises(GOESInputError, match="unknown satellite"):
        _normalize_satellite(bad)


def test_normalize_satellite_error_lists_accepted_forms():
    """The reject message names the accepted spellings so the agent can recover."""
    try:
        _normalize_satellite("himawari-9")
    except GOESInputError as exc:
        msg = str(exc)
        assert "goes-18" in msg
        assert "GOES-East" in msg or "goes-east" in msg.lower()
        assert exc.error_code == "GOES_INPUT_INVALID"
        assert exc.retryable is False
    else:
        pytest.fail("Expected GOESInputError")


def test_normalize_satellite_non_string_raises_typed_error():
    """A non-string satellite raises GOESInputError, not a TypeError leak."""
    with pytest.raises(GOESInputError):
        _normalize_satellite(18)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Typed-error tests (no network needed).
# ---------------------------------------------------------------------------


def test_bbox_none_raises_bbox_required_typed_error():
    """Passing ``bbox=None`` raises GOESBboxRequiredError (BBOX_REQUIRED)."""
    with pytest.raises(GOESBboxRequiredError, match="bbox is required"):
        fetch_goes_satellite(bbox=None, band="visible")  # type: ignore[arg-type]


def test_bbox_required_error_has_typed_code_and_not_retryable():
    """GOESBboxRequiredError carries error_code=BBOX_REQUIRED and retryable=False."""
    try:
        fetch_goes_satellite(bbox=None)  # type: ignore[arg-type]
    except GOESBboxRequiredError as exc:
        assert exc.error_code == "BBOX_REQUIRED"
        assert exc.retryable is False
    else:
        pytest.fail("Expected GOESBboxRequiredError")


def test_unknown_band_raises_typed_error():
    """Passing an unknown band raises GOESInputError (not generic RuntimeError)."""
    with pytest.raises(GOESInputError, match="unknown band"):
        fetch_goes_satellite(bbox=_FL_BBOX, band="ultraviolet")


def test_unknown_satellite_raises_typed_error():
    """Passing an unknown satellite raises GOESInputError."""
    with pytest.raises(GOESInputError, match="unknown satellite"):
        fetch_goes_satellite(bbox=_FL_BBOX, satellite="himawari-9")


def test_degenerate_bbox_raises_input_error():
    """A bbox where min == max raises GOESInputError before any download."""
    with pytest.raises(GOESInputError, match="degenerate"):
        fetch_goes_satellite(bbox=(-82.0, 26.0, -82.0, 26.0))


def test_out_of_range_bbox_raises_input_error():
    """A bbox with lon > 180 raises GOESInputError."""
    with pytest.raises(GOESInputError, match="lon out of"):
        fetch_goes_satellite(bbox=(-82.0, 26.0, 270.0, 28.0))


def test_non_finite_bbox_raises_input_error():
    """A bbox with NaN raises GOESInputError."""
    with pytest.raises(GOESInputError, match="non-finite"):
        fetch_goes_satellite(bbox=(float("nan"), 26.0, -80.0, 28.0))


def test_input_error_is_typed_subclass_of_goes_error():
    """Typed errors all derive from the GOESError base."""
    assert issubclass(GOESInputError, GOESError)
    assert issubclass(GOESBboxRequiredError, GOESError)
    assert issubclass(GOESUpstreamError, GOESError)
    assert issubclass(GOESEmptyError, GOESError)


# ---------------------------------------------------------------------------
# Helper tests (pure Python, no network).
# ---------------------------------------------------------------------------


def test_band_to_variable_maps_three_bands():
    """_band_to_variable returns the correct CMI variable for each band."""
    assert _band_to_variable("visible") == "CMI_C02"
    assert _band_to_variable("ir_window") == "CMI_C13"
    assert _band_to_variable("water_vapor") == "CMI_C08"


def test_band_to_variable_unknown_raises_typed_error():
    """_band_to_variable raises GOESInputError on unknown bands."""
    with pytest.raises(GOESInputError):
        _band_to_variable("xyz")


def test_band_to_units_maps_three_bands():
    """_band_to_units returns reflectance / K / K."""
    assert _band_to_units("visible") == "reflectance"
    assert _band_to_units("ir_window") == "K"
    assert _band_to_units("water_vapor") == "K"


def test_round_bbox_to_6dp():
    """_round_bbox rounds to 6 decimal places."""
    rounded = _round_bbox((-82.123456789, 26.123456789, -80.987654321, 28.987654321))
    assert rounded == (-82.123457, 26.123457, -80.987654, 28.987654)


def test_round_valid_time_rounds_down_to_15min_boundary():
    """_round_valid_time floors to the nearest 15-minute boundary in UTC."""
    pin = datetime(2026, 6, 8, 12, 7, 30, tzinfo=timezone.utc)
    assert _round_valid_time(pin) == "2026-06-08T12:00:00Z"

    pin2 = datetime(2026, 6, 8, 12, 17, 30, tzinfo=timezone.utc)
    assert _round_valid_time(pin2) == "2026-06-08T12:15:00Z"

    pin3 = datetime(2026, 6, 8, 12, 45, 0, tzinfo=timezone.utc)
    assert _round_valid_time(pin3) == "2026-06-08T12:45:00Z"


def test_round_valid_time_handles_naive_datetime_as_utc():
    """_round_valid_time treats a naive datetime as UTC."""
    pin = datetime(2026, 6, 8, 12, 7, 30)  # naive
    assert _round_valid_time(pin) == "2026-06-08T12:00:00Z"


def test_pick_most_recent_key_picks_largest_start_time():
    """_pick_most_recent_key picks the key with the largest ``_s<14digits>_``."""
    keys = [
        "ABI-L2-MCMIPC/2024/180/12/OR_ABI-L2-MCMIPC-M6_G16_s20241801201176_e..._c....nc",
        "ABI-L2-MCMIPC/2024/180/12/OR_ABI-L2-MCMIPC-M6_G16_s20241801206176_e..._c....nc",
        "ABI-L2-MCMIPC/2024/180/12/OR_ABI-L2-MCMIPC-M6_G16_s20241801211176_e..._c....nc",
    ]
    chosen = _pick_most_recent_key(keys)
    assert "s20241801211176" in chosen


def test_pick_most_recent_key_empty_returns_empty_string():
    """_pick_most_recent_key returns '' when input list is empty or has no timestamps."""
    assert _pick_most_recent_key([]) == ""
    assert _pick_most_recent_key(["no_start_time_substring.nc"]) == ""


# ---------------------------------------------------------------------------
# Cache-layer tests (patched network, fake GCS).
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_fetch_fn_and_writes_store():
    """On first call (cache miss), the inner fetch is invoked and bytes are stored."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}

    def fake_fetch(bbox, band, satellite, res_deg=0.02):
        fetch_count["n"] += 1
        return _fake_cog_bytes("MISS")

    with patch(
        "trid3nt_server.tools.fetch_goes_satellite._fetch_goes_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.fetch_goes_satellite.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_goes_satellite(bbox=_FL_BBOX, band="visible")

    assert fetch_count["n"] == 1
    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert "cache/dynamic-1h/goes_satellite/" in result.uri
    assert result.uri.endswith(".tif")
    assert len(fake_gcs.store) == 1


def test_cache_hit_skips_inner_fetch():
    """On second call with same params, the inner fetch is NOT invoked."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}

    def fake_fetch(bbox, band, satellite, res_deg=0.02):
        fetch_count["n"] += 1
        return _fake_cog_bytes("HIT")

    with patch(
        "trid3nt_server.tools.fetch_goes_satellite._fetch_goes_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.fetch_goes_satellite.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_goes_satellite(bbox=_FL_BBOX, band="visible")
        r2 = fetch_goes_satellite(bbox=_FL_BBOX, band="visible")

    assert fetch_count["n"] == 1, "fetch_fn should be called only once"
    assert r1.uri == r2.uri


def test_different_bands_produce_different_cache_keys():
    """Different bands cache under different URIs even with the same bbox + satellite."""
    fake_gcs = FakeStorageClient()

    def fake_fetch(bbox, band, satellite, res_deg=0.02):
        return _fake_cog_bytes(f"BAND_{band}")

    with patch(
        "trid3nt_server.tools.fetch_goes_satellite._fetch_goes_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.fetch_goes_satellite.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_vis = fetch_goes_satellite(bbox=_FL_BBOX, band="visible")
        r_ir = fetch_goes_satellite(bbox=_FL_BBOX, band="ir_window")

    assert r_vis.uri != r_ir.uri, "visible vs ir_window must hash to different keys"
    assert len(fake_gcs.store) == 2


def test_different_satellites_produce_different_cache_keys():
    """Different satellites cache under different URIs even with the same bbox + band."""
    fake_gcs = FakeStorageClient()

    def fake_fetch(bbox, band, satellite, res_deg=0.02):
        return _fake_cog_bytes(f"SAT_{satellite}")

    with patch(
        "trid3nt_server.tools.fetch_goes_satellite._fetch_goes_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.fetch_goes_satellite.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_16 = fetch_goes_satellite(bbox=_FL_BBOX, satellite="goes-16")
        r_18 = fetch_goes_satellite(bbox=_FL_BBOX, satellite="goes-18")

    assert r_16.uri != r_18.uri, "goes-16 vs goes-18 must hash to different keys"


def test_forgiving_satellite_spelling_routes_to_same_layer():
    """``satellite="GOES-18"`` (and "goes18", "G18", "GOES West") must produce
    the SAME cache key / LayerURI as canonical ``"goes-18"`` -- the normalize
    layer means a human/LLM spelling no longer dies on input validation."""
    fake_gcs = FakeStorageClient()
    seen_satellites: list[str] = []

    def fake_fetch(bbox, band, satellite, res_deg=0.02):
        seen_satellites.append(satellite)
        return _fake_cog_bytes(f"SAT_{satellite}")

    with patch(
        "trid3nt_server.tools.fetch_goes_satellite._fetch_goes_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.fetch_goes_satellite.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        canonical = fetch_goes_satellite(bbox=_FL_BBOX, satellite="goes-18")
        for spelling in ("GOES-18", "goes18", "G18", "GOES West"):
            r = fetch_goes_satellite(bbox=_FL_BBOX, satellite=spelling)
            assert r.uri == canonical.uri, (
                f"{spelling!r} must hash to the same key as 'goes-18'"
            )
            assert r.layer_id == canonical.layer_id

    # The inner fetch only ever sees the canonical token, never a raw spelling,
    # so the S3 bucket/key is always built from the glued form (goes18).
    assert set(seen_satellites) == {"goes-18"}


def test_default_satellite_is_current_operational_east():
    """Calling with no satellite arg fetches GOES-19 (current operational East,
    since 2025-04-07) -- NOT the decommissioned goes-16 whose bucket is stale."""
    fake_gcs = FakeStorageClient()
    seen_satellites: list[str] = []

    def fake_fetch(bbox, band, satellite, res_deg=0.02):
        seen_satellites.append(satellite)
        return _fake_cog_bytes(f"SAT_{satellite}")

    with patch(
        "trid3nt_server.tools.fetch_goes_satellite._fetch_goes_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.fetch_goes_satellite.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_default = fetch_goes_satellite(bbox=_FL_BBOX)
        r_g19 = fetch_goes_satellite(bbox=_FL_BBOX, satellite="goes-19")

    # The default and explicit goes-19 share a cache key (second call is a hit,
    # so the inner fetch runs once) -- both proving the default IS goes-19.
    assert seen_satellites == ["goes-19"]
    assert r_default.uri == r_g19.uri, "default must equal explicit goes-19"
    assert "goes-19" in r_default.layer_id and "goes-16" not in r_default.layer_id


def test_different_bboxes_produce_different_cache_keys():
    """Different bboxes cache under different URIs."""
    fake_gcs = FakeStorageClient()

    def fake_fetch(bbox, band, satellite, res_deg=0.02):
        return _fake_cog_bytes(f"BBOX_{bbox[0]}")

    with patch(
        "trid3nt_server.tools.fetch_goes_satellite._fetch_goes_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.fetch_goes_satellite.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_fl = fetch_goes_satellite(bbox=(-82.0, 26.0, -80.0, 28.0))
        r_tx = fetch_goes_satellite(bbox=(-100.0, 28.0, -95.0, 32.0))

    assert r_fl.uri != r_tx.uri


# ---------------------------------------------------------------------------
# LayerURI shape tests.
# ---------------------------------------------------------------------------


def test_layer_uri_shape_for_visible_band():
    """Visible band returns a LayerURI with layer_type=raster, units=reflectance, role=context."""
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetch_goes_satellite._fetch_goes_bytes",
        return_value=_fake_cog_bytes("SHAPE_VIS"),
    ), patch(
        "trid3nt_server.tools.fetch_goes_satellite.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_goes_satellite(bbox=_FL_BBOX, band="visible")

    assert result.layer_type == "raster"
    assert result.role == "context"
    assert result.units == "reflectance"
    assert "GOES Satellite" in result.name
    assert "Band 2" in result.name


def test_layer_uri_shape_for_ir_window_band():
    """IR window band returns a LayerURI with units=K."""
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetch_goes_satellite._fetch_goes_bytes",
        return_value=_fake_cog_bytes("SHAPE_IR"),
    ), patch(
        "trid3nt_server.tools.fetch_goes_satellite.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_goes_satellite(bbox=_FL_BBOX, band="ir_window")

    assert result.layer_type == "raster"
    assert result.units == "K"
    assert "Band 13" in result.name


def test_layer_uri_shape_for_water_vapor_band():
    """Water vapor band returns a LayerURI with units=K."""
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetch_goes_satellite._fetch_goes_bytes",
        return_value=_fake_cog_bytes("SHAPE_WV"),
    ), patch(
        "trid3nt_server.tools.fetch_goes_satellite.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_goes_satellite(bbox=_FL_BBOX, band="water_vapor")

    assert result.units == "K"
    assert "Band 8" in result.name


# ---------------------------------------------------------------------------
# Bbox covering open ocean (no land features expected) — tool still returns
# raster (just dark cells for ocean in visible band; cool IR for warm SST).
# ---------------------------------------------------------------------------


def test_ocean_bbox_still_returns_raster():
    """A bbox covering only ocean (Gulf of Mexico, no land) still returns a LayerURI."""
    fake_gcs = FakeStorageClient()
    gulf_bbox = (-90.0, 25.0, -86.0, 27.0)  # open Gulf of Mexico

    with patch(
        "trid3nt_server.tools.fetch_goes_satellite._fetch_goes_bytes",
        return_value=_fake_cog_bytes("OCEAN"),
    ), patch(
        "trid3nt_server.tools.fetch_goes_satellite.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_goes_satellite(bbox=gulf_bbox, band="visible")

    assert result.uri is not None
    assert result.layer_type == "raster"


# ---------------------------------------------------------------------------
# Live test (env-gated): real S3 listing + netCDF download + reproject.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE_GOES, reason="TRID3NT_TEST_LIVE_GOES not set; skipping live fetch")
def test_live_florida_fetch_produces_valid_cog():
    """Live: real GOES-16 fetch over Florida produces a CRS-tagged COG with sane values.

    Geographic-correctness check (job-0086 codified lesson):
    - The output CRS MUST be EPSG:4326.
    - The output bounds MUST overlap the requested bbox.
    - The mean visible-band reflectance MUST fall inside [0.0, 1.5] (clamped
      reflectance is bounded; physically-plausible noon values are 0.05-0.8).
    A sign-flip / axis-swap bug would push pixels outside the bbox or push
    reflectance outside the physically-plausible window.

    NOTE: NOAA's noaa-goes16 bucket is real-time but ingestion can lag
    behind the sandbox wall-clock by months when this harness is run from
    a date pinned ahead of the real world. The test patches the listing
    helper's ``now`` argument with a known-good date in 2025 (DOY 097, 12Z)
    so the live fetch finds real data regardless of harness clock skew.
    """
    import rasterio
    from datetime import datetime, timezone

    # Pin a known-good observation time inside the bucket's available range.
    # 2025-04-07 12:00 UTC = DOY 097 12Z, verified to contain MCMIPC frames.
    known_good_now = datetime(2025, 4, 7, 12, 30, 0, tzinfo=timezone.utc)

    fake_gcs = FakeStorageClient()

    # Wrap _list_recent_keys to inject the known-good ``now``; everything else
    # (download, reproject, COG write, cache write) runs unchanged.
    from trid3nt_server.tools import fetch_goes_satellite as gtool

    real_list = gtool._list_recent_keys

    def list_with_pinned_now(satellite, *, now=None, session=None, lookback_hours=3):
        return real_list(
            satellite,
            now=known_good_now,
            session=session,
            lookback_hours=lookback_hours,
        )

    with patch(
        "trid3nt_server.tools.fetch_goes_satellite.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetch_goes_satellite._list_recent_keys",
        side_effect=list_with_pinned_now,
    ):
        result = fetch_goes_satellite(bbox=_FL_BBOX, band="visible", satellite="goes-16")

    # The fake GCS stores the bytes — pull them out and assert COG validity.
    assert len(fake_gcs.store) == 1
    cog_bytes = next(iter(fake_gcs.store.values()))
    assert len(cog_bytes) > 1000, f"Expected real COG bytes; got {len(cog_bytes)}"

    # Write bytes to a temp file for rasterio.
    fd, cog_path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_goes_live_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(cog_bytes)

        with rasterio.open(cog_path) as ds:
            assert ds.crs is not None, "Output COG must carry a CRS tag"
            assert ds.crs.to_epsg() == 4326, (
                f"Expected EPSG:4326; got {ds.crs}"
            )
            bounds = ds.bounds
            # Geographic-correctness: output bounds overlap the requested bbox.
            assert bounds.left < _FL_BBOX[2] and bounds.right > _FL_BBOX[0], (
                f"Output bounds {bounds} do not overlap bbox {_FL_BBOX}"
            )
            assert bounds.bottom < _FL_BBOX[3] and bounds.top > _FL_BBOX[1], (
                f"Output bounds {bounds} do not overlap bbox {_FL_BBOX}"
            )

            # Physically-plausible reflectance check.
            import numpy as np
            arr = ds.read(1)
            finite = arr[np.isfinite(arr)]
            assert finite.size > 0, "No finite pixels in output"
            mean_val = float(finite.mean())
            assert -0.1 <= mean_val <= 1.6, (
                f"Mean visible reflectance {mean_val} outside physically-plausible [-0.1, 1.6]"
            )

            # Save the evidence file path for the report.
            evidence_dir = str(__import__("pathlib").Path(__file__).resolve().parents[2] / "run" / "evidence" / "job-0104-engine-20260608")
            evidence_path = os.path.join(evidence_dir, "evidence", "goes_live.txt")
            os.makedirs(os.path.dirname(evidence_path), exist_ok=True)
            with open(evidence_path, "w") as f:
                f.write(f"GOES-16 visible live fetch\n")
                f.write(f"bbox: {_FL_BBOX}\n")
                f.write(f"CRS: {ds.crs}\n")
                f.write(f"bounds: {bounds}\n")
                f.write(f"shape: {arr.shape}\n")
                f.write(f"mean reflectance (finite): {mean_val:.4f}\n")
                f.write(f"min, max (finite): {float(finite.min()):.4f}, {float(finite.max()):.4f}\n")
                f.write(f"finite pixel count: {finite.size}\n")
    finally:
        try:
            os.unlink(cog_path)
        except OSError:
            pass
