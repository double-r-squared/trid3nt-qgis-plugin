"""Offline regression: OPEN-25b DEM retry ladder + USGS 3DEP fallback.

Pins the 2026-07-18 outage: the Planetary Computer STAC endpoint served Azure
Front Door 503 HTML pages and the one-shot DEM fetch killed runs outright.
The ladder must (a) retry the STAC rung 3x with 5/20/60 s backoff, (b) fall
back to USGS 3DEP exportImage, (c) raise the pipeline's plain RuntimeError
(the typed metrics error entrypoint.main already surfaces) when both rungs
fail. All network mocked - no sockets touched.

Run: python -m pytest services/workers/telemac/tests/ -q
"""
import sys
from pathlib import Path

import numpy as np
import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import telemac_river_dye_build as B

BBOX = [-123.10, 46.10, -122.90, 46.25]
LON = np.array([-123.05, -123.00, -122.95])
LAT = np.array([46.15, 46.17, 46.20])


@pytest.fixture()
def sleeps(monkeypatch):
    """Record (and skip) the ladder's backoff sleeps."""
    slept = []
    monkeypatch.setattr(B.time, "sleep", lambda s: slept.append(float(s)))
    return slept


def test_stac_outage_retries_then_3dep_fallback(monkeypatch, sleeps):
    calls = {"stac": 0, "dep": 0}

    def stac_503(lon, lat, bbox):
        calls["stac"] += 1
        raise requests.exceptions.HTTPError("503 Server Error: AFD")

    def dep_ok(lon, lat, bbox):
        calls["dep"] += 1
        return np.array([10.0, 11.0, 12.0])

    monkeypatch.setattr(B, "_sample_dem_stac", stac_503)
    monkeypatch.setattr(B, "_sample_dem_3dep", dep_ok)
    z, source = B._fetch_dem_samples(LON, LAT, BBOX)
    assert source == "usgs-3dep"
    assert calls == {"stac": 3, "dep": 1}
    assert sleeps == [5.0, 20.0]
    np.testing.assert_allclose(z, [10.0, 11.0, 12.0])


def test_stac_success_skips_fallback(monkeypatch, sleeps):
    def dep_boom(lon, lat, bbox):
        raise AssertionError("fallback must not run when STAC succeeds")

    monkeypatch.setattr(B, "_sample_dem_stac",
                        lambda lon, lat, bbox: np.array([1.0, 2.0, 3.0]))
    monkeypatch.setattr(B, "_sample_dem_3dep", dep_boom)
    z, source = B._fetch_dem_samples(LON, LAT, BBOX)
    assert source == "cop-dem-glo-30"
    assert sleeps == []
    np.testing.assert_allclose(z, [1.0, 2.0, 3.0])


def test_no_tiles_goes_straight_to_fallback(monkeypatch, sleeps):
    calls = {"stac": 0}

    def stac_empty(lon, lat, bbox):
        calls["stac"] += 1
        return None  # empty catalog is deterministic - no retries

    monkeypatch.setattr(B, "_sample_dem_stac", stac_empty)
    monkeypatch.setattr(B, "_sample_dem_3dep",
                        lambda lon, lat, bbox: np.array([7.0, 8.0, 9.0]))
    z, source = B._fetch_dem_samples(LON, LAT, BBOX)
    assert source == "usgs-3dep"
    assert calls["stac"] == 1
    assert sleeps == []


def test_both_rungs_fail_raise_the_pipeline_typed_error(monkeypatch, sleeps):
    def stac_503(lon, lat, bbox):
        raise requests.exceptions.ConnectionError("AFD reset")

    def dep_down(lon, lat, bbox):
        raise requests.exceptions.ConnectionError("3DEP down too")

    monkeypatch.setattr(B, "_sample_dem_stac", stac_503)
    monkeypatch.setattr(B, "_sample_dem_3dep", dep_down)
    with pytest.raises(RuntimeError) as ei:
        B._fetch_dem_samples(LON, LAT, BBOX)
    # exactly the RuntimeError shape entrypoint.main folds into metrics.error
    assert ei.type is RuntimeError
    assert "STAC" in str(ei.value) and "3DEP" in str(ei.value)


def _synthetic_tiff(bbox, value=42.0, nodata=-9999.0):
    """A tiny in-memory GeoTIFF covering bbox (one nodata corner pixel)."""
    from rasterio.io import MemoryFile
    from rasterio.transform import from_bounds

    w = h = 32
    arr = np.full((h, w), value, dtype="float32")
    arr[0, 0] = nodata
    tf = from_bounds(bbox[0], bbox[1], bbox[2], bbox[3], w, h)
    with MemoryFile() as mf:
        with mf.open(driver="GTiff", width=w, height=h, count=1,
                     dtype="float32", crs="EPSG:4326", transform=tf,
                     nodata=nodata) as ds:
            ds.write(arr, 1)
        return mf.read()


class _Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


def test_3dep_rung_parses_export_tiff_and_samples_nodes(monkeypatch):
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"] = url
        seen["params"] = params
        return _Resp(_synthetic_tiff(BBOX))

    monkeypatch.setattr(requests, "get", fake_get)
    z = B._sample_dem_3dep(LON, LAT, BBOX)
    # same z_raw contract as the STAC rung: one finite sample per node
    assert z.shape == LON.shape
    np.testing.assert_allclose(z, 42.0)
    assert seen["url"].endswith("/3DEPElevation/ImageServer/exportImage")
    assert seen["params"]["f"] == "image"
    assert seen["params"]["format"] == "tiff"
    assert seen["params"]["bboxSR"] == "4326"


def test_3dep_rung_rejects_arcgis_200_json_error(monkeypatch):
    monkeypatch.setattr(
        requests, "get",
        lambda url, params=None, timeout=None:
            _Resp(b'{"error":{"code":500,"message":"boom"}}'))
    with pytest.raises(RuntimeError, match="non-tiff"):
        B._sample_dem_3dep(LON, LAT, BBOX)
