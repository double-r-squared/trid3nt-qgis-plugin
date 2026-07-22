"""Unit tests for the ``fetch_roads_osm`` atomic tool (job-0097).

Coverage:
- Tool is registered in TOOL_REGISTRY with expected metadata.
- 50-way mocked Overpass response → 50 features in FlatGeobuf.
- ``road_classes=['motorway']`` filter narrows the QL regex to motorway only.
- Empty bbox (no Overpass elements) → 0 features, no error, valid empty FGB.
- HTTP 504 (Overpass gateway timeout) → typed retryable ``OSMUpstreamError``.
- HTTP 400 (bad query) → typed non-retryable ``OSMUpstreamError``.
- Cache miss vs cache hit (fake GCS).
- Invalid bbox / unknown highway class → ``OSMInputError`` (non-retryable).
- ``road_classes`` sorting collapses different orderings onto the same cache key.
- Live verification (``GRACE2_TEST_LIVE_OSM=1``):
  Fort Myers small bbox → ≥1 LineString feature, all coords inside bbox.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.fetch_roads_osm import (
    _DEFAULT_ROAD_CLASSES,
    _build_overpass_ql,
    _clip_record_to_bbox,
    _clip_records_to_bbox,
    _extract_way_record,
    _extract_way_records,
    _linestring_parts,
    _round_bbox_to_6dp,
    _validate_and_normalize_road_classes,
    OSMError,
    OSMInputError,
    OSMUpstreamError,
    fetch_roads_osm,
)


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

# Fort Myers small bbox — covers a slice of I-75 + US-41 (Tamiami Trail).
_FORT_MYERS_BBOX = (-82.0, 26.5, -81.8, 26.7)

_LIVE_OSM = os.environ.get("GRACE2_TEST_LIVE_OSM") == "1"


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors test_fetch_administrative_boundaries.py).
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


def _make_way(osm_id: int, coords: list[tuple[float, float]], **tags: Any) -> dict[str, Any]:
    """Build a mocked Overpass ``way`` element."""
    return {
        "type": "way",
        "id": osm_id,
        "geometry": [{"lat": lat, "lon": lon} for lon, lat in coords],
        "tags": tags or {},
    }


def _mock_overpass_payload(ways: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 0.6,
        "generator": "Overpass API (mock)",
        "elements": ways,
    }


def _fast_sleep(monkeypatch):
    """Make the 1-second polite delay no-op so tests run fast."""
    monkeypatch.setattr(
        "grace2_agent.tools.fetch_roads_osm.time.sleep", lambda *_: None
    )


# ---------------------------------------------------------------------------
# Registration tests.
# ---------------------------------------------------------------------------


def test_tool_is_registered_in_registry():
    """fetch_roads_osm appears in TOOL_REGISTRY with the expected metadata."""
    assert "fetch_roads_osm" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["fetch_roads_osm"]
    assert entry.metadata.name == "fetch_roads_osm"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "osm_roads"
    assert entry.metadata.cacheable is True


def test_default_road_classes_match_kickoff():
    """Default road_classes set matches the audit.md kickoff verbatim."""
    assert set(_DEFAULT_ROAD_CLASSES) == {
        "motorway", "trunk", "primary", "secondary", "tertiary",
        "motorway_link", "trunk_link", "primary_link",
    }


# ---------------------------------------------------------------------------
# Input validation tests.
# ---------------------------------------------------------------------------


def test_bad_bbox_raises_input_error():
    """Degenerate bbox raises OSMInputError (not generic RuntimeError)."""
    with pytest.raises(OSMInputError):
        fetch_roads_osm(bbox=(-82.0, 26.5, -82.0, 26.5))


def test_unknown_highway_class_raises_input_error():
    """Unknown highway tag value raises OSMInputError, retryable=False."""
    try:
        fetch_roads_osm(bbox=_FORT_MYERS_BBOX, road_classes=["bogus_class"])
    except OSMInputError as exc:
        assert exc.retryable is False
    else:
        pytest.fail("Expected OSMInputError")


def test_empty_road_classes_raises_input_error():
    """Empty road_classes list raises OSMInputError (ambiguous intent)."""
    with pytest.raises(OSMInputError):
        fetch_roads_osm(bbox=_FORT_MYERS_BBOX, road_classes=[])


def test_road_classes_normalizer_sorts_and_dedupes():
    """_validate_and_normalize_road_classes sorts and dedupes the input list."""
    out = _validate_and_normalize_road_classes(["primary", "motorway", "primary"])
    assert out == ("motorway", "primary")


def test_road_classes_none_returns_default_sorted():
    """road_classes=None returns the sorted default tuple."""
    out = _validate_and_normalize_road_classes(None)
    assert out == tuple(sorted(_DEFAULT_ROAD_CLASSES))


# ---------------------------------------------------------------------------
# Overpass-QL builder tests.
# ---------------------------------------------------------------------------


def test_overpass_ql_contains_bbox_and_classes():
    """_build_overpass_ql emits QL with correct (s,w,n,e) bbox and class regex."""
    bbox = (-82.0, 26.5, -81.8, 26.7)
    ql = _build_overpass_ql(bbox, ("motorway", "primary"))
    # bbox: south,west,north,east = 26.5,-82.0,26.7,-81.8
    assert "(26.5,-82.0,26.7,-81.8)" in ql
    assert "^(motorway|primary)$" in ql
    assert "out geom;" in ql
    assert "[out:json][timeout:60]" in ql


def test_overpass_ql_narrowed_to_motorway_only():
    """When road_classes=['motorway'], the QL regex names only motorway."""
    bbox = (-82.0, 26.5, -81.8, 26.7)
    ql = _build_overpass_ql(bbox, ("motorway",))
    assert "^(motorway)$" in ql
    assert "primary" not in ql
    assert "secondary" not in ql


# ---------------------------------------------------------------------------
# Way-record extraction tests.
# ---------------------------------------------------------------------------


def test_extract_way_record_with_geometry():
    """A well-formed way element projects to the expected record schema."""
    way = _make_way(
        12345,
        [(-82.0, 26.5), (-81.95, 26.55), (-81.9, 26.6)],
        name="I-75", highway="motorway", lanes="4", maxspeed="70 mph",
    )
    rec = _extract_way_record(way)
    assert rec is not None
    assert rec["osm_id"] == 12345
    assert rec["name"] == "I-75"
    assert rec["highway"] == "motorway"
    assert rec["lanes"] == "4"
    assert rec["maxspeed"] == "70 mph"
    assert rec["coords"] == [(-82.0, 26.5), (-81.95, 26.55), (-81.9, 26.6)]


def test_extract_way_record_skips_single_point():
    """A way with <2 valid points returns None (not a LineString)."""
    way = _make_way(99, [(-82.0, 26.5)], highway="motorway")
    assert _extract_way_record(way) is None


def test_extract_way_records_filters_invalid_elements():
    """_extract_way_records skips non-way elements and malformed ways."""
    payload = _mock_overpass_payload([
        _make_way(1, [(-82.0, 26.5), (-81.9, 26.6)], highway="motorway"),
        {"type": "node", "id": 2, "lat": 26.5, "lon": -82.0},  # non-way
        _make_way(3, [(-82.1, 26.55)], highway="primary"),  # single point
        _make_way(4, [(-82.0, 26.5), (-81.95, 26.55)], highway="primary"),
    ])
    recs = _extract_way_records(payload)
    assert len(recs) == 2
    assert [r["osm_id"] for r in recs] == [1, 4]


# ---------------------------------------------------------------------------
# Bbox-clip tests (F39 — roads must not spill outside the requested AOI).
# ---------------------------------------------------------------------------


def _coords_inside(coords, bbox, eps: float = 1e-9) -> bool:
    """All (lon, lat) coords fall within bbox (inclusive, with float epsilon)."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return all(
        (min_lon - eps) <= lon <= (max_lon + eps)
        and (min_lat - eps) <= lat <= (max_lat + eps)
        for lon, lat in coords
    )


