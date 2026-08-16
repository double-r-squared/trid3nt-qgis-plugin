"""Router CDS library-delegate fold (ADR 0085): ERA5 + GTSM parity coverage.

The twins (fetch_era5_reanalysis.py / fetch_gtsm_tide_surge.py) are DELETED; both
sources are spec-driven through the ``cds`` delegate hooks. cdsapi is not installed,
so these tests fake it (the twins' established ``sys.modules`` injection) to drive the
missing-key / auth / upstream classifier and the NetCDF decode -- the OFFLINE surface
(input-validation + missing-key parity + happy-path array/feature shape). No CDS key is
ever registered; live-positive requires a resolvable key from the environment.
"""

from __future__ import annotations

import datetime as _dt
import sys
import types
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.agent.tools.fetchers._router import hooks
from trid3nt_server.agent.tools.fetchers._router.spec import load_spec_from_path

_BASE = Path(__file__).resolve().parents[1] / "trid3nt_server/agent/tools/fetchers"


@pytest.fixture(scope="module")
def era5_spec():
    return load_spec_from_path(_BASE / "climate/fetch_era5_reanalysis/source.yaml")


@pytest.fixture(scope="module")
def gtsm_spec():
    return load_spec_from_path(_BASE / "ocean/fetch_gtsm_tide_surge/source.yaml")


def _install_fake_cdsapi(monkeypatch, *, on_construct=None, on_retrieve=None):
    m = types.ModuleType("cdsapi")

    class FakeClient:
        def __init__(self, **_kw):
            if on_construct:
                raise on_construct

        def retrieve(self, _ds, _req, out):
            if on_retrieve:
                raise on_retrieve()
            Path(out).write_bytes(b"x")

    m.Client = FakeClient
    monkeypatch.setitem(sys.modules, "cdsapi", m)


def _write_synthetic_era5_netcdf(out_path, variable, bbox, n_hours=24):
    import xarray as xr

    west, south, east, north = bbox
    lats = np.arange(north, south - 0.01, -0.25)
    lons = np.arange(west, east + 0.01, 0.25)
    times = np.array([np.datetime64(_dt.datetime(2024, 9, 26, h, 0), "ns") for h in range(n_hours)])
    arr = np.zeros((len(times), len(lats), len(lons)), dtype=np.float32)
    cy, cx = len(lats) // 2, len(lons) // 2
    for t in range(len(times)):
        for j in range(len(lats)):
            for i in range(len(lons)):
                arr[t, j, i] = float(0.01 * (1 + t) * np.exp(-((j - cy) ** 2 + (i - cx) ** 2) / 4.0))
    short = {"total_precipitation": "tp", "2m_temperature": "t2m",
             "10m_u_component_of_wind": "u10", "10m_v_component_of_wind": "v10"}.get(variable, variable[:8])
    da = xr.DataArray(arr, dims=("time", "latitude", "longitude"),
                      coords={"time": times, "latitude": lats, "longitude": lons}, name=short,
                      attrs={"long_name": variable.replace("_", " "), "units": "m"})
    da.to_dataset().to_netcdf(out_path)


def _write_synthetic_gtsm_netcdf(out_path, station_lons, station_lats, n=24):
    import xarray as xr

    times = np.array([np.datetime64(_dt.datetime(2017, 9, 5, h, 0), "ns") for h in range(n)])
    ns = len(station_lons)
    wl = np.tile(np.sin(np.linspace(0, 6.28, n))[:, None], (1, ns)).astype("float32")
    ds = xr.Dataset(
        {"total_water_level": (("time", "stations"), wl)},
        coords={"time": times, "stations": np.arange(ns),
                "station_x_coordinate": ("stations", np.asarray(station_lons, dtype="float64")),
                "station_y_coordinate": ("stations", np.asarray(station_lats, dtype="float64"))},
    )
    ds.to_netcdf(out_path)


# --------------------------------------------------------------------------- #
# Registration + spec-shape.
# --------------------------------------------------------------------------- #


