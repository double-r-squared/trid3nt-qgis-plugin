"""Unit tests for the ``fetch_nws_event`` atomic tool (job-0090).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Mocked: FL state response with 3 active alerts → FGB written through cache.
- event_types filter narrows to Hurricane only (URL building verification).
- bbox center conversion: (-81.9, 26.5, -81.7, 26.7) → lat=26.6, lon=-81.8.
  This is the GEOGRAPHIC-CORRECTNESS check per the codified job-0086 lesson:
  the algebraic identity (center of bbox) is asserted directly so a sign-flip
  or axis-swap bug surfaces as test failure, not as a silently-wrong polygon.
- User-Agent header verified present in request.
- Invalid status / message_type / area inputs raise typed errors.
- Cache miss → fetch_fn invoked; cache hit → fetch_fn skipped.
- Live (env TRID3NT_TEST_LIVE_NWS=1): area='FL' returns ≥0 features (zero is OK).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetch_nws_event import (
    NWSError,
    NWSInputError,
    NWSUpstreamError,
    _bbox_to_point_center,
    _build_nws_url,
    _canonicalize_area,
    _fetch_nws_event_bytes,
    fetch_nws_event,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Fort Myers, FL bounding box (job-0086 lesson reference: algebraic identity)
# Center of this bbox MUST be (lat=26.6, lon=-81.8) — see audit.md spec.
_FORT_MYERS_BBOX = (-81.9, 26.5, -81.7, 26.7)

# Marker for live tests
_LIVE_NWS = os.environ.get("TRID3NT_TEST_LIVE_NWS") == "1"


# Minimal valid FlatGeobuf bytes placeholder for cache tests.
def _fake_fgb_bytes(tag: str = "TEST") -> bytes:
    return b"FAKE_NWS_FGB_" + tag.encode() + b"\x00" * 16


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


def _sample_fl_geojson() -> dict:
    """Return a stub NWS GeoJSON response with 3 active FL alerts."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-82.0, 26.4], [-81.6, 26.4],
                        [-81.6, 26.8], [-82.0, 26.8],
                        [-82.0, 26.4],
                    ]],
                },
                "properties": {
                    "id": "alert-1",
                    "event": "Hurricane Warning",
                    "headline": "Hurricane Warning for SW Florida",
                    "description": "A hurricane warning is in effect.",
                    "severity": "Extreme",
                    "urgency": "Immediate",
                    "certainty": "Observed",
                    "onset": "2026-06-08T00:00:00Z",
                    "ends": "2026-06-09T00:00:00Z",
                    "senderName": "NWS Tampa Bay",
                    "areaDesc": "Lee County, FL",
                    "category": "Met",
                    "messageType": "Alert",
                    "status": "Actual",
                },
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-81.5, 26.2], [-81.0, 26.2],
                        [-81.0, 26.6], [-81.5, 26.6],
                        [-81.5, 26.2],
                    ]],
                },
                "properties": {
                    "id": "alert-2",
                    "event": "Flood Warning",
                    "headline": "Flood Warning for Lee County",
                    "severity": "Severe",
                    "urgency": "Expected",
                    "certainty": "Likely",
                    "senderName": "NWS Tampa Bay",
                    "areaDesc": "Lee County, FL",
                },
            },
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-80.5, 25.8], [-80.0, 25.8],
                        [-80.0, 26.2], [-80.5, 26.2],
                        [-80.5, 25.8],
                    ]],
                },
                "properties": {
                    "id": "alert-3",
                    "event": "Severe Thunderstorm Watch",
                    "headline": "Severe Thunderstorm Watch SE FL",
                    "severity": "Moderate",
                    "urgency": "Future",
                    "certainty": "Possible",
                    "senderName": "NWS Miami",
                    "areaDesc": "Miami-Dade, FL",
                    # Test nested values — should be JSON-stringified for FGB.
                    "parameters": {"NWSheadline": ["WATCH IN EFFECT"]},
                },
            },
        ],
    }


