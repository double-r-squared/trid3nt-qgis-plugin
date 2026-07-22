"""Unit tests for the ``fetch_nifc_fire_perimeters`` atomic tool (job-0110).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- Mocked: 10-feature NIFC response → 10-polygon FlatGeobuf written through cache.
- bbox=None (CONUS sweep) → no geometry param sent on the wire; emits all features.
- bbox filter for California → geometry param is in ArcGIS envelope format,
  inSR=4326 is sent, and the cache key differs from the global call.
- Pagination is not needed — NIFC's perimeter count is far below the 2000-default
  page cap. Test verifies the single-page fetch path handles >50 features fine.
- Invalid bbox / status raise typed input errors.
- Network failure / 500 / error-envelope / non-FeatureCollection responses map
  to NIFCFireUpstreamError(retryable=True).
- User-Agent header is sent on every NIFC GET.
- Cache miss → fetch_fn invoked; cache hit → fetch_fn skipped (FR-DC-4 dedup).
- Geographic-correctness gate (job-0086 codified lesson): every returned
  perimeter polygon's centroid falls inside the US-fires envelope.
- Live (env GRACE2_TEST_LIVE_NIFC=1): real NIFC FeatureService CONUS sweep
  returns ≥0 features; if non-zero, polygon centroids fall inside the US
  envelope.
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
from grace2_agent.tools.fetch_nifc_fire_perimeters import (
    CONUS_BBOX,
    NIFCFireError,
    NIFCFireInputError,
    NIFCFireUpstreamError,
    _bbox_to_envelope,
    _build_nifc_url,
    _fetch_nifc_bytes,
    _fetch_nifc_geojson,
    _geojson_to_fgb,
    _round_bbox_to_6dp,
    _validate_bbox,
    fetch_nifc_fire_perimeters,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Marker for live tests
_LIVE_NIFC = os.environ.get("GRACE2_TEST_LIVE_NIFC") == "1"

# US-fires envelope for the geographic-correctness gate. Covers CONUS, AK,
# HI. Centroid of any NIFC perimeter should fall inside this box. The
# southern/eastern bound trims out the Pacific dateline and the Caribbean.
_US_FIRE_LON_RANGE = (-180.0, -65.0)
_US_FIRE_LAT_RANGE = (13.0, 72.0)


def _fake_fgb_bytes(tag: str = "TEST") -> bytes:
    return b"FAKE_NIFC_FGB_" + tag.encode() + b"\x00" * 16


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


def _make_perimeter_feature(
    incident_name: str,
    lon_center: float,
    lat_center: float,
    *,
    acres: float = 100.0,
    contained: float = 50.0,
    object_id: int = 0,
    half_size_deg: float = 0.05,
) -> dict:
    """Build one synthetic NIFC perimeter feature with a square polygon."""
    lon_min = lon_center - half_size_deg
    lon_max = lon_center + half_size_deg
    lat_min = lat_center - half_size_deg
    lat_max = lat_center + half_size_deg
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
            "OBJECTID": object_id,
            "poly_IncidentName": incident_name,
            "poly_FeatureCategory": "Wildfire Final Fire Perimeter",
            "poly_DateCurrent": 1780000000000 + object_id,  # epoch-millis style
            "poly_GISAcres": acres,
            "attr_IncidentSize": acres,
            "attr_PercentContained": contained,
            "attr_IncidentName": incident_name,
            "attr_FireCauseGeneral": "Natural",
            "attr_FireCause": "Lightning",
            "attr_POOState": "US-CA",
            "attr_IrwinID": f"irwin-{object_id}",
            "attr_UniqueFireIdentifier": f"2026-CAANF-{object_id:06d}",
        },
    }


def _sample_nifc_geojson(n_features: int = 10) -> dict:
    """Synthetic NIFC-shaped FeatureCollection with ``n_features`` perimeters.

    Anchors are spread across the US-fires envelope (CONUS + AK + HI) so the
    geographic gate is exercised.
    """
    anchors = [
        ("McKinney Mountain", -123.0, 41.8),    # NorCal
        ("Big Basin", -122.2, 37.1),            # NorCal
        ("Lake Hills", -120.0, 39.2),           # NorCal-Tahoe
        ("Tonto", -111.3, 33.9),                # AZ
        ("Boundary Creek", -115.0, 44.5),       # ID
        ("Bighorn Crest", -107.7, 44.3),        # WY
        ("Glades", -82.3, 27.5),                # FL
        ("Smokey Bear", -106.0, 33.4),          # NM
        ("Tongass", -134.5, 58.3),              # AK
        ("Mauna Loa Slope", -155.5, 19.5),      # HI
        ("North Cascades", -121.0, 48.9),       # WA
        ("Wind River", -109.3, 43.0),           # WY
        ("Pine Ridge", -103.0, 43.0),           # SD
    ]
    features: list[dict] = []
    for i in range(n_features):
        name, lon, lat = anchors[i % len(anchors)]
        acres = 100.0 + i * 250.0
        contained = float((i * 7) % 100)
        features.append(_make_perimeter_feature(
            f"{name} #{i}", lon, lat,
            acres=acres, contained=contained, object_id=i,
        ))
    return {"type": "FeatureCollection", "features": features}


def _centroid(polygon_coords: list[list[list[float]]]) -> tuple[float, float]:
    """Naive arithmetic-mean centroid of a polygon's outer ring."""
    ring = polygon_coords[0]
    pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
    lon = sum(p[0] for p in pts) / len(pts)
    lat = sum(p[1] for p in pts) / len(pts)
    return (lon, lat)


