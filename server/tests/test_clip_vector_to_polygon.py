"""Unit + live tests for ``clip_vector_to_polygon`` atomic tool (job-0107).

Coverage:
1. ``test_clip_vector_to_polygon_registered`` — tool appears in TOOL_REGISTRY
   with correct metadata (cacheable=True, ttl_class="static-30d",
   source_class="clip_vector_polygon").
2. ``test_points_within_polygon_retained_outside_discarded`` — synthetic point
   layer + polygon mask → only points inside the polygon survive.
3. ``test_polygons_partial_overlap_keep_partial_True_kept`` — overlapping
   polygon kept when keep_partial=True; discarded when False.
4. ``test_lines_crossing_polygon_boundary_keep_partial_behavior`` — line
   crossing mask boundary: kept (intersects) when keep_partial=True; dropped
   (within) when False.
5. ``test_feature_filter_on_multi_feature_polygon`` — multi-polygon source +
   STUSPS-style filter → only matching polygon used as mask.
6. ``test_cache_miss_writes_and_hit_skips_recompute`` — first call (miss) runs
   the clip; second call with same args (hit) does NOT.
7. ``test_unknown_vector_uri_raises_typed_error`` — non-gs:// non-file URI raises
   ClipVectorError with error_code="UNKNOWN_VECTOR_URI".
8. ``test_cache_keys_vary_across_params`` — cache keys differ across the four
   parameter combinations.
9. ``test_polygon_filter_empty_raises`` — filter matching zero features raises
   ClipVectorError(POLYGON_FILTER_EMPTY).
10. Live (env TRID3NT_TEST_LIVE_CLIPV=1): clip nationwide GBIF panther occurrences
    to TIGER FL state polygon → fewer features than input AND all output
    points fall inside FL's bbox (geographic-correctness gate).
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import geopandas as gpd
import pytest
from shapely.geometry import LineString, Point, Polygon

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing.clip_vector_to_polygon import (
    ClipVectorError,
    clip_vector_to_polygon,
)

# ---------------------------------------------------------------------------
# Pinned timestamp for deterministic cache keys
# ---------------------------------------------------------------------------

PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# FakeBlob / FakeStorageClient for cache shim isolation
# ---------------------------------------------------------------------------


class _S3Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeStorageClient:
    """In-memory S3 double (GCP decommissioned). ``store`` keyed by object KEY.

    Returns the per-test active instance installed by the autouse
    ``_route_cache_to_inmemory_s3`` fixture so the tool's real S3 read-through
    (boto3) reads/writes the same store the test inspects.
    """

    _active: "FakeStorageClient | None" = None

    def __new__(cls) -> "FakeStorageClient":
        if cls._active is not None:
            return cls._active
        return super().__new__(cls)

    def __init__(self) -> None:
        if getattr(self, "_init", False):
            return
        self._init = True
        self.store: dict[str, bytes] = {}
        self.last_put: dict | None = None

    def get_object(self, *, Bucket, Key):
        from botocore.exceptions import ClientError

        try:
            data = self.store[Key]
        except KeyError:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            )
        return {"Body": _S3Body(data)}

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        data = Body.read() if hasattr(Body, "read") else Body
        self.store[Key] = data
        self.last_put = {"Bucket": Bucket, "Key": Key, "ContentType": ContentType}
        return {}


@pytest.fixture(autouse=True)
def _route_cache_to_inmemory_s3(monkeypatch):
    """Route boto3 S3 (the cache shim's only object store) to an in-memory double."""
    import boto3

    FakeStorageClient._active = None
    client = FakeStorageClient()
    FakeStorageClient._active = client

    def _factory(service_name, *a, **k):
        assert service_name == "s3"
        return client

    monkeypatch.setattr(boto3, "client", _factory)
    try:
        yield client
    finally:
        FakeStorageClient._active = None


# ---------------------------------------------------------------------------
# Helpers: synthetic vector + polygon creation
# ---------------------------------------------------------------------------


def _write_points_fgb(path: str, coords: list[tuple[float, float]], crs: str = "EPSG:4326") -> None:
    gdf = gpd.GeoDataFrame(
        {"id": list(range(len(coords)))},
        geometry=[Point(x, y) for x, y in coords],
        crs=crs,
    )
    gdf.to_file(path, driver="FlatGeobuf", engine="pyogrio")


def _write_lines_fgb(path: str, lines: list[list[tuple[float, float]]], crs: str = "EPSG:4326") -> None:
    gdf = gpd.GeoDataFrame(
        {"id": list(range(len(lines)))},
        geometry=[LineString(coords) for coords in lines],
        crs=crs,
    )
    gdf.to_file(path, driver="FlatGeobuf", engine="pyogrio")


def _write_polygons_fgb(
    path: str,
    polys: list[list[tuple[float, float]]],
    properties: list[dict] | None = None,
    crs: str = "EPSG:4326",
) -> None:
    geoms = [Polygon(coords) for coords in polys]
    attrs: dict = {"id": list(range(len(polys)))}
    if properties:
        for k in properties[0]:
            attrs[k] = [p[k] for p in properties]
    gdf = gpd.GeoDataFrame(attrs, geometry=geoms, crs=crs)
    gdf.to_file(path, driver="FlatGeobuf", engine="pyogrio")


# ---------------------------------------------------------------------------
# Test 1 — registration check
# ---------------------------------------------------------------------------


def test_clip_vector_to_polygon_registered():
    """clip_vector_to_polygon is in TOOL_REGISTRY with expected metadata."""
    assert "clip_vector_to_polygon" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["clip_vector_to_polygon"]
    assert entry.metadata.cacheable is True
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "clip_vector_polygon"


# ---------------------------------------------------------------------------
# Test 2 — points: inside kept, outside discarded
# ---------------------------------------------------------------------------


def test_points_within_polygon_retained_outside_discarded():
    """Synthetic point layer + square polygon mask → inside-points only."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Vector: 4 points — 2 inside the unit square [0,0]-[1,1], 2 outside.
        vec_path = os.path.join(tmpdir, "points.fgb")
        _write_points_fgb(
            vec_path,
            coords=[
                (0.3, 0.3),   # inside
                (0.7, 0.7),   # inside
                (2.0, 2.0),   # outside
                (-1.0, -1.0), # outside
            ],
        )

        # Polygon: unit square.
        poly_path = os.path.join(tmpdir, "mask.fgb")
        _write_polygons_fgb(
            poly_path,
            polys=[[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]],
        )

        fake_sc = FakeStorageClient()
        result = clip_vector_to_polygon(
            vector_uri=vec_path,
            polygon_uri=poly_path,
            feature_filter=None,
            keep_partial=True,
            _bucket="test-bucket",
        )

        # Read the cached output back.
        assert len(fake_sc.store) == 1
        clip_bytes = list(fake_sc.store.values())[0]
        out_path = os.path.join(tmpdir, "out.fgb")
        with open(out_path, "wb") as f:
            f.write(clip_bytes)
        out_gdf = gpd.read_file(out_path, engine="pyogrio")

        assert len(out_gdf) == 2, f"Expected 2 inside-points, got {len(out_gdf)}"
        # Every surviving point is inside [0,1]x[0,1].
        for geom in out_gdf.geometry:
            assert 0 <= geom.x <= 1 and 0 <= geom.y <= 1, (
                f"Point {geom} should be inside the mask"
            )

        assert result.layer_type == "vector"
        assert result.role == "context"
        assert result.uri.startswith("s3://")
        assert "clip_vector_polygon" in result.uri


# ---------------------------------------------------------------------------
# Test 3 — polygons: partial overlap behavior
# ---------------------------------------------------------------------------


def test_polygons_partial_overlap_keep_partial_True_kept_False_discarded():
    """Polygon that partially overlaps the mask: kept w/ keep_partial=True, dropped w/ False."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Vector: 3 polygons:
        # - A: entirely inside mask [0,1]x[0,1]
        # - B: partially overlaps mask (centered at (1.0, 0.5), spans [0.5,1.5]x[0,1])
        # - C: entirely outside mask (centered at (5,5), spans [4.5,5.5]x[4.5,5.5])
        vec_path = os.path.join(tmpdir, "polys.fgb")
        _write_polygons_fgb(
            vec_path,
            polys=[
                # A: small square inside [0.1,0.4]x[0.1,0.4]
                [(0.1, 0.1), (0.4, 0.1), (0.4, 0.4), (0.1, 0.4), (0.1, 0.1)],
                # B: spans the right edge of the mask
                [(0.5, 0.2), (1.5, 0.2), (1.5, 0.8), (0.5, 0.8), (0.5, 0.2)],
                # C: far outside
                [(4.5, 4.5), (5.5, 4.5), (5.5, 5.5), (4.5, 5.5), (4.5, 4.5)],
            ],
            properties=[{"label": "A"}, {"label": "B"}, {"label": "C"}],
        )

        # Mask: unit square.
        poly_path = os.path.join(tmpdir, "mask.fgb")
        _write_polygons_fgb(
            poly_path,
            polys=[[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]],
        )

        # keep_partial=True → expect A + B (2 features).
        fake_sc_True = FakeStorageClient()
        clip_vector_to_polygon(
            vector_uri=vec_path,
            polygon_uri=poly_path,
            feature_filter=None,
            keep_partial=True,
            _bucket="test-bucket",
        )
        clip_bytes_True = list(fake_sc_True.store.values())[0]
        out_path_True = os.path.join(tmpdir, "out_True.fgb")
        with open(out_path_True, "wb") as f:
            f.write(clip_bytes_True)
        gdf_True = gpd.read_file(out_path_True, engine="pyogrio")

        labels_True = sorted(gdf_True["label"].tolist())
        assert labels_True == ["A", "B"], (
            f"keep_partial=True expected A+B, got {labels_True}"
        )

        # keep_partial=False → only A (fully contained).
        fake_sc_False = FakeStorageClient()
        fake_sc_False.store.clear()  # isolate this call's write in the shared S3 store
        clip_vector_to_polygon(
            vector_uri=vec_path,
            polygon_uri=poly_path,
            feature_filter=None,
            keep_partial=False,
            _bucket="test-bucket",
        )
        clip_bytes_False = list(fake_sc_False.store.values())[0]
        out_path_False = os.path.join(tmpdir, "out_False.fgb")
        with open(out_path_False, "wb") as f:
            f.write(clip_bytes_False)
        gdf_False = gpd.read_file(out_path_False, engine="pyogrio")

        labels_False = sorted(gdf_False["label"].tolist())
        assert labels_False == ["A"], (
            f"keep_partial=False expected only A, got {labels_False}"
        )


# ---------------------------------------------------------------------------
# Test 4 — lines crossing the boundary
# ---------------------------------------------------------------------------


def test_lines_crossing_polygon_boundary_keep_partial_behavior():
    """Line crossing the mask boundary: kept on keep_partial=True, dropped on False."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Lines:
        # - L1: entirely inside mask [0,1]x[0,1]
        # - L2: crosses the right edge — from (0.5,0.5) to (1.5,0.5)
        # - L3: entirely outside
        vec_path = os.path.join(tmpdir, "lines.fgb")
        _write_lines_fgb(
            vec_path,
            lines=[
                [(0.2, 0.2), (0.8, 0.8)],         # L1 inside
                [(0.5, 0.5), (1.5, 0.5)],         # L2 crosses east edge
                [(4.0, 4.0), (5.0, 5.0)],         # L3 outside
            ],
        )

        poly_path = os.path.join(tmpdir, "mask.fgb")
        _write_polygons_fgb(
            poly_path,
            polys=[[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]],
        )

        # keep_partial=True → L1 + L2.
        fake_sc = FakeStorageClient()
        clip_vector_to_polygon(
            vector_uri=vec_path,
            polygon_uri=poly_path,
            keep_partial=True,
            _bucket="test-bucket",
        )
        clip_bytes = list(fake_sc.store.values())[0]
        out_path = os.path.join(tmpdir, "out_True.fgb")
        with open(out_path, "wb") as f:
            f.write(clip_bytes)
        gdf_True = gpd.read_file(out_path, engine="pyogrio")
        assert len(gdf_True) == 2, (
            f"keep_partial=True expected 2 lines (L1+L2), got {len(gdf_True)}"
        )

        # keep_partial=False → only L1 (fully within).
        fake_sc2 = FakeStorageClient()
        fake_sc2.store.clear()  # isolate this call's write in the shared S3 store
        clip_vector_to_polygon(
            vector_uri=vec_path,
            polygon_uri=poly_path,
            keep_partial=False,
            _bucket="test-bucket",
        )
        clip_bytes2 = list(fake_sc2.store.values())[0]
        out_path2 = os.path.join(tmpdir, "out_False.fgb")
        with open(out_path2, "wb") as f:
            f.write(clip_bytes2)
        gdf_False = gpd.read_file(out_path2, engine="pyogrio")
        assert len(gdf_False) == 1, (
            f"keep_partial=False expected 1 line (L1), got {len(gdf_False)}"
        )


# ---------------------------------------------------------------------------
# Test 5 — feature_filter on multi-feature polygon
# ---------------------------------------------------------------------------


def test_feature_filter_on_multi_feature_polygon():
    """Multi-feature polygon source + filter → only filter-matching polygon used as mask."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Points: 3 points, one in each of three potential mask squares.
        vec_path = os.path.join(tmpdir, "points.fgb")
        _write_points_fgb(
            vec_path,
            coords=[
                (0.5, 0.5),   # inside mask "A"
                (2.5, 0.5),   # inside mask "B"
                (4.5, 0.5),   # inside mask "C"
            ],
        )

        # Polygon source: 3 distinct squares with STUSPS-style labels.
        poly_path = os.path.join(tmpdir, "masks.fgb")
        _write_polygons_fgb(
            poly_path,
            polys=[
                [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)],
                [(2, 0), (3, 0), (3, 1), (2, 1), (2, 0)],
                [(4, 0), (5, 0), (5, 1), (4, 1), (4, 0)],
            ],
            properties=[
                {"STUSPS": "A"},
                {"STUSPS": "B"},
                {"STUSPS": "C"},
            ],
        )

        fake_sc = FakeStorageClient()
        clip_vector_to_polygon(
            vector_uri=vec_path,
            polygon_uri=poly_path,
            feature_filter={"STUSPS": "B"},
            keep_partial=True,
            _bucket="test-bucket",
        )

        clip_bytes = list(fake_sc.store.values())[0]
        out_path = os.path.join(tmpdir, "out.fgb")
        with open(out_path, "wb") as f:
            f.write(clip_bytes)
        out_gdf = gpd.read_file(out_path, engine="pyogrio")

        # Only the B-mask point survives.
        assert len(out_gdf) == 1
        point = out_gdf.geometry.iloc[0]
        assert 2.0 <= point.x <= 3.0, f"Expected point in B's square (x∈[2,3]); got {point}"