# ---------------------------------------------------------------------------
# Registration tests (no network needed).
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_nws_event appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_nws_event" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_nws_event"]
    assert entry.metadata.name == "fetch_nws_event"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "nws_event"
    assert entry.metadata.cacheable is True


# ---------------------------------------------------------------------------
# bbox → point center: GEOGRAPHIC-CORRECTNESS check (job-0086 lesson).
# ---------------------------------------------------------------------------


def test_bbox_to_point_center_matches_audit_md_spec():
    """Audit.md spec: (-81.9, 26.5, -81.7, 26.7) → lat=26.6, lon=-81.8.

    This is the CODIFIED JOB-0086 LESSON in action: assert the algebraic
    identity (center of bbox), not just round-trip. A sign-flip or axis-swap
    in _bbox_to_point_center surfaces here as a wrong number, not as a
    silently-wrong polygon downstream.
    """
    lat, lon = _bbox_to_point_center(_FORT_MYERS_BBOX)
    assert lat == pytest.approx(26.6, abs=1e-9), (
        f"Expected lat=26.6 (audit.md spec); got {lat}"
    )
    assert lon == pytest.approx(-81.8, abs=1e-9), (
        f"Expected lon=-81.8 (audit.md spec); got {lon}"
    )


@pytest.mark.parametrize("bbox,expected_lat,expected_lon", [
    # SW quadrant (negative lon, negative lat — would expose lat/lon swap)
    ((-30.0, -40.0, -28.0, -38.0), -39.0, -29.0),
    # NE quadrant (positive lon, positive lat)
    ((100.0, 30.0, 102.0, 32.0), 31.0, 101.0),
    # Crosses prime meridian
    ((-1.0, 50.0, 1.0, 52.0), 51.0, 0.0),
    # Tight bbox (single-degree)
    ((-82.5, 26.0, -81.5, 27.0), 26.5, -82.0),
])
def test_bbox_to_point_center_geometry_correctness(bbox, expected_lat, expected_lon):
    """Verify center algebra for multiple bboxes — exposes lat/lon swap bugs.

    Each case has lat != lon so an axis swap surfaces as a wrong number.
    """
    lat, lon = _bbox_to_point_center(bbox)
    assert lat == pytest.approx(expected_lat, abs=1e-9), (
        f"bbox={bbox}: expected lat={expected_lat}, got {lat}"
    )
    assert lon == pytest.approx(expected_lon, abs=1e-9), (
        f"bbox={bbox}: expected lon={expected_lon}, got {lon}"
    )


# ---------------------------------------------------------------------------
# Area canonicalization tests.
# ---------------------------------------------------------------------------


def test_canonicalize_area_state_code():
    canon = _canonicalize_area("FL")
    assert canon == {"kind": "state", "value": "FL"}


def test_canonicalize_area_state_code_lowercase_uppercased():
    canon = _canonicalize_area("fl")
    assert canon["kind"] == "state"
    assert canon["value"] == "FL"


def test_canonicalize_area_fips():
    canon = _canonicalize_area("12071")  # Lee County, FL
    assert canon == {"kind": "fips", "value": "12071"}


def test_canonicalize_area_bbox():
    canon = _canonicalize_area(_FORT_MYERS_BBOX)
    assert canon["kind"] == "point"
    # Per the audit.md spec: bbox center = (26.6, -81.8)
    assert canon["lat"] == pytest.approx(26.6, abs=1e-9)
    assert canon["lon"] == pytest.approx(-81.8, abs=1e-9)
    assert canon["bbox"] == [-81.9, 26.5, -81.7, 26.7]


def test_canonicalize_area_invalid_string_raises():
    with pytest.raises(NWSInputError, match="not a recognized"):
        _canonicalize_area("MUNICIPALITY")


def test_canonicalize_area_invalid_type_raises():
    with pytest.raises(NWSInputError, match="area must be"):
        _canonicalize_area(12345)  # type: ignore[arg-type]


def test_canonicalize_area_invalid_bbox_raises():
    with pytest.raises(NWSInputError):
        _canonicalize_area((10.0, 20.0, 10.0, 20.0))  # degenerate


# ---------------------------------------------------------------------------
# URL building tests (event_types filter, etc.).
# ---------------------------------------------------------------------------


