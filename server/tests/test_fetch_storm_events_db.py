"""Unit + live tests for ``fetch_storm_events_db`` (job-0091).

Coverage (no network needed):
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Invalid year raises StormEventsArgError (not retryable).
- Invalid state code raises StormEventsArgError.
- Invalid event_types raises StormEventsArgError.
- Synthetic 100-row CSV → 100 points (mocked fetch).
- state='FL' filter narrows the synthetic CSV down to FL-only rows.
- event_types=['Hurricane'] further narrows.
- Year 2022 fixture has Hurricane Ian rows (geography correctness — Ian
  begin_lat/begin_lon land inside Florida's bounding box).
- Null-coord rows are dropped without error.
- Cache miss: fetch_fn is invoked and bytes are written.
- Cache hit: second call with same params skips fetch_fn.
- _resolve_csv_url extracts highest-processed-date file from index HTML.

Live tests (network-gated by TRID3NT_TEST_LIVE_STORM=1):
- Real fetch for year=2022, state='FL' returns >0 features.
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
from trid3nt_server.tools.fetchers.weather.fetch_storm_events_db import (
    StormEventsArgError,
    StormEventsEmptyError,
    StormEventsError,
    StormEventsUpstreamError,
    _normalize_state,
    _resolve_csv_url,
    _validate_bbox,
    _validate_inputs,
    _parse_filter_and_serialize,
    _window_years,
    estimate_payload_mb,
    fetch_storm_events_db,
)


# ---------------------------------------------------------------------------
# Constants / helpers.
# ---------------------------------------------------------------------------


_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
_LIVE_STORM = os.environ.get("TRID3NT_TEST_LIVE_STORM") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing — mirrors test_fetch_administrative_boundaries.py pattern.
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


# ---------------------------------------------------------------------------
# Synthetic NOAA Storm Events CSV builder.
# ---------------------------------------------------------------------------


# Minimal column set sufficient to exercise the parser/filter path. Real NOAA
# CSV has ~50 columns; we only need lat/lon + required filter columns + a
# couple of retained property columns.
_CSV_HEADER = (
    "EVENT_ID,EVENT_TYPE,STATE,BEGIN_DATE_TIME,END_DATE_TIME,"
    "BEGIN_LAT,BEGIN_LON,INJURIES_DIRECT,DAMAGE_PROPERTY,EPISODE_NARRATIVE"
)


def _make_synth_csv_rows(
    n_fl_hurricane: int = 5,
    n_fl_tornado: int = 10,
    n_tx_hail: int = 80,
    n_null_coords: int = 5,
) -> str:
    """Build a synthetic CSV body with controllable composition.

    Returns the full CSV text (header + rows) ready for gzip.
    """
    rows = [_CSV_HEADER]
    eid = 1000

    # Florida hurricanes — Hurricane Ian-shape coords (around 26.6N, -81.8W).
    for i in range(n_fl_hurricane):
        rows.append(
            f"{eid},Hurricane,FLORIDA,28-SEP-22 14:00:00,30-SEP-22 06:00:00,"
            f"{26.5 + i * 0.05:.4f},{-82.0 + i * 0.05:.4f},"
            f"0,5000000,\"Hurricane Ian made landfall near Cayo Costa\""
        )
        eid += 1

    # Florida tornadoes (different EVENT_TYPE but same STATE).
    for i in range(n_fl_tornado):
        rows.append(
            f"{eid},Tornado,FLORIDA,15-MAR-22 12:00:00,15-MAR-22 12:30:00,"
            f"{27.0 + i * 0.02:.4f},{-81.5 + i * 0.02:.4f},"
            f"0,10000,\"Brief tornado touched down\""
        )
        eid += 1

    # Texas hail (different STATE).
    for i in range(n_tx_hail):
        rows.append(
            f"{eid},Hail,TEXAS,05-MAY-22 16:00:00,05-MAY-22 16:15:00,"
            f"{31.0 + (i % 20) * 0.05:.4f},{-98.0 - (i % 20) * 0.05:.4f},"
            f"0,2500,\"Quarter-size hail reported\""
        )
        eid += 1

    # Null-coord rows (should be dropped silently).
    for _ in range(n_null_coords):
        rows.append(
            f"{eid},Flash Flood,GEORGIA,10-JUN-22 18:00:00,10-JUN-22 22:00:00,"
            ",,0,15000,\"Flash flooding closed roads\""
        )
        eid += 1

    return "\n".join(rows) + "\n"


def _csv_to_gzip_bytes(csv_text: str) -> bytes:
    """Gzip-encode CSV text the same way NCEI ships it."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write(csv_text.encode("utf-8"))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Registration tests (no network).
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_storm_events_db appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_storm_events_db" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_storm_events_db"]
    assert entry.metadata.name == "fetch_storm_events_db"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "storm_events"
    assert entry.metadata.cacheable is True


