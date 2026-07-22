"""Unit tests for ``cut_features_with_polygon`` (DigitizingTools DtCutWithPolygon).

All synthetic, no network/LLM; the cache shim is routed to an in-memory boto3
double.

Coverage:
- Registration + metadata.
- A cutter that overlaps half of a target square -> the cut area equals the
  un-overlapped half; target attributes preserved.
- A target fully inside the cutter -> dropped when delete_emptied=True; kept
  (empty geometry) when False.
- A target untouched by the cutter -> geometry unchanged, attributes preserved.
- CRS mismatch: a cutter in EPSG:3857 cuts a target in EPSG:4326 correctly
  (cutter reprojected to the target CRS).
- All features consumed + delete_emptied=True -> ALL_FEATURES_CONSUMED.
- Unknown URIs -> typed errors.
"""

from __future__ import annotations

import os
import tempfile

import geopandas as gpd
import pytest
from shapely.geometry import Polygon

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.cut_features_with_polygon import (
    CutFeaturesError,
    cut_features_with_polygon,
)


# ---------------------------------------------------------------------------
# In-memory S3 double for the cache shim
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
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}}, "GetObject"
            )
        return {"Body": _S3Body(data)}

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        data = Body.read() if hasattr(Body, "read") else Body
        self.store[Key] = data
        self.put_count += 1
        return {}


@pytest.fixture(autouse=True)
def _s3(monkeypatch):
    import boto3

    FakeStorageClient._active = None
    client = FakeStorageClient()
    FakeStorageClient._active = client
    monkeypatch.setattr(boto3, "client", lambda service_name, *a, **k: client)
    try:
        yield client
    finally:
        FakeStorageClient._active = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _square(x0, y0, side=1.0) -> Polygon:
    return Polygon([(x0, y0), (x0 + side, y0), (x0 + side, y0 + side), (x0, y0 + side)])


def _write_polys(path, polys, props=None, crs="EPSG:4326") -> None:
    attrs = {"id": list(range(len(polys)))}
    if props:
        for k in props[0]:
            attrs[k] = [p[k] for p in props]
    gpd.GeoDataFrame(attrs, geometry=list(polys), crs=crs).to_file(
        path, driver="FlatGeobuf", engine="pyogrio"
    )


def _read_result(store, out) -> gpd.GeoDataFrame:
    key = out.uri.split("/", 3)[-1]
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as f:
        f.write(store[key])
        p = f.name
    try:
        return gpd.read_file(p, engine="pyogrio")
    finally:
        os.unlink(p)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cut_features_registered():
    assert "cut_features_with_polygon" in TOOL_REGISTRY
    md = TOOL_REGISTRY["cut_features_with_polygon"].metadata
    assert md.cacheable is True
    assert md.ttl_class == "static-30d"
    assert md.source_class == "cut_features_polygon"


def test_half_overlap_cut_area_and_attrs(_s3):
    with tempfile.TemporaryDirectory() as tmp:
        tgt = os.path.join(tmp, "tgt.fgb")
        cut = os.path.join(tmp, "cut.fgb")
        # Target: unit square [0,1]x[0,1] area 1. Cutter: [0.5,1.5]x[0,1] overlaps
        # the right half (area 0.5). Difference -> left half, area 0.5.
        _write_polys(tgt, [_square(0, 0)], [{"name": "parcel"}])
        _write_polys(cut, [_square(0.5, 0)])
        out = cut_features_with_polygon(tgt, cut)
        gdf = _read_result(_s3.store, out)
        assert len(gdf) == 1
        assert gdf.geometry.iloc[0].area == pytest.approx(0.5, rel=1e-6)
        assert gdf.iloc[0]["name"] == "parcel"  # attribute preserved in place


def test_fully_consumed_dropped_when_delete_emptied(_s3):
    with tempfile.TemporaryDirectory() as tmp:
        tgt = os.path.join(tmp, "tgt.fgb")
        cut = os.path.join(tmp, "cut.fgb")
        # Target small square fully inside a big cutter -> emptied.
        _write_polys(tgt, [_square(0, 0), _square(5, 5)], [{"n": "a"}, {"n": "b"}])
        _write_polys(cut, [_square(-1, -1, side=3.0)])  # covers the (0,0) square only
        out = cut_features_with_polygon(tgt, cut, delete_emptied=True)
        gdf = _read_result(_s3.store, out)
        # One target (covering 0,0) is consumed and dropped; the (5,5) survives.
        assert len(gdf) == 1
        assert set(gdf["n"]) == {"b"}


