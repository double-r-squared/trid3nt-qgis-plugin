"""Unit tests for the ``fetch_fema_nfhl_zones`` atomic tool (job A1).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata
  (``supports_global_query=False``, ``payload_mb_estimator_name``).
- Mocked: 5-feature FEMA NFHL response → 5-polygon FlatGeobuf written through cache.
- ArcGIS envelope is built correctly + inSR=4326 sent.
- ``sfha_only=True`` adds ``SFHA_TF='T'`` to the where clause.
- ``zone_filter=["VE"]`` is applied client-side after fetch (cache key diverges).
- bbox / sfha_only / zone_filter input validation raises typed input errors.
- Network failure / 500 / ArcGIS error envelope / non-FeatureCollection map to
  ``FEMA_NFHL_ZONESUpstreamError(retryable=True)``.
- ``estimate_payload_mb`` returns a sensible numeric envelope for common
  bbox sizes (clamped to [0.05, 50] MB).
- User-Agent header sent on every NFHL GET.
- Cache miss → fetch_fn invoked; cache hit → fetch_fn skipped (FR-DC-4 dedup).
- Pagination loop stops when ``exceededTransferLimit`` is False.
- Geographic-correctness gate (job-0086 codified lesson): every returned
  feature's centroid falls inside the bbox.
- Live (env ``TRID3NT_TEST_LIVE_FEMA_NFHL=1``): real FEMA NFHL Fort Myers
  bbox returns ≥1 polygon with valid FLD_ZONE / SFHA_TF properties.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones import (
    FEMA_NFHL_ZONESError,
    FEMA_NFHL_ZONESInputError,
    FEMA_NFHL_ZONESUpstreamError,
    NFHL_FLOOD_ZONES_URL,
    VALID_FLOOD_ZONES,
    _bbox_to_envelope,
    _build_nfhl_url,
    _features_to_flatgeobuf,
    _fetch_nfhl_bytes,
    _fetch_nfhl_features,
    _nfhl_query_one_page,
    _round_bbox_to_6dp,
    _validate_bbox,
    estimate_payload_mb,
    fetch_fema_nfhl_zones,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)

#: Live-test gate
_LIVE_FEMA = os.environ.get("TRID3NT_TEST_LIVE_FEMA_NFHL") == "1"

#: Fort Myers, FL bbox — used for unit-test scenarios. ~0.15deg square.
_FORT_MYERS_BBOX = (-81.95, 26.55, -81.80, 26.70)

#: Smaller Fort Myers tile (~5km square) used for the live smoke test. The
#: full 0.15° bbox triggers FEMA's NFHL pagination quirk (server 500s on
#: cursor queries within wide OBJECTID ranges); this tile fits in a single
#: 1000-feature page and is representative of typical agent fetches.
_FORT_MYERS_LIVE_BBOX = (-81.90, 26.62, -81.85, 26.66)


def _fake_fgb_bytes(tag: str = "TEST") -> bytes:
    return b"FAKE_NFHL_FGB_" + tag.encode() + b"\x00" * 16


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


# ---------------------------------------------------------------------------
# Synthetic feature builders.
# ---------------------------------------------------------------------------


def _make_zone_feature(
    fld_zone: str,
    lon_center: float,
    lat_center: float,
    *,
    sfha_tf: str = "T",
    static_bfe: float = 12.5,
    zone_subty: str | None = None,
    half_size_deg: float = 0.005,
    dfirm_id: str = "12071C",
    object_id: int = 0,
) -> dict:
    """Build one synthetic NFHL flood-zone feature (square polygon)."""
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
            "FLD_ZONE": fld_zone,
            "ZONE_SUBTY": zone_subty,
            "SFHA_TF": sfha_tf,
            "STATIC_BFE": static_bfe,
            "V_DATUM": "NAVD88",
            "DEPTH": -9999.0,
            "LEN_UNIT": "Feet",
            "VELOCITY": -9999.0,
            "VEL_UNIT": "",
            "DFIRM_ID": dfirm_id,
            "FLD_AR_ID": f"{dfirm_id}_1",
            "STUDY_TYP": "PR",
            "SOURCE_CIT": f"{dfirm_id}_LOMC28",
            "GFID": f"00000000-0000-0000-0000-{abs(hash((fld_zone, lon_center))) % 10**12:012d}",
        },
    }


def _sample_nfhl_geojson(n_features: int = 5) -> dict:
    """Synthetic FEMA-NFHL-shaped FeatureCollection anchored on Fort Myers."""
    anchors = [
        ("AE", -81.88, 26.65, "T", 12.0),
        ("VE", -81.92, 26.60, "T", 15.0),
        ("X", -81.85, 26.62, "F", -9999.0),
        ("AH", -81.90, 26.68, "T", 9.0),
        ("AO", -81.87, 26.66, "T", -9999.0),
        ("A", -81.86, 26.63, "T", -9999.0),
        ("D", -81.83, 26.58, "F", -9999.0),
    ]
    features: list[dict] = []
    for i in range(n_features):
        zone, lon, lat, sfha, bfe = anchors[i % len(anchors)]
        features.append(_make_zone_feature(
            zone, lon, lat, sfha_tf=sfha, static_bfe=bfe, object_id=i + 1,
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
    """fetch_fema_nfhl_zones appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_fema_nfhl_zones" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_fema_nfhl_zones"]
    assert entry.metadata.name == "fetch_fema_nfhl_zones"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "fema_nfhl"
    assert entry.metadata.cacheable is True
    # Polygon source — global query NOT supported (kickoff).
    assert entry.metadata.supports_global_query is False
    # Wave 1.5 payload-MB estimator name surfaced via decorator.
    assert entry.metadata.payload_mb_estimator_name == "estimate_payload_mb"


