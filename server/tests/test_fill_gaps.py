"""Unit tests for ``fill_gaps`` (DigitizingTools DtFillGap reimplementation).

All synthetic, no network/LLM; the cache shim is routed to an in-memory boto3
double.

Coverage:
- Registration + metadata.
- A ring of 8 unit squares around a missing center cell -> ONE gap polygon of
  area 1 (the enclosed center void = the union's interior ring).
- No enclosed gap (two disjoint, gap-free squares) -> NO_GAPS_FOUND.
- max_gap_area filters out gaps larger than the cap.
- Non-polygon input (points) -> NOT_POLYGONS.
- Multi-layer union: two layers that together enclose a void -> the gap is found.
- Unknown URI -> typed error.
"""

from __future__ import annotations

import os
import tempfile

import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.processing.fill_gaps import FillGapsError, fill_gaps


# ---------------------------------------------------------------------------
# In-memory S3 double
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


def _cell(cx, cy, side=1.0) -> Polygon:
    """Unit cell with lower-left corner (cx, cy)."""
    return Polygon([(cx, cy), (cx + side, cy), (cx + side, cy + side), (cx, cy + side)])


def _ring_of_8_cells() -> list[Polygon]:
    """3x3 grid of unit cells with the center (1,1) cell MISSING.

    The union of the 8 surrounding cells encloses the center 1x1 void -> exactly
    one interior ring (gap) of area 1.
    """
    cells = []
    for ix in range(3):
        for iy in range(3):
            if ix == 1 and iy == 1:
                continue  # the missing center -> the gap
            cells.append(_cell(ix, iy))
    return cells


def _write_polys(path, polys, crs="EPSG:4326") -> None:
    gpd.GeoDataFrame(
        {"id": list(range(len(polys)))}, geometry=list(polys), crs=crs
    ).to_file(path, driver="FlatGeobuf", engine="pyogrio")


def _write_points(path, pts, crs="EPSG:4326") -> None:
    gpd.GeoDataFrame(
        {"id": list(range(len(pts)))},
        geometry=[Point(*p) for p in pts],
        crs=crs,
    ).to_file(path, driver="FlatGeobuf", engine="pyogrio")


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


def test_fill_gaps_registered():
    assert "fill_gaps" in TOOL_REGISTRY
    md = TOOL_REGISTRY["fill_gaps"].metadata
    assert md.cacheable is True
    assert md.ttl_class == "static-30d"
    assert md.source_class == "fill_gaps"


def test_enclosed_center_void_emitted_as_one_gap(_s3):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cells.fgb")
        _write_polys(path, _ring_of_8_cells())
        out = fill_gaps(path)
        gdf = _read_result(_s3.store, out)
        assert len(gdf) == 1
        gap = gdf.geometry.iloc[0]
        assert gap.area == pytest.approx(1.0, rel=1e-6)
        # The gap is the center cell [1,2]x[1,2].
        assert gap.bounds == pytest.approx((1.0, 1.0, 2.0, 2.0), abs=1e-6)
        assert "gap_area" in gdf.columns


def test_no_enclosed_gap_raises(_s3):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "two.fgb")
        # Two disjoint squares -- no interior ring, no gap.
        _write_polys(path, [_cell(0, 0), _cell(5, 5)])
        with pytest.raises(FillGapsError) as exc:
            fill_gaps(path)
        assert exc.value.error_code == "NO_GAPS_FOUND"


def test_max_gap_area_filters_large_gaps(_s3):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cells.fgb")
        _write_polys(path, _ring_of_8_cells())
        # The only gap is area 1.0; a cap below that filters it out -> NO_GAPS_FOUND.
        with pytest.raises(FillGapsError) as exc:
            fill_gaps(path, max_gap_area=0.5)
        assert exc.value.error_code == "NO_GAPS_FOUND"
        # A cap above 1.0 keeps it.
        out = fill_gaps(path, max_gap_area=2.0)
        gdf = _read_result(_s3.store, out)
        assert len(gdf) == 1


def test_points_input_raises_not_polygons(_s3):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "pts.fgb")
        _write_points(path, [(0, 0), (1, 1)])
        with pytest.raises(FillGapsError) as exc:
            fill_gaps(path)
        assert exc.value.error_code == "NOT_POLYGONS"


def test_multi_layer_union_finds_gap(_s3):
    with tempfile.TemporaryDirectory() as tmp:
        a = os.path.join(tmp, "a.fgb")
        b = os.path.join(tmp, "b.fgb")
        cells = _ring_of_8_cells()
        # Split the 8 surrounding cells across two layers; together they enclose
        # the center void, individually they do not.
        _write_polys(a, cells[:4])
        _write_polys(b, cells[4:])
        out = fill_gaps(a, extra_layer_uris=[b])
        gdf = _read_result(_s3.store, out)
        assert len(gdf) == 1
        assert gdf.geometry.iloc[0].area == pytest.approx(1.0, rel=1e-6)


def test_unknown_uri_raises():
    with pytest.raises(FillGapsError) as exc:
        fill_gaps("/no/such/file.fgb")
    assert exc.value.error_code == "UNKNOWN_VECTOR_URI"


def test_cache_miss_then_hit(_s3):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cells.fgb")
        _write_polys(path, _ring_of_8_cells())
        fill_gaps(path)
        n = _s3.put_count
        assert n >= 1
        fill_gaps(path)
        assert _s3.put_count == n