def test_clip_record_trims_geometry_extending_outside_bbox():
    """A way that spills past the bbox is clipped to the bbox boundary."""
    bbox = (-82.0, 26.5, -81.8, 26.7)
    # Road runs from well WEST of the bbox into its interior.
    rec = {
        "osm_id": 1, "name": "I-75", "highway": "motorway",
        "lanes": "4", "maxspeed": "70 mph",
        "coords": [(-82.3, 26.6), (-81.9, 26.6)],
    }
    out = _clip_record_to_bbox(rec, bbox)
    assert len(out) == 1
    clipped = out[0]
    # Attributes carried through unchanged.
    assert clipped["osm_id"] == 1
    assert clipped["name"] == "I-75"
    assert clipped["highway"] == "motorway"
    assert clipped["lanes"] == "4"
    assert clipped["maxspeed"] == "70 mph"
    # Geometry no longer extends west of the bbox's min_lon (-82.0).
    assert _coords_inside(clipped["coords"], bbox)
    lons = [lon for lon, _ in clipped["coords"]]
    assert min(lons) >= -82.0 - 1e-9
    # The western endpoint snapped to the bbox edge.
    assert min(lons) == pytest.approx(-82.0)


def test_clip_record_already_inside_is_unchanged():
    """A way fully inside the bbox passes through with identical coords."""
    bbox = (-82.0, 26.5, -81.8, 26.7)
    coords = [(-81.95, 26.55), (-81.9, 26.6), (-81.85, 26.65)]
    rec = {
        "osm_id": 2, "name": "Local Rd", "highway": "primary",
        "lanes": None, "maxspeed": None, "coords": coords,
    }
    out = _clip_record_to_bbox(rec, bbox)
    assert len(out) == 1
    assert _coords_inside(out[0]["coords"], bbox)
    assert out[0]["coords"] == coords


