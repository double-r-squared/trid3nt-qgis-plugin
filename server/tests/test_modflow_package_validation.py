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

import pytest

from trid3nt_server.agent.mesh import modflow_package_validation as core

_HAS_MF6 = core.resolve_mf6_binary() is not None
_needs_mf6 = pytest.mark.skipif(not _HAS_MF6, reason="no mf6 binary resolvable")


# --------------------------------------------------------------------------- #
# Case registry + deck authoring (no mf6).
# --------------------------------------------------------------------------- #


def test_case_registry_is_wellformed():
    assert set(core.VALIDATION_CASES) == {
        "newton_dry_rewet", "maw_crossaquifer", "hfb_barrier",
        "prt_capture_zone", "henry_saltwater",
    }
    for name, meta in core.VALIDATION_CASES.items():
        assert meta.case == name
        assert meta.package
        # every case cites a published/analytical reference source.
        assert any(tok in meta.reference_source
                   for tok in ("modflow6-", "mf6-examples", "Grubb", "Henry"))
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
def test_unknown_case_raises():
    with pytest.raises(core.ModflowValidationError):
        core.run_validation_case("not_a_case")