def test_both_cds_specs_load_and_register():
    from trid3nt_server.agent.tools import TOOL_REGISTRY

    for name in ("fetch_era5_reanalysis", "fetch_gtsm_tide_surge"):
        assert name in TOOL_REGISTRY, f"{name} not registered (spec fold)"


def test_hooks_registered():
    for h in ("era5.read", "era5.validate", "gtsm.read", "gtsm.validate"):
        assert h in hooks.HOOK_REGISTRY


# --------------------------------------------------------------------------- #
# Input-validation parity (the twins' _validate_* helpers).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("params", [
    dict(bbox=[10, 10, 10, 20], variable="2m_temperature", start_date="2020-01-01", end_date="2020-01-02"),
    dict(bbox=[-100, 20, -80, 35], variable="nope", start_date="2020-01-01", end_date="2020-01-02"),
    dict(bbox=[-100, 20, -80, 35], variable="2m_temperature", start_date="bad", end_date="2020-01-02"),
    dict(bbox=[-100, 20, -80, 35], variable="2m_temperature", start_date="2020-05-01", end_date="2020-01-02"),
    dict(bbox=[-100, 20, -80, 35], variable="2m_temperature", start_date="2019-01-01", end_date="2020-06-01"),
])
def test_era5_validate_input_error(era5_spec, params):
    with pytest.raises(Exception) as ei:
        hooks.HOOK_REGISTRY["era5.validate"](era5_spec, params)
    assert ei.value.error_code == "ERA5_INPUT_ERROR"
    assert ei.value.retryable is False


@pytest.mark.parametrize("params", [
    dict(bbox=[10, 10, 10, 20], output="water_level", start_date="2017-09-05", end_date="2017-09-11"),
    dict(bbox=[-70, 10, -60, 20], output="nope", start_date="2017-09-05", end_date="2017-09-11"),
    dict(bbox=[-70, 10, -60, 20], output="water_level", start_date="xx", end_date="2017-09-11"),
])
def test_gtsm_validate_input_error(gtsm_spec, params):
    with pytest.raises(Exception) as ei:
        hooks.HOOK_REGISTRY["gtsm.validate"](gtsm_spec, params)
    assert ei.value.error_code == "GTSM_INPUT_ERROR"
    assert ei.value.retryable is False


# --------------------------------------------------------------------------- #
# CDS-failure classification parity (missing-key / auth / upstream).
# --------------------------------------------------------------------------- #

_ERA5_GOOD = dict(bbox=[-82.4, 26.3, -81.6, 26.9], variable="2m_temperature", start_date="2020-01-01", end_date="2020-01-01")
_GTSM_GOOD = dict(bbox=[-70, 10, -60, 20], output="water_level", start_date="2017-09-05", end_date="2017-09-05")


@pytest.mark.parametrize("exc,code,retry", [
    (Exception("Missing/incomplete configuration file: /root/.cdsapirc"), "ERA5_MISSING_KEY", False),
    (Exception("403 Client Error: Forbidden - user not authenticated"), "ERA5_AUTH_ERROR", False),
    (Exception("503 queue backend unavailable"), "ERA5_UPSTREAM_ERROR", True),
])
def test_era5_classify(monkeypatch, era5_spec, exc, code, retry):
    _install_fake_cdsapi(monkeypatch, on_construct=exc)
    with pytest.raises(Exception) as ei:
        hooks.HOOK_REGISTRY["era5.read"](era5_spec, _ERA5_GOOD, timeout_s=10.0)
    assert ei.value.error_code == code
    assert ei.value.retryable is retry


@pytest.mark.parametrize("exc,code,retry", [
    # GTSM's narrower classifier (twin parity): the .cdsapirc message lacks "key",
    # so it classifies as UPSTREAM (the ADR-flagged pre-existing asymmetry, reproduced).
    (Exception("Missing/incomplete configuration file: /root/.cdsapirc"), "GTSM_UPSTREAM_ERROR", True),
    (Exception("no api key available"), "GTSM_MISSING_KEY", False),
    (Exception("403 Forbidden authentication failed"), "GTSM_AUTH_ERROR", False),
    (Exception("503 backend down"), "GTSM_UPSTREAM_ERROR", True),
])
def test_gtsm_classify(monkeypatch, gtsm_spec, exc, code, retry):
    _install_fake_cdsapi(monkeypatch, on_construct=exc)
    with pytest.raises(Exception) as ei:
        hooks.HOOK_REGISTRY["gtsm.read"](gtsm_spec, _GTSM_GOOD, timeout_s=10.0)
    assert ei.value.error_code == code
    assert ei.value.retryable is retry