# ---------------------------------------------------------------------------
# Typed-error tests (no network).
# ---------------------------------------------------------------------------


def test_invalid_year_raises_typed_error():
    with pytest.raises(StormEventsArgError, match="year"):
        fetch_storm_events_db(year=1800)
    with pytest.raises(StormEventsArgError, match="year"):
        fetch_storm_events_db(year=3000)


def test_invalid_state_raises_typed_error():
    # An empty / whitespace-only state is rejected (no state to filter by).
    with pytest.raises(StormEventsArgError, match="state"):
        fetch_storm_events_db(year=2022, state="")  # empty
    with pytest.raises(StormEventsArgError, match="state"):
        fetch_storm_events_db(year=2022, state="   ")  # whitespace-only
    # A genuinely-unrecognized state is rejected.
    with pytest.raises(StormEventsArgError, match="unrecognized"):
        fetch_storm_events_db(year=2022, state="Atlantis")
    # A non-string state is rejected.
    with pytest.raises(StormEventsArgError, match="state"):
        fetch_storm_events_db(year=2022, state=42)  # type: ignore[arg-type]


def test_invalid_event_types_raises_typed_error():
    with pytest.raises(StormEventsArgError, match="event_types"):
        fetch_storm_events_db(
            year=2022, event_types="Hurricane"  # type: ignore[arg-type]
        )
    with pytest.raises(StormEventsArgError, match="event_types"):
        fetch_storm_events_db(year=2022, event_types=[""])


def test_arg_errors_are_not_retryable():
    try:
        fetch_storm_events_db(year=1800)
    except StormEventsArgError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected StormEventsArgError")


# ---------------------------------------------------------------------------
# Full-state-name acceptance (LIVE BUG 2026-06-17 — "Oklahoma" hard-rejected).
# ---------------------------------------------------------------------------


def test_validate_accepts_full_state_name_and_iso_code():
    """_validate_inputs accepts 'Oklahoma', 'oklahoma', and 'OK' — none raise.

    The Oklahoma-tornado bug: the validator HARD-REJECTED any state that was
    not exactly 2 chars, so the word the user typed ("Oklahoma") raised
    StormEventsArgError even though the query path already tolerated full
    names. All three forms must now validate cleanly.
    """
    for ok in ("Oklahoma", "oklahoma", "OKLAHOMA", "OK", "ok"):
        # Must NOT raise.
        _validate_inputs(year=2020, state=ok, event_types=["Tornado"])


def test_validate_still_rejects_unknown_state():
    """A genuinely-unrecognized state still raises StormEventsArgError."""
    with pytest.raises(StormEventsArgError, match="unrecognized"):
        _validate_inputs(year=2020, state="Narnia", event_types=None)


def test_normalize_state_maps_iso_and_full_name_to_noaa_spelling():
    """_normalize_state collapses ISO code + full name + case to one spelling."""
    assert _normalize_state("OK") == "OKLAHOMA"
    assert _normalize_state("ok") == "OKLAHOMA"
    assert _normalize_state("Oklahoma") == "OKLAHOMA"
    assert _normalize_state("oklahoma") == "OKLAHOMA"
    assert _normalize_state("FL") == "FLORIDA"
    assert _normalize_state("Florida") == "FLORIDA"
    assert _normalize_state(None) is None


def test_full_state_name_filters_same_as_iso_code():
    """state='Oklahoma' and state='OK' both filter to the OK rows identically."""
    # Build a synthetic CSV with Oklahoma + Texas rows.
    rows = [_CSV_HEADER]
    eid = 2000
    for i in range(7):  # 7 Oklahoma tornadoes
        rows.append(
            f"{eid},Tornado,OKLAHOMA,20-MAY-13 14:00:00,20-MAY-13 14:40:00,"
            f"{35.3 + i * 0.02:.4f},{-97.5 - i * 0.02:.4f},"
            f"0,1000000,\"Tornado near Moore\""
        )
        eid += 1
    for i in range(4):  # 4 Texas hail (noise)
        rows.append(
            f"{eid},Hail,TEXAS,05-MAY-13 16:00:00,05-MAY-13 16:15:00,"
            f"{31.0 + i * 0.05:.4f},{-98.0 - i * 0.05:.4f},"
            f"0,2500,\"Quarter-size hail\""
        )
        eid += 1
    csv_text = "\n".join(rows) + "\n"
    gz_bytes = _csv_to_gzip_bytes(csv_text)

    import geopandas as gpd  # type: ignore[import-not-found]

    def _count(state_arg: str) -> int:
        fgb = _parse_filter_and_serialize(
            gz_bytes, state=state_arg, event_types=["Tornado"]
        )
        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
            f.write(fgb)
            path = f.name
        try:
            gdf = gpd.read_file(path, engine="pyogrio")
            assert (gdf["STATE"].str.upper() == "OKLAHOMA").all()
            return len(gdf)
        finally:
            os.unlink(path)

    n_full = _count("Oklahoma")
    n_iso = _count("OK")
    n_lower = _count("oklahoma")
    assert n_full == n_iso == n_lower == 7