def test_clip_record_entirely_outside_is_dropped():
    """A way fully outside the bbox contributes zero records."""
    bbox = (-82.0, 26.5, -81.8, 26.7)
    rec = {
        "osm_id": 3, "name": "Far Away Rd", "highway": "motorway",
        "lanes": None, "maxspeed": None,
        "coords": [(-83.0, 26.6), (-82.5, 26.6)],
    }
    assert _clip_record_to_bbox(rec, bbox) == []


def test_clip_record_crossing_boundary_twice_yields_two_segments():
    """A way that exits and re-enters the bbox yields multiple in-AOI segments,
    each carrying the source way's attributes."""
    bbox = (-82.0, 26.5, -81.8, 26.7)
    # Zig-zag: inside -> west(out) -> inside -> west(out) -> inside.
    rec = {
        "osm_id": 4, "name": "Zigzag Hwy", "highway": "trunk",
        "lanes": "2", "maxspeed": None,
        "coords": [
            (-81.9, 26.65),   # inside
            (-82.2, 26.65),   # out (west)
            (-81.9, 26.60),   # inside
            (-82.2, 26.60),   # out (west)
            (-81.9, 26.55),   # inside
        ],
    }
    out = _clip_record_to_bbox(rec, bbox)
    assert len(out) >= 2, f"expected ≥2 clipped segments, got {len(out)}"
    for seg in out:
        assert seg["osm_id"] == 4
        assert seg["name"] == "Zigzag Hwy"
        assert seg["highway"] == "trunk"
        assert _coords_inside(seg["coords"], bbox)
        assert len(seg["coords"]) >= 2


def test_clip_records_to_bbox_aggregates_and_filters():
    """_clip_records_to_bbox clips each record; outside ways drop, inside stay."""
    bbox = (-82.0, 26.5, -81.8, 26.7)
    records = [
        {"osm_id": 1, "name": "in", "highway": "primary",
         "lanes": None, "maxspeed": None,
         "coords": [(-81.95, 26.55), (-81.9, 26.6)]},          # fully inside
        {"osm_id": 2, "name": "out", "highway": "primary",
         "lanes": None, "maxspeed": None,
         "coords": [(-83.0, 26.6), (-82.5, 26.6)]},            # fully outside
        {"osm_id": 3, "name": "straddle", "highway": "primary",
         "lanes": None, "maxspeed": None,
         "coords": [(-82.3, 26.6), (-81.9, 26.6)]},            # straddling
    ]
    out = _clip_records_to_bbox(records, bbox)
    ids = sorted({r["osm_id"] for r in out})
    assert ids == [1, 3], f"outside way should drop; got ids={ids}"
    for r in out:
        assert _coords_inside(r["coords"], bbox)