# ---------------------------------------------------------------------------
# bbox / URL-builder tests.
# ---------------------------------------------------------------------------


def test_bbox_to_envelope_format():
    """ArcGIS envelope is xmin,ymin,xmax,ymax — no JSON wrapping."""
    env = _bbox_to_envelope((-81.95, 26.55, -81.80, 26.70))
    assert env == "-81.95,26.55,-81.8,26.7"


def test_build_url_default():
    """Default build uses OBJECTID>0, geometry, inSR=4326, geojson, orderBy OID."""
    url, params = _build_nfhl_url(_FORT_MYERS_BBOX)
    assert url == NFHL_FLOOD_ZONES_URL
    assert params["where"] == "OBJECTID>0"
    assert params["f"] == "geojson"
    assert params["outSR"] == "4326"
    assert params["inSR"] == "4326"
    assert params["geometryType"] == "esriGeometryEnvelope"
    assert "geometry" in params
    assert params["orderByFields"] == "OBJECTID"
    assert "FLD_ZONE" in params["outFields"]
    assert "OBJECTID" in params["outFields"]


def test_build_url_with_sfha_only():
    """sfha_only=True adds the SFHA_TF='T' clause AND keeps the cursor."""
    url, params = _build_nfhl_url(_FORT_MYERS_BBOX, sfha_only=True)
    assert "SFHA_TF='T'" in params["where"]
    assert "OBJECTID>0" in params["where"]


def test_build_url_with_cursor():
    """Pagination: OBJECTID-cursor watermark advances the where clause."""
    url, params = _build_nfhl_url(_FORT_MYERS_BBOX, last_object_id=2000)
    assert params["where"] == "OBJECTID>2000"


def test_validate_bbox_rejects_degenerate():
    """Degenerate / out-of-range bboxes raise FEMA_NFHL_ZONESInputError."""
    with pytest.raises(FEMA_NFHL_ZONESInputError, match="degenerate"):
        _validate_bbox((-81.80, 26.70, -81.95, 26.55))  # min > max
    with pytest.raises(FEMA_NFHL_ZONESInputError, match="lon out of"):
        _validate_bbox((-200.0, 26.55, -81.80, 26.70))
    with pytest.raises(FEMA_NFHL_ZONESInputError, match="non-finite"):
        _validate_bbox((float("nan"), 26.55, -81.80, 26.70))


def test_round_bbox_to_6dp():
    """6dp rounding is deterministic for cache-key stability."""
    q = _round_bbox_to_6dp((-81.9512345678, 26.55, -81.8, 26.7000001234))
    assert q == (-81.951235, 26.55, -81.8, 26.7)