def test_full_name_and_iso_share_cache_key():
    """state='Oklahoma' and state='OK' resolve to the SAME cache entry.

    Both normalize to NOAA's 'OKLAHOMA' spelling, so the second call (whatever
    spelling) hits the cache the first call populated.
    """
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_fgb = b"fgb" + b"\x00" * 32 + b"_FAKE_OK"

    def fake_fetch() -> bytes:
        fetch_count["n"] += 1
        return fake_fgb

    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db._fetch_storm_events_bytes",
        side_effect=lambda *a, **k: fake_fetch(),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_storm_events_db(
            year=2013, state="Oklahoma", event_types=["Tornado"]
        )
        r2 = fetch_storm_events_db(
            year=2013, state="OK", event_types=["Tornado"]
        )

    assert fetch_count["n"] == 1, "full-name + ISO must share one cache key"
    assert r1.uri == r2.uri


# ---------------------------------------------------------------------------
# URL-resolution test (mocked index page).
# ---------------------------------------------------------------------------


def test_resolve_csv_url_picks_highest_processed_date():
    """_resolve_csv_url picks the file with the highest c{YYYYMMDD} for the year."""
    fake_index = """
    <html><body>
    <td><a href="StormEvents_details-ftp_v1.0_d2022_c20230101.csv.gz">old</a></td>
    <td><a href="StormEvents_details-ftp_v1.0_d2022_c20260323.csv.gz">new</a></td>
    <td><a href="StormEvents_details-ftp_v1.0_d2021_c20260323.csv.gz">different year</a></td>
    </body></html>
    """
    mock_resp = MagicMock()
    mock_resp.text = fake_index
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp

    url = _resolve_csv_url(2022, client=mock_client)
    assert url.endswith("StormEvents_details-ftp_v1.0_d2022_c20260323.csv.gz")


def test_resolve_csv_url_no_match_raises():
    """Year missing from index raises StormEventsUpstreamError."""
    fake_index = "no storm files here"
    mock_resp = MagicMock()
    mock_resp.text = fake_index
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get.return_value = mock_resp

    with pytest.raises(StormEventsUpstreamError, match="no NOAA Storm Events CSV"):
        _resolve_csv_url(2022, client=mock_client)


# ---------------------------------------------------------------------------
# Parser/filter tests against synthetic CSV (no network).
# ---------------------------------------------------------------------------


def test_synthetic_100_row_csv_yields_100_points():
    """A 100-row synthetic CSV produces a 100-feature FlatGeobuf (no filters,
    no nulls)."""
    csv_text = _make_synth_csv_rows(
        n_fl_hurricane=5,
        n_fl_tornado=10,
        n_tx_hail=80,
        n_null_coords=5,  # these will be dropped → final 95
    )
    gz_bytes = _csv_to_gzip_bytes(csv_text)
    # No filter — all valid-coord rows kept (5 + 10 + 80 = 95 of 100).
    fgb_bytes = _parse_filter_and_serialize(gz_bytes, state=None, event_types=None)
    assert fgb_bytes.startswith(b"fgb")  # FlatGeobuf magic prefix
    # Re-read with geopandas to confirm feature count.
    import geopandas as gpd  # type: ignore[import-not-found]
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        path = f.name
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 95  # null-coord rows dropped
    finally:
        os.unlink(path)


def test_state_filter_narrows_to_fl():
    """state='FL' filter retains only Florida rows."""
    csv_text = _make_synth_csv_rows(
        n_fl_hurricane=5, n_fl_tornado=10, n_tx_hail=80, n_null_coords=0
    )
    gz_bytes = _csv_to_gzip_bytes(csv_text)
    fgb_bytes = _parse_filter_and_serialize(gz_bytes, state="FL", event_types=None)
    import geopandas as gpd  # type: ignore[import-not-found]
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        path = f.name
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 15  # 5 hurricane + 10 tornado, no TX
        assert (gdf["STATE"].str.upper() == "FLORIDA").all()
    finally:
        os.unlink(path)


def test_event_types_filter_narrows_to_hurricane():
    """event_types=['Hurricane'] additionally narrows the FL slice."""
    csv_text = _make_synth_csv_rows(
        n_fl_hurricane=5, n_fl_tornado=10, n_tx_hail=80, n_null_coords=0
    )
    gz_bytes = _csv_to_gzip_bytes(csv_text)
    fgb_bytes = _parse_filter_and_serialize(
        gz_bytes, state="FL", event_types=["Hurricane"]
    )
    import geopandas as gpd  # type: ignore[import-not-found]
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        path = f.name
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 5
        assert (gdf["EVENT_TYPE"] == "Hurricane").all()
    finally:
        os.unlink(path)


