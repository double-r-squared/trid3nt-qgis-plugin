"""Tests for the ``modflow_package_validation`` engine template (ADR 0153).

Two tiers, mirroring the modflow archetype test discipline:

- DECK-AUTHORING asserts (always run, no mf6): each ``build_*`` writes a runnable
  mf6 deck (mfsim.nam + the package files) and the case registry is well-formed.
- REAL-SOLVE V&V asserts (skipped when no mf6 binary is resolvable): run each case
  through mf6 and assert the computed-vs-reference deltas + the Newton contrast.
  The repo ships ``bin/mf6`` so these run in the offline slice.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from trid3nt_server.mesh import modflow_package_validation as core

_HAS_MF6 = core.resolve_mf6_binary() is not None
_needs_mf6 = pytest.mark.skipif(not _HAS_MF6, reason="no mf6 binary resolvable")


# --------------------------------------------------------------------------- #
# Case registry + deck authoring (no mf6).
# --------------------------------------------------------------------------- #


def test_case_registry_is_wellformed():
    assert set(core.VALIDATION_CASES) == {
        "newton_dry_rewet", "maw_crossaquifer", "hfb_barrier",
        "prt_capture_zone", "henry_saltwater", "sfr_stream_depletion",
        "mvr_routing",
    }
    for name, meta in core.VALIDATION_CASES.items():
        assert meta.case == name
        assert meta.package
        # every case cites a published/analytical reference source.
        assert any(tok in meta.reference_source
                   for tok in ("modflow6-", "mf6-examples", "Grubb", "Henry",
                               "Glover", "gwf-mvr"))
        assert meta.question


def test_build_newton_authors_two_formulations():
    sims, ws, botm = core.build_newton_dry_rewet()
    assert set(sims) == {"newton", "standard"}
    # the staircase base descends 20 -> 0 in 5 m steps.
    assert botm[0] == 20.0 and botm[-1] == 0.0
    assert sorted(set(botm.tolist())) == [0.0, 5.0, 10.0, 15.0, 20.0]
    # NEWTON on the newton model's namefile, absent on the standard one.
    nwt_nam = os.path.join(ws, "newton", "newton.nam")
    std_nam = os.path.join(ws, "standard", "standard.nam")
    assert "NEWTON" in open(nwt_nam).read().upper()
    assert "NEWTON" not in open(std_nam).read().upper()


def test_build_maw_authors_two_connection_well():
    sim, ws, analytical, params = core.build_maw_crossaquifer()
    # transmissivity-weighted analytical between the two aquifer heads.
    assert params["h1"] < analytical < params["h2"]
    assert os.path.isfile(os.path.join(ws, "maw.maw"))  # MAW package written
    maw_txt = open(os.path.join(ws, "maw.maw")).read().upper()
    assert "THIEM" in maw_txt


def test_build_hfb_authors_grid_sweep_with_barrier():
    sims, ws, analytical_q = core.build_hfb_barrier()
    assert set(sims) == set(core.HFB_GRID_NCOLS)
    assert analytical_q == pytest.approx(9.0e-4, rel=1e-9)  # HYDCHR*area*dh
    for ncol in core.HFB_GRID_NCOLS:
        hfb_file = os.path.join(ws, f"n{ncol}", f"hfb{ncol}.hfb")
        assert os.path.isfile(hfb_file), hfb_file


def test_build_prt_gwf_authors_well_river_deck():
    sim, ws, sub = core.build_prt_gwf()
    # WEL + CHD (inflow) + RIV (discharge) all written on the confined grid.
    assert os.path.isfile(os.path.join(sub, "gwf.wel"))
    assert os.path.isfile(os.path.join(sub, "gwf.riv"))
    assert os.path.isfile(os.path.join(sub, "gwf.chd"))
    # Grubb analytical is a pure function of Q, K, b, i -> a fixed positive value.
    assert core.PRT_STAGNATION_M > 0
    assert core.PRT_CAPTURE_WIDTH_ASYMPTOTE_M > core.PRT_STAGNATION_M


def test_grubb_capture_halfwidth_is_bounded_by_asymptote():
    # the finite-distance half-width is below Q/(2U) and grows toward it.
    asymptote_half = core.PRT_CAPTURE_WIDTH_ASYMPTOTE_M / 2.0
    near = core._grubb_capture_halfwidth(590.0)
    far = core._grubb_capture_halfwidth(60000.0)
    assert 0 < near < far < asymptote_half + 1e-6
    assert far == pytest.approx(asymptote_half, rel=0.02)


def test_build_henry_authors_buy_gwt_pair():
    sim, ws = core.build_henry_saltwater()
    # BUY on the flow model + a GWT transport model + the GWF-GWT exchange.
    assert os.path.isfile(os.path.join(ws, "flow.buy"))
    assert os.path.isfile(os.path.join(ws, "trans.nam"))
    assert os.path.isfile(os.path.join(ws, "henry.gwfgwt"))
    buy_txt = open(os.path.join(ws, "flow.buy")).read().upper()
    assert "DENSEREF" in buy_txt


def test_glover_analytical_is_monotone_and_bounded():
    # the closed-form depletion fraction rises with time, in (0, 1).
    fracs = [core.glover_depletion_fraction(t) for t in core.GLOVER_TIMES_D]
    assert all(0.0 < f < 1.0 for f in fracs)
    assert all(fracs[i] < fracs[i + 1] for i in range(len(fracs) - 1))
    assert core.GLOVER_A_M == 300.0


def test_build_glover_sfr_authors_pump_and_baseline_decks():
    for pump in (True, False):
        sim, sub, name = core.build_glover_sfr(pump=pump)
        assert os.path.isfile(os.path.join(sub, f"{name}.sfr"))  # SFR package
        assert os.path.isfile(os.path.join(sub, f"{name}.wel"))
        wel_txt = open(os.path.join(sub, f"{name}.wel")).read()
        # the pumping deck writes a nonzero rate; the baseline writes zero
        # (flopy writes the rate in scientific notation, e.g. -4.00000000E+02).
        assert ("4.00000000E+02" in wel_txt) is pump


def test_build_mvr_routing_authors_uzf_drn_sfr_mover():
    sim, ws, name = core.build_mvr_routing()
    for ext in ("sfr", "drn", "uzf", "mvr"):
        assert os.path.isfile(os.path.join(ws, f"{name}.{ext}")), ext
    mvr_txt = open(os.path.join(ws, f"{name}.mvr")).read().upper()
    # DRN + UZF are movers routing into SFR.
    assert "DRN-1" in mvr_txt and "UZF-1" in mvr_txt and "SFR-1" in mvr_txt


# --------------------------------------------------------------------------- #
# Real mf6 solve V&V (gated on a resolvable mf6 binary).
# --------------------------------------------------------------------------- #


@_needs_mf6
def test_newton_dry_rewet_contrast():
    r = core.run_validation_case("newton_dry_rewet")
    assert r.validated is True
    assert r.metrics["newton_dry_cells"] == 0
    assert r.metrics["standard_dry_cells"] > 0  # standard collapses cells to dry
    assert r.metrics["newton_monotone_descending"] is True
    assert r.metrics["newton_head_min_m"] == pytest.approx(10.0, abs=1e-3)
    assert r.metrics["newton_head_max_m"] == pytest.approx(23.0, abs=1e-3)


@_needs_mf6
def test_maw_crossaquifer_matches_sokol_analytical():
    r = core.run_validation_case("maw_crossaquifer")
    assert r.validated is True
    assert r.computed_value == pytest.approx(r.reference_value, abs=1e-4)
    assert r.relative_error is not None and r.relative_error < 1e-6


@_needs_mf6
def test_hfb_barrier_flux_grid_independent():
    r = core.run_validation_case("hfb_barrier")
    assert r.validated is True
    # flux matches the HYDCHR analytical at the finest grid ...
    assert r.computed_value == pytest.approx(r.reference_value, rel=1e-2)
    # ... and barely moves across grid refinements (the HFB point).
    assert r.metrics["max_relative_grid_variation"] < 1e-3
    fluxes = r.metrics["flux_by_ncol_m3_d"]
    assert set(fluxes) == {str(n) for n in core.HFB_GRID_NCOLS}


@_needs_mf6
def test_prt_capture_zone_matches_grubb_analytical():
    r = core.run_validation_case("prt_capture_zone", direction="backward")
    assert r.validated is True
    # backward stagnation distance vs Grubb Q/(2*pi*U), tight.
    assert r.computed_value == pytest.approx(r.reference_value, rel=0.10)
    m = r.metrics
    # forward capture band vs the Grubb finite-distance width.
    assert m["capture_width_relative_error"] < 0.20
    assert m["forward_all_terminated"] is True
    assert m["forward_captured"] > 0
    # every backward particle traces up-gradient to the inflow boundary.
    assert m["backward_terminate_upgradient"] == m["backward_particles"]


@_needs_mf6
def test_prt_direction_knob_selects_framing_not_physics():
    fwd = core.run_validation_case("prt_capture_zone", direction="forward")
    bwd = core.run_validation_case("prt_capture_zone", direction="backward")
    assert fwd.metrics["direction_shown"] == "forward"
    assert bwd.metrics["direction_shown"] == "backward"
    # the underlying V&V numbers are identical (both directions always solved).
    assert fwd.computed_value == pytest.approx(bwd.computed_value, rel=1e-6)


@_needs_mf6
def test_prt_bad_direction_raises():
    with pytest.raises(core.ModflowValidationError):
        core.run_validation_case("prt_capture_zone", direction="sideways")


@_needs_mf6
def test_henry_saltwater_reproduces_wedge():
    r = core.run_validation_case("henry_saltwater")
    assert r.validated is True
    m = r.metrics
    assert m["bottom_salinity_monotone_to_sea"] is True
    assert m["fresh_top_inland"] is True
    assert m["salt_bottom_seaward"] is True
    # 0.5-isochlor toe at an intermediate inland penetration (a real wedge).
    assert 0.0 < m["toe_penetration_from_sea_m"] < m["domain_length_m"]
    assert r.relative_error < 0.30


@_needs_mf6
def test_sfr_stream_depletion_matches_glover():
    r = core.run_validation_case("sfr_stream_depletion")
    assert r.validated is True
    m = r.metrics
    # the SFR depletion curve tracks Glover erfc over the resolved window ...
    assert m["max_relative_error_resolved"] < 0.15
    assert m["monotone_increasing"] is True
    assert m["pump_converged"] and m["baseline_converged"]
    # ... and both the mf6 and Glover late-time fractions are a real capture (>0.5).
    assert r.computed_value > 0.5 and r.reference_value > 0.5
    assert len(m["depletion_fraction_mf6"]) == len(core.GLOVER_TIMES_D)


@_needs_mf6
def test_mvr_routing_conserves_mass():
    r = core.run_validation_case("mvr_routing")
    assert r.validated is True
    m = r.metrics
    # SFR receives EXACTLY the sum drawn from the two providers (mover invariant).
    assert m["sfr_received_from_mvr_m3_d"] == pytest.approx(
        m["providers_total_m3_d"], rel=1e-6)
    assert m["conservation_delta_m3_d"] < 1e-3
    # both providers actually route (rejected UZF + DRN discharge are nonzero).
    assert m["uzf_rejected_to_mvr_m3_d"] > 0.0
    assert m["drn_discharge_to_mvr_m3_d"] > 0.0


@_needs_mf6
def test_unknown_case_raises():
    with pytest.raises(core.ModflowValidationError):
        core.run_validation_case("not_a_case")


# --------------------------------------------------------------------------- #
# Temp-workspace ownership: every mf_vv_ directory has a reaper (no mf6).
# --------------------------------------------------------------------------- #


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """Point the module's whole temp surface at an isolated directory.

    ``tempfile.tempdir`` backs both ``gettempdir`` (what the sweep walks) and
    ``mkdtemp`` (where the factory mints), so one patch redirects both and the
    real system temp root is never swept by these tests.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _age(path: Path, seconds_old: float) -> None:
    stamp = time.time() - seconds_old
    os.utime(path, (stamp, stamp))


