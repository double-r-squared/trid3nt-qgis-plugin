"""Unit + live tests for ``fetch_firms_active_fire`` (job-0108).

Coverage (no network needed):
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Invalid source raises FirmsArgError (not retryable).
- Invalid days_back raises FirmsArgError.
- Invalid bbox (degenerate / out-of-range) raises FirmsArgError.
- Mocked CSV → FlatGeobuf Point layer with retained columns.
- Geography-correctness: synthetic California-shape rows produce points
  inside California's WGS84 envelope (per job-0086 lesson).
- Empty FIRMS response → 0-feature FlatGeobuf (no exception).
- Auth-failure ("Invalid MAP_KEY.") → FirmsAuthError.
- Cache miss: fetch_fn invoked + bytes written.
- Cache hit: second call with same params skips fetch_fn.

Live tests (network-gated by TRID3NT_TEST_LIVE_FIRMS=1 AND a valid
TRID3NT_FIRMS_MAP_KEY env var):
- Real fetch for California-shape bbox last 7 days returns ≥0 features in
  the bbox.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire import (
    FirmsArgError,
    FirmsAuthError,
    FirmsUpstreamError,
    _parse_firms_csv_to_fgb,
    _resolve_map_key,
    fetch_firms_active_fire,
)


# ---------------------------------------------------------------------------
# Constants / helpers.
# ---------------------------------------------------------------------------


_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
_LIVE_FIRMS = os.environ.get("TRID3NT_TEST_LIVE_FIRMS") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing — mirrors test_fetch_storm_events_db.py pattern.
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

    def upload_from_string(
        self, data: bytes, content_type: str | None = None
    ) -> None:
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


# ---------------------------------------------------------------------------
# Synthetic FIRMS CSV builder.
# ---------------------------------------------------------------------------


_FIRMS_HEADER = (
    "latitude,longitude,brightness,scan,track,acq_date,acq_time,"
    "satellite,instrument,confidence,version,bright_t31,frp,daynight"
)


def _make_synth_firms_csv(
    n_ca_fires: int = 5,
    ca_lat0: float = 37.5,
    ca_lon0: float = -120.5,
    lat_step: float = 0.05,
    lon_step: float = 0.05,
) -> str:
    """Build a synthetic FIRMS CSV body shaped like the VIIRS_SNPP_NRT product.

    California coords: roughly (37.5, -120.5) area, well inside CA's WGS84
    envelope (-124.5, 32.5, -114.1, 42.0).
    """
    rows = [_FIRMS_HEADER]
    for i in range(n_ca_fires):
        rows.append(
            f"{ca_lat0 + i * lat_step:.4f},{ca_lon0 + i * lon_step:.4f},"
            f"{320.5 + i:.1f},0.42,0.41,2026-06-07,1230,"
            f"N,VIIRS,nominal,2.0NRT,300.1,{15.5 + i * 0.5:.1f},D"
        )
    return "\n".join(rows) + "\n"


# ---------------------------------------------------------------------------
# Registration tests (no network).
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_firms_active_fire appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_firms_active_fire" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_firms_active_fire"]
    assert entry.metadata.name == "fetch_firms_active_fire"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "firms_active_fire"
    assert entry.metadata.cacheable is True


# ---------------------------------------------------------------------------
# Typed-error tests (no network).
# ---------------------------------------------------------------------------


def test_invalid_source_raises_typed_error():
    """Unknown source name raises FirmsArgError (not retryable)."""
    with pytest.raises(FirmsArgError, match="source"):
        fetch_firms_active_fire(
            bbox=(-122.0, 38.0, -119.0, 40.0),
            source="LANDSAT_NRT",  # type: ignore[arg-type]
        )


def test_invalid_days_back_raises_typed_error():
    """days_back outside [1,10] raises FirmsArgError."""
    with pytest.raises(FirmsArgError, match="days_back"):
        fetch_firms_active_fire(
            bbox=(-122.0, 38.0, -119.0, 40.0), days_back=0
        )
    with pytest.raises(FirmsArgError, match="days_back"):
        fetch_firms_active_fire(
            bbox=(-122.0, 38.0, -119.0, 40.0), days_back=11
        )


def test_invalid_bbox_raises_typed_error():
    """Degenerate or out-of-range bbox raises FirmsArgError."""
    # Degenerate (min == max).
    with pytest.raises(FirmsArgError, match="bbox"):
        fetch_firms_active_fire(bbox=(-122.0, 38.0, -122.0, 40.0))
    # Lon out of range.
    with pytest.raises(FirmsArgError, match="bbox"):
        fetch_firms_active_fire(bbox=(-200.0, 38.0, -119.0, 40.0))
    # Wrong number of elements.
    with pytest.raises(FirmsArgError, match="bbox"):
        fetch_firms_active_fire(bbox=(-122.0, 38.0, -119.0))  # type: ignore[arg-type]


def test_arg_errors_are_not_retryable():
    """All FirmsArgError instances have retryable=False per FR-AS-11."""
    try:
        fetch_firms_active_fire(
            bbox=(-122.0, 38.0, -119.0, 40.0), days_back=0
        )
    except FirmsArgError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected FirmsArgError")


# ---------------------------------------------------------------------------
# CSV → FlatGeobuf parser tests (no network).
# ---------------------------------------------------------------------------


def test_parse_csv_to_fgb_yields_points():
    """5 synthetic CA rows → 5-point FlatGeobuf with FGB magic prefix."""
    csv_text = _make_synth_firms_csv(n_ca_fires=5)
    fgb_bytes = _parse_firms_csv_to_fgb(csv_text)
    assert fgb_bytes.startswith(b"fgb"), "FlatGeobuf magic prefix missing"

    import geopandas as gpd  # type: ignore[import-not-found]
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        path = f.name
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 5
        assert gdf.crs.to_epsg() == 4326
        # Required FIRMS-property columns retained.
        for col in ("brightness", "frp", "confidence", "acq_date"):
            assert col in gdf.columns, f"property column {col!r} missing"
    finally:
        os.unlink(path)


def test_parse_csv_empty_returns_zero_feature_fgb():
    """Header-only CSV → valid 0-feature FlatGeobuf (no exception).

    Per kickoff: "Empty response → 0-feature FlatGeobuf". This is the FIRMS
    "no detections this window" case — must not crash the pipeline.
    """
    csv_text = _FIRMS_HEADER + "\n"
    fgb_bytes = _parse_firms_csv_to_fgb(csv_text)
    assert fgb_bytes.startswith(b"fgb")

    import geopandas as gpd  # type: ignore[import-not-found]
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        path = f.name
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 0
    finally:
        os.unlink(path)


def test_parse_csv_blank_response_raises():
    """Completely blank body raises FirmsUpstreamError."""
    with pytest.raises(FirmsUpstreamError, match="empty response"):
        _parse_firms_csv_to_fgb("")
    with pytest.raises(FirmsUpstreamError, match="empty response"):
        _parse_firms_csv_to_fgb("   \n   ")


def test_parse_csv_missing_required_columns_raises():
    """Missing latitude/longitude columns raise FirmsUpstreamError."""
    bad_csv = "foo,bar\n1,2\n"
    with pytest.raises(FirmsUpstreamError, match="missing required columns"):
        _parse_firms_csv_to_fgb(bad_csv)


def test_geography_correctness_california_synthetic_rows():
    """Synthetic CA rows produce points inside California's WGS84 envelope.

    Per the codified job-0086 lesson — verify output against known geography
    of the bbox, not just bytes round-tripping. California is roughly
    (-124.5, 32.5, -114.1, 42.0); the synthetic rows are centered around
    (-120.5, 37.5) which IS inside California (Central Valley area).
    """
    csv_text = _make_synth_firms_csv(n_ca_fires=5)
    fgb_bytes = _parse_firms_csv_to_fgb(csv_text)

    import geopandas as gpd  # type: ignore[import-not-found]
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        path = f.name
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert gdf.crs.to_epsg() == 4326
        # California envelope (generous).
        ca_min_lon, ca_min_lat, ca_max_lon, ca_max_lat = -124.5, 32.5, -114.1, 42.0
        for geom in gdf.geometry:
            assert ca_min_lon <= geom.x <= ca_max_lon, (
                f"longitude {geom.x} outside California"
            )
            assert ca_min_lat <= geom.y <= ca_max_lat, (
                f"latitude {geom.y} outside California"
            )
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Auth-failure detection (mocked).
# ---------------------------------------------------------------------------


def test_invalid_map_key_response_raises_auth_error():
    """When FIRMS returns ``Invalid MAP_KEY.`` body (HTTP 400), raise FirmsAuthError.

    Verified live 2026-06-08: FIRMS returns HTTP 400 + plain-text body
    "Invalid MAP_KEY." for unknown / unregistered keys (DEMO / 'demo' both
    fail this way). The auth-error path must fire BEFORE the HTTP-status
    guard so the user gets the actionable "set TRID3NT_FIRMS_MAP_KEY" message.
    """
    from trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire import _fetch_firms_csv

    # HTTP 400 + invalid-key body (the actual live behavior).
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Invalid MAP_KEY.\nInvalid day range. Expects [1..5]."

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire.requests.get",
        return_value=mock_resp,
    ):
        with pytest.raises(FirmsAuthError, match="MAP_KEY"):
            _fetch_firms_csv(
                bbox=(-122.0, 38.0, -119.0, 40.0),
                days_back=1,
                source="VIIRS_SNPP_NRT",
                map_key="demo",
            )

    # Also verify HTTP 200 + invalid-key body still raises auth (defensive —
    # the FIRMS server may return either; we surface the same typed error).
    mock_resp_200 = MagicMock()
    mock_resp_200.status_code = 200
    mock_resp_200.text = "Invalid MAP_KEY."
    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire.requests.get",
        return_value=mock_resp_200,
    ):
        with pytest.raises(FirmsAuthError, match="MAP_KEY"):
            _fetch_firms_csv(
                bbox=(-122.0, 38.0, -119.0, 40.0),
                days_back=1,
                source="VIIRS_SNPP_NRT",
                map_key="demo",
            )


def test_resolve_map_key_honors_env_var():
    """_resolve_map_key reads TRID3NT_FIRMS_MAP_KEY env var; falls back to 'demo'."""
    with patch.dict(
        os.environ, {"TRID3NT_FIRMS_MAP_KEY": "my-real-key-abc"}, clear=False
    ):
        assert _resolve_map_key() == "my-real-key-abc"
    # Without the env var (popped), falls back to "demo".
    env = dict(os.environ)
    env.pop("TRID3NT_FIRMS_MAP_KEY", None)
    with patch.dict(os.environ, env, clear=True):
        assert _resolve_map_key() == "demo"


# ---------------------------------------------------------------------------
# Cache miss/hit tests (mocked GCS + mocked fetch).
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_fetch_and_writes():
    """On cache miss, _fetch_firms_active_fire_bytes is invoked and bytes are stored."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_fgb = b"fgb" + b"\x00" * 32 + b"_FAKE_FIRMS"

    def fake_fetch(*_args, **_kwargs) -> bytes:
        fetch_count["n"] += 1
        return fake_fgb

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire._fetch_firms_active_fire_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_firms_active_fire(
            bbox=(-122.0, 38.0, -119.0, 40.0),
            days_back=7,
            source="VIIRS_SNPP_NRT",
        )

    assert fetch_count["n"] == 1, "fetch_fn should fire once on cache miss"
    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.units is None
    assert result.uri.startswith("s3://")
    assert "firms_active_fire" in result.uri
    assert len(fake_gcs.store) == 1, "one artifact written to fake GCS"
    # bbox echoed in LayerURI for zoom-to wiring.
    assert result.bbox == (-122.0, 38.0, -119.0, 40.0)


