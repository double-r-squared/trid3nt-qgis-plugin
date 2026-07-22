"""Unit tests for the ``fetch_nws_alerts_conus`` atomic tool (job-0105).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Mocked: 50-feature CONUS response → 50-feature FlatGeobuf written through cache.
- event_types filter narrows client-side (e.g. Hurricane Warning only from a
  mixed 50-feature sample).
- Cache miss → fetch_fn invoked; cache hit → fetch_fn skipped (deduplication).
- User-Agent header verified present on every NWS GET (NWS 403s without it).
- Invalid status / non-string event_types raise typed input errors.
- 403 / network failure map to typed NWSConusUpstreamError(retryable=True).
- URL contains status param but NO area/point param (CONUS-wide variant).
- Geographic-correctness gate (job-0086): every alert polygon in the returned
  FGB whose centroid is in CONUS+territories+marine-zones envelope.
- Live (env GRACE2_TEST_LIVE_NWS_CONUS=1): real api.weather.gov returns ≥0
  features; if non-zero, polygon centroids fall inside the US envelope.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.fetch_nws_alerts_conus import (
    NWSConusError,
    NWSConusInputError,
    NWSConusUpstreamError,
    _build_nws_conus_url,
    _fetch_nws_alerts_conus_bytes,
    _filter_features_by_event_types,
    _geojson_to_fgb,
    fetch_nws_alerts_conus,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Marker for live tests
_LIVE_NWS_CONUS = os.environ.get("GRACE2_TEST_LIVE_NWS_CONUS") == "1"

# Generous US+territories+marine-zones envelope for the geographic-correctness
# gate. Covers CONUS, AK, HI, PR/VI, GU/MP/AS, and the marine zones offshore.
# Centroid of any NWS alert polygon should fall inside this box.
_US_ENVELOPE_LONS = (-180.0, -64.0)   # AK westernmost ~ -179.7 to PR east ~ -64.5
_US_ENVELOPE_LATS = (13.0, 72.0)       # AS ~ -14 but we keep N hemisphere for v0.1
# NOTE: American Samoa is south of the equator (~-14°). We deliberately exclude
# it from the gate envelope — NWS issues very few AS alerts and the gate would
# otherwise be too permissive globally. Surfaced as OQ-0105-AS-LATITUDE-GATE.


def _fake_fgb_bytes(tag: str = "TEST") -> bytes:
    return b"FAKE_NWS_CONUS_FGB_" + tag.encode() + b"\x00" * 16


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors existing test patterns).
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


def _make_feature(
    event: str,
    severity: str,
    lon_min: float,
    lat_min: float,
    *,
    feature_id: str | None = None,
) -> dict:
    """Build one synthetic NWS GeoJSON feature centered roughly at given coords."""
    lon_max = lon_min + 0.5
    lat_max = lat_min + 0.5
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [lon_min, lat_min], [lon_max, lat_min],
                [lon_max, lat_max], [lon_min, lat_max],
                [lon_min, lat_min],
            ]],
        },
        "properties": {
            "id": feature_id or f"alert-{event}-{lon_min}-{lat_min}",
            "event": event,
            "headline": f"{event} for synthetic test feature",
            "description": f"{event} description for testing.",
            "severity": severity,
            "urgency": "Expected",
            "certainty": "Likely",
            "senderName": "NWS Test Office",
            "areaDesc": "Test Area",
            "category": "Met",
            "messageType": "Alert",
            "status": "Actual",
        },
    }


def _sample_conus_geojson(n_features: int = 50) -> dict:
    """Synthetic CONUS-wide NWS response with ``n_features`` mixed alerts.

    Distributes features across the CONUS+AK+HI+PR envelope so the geographic
    gate is exercised. Mixes 5 event types so the client-side filter has
    something to narrow.
    """
    events = [
        ("Hurricane Warning", "Extreme"),
        ("Flood Warning", "Severe"),
        ("Severe Thunderstorm Warning", "Severe"),
        ("Winter Storm Warning", "Moderate"),
        ("Heat Advisory", "Minor"),
    ]
    # Spread anchor points across CONUS + AK + HI + PR.
    anchors = [
        (-122.0, 47.0),  # WA
        (-105.0, 40.0),  # CO
        (-95.0, 35.0),   # OK
        (-87.0, 41.0),   # IL
        (-80.0, 35.0),   # NC
        (-81.0, 26.0),   # FL
        (-115.0, 36.0),  # NV
        (-100.0, 45.0),  # SD
        (-90.0, 30.0),   # LA
        (-75.0, 40.0),   # NJ
        (-150.0, 61.0),  # AK
        (-156.0, 20.0),  # HI
        (-66.0, 18.3),   # PR
    ]
    features: list[dict] = []
    for i in range(n_features):
        event, severity = events[i % len(events)]
        lon, lat = anchors[i % len(anchors)]
        # Stagger so polygons don't all coincide.
        lon += (i // len(anchors)) * 0.3
        lat += (i // len(anchors)) * 0.2
        features.append(_make_feature(event, severity, lon, lat, feature_id=f"alert-{i}"))
    return {"type": "FeatureCollection", "features": features}


def _centroid(polygon_coords: list[list[list[float]]]) -> tuple[float, float]:
    """Naive arithmetic-mean centroid of a polygon's outer ring."""
    ring = polygon_coords[0]
    # Drop the closing vertex (NWS rings always close).
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    lon = sum(p[0] for p in pts) / len(pts)
    lat = sum(p[1] for p in pts) / len(pts)
    return (lon, lat)