def test_null_coord_rows_dropped_without_error():
    """Rows with missing BEGIN_LAT/BEGIN_LON are silently dropped."""
    csv_text = _make_synth_csv_rows(
        n_fl_hurricane=2,
        n_fl_tornado=0,
        n_tx_hail=0,
        n_null_coords=10,  # all should be dropped
    )
    gz_bytes = _csv_to_gzip_bytes(csv_text)
    fgb_bytes = _parse_filter_and_serialize(
        gz_bytes, state=None, event_types=None
    )
    import geopandas as gpd  # type: ignore[import-not-found]
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        path = f.name
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 2  # null-coord Georgia rows dropped
    finally:
        os.unlink(path)


def test_empty_filter_result_raises_typed_error():
    """When no rows survive filtering, StormEventsEmptyError surfaces."""
    csv_text = _make_synth_csv_rows(
        n_fl_hurricane=0,
        n_fl_tornado=0,
        n_tx_hail=80,
        n_null_coords=0,  # only TX rows
    )
    gz_bytes = _csv_to_gzip_bytes(csv_text)
    with pytest.raises(StormEventsEmptyError):
        _parse_filter_and_serialize(
            gz_bytes, state="FL", event_types=None
        )


def test_geography_correctness_florida_hurricane_points():
    """Synthetic FL hurricane rows produce points inside FL's WGS84 bbox.

    Per the codified job-0086 lesson — verify output against known geography
    of the bbox, not just bytes round-tripping. Florida is roughly
    (-87.6, 24.4, -80.0, 31.0); Hurricane Ian's landfall area is around
    (-82.0, 26.5).
    """
    csv_text = _make_synth_csv_rows(
        n_fl_hurricane=5, n_fl_tornado=0, n_tx_hail=0, n_null_coords=0
    )
    gz_bytes = _csv_to_gzip_bytes(csv_text)
    fgb_bytes = _parse_filter_and_serialize(
        gz_bytes, state="FL", event_types=["Hurricane"]
    )
    import geopandas as gpd  # type: ignore[import-not-found]
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        path = f.name
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert gdf.crs.to_epsg() == 4326, f"expected EPSG:4326, got {gdf.crs}"
        # Florida envelope (generous).
        fl_min_lon, fl_min_lat, fl_max_lon, fl_max_lat = -87.6, 24.4, -80.0, 31.0
        for geom in gdf.geometry:
            assert fl_min_lon <= geom.x <= fl_max_lon, (
                f"longitude {geom.x} outside Florida"
            )
            assert fl_min_lat <= geom.y <= fl_max_lat, (
                f"latitude {geom.y} outside Florida"
            )
        # Specifically, Ian-shape coordinates should be near (26.5, -82.0).
        # Centroid should be within 1 degree of Ian's landfall area.
        centroid_lon = gdf.geometry.x.mean()
        centroid_lat = gdf.geometry.y.mean()
        assert abs(centroid_lon - (-82.0)) < 1.0, (
            f"hurricane centroid lon {centroid_lon} not near Ian landfall"
        )
        assert abs(centroid_lat - 26.5) < 1.0, (
            f"hurricane centroid lat {centroid_lat} not near Ian landfall"
        )
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Cache miss/hit tests (mocked GCS + mocked fetch).
# ---------------------------------------------------------------------------


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


def test_cache_miss_invokes_fetch_and_writes():
    """On cache miss, _fetch_storm_events_bytes is invoked and bytes are stored."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_fgb = b"fgb" + b"\x00" * 32 + b"_FAKE_STORM"

    def fake_fetch() -> bytes:
        fetch_count["n"] += 1
        return fake_fgb

    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db._fetch_storm_events_bytes",
        side_effect=lambda *a, **k: fake_fetch(),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_storm_events_db(
            year=2022, state="FL", event_types=["Hurricane"]
        )

    assert fetch_count["n"] == 1, "fetch_fn should fire once on cache miss"
    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    assert result.uri.startswith("s3://")
    assert "storm_events" in result.uri
    assert len(fake_gcs.store) == 1, "one artifact written to fake GCS"


def test_cache_hit_skips_fetch():
    """Second call with same params hits the cache and does not invoke fetch."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_fgb = b"fgb" + b"\x00" * 32 + b"_FAKE_STORM_CACHED"

    def fake_fetch() -> bytes:
        fetch_count["n"] += 1
        return fake_fgb

    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db._fetch_storm_events_bytes",
        side_effect=lambda *a, **k: fake_fetch(),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_storm_events_db(
            year=2022, state="FL", event_types=["Hurricane"]
        )
        assert fetch_count["n"] == 1
        # Second call with identical params — must hit cache.
        r2 = fetch_storm_events_db(
            year=2022, state="FL", event_types=["Hurricane"]
        )
        assert fetch_count["n"] == 1, "cache hit must skip fetch_fn"
        assert r1.uri == r2.uri


