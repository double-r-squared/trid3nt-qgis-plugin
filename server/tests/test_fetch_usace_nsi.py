"""Unit tests for the ``fetch_usace_nsi`` atomic tool (job A6).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- bbox validation: rejects degenerate / out-of-range / oversized envelopes.
- POST body: built as a single-polygon FeatureCollection wrapping the bbox.
- Mocked: 5-feature NSI response → 5-point FlatGeobuf written through cache.
- Output FlatGeobuf includes Pelicun-consumer columns (``component_type`` =
  ``occtype``, ``replacement_value`` = ``val_struct``) so the downstream
  ``run_pelicun_damage_assessment`` branch fires unchanged.
- Network failure / 500 / non-FeatureCollection responses map to
  USACE_NSIUpstreamError(retryable=True).
- User-Agent header is sent on every NSI POST.
- Cache miss → fetch_fn invoked; cache hit → fetch_fn skipped (FR-DC-4 dedup).
- estimate_payload_mb hook returns a finite positive number proportional to
  the bbox area.
- Live (env ``GRACE2_TEST_LIVE_NSI=1``): real NSI POST against a tiny Fort
  Myers Beach bbox returns ≥1 NSI structure.
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
from grace2_agent.tools.fetch_usace_nsi import (
    NSI_BBOX_MAX_SPAN_DEG,
    USACE_NSIError,
    USACE_NSIInputError,
    USACE_NSIUpstreamError,
    _bbox_to_polygon_feature,
    _build_nsi_polygon_body,
    _fetch_nsi_bytes,
    _fetch_nsi_geojson,
    _geojson_to_fgb,
    _round_bbox_to_6dp,
    _validate_bbox,
    estimate_payload_mb,
    fetch_usace_nsi,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)

# Marker for live tests
_LIVE_NSI = os.environ.get("GRACE2_TEST_LIVE_NSI") == "1"

# Fort Myers Beach — verified live during the smoke test.
_FORT_MYERS_BBOX = (-81.870, 26.640, -81.860, 26.650)


def _fake_fgb_bytes(tag: str = "TEST") -> bytes:
    return b"FAKE_NSI_FGB_" + tag.encode() + b"\x00" * 16


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


def _make_nsi_feature(
    fd_id: int,
    occtype: str,
    val_struct: float,
    lon: float,
    lat: float,
) -> dict:
    """Build one synthetic NSI structure feature (Point geometry)."""
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [lon, lat],
        },
        "properties": {
            "fd_id": fd_id,
            "bid": f"bid-{fd_id}",
            "occtype": occtype,
            "st_damcat": "RES" if occtype.startswith("RES") else "COM",
            "bldgtype": "W",
            "found_type": "S",
            "found_ht": 1.5,
            "num_story": 1,
            "sqft": 1800.0,
            "med_yr_blt": 1985,
            "val_struct": val_struct,
            "val_cont": val_struct * 0.5,
            "val_vehic": 27000.0,
            "firmzone": "AE",
            "cbfips": "120710803001029",
            "ground_elv": 9.5,
            "ground_elv_m": 2.9,
            "pop2amu65": 2,
            "pop2amo65": 1,
            "pop2pmu65": 1,
            "pop2pmo65": 0,
            "students": 0,
            "source": "E",
        },
    }


def _sample_nsi_geojson(n_features: int = 5) -> dict:
    """Synthetic NSI FeatureCollection with ``n_features`` structures."""
    occ_cycle = ["RES1", "RES3A", "COM1", "COM3", "EDU1"]
    val_cycle = [250_000.0, 350_000.0, 1_400_000.0, 1_200_000.0, 5_500_000.0]
    features: list[dict] = []
    for i in range(n_features):
        # Step the points around inside the Fort Myers bbox.
        lon = -81.865 + (i % 5) * 0.001
        lat = 26.645 + (i // 5) * 0.001
        features.append(_make_nsi_feature(
            fd_id=497013000 + i,
            occtype=occ_cycle[i % len(occ_cycle)],
            val_struct=val_cycle[i % len(val_cycle)],
            lon=lon,
            lat=lat,
        ))
    return {"type": "FeatureCollection", "features": features}


# ---------------------------------------------------------------------------
# Registration tests (no network needed).
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_usace_nsi appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_usace_nsi" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_usace_nsi"]
    assert entry.metadata.name == "fetch_usace_nsi"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "usace_nsi"
    assert entry.metadata.cacheable is True
    # Wave 1.5 schema amendment (job-0114): NSI is bbox-only.
    assert entry.metadata.supports_global_query is False
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


# ---------------------------------------------------------------------------
# bbox / body-builder tests.
# ---------------------------------------------------------------------------


def test_bbox_to_polygon_feature_format():
    """Polygon feature is a closed-ring CCW rectangle in EPSG:4326."""
    feat = _bbox_to_polygon_feature((-81.880, 26.620, -81.860, 26.640))
    assert feat["type"] == "Feature"
    assert feat["geometry"]["type"] == "Polygon"
    ring = feat["geometry"]["coordinates"][0]
    # Closed ring (first == last) of 5 points.
    assert len(ring) == 5
    assert ring[0] == ring[-1]
    # All four corners are present.
    assert (-81.880, 26.620) == tuple(ring[0])
    assert (-81.860, 26.620) == tuple(ring[1])
    assert (-81.860, 26.640) == tuple(ring[2])
    assert (-81.880, 26.640) == tuple(ring[3])


def test_build_nsi_polygon_body_is_feature_collection():
    """NSI POST body is a single-feature FeatureCollection."""
    body = _build_nsi_polygon_body((-81.880, 26.620, -81.860, 26.640))
    assert body["type"] == "FeatureCollection"
    assert len(body["features"]) == 1
    assert body["features"][0]["geometry"]["type"] == "Polygon"


def test_validate_bbox_rejects_degenerate():
    """Degenerate / out-of-range bboxes raise USACE_NSIInputError."""
    with pytest.raises(USACE_NSIInputError, match="degenerate"):
        _validate_bbox((-81.860, 26.640, -81.870, 26.630))  # min > max
    with pytest.raises(USACE_NSIInputError, match="lon out of"):
        _validate_bbox((-200.0, 26.620, -81.860, 26.640))
    with pytest.raises(USACE_NSIInputError, match="non-finite"):
        _validate_bbox((float("nan"), 26.620, -81.860, 26.640))


def test_validate_bbox_rejects_oversized():
    """bbox spans larger than NSI_BBOX_MAX_SPAN_DEG raise input error."""
    big = (-82.5, 26.0, -82.5 + NSI_BBOX_MAX_SPAN_DEG + 0.1, 27.0)
    with pytest.raises(USACE_NSIInputError, match="span exceeds"):
        _validate_bbox(big)


def test_round_bbox_to_6dp_quantizes():
    """6dp rounding is deterministic for cache-key stability."""
    q = _round_bbox_to_6dp((-81.8700001, 26.6400001, -81.8600001, 26.6500001))
    assert q == (-81.87, 26.64, -81.86, 26.65)


# ---------------------------------------------------------------------------
# User-Agent header verification.
# ---------------------------------------------------------------------------


def test_user_agent_header_sent_on_request():
    """Verify the User-Agent header is present on every NSI POST."""
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

        def post(self, url, params=None, json=None, headers=None):
            captured_headers.update(headers or {})
            return FakeResponse()

    with patch("grace2_agent.tools.fetch_usace_nsi.httpx.Client", FakeClient):
        _fetch_nsi_geojson(
            "https://nsi.sec.usace.army.mil/nsiapi/structures",
            {"type": "FeatureCollection", "features": []},
        )

    assert "User-Agent" in captured_headers, (
        f"User-Agent header missing! Captured: {captured_headers}"
    )
    ua = captured_headers["User-Agent"]
    assert "trid3nt" in ua, f"User-Agent should identify trid3nt: {ua!r}"


# ---------------------------------------------------------------------------
# Mocked end-to-end: 5-feature response.
# ---------------------------------------------------------------------------


def test_5_feature_response_writes_fgb_with_pelicun_columns():
    """Mocked 5-feature NSI response → 5-point FlatGeobuf with Pelicun columns."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nsi_geojson(5)

    with patch(
        "grace2_agent.tools.fetch_usace_nsi._fetch_nsi_geojson",
        return_value=fake_geojson,
    ), patch(
        "grace2_agent.tools.fetch_usace_nsi.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_usace_nsi(bbox=_FORT_MYERS_BBOX)

    assert result.uri.startswith("s3://")
    assert "usace_nsi" in result.uri
    assert "static-30d" in result.uri
    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.units is None
    assert "USACE NSI" in result.name
    assert len(fake_gcs.store) == 1

    # Read back the FlatGeobuf and confirm 5 points with the Pelicun
    # convenience columns present.
    fgb_bytes = next(iter(fake_gcs.store.values()))
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 5, f"Expected 5 features, got {len(gdf)}"
        # NSI-native columns preserved.
        assert "occtype" in gdf.columns
        assert "val_struct" in gdf.columns
        # Pelicun-consumer convenience columns added.
        assert "component_type" in gdf.columns, (
            "component_type column is required for Pelicun consumer"
        )
        assert "replacement_value" in gdf.columns, (
            "replacement_value column is required for Pelicun consumer"
        )
        # Every geometry should be a Point.
        assert (gdf.geometry.geom_type == "Point").all()
        # component_type mirrors occtype, replacement_value mirrors val_struct.
        for _, row in gdf.iterrows():
            assert row["component_type"] == row["occtype"]
            assert float(row["replacement_value"]) == float(row["val_struct"])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Input-validation tests.
# ---------------------------------------------------------------------------


def test_missing_bbox_raises_input_error():
    """A None bbox raises USACE_NSIInputError (NSI is bbox-only)."""
    with pytest.raises(USACE_NSIInputError, match="bbox is required"):
        fetch_usace_nsi(bbox=None)


def test_invalid_bbox_raises_input_error():
    """Degenerate bbox raises USACE_NSIInputError, not a generic exception."""
    with pytest.raises(USACE_NSIInputError, match="degenerate"):
        fetch_usace_nsi(bbox=(-81.860, 26.640, -81.870, 26.630))


def test_input_errors_are_not_retryable():
    """USACE_NSIInputError carries retryable=False for FR-AS-11."""
    try:
        fetch_usace_nsi(bbox=None)
    except USACE_NSIInputError as exc:
        assert exc.retryable is False
        assert exc.error_code == "USACE_NSI_INPUT_INVALID"
    else:
        pytest.fail("Expected USACE_NSIInputError")


# ---------------------------------------------------------------------------
# Upstream-error mapping.
# ---------------------------------------------------------------------------


def test_500_raises_typed_upstream_error():
    """500 from NSI surfaces as USACE_NSIUpstreamError(retryable=True)."""
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

        def post(self, url, params=None, json=None, headers=None):
            return FakeResponse()

    with patch("grace2_agent.tools.fetch_usace_nsi.httpx.Client", FakeClient):
        with pytest.raises(USACE_NSIUpstreamError, match="500"):
            _fetch_nsi_geojson(
                "https://nsi.sec.usace.army.mil/nsiapi/structures",
                {"type": "FeatureCollection", "features": []},
            )


def test_network_failure_wraps_to_upstream_error():
    """httpx.HTTPError → USACE_NSIUpstreamError."""
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, params=None, json=None, headers=None):
            raise httpx.ConnectError("simulated DNS failure")

    with patch("grace2_agent.tools.fetch_usace_nsi.httpx.Client", FakeClient):
        with pytest.raises(USACE_NSIUpstreamError, match="request failed"):
            _fetch_nsi_geojson(
                "https://nsi.sec.usace.army.mil/nsiapi/structures",
                {"type": "FeatureCollection", "features": []},
            )


