"""Unit tests for the ``compute_contours`` atomic tool.

The GDAL subprocess and the DEM read are mocked, so no contour binary is needed.
Registration and metadata; the default interval derived from relief, never zero
or negative; the interval and the FlatGeobuf driver reaching the argv; the vector
LayerURI's shape; the binary-missing and no-input typed errors; a cache hit."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.derive.compute_contours.compute_contours import (
    ContourComputeError,
    _derive_interval_m,
    _snap_to_nice_interval,
    _run_gdal_contour,
    compute_contours,
)

PINNED_NOW = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)




def _write_synthetic_dem(
    path: str,
    relief_m: float = 150.0,
    size: int = 32,
    dx_m: float = 10.0,
) -> None:
    """Write a 32x32 GeoTIFF DEM with a known N-S relief (EPSG:5070, metres)."""
    elevations = np.zeros((size, size), dtype=np.float32)
    for row in range(size):
        # Row 0 (north) lowest; increases southward up to relief_m.
        elevations[row, :] = (row / (size - 1)) * relief_m

    west, south, east, north = 0.0, 0.0, size * dx_m, size * dx_m
    transform = from_bounds(west, south, east, north, size, size)
    profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": size,
        "height": size,
        "count": 1,
        "crs": "EPSG:5070",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(elevations, 1)


def _fake_dem_bytes(relief_m: float = 150.0) -> bytes:
    """Return synthetic DEM GeoTIFF bytes (used as the mock download)."""
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp:
        path = tmp.name
    try:
        _write_synthetic_dem(path, relief_m=relief_m)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _fake_contour_fgb_bytes() -> bytes:
    """Return a valid FlatGeobuf of one LineString contour (elev attr), 4326."""
    import geopandas as gpd
    from shapely.geometry import LineString

    gdf = gpd.GeoDataFrame(
        {"elev": [50.0]},
        geometry=[LineString([(-100.0, 40.0), (-99.9, 40.05)])],
        crs="EPSG:4326",
    )
    with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as tmp:
        path = tmp.name
    try:
        gdf.to_file(path, driver="FlatGeobuf", engine="pyogrio")
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass




class _S3Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeStorageClient:
    """In-memory S3 double; ``store`` is keyed by object KEY.

    Returns the per-test instance the autouse fixture installs, so the tool's real
    boto3 read-through reads and writes the store the test inspects."""

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


@pytest.fixture()
def fake_storage():
    return FakeStorageClient()




def test_compute_contours_registered():
    assert "compute_contours" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["compute_contours"]
    assert entry.metadata.cacheable is True
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "contours"




def test_snap_to_nice_interval_never_zero_or_negative():
    assert _snap_to_nice_interval(0.0) > 0.0
    assert _snap_to_nice_interval(-5.0) > 0.0
    assert _snap_to_nice_interval(float("nan")) > 0.0


def test_snap_to_nice_interval_picks_closest_nice():
    # relief/15 ~= 9 → snaps to 10 (closest nice value).
    assert _snap_to_nice_interval(9.0) == 10.0
    # ~22 → snaps to 20.
    assert _snap_to_nice_interval(22.0) == 20.0
    # huge raw → snaps to the largest nice value.
    assert _snap_to_nice_interval(99999.0) == 1000.0


def test_derive_interval_from_dem_relief_gives_readable_count():
    """A 150 m relief DEM → interval ~10 m → ~15 contours (readable)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        _write_synthetic_dem(dem_path, relief_m=150.0)
        interval = _derive_interval_m(dem_path)
    assert interval > 0.0
    # relief 150 / 15 = 10 → nice 10 m.
    assert interval == 10.0
    n_contours = 150.0 / interval
    assert 8 <= n_contours <= 25


