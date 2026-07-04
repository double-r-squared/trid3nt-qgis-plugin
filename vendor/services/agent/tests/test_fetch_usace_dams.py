"""Unit tests for the ``fetch_usace_dams`` atomic tool (job-A5).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- bbox helpers (envelope format, validation, 6dp rounding).
- _build_nid_url emits pagination + bbox params correctly.
- estimate_payload_mb scales with bbox area (CONUS sweep ~50 MB cap;
  small bbox proportionally smaller).
- User-Agent header is sent on every NID GET.
- Mocked end-to-end: synthetic NID GeoJSON → FGB written through cache.
- Bbox vs global cache keys differ.
- Pagination loop terminates on short page; respects max_features cap.
- Invalid bbox raises typed USACEDAMSInputError (retryable=False).
- 500 / network failure / ArcGIS error envelope → USACEDAMSUpstreamError(retryable=True).
- Cache miss → fetch_fn invoked once; cache hit → fetch_fn skipped.
- LayerURI shape matches contract.
- Geographic-correctness gate: point centroids fall inside the US envelope.
- Live (env GRACE2_TEST_LIVE_USACE_DAMS=1): real NID FeatureService small-bbox
  query returns ≥1 dam point in the Fort Myers area.
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
from grace2_agent.tools.fetch_usace_dams import (
    CONUS_BBOX,
    PRESERVED_PROPERTIES,
    USACEDAMSError,
    USACEDAMSInputError,
    USACEDAMSUpstreamError,
    _bbox_to_envelope,
    _build_nid_url,
    _fetch_nid_bytes,
    _fetch_nid_geojson_page,
    _fetch_nid_all_features,
    _geojson_to_fgb,
    _round_bbox_to_6dp,
    _validate_bbox,
    estimate_payload_mb,
    fetch_usace_dams,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)

_LIVE = os.environ.get("GRACE2_TEST_LIVE_USACE_DAMS") == "1"

# US-dam envelope for the geographic-correctness gate. Covers CONUS, AK,
# HI. Every NID dam point centroid should fall inside this box.
_US_LON_RANGE = (-180.0, -65.0)
_US_LAT_RANGE = (13.0, 72.0)


def _fake_fgb_bytes(tag: str = "TEST") -> bytes:
    return b"FAKE_NID_FGB_" + tag.encode() + b"\x00" * 16


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


# ---------------------------------------------------------------------------
# Synthetic feature builders.
# ---------------------------------------------------------------------------


def _make_dam_feature(
    name: str,
    lon: float,
    lat: float,
    *,
    nidid: str = "ZZ00001",
    state: str = "FL",
    hazard: str = "High",
    height: float = 50.0,
    storage: float = 10000.0,
    year: int = 1970,
    object_id: int = 0,
) -> dict:
    """Build one synthetic NID dam feature with a point geometry."""
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [lon, lat],
        },
        "properties": {
            "OBJECTID": object_id,
            "NIDID": nidid,
            "FEDERAL_ID": None,
            "NAME": name,
            "OTHER_NAMES": None,
            "STATE": state,
            "COUNTYSTATE": f"Test County, {state}",
            "CITY": "TestCity",
            "LATITUDE": lat,
            "LONGITUDE": lon,
            "RIVER_OR_STREAM": "Test Creek",
            "CONGDIST": f"{state}-01",
            "OWNER_TYPES": "Local Government",
            "PRIMARY_OWNER_TYPE": "Local Government",
            "STATE_REGULATED": "Yes",
            "STATE_JURISDICTION": "Yes",
            "STATE_REGULATORY_AGENCY": f"{state} DEP",
            "PRIMARY_SOURCE_AGENCY": f"{state} DEP",
            "PRIMARY_PURPOSE": "Flood Risk Reduction",
            "PURPOSES": "Flood Risk Reduction, Recreation",
            "PRIMARY_DAM_TYPE": "Earth",
            "DAM_TYPES": "Earth",
            "DAM_HEIGHT": height,
            "HYDRAULIC_HEIGHT": height - 5,
            "STRUCTURAL_HEIGHT": height,
            "NID_HEIGHT": int(height),
            "DAM_LENGTH": 1000.0,
            "DAM_VOLUME": 100000,
            "YEAR_COMPLETED": year,
            "NID_STORAGE": storage,
            "MAX_STORAGE": storage * 1.2,
            "NORMAL_STORAGE": storage * 0.8,
            "SURFACE_AREA": 100.0,
            "DRAINAGE_AREA": 500.0,
            "MAX_DISCHARGE": 5000.0,
            "SPILLWAY_TYPE": "Uncontrolled",
            "SPILLWAY_WIDTH": 50,
            "HAZARD_POTENTIAL": hazard,
            "CONDITION_ASSESSMENT": "Satisfactory",
            "CONDITION_ASSESS_DATE": "2024-01-01",
            "EAP_PREPARED": "Yes",
            "EAP_LAST_REV_DATE": "2023-06-01",
            "LAST_INSPECTION_DATE": "2024-06-15",
            "INSPECTION_FREQUENCY": "2 yr",
            "OPERATIONAL_STATUS": "Active",
            "OPERATIONAL_STATUS_DATE": "2024-01-01",
            "DATA_UPDATED": 1733011200000,
        },
    }


def _sample_nid_geojson(n_features: int = 5) -> dict:
    """Synthetic NID-shaped FeatureCollection spread across the US."""
    anchors = [
        ("Hoover Dam", -114.7373, 36.0161, "NV"),
        ("Grand Coulee Dam", -118.9809, 47.9568, "WA"),
        ("Glen Canyon Dam", -111.4849, 36.9374, "AZ"),
        ("Oroville Dam", -121.4847, 39.5384, "CA"),
        ("Fort Peck Dam", -106.4156, 48.0042, "MT"),
        ("Garrison Dam", -101.4254, 47.4979, "ND"),
        ("Toledo Bend Dam", -93.7236, 31.1747, "LA"),
        ("Buford Dam", -84.0691, 34.1599, "GA"),
        ("Carters Dam", -84.6727, 34.6122, "GA"),
        ("Lake Mead — Test Anchor", -114.5, 36.1, "NV"),
    ]
    features: list[dict] = []
    for i in range(n_features):
        name, lon, lat, state = anchors[i % len(anchors)]
        features.append(_make_dam_feature(
            f"{name} #{i}",
            lon,
            lat,
            nidid=f"{state}9{i:05d}",
            state=state,
            object_id=i,
        ))
    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# Registration tests (no network needed).
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_usace_dams appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_usace_dams" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_usace_dams"]
    assert entry.metadata.name == "fetch_usace_dams"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "usace_nid_dams"
    assert entry.metadata.cacheable is True
    assert entry.metadata.supports_global_query is True
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


# ---------------------------------------------------------------------------
# bbox / URL-builder tests.
# ---------------------------------------------------------------------------


def test_bbox_to_envelope_format():
    """ArcGIS envelope is xmin,ymin,xmax,ymax — no JSON wrapping."""
    env = _bbox_to_envelope((-82.5, 26.0, -81.0, 27.0))
    assert env == "-82.5,26.0,-81.0,27.0"


def test_build_url_with_bbox_includes_geometry_and_pagination():
    """A bbox query sends geometry + geometryType + inSR + pagination params."""
    url, params = _build_nid_url((-82.5, 26.0, -81.0, 27.0))
    assert url.endswith("FeatureServer/0/query")
    assert params["f"] == "geojson"
    assert params["outFields"]  # non-empty allow-list
    assert "NIDID" in params["outFields"]
    assert "HAZARD_POTENTIAL" in params["outFields"]
    assert params["outSR"] == "4326"
    assert params["inSR"] == "4326"
    assert params["geometryType"] == "esriGeometryEnvelope"
    assert params["geometry"] == "-82.5,26.0,-81.0,27.0"
    assert params["where"] == "1=1"
    assert params["resultOffset"] == "0"
    assert params["resultRecordCount"] == "2000"
    assert "orderByFields" in params


def test_build_url_without_bbox_omits_geometry():
    """A None bbox query OMITS geometry / inSR / geometryType params."""
    url, params = _build_nid_url(None)
    assert "geometry" not in params
    assert "geometryType" not in params
    assert "inSR" not in params
    assert params["f"] == "geojson"


def test_build_url_pagination_offsets():
    """resultOffset is forwarded; resultRecordCount caps at server max (2000)."""
    url, params = _build_nid_url(None, result_offset=4000, result_record_count=5000)
    assert params["resultOffset"] == "4000"
    # Server enforces 2000; we cap silently to match.
    assert params["resultRecordCount"] == "2000"


def test_validate_bbox_rejects_degenerate():
    """Degenerate / out-of-range bboxes raise USACEDAMSInputError."""
    with pytest.raises(USACEDAMSInputError, match="degenerate"):
        _validate_bbox((-120.0, 40.0, -125.0, 38.0))  # min_lon > max_lon
    with pytest.raises(USACEDAMSInputError, match="lon out of"):
        _validate_bbox((-200.0, 40.0, -120.0, 42.0))
    with pytest.raises(USACEDAMSInputError, match="non-finite"):
        _validate_bbox((float("nan"), 40.0, -120.0, 42.0))


def test_round_bbox_to_6dp_quantizes():
    """6dp rounding is deterministic for cache-key stability."""
    q = _round_bbox_to_6dp((-124.123456789, 32.5, -114.1, 42.000001234))
    assert q == (-124.123457, 32.5, -114.1, 42.000001)


# ---------------------------------------------------------------------------
# Payload estimator.
# ---------------------------------------------------------------------------


def test_payload_estimate_global_sweep_caps_at_50mb():
    """estimate_payload_mb with no bbox returns the cap-limited CONUS estimate."""
    mb = estimate_payload_mb()
    # 50000 features * 1024 bytes = ~48.8 MB.
    assert 40.0 < mb < 60.0


def test_payload_estimate_small_bbox_is_small():
    """A single-county-sized bbox returns a small estimate (<2 MB)."""
    fort_myers = (-82.5, 26.0, -81.0, 27.0)
    mb = estimate_payload_mb(bbox=fort_myers)
    assert 0.0 < mb < 2.0


def test_payload_estimate_scales_with_area():
    """Larger bbox → larger estimate."""
    small = estimate_payload_mb(bbox=(-82.5, 26.0, -81.0, 27.0))
    large = estimate_payload_mb(bbox=(-125.0, 32.0, -114.0, 42.0))
    assert large > small


def test_payload_estimate_handles_garbage_input():
    """Garbage bbox falls back to the global cap estimate without raising."""
    mb_str = estimate_payload_mb(bbox="not-a-bbox")
    mb_tuple_wrong_len = estimate_payload_mb(bbox=(1.0, 2.0))
    mb_non_numeric = estimate_payload_mb(bbox=("a", "b", "c", "d"))
    assert mb_str > 0.0
    assert mb_tuple_wrong_len > 0.0
    assert mb_non_numeric > 0.0


# ---------------------------------------------------------------------------
# User-Agent header verification.
# ---------------------------------------------------------------------------


def test_user_agent_header_sent_on_request():
    """Verify the User-Agent header is present on every NID GET."""
    captured_headers: dict[str, str] = {}

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

        def get(self, url, params=None, headers=None):
            captured_headers.update(headers or {})
            return FakeResponse()

    with patch("grace2_agent.tools.fetch_usace_dams.httpx.Client", FakeClient):
        _fetch_nid_geojson_page(
            "https://services2.arcgis.com/.../FeatureServer/0/query",
            {"f": "geojson"},
        )

    assert "User-Agent" in captured_headers, (
        f"User-Agent header missing! Captured: {captured_headers}"
    )
    ua = captured_headers["User-Agent"]
    assert "grace-2" in ua, f"User-Agent should identify grace-2: {ua!r}"


# ---------------------------------------------------------------------------
# Mocked end-to-end: synthetic response writes FGB.
# ---------------------------------------------------------------------------


def test_synthetic_response_writes_fgb_with_correct_count():
    """Mocked 5-feature NID response → 5-point FlatGeobuf written through cache."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nid_geojson(5)

    with patch(
        "grace2_agent.tools.fetch_usace_dams._fetch_nid_all_features",
        return_value=fake_geojson,
    ), patch(
        "grace2_agent.tools.fetch_usace_dams.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_usace_dams(bbox=(-125.0, 25.0, -65.0, 50.0))

    assert result.uri.startswith("s3://")
    assert "usace_nid_dams" in result.uri
    assert "static-30d" in result.uri
    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.units is None
    assert "USACE National Inventory of Dams" in result.name
    assert len(fake_gcs.store) == 1

    fgb_bytes = next(iter(fake_gcs.store.values()))
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 5, f"Expected 5 features, got {len(gdf)}"
        # Spot-check the preserved properties.
        assert "NIDID" in gdf.columns
        assert "NAME" in gdf.columns
        assert "HAZARD_POTENTIAL" in gdf.columns
        assert "DAM_HEIGHT" in gdf.columns
        # Every geometry should be a point.
        assert (gdf.geometry.geom_type == "Point").all()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_bbox_vs_global_cache_keys_differ():
    """A bbox call produces a different cache key than the global sweep."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nid_geojson(2)

    with patch(
        "grace2_agent.tools.fetch_usace_dams._fetch_nid_all_features",
        return_value=fake_geojson,
    ), patch(
        "grace2_agent.tools.fetch_usace_dams.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_global = fetch_usace_dams()
        r_bbox = fetch_usace_dams(bbox=(-82.5, 26.0, -81.0, 27.0))

    assert r_global.uri != r_bbox.uri
    assert "global" in r_global.layer_id
    assert "global" not in r_bbox.layer_id
    assert len(fake_gcs.store) == 2


# ---------------------------------------------------------------------------
# Pagination loop.
# ---------------------------------------------------------------------------


def test_pagination_loop_stops_on_short_page():
    """_fetch_nid_all_features stops requesting once a page returns < page_size."""
    call_counts = {"n": 0}
    short_page_features = _sample_nid_geojson(3)["features"]

    def fake_page(url, params):
        call_counts["n"] += 1
        # Always return a "short" page → loop should exit after one fetch.
        return {"type": "FeatureCollection", "features": short_page_features}

    with patch(
        "grace2_agent.tools.fetch_usace_dams._fetch_nid_geojson_page",
        side_effect=fake_page,
    ):
        out = _fetch_nid_all_features(bbox=None)

    assert call_counts["n"] == 1
    assert len(out["features"]) == 3


def test_pagination_loop_respects_max_features_cap():
    """Pagination terminates when accumulated count reaches max_features."""
    # Each page returns a full 2000 features → loop should hit the 4500 cap
    # after the 3rd page (truncating to 4500).
    base_features = _sample_nid_geojson(10)["features"]
    full_page = (base_features * 200)[:2000]

    def fake_page(url, params):
        return {"type": "FeatureCollection", "features": list(full_page)}

    with patch(
        "grace2_agent.tools.fetch_usace_dams._fetch_nid_geojson_page",
        side_effect=fake_page,
    ):
        out = _fetch_nid_all_features(bbox=None, max_features=4500)

    assert len(out["features"]) == 4500


# ---------------------------------------------------------------------------
# Input-validation tests.
# ---------------------------------------------------------------------------


def test_invalid_bbox_raises_input_error():
    """Degenerate bbox raises USACEDAMSInputError, not a generic exception."""
    with pytest.raises(USACEDAMSInputError, match="degenerate"):
        fetch_usace_dams(bbox=(-120.0, 40.0, -125.0, 38.0))


def test_input_errors_are_not_retryable():
    """USACEDAMSInputError carries retryable=False for FR-AS-11."""
    try:
        fetch_usace_dams(bbox=(-200.0, 40.0, -120.0, 42.0))
    except USACEDAMSInputError as exc:
        assert exc.retryable is False
        assert exc.error_code == "USACE_DAMS_INPUT_INVALID"
    else:
        pytest.fail("Expected USACEDAMSInputError")


def test_extra_kwargs_absorbed_by_signature():
    """job-0164: invented Gemini kwargs must NOT raise TypeError."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nid_geojson(2)
    with patch(
        "grace2_agent.tools.fetch_usace_dams._fetch_nid_all_features",
        return_value=fake_geojson,
    ), patch(
        "grace2_agent.tools.fetch_usace_dams.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        # These kwargs are not declared but must be absorbed.
        result = fetch_usace_dams(
            bbox=(-82.5, 26.0, -81.0, 27.0),
            include_low_hazard=True,
            limit=100,
            format="geojson",
        )
    assert result.uri.startswith("s3://")


# ---------------------------------------------------------------------------
# Upstream-error mapping.
# ---------------------------------------------------------------------------


def test_500_raises_typed_upstream_error():
    """500 from NID surfaces as USACEDAMSUpstreamError(retryable=True)."""
    class FakeResponse:
        status_code = 500
        text = "Internal Server Error"

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            return FakeResponse()

    with patch("grace2_agent.tools.fetch_usace_dams.httpx.Client", FakeClient):
        with pytest.raises(USACEDAMSUpstreamError, match="500"):
            _fetch_nid_geojson_page(
                "https://services2.arcgis.com/.../FeatureServer/0/query",
                {"f": "geojson"},
            )


def test_network_failure_wraps_to_upstream_error():
    """httpx.HTTPError → USACEDAMSUpstreamError."""
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            raise httpx.ConnectError("simulated DNS failure")

    with patch("grace2_agent.tools.fetch_usace_dams.httpx.Client", FakeClient):
        with pytest.raises(USACEDAMSUpstreamError, match="request failed"):
            _fetch_nid_geojson_page(
                "https://services2.arcgis.com/.../FeatureServer/0/query",
                {"f": "geojson"},
            )


def test_arcgis_error_envelope_in_200_body_raises():
    """ArcGIS error envelopes inside a 200 body raise USACEDAMSUpstreamError."""
    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"error": {"code": 400, "message": "Invalid query"}}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            return FakeResponse()

    with patch("grace2_agent.tools.fetch_usace_dams.httpx.Client", FakeClient):
        with pytest.raises(USACEDAMSUpstreamError, match="error envelope"):
            _fetch_nid_geojson_page(
                "https://services2.arcgis.com/.../FeatureServer/0/query",
                {"f": "geojson"},
            )


def test_non_feature_collection_raises():
    """A 200 body that isn't a FeatureCollection raises USACEDAMSUpstreamError."""
    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"type": "Feature", "geometry": None}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            return FakeResponse()

    with patch("grace2_agent.tools.fetch_usace_dams.httpx.Client", FakeClient):
        with pytest.raises(USACEDAMSUpstreamError, match="FeatureCollection"):
            _fetch_nid_geojson_page(
                "https://services2.arcgis.com/.../FeatureServer/0/query",
                {"f": "geojson"},
            )