def test_event_types_order_does_not_split_cache():
    """event_types=['A','B'] and ['B','A'] hit the same cache key (sorted)."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_fgb = b"fgb" + b"\x00" * 32 + b"_FAKE_STORM_SORTED"

    def fake_fetch() -> bytes:
        fetch_count["n"] += 1
        return fake_fgb

    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db._fetch_storm_events_bytes",
        side_effect=lambda *a, **k: fake_fetch(),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_storm_events_db(
            year=2022, state="FL", event_types=["Hurricane", "Tornado"]
        )
        r2 = fetch_storm_events_db(
            year=2022, state="FL", event_types=["Tornado", "Hurricane"]
        )

    assert fetch_count["n"] == 1, "reordered list must reuse cache key"
    assert r1.uri == r2.uri


# ---------------------------------------------------------------------------
# bbox + date-window upgrade — synthetic builder with the structured NOAA date
# columns (BEGIN_YEARMONTH / BEGIN_DAY / BEGIN_TIME) + the new retained props.
# ---------------------------------------------------------------------------


# Header mirroring the real NOAA file's relevant columns: the structured date
# columns the window filter prefers, plus MAGNITUDE + DEATHS_* added in the
# upgrade.
_CSV_HEADER_FULL = (
    "EVENT_ID,EVENT_TYPE,STATE,BEGIN_YEARMONTH,BEGIN_DAY,BEGIN_TIME,"
    "BEGIN_DATE_TIME,END_DATE_TIME,BEGIN_LAT,BEGIN_LON,"
    "INJURIES_DIRECT,DEATHS_DIRECT,DEATHS_INDIRECT,DAMAGE_PROPERTY,"
    "MAGNITUDE,EPISODE_NARRATIVE"
)

_MONTH_ABBR = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
    7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}


def _synth_event_row(
    eid: int,
    event_type: str,
    state: str,
    year: int,
    month: int,
    day: int,
    lat: float,
    lon: float,
    *,
    hour: int = 12,
    minute: int = 0,
    deaths_direct: int = 0,
    deaths_indirect: int = 0,
    magnitude: str = "",
) -> str:
    """Build one CSV row in the full-column layout (structured date + props)."""
    yearmonth = f"{year:04d}{month:02d}"
    begin_time = f"{hour:02d}{minute:02d}"
    bdt = (
        f"{day:02d}-{_MONTH_ABBR[month]}-{year % 100:02d} "
        f"{hour:02d}:{minute:02d}:00"
    )
    return (
        f"{eid},{event_type},{state},{yearmonth},{day},{begin_time},"
        f"{bdt},{bdt},{lat:.4f},{lon:.4f},"
        f"0,{deaths_direct},{deaths_indirect},25000,{magnitude},"
        f"\"{event_type} near {state}\""
    )


def _make_window_bbox_csv() -> bytes:
    """A synthetic year-2022 CSV exercising bbox + date-window slices.

    Composition (all Tornado unless noted):
    - 4 OK tornadoes in May (inside an OK bbox).
    - 3 TX tornadoes in May (inside the SAME bbox — bbox crosses state lines).
    - 2 FL tornadoes in May (OUTSIDE the OK bbox).
    - 5 OK tornadoes in September (inside bbox, outside a May window).
    - 1 OK Hail event in May (inside bbox; different EVENT_TYPE).
    """
    rows = [_CSV_HEADER_FULL]
    eid = 5000
    # OK tornadoes, May. lon ~ -97.5, lat ~ 35.4 (inside OK bbox).
    for i in range(4):
        rows.append(_synth_event_row(
            eid, "Tornado", "OKLAHOMA", 2022, 5, 10 + i,
            35.4 + i * 0.02, -97.5 - i * 0.02, magnitude="EF2",
            deaths_direct=i,
        ))
        eid += 1
    # TX tornadoes, May. lat ~ 34.0, lon ~ -99.0 (inside OK bbox span).
    for i in range(3):
        rows.append(_synth_event_row(
            eid, "Tornado", "TEXAS", 2022, 5, 12 + i,
            34.0 + i * 0.05, -99.0 - i * 0.05,
        ))
        eid += 1
    # FL tornadoes, May. lat ~ 27.5, lon ~ -81.5 (OUTSIDE OK bbox).
    for i in range(2):
        rows.append(_synth_event_row(
            eid, "Tornado", "FLORIDA", 2022, 5, 20 + i,
            27.5 + i * 0.02, -81.5 - i * 0.02,
        ))
        eid += 1
    # OK tornadoes, September (inside bbox, outside May window).
    for i in range(5):
        rows.append(_synth_event_row(
            eid, "Tornado", "OKLAHOMA", 2022, 9, 5 + i,
            35.5 + i * 0.02, -97.6 - i * 0.02,
        ))
        eid += 1
    # OK Hail, May (inside bbox, different EVENT_TYPE).
    rows.append(_synth_event_row(
        eid, "Hail", "OKLAHOMA", 2022, 5, 15, 35.45, -97.55, magnitude="1.75",
    ))
    eid += 1
    return _csv_to_gzip_bytes("\n".join(rows) + "\n")


# OK-region bbox (W, S, E, N) spanning the OK + bordering TX rows above.
_OK_BBOX = (-103.0, 33.6, -94.4, 37.0)


def _read_fgb(fgb_bytes: bytes):
    import geopandas as gpd  # type: ignore[import-not-found]
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb_bytes)
        path = f.name
    try:
        return gpd.read_file(path, engine="pyogrio")
    finally:
        os.unlink(path)


def test_bbox_filter_keeps_only_points_inside_box():
    """A bbox keeps every event inside it and drops everything outside.

    The OK bbox spans OK + bordering TX tornadoes but excludes the FL rows;
    bbox is a SPATIAL filter, so it crosses state lines.
    """
    gz = _make_window_bbox_csv()
    fgb = _parse_filter_and_serialize(
        gz, state=None, event_types=["Tornado"], bbox=_OK_BBOX
    )
    gdf = _read_fgb(fgb)
    # 4 OK May + 3 TX May + 5 OK Sep = 12 tornadoes inside the box; 2 FL out.
    assert len(gdf) == 12
    assert set(gdf["STATE"].str.upper().unique()) == {"OKLAHOMA", "TEXAS"}
    west, south, east, north = _OK_BBOX
    for geom in gdf.geometry:
        assert west <= geom.x <= east, f"lon {geom.x} outside bbox"
        assert south <= geom.y <= north, f"lat {geom.y} outside bbox"


def test_bbox_excludes_florida_rows():
    """The FL tornadoes fall outside the OK bbox and must not appear."""
    gz = _make_window_bbox_csv()
    fgb = _parse_filter_and_serialize(
        gz, state=None, event_types=["Tornado"], bbox=_OK_BBOX
    )
    gdf = _read_fgb(fgb)
    assert "FLORIDA" not in set(gdf["STATE"].str.upper().unique())


def test_date_window_filter_narrows_to_may():
    """A May 2022 window keeps May rows and drops the September rows."""
    gz = _make_window_bbox_csv()
    fgb = _parse_filter_and_serialize(
        gz,
        state=None,
        event_types=["Tornado"],
        begin_date="2022-05-01",
        end_date="2022-05-31",
    )
    gdf = _read_fgb(fgb)
    # 4 OK May + 3 TX May + 2 FL May = 9 tornadoes in May; the 5 Sep dropped.
    assert len(gdf) == 9
    # Every retained row's BEGIN_DATE_TIME is in May ("-MAY-").
    assert gdf["BEGIN_DATE_TIME"].str.contains("-MAY-").all()


def test_bbox_and_window_combine():
    """bbox AND window AND event_type compose to the intersection."""
    gz = _make_window_bbox_csv()
    fgb = _parse_filter_and_serialize(
        gz,
        state=None,
        event_types=["Tornado"],
        bbox=_OK_BBOX,
        begin_date="2022-05-01",
        end_date="2022-05-31",
    )
    gdf = _read_fgb(fgb)
    # Inside box AND May AND Tornado = 4 OK + 3 TX = 7 (Sep + FL + Hail gone).
    assert len(gdf) == 7
    assert set(gdf["STATE"].str.upper().unique()) == {"OKLAHOMA", "TEXAS"}


def test_window_end_is_inclusive_bare_date():
    """A bare-date end_date includes events on that whole day."""
    gz = _make_window_bbox_csv()
    # The latest OK September row is day 9 (5..9). End on exactly 2022-09-09.
    fgb = _parse_filter_and_serialize(
        gz,
        state="OK",
        event_types=["Tornado"],
        begin_date="2022-09-01",
        end_date="2022-09-09",
    )
    gdf = _read_fgb(fgb)
    # 5 OK Sept tornadoes, days 5..9 — all inside [09-01, 09-09].
    assert len(gdf) == 5


def test_retained_columns_include_magnitude_and_deaths():
    """The output carries MAGNITUDE + DEATHS_DIRECT/DEATHS_INDIRECT."""
    gz = _make_window_bbox_csv()
    fgb = _parse_filter_and_serialize(
        gz, state="OK", event_types=["Tornado"],
        begin_date="2022-05-01", end_date="2022-05-31",
    )
    gdf = _read_fgb(fgb)
    for col in ("MAGNITUDE", "DEATHS_DIRECT", "DEATHS_INDIRECT"):
        assert col in gdf.columns, f"{col} missing from output"
    # The May OK tornadoes carry EF2 magnitude + an increasing death count.
    assert (gdf["MAGNITUDE"] == "EF2").all()
    assert sorted(gdf["DEATHS_DIRECT"].astype(int).tolist()) == [0, 1, 2, 3]


def test_empty_window_raises_typed_error():
    """A window with no matching events raises StormEventsEmptyError."""
    gz = _make_window_bbox_csv()
    with pytest.raises(StormEventsEmptyError):
        _parse_filter_and_serialize(
            gz,
            state=None,
            event_types=["Tornado"],
            begin_date="2022-12-01",
            end_date="2022-12-31",  # no December rows
        )


def test_bbox_with_no_events_inside_raises_empty():
    """A bbox over open ocean (no synthetic rows) raises StormEventsEmptyError."""
    gz = _make_window_bbox_csv()
    with pytest.raises(StormEventsEmptyError):
        _parse_filter_and_serialize(
            gz,
            state=None,
            event_types=["Tornado"],
            bbox=(-160.0, 0.0, -150.0, 10.0),  # mid-Pacific
        )


# ---------------------------------------------------------------------------
# Input-validation tests for the new params.
# ---------------------------------------------------------------------------


def test_validate_rejects_malformed_bbox():
    """A bad bbox raises StormEventsArgError (not retryable)."""
    for bad in [
        (1.0, 2.0, 3.0),               # wrong arity
        (-200.0, 0.0, 10.0, 10.0),     # lon out of range
        (-100.0, 40.0, -90.0, 30.0),   # south >= north
        (10.0, 0.0, -10.0, 10.0),      # west >= east
        "not-a-bbox",                  # wrong type
    ]:
        with pytest.raises(StormEventsArgError):
            _validate_bbox(bad)  # type: ignore[arg-type]


def test_validate_rejects_malformed_dates():
    """Non-ISO begin_date/end_date raise StormEventsArgError."""
    with pytest.raises(StormEventsArgError, match="begin_date"):
        _validate_inputs(year=2022, state=None, event_types=None,
                         begin_date="May 2022", end_date=None)
    with pytest.raises(StormEventsArgError, match="end_date"):
        _validate_inputs(year=2022, state=None, event_types=None,
                         begin_date=None, end_date="not-a-date")


def test_validate_rejects_reversed_window():
    """begin_date after end_date raises StormEventsArgError."""
    with pytest.raises(StormEventsArgError, match="after"):
        _validate_inputs(year=2022, state=None, event_types=None,
                         begin_date="2022-06-01", end_date="2022-05-01")


def test_validate_accepts_good_bbox_and_window():
    """A valid bbox + window passes validation cleanly (no raise)."""
    _validate_inputs(
        year=2022, state="OK", event_types=["Tornado"],
        bbox=_OK_BBOX, begin_date="2022-05-01", end_date="2022-05-31",
    )


# ---------------------------------------------------------------------------
# _window_years — multi-year spanning.
# ---------------------------------------------------------------------------


def test_window_years_single_year_default():
    """No window -> only the anchor year is fetched (backward compatible)."""
    assert _window_years(2022, None, None) == [2022]


def test_window_years_spans_year_boundary():
    """A window crossing New Year fetches both annual CSVs."""
    assert _window_years(2022, "2021-12-15", "2022-01-10") == [2021, 2022]


def test_window_years_spans_multiple_years():
    """A multi-year window fetches every annual CSV it touches."""
    assert _window_years(2022, "2020-06-01", "2023-03-01") == [
        2020, 2021, 2022, 2023
    ]


def test_window_years_only_begin_includes_anchor():
    """A begin-only window still includes the anchor year."""
    assert _window_years(2019, "2018-11-01", None) == [2018, 2019]


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_full_year_is_large():
    """A full national year estimate is in the tens of MB."""
    mb = estimate_payload_mb(year=2022)
    assert mb > 10.0, f"full-year estimate {mb} unexpectedly small"


def test_estimate_payload_mb_narrow_filter_is_small():
    """A single state + type + 1-month window shrinks the estimate sharply."""
    mb = estimate_payload_mb(
        year=2022, state="OK", event_types=["Tornado"],
        begin_date="2022-05-01", end_date="2022-05-31",
    )
    assert mb < estimate_payload_mb(year=2022)
    assert mb > 0.0


def test_estimate_payload_mb_scales_with_window_years():
    """A 3-year window estimates more than a single year."""
    one = estimate_payload_mb(year=2022)
    three = estimate_payload_mb(
        year=2022, begin_date="2020-01-01", end_date="2022-12-31"
    )
    assert three > one


# ---------------------------------------------------------------------------
# Cache-key tests for the new params.
# ---------------------------------------------------------------------------


def test_bbox_splits_cache_key():
    """Two different bboxes do NOT share a cache entry."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_fgb = b"fgb" + b"\x00" * 32 + b"_BBOX"

    def fake_fetch() -> bytes:
        fetch_count["n"] += 1
        return fake_fgb

    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db._fetch_storm_events_bytes",
        side_effect=lambda *a, **k: fake_fetch(),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_storm_events_db(
            year=2022, event_types=["Tornado"], bbox=(-103.0, 33.6, -94.4, 37.0)
        )
        r2 = fetch_storm_events_db(
            year=2022, event_types=["Tornado"], bbox=(-90.0, 30.0, -80.0, 35.0)
        )

    assert fetch_count["n"] == 2, "different bboxes must not share a cache key"
    assert r1.uri != r2.uri