def test_derive_interval_flat_dem_falls_back_to_smallest():
    """A flat DEM (zero relief) → smallest nice interval (never 0)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        dem_path = os.path.join(tmpdir, "dem.tif")
        # relief 0: every pixel the same.
        data = np.full((16, 16), 100.0, dtype=np.float32)
        transform = from_bounds(0, 0, 160, 160, 16, 16)
        with rasterio.open(
            dem_path, "w", driver="GTiff", dtype="float32",
            width=16, height=16, count=1, crs="EPSG:5070", transform=transform,
        ) as dst:
            dst.write(data, 1)
        interval = _derive_interval_m(dem_path)
    assert interval == 1.0




def test_run_gdal_contour_invocation_args():
    """_run_gdal_contour builds argv with -a elev, -i <interval>, FlatGeobuf."""
    captured = {}

    class _Completed:
        returncode = 0
        stdout = b""
        stderr = b""

    def _fake_run(cmd, *a, **kw):
        captured["cmd"] = cmd
        return _Completed()

    with patch(
        "trid3nt_server.tools.derive.compute_contours.compute_contours._get_gdal_contour_bin",
        return_value="/usr/bin/gdal_contour",
    ), patch("subprocess.run", side_effect=_fake_run):
        _run_gdal_contour("/tmp/in.tif", "/tmp/out.fgb", interval_m=20.0)

    cmd = captured["cmd"]
    assert cmd[0] == "/usr/bin/gdal_contour"
    # -a elev pair present in order.
    assert "-a" in cmd and cmd[cmd.index("-a") + 1] == "elev"
    # -i <interval> pair present in order.
    assert "-i" in cmd and cmd[cmd.index("-i") + 1] == "20.0"
    # FlatGeobuf driver.
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "FlatGeobuf"
    assert "/tmp/in.tif" in cmd and "/tmp/out.fgb" in cmd




def test_compute_contours_layer_uri_shape(fake_storage):
    """End-to-end (mocked subprocess + DEM read): returns a vector LayerURI."""
    fake_dem = _fake_dem_bytes(relief_m=150.0)
    fake_fgb = _fake_contour_fgb_bytes()

    def _fake_gdal_contour(inp, out, interval_m):
        # Write a real FGB so the reproject step (geopandas read) succeeds.
        with open(out, "wb") as f:
            f.write(fake_fgb)

    with patch(
        "trid3nt_server.tools.derive.compute_contours.compute_contours._download_dem_bytes",
        return_value=fake_dem,
    ), patch(
        "trid3nt_server.tools.derive.compute_contours.compute_contours._run_gdal_contour",
        side_effect=_fake_gdal_contour,
    ), patch("trid3nt_server.tools.cache.CACHE_BUCKET", "test-bucket"):
        result = compute_contours(
            dem_uri="gs://test-bucket/cache/static-30d/dem/abc123.tif",
            _bucket="test-bucket",
        )

    assert result.layer_type == "vector"
    assert result.role == "context"
    assert result.style == {"kind": "reference", "geometry": "line"}
    assert result.units == "m"
    assert result.uri.startswith("s3://")
    assert "/contours/" in result.uri
    # bbox set to the DEM extent (EPSG:4326) for auto-zoom.
    assert result.bbox is not None
    assert len(result.bbox) == 4
    # derived interval (10 m) flows into the id/name.
    assert "contours" in result.layer_id
    assert "10" in result.name


def test_compute_contours_explicit_interval_in_name(fake_storage):
    """A pinned interval_m is reflected in the layer name + id."""
    fake_dem = _fake_dem_bytes()
    fake_fgb = _fake_contour_fgb_bytes()

    with patch(
        "trid3nt_server.tools.derive.compute_contours.compute_contours._download_dem_bytes",
        return_value=fake_dem,
    ), patch(
        "trid3nt_server.tools.derive.compute_contours.compute_contours._run_gdal_contour",
        side_effect=lambda inp, out, interval_m: open(out, "wb").write(fake_fgb) or None,
    ), patch("trid3nt_server.tools.cache.CACHE_BUCKET", "test-bucket"):
        result = compute_contours(
            dem_uri="gs://test-bucket/cache/static-30d/dem/abc123.tif",
            interval_m=50.0,
            _bucket="test-bucket",
        )
    assert "50" in result.name
    assert "50" in result.layer_id




def test_compute_contours_binary_missing_raises(fake_storage):
    """A missing gdal_contour binary → ContourComputeError(GDAL_CONTOUR_UNAVAILABLE)."""
    fake_dem = _fake_dem_bytes()

    with patch(
        "trid3nt_server.tools.derive.compute_contours.compute_contours._download_dem_bytes",
        return_value=fake_dem,
    ), patch(
        "trid3nt_server.tools.derive.compute_contours.compute_contours._get_gdal_contour_bin",
        side_effect=ContourComputeError(
            "GDAL_CONTOUR_UNAVAILABLE", "gdal_contour binary not found"
        ),
    ), patch("trid3nt_server.tools.cache.CACHE_BUCKET", "test-bucket"):
        with pytest.raises(ContourComputeError) as exc_info:
            compute_contours(
                dem_uri="gs://test-bucket/dem/abc.tif",
                interval_m=10.0,
                _bucket="test-bucket",
            )
    assert exc_info.value.error_code == "GDAL_CONTOUR_UNAVAILABLE"




def test_compute_contours_no_dem_input_raises():
    with pytest.raises(ContourComputeError) as exc_info:
        compute_contours(_bucket="test-bucket")
    assert exc_info.value.error_code == "NO_DEM_INPUT"




def test_compute_contours_cache_hit_skips_fetch(fake_storage):
    """On cache hit, gdal_contour is NOT invoked; cached bytes are returned."""
    fake_fgb = _fake_contour_fgb_bytes()

    from trid3nt_server.tools.cache import cache_path as make_cache_path
    from trid3nt_server.tools.cache import compute_cache_key

    dem_uri = "gs://test-bucket/cache/static-30d/dem/abc123.tif"
    params = {"dem_uri": dem_uri, "interval_m": 10.0}
    # ``compute_contours`` calls ``read_through`` without a ``now=`` override,
    # so production keys on the REAL current time -- seeding with a key from
    # the stale module-level ``PINNED_NOW`` drifts out of the same
    # ``static-30d`` (YYYY-MM) bucket after the month rolls over, silently
    # missing the cache. Compute the seeding key from the actual current time.
    now = datetime.now(timezone.utc)
    key = compute_cache_key("contours", params, "static-30d", now=now)
    path = make_cache_path("contours", "static-30d", key, "fgb")
    fake_storage.store[path] = fake_fgb

    contour_called = []

    def _no_contour(*args, **kwargs):
        contour_called.append(args)

    with patch(
        "trid3nt_server.tools.derive.compute_contours.compute_contours._run_gdal_contour",
        side_effect=_no_contour,
    ), patch(
        "trid3nt_server.tools.derive.compute_contours.compute_contours._download_dem_bytes",
        return_value=b"",
    ), patch("trid3nt_server.tools.cache.CACHE_BUCKET", "test-bucket"):
        result = compute_contours(
            dem_uri=dem_uri,
            interval_m=10.0,
            _bucket="test-bucket",
        )

    assert len(contour_called) == 0, "gdal_contour ran on a cache hit"
    assert result.uri.endswith(f"{key}.fgb")
    assert result.layer_type == "vector"




def test_compute_contours_bbox_fetches_dem(fake_storage):
    """When only bbox is given, the DEM is acquired via fetch_dem (no reinvent)."""
    from trid3nt_contracts.execution import LayerURI

    fake_dem = _fake_dem_bytes()
    fake_fgb = _fake_contour_fgb_bytes()

    fetch_dem_calls = []

    def _fake_fetch_dem(bbox, *a, **kw):
        fetch_dem_calls.append(bbox)
        return LayerURI(
            layer_id="dem-x",
            name="DEM",
            layer_type="raster",
            uri="gs://test-bucket/cache/static-30d/dem/frombbox.tif",
            role="input",
            units="meters",
        )

    with patch(
        "trid3nt_server.tools.derive.compute_contours.compute_contours.fetch_dem",
        side_effect=_fake_fetch_dem,
    ), patch(
        "trid3nt_server.tools.derive.compute_contours.compute_contours._download_dem_bytes",
        return_value=fake_dem,
    ), patch(
        "trid3nt_server.tools.derive.compute_contours.compute_contours._run_gdal_contour",
        side_effect=lambda inp, out, interval_m: open(out, "wb").write(fake_fgb) or None,
    ), patch("trid3nt_server.tools.cache.CACHE_BUCKET", "test-bucket"):
        result = compute_contours(
            bbox=(-100.0, 40.0, -99.9, 40.1),
            _bucket="test-bucket",
        )

    assert len(fetch_dem_calls) == 1, "fetch_dem should be called for the bbox path"
    assert result.layer_type == "vector"
    assert "frombbox" in result.uri or result.uri.endswith(".fgb")
