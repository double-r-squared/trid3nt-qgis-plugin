"""Unit tests for the ``fetch_ebird_observations`` atomic tool (job-0128).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Validation: bad bbox / bad days_back / bad species_code raise typed errors.
- Key resolution priority: explicit api_key > secret_ref > env var; missing
  key raises EBirdMissingKeyError BEFORE any network call.
- Mocked happy path: a single-tile bbox + 200-status response → FlatGeobuf
  with the same feature count.
- Multi-tile bbox: tile cover produces multiple HTTP calls; overlapping
  records dedupe by subId.
- Empty response: returns an empty FlatGeobuf without error.
- 401/403 → EBirdAuthError (not retryable).
- 5xx → EBirdUpstreamError (retryable).
- 404 → EBirdInputError (not retryable, bad species code).
- Geographic correctness (job-0086 codified lesson): records outside the
  requested bbox are filtered.
- LayerURI shape: layer_type, role, style_preset, units verified.
- Cache hit: second identical call reuses cached FlatGeobuf.

Live tests (gated by ``TRID3NT_TEST_LIVE_EBIRD=1`` + ``TRID3NT_EBIRD_API_KEY``):
- ``bewwre`` (Bewick's Wren) over CA bbox → ≥0 features; evidence captured.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations import (
    EBirdAuthError,
    EBirdError,
    EBirdInputError,
    EBirdMissingKeyError,
    EBirdUpstreamError,
    _bbox_to_tile_centers,
    _records_to_flatgeobuf_bytes,
    _resolve_api_key,
    _round_bbox_to_6dp,
    _validate_bbox,
    _validate_days_back,
    _validate_species_code,
    fetch_ebird_observations,
    set_persistence_for_secrets,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Small bbox = 1 tile (~0.4 degree square at mid-lat ≈ 44km N-S × 35km E-W,
# well under the 50km tile radius so a single tile covers it).
_SMALL_BBOX = (-122.4, 38.0, -122.0, 38.4)

# Bigger bbox spanning ~3 degrees E-W × 2 degrees N-S — guaranteed to need
# multiple tile centers.
_MULTI_TILE_BBOX = (-122.0, 38.0, -119.0, 40.0)

# Live test gates.
_LIVE_EBIRD = os.environ.get("TRID3NT_TEST_LIVE_EBIRD") == "1"
_LIVE_EBIRD_KEY = os.environ.get("TRID3NT_EBIRD_API_KEY")

# Bewick's Wren — common in California per audit.md.
_BEWICK_WREN_CODE = "bewwre"

# Test API key (mock-only; never sent to real eBird).
_MOCK_API_KEY = "test-mock-key-abc123"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors test_fetch_gbif_occurrences pattern).
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


def _make_obs_record(
    *,
    sub_id: str,
    lat: float,
    lng: float,
    com_name: str = "Bewick's Wren",
    sci_name: str = "Thryomanes bewickii",
    species_code: str = "bewwre",
    obs_dt: str = "2026-06-01 08:30",
    loc_name: str = "Lake Sonoma",
    how_many: int | str | None = 2,
) -> dict[str, Any]:
    """Build an eBird-shaped observation record."""
    rec: dict[str, Any] = {
        "subId": sub_id,
        "lat": lat,
        "lng": lng,
        "comName": com_name,
        "sciName": sci_name,
        "speciesCode": species_code,
        "obsDt": obs_dt,
        "locName": loc_name,
    }
    if how_many is not None:
        rec["howMany"] = how_many
    return rec


class _FakeHTTPResponse:
    """Minimal httpx.Response-like object for patching."""

    def __init__(
        self,
        status_code: int,
        payload: list[dict[str, Any]] | None = None,
        text: str = "",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> list[dict[str, Any]]:
        if self._payload is None:
            raise ValueError("no JSON payload")
        return self._payload


class _MockHTTPClient:
    """Context-manager-aware fake ``httpx.Client``.

    The real tool builds its own ``httpx.Client`` inside ``_fetch_all_tiles``
    using ``with httpx.Client(...) as client``. We patch ``httpx.Client`` so
    every constructor returns this same instance; ``__enter__``/``__exit__``
    delegate to self so the ``with`` block works.
    """

    def __init__(self, responses: list[_FakeHTTPResponse]) -> None:
        self._responses = list(responses)
        self.get_calls: list[dict[str, Any]] = []

    def __enter__(self) -> _MockHTTPClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeHTTPResponse:
        self.get_calls.append({"url": url, "params": params, "headers": headers})
        if not self._responses:
            raise RuntimeError("no mock responses left")
        return self._responses.pop(0)


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_ebird_observations appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_ebird_observations" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_ebird_observations"]
    assert entry.metadata.name == "fetch_ebird_observations"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "ebird"
    assert entry.metadata.cacheable is True
    assert entry.metadata.supports_global_query is False


# ---------------------------------------------------------------------------
# Validation / typed-error tests.
# ---------------------------------------------------------------------------


def test_degenerate_bbox_raises_input_error():
    with pytest.raises(EBirdInputError):
        _validate_bbox((-122.0, 38.0, -122.0, 38.0))


def test_lon_out_of_range_raises_input_error():
    with pytest.raises(EBirdInputError):
        _validate_bbox((-181.0, 38.0, -120.0, 39.0))


def test_lat_out_of_range_raises_input_error():
    with pytest.raises(EBirdInputError):
        _validate_bbox((-122.0, 38.0, -121.0, 91.0))


def test_days_back_zero_raises_input_error():
    with pytest.raises(EBirdInputError):
        _validate_days_back(0)


def test_days_back_over_30_raises_input_error():
    with pytest.raises(EBirdInputError):
        _validate_days_back(45)


def test_days_back_non_int_raises_input_error():
    with pytest.raises(EBirdInputError):
        _validate_days_back("30")  # type: ignore[arg-type]


def test_species_code_empty_raises_input_error():
    with pytest.raises(EBirdInputError):
        _validate_species_code("")


def test_species_code_too_long_raises_input_error():
    with pytest.raises(EBirdInputError):
        _validate_species_code("a" * 100)


def test_species_code_non_alphanumeric_raises_input_error():
    with pytest.raises(EBirdInputError):
        _validate_species_code("bew-wre")


def test_input_error_is_not_retryable():
    """EBirdInputError carries retryable=False."""
    err = EBirdInputError("bad")
    assert err.retryable is False


def test_upstream_error_is_retryable():
    err = EBirdUpstreamError("5xx")
    assert err.retryable is True


def test_auth_error_is_not_retryable():
    err = EBirdAuthError("401")
    assert err.retryable is False


def test_missing_key_error_is_not_retryable():
    err = EBirdMissingKeyError("no key")
    assert err.retryable is False


# ---------------------------------------------------------------------------
# Helper tests.
# ---------------------------------------------------------------------------


def test_round_bbox_to_6dp():
    raw = (-122.123456789, 38.123456789, -121.987654321, 39.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-122.123457, 38.123457, -121.987654, 39.987654)


def test_bbox_to_tile_centers_small_bbox_single_tile():
    """A ~50km bbox produces a single tile center at the bbox center."""
    centers = _bbox_to_tile_centers(_SMALL_BBOX)
    # The small bbox is ~55km E-W × 55km N-S at lat ~38, so 1 row × 1 col.
    assert len(centers) == 1
    lat, lng = centers[0]
    assert _SMALL_BBOX[0] < lng < _SMALL_BBOX[2]
    assert _SMALL_BBOX[1] < lat < _SMALL_BBOX[3]


def test_bbox_to_tile_centers_multi_tile_bbox():
    """A wider bbox produces a multi-tile cover."""
    centers = _bbox_to_tile_centers(_MULTI_TILE_BBOX)
    assert len(centers) > 1
    # All centers must lie within the bbox.
    for lat, lng in centers:
        assert _MULTI_TILE_BBOX[0] <= lng <= _MULTI_TILE_BBOX[2]
        assert _MULTI_TILE_BBOX[1] <= lat <= _MULTI_TILE_BBOX[3]


def test_bbox_to_tile_centers_huge_bbox_raises():
    """A continent-scale bbox blows the hard cap → EBirdInputError."""
    huge = (-160.0, -50.0, 160.0, 50.0)  # 320 deg × 100 deg
    with pytest.raises(EBirdInputError, match="tile cover would require"):
        _bbox_to_tile_centers(huge)


# ---------------------------------------------------------------------------
# Key-resolution tests (FR-AS-11 + §F.3).
# ---------------------------------------------------------------------------


def test_resolve_api_key_explicit_kwarg_wins():
    """An explicit ``api_key`` kwarg short-circuits all other paths."""
    # Set the env var to a different value; the kwarg should still win.
    with patch.dict(os.environ, {"TRID3NT_EBIRD_API_KEY": "env-value"}):
        out = _resolve_api_key(api_key="explicit-value", secret_ref=None)
    assert out == "explicit-value"


def test_resolve_api_key_env_var_fallback():
    """With no kwarg and no secret_ref, env var wins."""
    with patch.dict(os.environ, {"TRID3NT_EBIRD_API_KEY": "env-value"}):
        out = _resolve_api_key(api_key=None, secret_ref=None)
    assert out == "env-value"


def test_resolve_api_key_secret_ref_str_shortcut():
    """The test-only str shortcut on secret_ref delivers the value verbatim."""
    with patch.dict(os.environ, {}, clear=True):
        # Save & restore the TRID3NT_EBIRD_API_KEY if present in the live env.
        # patch.dict(clear=True) removed it for this test.
        out = _resolve_api_key(api_key=None, secret_ref="secret-direct-value")
    assert out == "secret-direct-value"


def test_resolve_api_key_no_path_raises_missing_key():
    """No kwarg, no secret_ref, no env var → EBirdMissingKeyError."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(EBirdMissingKeyError):
            _resolve_api_key(api_key=None, secret_ref=None)


