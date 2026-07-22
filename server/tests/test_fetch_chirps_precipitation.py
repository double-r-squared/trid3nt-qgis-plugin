"""Tests for ``fetch_chirps_precipitation`` — UCSB CHIRPS rainfall COG.

No network: a synthetic CHIRPS-shaped GeoTIFF (EPSG:4326, 0.05 deg, float32 mm,
with a -9999 ocean sentinel patch) is gzip-compressed and fed through the tool's
``_http_get`` seam so the full download -> gunzip -> window -> COG pipeline runs
offline. Covers:

  - Registration + metadata invariants.
  - Synthetic correctness: window -> single-band mm COG, sentinel collapsed.
  - Honest-empty: bbox outside the 50S-50N extent / all-ocean -> CHIRPSEmptyError.
  - Honest 404: an unpublished date -> CHIRPSNotAvailableError.
  - Input validation: bad bbox / bad date / bad period / missing date.
  - URL resolution (monthly vs daily) + payload estimator.
  - End-to-end LayerURI shape with a mocked read_through.
"""

from __future__ import annotations

import gzip
import urllib.error
from datetime import date as _date
from unittest.mock import patch

import numpy as np
import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.cache import ReadThroughResult
import trid3nt_server.tools.fetch_chirps_precipitation as mod
from trid3nt_server.tools.fetch_chirps_precipitation import (
    CHIRPSEmptyError,
    CHIRPSInputError,
    CHIRPSNotAvailableError,
    estimate_payload_mb,
    fetch_chirps_precipitation,
    _resolve_chirps_url,
    _window_chirps_to_cog,
)


# ---------------------------------------------------------------------------
# Synthetic CHIRPS GeoTIFF fixture.
# ---------------------------------------------------------------------------


def _make_synthetic_chirps_gz(
    bbox: tuple[float, float, float, float] = (-180.0, -50.0, 180.0, 50.0),
    width: int = 360,
    height: int = 100,
    ocean_band: bool = True,
) -> bytes:
    """Build a gzip-compressed synthetic CHIRPS-shaped GeoTIFF.

    EPSG:4326, float32 precip in mm with a linear N-S gradient; the left third
    of columns is set to the -9999 ocean sentinel when ``ocean_band`` is True,
    to exercise the sentinel-collapse + all-nodata-window paths.
    """
    import rasterio
    from rasterio.io import MemoryFile
    from rasterio.transform import from_bounds

    arr = np.tile(
        np.linspace(0.0, 500.0, height, dtype="float32")[:, None], (1, width)
    )
    if ocean_band:
        arr[:, : width // 3] = -9999.0
    transform = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], width, height)
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": rasterio.crs.CRS.from_epsg(4326),
        "transform": transform,
        # CHIRPS source does NOT tag nodata in the header — mirror that.
    }
    with MemoryFile() as memf:
        with memf.open(**profile) as dst:
            dst.write(arr, 1)
        tif_bytes = memf.read()
    return gzip.compress(tif_bytes)


# ---------------------------------------------------------------------------
# Registration + metadata.
# ---------------------------------------------------------------------------


def test_tool_is_registered():
    assert "fetch_chirps_precipitation" in TOOL_REGISTRY


def test_metadata_invariants():
    m = TOOL_REGISTRY["fetch_chirps_precipitation"].metadata
    assert m.ttl_class == "static-30d"
    assert m.source_class == "chirps_precipitation"
    assert m.cacheable is True
    assert getattr(m, "supports_global_query", False) is True


# ---------------------------------------------------------------------------
# Input validation.
# ---------------------------------------------------------------------------


def test_missing_date_raises_input_error():
    with pytest.raises(CHIRPSInputError):
        fetch_chirps_precipitation(bbox=(72, 15, 78, 21), date=None)


def test_bad_period_raises_input_error():
    with pytest.raises(CHIRPSInputError):
        fetch_chirps_precipitation(date="2023-07", period="hourly")


def test_degenerate_bbox_raises_input_error():
    with pytest.raises(CHIRPSInputError):
        fetch_chirps_precipitation(bbox=(78, 21, 72, 15), date="2023-07")


def test_lon_out_of_range_raises_input_error():
    with pytest.raises(CHIRPSInputError):
        fetch_chirps_precipitation(bbox=(-200, 15, 78, 21), date="2023-07")


def test_unparseable_date_raises_input_error():
    with pytest.raises(CHIRPSInputError):
        fetch_chirps_precipitation(date="July 2023")


def test_daily_requires_full_date():
    with pytest.raises(CHIRPSInputError):
        fetch_chirps_precipitation(date="2023-07", period="daily")


def test_pre_1981_date_raises_input_error():
    with pytest.raises(CHIRPSInputError):
        fetch_chirps_precipitation(date="1975-06")


def test_future_date_raises_input_error():
    with pytest.raises(CHIRPSInputError):
        fetch_chirps_precipitation(date="2099-01")


# ---------------------------------------------------------------------------
# URL resolution + payload estimator.
# ---------------------------------------------------------------------------


def test_resolve_url_monthly():
    url = _resolve_chirps_url(_date(2023, 7, 1), "monthly")
    assert url.endswith("global_monthly/tifs/chirps-v2.0.2023.07.tif.gz")


def test_resolve_url_daily():
    url = _resolve_chirps_url(_date(2022, 8, 25), "daily")
    assert url.endswith("global_daily/tifs/p05/2022/chirps-v2.0.2022.08.25.tif.gz")


