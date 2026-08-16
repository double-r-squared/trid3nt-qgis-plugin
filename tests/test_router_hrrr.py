"""HRRR-Zarr library-delegate fold parity (ADR 0083): fetch_hrrr_forecast + fetch_hrrr_smoke.

Migrates the OFFLINE-testable coverage of the deleted twins (test_fetch_hrrr_forecast.py
/ test_fetch_hrrr_smoke.py) onto the router's library-delegate raster mode. The live
Zarr data path (s3fs cycle walk + xarray open + LCC->4326 reproject + clip + forecast
hypot) is proven by the ADR 0083 live twin-vs-router parity harness (value-identical on
all 5 variables); here the socket is mocked for a hermetic run, and the offline surfaces
are: spec identity, the delegate_validate CONUS + forecast_hour-horizon gates, the
delegate_resolve cycle walk + NOT_AVAILABLE backstop, the read hook's array/hypot shaping,
the units/style/role LayerURI stamps, and the payload estimate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from trid3nt_server.data.fetchers._router import router
from trid3nt_server.data.fetchers._router.errors import RouterInputError, RouterError
from trid3nt_server.data.fetchers._router.hooks import hrrr as H
from trid3nt_server.data.fetchers._router.spec import load_spec_from_path

_ROOT = Path(__file__).resolve().parents[1] / "trid3nt_server/data/fetchers/weather"
SPEC_FC = load_spec_from_path(_ROOT / "fetch_hrrr_forecast/source.yaml")
SPEC_SM = load_spec_from_path(_ROOT / "fetch_hrrr_smoke/source.yaml")

_FL = (-82.4, 26.3, -81.6, 26.9)  # Fort Myers / Lee County (in-CONUS)


def _vp(spec, **raw: Any) -> dict[str, Any]:
    return router.validate_params(spec, raw)


def _da(values: np.ndarray):
    """A tiny rioxarray DataArray (EPSG:4326) the read hook can shape."""
    import rioxarray  # noqa: F401 -- registers .rio
    import xarray as xr
    from affine import Affine

    h, w = values.shape
    da = xr.DataArray(values, dims=("y", "x"),
                      coords={"y": np.arange(h, dtype=float), "x": np.arange(w, dtype=float)})
    da.rio.write_crs("EPSG:4326", inplace=True)
    da.rio.write_transform(Affine(0.01, 0, -82.4, 0, -0.01, 26.9), inplace=True)
    return da


# --------------------------------------------------------------------------- #
# Spec identity.
# --------------------------------------------------------------------------- #


def test_spec_identity():
    assert SPEC_FC.name == "fetch_hrrr_forecast" and SPEC_FC.source_class == "hrrr"
    assert SPEC_FC.error_code_prefix == "HRRR_FORECAST"
    assert SPEC_SM.name == "fetch_hrrr_smoke" and SPEC_SM.source_class == "hrrr_smoke"
    assert SPEC_SM.error_code_prefix == "HRRR_SMOKE"
    for spec in (SPEC_FC, SPEC_SM):
        assert spec.shape == "raster-cog" and spec.output.layer_type == "raster"
        assert spec.hooks.delegate == "hrrr.read"
        assert spec.hooks.delegate_resolve == "hrrr.resolve_cycle"
        assert spec.hooks.delegate_validate == "hrrr.validate"
        assert (spec.ingest or {}).get("access") == "library_delegate"
    assert "10m_wind_speed" in SPEC_FC.params["variable"].values
    assert set(SPEC_SM.params["variable"].values) == {
        "near_surface_smoke", "smoke_column_mass", "aerosol_optical_depth"}


def test_docstring_verbatim_nonempty():
    # docstring carried verbatim from the twin (register_spec is the sole doc source)
    assert SPEC_FC.docstring and "HRRR" in SPEC_FC.docstring and len(SPEC_FC.docstring) > 3000
    assert SPEC_SM.docstring and "Smoke" in SPEC_SM.docstring


# --------------------------------------------------------------------------- #
# delegate_validate: CONUS + forecast_hour horizon (pure, offline).
# --------------------------------------------------------------------------- #


def test_validate_conus_gate_rejects_non_conus():
    p = _vp(SPEC_FC, bbox=[10.0, 40.0, 11.0, 41.0], variable="2m_temperature")
    with pytest.raises(RouterInputError) as ei:
        H.validate_inputs(SPEC_FC, p)
    assert ei.value.error_code == "HRRR_FORECAST_INPUT_ERROR"


def test_validate_forecast_hour_horizon_standard_cycle():
    # 05z is a standard cycle -> 18 h horizon; fh=30 exceeds it.
    p = _vp(SPEC_FC, bbox=list(_FL), variable="2m_temperature",
            forecast_hour=30, cycle="2026-08-02T05:00:00Z")
    with pytest.raises(RouterInputError, match="exceeds"):
        H.validate_inputs(SPEC_FC, p)


def test_validate_forecast_hour_horizon_extended_cycle_ok():
    # 00z is extended -> 48 h horizon; fh=30 is allowed.
    p = _vp(SPEC_FC, bbox=list(_FL), variable="2m_temperature",
            forecast_hour=30, cycle="2026-08-02T00:00:00Z")
    H.validate_inputs(SPEC_FC, p)  # no raise


def test_validate_passes_in_conus():
    H.validate_inputs(SPEC_SM, _vp(SPEC_SM, bbox=list(_FL), variable="near_surface_smoke"))


def test_bad_variable_enum_rejected():
    with pytest.raises(RouterError):
        _vp(SPEC_FC, bbox=list(_FL), variable="not_a_var")


def test_bad_cycle_format_rejected():
    p = {"bbox": list(_FL), "variable": "2m_temperature", "cycle": "not-a-date"}
    with pytest.raises(RouterInputError, match="ISO-8601"):
        H.validate_inputs(SPEC_FC, p)


# --------------------------------------------------------------------------- #
# delegate_resolve: s3fs cycle walk (mocked socket).
# --------------------------------------------------------------------------- #


def _patch_fs(monkeypatch, exists_fn):
    import fsspec

    class _FS:
        def exists(self, path):
            return exists_fn(path)

    monkeypatch.setattr(fsspec, "filesystem", lambda *a, **k: _FS())


def test_resolve_cycle_returns_merge(monkeypatch):
    _patch_fs(monkeypatch, lambda p: True)  # first candidate exists
    merged = H.resolve_cycle(SPEC_FC, {"variable": "2m_temperature", "forecast_hour": 3,
                                       "cycle": "2026-08-02T05:00:00Z"}, timeout_s=90)
    assert merged == {"cycle_date": "2026-08-02", "cycle_hour": 5}


def test_resolve_cycle_derived_probes_component(monkeypatch):
    seen = {}
    def ex(p):
        seen["path"] = p
        return True
    _patch_fs(monkeypatch, ex)
    H.resolve_cycle(SPEC_FC, {"variable": "10m_wind_speed", "forecast_hour": 3,
                              "cycle": "2026-08-02T05:00:00Z"}, timeout_s=90)
    # derived wind_speed probes the UGRD component (10m_above_ground/UGRD)
    assert "10m_above_ground/UGRD" in seen["path"]


def test_resolve_cycle_backstop_exhausted_not_available(monkeypatch):
    _patch_fs(monkeypatch, lambda p: False)  # nothing published
    with pytest.raises(RouterError) as ei:
        H.resolve_cycle(SPEC_FC, {"variable": "2m_temperature", "forecast_hour": 3,
                                  "cycle": "2026-08-02T05:00:00Z"}, timeout_s=90)
    assert ei.value.error_code == "HRRR_FORECAST_NOT_AVAILABLE"
    assert ei.value.retryable is True


# --------------------------------------------------------------------------- #
# read hook: array shaping + derived hypot (mocked component open).
# --------------------------------------------------------------------------- #


def _read_params(variable: str) -> dict[str, Any]:
    return {"variable": variable, "bbox": list(_FL), "forecast_hour": 3,
            "cycle_date": "2026-08-02", "cycle_hour": 5, "cycle": None}


def test_read_plain_variable_returns_array(monkeypatch):
    vals = np.array([[298.0, 299.0], [300.0, 301.0]], dtype="float32")
    monkeypatch.setattr(H, "_open_component_4326", lambda *a, **k: _da(vals))
    arr, transform, crs = H.read_slice(SPEC_FC, _read_params("2m_temperature"), timeout_s=90)
    assert np.allclose(arr, vals)
    assert "4326" in crs.to_string()


def test_read_derived_wind_speed_is_hypot(monkeypatch):
    u = np.array([[3.0, 0.0]], dtype="float32")
    v = np.array([[4.0, 5.0]], dtype="float32")
    seq = iter([_da(u), _da(v)])
    monkeypatch.setattr(H, "_open_component_4326", lambda *a, **k: next(seq))
    arr, _t, _c = H.read_slice(SPEC_FC, _read_params("10m_wind_speed"), timeout_s=90)
    assert np.allclose(arr, np.hypot(u, v))  # 5.0, 5.0


def test_read_all_nan_raises_empty(monkeypatch):
    vals = np.full((2, 2), np.nan, dtype="float32")
    monkeypatch.setattr(H, "_open_component_4326", lambda *a, **k: _da(vals))
    with pytest.raises(RouterError) as ei:
        H.read_slice(SPEC_SM, _read_params("near_surface_smoke"), timeout_s=90)
    assert ei.value.error_code == "HRRR_SMOKE_EMPTY"


# --------------------------------------------------------------------------- #
# LayerURI stamps + payload estimate.
# --------------------------------------------------------------------------- #


def test_units_and_style_by_param():
    p = _vp(SPEC_FC, bbox=list(_FL), variable="10m_wind_speed")
    layer = router.build_layer_uri(SPEC_FC, p, "s3://c/k.tif")
    assert layer.units == "m s-1" and layer.style_preset == "wind_speed"
    p2 = _vp(SPEC_FC, bbox=list(_FL), variable="2m_temperature")
    l2 = router.build_layer_uri(SPEC_FC, p2, "s3://c/k.tif")
    assert l2.units == "K" and l2.style_preset == "hrrr_2m_temperature"
    ps = _vp(SPEC_SM, bbox=list(_FL), variable="aerosol_optical_depth")
    ls = router.build_layer_uri(SPEC_SM, ps, "s3://c/k.tif")
    assert ls.units == "1" and ls.style_preset == "hrrr_smoke_aerosol_optical_depth"
    assert layer.layer_type == "raster" and layer.role == "primary"


def test_payload_estimate_matches_twin_formula():
    est = router.synthesize_payload_estimator(SPEC_FC)
    # twin: max(0.05, min(5.0, 5.0 * sq_deg / 2368))
    for bbox in [(-82.4, 26.3, -81.6, 26.9), (-124.0, 25.0, -66.0, 49.0)]:
        w, s, e, n = bbox
        sq = (e - w) * (n - s)
        expect = max(0.05, min(5.0, 5.0 * sq / (74.0 * 32.0)))
        assert est(bbox=list(bbox), variable="2m_temperature") == pytest.approx(expect, rel=1e-6)