def test_resolve_api_key_via_persistence_secret_ref(monkeypatch):
    """When ``secret_ref`` is a SecretRecord-like object, the resolver goes
    through the bound Persistence and returns the materialized value."""

    class FakePersistence:
        async def get_secret_value(self, secret_ref):
            return "vault-resolved-key"

    # Bind a fake Persistence.
    set_persistence_for_secrets(FakePersistence())
    try:
        # Pass a non-str object so the str shortcut is bypassed.
        class FakeRecord:
            secret_id = "S01"
            provider = "ebird"
            is_active = True
            vault_ref = "projects/p/secrets/s/versions/latest"

        with patch.dict(os.environ, {}, clear=True):
            out = _resolve_api_key(api_key=None, secret_ref=FakeRecord())
        assert out == "vault-resolved-key"
    finally:
        set_persistence_for_secrets(None)


# ---------------------------------------------------------------------------
# FlatGeobuf serialization tests.
# ---------------------------------------------------------------------------


def test_records_to_flatgeobuf_serializes_features():
    """A handful of in-bbox records become a non-trivial FlatGeobuf."""
    records = [
        _make_obs_record(sub_id="S1", lat=38.2, lng=-122.3),
        _make_obs_record(sub_id="S2", lat=38.4, lng=-122.1),
        _make_obs_record(sub_id="S3", lat=38.1, lng=-122.4),
    ]
    fgb_bytes = _records_to_flatgeobuf_bytes(records, _SMALL_BBOX, "bewwre")
    assert len(fgb_bytes) > 0
    assert fgb_bytes.startswith(b"fgb")

    import tempfile
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(fgb_bytes)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 3
        assert set(gdf.columns) >= {
            "subId",
            "obsDt",
            "locName",
            "howMany",
            "comName",
            "sciName",
            "speciesCode",
            "geometry",
        }
    finally:
        os.unlink(tf_path)


