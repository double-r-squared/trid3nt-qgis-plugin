"""SWMM RTK unit-hydrograph RDII (ADR 0190 row 4; declared per ADR 0307).

Offline coverage for the RTK closed form (triangular UH, volume identity,
convolution) + the declared plan and its registration. The native-SWMM
cross-check is now a DECLARED plan step rather than an optional branch, so the
fast tests exercise the closed-form step directly and the full tool runs the
solve.

The EPA Table 7-1 values live in ``scripts/demo_swmm_rdii_epa_table_7_1.py`` -
the banner-labeled saved invocation - not in the workflow module, so this file
imports them from there rather than restating a second copy.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from demo_swmm_rdii_epa_table_7_1 import (  # noqa: E402
    ARGS as EPA_ARGS,
    PUBLISHED_RDII_CFS as EPA_PUBLISHED_RDII_CFS,
    SUM_R as EPA_SUM_R,
)

from trid3nt_server.workflows.swmm.rdii_rtk.rdii_rtk import (  # noqa: E402
    PARAMS,
    plan,
    swmm_rdii_rtk_unit_hydrograph,
)
from trid3nt_server.workflows.swmm.rdii_rtk.steps import (  # noqa: E402
    build_rtk_rdii_inp,
    closed_form_rdii,
    rdii_hydrograph,
    rdii_volume_cf,
    rtk_expected_volume_cf,
    rtk_unit_hydrograph,
)

_UHS = [(0.10, 2.0, 2.0), (0.06, 6.0, 3.0), (0.03, 12.0, 4.0)]


# --- triangular unit hydrograph -------------------------------------------- #
def test_uh_is_triangular_peaks_at_T():
    dt = 0.25
    ords = rtk_unit_hydrograph(0.10, 2.0, 2.0, dt, 100.0)
    peak_i = ords.index(max(ords))
    assert peak_i * dt == pytest.approx(2.0, abs=dt)  # peak at T
    assert ords[0] == pytest.approx(0.0)
    assert ords[-1] == pytest.approx(0.0)  # returns to zero at base


def test_uh_base_is_T_times_one_plus_K():
    dt = 0.25
    ords = rtk_unit_hydrograph(0.10, 2.0, 2.0, dt, 100.0)
    base_hr = (len(ords) - 1) * dt
    assert base_hr == pytest.approx(2.0 * (1 + 2.0), abs=dt)  # T*(1+K)=6


def test_uh_area_equals_R_volume_identity():
    """Integral of the UH = R * area (in cfs*hr -> acre-in). This is the RTK
    volume identity for a single unit hydrograph."""
    dt = 0.05
    R, T, K, area = 0.10, 2.0, 2.0, 100.0
    ords = rtk_unit_hydrograph(R, T, K, dt, area)
    area_cf = sum(ords) * dt * 3600.0  # cfs*s
    expected = R * (1.0 / 12.0) * area * 43560.0  # 1 inch over area, cf
    assert area_cf == pytest.approx(expected, rel=0.01)


# --- convolution + volume identity ----------------------------------------- #
def test_rdii_volume_identity_holds():
    dt = 0.25
    rain = [0.25, 0.25, 0.25, 0.25]  # 1 inch over 1 hour
    rdii = rdii_hydrograph(_UHS, rain, dt, 100.0)
    vol = rdii_volume_cf(rdii, dt)
    exp = rtk_expected_volume_cf(_UHS, 1.0, 100.0)
    assert vol / exp == pytest.approx(1.0, abs=0.01)


def test_more_rainfall_more_rdii():
    dt = 0.25
    small = rdii_hydrograph(_UHS, [0.1, 0.1], dt, 100.0)
    big = rdii_hydrograph(_UHS, [0.5, 0.5], dt, 100.0)
    assert max(big) > max(small)


# --- the closed-form STEP (fast: no engine) --------------------------------- #
@pytest.mark.asyncio
async def test_closed_form_step_reports_the_volume_identity():
    out = await closed_form_rdii(
        R1=0.10, T1=2.0, K1=2.0, R2=0.06, T2=6.0, K2=3.0,
        R3=0.03, T3=12.0, K3=4.0, sewershed_area_ac=100.0,
        rainfall_depth_in=1.0, storm_duration_hr=1.0, dt_min=15,
    )
    assert out["sum_R"] == pytest.approx(0.19)
    assert out["rtk_volume_identity_ratio"] == pytest.approx(1.0, abs=0.02)
    assert max(out["rdii_cfs"]) > 0


@pytest.mark.asyncio
async def test_closed_form_step_refuses_when_no_unit_hydrograph_is_active():
    """R=0 across all three is not a degenerate model, it is no model."""
    from trid3nt_server.workflows.swmm.steps import SwmmDeckError

    with pytest.raises(SwmmDeckError) as exc:
        await closed_form_rdii(
            R1=0.0, T1=2.0, K1=2.0, R2=0.0, T2=6.0, K2=3.0,
            R3=0.0, T3=12.0, K3=4.0, sewershed_area_ac=100.0,
            rainfall_depth_in=1.0, storm_duration_hr=1.0, dt_min=15,
        )
    assert exc.value.error_code == "SWMM_RDII_RTK_INVALID"


@pytest.mark.asyncio
async def test_malformed_hyetograph_refuses_rather_than_reverting():
    """The swallow class: HEAD silently used the design storm on bad input."""
    from trid3nt_server.workflows.swmm.steps import SwmmDeckError

    with pytest.raises(SwmmDeckError):
        await closed_form_rdii(
            R1=0.10, T1=2.0, K1=2.0, R2=0.0, T2=6.0, K2=3.0,
            R3=0.0, T3=12.0, K3=4.0, sewershed_area_ac=100.0,
            rainfall_depth_in=1.0, storm_duration_hr=1.0, dt_min=15,
            rainfall_series_in_per_hr=["not-a-depth"],
        )


# --- native SWMM deck author ------------------------------------------------ #
def test_inp_has_hydrographs_and_rdii_sections():
    inp = build_rtk_rdii_inp(_UHS, [1.0, 1.0, 1.0, 1.0], 15, 100.0, 24.0)
    assert "[HYDROGRAPHS]" in inp
    assert "[RDII]" in inp
    assert "UH1 ALL SHORT 0.1 2.0 2.0" in inp
    assert "N1 UH1 100.0" in inp


# --- the declared plan ------------------------------------------------------ #
@pytest.mark.asyncio
async def test_plan_declares_the_native_cross_check_as_a_step():
    """The cross-check is not optional: it is one of the two acceptance checks."""
    from trid3nt_server.workflows.lib import resolve_params, validate_plan

    p = await resolve_params(PARAMS, {})
    built = plan(p, None)
    validate_plan(built, PARAMS, ())
    labels = [s.label for s in built.declared()]
    assert labels == ["form", "closed_form", "deck", "solve", "rdii"]
    charts = [c.name for s in built.declared() for c in s.charts]
    assert charts == ["rdii_vs_runoff"]


@pytest.mark.asyncio
async def test_no_physics_param_rests_on_a_labeled_default():
    """Law 9, structurally: R/T/K are calibration SCENARIO values, not physics."""
    from trid3nt_server.workflows.lib.params import doors

    offenders = [q.name for q in PARAMS
                 if q.consequence == "physics"
                 and q.door in (doors.SCENARIO, doors.CONSTANT)]
    assert offenders == []


# --- tool (full: closed form + native cross-check) -------------------------- #
@pytest.mark.asyncio
async def test_tool_scalars_and_native_cross_check():
    res = await swmm_rdii_rtk_unit_hydrograph()
    assert res["status"] == "ok", res
    assert res["model"] == "rtk_unit_hydrograph_rdii"
    assert res["rtk_volume_identity_ratio"] == pytest.approx(1.0, abs=0.02)
    assert res["rdii_peak_cfs"] > 0
    assert 0.0 <= res["rdii_fraction_of_total"] <= 1.0
    assert res["swmm_rdii_peak_cfs"] is not None, "native SWMM cross-check did not run"
    assert res["swmm_vs_closed_form_peak_ratio"] == pytest.approx(1.0, abs=0.03)
    assert res["chart_specs"] == ["rdii_vs_runoff"]


@pytest.mark.asyncio
async def test_epa_table_7_1_replication():
    """The EPA SWMM 5 Ch.7 Table 7-1 worked example, driven from the demo script's
    saved invocation: a representative RTK set reproduces the native SWMM RDII
    exactly and the published Figure 7-10 peak (~1.02 cfs) closely."""
    res = await swmm_rdii_rtk_unit_hydrograph(**EPA_ARGS)
    assert res["sum_R"] == pytest.approx(EPA_SUM_R, abs=1e-9)
    assert res["rtk_volume_identity_ratio"] == pytest.approx(1.0, abs=0.02)
    # closed form reproduces the native SWMM engine
    assert res["swmm_vs_closed_form_peak_ratio"] == pytest.approx(1.0, abs=0.03)
    # and lands near the published Figure 7-10 peak (representative R/T/K split)
    published_peak = max(EPA_PUBLISHED_RDII_CFS.values())
    assert res["rdii_peak_cfs"] == pytest.approx(published_peak, rel=0.10)


def test_registered():
    from trid3nt_server.tools import TOOL_REGISTRY
    assert "swmm_rdii_rtk_unit_hydrograph" in TOOL_REGISTRY