def test_build_url_state_no_filter():
    """area='FL' with no event_types → simple area param."""
    canon = _canonicalize_area("FL")
    url = _build_nws_url(canon, None, "actual", "alert")
    assert url.startswith("https://api.weather.gov/alerts/active?")
    assert "area=FL" in url
    assert "status=actual" in url
    assert "message_type=alert" in url
    assert "event=" not in url


def test_build_url_with_event_types_filter_narrows_to_named_events():
    """event_types filter: NWS supports repeatable &event= per audit.md."""
    canon = _canonicalize_area("FL")
    url = _build_nws_url(
        canon, ["Hurricane Warning"], "actual", "alert",
    )
    # Hurricane Warning URL-encoded → "Hurricane%20Warning"
    assert "event=Hurricane%20Warning" in url, f"Bad URL: {url}"


def test_build_url_with_event_types_repeats_param():
    """Multiple event types → multiple &event= occurrences."""
    canon = _canonicalize_area("FL")
    url = _build_nws_url(
        canon, ["Hurricane Warning", "Flood Warning"], "actual", "alert",
    )
    # Count occurrences of "event=" in the query string.
    assert url.count("event=") == 2, f"Expected 2 event= params, got {url}"
    assert "Hurricane%20Warning" in url
    assert "Flood%20Warning" in url


def test_build_url_bbox_uses_point_param():
    """bbox → ?point=lat,lon (NOT ?area=)."""
    canon = _canonicalize_area(_FORT_MYERS_BBOX)
    url = _build_nws_url(canon, None, "actual", "alert")
    assert "point=" in url, f"Expected point= in URL: {url}"
    assert "area=" not in url
    # Verify the EXACT point coordinates per the audit.md spec.
    # urlencode encodes "," as %2C.
    assert "point=26.6%2C-81.8" in url, (
        f"Expected point=26.6,-81.8 (audit.md spec); URL: {url}"
    )


# ---------------------------------------------------------------------------
# User-Agent header verification.
# ---------------------------------------------------------------------------


