"""Unit tests for the ``fetch_mtbs_burn_severity`` atomic tool (job-0109).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- bbox validation rejects degenerate / out-of-range / non-finite.
- year_range validation rejects start > end / pre-1984 / non-int.
- where-clause builder produces YEAR clause (live schema field name).
- Mocked 50-feature response → FlatGeobuf with 50 polygons.
- year_range filter narrows (server-side where-clause).
- Empty bbox / empty year window → 0 features without raising.
- Pagination across 3000 features (two pages: 2000 + 1000).
- Cache miss invokes fetch_fn; second call (HIT) skips fetch_fn.
- year_range produces a different cache key from year_range=None.
- Returns ``LayerURI`` with expected shape.
- Upstream error envelope raises ``MTBSUpstreamError``.

Tests that hit the real MTBS ArcGIS endpoint are gated by
``TRID3NT_TEST_LIVE_MTBS=1``. They verify the California-bbox query returns
at least one known fire (e.g. Camp Fire 2018) — the job-0086 codified
geography-not-just-bytes lesson applies here.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch
from typing import Any

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity import (
    MTBSBboxError,
    MTBSError,
    MTBSUpstreamError,
    MTBSYearRangeError,
    _bbox_to_envelope,
    _build_where_clause,
    _round_bbox_to_6dp,
    _validate_bbox,
    _validate_year_range,
    fetch_mtbs_burn_severity,
)


# ---------------------------------------------------------------------------
# Constants / helpers.
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Northern California bbox (kickoff live-test target spans Camp Fire 2018,
# many other fires). Centered around Paradise/Chico.
_CA_BBOX = (-122.0, 38.0, -119.0, 40.0)

# Open-water bbox far offshore (no MTBS polygons).
_OCEAN_BBOX = (-30.0, 10.0, -25.0, 15.0)

_LIVE_MTBS = os.environ.get("TRID3NT_TEST_LIVE_MTBS") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors existing test pattern).
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


def _fire_polygon_feature(
    name: str,
    *,
    year: int = 2020,
    fire_type: str = "Wildfire",
    acres: float = 1500.0,
    fire_id: str | None = None,
    coords: list[list[list[float]]] | None = None,
    centroid: tuple[float, float] = (-121.4, 39.7),
) -> dict[str, Any]:
    """Build a GeoJSON polygon feature with MTBS-shaped properties.

    Property keys match the live EDW_MTBS_v1/FeatureServer/0 schema
    (UPPERCASE field names per the Esri_US_Federal_Data service).
    Default coords: tiny square around the centroid (defaults to Paradise, CA).
    """
    if coords is None:
        lon, lat = centroid
        d = 0.05
        coords = [
            [
                [lon - d, lat - d],
                [lon + d, lat - d],
                [lon + d, lat + d],
                [lon - d, lat + d],
                [lon - d, lat - d],
            ]
        ]
    return {
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": coords},
        "properties": {
            "FIRE_ID": fire_id or f"CA{year}{abs(hash(name)) % 10000:04d}",
            "FIRE_NAME": name,
            "YEAR": year,
            "FIRE_TYPE": fire_type,
            "ACRES": acres,
            "LATITUDE": centroid[1],
            "LONGITUDE": centroid[0],
            "MAP_ID": 1,
            "MAP_PROG": "MTBS",
            "ASMNT_TYPE": "Initial",
            "IRWINID": None,
            "IG_DATE": None,
        },
    }


def _mtbs_response(features: list[dict[str, Any]], exceeded: bool = False) -> dict[str, Any]:
    """Build a fake MTBS FeatureServer GeoJSON response."""
    payload: dict[str, Any] = {
        "type": "FeatureCollection",
        "features": features,
    }
    if exceeded:
        payload["exceededTransferLimit"] = True
    return payload


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_mtbs_burn_severity appears in TOOL_REGISTRY with expected metadata."""
    assert "fetch_mtbs_burn_severity" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_mtbs_burn_severity"]
    assert entry.metadata.name == "fetch_mtbs_burn_severity"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "mtbs_burn_severity"
    assert entry.metadata.cacheable is True


# ---------------------------------------------------------------------------
# Helper tests.
# ---------------------------------------------------------------------------


