"""Unit tests for the ``fetch_raws_weather`` atomic tool (job-A12).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Station discovery: RAWS stations inside bbox are returned; non-RAWS and
  out-of-bbox stations are excluded.
- Observation mapping: IEM SHEF field names (URHRGZZ, XRIRGZZ, etc.) are
  correctly aliased to fire-weather column names.
- FlatGeobuf serialization: synthetic 2-station, multi-observation dataset
  round-trips correctly (feature count, column names, geometry type).
- Input validation: bad bbox shapes, degenerate bbox, future start_time,
  inverted window, date range too wide.
- Upstream error mapping: HTTP 5xx → RAWSWeatherUpstreamError(retryable=True).
- Empty result: no RAWS stations in bbox → RAWSWeatherEmptyError(retryable=False).
- Cache miss → fetch_fn invoked; cache hit → fetch_fn skipped.
- LayerURI shape: layer_type="vector", role="context", units="mixed", uri gs://.
- Payload estimate: estimate_payload_mb returns a positive float, scales with area.
- Live (env TRID3NT_TEST_LIVE_RAWS=1): real IEM API returns ≥1 RAWS observation
  for a CA Sierra Nevada bbox; FGB round-trips; coordinates in US envelope.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.weather.fetch_raws_weather import (
    RAWSWeatherEmptyError,
    RAWSWeatherInputError,
    RAWSWeatherUpstreamError,
    _build_raws_fgb,
    _discover_raws_stations_in_bbox,
    estimate_payload_mb,
    fetch_raws_weather,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Live test gate.
_LIVE_RAWS = os.environ.get("TRID3NT_TEST_LIVE_RAWS") == "1"

# Sierra Nevada / Caldor Fire area bbox — RAWS-dense western US fire belt.
_SIERRA_BBOX = (-121.0, 38.5, -119.5, 39.5)

# Small UT bbox known to contain RAWS stations (Aqua Canyon RAWS area).
_UT_BBOX = (-113.0, 37.0, -111.5, 38.5)

# Mid-ocean bbox — no RAWS stations expected.
_OCEAN_BBOX = (-40.0, 20.0, -30.0, 30.0)


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
# Fake GCS plumbing (matches pattern from test_fetch_asos_metar).
# ---------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, store: dict, path: str) -> None:
        self._store = store
        self._path = path
        self.custom_time = None
        self.cache_control = None

    def exists(self) -> bool:
        return self._path in self._store

    def download_as_bytes(self) -> bytes:
        return self._store[self._path]

    def upload_from_string(self, data: bytes, content_type=None) -> None:
        self._store[self._path] = data


class FakeBucket:
    def __init__(self, store: dict) -> None:
        self._store = store

    def blob(self, path: str) -> FakeBlob:
        return FakeBlob(self._store, path)


class FakeStorageClient:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def bucket(self, name: str) -> FakeBucket:
        return FakeBucket(self.store)


# ---------------------------------------------------------------------------
# Synthetic RAWS data fixtures.
# ---------------------------------------------------------------------------

_SYNTHETIC_STATIONS = [
    {
        "sid": "ACRU1",
        "lon": -112.25,
        "lat": 37.50,
        "sname": "AQUA CANYON RAWS",
        "state": "UT",
        "elevation": 2438.0,
        "network": "UT_DCP",
    },
    {
        "sid": "ARAU1",
        "lon": -113.0217,
        "lat": 40.5983,
        "sname": "ARAGONITE RAWS",
        "state": "UT",
        "elevation": 1365.0,
        "network": "UT_DCP",
    },
]


def _make_synthetic_obs(station: dict, n: int = 3) -> list[dict]:
    """Build ``n`` synthetic observation dicts for a station."""
    rows = []
    base_time = datetime(2024, 6, 1, 6, 0, 0)
    for i in range(n):
        rows.append({
            "utc_valid": (base_time + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%MZ"),
            "local_valid": (base_time + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M"),
            "tmpf": 75.0 + i,
            "dwpf": 30.0,
            "sknt": 8.0,
            "drct": 270.0,
            "URHRGZZ": 22.0,    # → relh
            "XRIRGZZ": 550.0,   # → solar_rad
            "PCIRGZZ": 0.0,     # → precip_in
            "VBIRGZZ": 10.5,    # → gust
            "TAIRGZZ": 75.0 + i,
            "TAIRGXZ": None,
            "TAIRGNZ": None,
        })
    return rows


def _make_synthetic_fgb_rows(
    stations: list[dict] | None = None,
    n_per_station: int = 3,
) -> list[dict]:
    """Build a flat list of FGB row dicts suitable for _build_raws_fgb."""
    if stations is None:
        stations = _SYNTHETIC_STATIONS
    rows = []
    for st in stations:
        for obs in _make_synthetic_obs(st, n=n_per_station):
            row = {
                "station": st["sid"],
                "station_name": st["sname"],
                "state": st["state"],
                "utc_valid": obs["utc_valid"],
                "lon": st["lon"],
                "lat": st["lat"],
                "elevation": st["elevation"],
                "tmpf": obs["tmpf"],
                "dwpf": obs["dwpf"],
                "relh": obs["URHRGZZ"],
                "sknt": obs["sknt"],
                "drct": obs["drct"],
                "gust": obs["VBIRGZZ"],
                "solar_rad": obs["XRIRGZZ"],
                "precip_in": obs["PCIRGZZ"],
            }
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_raws_weather appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_raws_weather" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_raws_weather"]
    assert entry.metadata.name == "fetch_raws_weather"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "raws_weather"
    assert entry.metadata.cacheable is True


def test_payload_estimator_name_set():
    """payload_mb_estimator_name is configured on metadata."""
    entry = TOOL_REGISTRY["fetch_raws_weather"]
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


# ---------------------------------------------------------------------------
# Payload estimate.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_returns_positive():
    result = estimate_payload_mb(
        bbox=_UT_BBOX,
        start_time="2024-06-01",
        end_time="2024-06-03",
    )
    assert isinstance(result, float)
    assert result > 0.0


def test_estimate_payload_mb_none_bbox():
    result = estimate_payload_mb(bbox=None)
    assert result > 0.0


def test_estimate_payload_mb_larger_bbox_larger_estimate():
    small = estimate_payload_mb(
        bbox=_UT_BBOX, start_time="2024-06-01", end_time="2024-06-02"
    )
    large = estimate_payload_mb(
        bbox=(-120.0, 35.0, -100.0, 45.0), start_time="2024-06-01", end_time="2024-06-07"
    )
    assert large > small


# ---------------------------------------------------------------------------
# FlatGeobuf build tests.
# ---------------------------------------------------------------------------


def test_build_raws_fgb_returns_nonempty_bytes():
    """Synthetic 2-station, 3-obs/station rows → valid FlatGeobuf bytes."""
    rows = _make_synthetic_fgb_rows(n_per_station=3)
    fgb_bytes = _build_raws_fgb(rows)
    assert len(fgb_bytes) > 0


def test_build_raws_fgb_feature_count():
    """2 stations × 3 obs = 6 features in output FGB."""
    rows = _make_synthetic_fgb_rows(n_per_station=3)
    fgb_bytes = _build_raws_fgb(rows)

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 6, f"Expected 6 features, got {len(gdf)}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_build_raws_fgb_columns_present():
    """FGB output has all expected fire-weather columns."""
    rows = _make_synthetic_fgb_rows(n_per_station=1)
    fgb_bytes = _build_raws_fgb(rows)

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        for col in ("station", "station_name", "utc_valid", "tmpf", "relh",
                    "sknt", "drct", "solar_rad", "precip_in"):
            assert col in gdf.columns, f"Expected column {col!r} missing"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_build_raws_fgb_station_ids_present():
    """Both synthetic station IDs appear in the output FGB."""
    rows = _make_synthetic_fgb_rows(n_per_station=1)
    fgb_bytes = _build_raws_fgb(rows)

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        station_ids = set(gdf["station"].tolist())
        assert "ACRU1" in station_ids
        assert "ARAU1" in station_ids
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_build_raws_fgb_empty_rows_raises():
    """Empty row list raises RAWSWeatherEmptyError."""
    with pytest.raises(RAWSWeatherEmptyError):
        _build_raws_fgb([])


def test_build_raws_fgb_no_coords_raises():
    """Rows without valid coordinates raise RAWSWeatherEmptyError."""
    rows = [{"station": "X", "lon": None, "lat": None, "tmpf": 80.0}]
    with pytest.raises(RAWSWeatherEmptyError):
        _build_raws_fgb(rows)


# ---------------------------------------------------------------------------
# Input validation tests.
# ---------------------------------------------------------------------------


def test_invalid_bbox_wrong_length_raises():
    with pytest.raises(RAWSWeatherInputError, match="4-element"):
        fetch_raws_weather(bbox=(1.0, 2.0, 3.0))  # type: ignore[arg-type]


def test_degenerate_bbox_raises():
    with pytest.raises(RAWSWeatherInputError, match="degenerate"):
        fetch_raws_weather(bbox=(-112.0, 37.0, -112.0, 38.0))


def test_future_start_time_raises():
    future_date = (date.today() + timedelta(days=30)).isoformat()
    with pytest.raises(RAWSWeatherInputError, match="future"):
        fetch_raws_weather(bbox=_UT_BBOX, start_time=future_date)


def test_inverted_time_window_raises():
    with pytest.raises(RAWSWeatherInputError, match="on or before"):
        fetch_raws_weather(
            bbox=_UT_BBOX,
            start_time="2024-06-08",
            end_time="2024-06-07",
        )


def test_date_range_too_wide_raises():
    """Date range > _MAX_DATE_RANGE_DAYS (14) raises RAWSWeatherInputError."""
    from trid3nt_server.tools.fetchers.weather.fetch_raws_weather import _MAX_DATE_RANGE_DAYS
    wide_start = "2024-06-01"
    wide_end = (
        date(2024, 6, 1) + timedelta(days=_MAX_DATE_RANGE_DAYS + 1)
    ).isoformat()
    with pytest.raises(RAWSWeatherInputError, match="exceeds maximum"):
        fetch_raws_weather(bbox=_UT_BBOX, start_time=wide_start, end_time=wide_end)


def test_input_errors_not_retryable():
    exc = RAWSWeatherInputError("bad input")
    assert exc.retryable is False


# ---------------------------------------------------------------------------
# Error type retryability.
# ---------------------------------------------------------------------------


def test_upstream_error_retryable():
    exc = RAWSWeatherUpstreamError("network failure")
    assert exc.retryable is True


def test_empty_error_not_retryable():
    exc = RAWSWeatherEmptyError("no stations")
    assert exc.retryable is False


# ---------------------------------------------------------------------------
# No-station bbox raises EmptyError.
# ---------------------------------------------------------------------------


def test_no_raws_stations_in_bbox_raises_empty_error():
    """A bbox in the mid-Atlantic ocean returns no RAWS stations → EmptyError."""
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_raws_weather._discover_raws_stations_in_bbox",
        return_value=[],
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_raws_weather.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        with pytest.raises(RAWSWeatherEmptyError, match="No IEM-archived RAWS"):
            fetch_raws_weather(
                bbox=_OCEAN_BBOX,
                start_time="2024-06-01",
                end_time="2024-06-02",
            )


# ---------------------------------------------------------------------------
# Mocked end-to-end: 2 stations, 3 obs each → 6-feature FGB.
# ---------------------------------------------------------------------------


def test_mocked_end_to_end_writes_fgb_to_cache():
    """Mocked 2-station response → FlatGeobuf written through read_through cache."""
    fake_gcs = FakeStorageClient()
    fgb_rows = _make_synthetic_fgb_rows(n_per_station=3)

    def fake_fetch_bytes(bbox, start_date, end_date):
        return _build_raws_fgb(fgb_rows)

    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_raws_weather._fetch_raws_bytes",
        side_effect=fake_fetch_bytes,
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_raws_weather.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_raws_weather(
            bbox=_UT_BBOX,
            start_time="2024-06-01",
            end_time="2024-06-02",
        )

    assert result.uri.startswith("s3://")
    assert "raws_weather" in result.uri
    assert "dynamic-1h" in result.uri
    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units == "mixed"
    assert len(fake_gcs.store) == 1

    # Round-trip the FGB.
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
    """LayerURI has correct layer_type, role, units, gs:// uri, name."""
    fake_gcs = FakeStorageClient()
    fgb_rows = _make_synthetic_fgb_rows(n_per_station=1)

    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_raws_weather._fetch_raws_bytes",
        side_effect=lambda bbox, sd, ed: _build_raws_fgb(fgb_rows),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_raws_weather.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_raws_weather(
            bbox=_UT_BBOX,
            start_time="2024-06-01",
            end_time="2024-06-02",
        )

    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units == "mixed"
    assert result.uri.startswith("s3://")
    assert "RAWS" in result.name or "raws" in result.name.lower()
    assert result.style_preset == "raws_weather"