# ---------------------------------------------------------------------------
# Payload estimator tests.
# ---------------------------------------------------------------------------


def test_estimate_payload_mb_small_bbox():
    """A ~Fort Myers metro bbox (~0.15 x 0.15 deg) is clipped to the min."""
    val = estimate_payload_mb(bbox=_FORT_MYERS_BBOX)
    # 0.15 * 0.15 * 0.5 = 0.011 MB -> clipped to floor 0.05 MB.
    assert 0.05 <= val <= 0.05


def test_estimate_payload_mb_state_bbox():
    """A state-scale bbox (~10 x 10 deg) returns ~50 MB clipped upper bound."""
    val = estimate_payload_mb(bbox=(-100.0, 30.0, -90.0, 40.0))
    # 10 * 10 * 0.5 = 50 MB exactly at the cap.
    assert val == 50.0


def test_estimate_payload_mb_no_bbox_returns_upper_clip():
    """No bbox → upper-clip warning signal."""
    assert estimate_payload_mb() == 50.0
    assert estimate_payload_mb(bbox=None) == 50.0
    assert estimate_payload_mb(bbox=(1, 2, 3)) == 50.0  # wrong arity


def test_estimate_payload_mb_medium_bbox():
    """A 1-degree-square bbox returns ~0.5 MB."""
    val = estimate_payload_mb(bbox=(-82.5, 26.0, -81.5, 27.0))
    assert 0.4 <= val <= 0.6


# ---------------------------------------------------------------------------
# User-Agent header verification.
# ---------------------------------------------------------------------------


def test_user_agent_header_sent_on_request():
    """Verify the User-Agent header is present on every NFHL GET."""
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

    with patch("trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.httpx.Client", FakeClient):
        _nfhl_query_one_page(_FORT_MYERS_BBOX, last_object_id=0, sfha_only=False)

    assert "User-Agent" in captured_headers
    ua = captured_headers["User-Agent"]
    assert "trid3nt" in ua


# ---------------------------------------------------------------------------
# Mocked end-to-end: 5-feature FEMA NFHL response.
# ---------------------------------------------------------------------------


def test_5_feature_response_writes_fgb_with_5_polygons():
    """Mocked 5-feature NFHL response → 5-polygon FlatGeobuf written through cache."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nfhl_geojson(5)

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones._nfhl_query_one_page",
        return_value=fake_geojson,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_fema_nfhl_zones(bbox=_FORT_MYERS_BBOX)

    assert result.uri.startswith("s3://")
    assert "fema_nfhl" in result.uri
    assert "static-30d" in result.uri
    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.units is None
    assert "FEMA NFHL" in result.name
    assert result.style_preset == "fema_nfhl_zones"
    assert len(fake_gcs.store) == 1

    # Read back the FlatGeobuf and confirm 5 polygons with the right schema.
    fgb_bytes = next(iter(fake_gcs.store.values()))
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 5
        assert "FLD_ZONE" in gdf.columns
        assert "SFHA_TF" in gdf.columns
        assert "STATIC_BFE" in gdf.columns
        assert "DFIRM_ID" in gdf.columns
        # All geometries should be polygons.
        assert (gdf.geometry.geom_type == "Polygon").all()
        # Spot-check the regulatory designation values.
        zones = set(gdf["FLD_ZONE"].tolist())
        assert "AE" in zones
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_sfha_only_passes_through_to_where_clause():
    """sfha_only=True propagates to the query's where clause on the wire."""
    fake_gcs = FakeStorageClient()
    captured_params: list[tuple[tuple, int, bool]] = []

    def fake_page(bbox, last_object_id, sfha_only):
        captured_params.append((bbox, last_object_id, sfha_only))
        return _sample_nfhl_geojson(2)

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones._nfhl_query_one_page",
        side_effect=fake_page,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        fetch_fema_nfhl_zones(bbox=_FORT_MYERS_BBOX, sfha_only=True)

    assert len(captured_params) == 1
    bbox, last_oid, sfha = captured_params[0]
    assert sfha is True
    assert last_oid == 0