def test_window_splits_cache_key_but_repeats_hit():
    """Same (year, type, window) hits cache; a different window misses."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_fgb = b"fgb" + b"\x00" * 32 + b"_WIN"

    def fake_fetch() -> bytes:
        fetch_count["n"] += 1
        return fake_fgb

    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db._fetch_storm_events_bytes",
        side_effect=lambda *a, **k: fake_fetch(),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_storm_events_db(
            year=2022, event_types=["Tornado"],
            begin_date="2022-05-01", end_date="2022-05-31",
        )
        r2 = fetch_storm_events_db(
            year=2022, event_types=["Tornado"],
            begin_date="2022-05-01", end_date="2022-05-31",
        )
        assert fetch_count["n"] == 1, "identical window must hit cache"
        r3 = fetch_storm_events_db(
            year=2022, event_types=["Tornado"],
            begin_date="2022-06-01", end_date="2022-06-30",
        )

    assert fetch_count["n"] == 2, "different window must miss cache"
    assert r1.uri == r2.uri
    assert r1.uri != r3.uri


def test_no_window_no_bbox_is_backward_compatible_cache_key():
    """Omitting bbox/window keeps the old (year,state,type) cache behavior."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_fgb = b"fgb" + b"\x00" * 32 + b"_COMPAT"

    def fake_fetch() -> bytes:
        fetch_count["n"] += 1
        return fake_fgb

    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db._fetch_storm_events_bytes",
        side_effect=lambda *a, **k: fake_fetch(),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_storm_events_db(year=2022, state="FL", event_types=["Hurricane"])
        r2 = fetch_storm_events_db(year=2022, state="FL", event_types=["Hurricane"])

    assert fetch_count["n"] == 1
    assert r1.uri == r2.uri