# ---------------------------------------------------------------------------
# Registration tests (no network needed).
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_nws_alerts_conus appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_nws_alerts_conus" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_nws_alerts_conus"]
    assert entry.metadata.name == "fetch_nws_alerts_conus"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "nws_alerts_conus"
    assert entry.metadata.cacheable is True


# ---------------------------------------------------------------------------
# URL building tests.
# ---------------------------------------------------------------------------


def test_build_url_has_status_param_no_area():
    """CONUS variant: URL carries status but NO area/point param."""
    url = _build_nws_conus_url("actual")
    assert url.startswith("https://api.weather.gov/alerts/active?")
    assert "status=actual" in url
    # Critical: no area or point param (this is the CONUS-wide variant).
    assert "area=" not in url, f"CONUS URL must not carry area=: {url}"
    assert "point=" not in url, f"CONUS URL must not carry point=: {url}"


def test_build_url_different_status_values():
    """Each valid status produces a distinct URL."""
    urls = {_build_nws_conus_url(s) for s in ("actual", "exercise", "test")}
    assert len(urls) == 3


# ---------------------------------------------------------------------------
# Client-side event_types filter tests.
# ---------------------------------------------------------------------------


def test_filter_none_returns_all():
    """No filter → input passed through unchanged."""
    features = _sample_conus_geojson(10)["features"]
    assert _filter_features_by_event_types(features, None) is features
    assert _filter_features_by_event_types(features, []) is features


def test_filter_narrows_to_hurricane_only():
    """Filter ['Hurricane Warning'] narrows a 50-feature sample to ~10 (50/5 cycle)."""
    features = _sample_conus_geojson(50)["features"]
    narrowed = _filter_features_by_event_types(features, ["Hurricane Warning"])
    # Sample cycles 5 events; 50/5 = 10 Hurricane Warnings.
    assert len(narrowed) == 10
    for f in narrowed:
        assert f["properties"]["event"] == "Hurricane Warning"


def test_filter_with_multiple_event_types():
    """Filter for two event types returns the union."""
    features = _sample_conus_geojson(50)["features"]
    narrowed = _filter_features_by_event_types(
        features, ["Hurricane Warning", "Flood Warning"],
    )
    assert len(narrowed) == 20  # 10 + 10
    events = {f["properties"]["event"] for f in narrowed}
    assert events == {"Hurricane Warning", "Flood Warning"}


# ---------------------------------------------------------------------------
# User-Agent header verification.
# ---------------------------------------------------------------------------


def test_user_agent_header_sent_on_request():
    """Verify the User-Agent header is REQUIRED-AND-PRESENT on every NWS GET.

    NWS returns 403 without a descriptive User-Agent.
    """
    captured_headers = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"type": "FeatureCollection", "features": []}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            captured_headers.update(headers or {})
            return FakeResponse()

    with patch("grace2_agent.tools.fetch_nws_alerts_conus.httpx.Client", FakeClient):
        from grace2_agent.tools.fetch_nws_alerts_conus import _fetch_nws_conus_geojson
        _fetch_nws_conus_geojson("https://api.weather.gov/alerts/active?status=actual")

    assert "User-Agent" in captured_headers, (
        f"User-Agent header missing! Captured: {captured_headers}"
    )
    ua = captured_headers["User-Agent"]
    assert "grace2-agent" in ua, f"User-Agent should identify grace2-agent: {ua!r}"
    assert "contact" in ua.lower(), (
        f"User-Agent should include a contact per NWS policy: {ua!r}"
    )


# ---------------------------------------------------------------------------
# Mocked end-to-end: 50-alert CONUS response.
# ---------------------------------------------------------------------------


def test_50_alert_conus_response_writes_fgb_with_50_features():
    """Mocked CONUS response with 50 alerts → 50-feature FlatGeobuf written through cache."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_conus_geojson(50)

    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus._fetch_nws_conus_geojson",
        return_value=fake_geojson,
    ), patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_nws_alerts_conus()

    assert result.uri.startswith("s3://")
    assert "nws_alerts_conus" in result.uri
    assert "dynamic-1h" in result.uri
    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.units is None
    assert "CONUS" in result.name
    assert len(fake_gcs.store) == 1

    # Read back the FlatGeobuf and confirm 50 features.
    fgb_bytes = next(iter(fake_gcs.store.values()))
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 50, f"Expected 50 features, got {len(gdf)}"
        events = set(gdf["event"].tolist())
        # Should have all 5 distinct event types from the sample.
        assert "Hurricane Warning" in events
        assert "Flood Warning" in events
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_event_types_filter_narrows_in_end_to_end_call():
    """Top-level call with event_types=['Hurricane Warning'] narrows to 10."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_conus_geojson(50)

    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus._fetch_nws_conus_geojson",
        return_value=fake_geojson,
    ), patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_nws_alerts_conus(event_types=["Hurricane Warning"])

    fgb_bytes = next(iter(fake_gcs.store.values()))
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 10, f"Expected 10 Hurricane Warnings, got {len(gdf)}"
        assert set(gdf["event"].tolist()) == {"Hurricane Warning"}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    # Name should reflect the filter.
    assert "Hurricane Warning" in result.name