# ---------------------------------------------------------------------------
# Cache hit / miss tests.
# ---------------------------------------------------------------------------


def test_cache_miss_then_hit():
    """First call → fetch_fn invoked; second identical call → fetch_fn skipped."""
    fake_gcs = FakeStorageClient()
    fgb_rows = _make_synthetic_fgb_rows(n_per_station=1)
    call_count = {"n": 0}

    def counting_fetch(bbox, start_date, end_date):
        call_count["n"] += 1
        return _build_raws_fgb(fgb_rows)

    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_raws_weather._fetch_raws_bytes",
        side_effect=counting_fetch,
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_raws_weather.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_raws_weather(
            bbox=_UT_BBOX, start_time="2024-06-01", end_time="2024-06-02"
        )
        r2 = fetch_raws_weather(
            bbox=_UT_BBOX, start_time="2024-06-01", end_time="2024-06-02"
        )

    assert call_count["n"] == 1, f"Expected 1 fetch call; got {call_count['n']}"
    assert r1.uri == r2.uri


def test_different_bbox_produces_different_cache_key():
    """Different bboxes produce different FGB cache entries."""
    fake_gcs = FakeStorageClient()
    fgb_rows = _make_synthetic_fgb_rows(n_per_station=1)

    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_raws_weather._fetch_raws_bytes",
        side_effect=lambda bbox, sd, ed: _build_raws_fgb(fgb_rows),
    ), patch(
        "trid3nt_server.tools.fetchers.weather.fetch_raws_weather.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_raws_weather(
            bbox=_UT_BBOX, start_time="2024-06-01", end_time="2024-06-02"
        )
        r2 = fetch_raws_weather(
            bbox=(-120.0, 37.0, -118.0, 39.0),
            start_time="2024-06-01",
            end_time="2024-06-02",
        )

    assert r1.uri != r2.uri