def test_upstream_error_is_retryable():
    """USACEDAMSUpstreamError is retryable=True."""
    err = USACEDAMSUpstreamError("test")
    assert err.retryable is True
    assert err.error_code == "USACE_DAMS_UPSTREAM_ERROR"


# ---------------------------------------------------------------------------
# Cache-layer tests.
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_fetch_fn_then_hit_skips():
    """First call → cache miss → fetch_fn invoked; second call → cache hit."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_bytes = _fake_fgb_bytes("BBOX")

    def patched_fetch_bytes(bbox, **_kw):
        fetch_count["n"] += 1
        return fake_bytes

    with patch(
        "grace2_agent.tools.fetch_usace_dams._fetch_nid_bytes",
        side_effect=patched_fetch_bytes,
    ), patch(
        "grace2_agent.tools.fetch_usace_dams.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_usace_dams(bbox=(-82.5, 26.0, -81.0, 27.0))
        r2 = fetch_usace_dams(bbox=(-82.5, 26.0, -81.0, 27.0))

    assert fetch_count["n"] == 1, (
        f"Expected 1 call (hit on second); got {fetch_count['n']}"
    )
    assert r1.uri == r2.uri


# ---------------------------------------------------------------------------
# LayerURI shape.
# ---------------------------------------------------------------------------


def test_layer_uri_shape_for_global_sweep():
    """Global sweep produces a LayerURI tagged role=primary, layer_type=vector."""
    fake_gcs = FakeStorageClient()
    with patch(
        "grace2_agent.tools.fetch_usace_dams._fetch_nid_bytes",
        return_value=_fake_fgb_bytes("GLOBAL"),
    ), patch(
        "grace2_agent.tools.fetch_usace_dams.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_usace_dams()

    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.units is None
    assert result.uri.startswith("s3://")
    assert "USACE National Inventory of Dams" in result.name
    assert result.layer_id == "usace-nid-dams-global"
    assert result.style_preset == "usace_nid_dams"


# ---------------------------------------------------------------------------
# Geographic-correctness gate.
# ---------------------------------------------------------------------------


def test_geographic_gate_all_points_fall_inside_us_envelope():
    """Every NID dam point centroid is inside the US envelope after FGB round-trip.

    A sign-flip or axis-swap in the GeoJSON → FlatGeobuf conversion would put
    points on the wrong continent — exactly the regression job-0086 codified.
    """
    fake_geojson = _sample_nid_geojson(7)

    lon_min, lon_max = _US_LON_RANGE
    lat_min, lat_max = _US_LAT_RANGE

    # Input sanity check.
    for feat in fake_geojson["features"]:
        cx, cy = feat["geometry"]["coordinates"]
        assert lon_min <= cx <= lon_max
        assert lat_min <= cy <= lat_max

    fgb_bytes = _geojson_to_fgb(fake_geojson)
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 7

        for idx, geom in enumerate(gdf.geometry):
            assert lon_min <= geom.x <= lon_max, (
                f"Point {idx} x={geom.x} outside US lon envelope — axis-swap bug"
            )
            assert lat_min <= geom.y <= lat_max, (
                f"Point {idx} y={geom.y} outside US lat envelope — axis-swap bug"
            )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_geojson_to_fgb_empty_collection_is_valid():
    """Empty NID FeatureCollection still produces valid FGB bytes."""
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


def test_geojson_to_fgb_drops_null_geometry_rows():
    """Features without a geometry are dropped (NID is point-only)."""
    mixed = {
        "type": "FeatureCollection",
        "features": [
            _make_dam_feature("With geom", -82.0, 26.5, object_id=1),
            {
                "type": "Feature",
                "geometry": None,
                "properties": {
                    "OBJECTID": 2,
                    "NIDID": "NULL01",
                    "NAME": "Null Geom Dam",
                },
            },
            _make_dam_feature("With geom 2", -114.7, 36.0, object_id=3),
        ],
    }
    fgb_bytes = _geojson_to_fgb(mixed)
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 2
        names = set(gdf["NAME"].tolist())
        assert "Null Geom Dam" not in names
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Live integration test (GRACE2_TEST_LIVE_USACE_DAMS=1 to run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE,
    reason="Set GRACE2_TEST_LIVE_USACE_DAMS=1 to run live NID tests",
)
def test_live_fort_myers_bbox_returns_dams():
    """LIVE: real NID FeatureService Fort Myers bbox returns ≥1 dam point."""
    # Fort Myers / Cape Coral area — known to have ~10-20 NID dams.
    bbox = (-82.5, 26.0, -81.0, 27.0)
    url, params = _build_nid_url(bbox, result_offset=0, result_record_count=200)
    body = _fetch_nid_geojson_page(url, params)
    assert body.get("type") == "FeatureCollection"
    features = body.get("features", [])
    assert len(features) >= 1, "Expected ≥1 dam point in Fort Myers area"

    lon_min, lon_max = _US_LON_RANGE
    lat_min, lat_max = _US_LAT_RANGE
    for feat in features:
        coords = feat.get("geometry", {}).get("coordinates", [])
        assert len(coords) == 2
        lon, lat = coords
        assert lon_min <= lon <= lon_max
        assert lat_min <= lat <= lat_max
        props = feat.get("properties", {})
        # NIDID is the canonical join key — must be present.
        assert props.get("NIDID"), f"Feature missing NIDID: {props}"


# ===========================================================================
# job-A5 UPGRADE tests — authoritative endpoint behind the credential path,
# live hazard_potential / state filters, authoritative -> mirror -> error
# degradation. (Appended; do not interleave with the original block.)
# ===========================================================================

from grace2_agent.tools.fetch_usace_dams import (  # noqa: E402
    USACEDAMSAuthError,
    _NID_AUTHORITATIVE_BASE,
    _NID_BASE,
    _build_where_clause,
    _fetch_nid_bytes,
    _resolve_nid_token,
    _validate_hazard_potential,
    _validate_state,
    set_persistence_for_secrets,
)


# ---------------------------------------------------------------------------
# Filter validation + WHERE-clause construction.
# ---------------------------------------------------------------------------


def test_validate_hazard_potential_normalizes_case_and_dedupes():
    assert _validate_hazard_potential("high") == ["High"]
    assert _validate_hazard_potential(["HIGH", "significant", "High"]) == [
        "High",
        "Significant",
    ]
    assert _validate_hazard_potential(None) == []


def test_validate_hazard_potential_rejects_unknown():
    with pytest.raises(USACEDAMSInputError, match="not a valid NID classification"):
        _validate_hazard_potential("catastrophic")


def test_validate_hazard_potential_rejects_non_string_entry():
    with pytest.raises(USACEDAMSInputError):
        _validate_hazard_potential([123])  # type: ignore[list-item]


def test_validate_state_expands_abbreviation_and_titlecases():
    assert _validate_state("nv") == ["Nevada"]
    assert _validate_state("NC") == ["North Carolina"]
    assert _validate_state("north carolina") == ["North Carolina"]
    assert _validate_state(["nevada", "AZ"]) == ["Nevada", "Arizona"]
    assert _validate_state(None) == []


def test_validate_state_rejects_empty():
    with pytest.raises(USACEDAMSInputError):
        _validate_state("   ")


def test_build_where_clause_composes_filters():
    assert _build_where_clause([], []) == "1=1"
    assert (
        _build_where_clause(["High"], [])
        == "HAZARD_POTENTIAL IN ('High')"
    )
    assert (
        _build_where_clause([], ["Nevada"])
        == "STATE IN ('Nevada')"
    )
    assert (
        _build_where_clause(["High", "Significant"], ["Nevada"])
        == "HAZARD_POTENTIAL IN ('High','Significant') AND STATE IN ('Nevada')"
    )


def test_build_where_clause_sql_escapes_single_quote():
    # Defense-in-depth: a single quote in a (synthetic) value is doubled.
    assert _build_where_clause([], ["O'Brien County"]) == (
        "STATE IN ('O''Brien County')"
    )


def test_build_url_threads_where_token_and_base():
    base, params = _build_nid_url(
        None,
        where="HAZARD_POTENTIAL IN ('High')",
        base_url=_NID_AUTHORITATIVE_BASE,
        token="TKN",
    )
    assert base == _NID_AUTHORITATIVE_BASE
    assert params["where"] == "HAZARD_POTENTIAL IN ('High')"
    assert params["token"] == "TKN"


def test_build_url_omits_token_when_none():
    _base, params = _build_nid_url(None)
    assert "token" not in params
    assert params["where"] == "1=1"


# ---------------------------------------------------------------------------
# Token resolution (canonical 3-path secret loader).
# ---------------------------------------------------------------------------


def test_resolve_token_returns_none_when_no_source(monkeypatch):
    monkeypatch.delenv("GRACE2_USACE_NID_TOKEN", raising=False)
    assert _resolve_nid_token(None, None) is None


def test_resolve_token_prefers_explicit_kwarg(monkeypatch):
    monkeypatch.setenv("GRACE2_USACE_NID_TOKEN", "env-tok")
    assert _resolve_nid_token("kwarg-tok", None) == "kwarg-tok"


def test_resolve_token_uses_string_secret_ref(monkeypatch):
    monkeypatch.delenv("GRACE2_USACE_NID_TOKEN", raising=False)
    assert _resolve_nid_token(None, "secret-tok") == "secret-tok"


def test_resolve_token_falls_back_to_env(monkeypatch):
    monkeypatch.delenv("GRACE2_USACE_NID_TOKEN", raising=False)
    monkeypatch.setenv("GRACE2_USACE_NID_TOKEN", "env-tok")
    assert _resolve_nid_token(None, None) == "env-tok"


def test_resolve_token_secret_ref_via_persistence_then_reset():
    """A non-string secret_ref resolves through the bound Persistence mock."""
    class _FakePersistence:
        async def get_secret_value(self, ref):  # noqa: D401
            return f"resolved::{ref['id']}"

    set_persistence_for_secrets(_FakePersistence())
    try:
        tok = _resolve_nid_token(None, {"id": "abc"})
        assert tok == "resolved::abc"
    finally:
        set_persistence_for_secrets(None)


def test_resolve_token_persistence_unbound_for_object_ref_raises():
    """A non-string secret_ref with NO bound Persistence is a credential error."""
    set_persistence_for_secrets(None)
    with pytest.raises(USACEDAMSAuthError):
        _resolve_nid_token(None, {"id": "abc"})


# ---------------------------------------------------------------------------
# Authoritative ESRI token-error envelope -> USACEDAMSAuthError.
# ---------------------------------------------------------------------------


def _token_envelope_client(code: int):
    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"error": {"code": code, "message": "Token", "details": []}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            return FakeResponse()

    return FakeClient


def test_esri_499_token_required_raises_auth_error():
    with patch(
        "grace2_agent.tools.fetch_usace_dams.httpx.Client",
        _token_envelope_client(499),
    ):
        with pytest.raises(USACEDAMSAuthError):
            _fetch_nid_geojson_page(_NID_AUTHORITATIVE_BASE, {"f": "json"})


def test_esri_498_invalid_token_raises_auth_error():
    with patch(
        "grace2_agent.tools.fetch_usace_dams.httpx.Client",
        _token_envelope_client(498),
    ):
        with pytest.raises(USACEDAMSAuthError):
            _fetch_nid_geojson_page(_NID_AUTHORITATIVE_BASE, {"f": "json"})


def test_http_401_from_authoritative_raises_auth_error():
    class FakeResponse:
        status_code = 401
        text = "Unauthorized"

        def json(self):
            return {}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            return FakeResponse()

    with patch("grace2_agent.tools.fetch_usace_dams.httpx.Client", FakeClient):
        with pytest.raises(USACEDAMSAuthError):
            _fetch_nid_geojson_page(_NID_AUTHORITATIVE_BASE, {"f": "json"})


def test_auth_error_is_credential_shaped_for_generic_card():
    """The credential pipeline classifies our auth error WITHOUT a registry row."""
    from grace2_agent.credential_registry import is_credential_shaped_error

    err = USACEDAMSAuthError("USACE NID requires a valid token (ESRI code 498)")
    assert is_credential_shaped_error("fetch_usace_dams", err) is True
    # A plain upstream error must NOT trip the credential gate (no false positive).
    up = USACEDAMSUpstreamError("USACE NID returned HTTP 503 maintenance")
    assert is_credential_shaped_error("fetch_usace_dams", up) is False


# ---------------------------------------------------------------------------
# authoritative -> mirror -> error orchestration (synthetic, no network).
# ---------------------------------------------------------------------------


def test_fetch_bytes_no_token_uses_mirror_only():
    """No token => only the mirror base is ever queried (never authoritative)."""
    seen: list[str] = []

    def recording_page(url, params):
        seen.append(url)
        # Short page so pagination stops immediately.
        return {"type": "FeatureCollection", "features": []}

    with patch(
        "grace2_agent.tools.fetch_usace_dams._fetch_nid_geojson_page",
        side_effect=recording_page,
    ):
        _fetch_nid_bytes(None, where="1=1", token=None)

    assert seen, "expected at least one page fetch"
    assert all(_NID_BASE in u for u in seen)
    assert all(_NID_AUTHORITATIVE_BASE not in u for u in seen)


def test_fetch_bytes_token_attempts_authoritative_then_degrades_to_mirror():
    """Token set: authoritative tried first; on non-auth failure -> mirror."""
    calls: list[str] = []

    def page(url, params):
        calls.append(url)
        if _NID_AUTHORITATIVE_BASE in url:
            # Simulate a non-auth authoritative failure (wrong service path 404).
            raise USACEDAMSUpstreamError("USACE NID returned HTTP 404 (service not found)")
        # Mirror returns a short page.
        return {"type": "FeatureCollection", "features": []}

    with patch(
        "grace2_agent.tools.fetch_usace_dams._fetch_nid_geojson_page",
        side_effect=page,
    ):
        _fetch_nid_bytes(None, where="1=1", token="some-token")

    assert any(_NID_AUTHORITATIVE_BASE in u for u in calls), "authoritative attempted"
    assert any(_NID_BASE in u for u in calls), "mirror used as fallback"


def test_fetch_bytes_token_auth_rejection_does_not_degrade():
    """A REJECTED token surfaces the credential error (NO silent mirror mask)."""
    calls: list[str] = []

    def page(url, params):
        calls.append(url)
        if _NID_AUTHORITATIVE_BASE in url:
            raise USACEDAMSAuthError("ESRI code 498 Invalid Token")
        return {"type": "FeatureCollection", "features": []}

    with patch(
        "grace2_agent.tools.fetch_usace_dams._fetch_nid_geojson_page",
        side_effect=page,
    ):
        with pytest.raises(USACEDAMSAuthError):
            _fetch_nid_bytes(None, where="1=1", token="bad-token")

    # The mirror must NOT have been queried — a bad token is a credential signal.
    assert all(_NID_BASE not in u or _NID_AUTHORITATIVE_BASE in u for u in calls)
    assert not any(u == _NID_BASE for u in calls)


# ---------------------------------------------------------------------------
# Honest-empty path: an empty filtered result serializes a valid (empty) FGB.
# ---------------------------------------------------------------------------


def test_empty_filtered_result_serializes_valid_empty_fgb():
    """A filter matching nothing yields a header-only FGB, not an error."""
    import geopandas as gpd  # noqa: PLC0415

    with patch(
        "grace2_agent.tools.fetch_usace_dams._fetch_nid_geojson_page",
        side_effect=lambda url, params: {"type": "FeatureCollection", "features": []},
    ):
        fgb = _fetch_nid_bytes(
            None,
            where="HAZARD_POTENTIAL IN ('High') AND STATE IN ('Nowhere')",
            token=None,
        )
    assert isinstance(fgb, bytes) and len(fgb) > 0  # valid FGB header
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(fgb)
        path = f.name
    try:
        gdf = gpd.read_file(path)
        assert len(gdf) == 0
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Tool-level: filters reach the cache key + layer id (mocked read_through).
# ---------------------------------------------------------------------------


def test_filters_distinguish_layer_id_and_cache_key():
    """Different hazard/state filters produce distinct layer ids + cache keys."""
    fake_gcs = FakeStorageClient()
    with patch(
        "grace2_agent.tools.fetch_usace_dams._fetch_nid_bytes",
        side_effect=lambda *a, **k: _geojson_to_fgb(_sample_nid_geojson(2)),
    ), patch(
        "grace2_agent.tools.fetch_usace_dams.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        l_all = fetch_usace_dams(bbox=(-115.4, 35.8, -114.3, 36.5))
        l_high = fetch_usace_dams(
            bbox=(-115.4, 35.8, -114.3, 36.5), hazard_potential="High"
        )
        l_high_nv = fetch_usace_dams(
            bbox=(-115.4, 35.8, -114.3, 36.5),
            hazard_potential="High",
            state="Nevada",
        )

    ids = {l_all.layer_id, l_high.layer_id, l_high_nv.layer_id}
    assert len(ids) == 3, f"filtered layers must differ: {ids}"
    assert "high" in l_high.layer_id
    assert "high" in l_high_nv.layer_id and "nevada" in l_high_nv.layer_id
    uris = {l_all.uri, l_high.uri, l_high_nv.uri}
    assert len(uris) == 3, "distinct filters must produce distinct cache keys"


@pytest.mark.skipif(
    not _LIVE,
    reason="Set GRACE2_TEST_LIVE_USACE_DAMS=1 to run live NID tests",
)
def test_live_state_and_hazard_filter_applies():
    """LIVE: hazard_potential='High' + state='Nevada' returns only matching dams."""
    where = _build_where_clause(
        _validate_hazard_potential("High"), _validate_state("Nevada")
    )
    url, params = _build_nid_url(
        (-115.4, 35.8, -114.3, 36.5),
        where=where,
        result_record_count=200,
    )
    body = _fetch_nid_geojson_page(url, params)
    feats = body.get("features", [])
    assert len(feats) >= 1
    for f in feats:
        props = f.get("properties", {})
        assert props.get("HAZARD_POTENTIAL") == "High"
        assert props.get("STATE") == "Nevada"