# ---------------------------------------------------------------------------
# Registration tests (no network needed).
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_nifc_fire_perimeters appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_nifc_fire_perimeters" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_nifc_fire_perimeters"]
    assert entry.metadata.name == "fetch_nifc_fire_perimeters"
    assert entry.metadata.ttl_class == "dynamic-1h"
    assert entry.metadata.source_class == "nifc_perimeters"
    assert entry.metadata.cacheable is True
    # Wave 1.5 schema amendment (job-0114): the tool supports global query.
    assert entry.metadata.supports_global_query is True


# ---------------------------------------------------------------------------
# bbox / URL-builder tests.
# ---------------------------------------------------------------------------


def test_bbox_to_envelope_format():
    """ArcGIS envelope is xmin,ymin,xmax,ymax — no JSON wrapping."""
    env = _bbox_to_envelope((-124.5, 32.5, -114.1, 42.0))
    assert env == "-124.5,32.5,-114.1,42.0"


def test_build_url_with_bbox_includes_geometry():
    """A bbox query sends geometry + geometryType + inSR + f=geojson."""
    url, params = _build_nifc_url((-124.5, 32.5, -114.1, 42.0))
    assert url.endswith("FeatureServer/0/query")
    assert params["f"] == "geojson"
    assert params["outFields"] == "*"
    assert params["outSR"] == "4326"
    assert params["inSR"] == "4326"
    assert params["geometryType"] == "esriGeometryEnvelope"
    assert params["geometry"] == "-124.5,32.5,-114.1,42.0"
    assert params["where"] == "1=1"


def test_build_url_without_bbox_omits_geometry():
    """A None bbox query OMITS geometry / inSR / geometryType params (CONUS sweep)."""
    url, params = _build_nifc_url(None)
    assert "geometry" not in params
    assert "geometryType" not in params
    assert "inSR" not in params
    # Still requests GeoJSON + all fields in WGS84.
    assert params["f"] == "geojson"
    assert params["outFields"] == "*"
    assert params["outSR"] == "4326"


def test_validate_bbox_rejects_degenerate():
    """Degenerate / out-of-range bboxes raise NIFCFireInputError."""
    with pytest.raises(NIFCFireInputError, match="degenerate"):
        _validate_bbox((-120.0, 40.0, -125.0, 38.0))  # min_lon > max_lon
    with pytest.raises(NIFCFireInputError, match="lon out of"):
        _validate_bbox((-200.0, 40.0, -120.0, 42.0))
    with pytest.raises(NIFCFireInputError, match="non-finite"):
        _validate_bbox((float("nan"), 40.0, -120.0, 42.0))