def test_records_outside_bbox_are_filtered_geographic_correctness():
    """job-0086 codified lesson: every emitted point must lie inside the bbox."""
    in_bbox = _make_obs_record(sub_id="S1", lat=38.2, lng=-122.3)
    way_outside = _make_obs_record(sub_id="S2", lat=40.0, lng=-119.0)  # NV
    just_outside = _make_obs_record(sub_id="S3", lat=38.2, lng=-121.99)  # 1km E of east edge

    fgb_bytes = _records_to_flatgeobuf_bytes(
        [in_bbox, way_outside, just_outside], _SMALL_BBOX, "bewwre"
    )

    import tempfile
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(fgb_bytes)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        # Only the in-bbox record survives.
        assert len(gdf) == 1
        for geom in gdf.geometry:
            x, y = geom.x, geom.y
            assert _SMALL_BBOX[0] <= x <= _SMALL_BBOX[2]
            assert _SMALL_BBOX[1] <= y <= _SMALL_BBOX[3]
    finally:
        os.unlink(tf_path)


def test_records_with_missing_coords_are_skipped():
    """Records without lat/lng are silently dropped."""
    good = _make_obs_record(sub_id="S1", lat=38.2, lng=-122.3)
    no_coords = {
        "subId": "S2",
        "comName": "Foo bar",
        "lat": None,
        "lng": None,
        "obsDt": "2026-06-01 08:30",
        "locName": "",
        "howMany": 1,
        "speciesCode": "bewwre",
        "sciName": "Foo bar",
    }
    fgb_bytes = _records_to_flatgeobuf_bytes([good, no_coords], _SMALL_BBOX, "bewwre")

    import tempfile
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(fgb_bytes)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 1
    finally:
        os.unlink(tf_path)