def test_linestring_parts_handles_empty_and_point():
    """_linestring_parts returns [] for empty / point-degenerate clip results."""
    from shapely import clip_by_rect
    from shapely.geometry import LineString

    bbox = (-82.0, 26.5, -81.8, 26.7)
    # Line entirely outside → empty GeometryCollection.
    empty = clip_by_rect(LineString([(-83.0, 26.6), (-82.5, 26.6)]), *bbox)
    assert _linestring_parts(empty) == []
    # None guard.
    assert _linestring_parts(None) == []


def test_clip_record_drops_degenerate_single_point_input():
    """A record whose coords degenerate to <2 points yields no segment."""
    bbox = (-82.0, 26.5, -81.8, 26.7)
    rec = {"osm_id": 9, "name": None, "highway": "primary",
           "lanes": None, "maxspeed": None, "coords": [(-81.9, 26.6)]}
    assert _clip_record_to_bbox(rec, bbox) == []


# ---------------------------------------------------------------------------
# Mocked Overpass POST tests — 50 ways round-trip.
# ---------------------------------------------------------------------------


def test_50_way_response_yields_50_features(monkeypatch):
    """50 mocked Overpass ways → 50 features in the FlatGeobuf."""
    _fast_sleep(monkeypatch)
    fake_gcs = FakeStorageClient()
    ways = [
        _make_way(
            100 + i,
            [(-82.0 + 0.001 * i, 26.5 + 0.001 * i),
             (-82.0 + 0.001 * (i + 1), 26.5 + 0.001 * (i + 1))],
            name=f"Test Rd {i}", highway="primary",
        )
        for i in range(50)
    ]
    payload = _mock_overpass_payload(ways)

    class FakeResponse:
        status_code = 200
        def raise_for_status(self) -> None: pass
        def json(self) -> dict[str, Any]: return payload

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, data=None):
            return FakeResponse()
        def close(self): pass

    with patch("grace2_agent.tools.fetch_roads_osm.httpx.Client", FakeClient), \
         patch(
             "grace2_agent.tools.fetch_roads_osm.read_through",
             side_effect=_make_read_through_injector(fake_gcs),
         ):
        result = fetch_roads_osm(bbox=_FORT_MYERS_BBOX, road_classes=["primary"])

    assert result.uri is not None
    assert result.uri.startswith("s3://")
    assert "osm_roads" in result.uri

    # Round-trip the cached FGB → assert 50 LineString features.
    import geopandas as gpd
    fgb_bytes = next(iter(fake_gcs.store.values()))
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) == 50
        assert (gdf.geometry.geom_type == "LineString").all()
        assert "name" in gdf.columns
        assert "highway" in gdf.columns
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_end_to_end_clips_spilling_roads_to_bbox(monkeypatch):
    """E2E: Overpass returns ways spilling outside the bbox; the cached FGB
    geometry is clipped so every vertex is strictly inside the requested bbox.
    """
    _fast_sleep(monkeypatch)
    fake_gcs = FakeStorageClient()
    bbox = _FORT_MYERS_BBOX  # (-82.0, 26.5, -81.8, 26.7)
    ways = [
        # Straddles the western edge (starts at -82.3, well outside).
        _make_way(1, [(-82.3, 26.6), (-81.9, 26.6)], name="W spill", highway="motorway"),
        # Fully inside.
        _make_way(2, [(-81.95, 26.55), (-81.85, 26.65)], name="inside", highway="primary"),
        # Fully outside (west) — should drop entirely.
        _make_way(3, [(-83.0, 26.6), (-82.5, 26.6)], name="gone", highway="primary"),
    ]
    payload = _mock_overpass_payload(ways)

    class FakeResponse:
        status_code = 200
        def raise_for_status(self) -> None: pass
        def json(self): return payload

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def post(self, url, data=None): return FakeResponse()
        def close(self): pass

    with patch("grace2_agent.tools.fetch_roads_osm.httpx.Client", FakeClient), \
         patch(
             "grace2_agent.tools.fetch_roads_osm.read_through",
             side_effect=_make_read_through_injector(fake_gcs),
         ):
        result = fetch_roads_osm(bbox=bbox, road_classes=["motorway", "primary"])

    assert result.uri is not None

    import geopandas as gpd
    from shapely.geometry import box as shapely_box

    fgb_bytes = next(iter(fake_gcs.store.values()))
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tf:
        path = tf.name
        tf.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        # The fully-outside way (osm_id=3) is gone; 2 in-AOI ways survive.
        assert len(gdf) >= 2
        ids = set(int(v) for v in gdf["osm_id"].dropna().tolist())
        assert 3 not in ids, "fully-outside way should be dropped"
        assert {1, 2} <= ids

        # STRICT geographic-correctness: NO geometry vertex spills outside bbox.
        bbox_poly = shapely_box(*bbox)
        eps = 1e-6
        for idx, geom in gdf.geometry.items():
            minx, miny, maxx, maxy = geom.bounds
            assert minx >= bbox[0] - eps and miny >= bbox[1] - eps, (
                f"feature {idx} bounds {geom.bounds} spill below bbox {bbox}"
            )
            assert maxx <= bbox[2] + eps and maxy <= bbox[3] + eps, (
                f"feature {idx} bounds {geom.bounds} spill above bbox {bbox}"
            )
            # Geometry is contained within (or on the boundary of) the bbox.
            assert geom.within(bbox_poly) or geom.intersects(bbox_poly.boundary)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_empty_bbox_yields_zero_features(monkeypatch):
    """Empty Overpass response (no ways) → 0 features, no exception."""
    _fast_sleep(monkeypatch)
    fake_gcs = FakeStorageClient()
    payload = _mock_overpass_payload([])

    class FakeResponse:
        status_code = 200
        def raise_for_status(self) -> None: pass
        def json(self) -> dict[str, Any]: return payload

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def post(self, url, data=None): return FakeResponse()
        def close(self): pass

    with patch("grace2_agent.tools.fetch_roads_osm.httpx.Client", FakeClient), \
         patch(
             "grace2_agent.tools.fetch_roads_osm.read_through",
             side_effect=_make_read_through_injector(fake_gcs),
         ):
        result = fetch_roads_osm(bbox=_FORT_MYERS_BBOX)

    assert result.uri is not None
    # Empty FGB still written to cache (correct behavior, not a sentinel).
    assert len(fake_gcs.store) == 1