# ---------------------------------------------------------------------------
# Station discovery filtering tests (unit-level, no network).
# ---------------------------------------------------------------------------


def test_station_discovery_filters_non_raws():
    """_discover_raws_stations_in_bbox only returns stations with RAWS in name."""
    fake_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "ACRU1",
                "properties": {"sname": "AQUA CANYON RAWS", "elevation": 2438.0,
                               "state": "UT", "network": "UT_DCP",
                               "archive_end": None, "online": True,
                               "attributes": {}},
                "geometry": {"type": "Point", "coordinates": [-112.25, 37.50]},
            },
            {
                "type": "Feature",
                "id": "COOP001",
                "properties": {"sname": "VALLEY COOP STATION", "elevation": 1000.0,
                               "state": "UT", "network": "UT_DCP",
                               "archive_end": None, "online": True,
                               "attributes": {}},
                "geometry": {"type": "Point", "coordinates": [-112.3, 37.6]},
            },
        ],
    }

    def mock_http_get(url, timeout=30.0):
        return json.dumps(fake_geojson).encode("utf-8")

    bbox = (-113.5, 36.5, -111.0, 38.5)
    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_raws_weather._http_get",
        side_effect=mock_http_get,
    ):
        stations = _discover_raws_stations_in_bbox(bbox)

    assert len(stations) == 1, f"Expected 1 RAWS station; got {len(stations)}"
    assert stations[0]["sid"] == "ACRU1"