# ---------------------------------------------------------------------------
# Test 6 — cache miss/hit
# ---------------------------------------------------------------------------


def test_cache_miss_writes_and_hit_skips_recompute():
    """First call invokes the clip pipeline; second call (same args) hits cache."""
    with tempfile.TemporaryDirectory() as tmpdir:
        vec_path = os.path.join(tmpdir, "points.fgb")
        _write_points_fgb(vec_path, coords=[(0.5, 0.5), (2.0, 2.0)])

        poly_path = os.path.join(tmpdir, "mask.fgb")
        _write_polygons_fgb(
            poly_path, polys=[[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]]
        )

        fake_sc = FakeStorageClient()

        call_count = [0]
        from trid3nt_server.tools.processing import clip_vector_to_polygon as cvp_mod

        original_clip = cvp_mod._clip_vector_locally

        def _counting(*args, **kwargs):
            call_count[0] += 1
            return original_clip(*args, **kwargs)

        with patch(
            "trid3nt_server.tools.processing.clip_vector_to_polygon._clip_vector_locally",
            side_effect=_counting,
        ):
            result1 = clip_vector_to_polygon(
                vector_uri=vec_path,
                polygon_uri=poly_path,
                feature_filter=None,
                keep_partial=True,
                _bucket="test-bucket",
            )
            assert call_count[0] == 1, "Expected clip pipeline to run once (miss)"

            result2 = clip_vector_to_polygon(
                vector_uri=vec_path,
                polygon_uri=poly_path,
                feature_filter=None,
                keep_partial=True,
                _bucket="test-bucket",
            )
            assert call_count[0] == 1, (
                f"Expected clip pipeline NOT to run on cache hit; ran {call_count[0]} times"
            )

        assert result1.uri == result2.uri