def test_empty_records_produces_empty_flatgeobuf():
    """An empty record list still produces a well-formed (empty) FlatGeobuf."""
    fgb_bytes = _records_to_flatgeobuf_bytes([], _SMALL_BBOX, "bewwre")
    assert len(fgb_bytes) > 0  # header still has content
    assert fgb_bytes.startswith(b"fgb")


def test_records_with_howmany_X_handled():
    """eBird ``howMany="X"`` (presence-only) maps to None without crashing."""
    rec = _make_obs_record(sub_id="S1", lat=38.2, lng=-122.3, how_many="X")
    fgb_bytes = _records_to_flatgeobuf_bytes([rec], _SMALL_BBOX, "bewwre")
    import tempfile
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(fgb_bytes)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 1
        # howMany should be None / NaN — not the literal string "X".
        assert gdf.iloc[0]["howMany"] is None or (
            isinstance(gdf.iloc[0]["howMany"], float)
            and gdf.iloc[0]["howMany"] != gdf.iloc[0]["howMany"]  # NaN check
        )
    finally:
        os.unlink(tf_path)


# ---------------------------------------------------------------------------
# Mocked HTTP tests — happy paths.
# ---------------------------------------------------------------------------


def test_mocked_happy_path_single_tile():
    """A small-bbox call → 1 tile → 200 OK → FlatGeobuf with N features."""
    fake_gcs = FakeStorageClient()
    records = [
        _make_obs_record(sub_id=f"S{i}", lat=38.2, lng=-122.3 + i * 0.001)
        for i in range(5)
    ]
    mock_client = _MockHTTPClient([_FakeHTTPResponse(200, records)])

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.httpx.Client",
        return_value=mock_client,
    ):
        result = fetch_ebird_observations(
            species_code=_BEWICK_WREN_CODE,
            bbox=_SMALL_BBOX,
            api_key=_MOCK_API_KEY,
        )

    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    assert result.style_preset == "ebird_observations"

    # Verify the saved FlatGeobuf has 5 features.
    [(path, data)] = list(fake_gcs.store.items())
    assert path.startswith("cache/dynamic-1h/ebird/")
    assert path.endswith(".fgb")

    import tempfile
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        assert len(gdf) == 5
    finally:
        os.unlink(tf_path)

    # Verify the request carried the right headers + species_code in URL.
    assert len(mock_client.get_calls) == 1
    call = mock_client.get_calls[0]
    assert _BEWICK_WREN_CODE in call["url"]
    assert call["headers"]["X-eBirdApiToken"] == _MOCK_API_KEY