# --------------------------------------------------------------------------- #
# Happy-path decode (mocked NetCDF): the delegate returns the expected shape.
# --------------------------------------------------------------------------- #


def test_era5_read_happy_path_array(monkeypatch, era5_spec):
    bbox = [-82.4, 26.3, -81.6, 26.9]

    def _construct_writes(**_kw):
        pass

    m = types.ModuleType("cdsapi")

    class FakeClient:
        def __init__(self, **_kw):
            pass

        def retrieve(self, _ds, _req, out):
            _write_synthetic_era5_netcdf(out, "2m_temperature", tuple(bbox))

    m.Client = FakeClient
    monkeypatch.setitem(sys.modules, "cdsapi", m)
    arr, transform, crs = hooks.HOOK_REGISTRY["era5.read"](
        era5_spec, dict(bbox=bbox, variable="2m_temperature", start_date="2024-09-26", end_date="2024-09-26"), timeout_s=10.0)
    assert arr.ndim == 2 and arr.size > 0
    assert np.isfinite(arr).any()
    assert crs == "EPSG:4326"


def test_era5_read_derived_wind_speed(monkeypatch, era5_spec):
    bbox = [-82.4, 26.3, -81.6, 26.9]
    m = types.ModuleType("cdsapi")

    class FakeClient:
        def __init__(self, **_kw):
            pass

        def retrieve(self, _ds, req, out):
            _write_synthetic_era5_netcdf(out, req["variable"], tuple(bbox))

    m.Client = FakeClient
    monkeypatch.setitem(sys.modules, "cdsapi", m)
    arr, _t, _c = hooks.HOOK_REGISTRY["era5.read"](
        era5_spec, dict(bbox=bbox, variable="10m_wind_speed", start_date="2024-09-26", end_date="2024-09-26"), timeout_s=10.0)
    assert arr.ndim == 2 and np.isfinite(arr).any()
    assert float(np.nanmin(arr)) >= 0.0  # wind SPEED magnitude is non-negative


def test_gtsm_read_happy_path_features(monkeypatch, gtsm_spec, tmp_path):
    nc = tmp_path / "gtsm.nc"
    _write_synthetic_gtsm_netcdf(str(nc), station_lons=[-90.0, -85.0, -70.0], station_lats=[28.0, 29.0, 10.0])
    # Decode the extracted NetCDF directly (bypass the CDS retrieve socket).
    recs = hooks.cds._gtsm_netcdf_to_records(gtsm_spec, [str(nc)], (-100.0, 20.0, -80.0, 35.0), "water_level")
    feats = hooks.cds._gtsm_records_to_features(recs, "water_level")
    assert len(feats) == 2  # the two in-bbox stations
    props = feats[0]["properties"]
    assert set(props) >= {"gauge_id", "lon", "lat", "time_start", "time_end", "n_timesteps",
                          "wl_min_m", "wl_max_m", "wl_mean_m", "output", "time_series_csv"}
    assert feats[0]["geometry"]["type"] == "Point"


def test_gtsm_empty_bbox_raises(gtsm_spec, tmp_path):
    nc = tmp_path / "gtsm.nc"
    _write_synthetic_gtsm_netcdf(str(nc), station_lons=[-70.0], station_lats=[10.0])
    with pytest.raises(Exception) as ei:
        hooks.cds._gtsm_netcdf_to_records(gtsm_spec, [str(nc)], (-100.0, 20.0, -80.0, 35.0), "water_level")
    assert ei.value.error_code == "GTSM_EMPTY"