def test_fully_consumed_kept_empty_when_not_delete(_s3):
    with tempfile.TemporaryDirectory() as tmp:
        tgt = os.path.join(tmp, "tgt.fgb")
        cut = os.path.join(tmp, "cut.fgb")
        _write_polys(tgt, [_square(0, 0)], [{"n": "a"}])
        _write_polys(cut, [_square(-1, -1, side=3.0)])
        out = cut_features_with_polygon(tgt, cut, delete_emptied=False)
        gdf = _read_result(_s3.store, out)
        # Kept with an empty geometry -- honest, not fabricated.
        assert len(gdf) == 1
        g = gdf.geometry.iloc[0]
        assert g is None or g.is_empty


def test_untouched_feature_unchanged(_s3):
    with tempfile.TemporaryDirectory() as tmp:
        tgt = os.path.join(tmp, "tgt.fgb")
        cut = os.path.join(tmp, "cut.fgb")
        _write_polys(tgt, [_square(0, 0)], [{"n": "a"}])
        _write_polys(cut, [_square(10, 10)])  # far away -> no intersection
        out = cut_features_with_polygon(tgt, cut)
        gdf = _read_result(_s3.store, out)
        assert len(gdf) == 1
        assert gdf.geometry.iloc[0].area == pytest.approx(1.0, rel=1e-6)


def test_crs_mismatch_cutter_reprojected(_s3):
    with tempfile.TemporaryDirectory() as tmp:
        tgt = os.path.join(tmp, "tgt.fgb")
        cut = os.path.join(tmp, "cut.fgb")
        # Target in EPSG:4326 near the equator; build the same-overlap cutter in
        # EPSG:3857 by reprojecting a 4326 cutter. The tool must reproject it
        # back to 4326 to cut correctly.
        _write_polys(tgt, [_square(0.0, 0.0, side=0.01)], [{"n": "a"}], crs="EPSG:4326")
        cutter_4326 = gpd.GeoDataFrame(
            {"id": [0]}, geometry=[_square(0.005, 0.0, side=0.01)], crs="EPSG:4326"
        )
        cutter_4326.to_crs("EPSG:3857").to_file(cut, driver="FlatGeobuf", engine="pyogrio")
        out = cut_features_with_polygon(tgt, cut)
        gdf = _read_result(_s3.store, out)
        assert len(gdf) == 1
        full = _square(0.0, 0.0, side=0.01).area
        # Right half removed -> roughly half the original area survives.
        assert gdf.geometry.iloc[0].area == pytest.approx(full / 2, rel=1e-2)


def test_all_consumed_raises(_s3):
    with tempfile.TemporaryDirectory() as tmp:
        tgt = os.path.join(tmp, "tgt.fgb")
        cut = os.path.join(tmp, "cut.fgb")
        _write_polys(tgt, [_square(0, 0)], [{"n": "a"}])
        _write_polys(cut, [_square(-1, -1, side=3.0)])
        with pytest.raises(CutFeaturesError) as exc:
            cut_features_with_polygon(tgt, cut, delete_emptied=True)
        assert exc.value.error_code == "ALL_FEATURES_CONSUMED"


def test_unknown_target_uri_raises():
    with tempfile.TemporaryDirectory() as tmp:
        cut = os.path.join(tmp, "cut.fgb")
        _write_polys(cut, [_square(0, 0)])
        with pytest.raises(CutFeaturesError) as exc:
            cut_features_with_polygon("/no/such/target.fgb", cut)
        assert exc.value.error_code == "UNKNOWN_TARGET_URI"


def test_cache_miss_then_hit(_s3):
    with tempfile.TemporaryDirectory() as tmp:
        tgt = os.path.join(tmp, "tgt.fgb")
        cut = os.path.join(tmp, "cut.fgb")
        _write_polys(tgt, [_square(0, 0)], [{"n": "a"}])
        _write_polys(cut, [_square(0.5, 0)])
        cut_features_with_polygon(tgt, cut)
        n = _s3.put_count
        assert n >= 1
        cut_features_with_polygon(tgt, cut)
        assert _s3.put_count == n
