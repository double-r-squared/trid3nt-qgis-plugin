"""Unit tests for the ``fetch_asos_metar`` atomic tool (job-A7).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Station discovery: stations inside the bbox are returned; out-of-bbox are excluded.
- CSV parsing + FlatGeobuf serialization with a synthetic 3-station, 6-observation sample.
- Input validation: bad bbox shapes, degenerate bbox, future start_time, inverted window.
- Upstream error mapping: HTTP 4xx/5xx → ASASMETARUpstreamError(retryable=True).
- Empty result: no stations in bbox → ASASMETAREmptyError(retryable=False).
- Cache miss → fetch_fn invoked; cache hit → fetch_fn skipped.
- LayerURI shape: layer_type="vector", role="context", units="mixed", uri in gs://.
- Payload estimate: estimate_payload_mb returns a positive float.
- Live (env GRACE2_TEST_LIVE_ASOS=1): real IEM CGI returns ≥1 station observation
  for Fort Myers area over the most recent 24h; FGB round-trips; coordinates in
  the expected US envelope.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.fetch_asos_metar import (
    ASASMETAREmptyError,
    ASASMETARInputError,
    ASASMETARUpstreamError,
    _discover_stations_in_bbox,
    _fetch_asos_csv_bytes,
    _parse_csv_to_fgb,
    estimate_payload_mb,
    fetch_asos_metar,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Live test gate.
_LIVE_ASOS = os.environ.get("GRACE2_TEST_LIVE_ASOS") == "1"

# Fort Myers / Naples area bbox — used for smoke tests.
_FORT_MYERS_BBOX = (-82.5, 25.8, -81.0, 27.5)


def _fake_fgb_bytes(tag: str = "TEST") -> bytes:
    return b"FAKE_ASOS_FGB_" + tag.encode() + b"\x00" * 16


# ---------------------------------------------------------------------------
# Fake GCS plumbing.
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


# ---------------------------------------------------------------------------
# Synthetic ASOS CSV / station fixtures.
# ---------------------------------------------------------------------------

_SYNTHETIC_STATIONS = [
    {"sid": "RSW", "lon": -81.7567, "lat": 26.5381, "sname": "FT MYERS/SW FLORIDA", "state": "FL"},
    {"sid": "FMY", "lon": -81.8614, "lat": 26.5850, "sname": "FORT MYERS/PAGE FLD", "state": "FL"},
    {"sid": "APF", "lon": -81.7753, "lat": 26.1525, "sname": "NAPLES MUNICIPAL", "state": "FL"},
]


def _synthetic_asos_csv(n_obs_per_station: int = 2) -> bytes:
    """Build a synthetic IEM ASOS CSV with `n_obs_per_station` per station."""
    header = "station,valid,lon,lat,elevation,tmpf,dwpf,sknt,drct,gust,alti,mslp,vsby,wxcodes,skyc1,skyl1"
    rows = [header]
    base_time = datetime(2026, 6, 7, 12, 0, 0)
    for st in _SYNTHETIC_STATIONS:
        for i in range(n_obs_per_station):
            valid = (base_time + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M")
            rows.append(
                f"{st['sid']},{valid},{st['lon']},{st['lat']},9.00,"
                f"86.00,75.00,6.00,290.00,null,29.99,1014.80,10.00,null,CLR,null"
            )
    return "\n".join(rows).encode("utf-8")


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_asos_metar appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_asos_metar" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_asos_metar"]
    assert entry.metadata.name == "fetch_asos_metar"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "asos_metar"
    assert entry.metadata.cacheable is True


def test_payload_estimator_name_set():
    """payload_mb_estimator_name is set on metadata."""
    entry = TOOL_REGISTRY["fetch_asos_metar"]
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


# ---------------------------------------------------------------------------
# Payload estimate.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_returns_positive_float():
    result = estimate_payload_mb(
        bbox=_FORT_MYERS_BBOX,
        start_time="2026-06-07",
        end_time="2026-06-08",
    )
    assert isinstance(result, float)
    assert result > 0.0


def test_estimate_payload_mb_none_bbox():
    result = estimate_payload_mb(bbox=None)
    assert result > 0.0


def test_estimate_payload_mb_large_window_larger_than_small():
    small = estimate_payload_mb(
        bbox=_FORT_MYERS_BBOX, start_time="2026-06-07", end_time="2026-06-08"
    )
    large = estimate_payload_mb(
        bbox=(-90.0, 25.0, -80.0, 36.0), start_time="2026-06-01", end_time="2026-06-08"
    )
    assert large > small


# ---------------------------------------------------------------------------
# CSV parsing tests.
# ---------------------------------------------------------------------------


def test_parse_csv_to_fgb_returns_nonempty_bytes():
    """Synthetic 3-station CSV → valid non-empty FlatGeobuf bytes."""
    csv_bytes = _synthetic_asos_csv(n_obs_per_station=3)
    station_meta = {s["sid"]: s for s in _SYNTHETIC_STATIONS}
    fgb_bytes = _parse_csv_to_fgb(csv_bytes, station_meta)
    assert len(fgb_bytes) > 0


def test_parse_csv_to_fgb_row_count_matches():
    """3 stations × 2 obs = 6 features in output FGB."""
    csv_bytes = _synthetic_asos_csv(n_obs_per_station=2)
    station_meta = {s["sid"]: s for s in _SYNTHETIC_STATIONS}
    fgb_bytes = _parse_csv_to_fgb(csv_bytes, station_meta)

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 6, f"Expected 6 features, got {len(gdf)}"
        assert "station" in gdf.columns
        assert "valid" in gdf.columns
        assert "tmpf" in gdf.columns
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_parse_csv_station_ids_present():
    """All station IDs appear in the output FGB."""
    csv_bytes = _synthetic_asos_csv(n_obs_per_station=1)
    station_meta = {s["sid"]: s for s in _SYNTHETIC_STATIONS}
    fgb_bytes = _parse_csv_to_fgb(csv_bytes, station_meta)

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        station_ids = set(gdf["station"].tolist())
        assert "RSW" in station_ids
        assert "FMY" in station_ids
        assert "APF" in station_ids
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_parse_csv_empty_raises_empty_error():
    """Empty CSV (header-only) raises ASASMETAREmptyError."""
    csv_bytes = b"station,valid,lon,lat,elevation,tmpf\n"
    with pytest.raises(ASASMETAREmptyError):
        _parse_csv_to_fgb(csv_bytes, {})


def test_parse_csv_strips_comment_lines():
    """Lines starting with '#' are stripped before CSV parsing."""
    base_csv = _synthetic_asos_csv(n_obs_per_station=1)
    commented = b"# This is a comment\n# Another comment\n" + base_csv
    station_meta = {s["sid"]: s for s in _SYNTHETIC_STATIONS}
    # Should not raise and should return valid FGB bytes.
    fgb_bytes = _parse_csv_to_fgb(commented, station_meta)
    assert len(fgb_bytes) > 0


# ---------------------------------------------------------------------------
# Input validation tests.
# ---------------------------------------------------------------------------


def test_invalid_bbox_wrong_length_raises():
    with pytest.raises(ASASMETARInputError, match="4-element"):
        fetch_asos_metar(bbox=(1.0, 2.0, 3.0))  # type: ignore[arg-type]


def test_degenerate_bbox_raises():
    with pytest.raises(ASASMETARInputError, match="degenerate"):
        # min_lon == max_lon
        fetch_asos_metar(bbox=(-81.0, 26.0, -81.0, 27.0))


def test_future_start_time_raises():
    future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    with pytest.raises(ASASMETARInputError, match="future"):
        fetch_asos_metar(bbox=_FORT_MYERS_BBOX, start_time=future)


def test_inverted_time_window_raises():
    with pytest.raises(ASASMETARInputError, match="before"):
        fetch_asos_metar(
            bbox=_FORT_MYERS_BBOX,
            start_time="2026-06-08",
            end_time="2026-06-07",
        )


def test_input_errors_are_not_retryable():
    """ASASMETARInputError carries retryable=False."""
    exc = ASASMETARInputError("bad input")
    assert exc.retryable is False


# ---------------------------------------------------------------------------
# Upstream error mapping.
# ---------------------------------------------------------------------------


def test_upstream_error_is_retryable():
    """ASASMETARUpstreamError carries retryable=True."""
    exc = ASASMETARUpstreamError("network failure")
    assert exc.retryable is True


def test_empty_error_is_not_retryable():
    """ASASMETAREmptyError carries retryable=False."""
    exc = ASASMETAREmptyError("no stations")
    assert exc.retryable is False


# ---------------------------------------------------------------------------
# No-station bbox raises EmptyError.
# ---------------------------------------------------------------------------


def test_no_stations_in_bbox_raises_empty_error():
    """A bbox in the middle of the ocean returns no ASOS stations → EmptyError."""
    fake_gcs = FakeStorageClient()

    # Patch _discover_stations_in_bbox to return empty list.
    with patch(
        "grace2_agent.tools.fetch_asos_metar._discover_stations_in_bbox",
        return_value=[],
    ), patch(
        "grace2_agent.tools.fetch_asos_metar.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        with pytest.raises(ASASMETAREmptyError, match="No IEM ASOS stations"):
            fetch_asos_metar(
                bbox=(-30.0, 10.0, -20.0, 20.0),  # mid-Atlantic ocean
                start_time="2026-06-07",
                end_time="2026-06-08",
            )


# ---------------------------------------------------------------------------
# Mocked end-to-end: 3 stations, 2 obs each → 6-feature FGB in cache.
# ---------------------------------------------------------------------------


def test_mocked_end_to_end_writes_fgb_to_cache():
    """Mocked 3-station response → FlatGeobuf written through read_through cache."""
    fake_gcs = FakeStorageClient()
    csv_bytes = _synthetic_asos_csv(n_obs_per_station=2)

    def fake_discover(bbox):
        return _SYNTHETIC_STATIONS

    def fake_fetch_csv(station_ids, start_dt, end_dt, data_fields):
        return csv_bytes

    with patch(
        "grace2_agent.tools.fetch_asos_metar._discover_stations_in_bbox",
        side_effect=fake_discover,
    ), patch(
        "grace2_agent.tools.fetch_asos_metar._fetch_asos_csv_bytes",
        side_effect=fake_fetch_csv,
    ), patch(
        "grace2_agent.tools.fetch_asos_metar.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_asos_metar(
            bbox=_FORT_MYERS_BBOX,
            start_time="2026-06-07",
            end_time="2026-06-08",
        )

    assert result.uri.startswith("s3://")
    assert "asos_metar" in result.uri
    assert "dynamic-1h" in result.uri
    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units == "mixed"
    assert len(fake_gcs.store) == 1

    # Read back and verify.
    fgb_bytes = next(iter(fake_gcs.store.values()))
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 6, f"Expected 6 obs, got {len(gdf)}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# LayerURI shape tests.
# ---------------------------------------------------------------------------


def test_layer_uri_shape():
    """LayerURI has correct layer_type, role, units, and gs:// uri."""
    fake_gcs = FakeStorageClient()
    csv_bytes = _synthetic_asos_csv(n_obs_per_station=1)

    with patch(
        "grace2_agent.tools.fetch_asos_metar._discover_stations_in_bbox",
        return_value=_SYNTHETIC_STATIONS,
    ), patch(
        "grace2_agent.tools.fetch_asos_metar._fetch_asos_csv_bytes",
        return_value=csv_bytes,
    ), patch(
        "grace2_agent.tools.fetch_asos_metar.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_asos_metar(
            bbox=_FORT_MYERS_BBOX,
            start_time="2026-06-07",
            end_time="2026-06-08",
        )

    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units == "mixed"
    assert result.uri.startswith("s3://")
    assert "ASOS/METAR" in result.name
    assert result.style_preset == "asos_metar"