def test_estimate_payload_positive_and_scales():
    small = estimate_payload_mb((72, 15, 73, 16), "monthly")
    big = estimate_payload_mb((0, -40, 120, 40), "monthly")
    glob = estimate_payload_mb(None, "monthly")
    assert small > 0.0
    assert big > small
    assert glob >= big


# ---------------------------------------------------------------------------
# Synthetic correctness — window -> single-band mm COG, sentinel collapsed.
# ---------------------------------------------------------------------------


def test_window_to_cog_emits_single_band_mm_cog():
    import rasterio
    from rasterio.io import MemoryFile

    gz = _make_synthetic_chirps_gz()
    tif = gzip.decompress(gz)
    # Window the eastern (land) half so we avoid the synthetic ocean band.
    cog = _window_chirps_to_cog(tif, bbox=(20.0, -20.0, 60.0, 20.0))
    with MemoryFile(cog) as memf:
        with memf.open() as src:
            assert src.count == 1
            assert src.dtypes[0] == "float32"
            assert src.crs.to_epsg() == 4326
            assert src.nodata == -9999.0
            assert src.descriptions[0] == "precipitation_mm"
            assert src.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG"
            arr = src.read(1)
            valid = arr[arr > -9000]
            assert valid.size > 0
            assert float(valid.min()) >= 0.0


def test_window_collapses_ocean_sentinel_to_nodata():
    import rasterio
    from rasterio.io import MemoryFile

    gz = _make_synthetic_chirps_gz(ocean_band=True)
    tif = gzip.decompress(gz)
    # Full-extent window keeps both the ocean band and the land band.
    cog = _window_chirps_to_cog(tif, bbox=None)
    with MemoryFile(cog) as memf:
        with memf.open() as src:
            arr = src.read(1)
            # The synthetic -9999 ocean band must remain at the GeoTIFF nodata,
            # and there must also be valid precip pixels.
            assert np.any(arr == -9999.0)
            assert np.any(arr > -9000.0)


# ---------------------------------------------------------------------------
# Honest-empty.
# ---------------------------------------------------------------------------


def test_offshore_all_ocean_bbox_raises_empty():
    gz = _make_synthetic_chirps_gz(ocean_band=True)
    tif = gzip.decompress(gz)
    # Window strictly inside the synthetic ocean band (left third of -180..180
    # i.e. lon < -60) -> all-nodata -> CHIRPSEmptyError.
    with pytest.raises(CHIRPSEmptyError):
        _window_chirps_to_cog(tif, bbox=(-170.0, -20.0, -120.0, 20.0))


# ---------------------------------------------------------------------------
# Honest 404 -> CHIRPSNotAvailableError.
# ---------------------------------------------------------------------------


def test_http_404_maps_to_not_available():
    def fake_urlopen(*_a, **_k):
        raise urllib.error.HTTPError(
            url="http://x", code=404, msg="Not Found", hdrs=None, fp=None
        )

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(CHIRPSNotAvailableError):
            mod._fetch_chirps_bytes(_date(2099, 1, 1), "monthly", None)


def test_http_500_maps_to_upstream_retryable():
    def fake_urlopen(*_a, **_k):
        raise urllib.error.HTTPError(
            url="http://x", code=500, msg="Server Error", hdrs=None, fp=None
        )

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        with pytest.raises(mod.CHIRPSUpstreamError):
            mod._fetch_chirps_bytes(_date(2023, 7, 1), "monthly", None)


# ---------------------------------------------------------------------------
# End-to-end LayerURI shape with synthetic bytes + mocked read_through.
# ---------------------------------------------------------------------------


def test_end_to_end_returns_layer_uri():
    gz = _make_synthetic_chirps_gz()

    def fake_http_get(url, timeout):  # noqa: ARG001
        return gz

    captured = {}

    def fake_read_through(metadata, params, ext, fetch_fn, **_kw):  # noqa: ARG001
        captured["data"] = fetch_fn()  # exercise the real fetch->window->COG path
        captured["params"] = params
        return ReadThroughResult(
            uri="s3://test-bucket/cache/static-30d/chirps_precipitation/abc.tif",
            data=captured["data"],
            hit=False,
        )

    with patch.object(mod, "_http_get", side_effect=fake_http_get), patch.object(
        mod, "read_through", side_effect=fake_read_through
    ):
        res = fetch_chirps_precipitation(
            bbox=(20.0, -20.0, 60.0, 20.0), date="2023-07", period="monthly"
        )

    assert res.layer_type == "raster"
    assert res.units == "mm"
    assert res.style_preset == "precip_mm"
    assert res.role == "primary"
    assert res.uri.startswith("s3://")
    assert res.bbox == (20.0, -20.0, 60.0, 20.0)
    assert "2023-07" in res.layer_id
    assert captured["params"]["period"] == "monthly"
    assert captured["params"]["date"] == "2023-07"
    # The fetched bytes must be a valid raster.
    assert len(captured["data"]) > 0


def test_global_query_uses_global_bbox_and_token():
    gz = _make_synthetic_chirps_gz()

    def fake_read_through(metadata, params, ext, fetch_fn, **_kw):  # noqa: ARG001
        return ReadThroughResult(
            uri="s3://test-bucket/cache/static-30d/chirps_precipitation/glob.tif",
            data=b"x",
            hit=False,
        )

    with patch.object(mod, "_http_get", side_effect=lambda u, timeout: gz), patch.object(
        mod, "read_through", side_effect=fake_read_through
    ):
        res = fetch_chirps_precipitation(date="2023-07", period="monthly")

    assert res.bbox == (-180.0, -50.0, 180.0, 50.0)
    assert "GLOBAL" in res.layer_id