# ---------------------------------------------------------------------------
# Input validation tests.
# ---------------------------------------------------------------------------


def test_invalid_status_raises_input_error():
    with pytest.raises(NWSConusInputError, match="status="):
        fetch_nws_alerts_conus(status="bogus")


def test_invalid_event_types_type_raises():
    with pytest.raises(NWSConusInputError, match="event_types must be"):
        fetch_nws_alerts_conus(event_types=[123])  # type: ignore[list-item]


def test_input_errors_are_not_retryable():
    """NWSConusInputError carries retryable=False for FR-AS-11."""
    try:
        fetch_nws_alerts_conus(status="bogus")
    except NWSConusInputError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected NWSConusInputError")


# ---------------------------------------------------------------------------
# Upstream-error mapping.
# ---------------------------------------------------------------------------


def test_403_raises_typed_upstream_error_with_useragent_message():
    """403 from NWS surfaces as NWSConusUpstreamError naming the User-Agent."""
    class FakeResponse:
        status_code = 403
        text = "Forbidden"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            return FakeResponse()

    with patch("grace2_agent.tools.fetch_nws_alerts_conus.httpx.Client", FakeClient):
        from grace2_agent.tools.fetch_nws_alerts_conus import _fetch_nws_conus_geojson
        with pytest.raises(NWSConusUpstreamError, match="403"):
            _fetch_nws_conus_geojson("https://api.weather.gov/alerts/active?status=actual")


def test_upstream_error_is_retryable():
    """NWSConusUpstreamError is retryable=True."""
    err = NWSConusUpstreamError("test")
    assert err.retryable is True


def test_network_failure_wraps_to_upstream_error():
    """httpx.HTTPError → NWSConusUpstreamError."""
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            raise httpx.ConnectError("simulated DNS failure")

    with patch("grace2_agent.tools.fetch_nws_alerts_conus.httpx.Client", FakeClient):
        from grace2_agent.tools.fetch_nws_alerts_conus import _fetch_nws_conus_geojson
        with pytest.raises(NWSConusUpstreamError, match="request failed"):
            _fetch_nws_conus_geojson("https://api.weather.gov/alerts/active?status=actual")


# ---------------------------------------------------------------------------
# Cache-layer tests.
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_fetch_fn_then_hit_skips():
    """First call → cache miss → fetch_fn invoked; second call → cache hit → fetch_fn skipped."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_bytes = _fake_fgb_bytes("CONUS")

    def patched_fetch_bytes(status, event_types, area_code=None):
        fetch_count["n"] += 1
        return fake_bytes

    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus._fetch_nws_alerts_conus_bytes",
        side_effect=patched_fetch_bytes,
    ), patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_nws_alerts_conus()
        r2 = fetch_nws_alerts_conus()

    assert fetch_count["n"] == 1, (
        f"Expected 1 call (hit on second); got {fetch_count['n']}"
    )
    assert r1.uri == r2.uri, "Both calls should resolve to the same cache key"


def test_event_types_filter_changes_cache_key():
    """A different event_types filter produces a different cache key.

    (See OQ-0105-CACHE-RAW-VS-FILTERED for a possible future optimization
    that would cache the RAW CONUS sweep and re-filter on hit.)
    """
    fake_gcs = FakeStorageClient()

    def patched_fetch_bytes(status, event_types, area_code=None):
        return _fake_fgb_bytes(str(event_types))

    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus._fetch_nws_alerts_conus_bytes",
        side_effect=patched_fetch_bytes,
    ), patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_all = fetch_nws_alerts_conus()
        r_hurricane = fetch_nws_alerts_conus(event_types=["Hurricane Warning"])

    assert r_all.uri != r_hurricane.uri


def test_event_types_order_does_not_affect_cache_key():
    """Sorting event_types before keying: ['A','B'] and ['B','A'] hit same cache."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}

    def patched_fetch_bytes(status, event_types, area_code=None):
        fetch_count["n"] += 1
        return _fake_fgb_bytes("X")

    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus._fetch_nws_alerts_conus_bytes",
        side_effect=patched_fetch_bytes,
    ), patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_nws_alerts_conus(
            event_types=["Hurricane Warning", "Flood Warning"],
        )
        r2 = fetch_nws_alerts_conus(
            event_types=["Flood Warning", "Hurricane Warning"],
        )

    assert r1.uri == r2.uri
    assert fetch_count["n"] == 1


# ---------------------------------------------------------------------------
# LayerURI shape.
# ---------------------------------------------------------------------------