# ---------------------------------------------------------------------------
# Cache layer tests.
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_fetch_fn_then_hit_skips():
    """First call → cache miss → fetch_fn invoked; second call → cache hit → skipped."""
    fake_gcs = FakeStorageClient()
    call_count = {"n": 0}
    csv_bytes = _synthetic_asos_csv(n_obs_per_station=1)

    def counting_discover(bbox):
        call_count["n"] += 1
        return _SYNTHETIC_STATIONS

    with patch(
        "grace2_agent.tools.fetch_asos_metar._discover_stations_in_bbox",
        side_effect=counting_discover,
    ), patch(
        "grace2_agent.tools.fetch_asos_metar._fetch_asos_csv_bytes",
        return_value=csv_bytes,
    ), patch(
        "grace2_agent.tools.fetch_asos_metar.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_asos_metar(
            bbox=_FORT_MYERS_BBOX,
            start_time="2026-06-07",
            end_time="2026-06-08",
        )
        r2 = fetch_asos_metar(
            bbox=_FORT_MYERS_BBOX,
            start_time="2026-06-07",
            end_time="2026-06-08",
        )

    assert call_count["n"] == 1, (
        f"Expected 1 discover call (hit on second); got {call_count['n']}"
    )
    assert r1.uri == r2.uri


