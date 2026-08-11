"""Offline tests for the SCHISM PaHM storm-surge landing (ADR 0217).

No docker / no network: exercises the pure Holland-1980 sflux physics + the surge
deck authoring (file inventory + the load-bearing param.nml / bctides toggles).
The live solve-through-the-image proof is the direct-harness drive
(scripts/run_schism_surge_direct.py), not a unit test.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path

import numpy as np
import pytest
from scipy.spatial import Delaunay

from trid3nt_server.agent.workflows.schism import deck_authoring as D
from trid3nt_server.agent.workflows.schism import holland_sflux as H
from trid3nt_server.agent.workflows.schism.pahm_surge.pahm_surge import (
    _emit_track_overlay,
)


def _ike_track() -> list[H.TrackFix]:
    return [
        H.TrackFix(0.0, -91.5, 26.6, 94800.0, 48.9, 92600.0, 100800.0),
        H.TrackFix(12.0, -93.6, 28.1, 95000.0, 41.2, 101_860.0, 100800.0),
        H.TrackFix(24.0, -94.7, 29.3, 95200.0, 41.2, 92600.0, 100800.0),
        H.TrackFix(30.0, -95.4, 30.5, 96400.0, 30.9, 111_120.0, 100800.0),
    ]


def _demo_mesh():
    gx, gy = np.meshgrid(np.linspace(-95.4, -94.4, 12), np.linspace(28.8, 29.6, 11))
    pts = np.column_stack([gx.ravel(), gy.ravel()]).astype(float)
    cells = Delaunay(pts).simplices.astype(np.int64)
    depths = (2.0 + (29.6 - pts[:, 1]) * 30.0).astype(float)
    return pts, cells, depths


def test_holland_B_physical_range():
    # A deep pressure deficit with a strong Vmax lands inside the [1.0, 2.5] clamp.
    b = H.holland_B(vmax_ms=50.0, dp_pa=6000.0)
    assert 1.0 <= b <= 2.5
    # Degenerate inputs return the safe default, never a divide-by-zero.
    assert H.holland_B(0.0, 0.0) == pytest.approx(1.3)


def test_holland_profile_peaks_near_rmw_and_calm_eye():
    pc, pn, rmw, lat = 95000.0, 101300.0, 46000.0, 29.3
    b = H.holland_B(49.4, pn - pc)
    v_eye, _ = H.holland_profile(500.0, pc, pn, rmw, b, lat)
    v_rmw, p_rmw = H.holland_profile(rmw, pc, pn, rmw, b, lat)
    v_far, p_far = H.holland_profile(300_000.0, pc, pn, rmw, b, lat)
    assert v_eye < 5.0                 # calm eye
    assert v_rmw > v_far > 0.0         # wind peaks near Rmw, decays outward
    assert p_rmw < p_far               # pressure lowest near the center
    assert pc <= p_rmw <= pn


def test_write_sflux_air_structure_and_extends_past_run(tmp_path):
    import netCDF4

    field = H.write_sflux_air(
        tmp_path / "sflux", _ike_track(), (-95.6, 28.6, -94.0, 30.0),
        base_date=(2008, 9, 12, 6), sim_days=1.0, cadence_hr=1.0, tail_hours=6.0,
    )
    nc = tmp_path / "sflux" / "sflux_air_1.0001.nc"
    assert nc.exists()
    assert (tmp_path / "sflux" / "sflux_inputs.txt").exists()
    assert field.peak_wind_ms > 20.0            # a real hurricane wind field
    assert field.min_pressure_pa < 100_000.0    # sub-ambient core
    with netCDF4.Dataset(nc) as ds:
        for v in ("lon", "lat", "time", "uwind", "vwind", "prmsl", "stmp", "spfh"):
            assert v in ds.variables, v
        assert list(ds.variables["time"].base_date) == [2008, 9, 12, 6]
        t = np.asarray(ds.variables["time"][:])
        # sflux MUST extend past the run end (else SCHISM aborts the last step).
        assert t[-1] * 24.0 >= 1.0 * 24.0 + 5.9
        u = np.asarray(ds.variables["uwind"][:])
        assert u.shape[1:] == (field.n_lat, field.n_lon)


def test_author_pahm_surge_deck_inventory_and_toggles(tmp_path):
    pts, cells, depths = _demo_mesh()
    res = D.author_pahm_surge_deck(
        tmp_path / "case", track=_ike_track(), mesh_bbox=(-95.4, 28.8, -94.4, 29.6),
        base_date=(2008, 9, 12, 6), points=pts, cells=cells, depths=depths,
        sim_days=1.0, open_boundary_side="south", dt_s=120.0,
    )
    case = tmp_path / "case"
    for name in ("hgrid.gr3", "hgrid.ll", "vgrid.in", "param.nml", "bctides.in",
                 "drag.gr3", "windrot_geo2proj.gr3", "station.in",
                 "sflux/sflux_air_1.0001.nc", "sflux/sflux_inputs.txt"):
        assert (case / name).exists(), name
    assert res["open_node_count"] > 0

    param = (case / "param.nml").read_text()
    # The load-bearing surge toggles (any regression here silently mis-forces).
    assert "nws = 2" in param
    assert "iwind_form = -1" in param
    # No ACTIVE nrampwind line (invalid &OPT member -> aborts init); the QA
    # template's commented `!  nrampwind` is inert and may remain.
    active = [ln for ln in param.splitlines()
              if "nrampwind" in ln and not ln.lstrip().startswith("!")]
    assert not active, active
    assert "start_year = 2008" in param

    # hgrid.gr3 is PROJECTED to metres; hgrid.ll reconstructs lon/lat node-for-node.
    g = (case / "hgrid.gr3").read_text().splitlines()
    ll = (case / "hgrid.ll").read_text().splitlines()
    assert g[1].split()[:2] == ll[1].split()[:2]        # same nelem/nnode
    gx = float(g[2].split()[1])
    assert abs(gx) > 100.0                                # metres, not degrees
    llx = float(ll[2].split()[1])
    assert -180.0 <= llx <= 180.0                         # lon/lat degrees

    # Still-water boundary (iettype=2), no tidal constituents -> surge is pure forcing.
    bctides = (case / "bctides.in").read_text()
    assert "0 nbfr" in bctides
    assert " 2 0 0 0" in bctides


class _FakeEmitter:
    """Minimal ``PipelineEmitter`` stand-in: captures ``add_loaded_layer`` calls."""

    def __init__(self) -> None:
        self.loaded: list[object] = []

    async def add_loaded_layer(self, layer) -> None:  # noqa: ANN001
        self.loaded.append(layer)


def test_emit_track_overlay_layer_uri_validates(monkeypatch):
    """Regression for the daemon-observed skip: ``LayerURI`` for the best-track
    overlay was missing the required ``style_preset`` field, so construction
    raised a pydantic ``ValidationError`` that the caller's best-effort
    try/except silently swallowed (log-only skip, no layer ever reached the
    Case). This exercises the real ``_emit_track_overlay`` code path end to
    end (S3 upload stubbed) and asserts the overlay actually reaches the
    emitter, fully validated, instead of raising loudly here if the
    construction ever breaks again."""
    from trid3nt_server.agent.tools.simulation.solver import solver as _solver

    monkeypatch.setattr(_solver, "_get_runs_bucket", lambda: "test-runs-bucket")

    class _FakeS3:
        def put_object(self, **kwargs):  # noqa: ANN003
            return {}

    monkeypatch.setattr(_solver, "_get_s3_client", lambda: _FakeS3())

    emitter = _FakeEmitter()
    asyncio.run(_emit_track_overlay(emitter, _ike_track(), "Ike"))

    assert len(emitter.loaded) == 1
    layer = emitter.loaded[0]
    assert layer.layer_type == "vector"
    assert layer.style_preset == "storm_track"
    assert layer.role == "context"
    assert layer.crs_authid == "EPSG:4326"
    assert layer.uri.startswith("s3://test-runs-bucket/")
