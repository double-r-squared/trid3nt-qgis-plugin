"""Unit + live tests for ``clip_raster_to_polygon`` atomic tool (job-0106, FR-CE-8, FR-DC).

Coverage:
1. ``test_clip_raster_to_polygon_registered`` — tool appears in TOOL_REGISTRY with
   correct metadata.
2. ``test_clip_with_square_polygon_yields_correct_extent`` — synthetic raster +
   square polygon → output extent matches polygon (geographic-correctness gate).
3. ``test_polygon_crs_mismatch_is_reprojected`` — polygon in EPSG:3857, raster in
   EPSG:4326 → reprojection applied; mask still works and the correct quadrant
   is selected.
4. ``test_feature_filter_selects_one_polygon`` — multi-feature input + filter
   picks one polygon by attribute; other polygon is excluded from mask.
5. ``test_nodata_outside_override`` — nodata_outside=-999 → output's nodata value
   is -999 and outside pixels are -999.
6. ``test_cache_miss_then_hit_skips_mask`` — first call masks; second call hits cache.
7. ``test_empty_filter_raises_typed_error`` — feature_filter matching zero
   features raises POLYGON_FILTER_EMPTY.
8. ``test_unknown_raster_uri_raises_typed_error``
9. ``test_unknown_polygon_uri_raises_typed_error``
10. Live (env GRACE2_TEST_LIVE_CLIP=1): clip a synthetic Fort-Myers-sized DEM to
    a Lee-County-shaped polygon → verify the masked pixels fall inside the polygon
    bounds and at least one pixel was masked.
"""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds

from grace2_agent.tools import TOOL_REGISTRY
from grace2_agent.tools.clip_raster_to_polygon import (
    ClipRasterPolygonError,
    clip_raster_to_polygon,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_synthetic_raster(
    path: str,
    width: int = 256,
    height: int = 256,
    crs: str = "EPSG:4326",
    west: float = -82.0,
    south: float = 26.0,
    east: float = -80.0,
    north: float = 28.0,
    fill_value: float | None = None,
) -> None:
    """Write a synthetic GeoTIFF filled with row*col elevation values (or a constant)."""
    transform = from_bounds(west, south, east, north, width, height)
    if fill_value is not None:
        data = np.full((height, width), fill_value, dtype=np.float32)
    else:
        data = (np.arange(width * height, dtype=np.float32).reshape(height, width))
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": width,
        "height": height,
        "count": 1,
        "crs": crs,
        "transform": transform,
        "nodata": -9999.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def _write_polygon_fgb(
    path: str,
    bbox: tuple[float, float, float, float],
    crs: str = "EPSG:4326",
    attributes: dict[str, str | int] | None = None,
) -> None:
    """Write a single-feature FlatGeobuf containing the rectangle ``bbox`` as a polygon."""
    import geopandas as gpd
    from shapely.geometry import box

    geom = box(*bbox)
    attrs = attributes or {}
    rec = {**attrs, "geometry": geom}
    gdf = gpd.GeoDataFrame([rec], geometry="geometry", crs=crs)
    gdf.to_file(path, driver="FlatGeobuf", engine="pyogrio")


def _write_polygon_fgb_multi(
    path: str,
    polys: list[tuple[tuple[float, float, float, float], dict]],
    crs: str = "EPSG:4326",
) -> None:
    """Write a multi-feature FlatGeobuf — each entry: (bbox, attribute_dict)."""
    import geopandas as gpd
    from shapely.geometry import box

    records = []
    for bb, attrs in polys:
        records.append({**attrs, "geometry": box(*bb)})
    gdf = gpd.GeoDataFrame(records, geometry="geometry", crs=crs)
    gdf.to_file(path, driver="FlatGeobuf", engine="pyogrio")


# ---------------------------------------------------------------------------
# Fake cache shim (mirrors clip_raster_to_bbox tests)
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
# Test 1 — registration check
# ---------------------------------------------------------------------------


def test_clip_raster_to_polygon_registered():
    """``clip_raster_to_polygon`` is in TOOL_REGISTRY with expected metadata."""
    assert "clip_raster_to_polygon" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["clip_raster_to_polygon"]
    assert entry.metadata.cacheable is True
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "clip_raster_polygon"


# ---------------------------------------------------------------------------
# Test 2 — geographic correctness: square polygon yields correct extent
# ---------------------------------------------------------------------------


def test_clip_with_square_polygon_yields_correct_extent(tmp_path):
    """256x256 raster (-82,26,-80,28) + center-quadrant polygon → output extent
    matches polygon bbox; pixels inside polygon retain source values; outside is nodata.

    This is the geographic-correctness gate: we don't just check bytes round-trip,
    we verify the masked output covers the polygon's bbox in EPSG:4326 coords.
    """
    src_path = tmp_path / "src.tif"
    poly_path = tmp_path / "poly.fgb"

    _write_synthetic_raster(
        str(src_path),
        width=256,
        height=256,
        crs="EPSG:4326",
        west=-82.0, south=26.0, east=-80.0, north=28.0,
    )
    poly_bbox = (-81.5, 26.5, -80.5, 27.5)  # center 1°x1° square
    _write_polygon_fgb(str(poly_path), poly_bbox, crs="EPSG:4326")

    fake_sc = FakeStorageClient()
    result = clip_raster_to_polygon(
        raster_uri=str(src_path),
        polygon_uri=str(poly_path),
        _bucket="test-bucket",
    )

    # Cache wrote exactly one object.
    assert len(fake_sc.store) == 1
    clip_bytes = list(fake_sc.store.values())[0]

    # Write to temp and inspect.
    out_path = tmp_path / "out.tif"
    out_path.write_bytes(clip_bytes)

    with rasterio.open(out_path) as ds:
        bounds = ds.bounds
        out_crs = ds.crs
        out_data = ds.read(1)
        out_nodata = ds.nodata

    # Geographic-correctness assertion: bounds match polygon bbox within ~1 pixel.
    pixel_lon = 2.0 / 256  # source raster is 2 deg wide / 256 px
    assert abs(bounds.left - poly_bbox[0]) <= pixel_lon, f"left {bounds.left} vs {poly_bbox[0]}"
    assert abs(bounds.bottom - poly_bbox[1]) <= pixel_lon, f"bottom {bounds.bottom} vs {poly_bbox[1]}"
    assert abs(bounds.right - poly_bbox[2]) <= pixel_lon, f"right {bounds.right} vs {poly_bbox[2]}"
    assert abs(bounds.top - poly_bbox[3]) <= pixel_lon, f"top {bounds.top} vs {poly_bbox[3]}"

    # Output CRS preserved.
    assert out_crs.to_epsg() == 4326

    # Output has nonzero size and at least some non-nodata pixels (the polygon
    # interior fills almost the entire output extent because crop=True).
    assert out_data.size > 0
    valid = out_data[out_data != out_nodata]
    assert valid.size > 0, "expected at least one valid (non-nodata) pixel inside polygon"

    # LayerURI shape.
    assert result.layer_type == "raster"
    assert result.uri.startswith("s3://")
    assert "clip_raster_polygon" in result.uri


# ---------------------------------------------------------------------------
# Test 3 — polygon CRS mismatch is reprojected
# ---------------------------------------------------------------------------


def test_polygon_crs_mismatch_is_reprojected(tmp_path):
    """Polygon in EPSG:3857 (web mercator), raster in EPSG:4326 → reprojection
    applied transparently; output is masked correctly.

    We pick a polygon whose 3857 bounds reproject to the center quadrant of the
    raster's WGS84 extent and verify the output bounds match.
    """
    src_path = tmp_path / "src4326.tif"
    poly_path = tmp_path / "poly3857.fgb"

    _write_synthetic_raster(
        str(src_path),
        width=256, height=256,
        crs="EPSG:4326",
        west=-82.0, south=26.0, east=-80.0, north=28.0,
    )

    # In WGS84: center is (-81, 27); want a 0.5-degree-wide polygon centered there.
    # Compute the 3857 equivalents.
    from pyproj import Transformer
    fwd = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    x0, y0 = fwd.transform(-81.25, 26.75)
    x1, y1 = fwd.transform(-80.75, 27.25)
    _write_polygon_fgb(str(poly_path), (x0, y0, x1, y1), crs="EPSG:3857")

    fake_sc = FakeStorageClient()
    result = clip_raster_to_polygon(
        raster_uri=str(src_path),
        polygon_uri=str(poly_path),
        _bucket="test-bucket",
    )

    clip_bytes = list(fake_sc.store.values())[0]
    out_path = tmp_path / "out.tif"
    out_path.write_bytes(clip_bytes)

    with rasterio.open(out_path) as ds:
        bounds = ds.bounds
        out_crs = ds.crs

    # Output CRS preserved (matches raster's native CRS).
    assert out_crs.to_epsg() == 4326

    # Reprojected polygon bounds (back in 4326) approximate (-81.25, 26.75, -80.75, 27.25).
    # Allow ~2 pixels of tolerance (raster is ~0.0078 deg/pixel).
    tol = 0.05
    assert abs(bounds.left - (-81.25)) < tol, f"left={bounds.left}"
    assert abs(bounds.right - (-80.75)) < tol, f"right={bounds.right}"
    assert abs(bounds.bottom - 26.75) < tol, f"bottom={bounds.bottom}"
    assert abs(bounds.top - 27.25) < tol, f"top={bounds.top}"


# ---------------------------------------------------------------------------
# Test 4 — feature_filter selects one polygon from multi-feature input
# ---------------------------------------------------------------------------


def test_feature_filter_selects_one_polygon(tmp_path):
    """Multi-feature polygon vector + feature_filter picks one polygon by attribute.

    The other polygon must NOT contribute to the mask: the output extent must
    match ONLY the selected polygon's bounds.
    """
    src_path = tmp_path / "src.tif"
    poly_path = tmp_path / "polys.fgb"

    _write_synthetic_raster(
        str(src_path), width=256, height=256, crs="EPSG:4326",
        west=-82.0, south=26.0, east=-80.0, north=28.0,
    )

    # Two polygons: "left" in the west half, "right" in the east half.
    left_bbox = (-81.8, 26.5, -81.2, 27.5)
    right_bbox = (-80.8, 26.5, -80.2, 27.5)
    _write_polygon_fgb_multi(
        str(poly_path),
        [
            (left_bbox, {"name": "Left"}),
            (right_bbox, {"name": "Right"}),
        ],
        crs="EPSG:4326",
    )

    fake_sc = FakeStorageClient()
    result = clip_raster_to_polygon(
        raster_uri=str(src_path),
        polygon_uri=str(poly_path),
        feature_filter={"property": "name", "value": "Right"},
        _bucket="test-bucket",
    )

    clip_bytes = list(fake_sc.store.values())[0]
    out_path = tmp_path / "out.tif"
    out_path.write_bytes(clip_bytes)

    with rasterio.open(out_path) as ds:
        bounds = ds.bounds

    # Output bounds should match RIGHT polygon's bbox, not include LEFT.
    pixel_lon = 2.0 / 256
    assert abs(bounds.left - right_bbox[0]) <= pixel_lon
    assert abs(bounds.right - right_bbox[2]) <= pixel_lon
    # CRITICAL: left polygon must NOT extend into output.
    assert bounds.left > left_bbox[2], (
        f"output left bound {bounds.left} indicates left polygon was included "
        f"(left polygon ends at {left_bbox[2]})"
    )

    # filter_suffix appears in layer_id.
    assert "Right" in result.layer_id


# ---------------------------------------------------------------------------
# Test 5 — nodata_outside override
# ---------------------------------------------------------------------------


def test_nodata_outside_override(tmp_path):
    """nodata_outside=-999 → output's nodata == -999 and outside-polygon pixels = -999."""
    src_path = tmp_path / "src.tif"
    poly_path = tmp_path / "poly.fgb"

    # Constant-value raster (10.0) so we can spot outside-polygon nodata clearly.
    _write_synthetic_raster(
        str(src_path), width=128, height=128, crs="EPSG:4326",
        west=-82.0, south=26.0, east=-80.0, north=28.0,
        fill_value=10.0,
    )
    # Small polygon inside the raster; crop=True means output extent = polygon bbox.
    # Use a slightly-rotated diamond shape so masking creates clear outside pixels.
    import geopandas as gpd
    from shapely.geometry import Polygon
    diamond = Polygon([
        (-81.0, 26.5),
        (-80.5, 27.0),
        (-81.0, 27.5),
        (-81.5, 27.0),
    ])
    gdf = gpd.GeoDataFrame([{"geometry": diamond}], geometry="geometry", crs="EPSG:4326")
    gdf.to_file(str(poly_path), driver="FlatGeobuf", engine="pyogrio")

    fake_sc = FakeStorageClient()
    result = clip_raster_to_polygon(
        raster_uri=str(src_path),
        polygon_uri=str(poly_path),
        nodata_outside=-999.0,
        _bucket="test-bucket",
    )

    clip_bytes = list(fake_sc.store.values())[0]
    out_path = tmp_path / "out.tif"
    out_path.write_bytes(clip_bytes)

    with rasterio.open(out_path) as ds:
        out_data = ds.read(1)
        out_nodata = ds.nodata

    assert out_nodata == -999.0
    # Corner pixels (outside the diamond) should be -999.
    # Top-left corner of the diamond's bbox-cropped output is outside the diamond.
    assert out_data[0, 0] == -999.0 or out_data[-1, 0] == -999.0 or out_data[0, -1] == -999.0
    # At least one pixel inside the diamond should be 10.0 (source fill value).
    assert np.any(out_data == 10.0), "expected at least one inside-polygon pixel == 10.0"


# ---------------------------------------------------------------------------
# Test 6 — cache miss then cache hit skips mask
# ---------------------------------------------------------------------------


def test_cache_miss_then_hit_skips_mask(tmp_path):
    """First call masks; second call with same args returns from cache without re-masking."""
    from unittest.mock import patch

    src_path = tmp_path / "src.tif"
    poly_path = tmp_path / "poly.fgb"

    _write_synthetic_raster(str(src_path), width=64, height=64, crs="EPSG:4326",
                            west=-82.0, south=26.0, east=-80.0, north=28.0)
    _write_polygon_fgb(str(poly_path), (-81.5, 26.5, -80.5, 27.5), crs="EPSG:4326")

    fake_sc = FakeStorageClient()
    mask_call_count = [0]
    original_mask = __import__(
        "grace2_agent.tools.clip_raster_to_polygon", fromlist=["_mask_and_write"]
    )._mask_and_write

    def _counting_mask(*args, **kwargs):
        mask_call_count[0] += 1
        return original_mask(*args, **kwargs)

    with patch(
        "grace2_agent.tools.clip_raster_to_polygon._mask_and_write",
        side_effect=_counting_mask,
    ):
        r1 = clip_raster_to_polygon(
            raster_uri=str(src_path), polygon_uri=str(poly_path), _bucket="test-bucket",
        )
        assert mask_call_count[0] == 1

        r2 = clip_raster_to_polygon(
            raster_uri=str(src_path), polygon_uri=str(poly_path), _bucket="test-bucket",
        )
        # mask NOT called again on cache hit.
        assert mask_call_count[0] == 1, (
            f"expected mask to be called once; got {mask_call_count[0]} calls"
        )

    assert r1.uri == r2.uri


# ---------------------------------------------------------------------------
# Test 7 — empty filter raises typed error
# ---------------------------------------------------------------------------


def test_empty_filter_raises_typed_error(tmp_path):
    """feature_filter that matches zero features raises POLYGON_FILTER_EMPTY."""
    src_path = tmp_path / "src.tif"
    poly_path = tmp_path / "polys.fgb"

    _write_synthetic_raster(str(src_path), width=64, height=64, crs="EPSG:4326",
                            west=-82.0, south=26.0, east=-80.0, north=28.0)
    _write_polygon_fgb_multi(
        str(poly_path),
        [
            ((-81.5, 26.5, -80.5, 27.5), {"name": "Real"}),
        ],
        crs="EPSG:4326",
    )

    fake_sc = FakeStorageClient()
    with pytest.raises(ClipRasterPolygonError) as exc_info:
        clip_raster_to_polygon(
            raster_uri=str(src_path),
            polygon_uri=str(poly_path),
            feature_filter={"property": "name", "value": "Nonexistent"},
            _bucket="test-bucket",
        )
    assert exc_info.value.error_code == "POLYGON_FILTER_EMPTY"


# ---------------------------------------------------------------------------
# Test 8 — unknown raster_uri
# ---------------------------------------------------------------------------


def test_unknown_raster_uri_raises_typed_error():
    """Non-gs:// non-file raster URI raises UNKNOWN_RASTER_URI."""
    from grace2_agent.tools.clip_raster_to_polygon import _get_source_crs

    with pytest.raises(ClipRasterPolygonError) as exc_info:
        _get_source_crs("/nonexistent/path/missing.tif")
    assert exc_info.value.error_code == "UNKNOWN_RASTER_URI"


# ---------------------------------------------------------------------------
# Test 9 — unknown polygon_uri
# ---------------------------------------------------------------------------


def test_unknown_polygon_uri_raises_typed_error(tmp_path):
    """Non-gs:// non-file polygon URI raises UNKNOWN_POLYGON_URI."""
    src_path = tmp_path / "src.tif"
    _write_synthetic_raster(str(src_path), width=64, height=64, crs="EPSG:4326",
                            west=-82.0, south=26.0, east=-80.0, north=28.0)
    fake_sc = FakeStorageClient()
    with pytest.raises(ClipRasterPolygonError) as exc_info:
        clip_raster_to_polygon(
            raster_uri=str(src_path),
            polygon_uri="/nonexistent/path/missing.fgb",
            _bucket="test-bucket",
        )
    assert exc_info.value.error_code == "UNKNOWN_POLYGON_URI"


# ---------------------------------------------------------------------------
# Test 10 — live geographic-correctness end-to-end
# ---------------------------------------------------------------------------


_LIVE = os.environ.get("GRACE2_TEST_LIVE_CLIP") == "1"


@pytest.mark.skipif(not _LIVE, reason="set GRACE2_TEST_LIVE_CLIP=1 to enable")
def test_live_clip_fortmyers_dem_to_lee_county_shape(tmp_path):
    """Live geographic-correctness gate.

    Simulates the real flow: a Fort Myers/Lee County-sized DEM (synthetic but
    geographically positioned at the Fort Myers AOI) clipped to a Lee County
    approximate-shape polygon. Verifies:

    1. The output's geographic bounds are inside Lee County's bbox.
    2. The masked output has at least one valid pixel.
    3. The output's pixel center coordinates fall inside the polygon (sampled).

    No external network — we hand-craft a Lee County approximation polygon so the
    test is deterministic but exercises the real rasterio.mask + reprojection
    path on a realistic geography (the "wettest pixels at the river mouth" gate
    from the codified-lesson #1 reminder).
    """
    src_path = tmp_path / "fortmyers_dem.tif"
    poly_path = tmp_path / "lee_county.fgb"

    # Fort Myers / Lee County bbox approximately (-82.30, 26.40, -81.55, 26.85)
    _write_synthetic_raster(
        str(src_path),
        width=512, height=512,
        crs="EPSG:4326",
        west=-82.30, south=26.40, east=-81.55, north=26.85,
    )

    # Lee County rough outline (simplified — captures the main land area).
    import geopandas as gpd
    from shapely.geometry import Polygon
    lee_county = Polygon([
        (-82.20, 26.50),
        (-82.10, 26.45),
        (-81.85, 26.45),
        (-81.65, 26.55),
        (-81.60, 26.70),
        (-81.70, 26.80),
        (-81.85, 26.82),
        (-82.00, 26.78),
        (-82.15, 26.70),
        (-82.20, 26.50),
    ])
    gdf = gpd.GeoDataFrame(
        [{"NAME": "Lee", "STATE": "FL", "geometry": lee_county}],
        geometry="geometry",
        crs="EPSG:4326",
    )
    gdf.to_file(str(poly_path), driver="FlatGeobuf", engine="pyogrio")

    fake_sc = FakeStorageClient()
    result = clip_raster_to_polygon(
        raster_uri=str(src_path),
        polygon_uri=str(poly_path),
        feature_filter={"property": "NAME", "value": "Lee"},
        _bucket="test-bucket",
    )

    clip_bytes = list(fake_sc.store.values())[0]
    out_path = tmp_path / "lee_clip.tif"
    out_path.write_bytes(clip_bytes)

    with rasterio.open(out_path) as ds:
        bounds = ds.bounds
        out_data = ds.read(1)
        out_nodata = ds.nodata
        out_transform = ds.transform
        out_crs = ds.crs
        h, w = ds.height, ds.width

    # 1. Output bounds inside Lee County's broader bbox (within tolerance).
    poly_bounds = lee_county.bounds
    assert bounds.left >= poly_bounds[0] - 0.01
    assert bounds.bottom >= poly_bounds[1] - 0.01
    assert bounds.right <= poly_bounds[2] + 0.01
    assert bounds.top <= poly_bounds[3] + 0.01

    # 2. Output has valid (in-polygon) pixels.
    valid = out_data[out_data != out_nodata]
    assert valid.size > 0, "Lee County mask produced no valid pixels — clip failed geographically"
    assert valid.size < out_data.size, (
        "Lee County mask produced no nodata pixels — polygon didn't actually mask"
    )

    # 3. Geographic-correctness: a sample of valid pixels' coords should fall
    #    inside the polygon. Pick the first valid pixel row,col and check.
    valid_yx = np.argwhere(out_data != out_nodata)
    assert valid_yx.size > 0
    # Sample up to 10 valid pixels evenly.
    sample_idx = np.linspace(0, len(valid_yx) - 1, num=min(10, len(valid_yx)), dtype=int)
    inside_count = 0
    for idx in sample_idx:
        row, col = valid_yx[idx]
        # Pixel center coords.
        x, y = out_transform * (col + 0.5, row + 0.5)
        from shapely.geometry import Point
        if lee_county.contains(Point(x, y)):
            inside_count += 1
    # At least 80% of sampled valid pixels must fall inside the polygon (the
    # masking is conservative — all_touched=False default — so all sampled
    # valid pixels should be inside).
    assert inside_count >= int(0.8 * len(sample_idx)), (
        f"Only {inside_count}/{len(sample_idx)} valid pixels fell inside Lee County polygon — "
        f"geographic-correctness gate failed."
    )

    # 4. LayerURI sanity.
    assert result.layer_type == "raster"
    assert "Lee" in result.layer_id
    assert out_crs.to_epsg() == 4326

    print(
        f"\nLIVE CLIP RESULT: bounds={bounds}, "
        f"valid_pixels={valid.size}/{out_data.size} "
        f"({100*valid.size/out_data.size:.1f}%), "
        f"sample_inside={inside_count}/{len(sample_idx)}"
    )