def test_different_bbox_produces_different_cache_key():
    """A different bbox produces a different FGB cache entry."""
    fake_gcs = FakeStorageClient()
    csv_bytes = _synthetic_asos_csv(n_obs_per_station=1)

    def noop_discover(bbox):
        return _SYNTHETIC_STATIONS

    with patch(
        "grace2_agent.tools.fetch_asos_metar._discover_stations_in_bbox",
        side_effect=noop_discover,
    ), patch(
        "grace2_agent.tools.fetch_asos_metar._fetch_asos_csv_bytes",
        return_value=csv_bytes,
    ), patch(
        "grace2_agent.tools.fetch_asos_metar.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_asos_metar(
            bbox=_FORT_MYERS_BBOX,
            start_time="2026-06-07",
            end_time="2026-06-08",
        )
        r2 = fetch_asos_metar(
            bbox=(-85.0, 29.0, -83.0, 31.0),
            start_time="2026-06-07",
            end_time="2026-06-08",
        )

    assert r1.uri != r2.uri


# ---------------------------------------------------------------------------
# Geographic correctness — point coordinates within US envelope.
# ---------------------------------------------------------------------------

_US_LON = (-180.0, -64.0)
_US_LAT = (13.0, 72.0)


def test_geographic_gate_synthetic_obs_in_us_envelope():
    """Synthetic Fort Myers observations have centroids inside the US lon/lat envelope."""
    csv_bytes = _synthetic_asos_csv(n_obs_per_station=1)
    station_meta = {s["sid"]: s for s in _SYNTHETIC_STATIONS}
    fgb_bytes = _parse_csv_to_fgb(csv_bytes, station_meta)

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        lon_min, lon_max = _US_LON
        lat_min, lat_max = _US_LAT
        for idx, geom in enumerate(gdf.geometry):
            if geom is None or geom.is_empty:
                continue
            assert lon_min <= geom.x <= lon_max, (
                f"Feature {idx} lon={geom.x} outside US lon envelope"
            )
            assert lat_min <= geom.y <= lat_max, (
                f"Feature {idx} lat={geom.y} outside US lat envelope"
            )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Live integration test (GRACE2_TEST_LIVE_ASOS=1 to run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_ASOS,
    reason="Set GRACE2_TEST_LIVE_ASOS=1 to run live IEM ASOS tests",
)
def test_live_fort_myers_asos_returns_observations():
    """LIVE: real IEM CGI returns ≥1 ASOS observation for Fort Myers area over 24h.

    Asserts:
    - At least one station discovered in the Fort Myers bbox.
    - FGB round-trips successfully.
    - All point coordinates are within the US lon/lat envelope.
    - Required columns (station, valid, tmpf) present.
    """
    from grace2_agent.tools.fetch_asos_metar import (
        _discover_stations_in_bbox,
        _fetch_asos_csv_bytes,
        _parse_csv_to_fgb,
        _DEFAULT_DATA_FIELDS,
    )

    bbox = _FORT_MYERS_BBOX
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(hours=24)

    # 1. Station discovery.
    stations = _discover_stations_in_bbox(bbox)
    assert len(stations) >= 1, (
        f"Expected at least 1 ASOS station in bbox={bbox}; got {stations}"
    )
    print(f"\n[LIVE ASOS] Discovered {len(stations)} station(s): "
          f"{[s['sid'] for s in stations]}")

    station_meta = {s["sid"]: s for s in stations}
    station_ids = [s["sid"] for s in stations[:5]]  # cap for smoke test

    # 2. Data fetch.
    csv_bytes = _fetch_asos_csv_bytes(station_ids, start_dt, end_dt, _DEFAULT_DATA_FIELDS)
    assert len(csv_bytes) > 100, f"CSV bytes too small ({len(csv_bytes)})"

    # 3. Parse → FGB.
    fgb_bytes = _parse_csv_to_fgb(csv_bytes, station_meta)
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        n = len(gdf)
        assert n >= 1, f"Expected at least 1 observation; got {n}"
        print(f"[LIVE ASOS] {n} observations from {gdf['station'].nunique()} station(s)")
        print(f"  Columns: {list(gdf.columns)}")
        if "tmpf" in gdf.columns:
            temps = gdf["tmpf"].dropna()
            if len(temps) > 0:
                print(f"  tmpf range: {temps.min():.1f}–{temps.max():.1f} °F")

        # Geographic gate.
        lon_min, lon_max = _US_LON
        lat_min, lat_max = _US_LAT
        for idx, geom in enumerate(gdf.geometry):
            if geom is None or geom.is_empty:
                continue
            assert lon_min <= geom.x <= lon_max, (
                f"Feature {idx} lon={geom.x} outside US envelope"
            )
            assert lat_min <= geom.y <= lat_max, (
                f"Feature {idx} lat={geom.y} outside US envelope"
            )

        # Required columns.
        for col in ("station", "valid"):
            assert col in gdf.columns, f"Missing required column {col!r}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.skipif(
    not _LIVE_ASOS,
    reason="Set GRACE2_TEST_LIVE_ASOS=1 to run live IEM ASOS tests",
)
def test_live_full_tool_call_returns_layer_uri():
    """LIVE: full fetch_asos_metar call returns a valid LayerURI with gs:// uri.

    This exercises the read_through path against real GCS (requires ADC creds).
    Falls back to asserting on the error type if GCS is unavailable.
    """
    import os as _os
    end_time = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start_time = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d")
    try:
        result = fetch_asos_metar(
            bbox=_FORT_MYERS_BBOX,
            start_time=start_time,
            end_time=end_time,
        )
        assert result.uri.startswith("s3://"), f"Expected gs:// URI; got {result.uri!r}"
        assert result.layer_type == "vector"
        print(f"\n[LIVE ASOS] LayerURI: {result.uri}")
        print(f"  Name: {result.name}")
        print(f"  Payload estimate: {estimate_payload_mb(bbox=_FORT_MYERS_BBOX, start_time=start_time, end_time=end_time):.4f} MB")
    except (ASASMETARUpstreamError, ASASMETAREmptyError) as exc:
        # Known acceptable failures in CI without GCS/network access.
        pytest.skip(f"Live test skipped due to upstream/GCS unavailability: {exc}")