def test_cache_hit_skips_fetch():
    """Second call with same params hits the cache and does not invoke fetch."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_fgb = b"fgb" + b"\x00" * 32 + b"_FAKE_FIRMS_CACHED"

    def fake_fetch(*_args, **_kwargs) -> bytes:
        fetch_count["n"] += 1
        return fake_fgb

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire._fetch_firms_active_fire_bytes",
        side_effect=fake_fetch,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_firms_active_fire(
            bbox=(-122.0, 38.0, -119.0, 40.0),
            days_back=7,
            source="VIIRS_SNPP_NRT",
        )
        assert fetch_count["n"] == 1
        # Second call with identical params — must hit cache.
        r2 = fetch_firms_active_fire(
            bbox=(-122.0, 38.0, -119.0, 40.0),
            days_back=7,
            source="VIIRS_SNPP_NRT",
        )
        assert fetch_count["n"] == 1, "cache hit must skip fetch_fn"
        assert r1.uri == r2.uri


# ---------------------------------------------------------------------------
# Live test — only runs with TRID3NT_TEST_LIVE_FIRMS=1 + valid MAP_KEY.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_FIRMS,
    reason="set TRID3NT_TEST_LIVE_FIRMS=1 to run live FIRMS test",
)
def test_live_fetch_california_last_5d(tmp_path):
    """Live FIRMS round-trip with two modes — both verify real upstream contact:

    1. ``TRID3NT_FIRMS_MAP_KEY`` set to a valid key → fetch CA bbox last 5d,
       assert FGB returned with every detected point inside the CA envelope.
       (5 days is the live FIRMS max as of 2026-06-08; kickoff said 10 but
       the upstream enforces [1..5]. See OQ-0108-DAYS-RANGE.)
    2. No real key set → assert ``FirmsAuthError`` raised on real FIRMS
       endpoint (verifies the auth-detection path is wired end-to-end).
    """
    real_key = os.environ.get("TRID3NT_FIRMS_MAP_KEY", "")
    has_real_key = bool(real_key) and real_key != "demo"

    fake_gcs = FakeStorageClient()

    if has_real_key:
        # Real-key mode: assert FGB-with-geography correctness.
        with patch(
            "trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire.read_through",
            side_effect=_make_read_through_injector(fake_gcs),
        ):
            result = fetch_firms_active_fire(
                bbox=(-122.0, 38.0, -119.0, 40.0),
                days_back=5,
                source="VIIRS_SNPP_NRT",
            )

        assert result.layer_type == "vector"
        assert len(fake_gcs.store) == 1
        fgb_bytes = next(iter(fake_gcs.store.values()))
        fgb_path = tmp_path / "live_firms.fgb"
        fgb_path.write_bytes(fgb_bytes)
        assert fgb_bytes.startswith(b"fgb")

        import geopandas as gpd  # type: ignore[import-not-found]
        gdf = gpd.read_file(str(fgb_path), engine="pyogrio")
        assert gdf.crs.to_epsg() == 4326

        if len(gdf) > 0:
            bbox = (-122.0, 38.0, -119.0, 40.0)
            for geom in gdf.geometry:
                assert bbox[0] <= geom.x <= bbox[2], (
                    f"live point lon {geom.x} outside requested CA bbox"
                )
                assert bbox[1] <= geom.y <= bbox[3], (
                    f"live point lat {geom.y} outside requested CA bbox"
                )
            print(
                f"\nlive_test (real key): {len(gdf)} active-fire detection(s) "
                f"in CA bbox last 5 days; first row: {gdf.iloc[0].to_dict()}"
            )
        else:
            print(
                "\nlive_test (real key): 0 active-fire detections in CA bbox last 5 days"
            )
    else:
        # No-key mode: verify the auth-detection branch fires end-to-end
        # against the real FIRMS endpoint (no mocking).
        with patch(
            "trid3nt_server.tools.fetchers.hazard.fetch_firms_active_fire.read_through",
            side_effect=_make_read_through_injector(fake_gcs),
        ):
            with pytest.raises(FirmsAuthError, match="MAP_KEY"):
                fetch_firms_active_fire(
                    bbox=(-122.0, 38.0, -119.0, 40.0),
                    days_back=1,
                    source="VIIRS_SNPP_NRT",
                )
        print(
            "\nlive_test (no key): FIRMS endpoint reached; FirmsAuthError "
            "fired as expected. Set TRID3NT_FIRMS_MAP_KEY to fetch real data."
        )
