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
        "newton_dry_rewet", "maw_crossaquifer", "hfb_barrier"
    }
    for name, meta in core.VALIDATION_CASES.items():
        assert meta.case == name
        assert meta.package
        assert meta.reference_source.startswith("modflow6-")
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
def test_unknown_case_raises():
    with pytest.raises(core.ModflowValidationError):
        core.run_validation_case("not_a_case")