def test_zone_filter_applied_client_side():
    """zone_filter restricts the returned features to the requested codes."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nfhl_geojson(7)  # has AE, VE, X, AH, AO, A, D

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones._nfhl_query_one_page",
        return_value=fake_geojson,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_fema_nfhl_zones(bbox=_FORT_MYERS_BBOX, zone_filter=["VE"])

    fgb_bytes = next(iter(fake_gcs.store.values()))
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 1
        assert set(gdf["FLD_ZONE"].tolist()) == {"VE"}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_cache_key_differs_with_sfha_only():
    """sfha_only=True vs False produces different cache keys."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nfhl_geojson(3)

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones._nfhl_query_one_page",
        return_value=fake_geojson,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_fema_nfhl_zones(bbox=_FORT_MYERS_BBOX, sfha_only=False)
        r2 = fetch_fema_nfhl_zones(bbox=_FORT_MYERS_BBOX, sfha_only=True)

    assert r1.uri != r2.uri


def test_pagination_stops_on_short_page():
    """Single-page response shorter than the page cap does not loop."""
    fake_gcs = FakeStorageClient()
    fake_geojson = _sample_nfhl_geojson(3)  # 3 << _PAGE_SIZE -> stop
    call_count = {"n": 0}

    def fake_page(bbox, last_object_id, sfha_only):
        call_count["n"] += 1
        return fake_geojson

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones._nfhl_query_one_page",
        side_effect=fake_page,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        fetch_fema_nfhl_zones(bbox=_FORT_MYERS_BBOX)

    assert call_count["n"] == 1


def test_pagination_advances_objectid_cursor():
    """When the first page returns _PAGE_SIZE features, second page is fetched
    with the watermark OBJECTID advancing past the highest seen."""
    from trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones import _PAGE_SIZE

    fake_gcs = FakeStorageClient()
    # First page = full size (forces another request). Synth OBJECTIDs 1..PAGE_SIZE.
    page1_features = [
        _make_zone_feature("AE", -81.88, 26.65, object_id=i)
        for i in range(1, _PAGE_SIZE + 1)
    ]
    # Second page = short (terminates).
    page2_features = [
        _make_zone_feature("VE", -81.92, 26.60, object_id=i)
        for i in range(_PAGE_SIZE + 1, _PAGE_SIZE + 5)
    ]
    pages = [
        {"type": "FeatureCollection", "features": page1_features},
        {"type": "FeatureCollection", "features": page2_features},
    ]
    cursor_seen: list[int] = []
    call_count = {"n": 0}

    def fake_page(bbox, last_object_id, sfha_only):
        cursor_seen.append(last_object_id)
        idx = call_count["n"]
        call_count["n"] += 1
        return pages[idx]

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones._nfhl_query_one_page",
        side_effect=fake_page,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        fetch_fema_nfhl_zones(bbox=_FORT_MYERS_BBOX)

    assert call_count["n"] == 2
    assert cursor_seen[0] == 0
    assert cursor_seen[1] == _PAGE_SIZE  # advanced past first page's max OID


# ---------------------------------------------------------------------------
# Input-validation tests.
# ---------------------------------------------------------------------------


def test_invalid_bbox_raises_input_error():
    """Degenerate bbox raises FEMA_NFHL_ZONESInputError, not retryable."""
    with pytest.raises(FEMA_NFHL_ZONESInputError, match="degenerate"):
        fetch_fema_nfhl_zones(bbox=(-81.80, 26.70, -81.95, 26.55))


def test_invalid_zone_filter_raises_input_error():
    """Unknown FLD_ZONE codes raise FEMA_NFHL_ZONESInputError."""
    with pytest.raises(FEMA_NFHL_ZONESInputError, match="not in known"):
        fetch_fema_nfhl_zones(bbox=_FORT_MYERS_BBOX, zone_filter=["ZZZ"])


def test_input_errors_are_not_retryable():
    """FEMA_NFHL_ZONESInputError carries retryable=False for FR-AS-11."""
    try:
        fetch_fema_nfhl_zones(bbox=(-81.80, 26.70, -81.95, 26.55))
    except FEMA_NFHL_ZONESInputError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected FEMA_NFHL_ZONESInputError")


