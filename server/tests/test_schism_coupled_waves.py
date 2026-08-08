"""Offline gates for the ``schism_coupled_waves`` archetype (SCHISM+WWM+GOTM,
ADR 0126/0129): deck-staging determinism, entrypoint variant selection, the
Hs/Tp postprocess + cross-shore V&V (synthetic UGRID), and the registration pins.

No docker, no MinIO, no live solve -- the live coupled acceptance (the full Duck
case + the cross-shore Hs/Tp chart) rides the worker image + the run harness. The
postprocess/V&V numbers here are computed from a synthetic out2d the same reader
consumes for the real coupled run.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from trid3nt_contracts.schism_contracts import (
    SCHISM_ARCHETYPES,
    SCHISM_WAVE_STYLE_PRESET,
    SchismWaveLayerURI,
)


# --------------------------------------------------------------------------- #
# 1. Contract shape + archetype registration
# --------------------------------------------------------------------------- #
def test_coupled_waves_archetype_registered():
    assert "coupled_waves" in SCHISM_ARCHETYPES
    assert SCHISM_WAVE_STYLE_PRESET == "continuous_flood_depth"


def test_wave_layer_uri_shape():
    w = SchismWaveLayerURI(
        layer_id="schism-hs-max-x", name="Hs", layer_type="raster",
        uri="s3://b/hs.tif", role="primary", units="m",
        style_preset=SCHISM_WAVE_STYLE_PRESET,
        hs_max_m=2.24, hs_mean_m=1.04, tp_max_s=10.8, offshore_hs_m=2.1,
        n_nodes=17054, sim_hours=4.0,
        vv_n_gauges=12, vv_hs_rmse_m=0.18, vv_hs_bias_m=-0.05, vv_hs_corr=0.93,
    )
    assert w.hs_max_m == pytest.approx(2.24)
    assert w.tp_max_s == pytest.approx(10.8)
    assert w.vv_hs_corr == pytest.approx(0.93)
    # negative bias is allowed (a signed field); RMSE is ge=0 constrained
    with pytest.raises(Exception):
        SchismWaveLayerURI(layer_id="x", name="x", layer_type="raster", uri="s3://b/x",
                           role="primary", style_preset=SCHISM_WAVE_STYLE_PRESET,
                           hs_max_m=1.0, vv_hs_rmse_m=-1.0)


# --------------------------------------------------------------------------- #
# 2. Deck staging: determinism + the ADR 0126 transforms
# --------------------------------------------------------------------------- #
def test_stage_wwm_duck_deck_deterministic_and_transformed(tmp_path: Path):
    from trid3nt_server.agent.workflows.schism import deck_authoring as D

    d1 = tmp_path / "r1"
    d2 = tmp_path / "r2"
    files1, nc1, ns1 = D.stage_wwm_duck_deck(d1, sim_hours=4.0)
    files2, nc2, ns2 = D.stage_wwm_duck_deck(d2, sim_hours=4.0)
    assert (nc1, ns1) == (nc2, ns2) == (4, 4)
    # identical staged param.nml across two runs (deterministic)
    p1 = (d1 / "param.nml").read_text()
    p2 = (d2 / "param.nml").read_text()
    assert p1 == p2

    # master-only vars stripped (an ACTIVE assignment would abort the v5.11.0
    # binary; a mention inside a comment is harmless).
    import re
    active = [ln for ln in p1.splitlines()
              if re.match(r"\s*(nbins_veg_vert|nmarsh_types|radflag)\s*=", ln, re.I)]
    assert active == []
    # itur=3 KEPT (the faithful GOTM closure)
    assert "itur = 3" in p1
    # output trim: elevation + Hs(1) + Tp(9) ON, the rest OFF
    assert "iof_hydro(1) = 1" in p1
    assert "iof_wwm(1)  = 1" in p1 or "iof_wwm(1) = 1" in p1
    assert "iof_wwm(9) = 1" in p1
    assert "iof_wwm(2)  = 0" in p1 or "iof_wwm(2) = 0" in p1
    assert "iof_hydro(26) = 0" in p1
    # GOTM/WWM file-name reconciliations present
    assert (d1 / "gotmturb.nml").exists()
    assert (d1 / "hgrid_WWM.gr3").exists()
    # the bundled wave-spectrum boundary is staged
    assert (d1 / "DUCK94_wave_spectra_8m_array.nc").exists()


def test_stage_wwm_duck_deck_sim_window(tmp_path: Path):
    from trid3nt_server.agent.workflows.schism import deck_authoring as D

    files, _, _ = D.stage_wwm_duck_deck(tmp_path, sim_hours=1.0)
    param = (tmp_path / "param.nml").read_text()
    # rnday = 1h/24 ~ 0.041667
    assert "rnday = 0.041667" in param


def test_fixture_sha_pins_present():
    from trid3nt_server.agent.workflows.schism import deck_authoring as D

    fx = D.wwm_duck_fixture_dir()
    sums = (fx / "SHA256SUMS").read_text()
    assert "hgrid.gr3" in sums and "DUCK94_wave_spectra_8m_array.nc" in sums
    assert (fx / "Data" / "timeseries_data_1010_to_1410_004Hz_025Hz.mat").exists()


# --------------------------------------------------------------------------- #
# 3. Entrypoint variant selection (the ADR 0126 glob fix)
# --------------------------------------------------------------------------- #
def _load_entrypoint():
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "services" / "workers" / "schism" / "entrypoint.py"
        if cand.exists():
            spec = importlib.util.spec_from_file_location("_schism_entry", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod
    raise AssertionError("entrypoint.py not found")


def test_variant_selection_wwm_vs_full(tmp_path: Path, monkeypatch):
    ep = _load_entrypoint()
    bindir = tmp_path
    # both binaries present; "wwm" must NOT resolve to the COSINE full-monty
    (bindir / "pschism_WWM_COSINE_ICM_FIB_SED_TVD-VL").write_text("x")
    (bindir / "pschism_WWM_GOTM_TVD-VL").write_text("x")
    (bindir / "pschism_TVD-VL").write_text("x")
    monkeypatch.setattr(ep, "BIN_DIR", bindir)

    assert ep._resolve_exe("wwm").name == "pschism_WWM_GOTM_TVD-VL"
    assert ep._resolve_exe("full").name == "pschism_WWM_COSINE_ICM_FIB_SED_TVD-VL"
    assert ep._resolve_exe("hydro").name == "pschism_TVD-VL"


# --------------------------------------------------------------------------- #
# 4. Wave postprocess + cross-shore V&V on a synthetic UGRID
# --------------------------------------------------------------------------- #
def _write_synth_wave_out2d(nc: Path, N: int = 40, T: int = 5) -> np.ndarray:
    """A cross-shore Hs field shoaling from ~2 m offshore to ~0 at the beach."""
    from netCDF4 import Dataset

    xs = np.linspace(0.0, 900.0, N)  # xFRF cross-shore (m), 0=beach, 900=offshore
    ys = np.full(N, 930.0)
    depth = np.linspace(0.2, 8.0, N)  # deep offshore
    # Hs grows with depth (offshore high, shoals to ~0); mild time variation
    hs_base = 0.2 + 0.25 * depth  # ~2.2 m at 8 m depth
    hs = hs_base[None, :] * (1.0 + 0.02 * np.sin(np.linspace(0, np.pi, T))[:, None])
    tp = np.full((T, N), 9.5)
    with Dataset(str(nc), "w") as ds:
        ds.createDimension("node", N)
        ds.createDimension("time", T)
        ds.createVariable("SCHISM_hgrid_node_x", "f8", ("node",))[:] = xs
        ds.createVariable("SCHISM_hgrid_node_y", "f8", ("node",))[:] = ys
        ds.createVariable("depth", "f8", ("node",))[:] = depth
        ds.createVariable("time", "f8", ("time",))[:] = np.linspace(0, 4 * 3600, T)
        ds.createVariable("sigWaveHeight", "f8", ("time", "node"))[:] = hs
        ds.createVariable("peakPeriod", "f8", ("time", "node"))[:] = tp
    return hs


def test_read_out2d_waves(tmp_path: Path):
    from trid3nt_server.agent.workflows.schism import postprocess_schism as PP

    nc = tmp_path / "out2d_1.nc"
    hs = _write_synth_wave_out2d(nc)
    out = PP.read_out2d_waves(nc)
    assert out["is_geographic"] is False  # planar FRF coords
    assert out["n_nodes"] == 40
    assert out["hs_max"].max() == pytest.approx(hs.max(), abs=1e-4)
    assert out["node_depth"] is not None and out["node_depth"].max() == pytest.approx(8.0)


def test_read_out2d_waves_empty_raises(tmp_path: Path):
    from trid3nt_server.agent.workflows.schism import postprocess_schism as PP
    from netCDF4 import Dataset

    nc = tmp_path / "bad.nc"
    with Dataset(str(nc), "w") as ds:
        ds.createDimension("node", 3)
        ds.createVariable("SCHISM_hgrid_node_x", "f8", ("node",))[:] = [0, 1, 2]
    with pytest.raises(PP.PostprocessSchismError):
        PP.read_out2d_waves(nc)


def test_verify_cross_shore_waves(tmp_path: Path):
    """Model transect vs a synthetic gauge .mat -> RMSE/bias/corr the composer cites."""
    from scipy.io import savemat

    from trid3nt_server.agent.workflows.schism import postprocess_schism as PP

    nc = tmp_path / "out2d_1.nc"
    _write_synth_wave_out2d(nc, N=60)
    # gauges sampled along the same transect; measured ~ model + small noise
    xg = np.linspace(50.0, 850.0, 8)
    depth_g = np.interp(xg, [0, 900], [0.2, 8.0])
    hs_meas = (0.2 + 0.25 * depth_g) * 1.03  # +3% high (a real gauge bias)
    tp_meas = np.full_like(xg, 9.3)
    mat = tmp_path / "gauges.mat"
    ntime = 6  # the real transect .mat is (ntime, ngauge) with a `time` key
    savemat(str(mat), {
        "time": np.linspace(0, 4 * 3600, ntime).reshape(-1, 1),
        "xPTs": np.tile(xg.reshape(1, -1), (ntime, 1)),   # (ntime, ngauge)
        "yPTs": np.full((ntime, xg.size), 930.0),
        "Hm0_nlin": np.tile(hs_meas.reshape(1, -1), (ntime, 1)),
        "Tp_nlin": np.tile(tp_meas.reshape(1, -1), (ntime, 1)),
    })
    vv = PP.verify_cross_shore_waves(nc, mat)
    assert vv is not None
    assert vv["n_gauges"] == 8
    assert vv["hs_corr"] > 0.98            # the shoaling trend is reproduced
    assert vv["hs_rmse_m"] < 0.25          # close, small systematic bias
    assert vv["offshore_hs_obs_m"] is not None
    assert len(vv["gauges"]) == 8


# --------------------------------------------------------------------------- #
# 5. Registration pins + corpus retrieval seed
# --------------------------------------------------------------------------- #
def test_coupled_waves_registered_and_solver_wired():
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    import trid3nt_server.agent.workflows  # noqa: F401 -- trigger solver reg
    from trid3nt_server.agent.categories import PRIMARY_CATEGORY
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        SOLVER_WORKFLOW_REGISTRY, LOCAL_SOLVER_SPEC_REGISTRY,
    )

    assert "schism_coupled_waves" in TOOL_REGISTRY
    md = TOOL_REGISTRY["schism_coupled_waves"].metadata
    assert md.engine == "schism" and md.tier == "template"
    assert PRIMARY_CATEGORY.get("schism_coupled_waves") == "simulation_modeling"
    assert SOLVER_WORKFLOW_REGISTRY.get("schism_coupled_waves") == "local-docker"
    assert "schism_coupled_waves" in LOCAL_SOLVER_SPEC_REGISTRY


def test_coupled_waves_corpus_seed_present():
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = (parent / "server" / "src" / "trid3nt_server" / "agent" / "workflows"
                / "schism" / "coupled_waves" / "corpus.yaml")
        if cand.exists():
            text = cand.read_text()
            assert "schism_coupled_waves:" in text
            assert "coupled wave-current" in text
            return
    raise AssertionError("coupled_waves corpus.yaml not found")
