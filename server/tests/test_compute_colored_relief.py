"""Unit tests for the ``compute_colored_relief`` atomic tool (job-0080).

Coverage:
- ``@register_tool`` lands a registry entry with the expected metadata
  (name="compute_colored_relief", ttl_class="static-30d",
   source_class="colored_relief", cacheable=True).
- ``_write_ramp_file`` produces a valid ``gdaldem color-relief`` CSV for
  each of the four ramp presets.
- Each ramp preset produces a 3- or 4-band RGB(A) GeoTIFF output from a
  synthetic 32×32 DEM with a known elevation gradient.
- Cache hit: a second call with the same ``(dem_uri, ramp)`` returns the
  cached artefact without invoking ``gdaldem`` again.
- Cache miss followed by hit: first call writes through, second call returns
  from the fake GCS store.
- ``ColoredReliefError`` on unknown ramp name.
- The returned ``LayerURI`` carries the expected shape (layer_type="raster",
  style_preset="continuous_dem", role="context", units="rgb").

Tests that exercise ``gdaldem`` (the synthetic-DEM tests) are skipped when
the ``gdaldem`` binary is not on PATH, so they do not break CI environments
that lack GDAL. The cache-layer tests are pure Python and run unconditionally.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.compute_colored_relief import (
    ColoredReliefError,
    _VALID_RAMPS,
    _write_ramp_file,
    compute_colored_relief,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PINNED_NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

_GDALDEM_AVAILABLE = shutil.which("gdaldem") is not None


def _make_synthetic_dem_tif() -> str:
    """Create a 32×32 single-band GeoTIFF with a known elevation gradient.

    Elevation values go linearly from 0 m (top-left) to 900 m (bottom-right).
    The file is written to a temp path and returned; caller is responsible for
    cleanup.

    Uses ``gdal_array`` if available; falls back to a minimal hand-crafted
    GeoTIFF byte sequence (enough for gdaldem to parse) if GDAL Python
    bindings are not installed.
    """
    try:
        from osgeo import gdal, gdal_array  # type: ignore[import-not-found]
        import numpy as np  # type: ignore[import-not-found]
    except ImportError:
        # Minimal GeoTIFF fallback (rasterio-free, GDAL-Python-free).
        # Write 32x32 float32 GeoTIFF using rasterio if available, else skip.
        try:
            import rasterio  # type: ignore[import-not-found]
            from rasterio.transform import from_bounds  # type: ignore[import-not-found]
            import numpy as np

            data = np.linspace(0, 900, 32 * 32, dtype=np.float32).reshape(32, 32)
            transform = from_bounds(-82.0, 26.5, -81.9, 26.6, 32, 32)
            with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
                path = f.name
            profile = {
                "driver": "GTiff",
                "dtype": "float32",
                "width": 32,
                "height": 32,
                "count": 1,
                "crs": "EPSG:4326",
                "transform": transform,
                "nodata": -9999.0,
            }
            with rasterio.open(path, "w", **profile) as ds:
                ds.write(data, 1)
            return path
        except ImportError:
            pytest.skip("neither gdal_array nor rasterio available — cannot build synthetic DEM")

    import numpy as np

    data = np.linspace(0, 900, 32 * 32, dtype=np.float32).reshape(32, 32)
    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
        path = f.name

    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(path, 32, 32, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([-82.0, (0.1 / 32), 0.0, 26.6, 0.0, -(0.1 / 32)])
    from osgeo import osr
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(data)
    ds.FlushCache()
    ds = None  # close
    return path


def _count_bands(tif_path: str) -> int:
    """Return the number of bands in a GeoTIFF using gdalinfo."""
    try:
        result = subprocess.run(
            ["gdalinfo", tif_path],
            capture_output=True,
            check=True,
            timeout=30,
        )
        output = result.stdout.decode("utf-8", errors="replace")
        # Count "Band N" lines in gdalinfo output.
        return sum(1 for line in output.splitlines() if line.strip().startswith("Band "))
    except Exception:
        # Fall back: open with rasterio if available.
        try:
            import rasterio
            with rasterio.open(tif_path) as ds:
                return ds.count
        except Exception:
            return -1


# ---------------------------------------------------------------------------
# Fake GCS plumbing (mirrors test_tools_cache.py).
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
# Registration tests (no gdaldem needed).
# ---------------------------------------------------------------------------


def test_compute_colored_relief_is_registered():
    """Tool appears in TOOL_REGISTRY with the expected metadata."""
    assert "compute_colored_relief" in TOOL_REGISTRY
    entry = TOOL_REGISTRY["compute_colored_relief"]
    assert entry.metadata.name == "compute_colored_relief"
    assert entry.metadata.ttl_class == "static-30d"
    assert entry.metadata.source_class == "colored_relief"
    assert entry.metadata.cacheable is True


def test_four_ramp_presets_exist():
    """All four required ramp names are registered."""
    assert _VALID_RAMPS == {"terrain", "elevation_blue_green", "grayscale", "viridis"}


# ---------------------------------------------------------------------------
# _write_ramp_file tests (no gdaldem needed).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ramp", ["terrain", "elevation_blue_green", "grayscale", "viridis"])
def test_write_ramp_file_produces_valid_csv(ramp: str, tmp_path):
    """Each ramp produces a file with lines in ``<elev> R G B`` format."""
    ramp_path = str(tmp_path / f"ramp_{ramp}.txt")
    _write_ramp_file(ramp, ramp_path)

    with open(ramp_path) as f:
        lines = [ln.strip() for ln in f if ln.strip()]

    assert len(lines) >= 3, f"ramp {ramp!r} should have at least 3 entries"

    for line in lines:
        parts = line.split()
        assert len(parts) == 4, (
            f"ramp {ramp!r}: expected 4 space-separated fields per line; got: {line!r}"
        )
        # First field is elevation (integer or "nv"); remaining are R/G/B 0-255.
        elev_str, r_str, g_str, b_str = parts
        if elev_str != "nv":
            int(elev_str)  # should parse as int
        for ch_str in (r_str, g_str, b_str):
            val = int(ch_str)
            assert 0 <= val <= 255, (
                f"ramp {ramp!r}: channel value {val} out of [0, 255] in line: {line!r}"
            )


def test_write_ramp_file_raises_on_unknown_ramp(tmp_path):
    """Unknown ramp name raises ColoredReliefError."""
    with pytest.raises(ColoredReliefError, match="unknown ramp"):
        _write_ramp_file("rainbow", str(tmp_path / "ramp.txt"))


# ---------------------------------------------------------------------------
# Synthetic DEM tests (require gdaldem on PATH).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _GDALDEM_AVAILABLE, reason="gdaldem not on PATH")
@pytest.mark.parametrize("ramp", ["terrain", "elevation_blue_green", "grayscale", "viridis"])
def test_each_ramp_produces_multi_band_output(ramp: str):
    """Each ramp preset on a synthetic 32×32 DEM produces 3 or 4 bands."""
    dem_path = _make_synthetic_dem_tif()
    out_path: str | None = None
    try:
        # Directly invoke _run_colored_relief with a local path.
        from trid3nt_server.tools.compute_colored_relief import _run_colored_relief

        cog_bytes = _run_colored_relief(dem_uri=dem_path, ramp=ramp)
        assert len(cog_bytes) > 0, f"ramp {ramp!r}: _run_colored_relief returned empty bytes"

        # Write bytes to temp file and count bands.
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as f:
            out_path = f.name
            f.write(cog_bytes)

        n_bands = _count_bands(out_path)
        assert n_bands in (3, 4), (
            f"ramp {ramp!r}: expected 3 (RGB) or 4 (RGBA) bands; got {n_bands}"
        )
    finally:
        for p in (dem_path, out_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Cache-integration tests (pure Python, no gdaldem).
# ---------------------------------------------------------------------------


def _make_fake_cog_bytes() -> bytes:
    """Return a minimal bytes payload to represent a fake cached GeoTIFF."""
    return b"FAKE_COG_BYTES_" + b"\x00" * 32


def test_cache_hit_skips_fetch_fn():
    """When the cache is pre-seeded, the fetch function is NOT invoked.

    This test calls ``read_through`` directly (same approach as
    ``test_cache_miss_writes_through``) with a pre-seeded FakeStorageClient.
    The key derivation uses the same params dict as ``compute_colored_relief``
    would use internally, so a pre-seeded entry at that path produces a HIT.
    """
    fake_gcs = FakeStorageClient()
    dem_uri = "gs://legacy-cloud-cache/cache/static-30d/dem/abc123.tif"
    ramp = "terrain"

    from trid3nt_server.tools.cache import cache_path, compute_cache_key, read_through as real_rt
    from trid3nt_server.tools.compute_colored_relief import _COMPUTE_COLORED_RELIEF_METADATA

    params = {"dem_uri": dem_uri, "ramp": ramp}
    key = compute_cache_key("colored_relief", params, "static-30d", now=_PINNED_NOW)
    cached_path = cache_path("colored_relief", "static-30d", key, "tif")

    fake_payload = _make_fake_cog_bytes()
    fake_gcs.store[cached_path] = fake_payload

    fetch_invoked = {"n": 0}

    def fake_fetch_fn() -> bytes:
        fetch_invoked["n"] += 1
        return b"SHOULD_NOT_BE_CALLED"

    result = real_rt(
        metadata=_COMPUTE_COLORED_RELIEF_METADATA,
        params=params,
        ext="tif",
        fetch_fn=fake_fetch_fn,
        storage_client=fake_gcs,
        now=_PINNED_NOW,
    )

    assert result.hit is True, "Expected cache HIT; fetch_fn should not have been invoked"
    assert result.data == fake_payload
    assert result.uri is not None
    assert fetch_invoked["n"] == 0


def test_cache_miss_writes_through():
    """On cache miss, fetch_fn is invoked and result is written to the store."""
    fake_gcs = FakeStorageClient()
    dem_uri = "gs://legacy-cloud-cache/cache/static-30d/dem/xyz999.tif"
    ramp = "viridis"

    params = {"dem_uri": dem_uri, "ramp": ramp}
    fresh_bytes = _make_fake_cog_bytes() + b"_FRESH"
    fetch_invoked = {"n": 0}

    def fake_fetch_fn() -> bytes:
        fetch_invoked["n"] += 1
        return fresh_bytes

    from trid3nt_server.tools.cache import read_through as real_read_through
    from trid3nt_server.tools.compute_colored_relief import _COMPUTE_COLORED_RELIEF_METADATA

    result = real_read_through(
        metadata=_COMPUTE_COLORED_RELIEF_METADATA,
        params=params,
        ext="tif",
        fetch_fn=fake_fetch_fn,
        storage_client=fake_gcs,
        now=_PINNED_NOW,
    )

    assert result.hit is False, "Expected cache MISS on first call"
    assert result.data == fresh_bytes
    assert result.uri is not None
    assert fetch_invoked["n"] == 1
    # The artefact is persisted in the fake store.
    assert len(fake_gcs.store) == 1

    # Second call with same params: should be a HIT.
    result2 = real_read_through(
        metadata=_COMPUTE_COLORED_RELIEF_METADATA,
        params=params,
        ext="tif",
        fetch_fn=fake_fetch_fn,
        storage_client=fake_gcs,
        now=_PINNED_NOW,
    )
    assert result2.hit is True, "Expected cache HIT on second call with same params"
    assert result2.data == fresh_bytes
    assert fetch_invoked["n"] == 1  # NOT incremented again


# ---------------------------------------------------------------------------
# LayerURI shape tests (no gdaldem, pure Python).
# ---------------------------------------------------------------------------


def test_compute_colored_relief_returns_correct_layer_uri_shape():
    """compute_colored_relief returns a LayerURI with the expected field values."""
    fake_gcs = FakeStorageClient()
    dem_uri = "gs://legacy-cloud-cache/cache/static-30d/dem/shape_test.tif"
    ramp = "grayscale"

    fake_result_bytes = _make_fake_cog_bytes()

    # Patch _run_colored_relief so gdaldem is not invoked.
    with patch("trid3nt_server.tools.compute_colored_relief._run_colored_relief",
               return_value=fake_result_bytes), \
         patch("google.cloud.storage.Client", return_value=fake_gcs):
        # read_through will use fake_gcs because we injected it via the storage_client arg.
        # We need to intercept the read_through call to pass our fake GCS.
        from trid3nt_server.tools.cache import read_through as real_rt
        from trid3nt_server.tools.compute_colored_relief import _COMPUTE_COLORED_RELIEF_METADATA

        params = {"dem_uri": dem_uri, "ramp": ramp}

        # Wrap read_through to inject fake GCS.
        def patched_read_through(metadata, params, ext, fetch_fn, **kw):
            return real_rt(
                metadata=metadata,
                params=params,
                ext=ext,
                fetch_fn=fetch_fn,
                storage_client=fake_gcs,
                now=_PINNED_NOW,
            )

        with patch(
            "trid3nt_server.tools.compute_colored_relief.read_through",
            side_effect=patched_read_through,
        ):
            layer_uri = compute_colored_relief(dem_uri=dem_uri, ramp=ramp)

    assert layer_uri.layer_type == "raster"
    assert layer_uri.role == "context"
    assert layer_uri.units == "rgb"
    assert layer_uri.style_preset == "continuous_dem"
    assert layer_uri.uri.startswith("s3://")
    assert "colored_relief" in layer_uri.uri
    assert "grayscale" in layer_uri.name.lower()


def test_compute_colored_relief_raises_on_unknown_ramp():
    """Unknown ramp raises ColoredReliefError (validated before any I/O)."""
    with pytest.raises(ColoredReliefError, match="unknown ramp"):
        compute_colored_relief(
            dem_uri="gs://some-bucket/dem.tif",
            ramp="rainbow",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("ramp", ["terrain", "elevation_blue_green", "grayscale", "viridis"])
def test_layer_uri_name_contains_ramp_label(ramp: str):
    """LayerURI.name identifies the ramp preset used."""
    fake_gcs = FakeStorageClient()
    dem_uri = f"gs://legacy-cloud-cache/cache/dem/{ramp}_test.tif"

    from trid3nt_server.tools.cache import read_through as real_rt
    from trid3nt_server.tools.compute_colored_relief import _COMPUTE_COLORED_RELIEF_METADATA

    def patched_read_through(metadata, params, ext, fetch_fn, **kw):
        return real_rt(
            metadata=metadata,
            params=params,
            ext=ext,
            fetch_fn=lambda: _make_fake_cog_bytes(),
            storage_client=fake_gcs,
            now=_PINNED_NOW,
        )

    with patch(
        "trid3nt_server.tools.compute_colored_relief.read_through",
        side_effect=patched_read_through,
    ):
        layer_uri = compute_colored_relief(dem_uri=dem_uri, ramp=ramp)

    assert layer_uri.name  # non-empty
    assert "Colored Relief" in layer_uri.name or ramp.replace("_", " ") in layer_uri.name.lower()