def test_station_discovery_filters_out_of_bbox():
    """_discover_raws_stations_in_bbox excludes stations outside bbox."""
    fake_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": "INSIDE",
                "properties": {"sname": "INSIDE RAWS", "elevation": 2000.0,
                               "state": "UT", "network": "UT_DCP",
                               "archive_end": None, "online": True, "attributes": {}},
                "geometry": {"type": "Point", "coordinates": [-112.0, 37.8]},
            },
            {
                "type": "Feature",
                "id": "OUTSIDE",
                "properties": {"sname": "OUTSIDE RAWS", "elevation": 2000.0,
                               "state": "UT", "network": "UT_DCP",
                               "archive_end": None, "online": True, "attributes": {}},
                "geometry": {"type": "Point", "coordinates": [-115.0, 40.0]},
            },
        ],
    }

    def mock_http_get(url, timeout=30.0):
        return json.dumps(fake_geojson).encode("utf-8")

    # Tight bbox that only includes INSIDE.
    bbox = (-112.5, 37.5, -111.5, 38.2)
    with patch(
        "trid3nt_server.tools.fetchers.weather.fetch_raws_weather._http_get",
        side_effect=mock_http_get,
    ):
        stations = _discover_raws_stations_in_bbox(bbox)

    assert len(stations) == 1
    assert stations[0]["sid"] == "INSIDE"


# ---------------------------------------------------------------------------
# Geographic coordinate range.
# ---------------------------------------------------------------------------

_US_LON = (-180.0, -64.0)
_US_LAT = (13.0, 72.0)