def test_mocked_multi_tile_dedup_across_tiles():
    """A larger bbox triggers a multi-tile cover; overlapping subIds dedupe."""
    fake_gcs = FakeStorageClient()

    # Build N tile centers' worth of responses. Each tile returns 2 records,
    # but each tile shares one subId with the next tile so the dedup count
    # is < total returned records.
    centers = _bbox_to_tile_centers(_MULTI_TILE_BBOX)
    n_tiles = len(centers)
    assert n_tiles >= 2, "bbox is supposed to require multi-tile cover"

    responses: list[_FakeHTTPResponse] = []
    for t in range(n_tiles):
        # Two records per tile: one unique to this tile, one shared with the
        # next tile (mod n).
        unique_id = f"U{t}"
        shared_id = f"SH{t % 2}"  # only 2 distinct shared ids across all tiles
        lat = centers[t][0]
        lng = centers[t][1]
        # Place all coords inside _MULTI_TILE_BBOX (use tile center).
        recs = [
            _make_obs_record(sub_id=unique_id, lat=lat, lng=lng),
            _make_obs_record(sub_id=shared_id, lat=lat, lng=lng),
        ]
        responses.append(_FakeHTTPResponse(200, recs))

    mock_client = _MockHTTPClient(responses)

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.httpx.Client",
        return_value=mock_client,
    ):
        result = fetch_ebird_observations(
            species_code=_BEWICK_WREN_CODE,
            bbox=_MULTI_TILE_BBOX,
            api_key=_MOCK_API_KEY,
        )

    assert result.uri is not None
    # n_tiles unique ids + 2 shared ids = n_tiles + 2 distinct subIds.
    [(_, data)] = list(fake_gcs.store.items())

    import tempfile
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
        expected_count = n_tiles + 2  # n_tiles unique + 2 shared subIds
        assert len(gdf) == expected_count
        # And we made exactly n_tiles HTTP calls.
        assert len(mock_client.get_calls) == n_tiles
    finally:
        os.unlink(tf_path)


def test_mocked_empty_response_returns_empty_flatgeobuf():
    """An empty list response → empty FlatGeobuf, no error."""
    fake_gcs = FakeStorageClient()
    mock_client = _MockHTTPClient([_FakeHTTPResponse(200, [])])

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.httpx.Client",
        return_value=mock_client,
    ):
        result = fetch_ebird_observations(
            species_code=_BEWICK_WREN_CODE,
            bbox=_SMALL_BBOX,
            api_key=_MOCK_API_KEY,
        )

    assert result.uri is not None
    [(_, data)] = list(fake_gcs.store.items())
    assert len(data) > 0  # FlatGeobuf header always present


# ---------------------------------------------------------------------------
# Error-path tests.
# ---------------------------------------------------------------------------


def test_missing_key_raises_pre_network():
    """No key, no env var, no secret_ref → EBirdMissingKeyError before any HTTP call."""
    fake_gcs = FakeStorageClient()
    mock_client = _MockHTTPClient([_FakeHTTPResponse(200, [])])

    with patch.dict(os.environ, {}, clear=True), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.httpx.Client",
        return_value=mock_client,
    ):
        with pytest.raises(EBirdMissingKeyError):
            fetch_ebird_observations(
                species_code=_BEWICK_WREN_CODE,
                bbox=_SMALL_BBOX,
            )
    # No HTTP call should have been made.
    assert mock_client.get_calls == []
    # No artifact should have been written.
    assert fake_gcs.store == {}