def test_layer_uri_shape_for_unfiltered():
    fake_gcs = FakeStorageClient()
    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus._fetch_nws_alerts_conus_bytes",
        return_value=_fake_fgb_bytes("CONUS"),
    ), patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_nws_alerts_conus()

    assert result.layer_type == "vector"
    assert result.role == "primary"  # CONUS-wide variant is a primary content layer
    assert result.units is None
    assert result.uri.startswith("s3://")
    assert "NWS Active Alerts" in result.name
    assert "CONUS" in result.name
    assert "all events" in result.name


# ---------------------------------------------------------------------------
# Geographic-correctness gate (job-0086 lesson).
# ---------------------------------------------------------------------------


def test_geographic_gate_all_polygons_fall_inside_us_envelope():
    """job-0086 codified lesson: every alert polygon centroid is inside the US envelope.

    Uses the synthetic 50-feature CONUS+AK+HI+PR sample. Any feature whose
    centroid falls outside the (-180, 13, -64, 72) envelope would surface a
    sign-flip / axis-swap bug in the GeoJSON→FGB conversion (a regression
    where coordinates get swapped or signed wrong would put centroids on the
    wrong continent).
    """
    fake_geojson = _sample_conus_geojson(50)

    lon_min, lon_max = _US_ENVELOPE_LONS
    lat_min, lat_max = _US_ENVELOPE_LATS

    # First verify INPUT features already fall in the envelope (sanity).
    for feat in fake_geojson["features"]:
        cx, cy = _centroid(feat["geometry"]["coordinates"])
        assert lon_min <= cx <= lon_max, (
            f"Input feature centroid lon={cx} outside [-180, -64]; bad sample"
        )
        assert lat_min <= cy <= lat_max, (
            f"Input feature centroid lat={cy} outside [13, 72]; bad sample"
        )

    # Now run through the converter and verify the geometries survive intact.
    fgb_bytes = _geojson_to_fgb(fake_geojson)
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 50

        # Geographic gate: every feature's centroid must fall inside the US envelope.
        # If lat/lon were swapped, centroids would land at (lat, lon) → e.g. (35, -95)
        # would become (95, -35) which is OUTSIDE the envelope.
        for idx, geom in enumerate(gdf.geometry):
            c = geom.centroid
            assert lon_min <= c.x <= lon_max, (
                f"Feature {idx} centroid x={c.x} outside US lon envelope — "
                f"possible axis-swap bug"
            )
            assert lat_min <= c.y <= lat_max, (
                f"Feature {idx} centroid y={c.y} outside US lat envelope — "
                f"possible axis-swap bug"
            )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# GeoJSON → FlatGeobuf conversion edge case.
# ---------------------------------------------------------------------------


def test_geojson_to_fgb_empty_collection_is_valid():
    """Empty NWS FeatureCollection still produces valid FGB bytes."""
    empty_geojson = {"type": "FeatureCollection", "features": []}
    fgb_bytes = _geojson_to_fgb(empty_geojson)
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 0
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Live integration test (GRACE2_TEST_LIVE_NWS_CONUS=1 to run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_NWS_CONUS,
    reason="Set GRACE2_TEST_LIVE_NWS_CONUS=1 to run live NWS CONUS tests",
)
def test_live_conus_sweep_returns_valid_response():
    """LIVE: real api.weather.gov CONUS sweep returns valid FGB (≥0 features).

    Empty FeatureCollection is LEGITIMATE (rare CONUS-wide quiet period).
    We assert the FGB round-trips and — if non-empty — that every feature's
    centroid falls inside the US envelope (geographic-correctness gate).
    """
    fgb_bytes = _fetch_nws_alerts_conus_bytes(status="actual", event_types=None)
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) >= 0
        print(f"\n[LIVE NWS CONUS] sweep returned {len(gdf)} active alert(s)")
        if len(gdf) > 0:
            events = gdf["event"].dropna().tolist()
            from collections import Counter
            counts = Counter(events).most_common(10)
            print(f"  top events: {counts}")
            print(f"  columns: {list(gdf.columns)}")

            # Geographic-correctness gate: features WITH polygons must have
            # centroids inside the US envelope. Some NWS alerts have null
            # geometry (zone-only references), which we tolerate.
            lon_min, lon_max = _US_ENVELOPE_LONS
            lat_min, lat_max = _US_ENVELOPE_LATS
            outside = 0
            inside = 0
            for geom in gdf.geometry:
                if geom is None or geom.is_empty:
                    continue
                c = geom.centroid
                if (lon_min <= c.x <= lon_max) and (lat_min <= c.y <= lat_max):
                    inside += 1
                else:
                    outside += 1
                    print(f"  WARN: centroid outside US envelope: ({c.x}, {c.y})")
            print(f"  geographic gate: inside={inside} outside={outside}")
            # Marine zones / Pacific can produce centroids slightly outside
            # the v0.1 envelope (especially the marine zones west of HI).
            # We allow up to 5% outside before failing.
            with_geom = inside + outside
            if with_geom > 0:
                outside_pct = outside / with_geom
                assert outside_pct <= 0.05, (
                    f"More than 5% of CONUS alerts ({outside}/{with_geom}) "
                    f"have centroids outside the US envelope — possible "
                    f"axis-swap regression"
                )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.skipif(
    not _LIVE_NWS_CONUS,
    reason="Set GRACE2_TEST_LIVE_NWS_CONUS=1 to run live NWS CONUS tests",
)
def test_live_conus_with_filter_narrows():
    """LIVE: client-side filter to Flood Warning narrows the CONUS sweep."""
    fgb_bytes = _fetch_nws_alerts_conus_bytes(
        status="actual",
        event_types=["Flood Warning", "Hurricane Warning"],
    )
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        if len(gdf) > 0:
            events = set(gdf["event"].dropna().tolist())
            allowed = {"Flood Warning", "Hurricane Warning"}
            assert events.issubset(allowed), (
                f"Filter should narrow events; got {events}, allowed {allowed}"
            )
            print(
                f"\n[LIVE NWS CONUS] filtered to {len(gdf)} Flood/Hurricane Warning(s)"
            )
        else:
            print("\n[LIVE NWS CONUS] filter → 0 alerts (steady state)")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# job-0261: state-aware path — "weather alerts for Texas" must NOT spill