# ---------------------------------------------------------------------------
# Live test — only runs with TRID3NT_TEST_LIVE_STORM=1.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_STORM,
    reason="set TRID3NT_TEST_LIVE_STORM=1 to run live NOAA Storm Events test",
)
def test_live_fetch_2022_florida_hurricane(tmp_path):
    """Real NOAA fetch for year=2022, state='FL', event_types=['Hurricane']
    returns a non-empty FlatGeobuf with at least 1 Hurricane Ian-shape point
    inside Florida's bounding box.
    """
    # Inject an in-memory fake GCS so we don't need real cache-bucket creds
    # for the live upstream test. The fetch path is fully real.
    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_storm_events_db.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_storm_events_db(
            year=2022, state="FL", event_types=["Hurricane"]
        )

    # Persist the FGB bytes locally so the test can inspect them.
    assert len(fake_gcs.store) == 1
    fgb_bytes = next(iter(fake_gcs.store.values()))
    fgb_path = tmp_path / "live_storm.fgb"
    fgb_path.write_bytes(fgb_bytes)

    import geopandas as gpd  # type: ignore[import-not-found]
    gdf = gpd.read_file(str(fgb_path), engine="pyogrio")
    assert len(gdf) > 0, "live fetch returned 0 features"
    assert gdf.crs.to_epsg() == 4326
    # Geography correctness: all points inside Florida envelope (generous).
    fl_min_lon, fl_min_lat, fl_max_lon, fl_max_lat = -87.6, 24.0, -79.5, 31.5
    for geom in gdf.geometry:
        assert fl_min_lon <= geom.x <= fl_max_lon, (
            f"live point lon {geom.x} outside Florida"
        )
        assert fl_min_lat <= geom.y <= fl_max_lat, (
            f"live point lat {geom.y} outside Florida"
        )
    print(
        f"\nlive_test: {len(gdf)} Hurricane events in FL 2022; "
        f"first row: {gdf.iloc[0].to_dict()}"
    )