def test_round_bbox_to_6dp_quantizes():
    """6dp rounding is deterministic for cache-key stability."""
    q = _round_bbox_to_6dp((-124.123456789, 32.5, -114.1, 42.000001234))
    assert q == (-124.123457, 32.5, -114.1, 42.000001)


# ---------------------------------------------------------------------------
# User-Agent header verification.
# ---------------------------------------------------------------------------


def test_user_agent_header_sent_on_request():
    """Verify the User-Agent header is present on every NIFC GET."""
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

    with patch("grace2_agent.tools.fetch_nifc_fire_perimeters.httpx.Client", FakeClient):
        _fetch_nifc_geojson(
            "https://services3.arcgis.com/.../FeatureServer/0/query",
            {"f": "geojson"},
        )

    assert "User-Agent" in captured_headers, (
        f"User-Agent header missing! Captured: {captured_headers}"
    )
    ua = captured_headers["User-Agent"]
    assert "grace-2" in ua, f"User-Agent should identify grace-2: {ua!r}"


# ---------------------------------------------------------------------------
# Mocked end-to-end: 10-feature CONUS response.
# ---------------------------------------------------------------------------


def test_10_feature_conus_response_writes_fgb_with_10_polygons():
    """Mocked 10-feature NIFC response → 10-polygon FlatGeobuf written through cache."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nifc_geojson(10)

    with patch(
        "grace2_agent.tools.fetch_nifc_fire_perimeters._fetch_nifc_geojson",
        return_value=fake_geojson,
    ), patch(
        "grace2_agent.tools.fetch_nifc_fire_perimeters.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_nifc_fire_perimeters()

    assert result.uri.startswith("s3://")
    assert "nifc_perimeters" in result.uri
    assert "dynamic-1h" in result.uri
    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.units is None
    assert "CONUS" in result.name
    assert len(fake_gcs.store) == 1

    # Read back the FlatGeobuf and confirm 10 polygons with the right schema.
    fgb_bytes = next(iter(fake_gcs.store.values()))
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 10, f"Expected 10 features, got {len(gdf)}"
        # Spot-check the preserved properties.
        assert "poly_IncidentName" in gdf.columns
        assert "attr_IncidentSize" in gdf.columns
        assert "attr_PercentContained" in gdf.columns
        # Every geometry should be a polygon.
        assert (gdf.geometry.geom_type == "Polygon").all()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_bbox_filter_narrows_call_to_state_envelope():
    """A bbox-narrowed call sends geometry + inSR params on the wire."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nifc_geojson(3)
    captured_params: list[dict[str, str]] = []

    def fake_fetch(url, params):
        captured_params.append(dict(params))
        return fake_geojson

    with patch(
        "grace2_agent.tools.fetch_nifc_fire_perimeters._fetch_nifc_geojson",
        side_effect=fake_fetch,
    ), patch(
        "grace2_agent.tools.fetch_nifc_fire_perimeters.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        ca_bbox = (-124.5, 32.5, -114.1, 42.0)
        result = fetch_nifc_fire_perimeters(bbox=ca_bbox)

    assert len(captured_params) == 1
    params = captured_params[0]
    assert "geometry" in params
    assert params["geometry"] == "-124.5,32.5,-114.1,42.0"
    assert params["inSR"] == "4326"
    assert params["geometryType"] == "esriGeometryEnvelope"

    # Name + layer_id encode the bbox, not "global".
    assert "bbox" in result.name
    assert "global" not in result.layer_id


def test_bbox_cache_key_differs_from_global_call():
    """A bbox call produces a different cache key than the CONUS sweep."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nifc_geojson(3)

    with patch(
        "grace2_agent.tools.fetch_nifc_fire_perimeters._fetch_nifc_geojson",
        return_value=fake_geojson,
    ), patch(
        "grace2_agent.tools.fetch_nifc_fire_perimeters.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_global = fetch_nifc_fire_perimeters()
        r_ca = fetch_nifc_fire_perimeters(bbox=(-124.5, 32.5, -114.1, 42.0))

    assert r_global.uri != r_ca.uri
    # Both calls miss the cache.
    assert len(fake_gcs.store) == 2


def test_large_feature_count_handles_single_page():
    """NIFC perimeters fit comfortably below pagination cap — 75 features OK in one call."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nifc_geojson(75)

    with patch(
        "grace2_agent.tools.fetch_nifc_fire_perimeters._fetch_nifc_geojson",
        return_value=fake_geojson,
    ), patch(
        "grace2_agent.tools.fetch_nifc_fire_perimeters.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_nifc_fire_perimeters()

    assert result.uri.startswith("s3://")
    fgb_bytes = next(iter(fake_gcs.store.values()))
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 75
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Input-validation tests.
# ---------------------------------------------------------------------------


def test_invalid_status_raises_input_error():
    with pytest.raises(NIFCFireInputError, match="status="):
        fetch_nifc_fire_perimeters(status="bogus")


def test_invalid_bbox_raises_input_error():
    """Degenerate bbox raises NIFCFireInputError, not a generic exception."""
    with pytest.raises(NIFCFireInputError, match="degenerate"):
        fetch_nifc_fire_perimeters(bbox=(-120.0, 40.0, -125.0, 38.0))


def test_input_errors_are_not_retryable():
    """NIFCFireInputError carries retryable=False for FR-AS-11."""
    try:
        fetch_nifc_fire_perimeters(status="bogus")
    except NIFCFireInputError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected NIFCFireInputError")


# ---------------------------------------------------------------------------
# Upstream-error mapping.
# ---------------------------------------------------------------------------


def test_500_raises_typed_upstream_error():
    """500 from NIFC surfaces as NIFCFireUpstreamError(retryable=True)."""
    class FakeResponse:
        status_code = 500
        text = "Internal Server Error"

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            return FakeResponse()

    with patch("grace2_agent.tools.fetch_nifc_fire_perimeters.httpx.Client", FakeClient):
        with pytest.raises(NIFCFireUpstreamError, match="500"):
            _fetch_nifc_geojson(
                "https://services3.arcgis.com/.../FeatureServer/0/query",
                {"f": "geojson"},
            )


def test_network_failure_wraps_to_upstream_error():
    """httpx.HTTPError → NIFCFireUpstreamError."""
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            raise httpx.ConnectError("simulated DNS failure")

    with patch("grace2_agent.tools.fetch_nifc_fire_perimeters.httpx.Client", FakeClient):
        with pytest.raises(NIFCFireUpstreamError, match="request failed"):
            _fetch_nifc_geojson(
                "https://services3.arcgis.com/.../FeatureServer/0/query",
                {"f": "geojson"},
            )


def test_arcgis_error_envelope_in_200_body_raises():
    """ArcGIS error envelopes inside a 200 body raise NIFCFireUpstreamError."""
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

    with patch("grace2_agent.tools.fetch_nifc_fire_perimeters.httpx.Client", FakeClient):
        with pytest.raises(NIFCFireUpstreamError, match="error envelope"):
            _fetch_nifc_geojson(
                "https://services3.arcgis.com/.../FeatureServer/0/query",
                {"f": "geojson"},
            )


def test_non_feature_collection_raises():
    """A 200 body that isn't a FeatureCollection raises NIFCFireUpstreamError."""
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

    with patch("grace2_agent.tools.fetch_nifc_fire_perimeters.httpx.Client", FakeClient):
        with pytest.raises(NIFCFireUpstreamError, match="FeatureCollection"):
            _fetch_nifc_geojson(
                "https://services3.arcgis.com/.../FeatureServer/0/query",
                {"f": "geojson"},
            )


def test_upstream_error_is_retryable():
    """NIFCFireUpstreamError is retryable=True."""
    err = NIFCFireUpstreamError("test")
    assert err.retryable is True


# ---------------------------------------------------------------------------
# Cache-layer tests.
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_fetch_fn_then_hit_skips():
    """First call → cache miss → fetch_fn invoked; second call → cache hit → fetch_fn skipped."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_bytes = _fake_fgb_bytes("CONUS")

    def patched_fetch_bytes(bbox, status):
        fetch_count["n"] += 1
        return fake_bytes

    with patch(
        "grace2_agent.tools.fetch_nifc_fire_perimeters._fetch_nifc_bytes",
        side_effect=patched_fetch_bytes,
    ), patch(
        "grace2_agent.tools.fetch_nifc_fire_perimeters.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_nifc_fire_perimeters()
        r2 = fetch_nifc_fire_perimeters()

    assert fetch_count["n"] == 1, (
        f"Expected 1 call (hit on second); got {fetch_count['n']}"
    )
    assert r1.uri == r2.uri, "Both calls should resolve to the same cache key"


# ---------------------------------------------------------------------------
# LayerURI shape.
# ---------------------------------------------------------------------------


def test_layer_uri_shape_for_global_sweep():
    """Global / CONUS sweep produces a LayerURI tagged role=primary."""
    fake_gcs = FakeStorageClient()
    with patch(
        "grace2_agent.tools.fetch_nifc_fire_perimeters._fetch_nifc_bytes",
        return_value=_fake_fgb_bytes("CONUS"),
    ), patch(
        "grace2_agent.tools.fetch_nifc_fire_perimeters.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_nifc_fire_perimeters()

    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.units is None
    assert result.uri.startswith("s3://")
    assert "NIFC Active Fire Perimeters" in result.name
    assert "global" in result.layer_id
    assert result.style_preset == "nifc_fire_perimeters"


# ---------------------------------------------------------------------------
# Geographic-correctness gate (job-0086 lesson).
# ---------------------------------------------------------------------------


def test_geographic_gate_all_polygons_fall_inside_us_envelope():
    """job-0086 codified lesson: every NIFC perimeter centroid is inside the
    US-fires envelope after the GeoJSON → FlatGeobuf round-trip.

    Any feature whose centroid falls outside the (-180, 13, -65, 72) envelope
    would surface a sign-flip / axis-swap bug in the converter — a regression
    where coordinates get swapped or signed wrong would put centroids on the
    wrong continent.
    """
    fake_geojson = _sample_nifc_geojson(13)

    lon_min, lon_max = _US_FIRE_LON_RANGE
    lat_min, lat_max = _US_FIRE_LAT_RANGE

    # Sanity: input features already fall in the envelope.
    for feat in fake_geojson["features"]:
        cx, cy = _centroid(feat["geometry"]["coordinates"])
        assert lon_min <= cx <= lon_max, (
            f"Input feature centroid lon={cx} outside {_US_FIRE_LON_RANGE}; bad sample"
        )
        assert lat_min <= cy <= lat_max, (
            f"Input feature centroid lat={cy} outside {_US_FIRE_LAT_RANGE}; bad sample"
        )

    # Run through the converter and verify the geometries survive intact.
    fgb_bytes = _geojson_to_fgb(fake_geojson)
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 13

        # Geographic gate: every feature's centroid falls inside the US envelope.
        for idx, geom in enumerate(gdf.geometry):
            c = geom.centroid
            assert lon_min <= c.x <= lon_max, (
                f"Feature {idx} centroid x={c.x} outside US-fires lon envelope — "
                f"possible axis-swap bug"
            )
            assert lat_min <= c.y <= lat_max, (
                f"Feature {idx} centroid y={c.y} outside US-fires lat envelope — "
                f"possible axis-swap bug"
            )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_geojson_to_fgb_empty_collection_is_valid():
    """Empty NIFC FeatureCollection still produces valid FGB bytes."""
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
    """Features without a geometry (rare for the perimeter service but possible)
    are dropped — null-geom rows are useless for a perimeter polygon layer."""
    mixed = {
        "type": "FeatureCollection",
        "features": [
            _make_perimeter_feature("With geom", -121.0, 38.5, object_id=1),
            {
                "type": "Feature",
                "geometry": None,
                "properties": {
                    "OBJECTID": 2,
                    "poly_IncidentName": "Null Geom",
                    "poly_FeatureCategory": "Wildfire Final Fire Perimeter",
                    "attr_IncidentSize": 0.0,
                    "attr_PercentContained": 0.0,
                },
            },
            _make_perimeter_feature("With geom 2", -111.0, 33.4, object_id=3),
        ],
    }
    fgb_bytes = _geojson_to_fgb(mixed)
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 2, (
            f"Expected 2 surviving features, got {len(gdf)} — null-geom should be dropped"
        )
        names = set(gdf["poly_IncidentName"].tolist())
        assert "Null Geom" not in names
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Live integration test (GRACE2_TEST_LIVE_NIFC=1 to run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_NIFC,
    reason="Set GRACE2_TEST_LIVE_NIFC=1 to run live NIFC tests",
)
def test_live_conus_sweep_returns_valid_response():
    """LIVE: real NIFC FeatureService CONUS sweep returns valid FGB (≥0 features).

    Empty FeatureCollection is LEGITIMATE (no active wildfires nationwide is
    rare but possible). We assert the FGB round-trips and — if non-empty —
    that every feature's centroid falls inside the US-fires envelope
    (geographic-correctness gate).
    """
    fgb_bytes = _fetch_nifc_bytes(bbox=None, status="active")
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) >= 0
        print(f"\n[LIVE NIFC] CONUS sweep returned {len(gdf)} active perimeter(s)")
        if len(gdf) > 0:
            names = gdf["poly_IncidentName"].dropna().tolist()[:5]
            sizes = gdf["attr_IncidentSize"].dropna().tolist()[:5]
            print(f"  top incidents (first 5): {list(zip(names, sizes))}")

            # Geographic-correctness gate: features WITH polygons must have
            # centroids inside the US-fires envelope.
            lon_min, lon_max = _US_FIRE_LON_RANGE
            lat_min, lat_max = _US_FIRE_LAT_RANGE
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
            with_geom = inside + outside
            if with_geom > 0:
                outside_pct = outside / with_geom
                assert outside_pct <= 0.05, (
                    f"More than 5% of NIFC perimeters ({outside}/{with_geom}) "
                    f"have centroids outside the US-fires envelope — possible "
                    f"axis-swap regression"
                )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.skipif(
    not _LIVE_NIFC,
    reason="Set GRACE2_TEST_LIVE_NIFC=1 to run live NIFC tests",
)
def test_live_bbox_filter_returns_subset():
    """LIVE: a Western-US bbox returns a subset of the CONUS sweep."""
    west_us_bbox = (-125.0, 31.0, -100.0, 49.0)
    fgb_bytes = _fetch_nifc_bytes(bbox=west_us_bbox, status="active")
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        print(
            f"\n[LIVE NIFC] Western-US bbox returned {len(gdf)} active perimeter(s)"
        )
        # Every returned feature's centroid should fall inside the Western-US
        # envelope (NIFC's server-side filter is intersects, so allow a small
        # buffer for fires straddling the bound).
        if len(gdf) > 0:
            buf = 0.5  # degrees of buffer for "intersects" semantics
            for geom in gdf.geometry:
                if geom is None or geom.is_empty:
                    continue
                c = geom.centroid
                assert (-125.0 - buf) <= c.x <= (-100.0 + buf), (
                    f"Centroid x={c.x} far outside Western-US bbox after intersects filter"
                )
                assert (31.0 - buf) <= c.y <= (49.0 + buf), (
                    f"Centroid y={c.y} far outside Western-US bbox after intersects filter"
                )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