def test_mocked_401_raises_auth_error_not_retryable():
    """A 401 from eBird raises non-retryable EBirdAuthError."""
    fake_gcs = FakeStorageClient()
    mock_client = _MockHTTPClient(
        [_FakeHTTPResponse(401, text="Unauthorized")]
    )

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.httpx.Client",
        return_value=mock_client,
    ):
        with pytest.raises(EBirdAuthError) as exc_info:
            fetch_ebird_observations(
                species_code=_BEWICK_WREN_CODE,
                bbox=_SMALL_BBOX,
                api_key=_MOCK_API_KEY,
            )
        assert exc_info.value.retryable is False
    assert fake_gcs.store == {}


def test_mocked_404_raises_input_error_bad_species():
    """A 404 → EBirdInputError (unknown species code)."""
    fake_gcs = FakeStorageClient()
    mock_client = _MockHTTPClient(
        [_FakeHTTPResponse(404, text="Species not found")]
    )

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.httpx.Client",
        return_value=mock_client,
    ):
        with pytest.raises(EBirdInputError) as exc_info:
            fetch_ebird_observations(
                species_code="notarealcode",
                bbox=_SMALL_BBOX,
                api_key=_MOCK_API_KEY,
            )
        assert exc_info.value.retryable is False


def test_mocked_5xx_raises_upstream_error_retryable():
    """A 503 → retryable EBirdUpstreamError."""
    fake_gcs = FakeStorageClient()
    mock_client = _MockHTTPClient(
        [_FakeHTTPResponse(503, text="Service Unavailable")]
    )

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.httpx.Client",
        return_value=mock_client,
    ):
        with pytest.raises(EBirdUpstreamError) as exc_info:
            fetch_ebird_observations(
                species_code=_BEWICK_WREN_CODE,
                bbox=_SMALL_BBOX,
                api_key=_MOCK_API_KEY,
            )
        assert exc_info.value.retryable is True
    assert fake_gcs.store == {}


# ---------------------------------------------------------------------------
# Cache-layer tests.
# ---------------------------------------------------------------------------


def test_cache_hit_skips_fetch_fn():
    """Second call with identical params returns the cached URI without re-fetching."""
    fake_gcs = FakeStorageClient()
    records = [_make_obs_record(sub_id="S1", lat=38.2, lng=-122.3)]
    mock_client = _MockHTTPClient([_FakeHTTPResponse(200, records)])

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.httpx.Client",
        return_value=mock_client,
    ):
        r1 = fetch_ebird_observations(
            species_code=_BEWICK_WREN_CODE,
            bbox=_SMALL_BBOX,
            api_key=_MOCK_API_KEY,
        )
        r2 = fetch_ebird_observations(
            species_code=_BEWICK_WREN_CODE,
            bbox=_SMALL_BBOX,
            api_key=_MOCK_API_KEY,
        )

    # Only one HTTP call should have been made (second hit the cache).
    assert len(mock_client.get_calls) == 1
    assert r1.uri == r2.uri


def test_layer_uri_shape_fields():
    """The returned LayerURI carries the documented fields."""
    fake_gcs = FakeStorageClient()
    records = [_make_obs_record(sub_id="S1", lat=38.2, lng=-122.3)]
    mock_client = _MockHTTPClient([_FakeHTTPResponse(200, records)])

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.httpx.Client",
        return_value=mock_client,
    ):
        result = fetch_ebird_observations(
            species_code=_BEWICK_WREN_CODE,
            bbox=_SMALL_BBOX,
            api_key=_MOCK_API_KEY,
        )

    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    assert result.style_preset == "ebird_observations"
    assert _BEWICK_WREN_CODE in result.layer_id
    assert "eBird" in result.name