def _stale() -> float:
    return core._STALE_WORKSPACE_AGE_S + 60.0


def test_factory_reaps_stale_workspaces_and_spares_fresh_and_foreign(temp_root):
    stale = temp_root / "mf_vv_glover_stale"
    fresh = temp_root / "mf_vv_glover_fresh"
    foreign = temp_root / "someone_elses_stale_dir"
    for d in (stale, fresh, foreign):
        d.mkdir()
        (d / "mfsim.nam").write_text("deck")
    _age(stale, _stale())
    _age(foreign, _stale())

    ws = core._new_ws("probe")
    try:
        assert not stale.exists()          # leaked past the threshold: reaped
        assert fresh.is_dir()              # inside the threshold: a live solve
        assert foreign.is_dir()            # not our prefix: never touched
        assert Path(ws).parent == temp_root
        assert Path(ws).name.startswith("mf_vv_probe_")
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_reaper_spares_a_live_workspace_past_the_stale_age(temp_root):
    with core._workspace("probe") as ws:
        _age(Path(ws), _stale())
        core._reap_stale_workspaces()
        assert Path(ws).is_dir()


def test_reaper_swallows_an_undeletable_entry(temp_root, monkeypatch):
    blocked = temp_root / "mf_vv_blocked_x"
    other = temp_root / "mf_vv_other_x"
    for d in (blocked, other):
        d.mkdir()
        _age(d, _stale())

    real_rmtree = shutil.rmtree

    def _refusing(path, *args, **kwargs):
        if Path(path).name == blocked.name:
            raise PermissionError(13, "not this process's directory")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(core.shutil, "rmtree", _refusing)

    core._reap_stale_workspaces()  # must not propagate

    assert blocked.is_dir()        # the refusal left it alone
    assert not other.exists()      # and did not abort the rest of the sweep