def test_non_feature_collection_raises():
    """A 200 body that isn't a FeatureCollection raises USACE_NSIUpstreamError."""
    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"message": "Internal Server Error"}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, params=None, json=None, headers=None):
            return FakeResponse()

    with patch("grace2_agent.tools.fetch_usace_nsi.httpx.Client", FakeClient):
        with pytest.raises(USACE_NSIUpstreamError, match="error message"):
            _fetch_nsi_geojson(
                "https://nsi.sec.usace.army.mil/nsiapi/structures",
                {"type": "FeatureCollection", "features": []},
            )


def test_upstream_error_is_retryable():
    """USACE_NSIUpstreamError is retryable=True."""
    err = USACE_NSIUpstreamError("test")
    assert err.retryable is True
    assert err.error_code == "USACE_NSI_UPSTREAM_ERROR"


# ---------------------------------------------------------------------------
# Cache-layer tests.
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_fetch_fn_then_hit_skips():
    """First call → cache miss → fetch_fn invoked; second call → cache hit → fetch_fn skipped."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}
    fake_bytes = _fake_fgb_bytes("FTMYERS")

    def patched_fetch_bytes(bbox):
        fetch_count["n"] += 1
        return fake_bytes

    with patch(
        "grace2_agent.tools.fetch_usace_nsi._fetch_nsi_bytes",
        side_effect=patched_fetch_bytes,
    ), patch(
        "grace2_agent.tools.fetch_usace_nsi.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_usace_nsi(bbox=_FORT_MYERS_BBOX)
        r2 = fetch_usace_nsi(bbox=_FORT_MYERS_BBOX)

    assert fetch_count["n"] == 1, (
        f"Expected 1 call (hit on second); got {fetch_count['n']}"
    )
    assert r1.uri == r2.uri, "Both calls should resolve to the same cache key"


# ---------------------------------------------------------------------------
# Payload estimate shape.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_returns_finite_positive():
    """The payload estimator returns a finite positive number."""
    mb = estimate_payload_mb(bbox=_FORT_MYERS_BBOX)
    assert isinstance(mb, float)
    assert mb >= 0.0
    # Tiny Fort Myers Beach bbox should land well under the chat warning gate.
    assert mb < 25.0, f"Tiny bbox should be < 25 MB; got {mb:.2f}"


def test_estimate_payload_mb_scales_with_area():
    """Doubling each side roughly quadruples the estimate."""
    small = estimate_payload_mb(bbox=(-81.880, 26.620, -81.860, 26.640))  # 0.02 x 0.02
    big = estimate_payload_mb(bbox=(-81.880, 26.620, -81.840, 26.660))    # 0.04 x 0.04
    assert big > small
    # Allow a bit of slack for rounding.
    assert big / max(small, 1e-9) >= 3.0


def test_estimate_payload_mb_no_bbox_returns_conservative_default():
    """Without a bbox the estimator returns a conservative default."""
    mb = estimate_payload_mb()
    assert isinstance(mb, float)
    assert mb > 0.0


# ---------------------------------------------------------------------------
# LayerURI shape.
# ---------------------------------------------------------------------------


def test_layer_uri_shape():
    """LayerURI is tagged role=primary, layer_type=vector, with NSI style."""
    fake_gcs = FakeStorageClient()
    with patch(
        "grace2_agent.tools.fetch_usace_nsi._fetch_nsi_bytes",
        return_value=_fake_fgb_bytes("FTMYERS"),
    ), patch(
        "grace2_agent.tools.fetch_usace_nsi.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_usace_nsi(bbox=_FORT_MYERS_BBOX)

    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.units is None
    assert result.uri.startswith("s3://")
    assert "USACE NSI" in result.name
    assert result.style_preset == "usace_nsi"
    assert result.layer_id.startswith("usace-nsi-")


# ---------------------------------------------------------------------------
# Extra-kwarg absorption (job-0164).
# ---------------------------------------------------------------------------


def test_extra_kwargs_are_absorbed():
    """LLM-invented kwargs must not raise; **_extra_ignored absorbs them."""
    fake_gcs = FakeStorageClient()
    with patch(
        "grace2_agent.tools.fetch_usace_nsi._fetch_nsi_bytes",
        return_value=_fake_fgb_bytes("FTMYERS"),
    ), patch(
        "grace2_agent.tools.fetch_usace_nsi.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        # Should NOT raise TypeError on the invented kwarg.
        result = fetch_usace_nsi(
            bbox=_FORT_MYERS_BBOX,
            # Made-up params the LLM might invent.
            include_population=True,
            year=2020,
            min_value=100000,
        )
    assert result.uri.startswith("s3://")


# ---------------------------------------------------------------------------
# Live smoke (gated on GRACE2_TEST_LIVE_NSI=1).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LIVE_NSI, reason="GRACE2_TEST_LIVE_NSI not set")
def test_live_fetch_returns_at_least_one_structure():
    """Real NSI POST against the Fort Myers Beach bbox returns ≥1 structure."""
    body = _build_nsi_polygon_body(_FORT_MYERS_BBOX)
    geojson = _fetch_nsi_geojson(
        "https://nsi.sec.usace.army.mil/nsiapi/structures",
        body,
    )
    assert geojson["type"] == "FeatureCollection"
    features = geojson.get("features", [])
    assert len(features) >= 1, (
        f"Expected ≥1 NSI structure in Fort Myers Beach; got {len(features)}"
    )
    # First feature should carry the expected NSI properties.
    p = features[0].get("properties") or {}
    for required in ("fd_id", "occtype", "val_struct"):
        assert required in p, f"Missing required NSI prop: {required}"