# ---------------------------------------------------------------------------
# HTTP error mapping.
# ---------------------------------------------------------------------------


def _make_failing_client(status_code: int):
    """Return a FakeClient class whose .post() raises HTTPStatusError with given code.

    Used by the HTTP-error tests below; the read_through shim is patched to
    inject a fake GCS so the test does not need ADC / a real cloud project.
    """
    class FakeResponse:
        def __init__(self): self.status_code = status_code
        def raise_for_status(self) -> None:
            raise httpx.HTTPStatusError(
                f"{status_code}",
                request=httpx.Request("POST", "https://overpass-api.de/api/interpreter"),
                response=httpx.Response(status_code),
            )
        def json(self): return {}

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def post(self, url, data=None): return FakeResponse()
        def close(self): pass

    return FakeClient


def test_504_maps_to_retryable_upstream_error(monkeypatch):
    """Overpass HTTP 504 → OSMUpstreamError with retryable=True."""
    _fast_sleep(monkeypatch)
    fake_gcs = FakeStorageClient()

    with patch(
        "grace2_agent.tools.fetch_roads_osm.httpx.Client",
        _make_failing_client(504),
    ), patch(
        "grace2_agent.tools.fetch_roads_osm.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        try:
            fetch_roads_osm(bbox=_FORT_MYERS_BBOX, road_classes=["motorway"])
        except OSMUpstreamError as exc:
            assert exc.retryable is True
        else:
            pytest.fail("Expected OSMUpstreamError on HTTP 504")


def test_400_maps_to_non_retryable_upstream_error(monkeypatch):
    """Overpass HTTP 400 (bad query) → OSMUpstreamError with retryable=False."""
    _fast_sleep(monkeypatch)
    fake_gcs = FakeStorageClient()

    with patch(
        "grace2_agent.tools.fetch_roads_osm.httpx.Client",
        _make_failing_client(400),
    ), patch(
        "grace2_agent.tools.fetch_roads_osm.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        try:
            fetch_roads_osm(bbox=_FORT_MYERS_BBOX, road_classes=["motorway"])
        except OSMUpstreamError as exc:
            assert exc.retryable is False
        else:
            pytest.fail("Expected OSMUpstreamError on HTTP 400")


def test_429_maps_to_retryable_upstream_error(monkeypatch):
    """Overpass HTTP 429 (rate limit) → OSMUpstreamError with retryable=True."""
    _fast_sleep(monkeypatch)
    fake_gcs = FakeStorageClient()

    with patch(
        "grace2_agent.tools.fetch_roads_osm.httpx.Client",
        _make_failing_client(429),
    ), patch(
        "grace2_agent.tools.fetch_roads_osm.read_through",
        side_effect=_make_read_through_injector(fake_gcs),
    ):
        try:
            fetch_roads_osm(bbox=_FORT_MYERS_BBOX, road_classes=["motorway"])
        except OSMUpstreamError as exc:
            assert exc.retryable is True
        else:
            pytest.fail("Expected OSMUpstreamError on HTTP 429")


# ---------------------------------------------------------------------------
# Cache miss vs hit.
# ---------------------------------------------------------------------------


def test_cache_miss_then_hit(monkeypatch):
    """First call fetches and writes; second call short-circuits to cache."""
    _fast_sleep(monkeypatch)
    fake_gcs = FakeStorageClient()
    call_count = {"n": 0}

    payload = _mock_overpass_payload([
        _make_way(1, [(-82.0, 26.5), (-81.9, 26.6)], name="X", highway="motorway"),
    ])

    class FakeResponse:
        status_code = 200
        def raise_for_status(self) -> None: pass
        def json(self): return payload

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def post(self, url, data=None):
            call_count["n"] += 1
            return FakeResponse()
        def close(self): pass

    with patch("grace2_agent.tools.fetch_roads_osm.httpx.Client", FakeClient), \
         patch(
             "grace2_agent.tools.fetch_roads_osm.read_through",
             side_effect=_make_read_through_injector(fake_gcs),
         ):
        r1 = fetch_roads_osm(bbox=_FORT_MYERS_BBOX, road_classes=["motorway"])
        r2 = fetch_roads_osm(bbox=_FORT_MYERS_BBOX, road_classes=["motorway"])

    assert call_count["n"] == 1, "Overpass should be hit once; second call is HIT"
    assert r1.uri == r2.uri


def test_cache_key_independent_of_class_ordering(monkeypatch):
    """road_classes=['motorway','primary'] and ['primary','motorway'] share a cache entry."""
    _fast_sleep(monkeypatch)
    fake_gcs = FakeStorageClient()
    call_count = {"n": 0}

    payload = _mock_overpass_payload([
        _make_way(1, [(-82.0, 26.5), (-81.9, 26.6)], highway="motorway"),
    ])

    class FakeResponse:
        status_code = 200
        def raise_for_status(self) -> None: pass
        def json(self): return payload

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def post(self, url, data=None):
            call_count["n"] += 1
            return FakeResponse()
        def close(self): pass

    with patch("grace2_agent.tools.fetch_roads_osm.httpx.Client", FakeClient), \
         patch(
             "grace2_agent.tools.fetch_roads_osm.read_through",
             side_effect=_make_read_through_injector(fake_gcs),
         ):
        r1 = fetch_roads_osm(
            bbox=_FORT_MYERS_BBOX, road_classes=["motorway", "primary"]
        )
        r2 = fetch_roads_osm(
            bbox=_FORT_MYERS_BBOX, road_classes=["primary", "motorway"]
        )

    assert call_count["n"] == 1
    assert r1.uri == r2.uri


# ---------------------------------------------------------------------------
# LayerURI shape.
# ---------------------------------------------------------------------------


def test_layer_uri_shape(monkeypatch):
    """fetch_roads_osm returns a LayerURI with vector / context / units=None."""
    _fast_sleep(monkeypatch)
    fake_gcs = FakeStorageClient()

    payload = _mock_overpass_payload([
        _make_way(1, [(-82.0, 26.5), (-81.9, 26.6)], name="I-75", highway="motorway"),
    ])

    class FakeResponse:
        status_code = 200
        def raise_for_status(self) -> None: pass
        def json(self): return payload

    class FakeClient:
        def __init__(self, *a, **kw): pass
        def post(self, url, data=None): return FakeResponse()
        def close(self): pass

    with patch("grace2_agent.tools.fetch_roads_osm.httpx.Client", FakeClient), \
         patch(
             "grace2_agent.tools.fetch_roads_osm.read_through",
             side_effect=_make_read_through_injector(fake_gcs),
         ):
        result = fetch_roads_osm(
            bbox=_FORT_MYERS_BBOX, road_classes=["motorway"]
        )

    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.units is None
    assert result.uri.startswith("s3://")
    assert "osm_roads" in result.uri
    assert result.style_preset == "osm_roads"
    assert "OSM Roads" in result.name
    assert "motorway" in result.name


# ---------------------------------------------------------------------------
# bbox-rounding helper.
# ---------------------------------------------------------------------------


def test_round_bbox_to_6dp():
    raw = (-82.123456789, 26.123456789, -81.987654321, 26.987654321)
    assert _round_bbox_to_6dp(raw) == (-82.123457, 26.123457, -81.987654, 26.987654)


# ---------------------------------------------------------------------------
# Live integration test (GRACE2_TEST_LIVE_OSM=1 to run).
#
# Per the codified job-0086 lesson: a geometric tool's acceptance MUST verify
# the output against the known geography — here, that returned roads actually
# fall INSIDE the requested bbox AND that the major routes through Fort Myers
# (I-75 and US-41) appear by name.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_OSM,
    reason="Set GRACE2_TEST_LIVE_OSM=1 to run live Overpass tests",
)
def test_live_fort_myers_returns_primary_and_motorway():
    """LIVE: small Fort Myers bbox returns ≥1 feature, all coords inside bbox,
    with I-75 (motorway) and/or US-41 (Tamiami Trail, primary) by name.
    """
    import geopandas as gpd

    from grace2_agent.tools.fetch_roads_osm import _fetch_osm_roads_bytes
    bbox = _FORT_MYERS_BBOX

    fgb_bytes = _fetch_osm_roads_bytes(bbox, ("primary", "motorway"))
    assert len(fgb_bytes) > 0

    from shapely.geometry import box as shapely_box  # type: ignore[import-not-found]

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        path = f.name
        f.write(fgb_bytes)
    try:
        gdf = gpd.read_file(path, engine="pyogrio")
        assert len(gdf) >= 1, "Expected at least 1 road feature"

        # Geographic-correctness check (job-0086 + F39 job-0178): every
        # returned way MUST fall STRICTLY inside the requested bbox. Overpass's
        # ``out geom`` returns the FULL way geometry for any way with at least
        # one node inside the bbox, so the tool now clips each LineString to
        # the exact bbox before serializing. The right contract to assert is
        # therefore the stronger one: "roads do not spill outside the AOI".
        bbox_poly = shapely_box(*bbox)
        eps = 1e-6
        for idx, geom in gdf.geometry.items():
            assert geom.intersects(bbox_poly), (
                f"feature {idx} ({gdf.iloc[idx].get('name')}) "
                f"does not intersect bbox {bbox}"
            )
            minx, miny, maxx, maxy = geom.bounds
            assert (
                minx >= bbox[0] - eps and miny >= bbox[1] - eps
                and maxx <= bbox[2] + eps and maxy <= bbox[3] + eps
            ), (
                f"feature {idx} ({gdf.iloc[idx].get('name')}) bounds "
                f"{geom.bounds} spill outside bbox {bbox} — clip failed"
            )

        # Geographic-correctness check: the major routes through Fort Myers
        # are I-75 (motorway) and the Tamiami Trail / US-41 (primary). At
        # least one of these named roads should appear in the result.
        names = [str(n) for n in gdf["name"].tolist() if n]
        names_lower = " ".join(names).lower()
        assert any(
            marker in names_lower
            for marker in ("i 75", "i-75", "interstate 75", "tamiami", "us 41", "us-41")
        ), (
            f"Expected I-75 or US-41/Tamiami in road names; got: {names!r}"
        )

        # Highway tag set is restricted to the requested filter — Overpass
        # honored the regex (geographic-correctness for the filter dimension).
        highways = set(gdf["highway"].dropna().unique().tolist())
        assert highways.issubset({"primary", "motorway"}), (
            f"Expected only primary/motorway features; got highway tags: {highways}"
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