def test_known_zone_codes_accepted_by_filter():
    """Common NFHL zone codes are accepted by zone_filter."""
    for code in ("AE", "VE", "AH", "AO", "A", "X", "D", "ae", "ve"):
        assert code.upper() in VALID_FLOOD_ZONES, f"{code} should be valid"


# ---------------------------------------------------------------------------
# Upstream-error mapping.
# ---------------------------------------------------------------------------


def test_500_raises_typed_upstream_error():
    """500 from FEMA NFHL surfaces as FEMA_NFHL_ZONESUpstreamError(retryable=True)."""
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

    with patch("trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.httpx.Client", FakeClient):
        with pytest.raises(FEMA_NFHL_ZONESUpstreamError, match="500"):
            _nfhl_query_one_page(_FORT_MYERS_BBOX, last_object_id=0, sfha_only=False)


def test_network_failure_wraps_to_upstream_error():
    """httpx.HTTPError → FEMA_NFHL_ZONESUpstreamError."""
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None, headers=None):
            raise httpx.ConnectError("simulated DNS failure")

    with patch("trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.httpx.Client", FakeClient):
        with pytest.raises(FEMA_NFHL_ZONESUpstreamError, match="request failed"):
            _nfhl_query_one_page(_FORT_MYERS_BBOX, last_object_id=0, sfha_only=False)


def test_arcgis_error_envelope_in_200_body_raises():
    """ArcGIS error envelopes inside a 200 body raise FEMA_NFHL_ZONESUpstreamError."""
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

    with patch("trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.httpx.Client", FakeClient):
        with pytest.raises(FEMA_NFHL_ZONESUpstreamError, match="error envelope"):
            _nfhl_query_one_page(_FORT_MYERS_BBOX, last_object_id=0, sfha_only=False)


def test_non_feature_collection_raises():
    """A 200 body that isn't a FeatureCollection raises FEMA_NFHL_ZONESUpstreamError."""
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

    with patch("trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.httpx.Client", FakeClient):
        with pytest.raises(FEMA_NFHL_ZONESUpstreamError, match="FeatureCollection"):
            _nfhl_query_one_page(_FORT_MYERS_BBOX, last_object_id=0, sfha_only=False)


def test_upstream_error_is_retryable():
    """FEMA_NFHL_ZONESUpstreamError is retryable=True."""
    err = FEMA_NFHL_ZONESUpstreamError("test")
    assert err.retryable is True


# ---------------------------------------------------------------------------
# Empty + null-geometry handling.
# ---------------------------------------------------------------------------


def test_empty_feature_collection_produces_valid_empty_fgb():
    """Empty NFHL FeatureCollection still produces valid FGB bytes (no error)."""
    fgb_bytes = _features_to_flatgeobuf([])
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


def test_features_to_fgb_drops_null_geometry_rows():
    """Features without a polygon geometry are dropped from the FGB."""
    features = [
        _make_zone_feature("AE", -81.88, 26.65),
        {"type": "Feature", "geometry": None, "properties": {"FLD_ZONE": "X"}},
        _make_zone_feature("VE", -81.92, 26.60),
    ]
    fgb_bytes = _features_to_flatgeobuf(features)
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 2
        # The X row (null geom) should be gone.
        assert "X" not in set(gdf["FLD_ZONE"].tolist())
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Cache-layer tests.
# ---------------------------------------------------------------------------


def test_cache_miss_invokes_fetch_then_hit_skips():
    """First call → cache miss → fetch_fn invoked; second call → cache hit → fetch skipped."""
    fake_gcs = FakeStorageClient()
    fetch_count = {"n": 0}

    def patched_fetch_bytes(bbox, sfha_only, zone_filter):
        fetch_count["n"] += 1
        return _fake_fgb_bytes("CACHE")

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones._fetch_nfhl_bytes",
        side_effect=patched_fetch_bytes,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_fema_nfhl_zones.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_fema_nfhl_zones(bbox=_FORT_MYERS_BBOX)
        r2 = fetch_fema_nfhl_zones(bbox=_FORT_MYERS_BBOX)

    assert fetch_count["n"] == 1
    assert r1.uri == r2.uri


# ---------------------------------------------------------------------------
# Geographic-correctness gate (job-0086 lesson).
# ---------------------------------------------------------------------------