def test_user_agent_header_sent_on_request():
    """Verify the User-Agent header is REQUIRED-AND-PRESENT on every NWS GET.

    NWS returns 403 without a descriptive User-Agent (audit.md spec).
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

    with patch("trid3nt_server.tools.fetch_nws_event.httpx.Client", FakeClient):
        from trid3nt_server.tools.fetch_nws_event import _fetch_nws_geojson
        _fetch_nws_geojson("https://api.weather.gov/alerts/active?area=FL")

    assert "User-Agent" in captured_headers, (
        f"User-Agent header missing! Captured: {captured_headers}"
    )
    ua = captured_headers["User-Agent"]
    assert "trid3nt-server" in ua, f"User-Agent should identify trid3nt-server: {ua!r}"
    assert "contact" in ua.lower(), (
        f"User-Agent should include a contact per NWS policy: {ua!r}"
    )


# ---------------------------------------------------------------------------
# Mocked end-to-end: FL state response with 3 active alerts.
# ---------------------------------------------------------------------------


def test_fl_state_response_with_three_alerts_writes_fgb():
    """Mocked FL state response: 3 alerts → FlatGeobuf written through cache."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_fl_geojson()

    with patch(
        "trid3nt_server.tools.fetch_nws_event._fetch_nws_geojson",
        return_value=fake_geojson,
    ), patch(
        "trid3nt_server.tools.fetch_nws_event.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_nws_event(area="FL")

    assert result.uri.startswith("s3://")
    assert "nws_event" in result.uri
    assert "dynamic-1h" in result.uri
    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    assert "State FL" in result.name
    assert len(fake_gcs.store) == 1, "One FGB should be in the fake GCS store"

    # Read back the FlatGeobuf from the fake store and confirm 3 features.
    fgb_bytes = next(iter(fake_gcs.store.values()))
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 3, f"Expected 3 features, got {len(gdf)}: {gdf}"
        events = gdf["event"].tolist()
        assert "Hurricane Warning" in events
        assert "Flood Warning" in events
        assert "Severe Thunderstorm Watch" in events
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Input validation tests.
# ---------------------------------------------------------------------------


def test_invalid_status_raises_input_error():
    with pytest.raises(NWSInputError, match="status="):
        fetch_nws_event(area="FL", status="bogus")


def test_invalid_message_type_raises_input_error():
    with pytest.raises(NWSInputError, match="message_type="):
        fetch_nws_event(area="FL", message_type="bogus")


def test_invalid_event_types_type_raises():
    with pytest.raises(NWSInputError, match="event_types must be"):
        fetch_nws_event(area="FL", event_types=[123])  # type: ignore[list-item]


def test_input_errors_are_not_retryable():
    """NWSInputError carries retryable=False for FR-AS-11."""
    try:
        fetch_nws_event(area="bogus")
    except NWSInputError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected NWSInputError")


# ---------------------------------------------------------------------------
# Upstream-error mapping.
# ---------------------------------------------------------------------------


def test_403_raises_typed_upstream_error_with_useragent_message():
    """403 from NWS surfaces as NWSUpstreamError naming the User-Agent."""
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

    with patch("trid3nt_server.tools.fetch_nws_event.httpx.Client", FakeClient):
        from trid3nt_server.tools.fetch_nws_event import _fetch_nws_geojson
        with pytest.raises(NWSUpstreamError, match="403"):
            _fetch_nws_geojson("https://api.weather.gov/alerts/active?area=FL")


def test_upstream_error_is_retryable():
    """NWSUpstreamError is retryable=True per audit.md."""
    err = NWSUpstreamError("test")
    assert err.retryable is True


def test_network_failure_wraps_to_upstream_error():
    """httpx.HTTPError → NWSUpstreamError."""
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            raise httpx.ConnectError("simulated DNS failure")

    with patch("trid3nt_server.tools.fetch_nws_event.httpx.Client", FakeClient):
        from trid3nt_server.tools.fetch_nws_event import _fetch_nws_geojson
        with pytest.raises(NWSUpstreamError, match="request failed"):
            _fetch_nws_geojson("https://api.weather.gov/alerts/active?area=FL")


# ---------------------------------------------------------------------------
# Cache-layer tests.
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_fetch_fn_then_hit_skips():
    """First call → cache miss → fetch_fn invoked; second call → cache hit → fetch_fn skipped."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_bytes = _fake_fgb_bytes("FL")

    def patched_fetch_bytes(canon_area, event_types, status, message_type):
        fetch_count["n"] += 1
        return fake_bytes

    with patch(
        "trid3nt_server.tools.fetch_nws_event._fetch_nws_event_bytes",
        side_effect=patched_fetch_bytes,
    ), patch(
        "trid3nt_server.tools.fetch_nws_event.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_nws_event(area="FL")
        r2 = fetch_nws_event(area="FL")

    assert fetch_count["n"] == 1, (
        f"Expected 1 call (hit on second); got {fetch_count['n']}"
    )
    assert r1.uri == r2.uri, "Both calls should resolve to the same cache key"


def test_cache_keys_differ_for_different_areas():
    """Different area → different cache key → different URI."""
    fake_gcs = FakeStorageClient()

    def patched_fetch_bytes(canon_area, event_types, status, message_type):
        return _fake_fgb_bytes(canon_area.get("value", "POINT"))

    with patch(
        "trid3nt_server.tools.fetch_nws_event._fetch_nws_event_bytes",
        side_effect=patched_fetch_bytes,
    ), patch(
        "trid3nt_server.tools.fetch_nws_event.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_fl = fetch_nws_event(area="FL")
        r_tx = fetch_nws_event(area="TX")
        r_bbox = fetch_nws_event(area=_FORT_MYERS_BBOX)

    uris = {r_fl.uri, r_tx.uri, r_bbox.uri}
    assert len(uris) == 3, f"Expected 3 distinct cache keys; got {uris}"


def test_event_types_filter_changes_cache_key():
    """A different event_types filter must produce a different cache key."""
    fake_gcs = FakeStorageClient()

    def patched_fetch_bytes(canon_area, event_types, status, message_type):
        return _fake_fgb_bytes("X")

    with patch(
        "trid3nt_server.tools.fetch_nws_event._fetch_nws_event_bytes",
        side_effect=patched_fetch_bytes,
    ), patch(
        "trid3nt_server.tools.fetch_nws_event.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_all = fetch_nws_event(area="FL")
        r_hurricane = fetch_nws_event(area="FL", event_types=["Hurricane Warning"])

    assert r_all.uri != r_hurricane.uri, (
        "event_types filter should change the cache key"
    )


def test_event_types_order_does_not_affect_cache_key():
    """Sorting event_types before keying: ['A','B'] and ['B','A'] hit same cache."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}

    def patched_fetch_bytes(canon_area, event_types, status, message_type):
        fetch_count["n"] += 1
        return _fake_fgb_bytes("X")

    with patch(
        "trid3nt_server.tools.fetch_nws_event._fetch_nws_event_bytes",
        side_effect=patched_fetch_bytes,
    ), patch(
        "trid3nt_server.tools.fetch_nws_event.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_nws_event(
            area="FL", event_types=["Hurricane Warning", "Flood Warning"],
        )
        r2 = fetch_nws_event(
            area="FL", event_types=["Flood Warning", "Hurricane Warning"],
        )

    assert r1.uri == r2.uri, "event_types sorting should produce same cache key"
    assert fetch_count["n"] == 1, "Second call should hit cache"


# ---------------------------------------------------------------------------
# LayerURI shape tests.
# ---------------------------------------------------------------------------


def test_layer_uri_shape_for_state_area():
    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetch_nws_event._fetch_nws_event_bytes",
        return_value=_fake_fgb_bytes("FL"),
    ), patch(
        "trid3nt_server.tools.fetch_nws_event.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_nws_event(area="FL")

    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    assert result.uri.startswith("s3://")
    assert "State FL" in result.name
    assert "NWS Active Alerts" in result.name


def test_layer_uri_shape_for_point_area():
    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetch_nws_event._fetch_nws_event_bytes",
        return_value=_fake_fgb_bytes("POINT"),
    ), patch(
        "trid3nt_server.tools.fetch_nws_event.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_nws_event(area=_FORT_MYERS_BBOX)

    # Per audit.md: bbox center = (26.6, -81.8)
    assert "26.6" in result.name
    assert "-81.8" in result.name


# ---------------------------------------------------------------------------
# GeoJSON → FlatGeobuf conversion tests.
# ---------------------------------------------------------------------------


def test_geojson_to_fgb_empty_collection_is_valid():
    """An empty NWS FeatureCollection still produces valid FGB bytes (≥0 features)."""
    from trid3nt_server.tools.fetch_nws_event import _geojson_to_fgb

    empty_geojson = {"type": "FeatureCollection", "features": []}
    fgb_bytes = _geojson_to_fgb(empty_geojson)
    assert len(fgb_bytes) > 0

    # Verify round-trip via geopandas.
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 0, f"Expected 0 features; got {len(gdf)}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_geojson_to_fgb_preserves_properties():
    """Audit.md spec preserved-properties survive the GeoJSON → FGB round-trip."""
    from trid3nt_server.tools.fetch_nws_event import _geojson_to_fgb

    geojson = _sample_fl_geojson()
    fgb_bytes = _geojson_to_fgb(geojson)

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 3
        # All audit.md-mandated properties should be present as columns.
        for required in ("event", "headline", "description", "severity",
                          "urgency", "certainty", "effective", "onset",
                          "ends", "senderName"):
            assert required in gdf.columns, (
                f"Audit.md-required column {required!r} missing; "
                f"have: {list(gdf.columns)}"
            )
        # Verify a known property value.
        hurricane_row = gdf[gdf["event"] == "Hurricane Warning"]
        assert len(hurricane_row) == 1
        assert hurricane_row.iloc[0]["severity"] == "Extreme"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Live integration test (TRID3NT_TEST_LIVE_NWS=1 to run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_NWS,
    reason="Set TRID3NT_TEST_LIVE_NWS=1 to run live NWS api.weather.gov tests",
)
def test_live_florida_state_returns_valid_response():
    """LIVE: real api.weather.gov call for area='FL' returns valid FGB (≥0 features).

    Empty FeatureCollection is LEGITIMATE — most of the time there are no
    active hurricane warnings. We assert the FGB round-trips, NOT that
    features are non-empty.
    """
    fgb_bytes = _fetch_nws_event_bytes(
        canon_area={"kind": "state", "value": "FL"},
        event_types=None,
        status="actual",
        message_type="alert",
    )
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        # ≥0 features is OK (zero alerts is the most common steady state).
        assert len(gdf) >= 0
        print(f"\n[LIVE NWS] FL state returned {len(gdf)} active alert(s)")
        if len(gdf) > 0:
            print(f"  events: {gdf['event'].tolist()}")
            print(f"  columns: {list(gdf.columns)}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.skipif(
    not _LIVE_NWS,
    reason="Set TRID3NT_TEST_LIVE_NWS=1 to run live NWS api.weather.gov tests",
)
def test_live_hurricane_warning_filter():
    """LIVE: filter to Hurricane Warning + Flood Warning for FL — per audit.md."""
    fgb_bytes = _fetch_nws_event_bytes(
        canon_area={"kind": "state", "value": "FL"},
        event_types=["Hurricane Warning", "Flood Warning"],
        status="actual",
        message_type="alert",
    )
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        # Filter-narrowing: any present event must be in the filter list.
        if len(gdf) > 0:
            events = set(gdf["event"].tolist())
            allowed = {"Hurricane Warning", "Flood Warning"}
            assert events.issubset(allowed), (
                f"Filter should narrow events; got {events}, allowed {allowed}"
            )
            print(f"\n[LIVE NWS] FL Hurricane+Flood filter → {len(gdf)} alert(s)")
        else:
            print("\n[LIVE NWS] FL Hurricane+Flood filter → 0 alerts (steady state)")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# job-0261: full state names accepted ("Texas" → state TX), so location text
# the LLM passes verbatim engages the precise server-side ?area= filter
# instead of erroring into the unscoped CONUS fallback.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,code",
    [
        ("Texas", "TX"),
        ("texas", "TX"),           # the live-demo prompt form
        ("  Texas  ", "TX"),
        ("State of Texas", "TX"),
        ("Florida", "FL"),
        ("new mexico", "NM"),
        ("Puerto Rico", "PR"),
    ],
)
def test_canonicalize_area_full_state_name(raw, code):
    from trid3nt_server.tools.fetch_nws_event import _canonicalize_area
    assert _canonicalize_area(raw) == {"kind": "state", "value": code}