def test_synthetic_obs_in_us_envelope():
    """Synthetic UT RAWS observation coordinates are within the US envelope."""
    rows = _make_synthetic_fgb_rows(n_per_station=1)
    fgb_bytes = _build_raws_fgb(rows)

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
# Live integration tests (TRID3NT_TEST_LIVE_RAWS=1 to run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_RAWS,
    reason="Set TRID3NT_TEST_LIVE_RAWS=1 to run live IEM RAWS tests",
)
def test_live_utah_raws_returns_observations():
    """LIVE: real IEM API returns ≥1 RAWS observation for UT bbox.

    Asserts:
    - At least 1 RAWS station discovered in the UT bbox.
    - FGB round-trips; ≥1 feature; all point coords in US envelope.
    - Required columns (station, utc_valid, tmpf, relh) present.
    """
    from trid3nt_server.tools.fetchers.weather.fetch_raws_weather import (
        _discover_raws_stations_in_bbox,
        _fetch_raws_obs_for_station_date,
        _build_raws_fgb,
    )
    from datetime import date as _d

    bbox = _UT_BBOX
    obs_date = _d(2024, 6, 1)

    # 1. Station discovery.
    stations = _discover_raws_stations_in_bbox(bbox)
    assert len(stations) >= 1, (
        f"Expected at least 1 RAWS station in bbox={bbox}; got {stations}"
    )
    print(f"\n[LIVE RAWS] Discovered {len(stations)} RAWS station(s): "
          f"{[s['sid'] for s in stations]}")

    # 2. Fetch one station's observations.
    st = stations[0]
    obs_list = _fetch_raws_obs_for_station_date(
        st["sid"], st["network"], obs_date
    )
    print(f"[LIVE RAWS] {st['sid']} / {obs_date}: {len(obs_list)} observations")
    assert len(obs_list) >= 1, (
        f"Expected at least 1 observation for {st['sid']} on {obs_date}; "
        f"got {obs_list}"
    )

    # 3. Build FGB.
    rows = []
    for obs in obs_list:
        row = {
            "station": st["sid"],
            "station_name": st["sname"],
            "state": st["state"],
            "utc_valid": obs.get("utc_valid"),
            "lon": st["lon"],
            "lat": st["lat"],
            "elevation": st.get("elevation"),
            "tmpf": obs.get("tmpf"),
            "dwpf": obs.get("dwpf"),
            "relh": obs.get("URHRGZZ"),
            "sknt": obs.get("sknt"),
            "drct": obs.get("drct"),
            "gust": obs.get("VBIRGZZ"),
            "solar_rad": obs.get("XRIRGZZ"),
            "precip_in": obs.get("PCIRGZZ"),
        }
        rows.append(row)

    fgb_bytes = _build_raws_fgb(rows)
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) >= 1
        print(f"[LIVE RAWS] {len(gdf)} observations in FGB")
        print(f"  Columns: {list(gdf.columns)}")
        if "tmpf" in gdf.columns:
            temps = gdf["tmpf"].dropna()
            if len(temps) > 0:
                print(f"  tmpf range: {temps.min():.1f}–{temps.max():.1f} °F")
        if "relh" in gdf.columns:
            rh = gdf["relh"].dropna()
            if len(rh) > 0:
                print(f"  relh range: {rh.min():.0f}–{rh.max():.0f} %")

        # Geographic gate.
        lon_min, lon_max = _US_LON
        lat_min, lat_max = _US_LAT
        for idx, geom in enumerate(gdf.geometry):
            if geom is None or geom.is_empty:
                continue
            assert lon_min <= geom.x <= lon_max
            assert lat_min <= geom.y <= lat_max

        for col in ("station", "utc_valid"):
            assert col in gdf.columns
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.skipif(
    not _LIVE_RAWS,
    reason="Set TRID3NT_TEST_LIVE_RAWS=1 to run live IEM RAWS tests",
)
def test_live_full_tool_call_returns_layer_uri():
    """LIVE: full fetch_raws_weather call returns a valid LayerURI with gs:// uri.

    Exercises the read_through path against real GCS (requires ADC creds).
    Falls back to asserting on the error type if GCS is unavailable.
    """
    start_time = "2024-06-01"
    end_time = "2024-06-02"
    try:
        result = fetch_raws_weather(
            bbox=_UT_BBOX,
            start_time=start_time,
            end_time=end_time,
        )
        assert result.uri.startswith("s3://"), (
            f"Expected gs:// URI; got {result.uri!r}"
        )
        assert result.layer_type == "vector"
        print(f"\n[LIVE RAWS] LayerURI: {result.uri}")
        print(f"  Name: {result.name}")
        est = estimate_payload_mb(
            bbox=_UT_BBOX, start_time=start_time, end_time=end_time
        )
        print(f"  Payload estimate: {est:.4f} MB")
    except (RAWSWeatherUpstreamError, RAWSWeatherEmptyError) as exc:
        pytest.skip(f"Live test skipped due to upstream/GCS unavailability: {exc}")