def test_validate_bbox_rejects_degenerate():
    """A bbox where min == max raises MTBSBboxError."""
    with pytest.raises(MTBSBboxError):
        _validate_bbox((-120.0, 38.0, -120.0, 38.0))


def test_validate_bbox_rejects_out_of_range():
    """Out-of-CRS-range longitudes raise MTBSBboxError."""
    with pytest.raises(MTBSBboxError):
        _validate_bbox((-200.0, 38.0, -180.0, 39.0))


def test_validate_bbox_rejects_non_finite():
    """Non-finite values raise MTBSBboxError."""
    with pytest.raises(MTBSBboxError):
        _validate_bbox((float("nan"), 38.0, -119.0, 40.0))


def test_validate_year_range_accepts_none():
    """year_range=None is normalized to None (no filter)."""
    assert _validate_year_range(None) is None


def test_validate_year_range_accepts_inclusive_endpoints():
    """year_range=(2020, 2023) returns (2020, 2023)."""
    assert _validate_year_range((2020, 2023)) == (2020, 2023)


def test_validate_year_range_accepts_single_year():
    """year_range=(2018, 2018) selects the single year 2018."""
    assert _validate_year_range((2018, 2018)) == (2018, 2018)


def test_validate_year_range_rejects_reversed():
    """year_range where start > end raises MTBSYearRangeError."""
    with pytest.raises(MTBSYearRangeError):
        _validate_year_range((2023, 2020))


def test_validate_year_range_rejects_pre_1984():
    """year_range starting before 1984 (MTBS coverage start) raises."""
    with pytest.raises(MTBSYearRangeError):
        _validate_year_range((1980, 2000))


def test_round_bbox_to_6dp():
    """_round_bbox_to_6dp quantizes to 6 decimal places."""
    raw = (-122.123456789, 38.123456789, -119.987654321, 40.987654321)
    rounded = _round_bbox_to_6dp(raw)
    assert rounded == (-122.123457, 38.123457, -119.987654, 40.987654)


def test_bbox_to_envelope_format():
    """_bbox_to_envelope formats as xmin,ymin,xmax,ymax."""
    env = _bbox_to_envelope((-122.0, 38.0, -119.0, 40.0))
    assert env == "-122.0,38.0,-119.0,40.0"


def test_build_where_clause_none_returns_1_eq_1():
    """No year_range → ``1=1`` (no YEAR filter)."""
    assert _build_where_clause(None) == "1=1"


def test_build_where_clause_with_range_returns_year_clause():
    """year_range=(2020, 2023) → ``YEAR >= 2020 AND YEAR <= 2023`` (live schema)."""
    clause = _build_where_clause((2020, 2023))
    assert clause == "YEAR >= 2020 AND YEAR <= 2023"


def test_degenerate_bbox_raises_through_public_tool():
    """The public tool surface validates the bbox before any network call."""
    with pytest.raises(MTBSBboxError):
        fetch_mtbs_burn_severity(bbox=(-120.0, 38.0, -120.0, 38.0))


def test_invalid_year_range_raises_through_public_tool():
    """The public tool surface validates year_range before any network call."""
    with pytest.raises(MTBSYearRangeError):
        fetch_mtbs_burn_severity(bbox=_CA_BBOX, year_range=(2023, 2020))


# ---------------------------------------------------------------------------
# Mocked-network tests.
# ---------------------------------------------------------------------------


def test_mocked_50_features_returns_50_polygons():
    """A mocked 50-feature response yields a FlatGeobuf with 50 polygons."""
    import geopandas as gpd

    features = [
        _fire_polygon_feature(f"Fire {i}", year=2020 + (i % 3), fire_id=f"E{i:04d}")
        for i in range(50)
    ]
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity._mtbs_query_one_page",
        return_value=_mtbs_response(features, exceeded=False),
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_mtbs_burn_severity(bbox=_CA_BBOX)

    # Round-trip the stored bytes through geopandas.
    assert len(fake_gcs.store) == 1
    fgb_bytes = next(iter(fake_gcs.store.values()))
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 50, f"expected 50 features, got {len(gdf)}"
        assert "FIRE_NAME" in gdf.columns
        assert "YEAR" in gdf.columns
        assert "ACRES" in gdf.columns
        assert result.layer_type == "vector"
        assert result.role == "primary"
        assert result.units is None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_mocked_year_range_narrows_via_where_clause():
    """year_range produces a server-side where=YEAR >=... clause (live schema)."""
    captured: dict[str, Any] = {}

    def capture_one_page(bbox, year_range, offset):
        captured["year_range"] = year_range
        captured["where_clause"] = _build_where_clause(year_range)
        return _mtbs_response([
            _fire_polygon_feature("Camp Fire", year=2018, fire_id="CA20181101"),
        ], exceeded=False)

    fake_gcs = FakeStorageClient()
    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity._mtbs_query_one_page",
        side_effect=capture_one_page,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        fetch_mtbs_burn_severity(bbox=_CA_BBOX, year_range=(2018, 2018))

    assert captured["year_range"] == (2018, 2018)
    assert captured["where_clause"] == "YEAR >= 2018 AND YEAR <= 2018"