def test_workspace_context_removes_its_own_directory(temp_root):
    with core._workspace("probe") as ws:
        (Path(ws) / "mfsim.nam").write_text("deck")
        assert Path(ws).is_dir()
        assert ws in core._LIVE_WORKSPACES
    assert not Path(ws).exists()
    assert ws not in core._LIVE_WORKSPACES


def test_workspace_context_removes_its_own_directory_on_raise(temp_root):
    held = {}
    with pytest.raises(RuntimeError):
        with core._workspace("probe") as ws:
            held["ws"] = ws
            (Path(ws) / "mfsim.nam").write_text("deck")
            raise RuntimeError("solve blew up")
    assert not Path(held["ws"]).exists()
    assert held["ws"] not in core._LIVE_WORKSPACES


def test_scoped_workspace_passes_a_reaped_workspace_to_the_solve(temp_root):
    seen = {}

    @core._scoped_workspace("probe")
    def _fake_solve(ws, *, knob):
        seen["ws"] = ws
        seen["knob"] = knob
        (Path(ws) / "mfsim.nam").write_text("deck")
        return "solved"

    assert _fake_solve(knob=3) == "solved"
    assert seen["knob"] == 3
    assert not Path(seen["ws"]).exists()


@_needs_mf6
def test_solved_case_leaves_no_workspace_behind():
    root = Path(tempfile.gettempdir())
    before = set(root.glob("mf_vv_*"))
    core.run_validation_case("maw_crossaquifer")
    assert set(root.glob("mf_vv_*")) <= before