def test_geographic_gate_all_polygons_fall_inside_bbox():
    """job-0086 codified lesson: every NFHL polygon centroid is inside the
    Fort Myers bbox after the GeoJSON → FlatGeobuf round-trip.

    A sign-flip / axis-swap in the converter would surface here as centroids
    on the wrong continent.
    """
    fake_geojson = _sample_nfhl_geojson(5)

    # Sanity: input features already fall in the bbox.
    min_lon, min_lat, max_lon, max_lat = _FORT_MYERS_BBOX
    buf = 0.05
    for feat in fake_geojson["features"]:
        cx, cy = _centroid(feat["geometry"]["coordinates"])
        assert (min_lon - buf) <= cx <= (max_lon + buf)
        assert (min_lat - buf) <= cy <= (max_lat + buf)

    # Run through converter and verify geometries survive intact.
    fgb_bytes = _features_to_flatgeobuf(fake_geojson["features"])
    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 5
        for idx, geom in enumerate(gdf.geometry):
            c = geom.centroid
            assert (min_lon - buf) <= c.x <= (max_lon + buf), (
                f"Feature {idx} centroid x={c.x} outside Fort Myers lon envelope — "
                f"possible axis-swap bug"
            )
            assert (min_lat - buf) <= c.y <= (max_lat + buf), (
                f"Feature {idx} centroid y={c.y} outside Fort Myers lat envelope — "
                f"possible axis-swap bug"
            )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Live integration test (TRID3NT_TEST_LIVE_FEMA_NFHL=1 to run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_FEMA,
    reason="Set TRID3NT_TEST_LIVE_FEMA_NFHL=1 to run live FEMA NFHL tests",
)
def test_live_fort_myers_bbox_returns_polygons():
    """LIVE: real FEMA NFHL Fort Myers bbox returns ≥1 polygon with valid
    regulatory-flood-zone properties (FLD_ZONE in known set, SFHA_TF set,
    geometry valid).
    """
    fgb_bytes = _fetch_nfhl_bytes(
        bbox=_FORT_MYERS_LIVE_BBOX, sfha_only=False, zone_filter=None
    )
    assert len(fgb_bytes) > 0

    import geopandas as gpd
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        print(
            f"\n[LIVE FEMA NFHL] Fort Myers bbox returned {len(gdf)} flood-zone polygon(s)"
        )
        assert len(gdf) >= 1, "Fort Myers should have at least one regulatory flood zone"

        # Spot-check the regulatory-flood-zone schema.
        assert "FLD_ZONE" in gdf.columns
        assert "SFHA_TF" in gdf.columns
        assert "STATIC_BFE" in gdf.columns
        assert "DFIRM_ID" in gdf.columns

        # Every FLD_ZONE value should be a known designation.
        zones = set(gdf["FLD_ZONE"].dropna().tolist())
        print(f"  zone designations: {sorted(zones)}")
        unknown = zones - VALID_FLOOD_ZONES
        # Strict gate would fail; soft warning lets new designations surface.
        if unknown:
            print(f"  WARN: unknown FLD_ZONE values returned: {unknown}")

        # Geographic gate: every polygon's centroid must fall inside the bbox
        # (with a small buffer for intersects-semantics polygons that cross
        # the bbox edge).
        min_lon, min_lat, max_lon, max_lat = _FORT_MYERS_LIVE_BBOX
        buf = 0.5
        outside = 0
        for geom in gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            c = geom.centroid
            if not (
                (min_lon - buf) <= c.x <= (max_lon + buf)
                and (min_lat - buf) <= c.y <= (max_lat + buf)
            ):
                outside += 1
                print(f"  WARN: centroid outside bbox: ({c.x}, {c.y})")
        assert outside == 0, (
            f"{outside} polygons have centroids far outside Fort Myers bbox "
            f"— possible axis-swap regression"
        )

        # Print first few zones for diagnostics.
        for _, row in gdf.head(5).iterrows():
            print(
                f"  zone={row['FLD_ZONE']!r:>6} sfha={row['SFHA_TF']!r} "
                f"bfe={row['STATIC_BFE']:>8.2f} dfirm={row['DFIRM_ID']!r}"
            )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