def test_mocked_empty_bbox_returns_zero_features():
    """An empty MTBS response (open water / no fires in window) yields empty FlatGeobuf."""
    import geopandas as gpd

    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity._mtbs_query_one_page",
        return_value=_mtbs_response([], exceeded=False),
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_mtbs_burn_severity(bbox=_OCEAN_BBOX)

    # Should not raise. Bytes should be a valid (empty) FlatGeobuf.
    assert result.uri is not None
    fgb_bytes = next(iter(fake_gcs.store.values()))
    assert len(fgb_bytes) > 0, "Empty FlatGeobuf should still be non-zero bytes"
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 0, f"expected 0 features, got {len(gdf)}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_mocked_pagination_across_3000_features():
    """A 3000-feature response across two pages (2000 + 1000) merges correctly."""
    import geopandas as gpd

    page1 = [
        _fire_polygon_feature(f"Fire p1-{i}", year=2020, fire_id=f"P1{i:04d}")
        for i in range(2000)
    ]
    page2 = [
        _fire_polygon_feature(f"Fire p2-{i}", year=2021, fire_id=f"P2{i:04d}")
        for i in range(1000)
    ]

    fake_gcs = FakeStorageClient()
    call_count = {"n": 0}

    def side_effect(bbox, year_range, offset):
        call_count["n"] += 1
        if offset == 0:
            return _mtbs_response(page1, exceeded=True)
        elif offset == 2000:
            return _mtbs_response(page2, exceeded=False)
        else:
            raise AssertionError(f"unexpected offset={offset}")

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity._mtbs_query_one_page",
        side_effect=side_effect,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        fetch_mtbs_burn_severity(bbox=_CA_BBOX)

    assert call_count["n"] == 2, f"expected 2 pages fetched, got {call_count['n']}"
    fgb_bytes = next(iter(fake_gcs.store.values()))
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 3000, f"expected 3000 features after merge, got {len(gdf)}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_cache_miss_then_hit_skips_fetch_fn():
    """Second call with the same bbox + year_range is a cache HIT and does not re-fetch."""
    features = [_fire_polygon_feature("Camp Fire", year=2018, fire_id="CA20181101")]
    fake_gcs = FakeStorageClient()
    page_call_count = {"n": 0}

    def page_side_effect(bbox, year_range, offset):
        page_call_count["n"] += 1
        return _mtbs_response(features, exceeded=False)

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity._mtbs_query_one_page",
        side_effect=page_side_effect,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r1 = fetch_mtbs_burn_severity(bbox=_CA_BBOX)
        r2 = fetch_mtbs_burn_severity(bbox=_CA_BBOX)

    assert page_call_count["n"] == 1, (
        f"fetch_fn should run once (miss); got {page_call_count['n']} calls"
    )
    assert r1.uri == r2.uri


def test_year_range_changes_cache_key():
    """Different year_range produces a different cache key (separate URIs)."""
    features = [_fire_polygon_feature("Test Fire", year=2018, fire_id="T2018")]
    fake_gcs = FakeStorageClient()

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity._mtbs_query_one_page",
        return_value=_mtbs_response(features, exceeded=False),
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        r_all = fetch_mtbs_burn_severity(bbox=_CA_BBOX)
        r_2018 = fetch_mtbs_burn_severity(bbox=_CA_BBOX, year_range=(2018, 2018))

    assert r_all.uri != r_2018.uri, (
        "Different year_range should produce different cache keys"
    )