# into surrounding states.
# ---------------------------------------------------------------------------


def test_build_url_with_area_code_uses_server_side_state_filter():
    """area_code='TX' → precise NWS server-side filter ?area=TX."""
    url = _build_nws_conus_url("actual", "TX")
    assert url.startswith("https://api.weather.gov/alerts/active?")
    assert "area=TX" in url, f"state-scoped URL must carry area=TX: {url}"
    assert "status=actual" in url
    assert "point=" not in url


def test_build_url_without_area_unchanged_conus_sweep():
    """Back-compat: no area_code → identical unscoped CONUS URL as before."""
    assert _build_nws_conus_url("actual") == (
        "https://api.weather.gov/alerts/active?status=actual"
    )
    assert _build_nws_conus_url("actual", None) == (
        "https://api.weather.gov/alerts/active?status=actual"
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("TX", "TX"),
        ("tx", "TX"),
        ("Texas", "TX"),
        ("texas", "TX"),          # the live-demo prompt form
        ("state of Texas", "TX"),
        ("New Mexico", "NM"),
        ("Puerto Rico", "PR"),
    ],
)
def test_resolve_area_or_raise_accepts(raw, expected):
    from grace2_agent.tools.fetch_nws_alerts_conus import _resolve_area_or_raise
    assert _resolve_area_or_raise(raw) == expected


@pytest.mark.parametrize("raw", ["Houston", "Lee County", "Canada", "XX", "12071"])
def test_resolve_area_or_raise_rejects_non_states(raw):
    """Unrecognized areas raise a typed, non-retryable input error that points
    Gemini at fetch_nws_event rather than silently sweeping the nation."""
    from grace2_agent.tools.fetch_nws_alerts_conus import _resolve_area_or_raise
    with pytest.raises(NWSConusInputError, match="fetch_nws_event"):
        _resolve_area_or_raise(raw)


def test_resolve_area_or_raise_non_string_raises():
    from grace2_agent.tools.fetch_nws_alerts_conus import _resolve_area_or_raise
    with pytest.raises(NWSConusInputError):
        _resolve_area_or_raise(("TX",))  # type: ignore[arg-type]