def test_cache_key_omits_api_key():
    """Two callers with the SAME bbox + species_code but DIFFERENT keys hit
    the same cache entry — the observations are user-independent.

    This is a deliberate Decision F orthogonal property: caching by api_key
    would defeat the cache. We assert the same cache key + URI is produced
    regardless of which api_key was used to populate the cache.
    """
    fake_gcs = FakeStorageClient()
    records = [_make_obs_record(sub_id="S1", lat=38.2, lng=-122.3)]
    mock_client = _MockHTTPClient([_FakeHTTPResponse(200, records)])

    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ), patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.httpx.Client",
        return_value=mock_client,
    ):
        r1 = fetch_ebird_observations(
            species_code=_BEWICK_WREN_CODE,
            bbox=_SMALL_BBOX,
            api_key="caller-A-key",
        )
        # Second call with a different api_key should still hit the cache.
        r2 = fetch_ebird_observations(
            species_code=_BEWICK_WREN_CODE,
            bbox=_SMALL_BBOX,
            api_key="caller-B-key",
        )

    assert r1.uri == r2.uri
    assert len(mock_client.get_calls) == 1  # cache hit on second call


# ---------------------------------------------------------------------------
# Live test — real eBird API call (env-gated).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (_LIVE_EBIRD and _LIVE_EBIRD_KEY),
    reason="TRID3NT_TEST_LIVE_EBIRD=1 + TRID3NT_EBIRD_API_KEY not set",
)
def test_live_bewickwren_over_ca_bbox(tmp_path):
    """LIVE: Bewick's Wren over a CA bbox (audit.md example).

    Calls the real eBird API. Captures evidence to evidence/ebird_live.txt.
    Asserts ≥0 features returned (the species may have no recent sightings
    in winter); when present, every feature lies inside the requested bbox.
    """
    # CA bbox from audit.md: (-122, 38, -119, 40).
    bbox = (-122.0, 38.0, -119.0, 40.0)

    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetchers.biodiversity.fetch_ebird_observations.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_ebird_observations(
            species_code=_BEWICK_WREN_CODE,
            bbox=bbox,
            days_back=30,
        )

    assert result.uri is not None
    [(path, data)] = list(fake_gcs.store.items())
    assert path.startswith("cache/dynamic-1h/ebird/")
    assert path.endswith(".fgb")

    import tempfile
    import geopandas as gpd  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        tf.write(data)
        tf_path = tf.name
    try:
        gdf = gpd.read_file(tf_path, engine="pyogrio")
    finally:
        os.unlink(tf_path)

    # Per audit.md: live test asserts ≥0 features.
    assert len(gdf) >= 0

    # Geographic correctness: every feature in bbox.
    for geom in gdf.geometry:
        x, y = geom.x, geom.y
        assert bbox[0] <= x <= bbox[2], f"feature lng {x} outside bbox"
        assert bbox[1] <= y <= bbox[3], f"feature lat {y} outside bbox"

    # Write evidence.
    evidence_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "evidence",
    )
    os.makedirs(evidence_dir, exist_ok=True)
    evidence_path = os.path.join(
        evidence_dir, "ebird_live_job_0128.txt"
    )
    lines = [
        "# eBird live test — Bewick's Wren over CA bbox",
        f"# bbox: {bbox}",
        f"# days_back: 30",
        f"# result.uri: {result.uri}",
        f"# feature count: {len(gdf)}",
        "",
    ]
    for i, row in enumerate(gdf.head(5).itertuples(index=False)):
        lines.append(f"feature {i}: {row}")
    with open(evidence_path, "w") as f:
        f.write("\n".join(lines))
    print("\n" + "\n".join(lines))
    print(f"# evidence written to: {evidence_path}")
