"""Router value coverage for the fetch_noaa_sst fold (ADR 0079).

The NOAA CRW SST twin folded to a source.yaml + the new raster_cog ``griddap``
access mode (ERDDAP griddap bracket-selector .nc GET -> in-memory xarray subset ->
north-up float32 array -> NaN-nodata COG). These tests cover the value-bearing
surface: the bracket-selector URL build (lat high:low), the north-up orientation,
the 404-body no-data disambiguation (NOAA_SST_NO_DATA), the all-NaN land window
(NO_DATA), the bbox-area + variable param gates, and the spec-identity flags.
"""

from __future__ import annotations

import os
import tempfile

import numpy as np
import pytest

from trid3nt_server.data.fetchers._router import router
from trid3nt_server.data.fetchers._router.executors import raster_cog
from trid3nt_server.data.fetchers._router.spec import compose_specs_from_tree
from trid3nt_server.data.fetchers._router import transport as _transport


@pytest.fixture(scope="module")
def spec():
    return compose_specs_from_tree()["fetch_noaa_sst"]


def _synth_nc(var: str = "CRW_SST") -> bytes:
    """A synthetic griddap NetCDF: a 3x3 ocean window, lat DESCENDING (CRW grid)."""
    import xarray as xr

    lat = np.array([40.0, 39.95, 39.9], dtype="float64")   # descending (north first)
    lon = np.array([-70.0, -69.95, -69.9], dtype="float64")
    data = (np.arange(9, dtype="float32").reshape(3, 3) + 20.0)
    da = xr.DataArray(
        data[None, ...],
        dims=("time", "latitude", "longitude"),
        coords={"time": [np.datetime64("2018-10-10T12:00:00")], "latitude": lat, "longitude": lon},
    )
    ds = xr.Dataset({var: da})
    fd, p = tempfile.mkstemp(suffix=".nc")
    os.close(fd)
    try:
        ds.to_netcdf(p, engine="netcdf4")
        with open(p, "rb") as f:
            return f.read()
    finally:
        os.unlink(p)


def test_spec_identity(spec):
    assert spec.name == "fetch_noaa_sst"
    assert spec.shape == "raster-cog"
    assert spec.error_code_prefix == "NOAA_SST"
    assert spec.supports_global_query is False
    assert spec.cache.ttl_class == "static-30d"
    assert spec.source_class == "noaa_sst"
    assert spec.output.auto_publish is True
    assert spec.output.emit_bbox is True
    assert spec.empty_error_suffix == "NO_DATA"
    assert (spec.ingest or {}).get("access") == "griddap"


def test_bbox_area_gate(spec):
    with pytest.raises(Exception) as ei:
        router.validate_params(spec, {"bbox": [-80.0, 20.0, -40.0, 45.0]})  # 40*25 = 1000 deg^2
    assert getattr(ei.value, "error_code", "") == "NOAA_SST_INPUT_ERROR"


def test_bad_variable_enum(spec):
    with pytest.raises(Exception) as ei:
        router.validate_params(spec, {"bbox": [-70.0, 39.9, -69.9, 40.0], "variable": "salinity"})
    assert getattr(ei.value, "error_code", "") == "NOAA_SST_INPUT_ERROR"


def test_griddap_url_and_north_up(spec, monkeypatch):
    captured = {}

    def fake_get_bytes(client, url, headers=None, params=None):
        captured["url"] = url
        return _synth_nc("CRW_SST"), "application/x-netcdf", url

    monkeypatch.setattr(_transport, "get_bytes", fake_get_bytes)
    params = router.validate_params(
        spec, {"bbox": [-70.0, 39.9, -69.9, 40.0], "date": "2018-10-10", "variable": "sst"}
    )
    arr, transform, crs = raster_cog.fetch_source_array(spec, params)
    # bracket-selector: var[(ts)][(north):(south)][(west):(east)] -- lat high:low.
    assert "CRW_SST[(2018-10-10T12:00:00Z)][(40.0):(39.9)][(-70.0):(-69.9)]" in captured["url"]
    assert ".nc?" in captured["url"] and "/griddap/NOAA_DHW.nc" in captured["url"]
    assert arr.shape == (3, 3)
    assert np.isfinite(arr).all()
    # north-up: row 0 is the northernmost row (lat=40 -> data row [20,21,22]).
    assert arr[0, 0] == pytest.approx(20.0)
    assert transform.e < 0  # negative y-step (north-up)
    assert str(crs) == "EPSG:4326"