def test_canonicalize_area_full_name_builds_area_param_url():
    """'Texas' canonicalizes to the same URL as 'TX' (?area=TX)."""
    from trid3nt_server.tools.fetch_nws_event import (
        _build_nws_url,
        _canonicalize_area,
    )
    url_name = _build_nws_url(_canonicalize_area("Texas"), None, "actual", "alert")
    url_code = _build_nws_url(_canonicalize_area("TX"), None, "actual", "alert")
    assert url_name == url_code
    assert "area=TX" in url_name


def test_canonicalize_area_city_still_rejected():
    """Cities are still not valid areas — typed input error, not a silent
    nationwide fallback."""
    from trid3nt_server.tools.fetch_nws_event import NWSInputError, _canonicalize_area
    with pytest.raises(NWSInputError, match="not a recognized"):
        _canonicalize_area("Houston")


def test_bbox_fallback_unchanged_by_state_name_support():
    """job-0261 must not disturb the bbox→point path (non-state areas)."""
    from trid3nt_server.tools.fetch_nws_event import _canonicalize_area
    canon = _canonicalize_area((-106.6, 25.8, -93.5, 36.5))  # Texas-ish bbox
    assert canon["kind"] == "point"
    assert canon["lat"] == pytest.approx((25.8 + 36.5) / 2, abs=1e-4)
    assert canon["lon"] == pytest.approx((-106.6 + -93.5) / 2, abs=1e-4)