# ---------------------------------------------------------------------------
# Test 7 — unknown vector URI → typed error
# ---------------------------------------------------------------------------


def test_unknown_vector_uri_raises_typed_error():
    """Non-gs:// non-file vector URI → ClipVectorError(UNKNOWN_VECTOR_URI)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        poly_path = os.path.join(tmpdir, "mask.fgb")
        _write_polygons_fgb(
            poly_path, polys=[[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]]
        )

        fake_sc = FakeStorageClient()
        with pytest.raises(ClipVectorError) as exc_info:
            clip_vector_to_polygon(
                vector_uri="/nonexistent/path/vector.fgb",
                polygon_uri=poly_path,
                feature_filter=None,
                keep_partial=True,
                _bucket="test-bucket",
            )
        assert exc_info.value.error_code == "UNKNOWN_VECTOR_URI"


# ---------------------------------------------------------------------------
# Test 8 — cache keys vary across params
# ---------------------------------------------------------------------------


def test_cache_keys_vary_across_params():
    """Cache keys differ for distinct (vector_uri, polygon_uri, feature_filter, keep_partial)."""
    from trid3nt_server.tools.cache import compute_cache_key

    base = {
        "vector_uri": "gs://b/v.fgb",
        "polygon_uri": "gs://b/p.fgb",
        "feature_filter": None,
        "keep_partial": True,
    }
    combos = [
        base,
        {**base, "vector_uri": "gs://b/v2.fgb"},
        {**base, "polygon_uri": "gs://b/p2.fgb"},
        {**base, "feature_filter": {"STUSPS": "FL"}},
        {**base, "keep_partial": False},
    ]
    keys = set()
    for params in combos:
        key = compute_cache_key("clip_vector_polygon", params, "static-30d", now=PINNED_NOW)
        keys.add(key)
    assert len(keys) == 5, f"Expected 5 distinct cache keys; got {len(keys)}: {keys}"


# ---------------------------------------------------------------------------
# Test 9 — POLYGON_FILTER_EMPTY when filter matches nothing
# ---------------------------------------------------------------------------


def test_polygon_filter_empty_raises():
    """feature_filter that matches zero polygons → POLYGON_FILTER_EMPTY."""
    with tempfile.TemporaryDirectory() as tmpdir:
        vec_path = os.path.join(tmpdir, "points.fgb")
        _write_points_fgb(vec_path, coords=[(0.5, 0.5)])

        poly_path = os.path.join(tmpdir, "masks.fgb")
        _write_polygons_fgb(
            poly_path,
            polys=[[(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)]],
            properties=[{"STUSPS": "FL"}],
        )

        fake_sc = FakeStorageClient()
        with pytest.raises(ClipVectorError) as exc_info:
            clip_vector_to_polygon(
                vector_uri=vec_path,
                polygon_uri=poly_path,
                feature_filter={"STUSPS": "XX"},
                _bucket="test-bucket",
            )
        assert exc_info.value.error_code == "POLYGON_FILTER_EMPTY"


# ---------------------------------------------------------------------------
# Test 10 — LIVE: clip nationwide GBIF panther points to TIGER FL polygon.
# Geographic-correctness gate (job-0086 lesson): emitted points must fall
# inside FL's actual bbox, not just round-trip.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("TRID3NT_TEST_LIVE_CLIPV") != "1",
    reason="live test gated by TRID3NT_TEST_LIVE_CLIPV=1",
)
def test_live_clip_gbif_panther_to_florida():
    """Live test: GBIF Puma concolor occurrences (US bbox) clipped to TIGER FL polygon.

    Geographic-correctness gate: every output point must fall inside Florida's
    actual bbox (-87.6, 24.4, -80.0, 31.0). Input is a wider US-east-coast bbox;
    output count < input count; all output points are inside FL.
    """
    from trid3nt_server.tools.fetchers.socioeconomic.fetch_administrative_boundaries import (
        fetch_administrative_boundaries,
    )
    from trid3nt_server.tools.fetchers.biodiversity.fetch_gbif_occurrences import fetch_gbif_occurrences

    # 1. Fetch TIGER state polygons covering FL+GA+AL (so we can filter to FL).
    states_uri = fetch_administrative_boundaries(
        level="state",
        bbox=(-87.6, 24.4, -80.0, 31.5),
    )

    # 2. Fetch broadly-distributed species (Odocoileus virginianus — white-tailed
    # deer) occurrences over a broader bbox spanning FL + GA + AL + SC. Use a
    # widely-distributed species so cross-state points exist in the unclipped
    # input but only FL points survive the clip.
    panther_uri = fetch_gbif_occurrences(
        species_key="Odocoileus virginianus",
        bbox=(-90.0, 24.0, -78.0, 36.0),
        max_records=500,
    )

    # 3. Clip panther points to FL polygon.
    result = clip_vector_to_polygon(
        vector_uri=panther_uri.uri,
        polygon_uri=states_uri.uri,
        feature_filter={"STUSPS": "FL"},
        keep_partial=True,
    )

    # 4. Download and inspect.
    from google.cloud import storage  # type: ignore[import-not-found]

    rest = result.uri[len("gs://"):]
    bucket_name, _, blob_path = rest.partition("/")
    sc = storage.Client()
    blob = sc.bucket(bucket_name).blob(blob_path)

    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tmp:
        tmp.write(blob.download_as_bytes())
        out_path = tmp.name

    try:
        out_gdf = gpd.read_file(out_path, engine="pyogrio")
    finally:
        os.unlink(out_path)

    # Input panther layer for comparison.
    rest_in = panther_uri.uri[len("gs://"):]
    bucket_in, _, blob_in_path = rest_in.partition("/")
    in_blob = sc.bucket(bucket_in).blob(blob_in_path)
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tmp:
        tmp.write(in_blob.download_as_bytes())
        in_path = tmp.name
    try:
        in_gdf = gpd.read_file(in_path, engine="pyogrio")
    finally:
        os.unlink(in_path)

    # Geographic-correctness checks:
    # a) Output count < input count.
    assert len(out_gdf) < len(in_gdf), (
        f"Expected fewer features after clip-to-FL: in={len(in_gdf)} out={len(out_gdf)}"
    )
    # b) Every output point inside FL bbox (Florida actual envelope).
    fl_bbox = (-87.6, 24.4, -80.0, 31.0)
    for geom in out_gdf.geometry:
        x, y = geom.x, geom.y
        assert fl_bbox[0] <= x <= fl_bbox[2] and fl_bbox[1] <= y <= fl_bbox[3], (
            f"Output point {(x, y)} falls outside FL bbox {fl_bbox}"
        )

    print(
        f"LIVE clip: {len(in_gdf)} input panther points → "
        f"{len(out_gdf)} after clip-to-FL"
    )