def test_default_date_when_absent(spec, monkeypatch):
    captured = {}

    def fake_get_bytes(client, url, headers=None, params=None):
        captured["url"] = url
        return _synth_nc("CRW_SST"), "application/x-netcdf", url

    monkeypatch.setattr(_transport, "get_bytes", fake_get_bytes)
    params = router.validate_params(spec, {"bbox": [-70.0, 39.9, -69.9, 40.0]})
    raster_cog.fetch_source_array(spec, params)
    # a date was defaulted (today-1) into the selector even though none was passed.
    assert "CRW_SST[(" in captured["url"] and "T12:00:00Z)]" in captured["url"]


def test_404_axis_marker_is_no_data(spec, monkeypatch):
    def fake_get_bytes(client, url, headers=None, params=None):
        raise _transport.TransportNotFound(
            "not found", body="Your query produced no matching results. "
            "time=... is greater than the axis maximum.")

    monkeypatch.setattr(_transport, "get_bytes", fake_get_bytes)
    params = router.validate_params(
        spec, {"bbox": [-70.0, 39.9, -69.9, 40.0], "date": "2099-01-01", "variable": "sst"}
    )
    with pytest.raises(Exception) as ei:
        raster_cog.fetch_source_array(spec, params)
    assert getattr(ei.value, "error_code", "") == "NOAA_SST_NO_DATA"


def test_404_without_marker_is_upstream(spec, monkeypatch):
    def fake_get_bytes(client, url, headers=None, params=None):
        raise _transport.TransportNotFound("not found", body="internal routing error")

    monkeypatch.setattr(_transport, "get_bytes", fake_get_bytes)
    params = router.validate_params(spec, {"bbox": [-70.0, 39.9, -69.9, 40.0], "variable": "sst"})
    with pytest.raises(Exception) as ei:
        raster_cog.fetch_source_array(spec, params)
    assert getattr(ei.value, "error_code", "") == "NOAA_SST_UPSTREAM_ERROR"


def test_all_land_window_is_no_data(spec, monkeypatch):
    def fake_get_bytes(client, url, headers=None, params=None):
        import xarray as xr

        lat = np.array([40.0, 39.95, 39.9], dtype="float64")
        lon = np.array([-70.0, -69.95, -69.9], dtype="float64")
        data = np.full((3, 3), np.nan, dtype="float32")  # fully-land / masked ocean
        da = xr.DataArray(
            data[None, ...], dims=("time", "latitude", "longitude"),
            coords={"time": [np.datetime64("2018-10-10T12:00:00")], "latitude": lat, "longitude": lon})
        ds = xr.Dataset({"CRW_SST": da})
        fd, p = tempfile.mkstemp(suffix=".nc")
        os.close(fd)
        try:
            ds.to_netcdf(p, engine="netcdf4")
            with open(p, "rb") as f:
                return f.read(), "application/x-netcdf", url
        finally:
            os.unlink(p)

    monkeypatch.setattr(_transport, "get_bytes", fake_get_bytes)
    params = router.validate_params(
        spec, {"bbox": [-70.0, 39.9, -69.9, 40.0], "date": "2018-10-10", "variable": "sst"})
    with pytest.raises(Exception) as ei:
        raster_cog.fetch_source_array(spec, params)
    assert getattr(ei.value, "error_code", "") == "NOAA_SST_NO_DATA"


def test_execute_serializes_cog(spec, monkeypatch):
    def fake_get_bytes(client, url, headers=None, params=None):
        return _synth_nc("CRW_SST"), "application/x-netcdf", url

    monkeypatch.setattr(_transport, "get_bytes", fake_get_bytes)
    params = router.validate_params(
        spec, {"bbox": [-70.0, 39.9, -69.9, 40.0], "date": "2018-10-10", "variable": "sst"})
    cog = raster_cog.execute(spec, params)
    assert cog[:4] in (b"II*\x00", b"MM\x00*")  # a (Geo)TIFF/COG