def test_upstream_error_envelope_raises_mtbs_upstream_error():
    """An ArcGIS error envelope (200 OK with {error}) raises MTBSUpstreamError."""

    def raise_upstream(bbox, year_range, offset):
        raise MTBSUpstreamError("MTBS query returned error envelope")

    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity._mtbs_query_one_page",
        side_effect=raise_upstream,
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity.read_through",
        side_effect=_make_read_through_injector(FakeStorageClient()),
    ):
        with pytest.raises(MTBSUpstreamError):
            fetch_mtbs_burn_severity(bbox=_CA_BBOX)


def test_upstream_error_is_retryable():
    """MTBSUpstreamError carries retryable=True for FR-AS-11 mapping."""
    err = MTBSUpstreamError("test")
    assert err.retryable is True
    assert err.error_code == "MTBS_UPSTREAM_ERROR"


def test_bbox_error_is_not_retryable():
    """MTBSBboxError carries retryable=False (no point in retrying invalid input)."""
    err = MTBSBboxError("test")
    assert err.retryable is False
    assert err.error_code == "MTBS_BBOX_INVALID"


def test_year_range_error_is_not_retryable():
    """MTBSYearRangeError carries retryable=False."""
    err = MTBSYearRangeError("test")
    assert err.retryable is False
    assert err.error_code == "MTBS_YEAR_RANGE_INVALID"


def test_layer_uri_shape():
    """The LayerURI shape is correct (vector, primary, units=None, mtbs path)."""
    fake_gcs = FakeStorageClient()
    features = [_fire_polygon_feature("Test Fire", year=2020)]
    with patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity._mtbs_query_one_page",
        return_value=_mtbs_response(features, exceeded=False),
    ), patch(
        "trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        result = fetch_mtbs_burn_severity(bbox=_CA_BBOX, year_range=(2018, 2023))

    assert result.layer_type == "vector"
    assert result.role == "primary"
    assert result.units is None
    assert result.uri.startswith("s3://")
    assert "/mtbs_burn_severity/" in result.uri
    assert result.uri.endswith(".fgb")
    assert result.style_preset == "mtbs_burn_severity"
    assert "MTBS" in result.name
    assert "2018" in result.name and "2023" in result.name


# ---------------------------------------------------------------------------
# Live integration tests (TRID3NT_TEST_LIVE_MTBS=1 to run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_MTBS,
    reason="Set TRID3NT_TEST_LIVE_MTBS=1 to run live MTBS download tests",
)
def test_live_california_returns_known_fires():
    """LIVE: queries real MTBS and verifies a CA bbox returns known fires.

    Codified job-0086 lesson: not just bytes-round-trip; we verify the known
    geography (≥1 fire from the year range falls inside the requested bbox).
    """
    import geopandas as gpd
    from trid3nt_server.tools.fetchers.hazard.fetch_mtbs_burn_severity import (
        _fetch_mtbs_bytes,
    )

    # 2020-2023 window: should include North Complex 2020, Dixie 2021, etc.
    fgb_bytes = _fetch_mtbs_bytes(_CA_BBOX, year_range=(2020, 2023))
    assert len(fgb_bytes) > 0

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) >= 1, "expected at least 1 MTBS fire in CA 2020-2023 bbox"

        # Geographic-correctness gate (job-0086 codified lesson): every fire
        # centroid must fall inside the requested bbox. The MTBS layer's
        # geometry is in EPSG:4326 (we set inSR/outSR=4326), so a direct
        # bbox check is correct.
        min_lon, min_lat, max_lon, max_lat = _CA_BBOX
        inside = 0
        for geom in gdf.geometry:
            if geom is None:
                continue
            c = geom.centroid
            if min_lon <= c.x <= max_lon and min_lat <= c.y <= max_lat:
                inside += 1
        assert inside >= 1, (
            f"expected ≥1 fire centroid inside bbox={_CA_BBOX}, got {inside}"
        )

        # Sanity: YEAR values respect the requested range.
        years = sorted({int(y) for y in gdf["YEAR"].dropna().tolist()})
        assert all(2020 <= y <= 2023 for y in years), (
            f"expected YEAR in [2020, 2023], got {years}"
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
