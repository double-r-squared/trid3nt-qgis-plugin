"""Unit tests for ``merge_features`` (QGIS DigitizingTools DtMerge reimplementation).

All tests use synthetic in-memory geometries -- no network, no LLM, no live S3
(the cache shim is routed to an in-memory boto3 double).

Coverage:
- Registration + metadata (cacheable / ttl_class / source_class).
- Two adjacent squares merged -> ONE feature whose area == the union area; the
  output geometry is MultiPolygon (promoted).
- keep_id selects which feature's ATTRIBUTES survive.
- feature_ids=None merges ALL features.
- Disjoint geometries union into a multi-part feature (both parts retained).
- Out-of-range feature_ids / keep_id -> INVALID_FEATURE_IDS.
- Cache miss writes, hit skips recompute (same args).
- Unknown URI -> UNKNOWN_VECTOR_URI.
"""

from __future__ import annotations

import os
import tempfile

import geopandas as gpd
import pytest
from shapely.geometry import Polygon

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.merge_features import MergeFeaturesError, merge_features


# ---------------------------------------------------------------------------
# In-memory S3 double for the cache shim (mirrors test_clip_vector_to_polygon)
# ---------------------------------------------------------------------------


class _S3Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeStorageClient:
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
        self.put_count = 0

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
        self.put_count += 1
        return {}


@pytest.fixture(autouse=True)
def _route_cache_to_inmemory_s3(monkeypatch):
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
# Synthetic-fixture helpers
# ---------------------------------------------------------------------------


def _square(x0: float, y0: float, side: float = 1.0) -> Polygon:
    return Polygon(
        [(x0, y0), (x0 + side, y0), (x0 + side, y0 + side), (x0, y0 + side)]
    )


def _write_polys_fgb(path: str, polys, props, crs: str = "EPSG:4326") -> None:
    attrs = {k: [p[k] for p in props] for k in props[0]}
    gdf = gpd.GeoDataFrame(attrs, geometry=list(polys), crs=crs)
    gdf.to_file(path, driver="FlatGeobuf", engine="pyogrio")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_merge_features_registered():
    assert "merge_features" in TOOL_REGISTRY
    md = TOOL_REGISTRY["merge_features"].metadata
    assert md.cacheable is True
    assert md.ttl_class == "static-30d"
    assert md.source_class == "merge_features"


def test_two_adjacent_squares_area_and_count(_route_cache_to_inmemory_s3):
    # Read the written bytes straight from the in-memory store to assert the result.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "in.fgb")
        _write_polys_fgb(
            path,
            [_square(0, 0), _square(1, 0)],
            [{"name": "left"}, {"name": "right"}],
        )
        out = merge_features(path)
        # Pull the FlatGeobuf bytes back out of the fake S3 store.
        key = out.uri.split("/", 3)[-1]
        data = _route_cache_to_inmemory_s3.store[key]
        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
            f.write(data)
            res_path = f.name
        try:
            gdf = gpd.read_file(res_path, engine="pyogrio")
        finally:
            os.unlink(res_path)
        assert len(gdf) == 1
        assert gdf.geometry.iloc[0].geom_type == "MultiPolygon"
        assert gdf.geometry.iloc[0].area == pytest.approx(2.0, rel=1e-6)
        # The keeper attribute is one of the two input names (default = first
        # feature as read; FlatGeobuf may reorder, so accept either).
        assert gdf.iloc[0]["name"] in {"left", "right"}


def _read_result(store, out) -> gpd.GeoDataFrame:
    key = out.uri.split("/", 3)[-1]
    data = store[key]
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(data)
        res_path = f.name
    try:
        return gpd.read_file(res_path, engine="pyogrio")
    finally:
        os.unlink(res_path)


def test_keep_id_selects_surviving_attributes(_route_cache_to_inmemory_s3):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "in.fgb")
        _write_polys_fgb(
            path,
            [_square(0, 0), _square(1, 0)],
            [{"name": "left"}, {"name": "right"}],
        )
        # The tool indexes the layer as the driver reads it; compute the expected
        # keeper name per keep_id from that same read order (robust to FlatGeobuf
        # spatial reordering).
        as_read = gpd.read_file(path, engine="pyogrio")
        expect0 = as_read.iloc[0]["name"]
        expect1 = as_read.iloc[1]["name"]
        assert expect0 != expect1  # the two names are distinct

        out0 = merge_features(path, keep_id=0)
        assert _read_result(_route_cache_to_inmemory_s3.store, out0).iloc[0]["name"] == expect0
        out1 = merge_features(path, keep_id=1)
        assert _read_result(_route_cache_to_inmemory_s3.store, out1).iloc[0]["name"] == expect1


def test_disjoint_geometries_union_into_multipart(_route_cache_to_inmemory_s3):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "in.fgb")
        # Two far-apart squares -> a MultiPolygon with 2 parts, total area 2.
        _write_polys_fgb(
            path,
            [_square(0, 0), _square(10, 10)],
            [{"name": "a"}, {"name": "b"}],
        )
        out = merge_features(path, feature_ids=None)
        key = out.uri.split("/", 3)[-1]
        data = _route_cache_to_inmemory_s3.store[key]
        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
            f.write(data)
            res_path = f.name
        try:
            gdf = gpd.read_file(res_path, engine="pyogrio")
        finally:
            os.unlink(res_path)
        assert len(gdf) == 1
        geom = gdf.geometry.iloc[0]
        assert geom.geom_type == "MultiPolygon"
        assert len(geom.geoms) == 2
        assert geom.area == pytest.approx(2.0, rel=1e-6)


def test_out_of_range_feature_ids_raises():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "in.fgb")
        _write_polys_fgb(path, [_square(0, 0)], [{"name": "x"}])
        with pytest.raises(MergeFeaturesError) as exc:
            merge_features(path, feature_ids=[0, 5])
        assert exc.value.error_code == "INVALID_FEATURE_IDS"


def test_out_of_range_keep_id_raises():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "in.fgb")
        _write_polys_fgb(path, [_square(0, 0)], [{"name": "x"}])
        with pytest.raises(MergeFeaturesError) as exc:
            merge_features(path, keep_id=9)
        assert exc.value.error_code == "INVALID_FEATURE_IDS"


def test_unknown_uri_raises_typed_error():
    with pytest.raises(MergeFeaturesError) as exc:
        merge_features("/no/such/file.fgb")
    assert exc.value.error_code == "UNKNOWN_VECTOR_URI"


def test_cache_miss_writes_then_hit_skips_recompute(_route_cache_to_inmemory_s3):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "in.fgb")
        _write_polys_fgb(
            path,
            [_square(0, 0), _square(1, 0)],
            [{"name": "left"}, {"name": "right"}],
        )
        merge_features(path)
        first_puts = _route_cache_to_inmemory_s3.put_count
        assert first_puts >= 1
        merge_features(path)  # identical args -> cache hit, no new write
        assert _route_cache_to_inmemory_s3.put_count == first_puts
