"""Offline tests for the ``elmfire_verification_elliptical_replication`` template
(ADR 0123, hazard-easy-four continuation #2).

Pins the agent-side elliptical-verification surface in ISOLATION (no solver, no
docker, no LANDFIRE/DEM fetch):

1. **Contract round-trip** -- ``ElmfireEllipseVerificationLayerURI`` is a
   ``FireSpreadLayerURI`` subtype carrying the verification triple. (no IO)
2. **Verifier (synthetic elliptical ToA)** -- a clean ellipse ToA recovers the
   ellipse geometry within the coarse tolerance (passed=True); a degenerate burn
   returns insufficient_perimeter. (numpy)
3. **Ellipse-overlay chart builder** -- a deterministic Vega-Lite spec; empty
   -> None. (no IO)
4. **Constant deck builder (no fetch)** -- authors an ALL-CONSTANT flat-grid deck
   with a GR2 fuel raster + grid identity across all rasters. (rasterio)
"""

from __future__ import annotations

import math

import numpy as np
import pytest


# ===========================================================================
# (1) Contract round-trip.
# ===========================================================================
def test_verification_layer_is_firespread_subtype():
    from trid3nt_contracts.elmfire_contracts import (
        ElmfireEllipseVerificationLayerURI,
        FireSpreadLayerURI,
    )

    layer = ElmfireEllipseVerificationLayerURI(
        layer_id="elmfire-verify-X",
        name="Fire arrival time (elliptical verification)",
        layer_type="raster",
        uri="s3://runs/X/elmfire_toa.tif",
        style_preset="continuous_fire_arrival_hr",
        role="primary",
        burned_area_km2=1.2,
        fire_arrival_max_hr=1.5,
        duration_hours=1.5,
        rmse_m=42.0,
        err_fraction=0.0375,
        correlation=0.986,
        corr_class="good",
        length_to_width_ratio=3.4,
        tolerance=0.08,
        passed=True,
    )
    assert isinstance(layer, FireSpreadLayerURI)
    assert layer.passed is True
    assert layer.corr_class == "good"


# ===========================================================================
# (2) Verifier on a synthetic elliptical ToA.
# ===========================================================================
def _synthetic_elliptical_toa(a0, b0, cx0, wind_from, n=160, cell=30.0):
    r0 = c0 = n // 2
    head = (wind_from + 180.0) % 360.0
    mth = np.deg2rad(90.0 - head)
    ux, uy = np.cos(mth), np.sin(mth)
    toa = np.full((n, n), np.nan)
    for r in range(n):
        for cc in range(n):
            x = (cc - c0) * cell
            y = (r0 - r) * cell
            u = x * ux + y * uy
            v = -x * uy + y * ux
            rho = math.sqrt(((u - cx0) / a0) ** 2 + (v / b0) ** 2)
            if rho <= 1.0:
                toa[r, cc] = rho * 1000.0 + 1.0
    return toa, (r0, c0), cell


def test_verifier_recovers_ellipse_and_passes():
    from trid3nt_server.workflows.elmfire.postprocess_elmfire import (
        verify_elliptical_replication,
    )

    toa, ign, cell = _synthetic_elliptical_toa(950.0, 700.0, 550.0, 270.0)
    res, overlay = verify_elliptical_replication(
        toa, cellsize_m=cell, ignition_rowcol=ign, wind_from_deg=270.0
    )
    assert res["passed"] is True
    assert res["err_fraction"] <= res["tolerance"]
    assert res["correlation"] >= 0.95
    assert res["corr_class"] in ("excellent", "good")
    # LW ratio recovered near the truth 950/700 ~ 1.357.
    assert 1.2 <= res["length_to_width_ratio"] <= 1.5
    assert len(overlay) > 0


def test_verifier_degenerate_burn_returns_insufficient():
    from trid3nt_server.workflows.elmfire.postprocess_elmfire import (
        verify_elliptical_replication,
    )

    toa = np.full((10, 10), np.nan)
    res, overlay = verify_elliptical_replication(
        toa, cellsize_m=30.0, ignition_rowcol=(5, 5), wind_from_deg=0.0
    )
    assert res["passed"] is False
    assert res["error"] == "insufficient_perimeter"
    assert overlay == []


# ===========================================================================
# (3) Ellipse-overlay chart builder.
# ===========================================================================
def test_ellipse_overlay_chart_spec():
    from trid3nt_contracts.chart_contracts import is_structurally_valid_vega_lite_spec
    from trid3nt_server.workflows.elmfire.postprocess_elmfire import (
        build_ellipse_overlay_chart_spec,
    )

    pts = [
        {"u_m": 100.0, "v_m": 0.0, "series": "numerical"},
        {"u_m": 98.0, "v_m": 2.0, "series": "ellipse"},
    ]
    spec = build_ellipse_overlay_chart_spec(pts)
    assert is_structurally_valid_vega_lite_spec(spec)
    assert spec["encoding"]["color"]["field"] == "series"
    assert build_ellipse_overlay_chart_spec([]) is None


# ===========================================================================
# (4) Constant deck builder (no fetch).
# ===========================================================================
def test_constant_verification_deck(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from trid3nt_contracts.elmfire_contracts import ElmfireRunArgs
    from trid3nt_server.workflows.elmfire.run_elmfire import (
        build_constant_verification_deck,
    )

    lon, lat = -98.5, 38.5
    half = (4000.0 / 2.0) / 111_320.0  # small 4 km domain for a fast test
    half_lon = half / math.cos(math.radians(lat))
    run_args = ElmfireRunArgs(
        bbox=(lon - half_lon, lat - half, lon + half_lon, lat + half),
        ignition_lonlat=(lon, lat),
        wind_speed_mph=15.0,
        wind_dir_deg=270.0,
        fuel_moisture="dry",
        duration_hours=1.0,
        cellsize_m=30.0,
    )
    manifest = build_constant_verification_deck(run_args, tmp_path, fuel_model=102)
    inputs = tmp_path / "inputs"
    # All 15 deck rasters + namelist written.
    tifs = sorted(p.name for p in inputs.glob("*.tif"))
    assert "fbfm40.tif" in tifs and "ws.tif" in tifs and "dem.tif" in tifs
    assert (inputs / "elmfire.data").exists()
    # fbfm40 is the constant GR2 fuel model 102.
    with rasterio.open(inputs / "fbfm40.tif") as ds:
        arr = ds.read(1)
        assert int(arr.min()) == 102 and int(arr.max()) == 102
    # slope raster is flat (0).
    with rasterio.open(inputs / "slp.tif") as ds:
        assert int(ds.read(1).max()) == 0
    # grid identity is asserted inside the builder; the manifest echoes the grid.
    assert manifest["grid"]["epsg"] == 5070