def test_area_texas_end_to_end_sends_area_param_and_labels_layer():
    """fetch_nws_alerts_conus(area='Texas') sends ?area=TX upstream and the
    returned layer is labeled as Texas-scoped (not CONUS)."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_conus_geojson(10)
    seen_urls: list[str] = []

    def capture_geojson(url):
        seen_urls.append(url)
        return fake_geojson

    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus._fetch_nws_conus_geojson",
        side_effect=capture_geojson,
    ), patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_nws_alerts_conus(area="Texas")

    assert len(seen_urls) == 1
    assert "area=TX" in seen_urls[0], (
        f"upstream URL must carry the server-side state filter: {seen_urls[0]}"
    )
    assert "Texas (TX)" in result.name
    assert "CONUS" not in result.name
    assert result.layer_id == "nws-TX-actual-all"


def test_area_changes_cache_key():
    """TX-scoped, FL-scoped, and unscoped sweeps must not share a cache key."""
    fake_gcs = FakeStorageClient()

    def patched_fetch_bytes(status, event_types, area_code=None):
        return _fake_fgb_bytes(str(area_code))

    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus._fetch_nws_alerts_conus_bytes",
        side_effect=patched_fetch_bytes,
    ), patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_conus = fetch_nws_alerts_conus()
        r_tx = fetch_nws_alerts_conus(area="TX")
        r_tx_name = fetch_nws_alerts_conus(area="Texas")
        r_fl = fetch_nws_alerts_conus(area="FL")

    assert r_conus.uri != r_tx.uri
    assert r_tx.uri != r_fl.uri
    # "TX" and "Texas" canonicalize to the SAME key (one upstream fetch).
    assert r_tx.uri == r_tx_name.uri
    assert len(fake_gcs.store) == 3


def test_garbage_area_raises_before_any_fetch():
    """A non-state area must fail loud (typed input error) without touching
    the network or the cache."""
    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.read_through",
    ) as rt:
        with pytest.raises(NWSConusInputError, match="not a recognized"):
            fetch_nws_alerts_conus(area="Gulf of Mexico City")
    rt.assert_not_called()


# ---------------------------------------------------------------------------
# F88: zone-reference → polygon resolution.
#
# NWS alerts frequently carry NULL inline geometry and reference affected areas
# by properties.affectedZones (NWS zone API URLs) and/or properties.geocode.UGC
# codes. A NULL-geometry row draws nothing — the live FL test ("7 active alerts,
# 0 polygons") was exactly this. The converter must resolve those zone refs to
# real polygons before the FGB write so MapLibre actually draws them.
# ---------------------------------------------------------------------------


def _zone_polygon(lon_min: float, lat_min: float) -> dict:
    """A synthetic NWS zone-API geometry (Polygon) centered near given coords."""
    lon_max = lon_min + 0.4
    lat_max = lat_min + 0.4
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon_min, lat_min], [lon_max, lat_min],
            [lon_max, lat_max], [lon_min, lat_max],
            [lon_min, lat_min],
        ]],
    }


def _zone_feature(ugc: str, lon_min: float, lat_min: float) -> dict:
    """A synthetic NWS zone-API Feature (mirrors api.weather.gov/zones/...)."""
    return {
        "type": "Feature",
        "geometry": _zone_polygon(lon_min, lat_min),
        "properties": {"id": ugc, "type": "public"},
    }


def _null_geom_alert(
    event: str,
    affected_zones: list[str],
    ugc: list[str],
) -> dict:
    """An NWS alert with NULL inline geometry + zone references (the F88 case)."""
    return {
        "type": "Feature",
        "geometry": None,
        "properties": {
            "id": f"alert-{event}-zoneonly",
            "event": event,
            "headline": f"{event} (zone-referenced, no inline geometry)",
            "description": f"{event} affecting referenced zones.",
            "severity": "Severe",
            "urgency": "Expected",
            "certainty": "Likely",
            "senderName": "NWS Test Office",
            "areaDesc": "Referenced Zones",
            "status": "Actual",
            "affectedZones": affected_zones,
            "geocode": {"UGC": ugc, "SAME": []},
        },
    }


def test_ugc_to_zone_url_maps_zone_and_county_types():
    """UGC 3rd char selects the NWS zone collection: Z→forecast, C→county."""
    from grace2_agent.tools.fetch_nws_alerts_conus import _ugc_to_zone_url
    assert _ugc_to_zone_url("LAZ091") == (
        "https://api.weather.gov/zones/forecast/LAZ091"
    )
    assert _ugc_to_zone_url("ILC011") == (
        "https://api.weather.gov/zones/county/ILC011"
    )
    # Case-insensitive.
    assert _ugc_to_zone_url("laz091") == (
        "https://api.weather.gov/zones/forecast/LAZ091"
    )
    # Unresolvable inputs → None (never a guessed URL).
    assert _ugc_to_zone_url("LA") is None
    assert _ugc_to_zone_url("LAX091") is None
    assert _ugc_to_zone_url(123) is None  # type: ignore[arg-type]


def test_zone_urls_for_feature_prefers_affected_zones_and_dedupes():
    """affectedZones are primary; geocode.UGC is the fallback union; no dupes."""
    from grace2_agent.tools.fetch_nws_alerts_conus import _zone_urls_for_feature
    props = {
        "affectedZones": ["https://api.weather.gov/zones/forecast/LAZ091"],
        "geocode": {"UGC": ["LAZ091", "LAZ093", "ILC011"]},
    }
    urls = _zone_urls_for_feature(props)
    # affectedZones first, then UGC-derived; LAZ091 must not appear twice.
    assert urls == [
        "https://api.weather.gov/zones/forecast/LAZ091",
        "https://api.weather.gov/zones/forecast/LAZ093",
        "https://api.weather.gov/zones/county/ILC011",
    ]


def test_zone_urls_for_feature_falls_back_to_ugc_when_no_affected_zones():
    """No affectedZones → derive URLs purely from geocode.UGC (fallback norm)."""
    from grace2_agent.tools.fetch_nws_alerts_conus import _zone_urls_for_feature
    props = {"geocode": {"UGC": ["FLZ072", "FLC086"]}}
    urls = _zone_urls_for_feature(props)
    assert urls == [
        "https://api.weather.gov/zones/forecast/FLZ072",
        "https://api.weather.gov/zones/county/FLC086",
    ]


def _zone_http_mock(zone_geoms: dict[str, dict]):
    """Build a fake httpx.Client whose .get returns the synthetic zone Feature
    for each requested zone URL, recording call counts per URL.

    ``zone_geoms`` maps zone-URL → geometry dict. Unknown URLs return 404.
    """
    calls: dict[str, int] = {}

    class FakeResponse:
        def __init__(self, body, status=200):
            self._body = body
            self.status_code = status
            self.text = ""

        def json(self):
            return self._body

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            calls[url] = calls.get(url, 0) + 1
            geom = zone_geoms.get(url)
            if geom is None:
                return FakeResponse({"detail": "not found"}, status=404)
            return FakeResponse(
                {"type": "Feature", "geometry": geom, "properties": {"id": url}}
            )

    return FakeClient, calls


def test_resolve_zone_geometries_attaches_real_polygons():
    """A NULL-geometry alert with affectedZones resolves to a drawable polygon."""
    from grace2_agent.tools.fetch_nws_alerts_conus import _resolve_zone_geometries

    z_url = "https://api.weather.gov/zones/forecast/FLZ072"
    alert = _null_geom_alert(
        "Coastal Flood Advisory", [z_url], ["FLZ072"],
    )
    collection = {"type": "FeatureCollection", "features": [alert]}

    FakeClient, calls = _zone_http_mock({z_url: _zone_polygon(-80.2, 26.1)})
    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.httpx.Client", FakeClient
    ):
        resolved = _resolve_zone_geometries(collection)

    feats = resolved["features"]
    assert len(feats) == 1
    geom = feats[0]["geometry"]
    assert geom is not None, "zone-referenced alert must gain real geometry"
    assert geom["type"] in ("Polygon", "MultiPolygon")
    # Property table preserved.
    assert feats[0]["properties"]["event"] == "Coastal Flood Advisory"
    assert calls[z_url] == 1


def test_resolve_zone_geometries_unions_multiple_zones_to_multipolygon():
    """Several affected zones → MultiPolygon union attached to the alert."""
    from grace2_agent.tools.fetch_nws_alerts_conus import _resolve_zone_geometries

    z1 = "https://api.weather.gov/zones/forecast/FLZ072"
    z2 = "https://api.weather.gov/zones/forecast/FLZ073"
    alert = _null_geom_alert(
        "Flood Warning", [z1, z2], ["FLZ072", "FLZ073"],
    )
    collection = {"type": "FeatureCollection", "features": [alert]}

    FakeClient, calls = _zone_http_mock({
        z1: _zone_polygon(-80.2, 26.1),
        z2: _zone_polygon(-80.6, 26.5),
    })
    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.httpx.Client", FakeClient
    ):
        resolved = _resolve_zone_geometries(collection)

    geom = resolved["features"][0]["geometry"]
    assert geom["type"] == "MultiPolygon"
    assert len(geom["coordinates"]) == 2


def test_resolve_zone_geometries_dedupes_shared_zone_across_alerts():
    """Alerts share zones — each distinct zone URL is fetched exactly once."""
    from grace2_agent.tools.fetch_nws_alerts_conus import _resolve_zone_geometries

    shared = "https://api.weather.gov/zones/forecast/FLZ072"
    a1 = _null_geom_alert("Flood Warning", [shared], ["FLZ072"])
    a2 = _null_geom_alert("Coastal Flood Advisory", [shared], ["FLZ072"])
    collection = {"type": "FeatureCollection", "features": [a1, a2]}

    FakeClient, calls = _zone_http_mock({shared: _zone_polygon(-80.2, 26.1)})
    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.httpx.Client", FakeClient
    ):
        resolved = _resolve_zone_geometries(collection)

    # Both alerts got geometry; the shared zone was fetched only once.
    assert all(f["geometry"] is not None for f in resolved["features"])
    assert calls[shared] == 1


def test_resolve_zone_geometries_keeps_inline_geometry_untouched():
    """Features that already carry inline geometry are not re-fetched."""
    from grace2_agent.tools.fetch_nws_alerts_conus import _resolve_zone_geometries

    inline = _make_feature("Tornado Warning", "Extreme", -97.0, 35.0)
    collection = {"type": "FeatureCollection", "features": [inline]}

    FakeClient, calls = _zone_http_mock({})
    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.httpx.Client", FakeClient
    ):
        resolved = _resolve_zone_geometries(collection)

    # No HTTP at all — inline geometry alerts skip the zone client entirely.
    assert calls == {}
    assert resolved["features"][0]["geometry"]["type"] == "Polygon"


def test_resolve_zone_geometries_unresolvable_keeps_null_never_fabricates():
    """A zone that 404s: alert keeps its row + NULL geometry (no fabrication)."""
    from grace2_agent.tools.fetch_nws_alerts_conus import _resolve_zone_geometries

    bad = "https://api.weather.gov/zones/forecast/ZZZ999"
    alert = _null_geom_alert("Special Weather Statement", [bad], ["ZZZ999"])
    collection = {"type": "FeatureCollection", "features": [alert]}

    # Empty mock → every zone URL 404s.
    FakeClient, _calls = _zone_http_mock({})
    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.httpx.Client", FakeClient
    ):
        resolved = _resolve_zone_geometries(collection)

    feat = resolved["features"][0]
    # Row preserved (property table intact) but geometry stays NULL — honest,
    # never fabricated.
    assert feat["geometry"] is None
    assert feat["properties"]["event"] == "Special Weather Statement"


def test_end_to_end_zone_referenced_alerts_become_drawable_fgb():
    """F88 acceptance: a FeatureCollection of NULL-geometry zone-referenced
    alerts converts to an FGB whose rows carry drawable (non-null) geometry.

    This is the exact failure NATE saw live: '7 active alerts' that drew ZERO
    polygons because every alert was zone-referenced with NULL inline geometry.
    """
    z072 = "https://api.weather.gov/zones/forecast/FLZ072"
    z073 = "https://api.weather.gov/zones/forecast/FLZ073"
    z086 = "https://api.weather.gov/zones/county/FLC086"

    # Mix: one inline-geometry alert + three zone-referenced NULL-geom alerts.
    inline = _make_feature("Tornado Warning", "Extreme", -81.0, 26.0)
    a1 = _null_geom_alert("Coastal Flood Advisory", [z072], ["FLZ072"])
    a2 = _null_geom_alert("Flood Warning", [z072, z073], ["FLZ072", "FLZ073"])
    # No affectedZones — UGC fallback path.
    a3 = _null_geom_alert("Heat Advisory", [], ["FLC086"])

    geojson = {
        "type": "FeatureCollection",
        "features": [inline, a1, a2, a3],
    }

    FakeClient, calls = _zone_http_mock({
        z072: _zone_polygon(-80.2, 26.1),
        z073: _zone_polygon(-80.6, 26.5),
        z086: _zone_polygon(-81.4, 27.0),
    })
    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.httpx.Client", FakeClient
    ):
        # _resolve_zone_geometries runs inside the converter pipeline; call it
        # then feed the result to the FGB writer (same order as the tool body).
        from grace2_agent.tools.fetch_nws_alerts_conus import (
            _resolve_zone_geometries,
        )
        resolved = _resolve_zone_geometries(geojson)
    fgb_bytes = _geojson_to_fgb(resolved)
    assert len(fgb_bytes) > 0

    # z072 is shared by a1 and a2 → fetched once.
    assert calls[z072] == 1

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 4
        # CRITICAL F88 assertion: every row carries drawable geometry. Before
        # the fix the three zone-referenced rows had NULL geometry and drew
        # nothing.
        drawable = gdf.geometry.notna() & ~gdf.geometry.is_empty
        assert drawable.all(), (
            f"Expected all 4 alerts to be drawable; got "
            f"{int(drawable.sum())}/4. Zone resolution did not flow through."
        )
        events = set(gdf["event"].tolist())
        assert "Coastal Flood Advisory" in events
        assert "Heat Advisory" in events  # UGC-fallback alert
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_full_fetch_pipeline_resolves_zones_via_bytes_path():
    """_fetch_nws_alerts_conus_bytes resolves zone refs end-to-end.

    Mocks the CONUS sweep (zone-referenced alerts) AND the per-zone GETs, then
    asserts the produced FGB carries drawable geometry. Verifies the resolution
    is wired into the real fetch path (not just callable in isolation).
    """
    z072 = "https://api.weather.gov/zones/forecast/FLZ072"
    sweep = {
        "type": "FeatureCollection",
        "features": [
            _null_geom_alert("Coastal Flood Advisory", [z072], ["FLZ072"]),
        ],
    }
    FakeClient, _calls = _zone_http_mock({z072: _zone_polygon(-80.2, 26.1)})

    with patch(
        "grace2_agent.tools.fetch_nws_alerts_conus._fetch_nws_conus_geojson",
        return_value=sweep,
    ), patch(
        "grace2_agent.tools.fetch_nws_alerts_conus.httpx.Client", FakeClient
    ):
        fgb_bytes = _fetch_nws_alerts_conus_bytes(
            status="actual", event_types=None,
        )

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 1
        assert gdf.geometry.notna().all(), (
            "fetch pipeline must resolve zone refs to drawable geometry"
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.skipif(
    not _LIVE_NWS_CONUS,
    reason="Set GRACE2_TEST_LIVE_NWS_CONUS=1 to run live NWS CONUS tests",
)
def test_live_area_tx_every_feature_is_texas():
    """LIVE (job-0261 acceptance): api.weather.gov/alerts/active?area=TX —
    every returned feature's geocode/areaDesc references Texas.

    NWS UGC zone/county codes are state-prefixed ("TXZ123", "TXC201"); an
    alert returned by the TX-scoped query must carry at least one TX UGC.
    This is the Gemini-free proof that the named state cannot spill into
    its neighbors the way the unscoped CONUS sweep did in the live demo.
    """
    from grace2_agent.tools.fetch_nws_alerts_conus import _fetch_nws_conus_geojson

    url = _build_nws_conus_url("actual", "TX")
    body = _fetch_nws_conus_geojson(url)
    features = body.get("features", []) or []
    print(f"\n[LIVE NWS area=TX] {len(features)} active alert(s)")
    for feat in features:
        props = feat.get("properties") or {}
        geocode = props.get("geocode") or {}
        ugc = geocode.get("UGC") or []
        area_desc = props.get("areaDesc") or ""
        assert any(str(code).upper().startswith("TX") for code in ugc), (
            f"non-Texas feature returned by area=TX query: "
            f"id={props.get('id')!r} areaDesc={area_desc!r} UGC={ugc!r}"
        )
