"""SWMM RTK unit-hydrograph RDII (ADR 0190 row 4).

Offline coverage for the RTK closed form (triangular UH, volume identity,
convolution) + registration/category. The native-SWMM cross-check is exercised
in the tool itself (a slow subprocess-free pyswmm run) and is smoke-tested here.
"""
from __future__ import annotations

import pytest

from trid3nt_server.agent.workflows.swmm.rdii_rtk.rdii_rtk import (
    build_rtk_rdii_inp,
    rtk_unit_hydrograph,
    rdii_hydrograph,
    swmm_rdii_rtk_unit_hydrograph,
    _rtk_expected_volume_cf,
    _rdii_volume_cf,
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
    vol = _rdii_volume_cf(rdii, dt)
    exp = _rtk_expected_volume_cf(_UHS, 1.0, 100.0)
    assert vol / exp == pytest.approx(1.0, abs=0.01)


def test_more_rainfall_more_rdii():
    dt = 0.25
    small = rdii_hydrograph(_UHS, [0.1, 0.1], dt, 100.0)
    big = rdii_hydrograph(_UHS, [0.5, 0.5], dt, 100.0)
    assert max(big) > max(small)


# --- native SWMM deck author ------------------------------------------------ #
def test_inp_has_hydrographs_and_rdii_sections():
    inp = build_rtk_rdii_inp(_UHS, [1.0, 1.0, 1.0, 1.0], 15, 100.0, 24.0)
    assert "[HYDROGRAPHS]" in inp
    assert "[RDII]" in inp
    assert "UH1 ALL SHORT 0.1 2.0 2.0" in inp
    assert "N1 UH1 100.0" in inp


# --- tool (closed form only, fast) ------------------------------------------ #
@pytest.mark.asyncio
async def test_tool_closed_form_scalars():
    res = await swmm_rdii_rtk_unit_hydrograph(cross_check_swmm=False)
    assert res["status"] == "ok"
    assert res["model"] == "rtk_unit_hydrograph_rdii"
    assert res["rtk_volume_identity_ratio"] == pytest.approx(1.0, abs=0.02)
    assert res["rdii_peak_cfs"] > 0
    assert 0.0 <= res["rdii_fraction_of_total"] <= 1.0
    assert res["swmm_rdii_peak_cfs"] is None  # cross-check disabled


@pytest.mark.asyncio
async def test_tool_native_swmm_cross_check_matches_closed_form():
    """The native SWMM 5 RDII peak reproduces the closed-form peak (~1%)."""
    res = await swmm_rdii_rtk_unit_hydrograph(cross_check_swmm=True)
    assert res["swmm_rdii_peak_cfs"] is not None, "native SWMM cross-check did not run"
    assert res["swmm_vs_closed_form_peak_ratio"] == pytest.approx(1.0, abs=0.03)


@pytest.mark.asyncio
async def test_epa_table_7_1_replication():
    """The EPA SWMM 5 Ch.7 Table 7-1 worked example (10 ac, R sum 0.36, the
    published hourly rainfall): a representative RTK set reproduces the native
    SWMM RDII exactly and the published Figure 7-10 peak (~1.02 cfs) closely."""
    from trid3nt_server.agent.workflows.swmm.rdii_rtk.rdii_rtk import (
        EPA_TABLE_7_1_RAINFALL_IN_PER_HR, EPA_TABLE_7_1_PUBLISHED_RDII_CFS,
        EPA_TABLE_7_1_SUM_R,
    )
    res = await swmm_rdii_rtk_unit_hydrograph(
        R1=0.12, T1=1.0, K1=2.0, R2=0.15, T2=3.0, K2=3.0, R3=0.09, T3=8.0, K3=3.0,
        sewershed_area_ac=10.0,
        rainfall_series_in_per_hr=EPA_TABLE_7_1_RAINFALL_IN_PER_HR,
        cross_check_swmm=True,
    )
    assert res["sum_R"] == pytest.approx(EPA_TABLE_7_1_SUM_R, abs=1e-9)
    assert res["rtk_volume_identity_ratio"] == pytest.approx(1.0, abs=0.02)
    # closed form reproduces the native SWMM engine
    assert res["swmm_vs_closed_form_peak_ratio"] == pytest.approx(1.0, abs=0.03)
    # and lands near the published Figure 7-10 peak (representative R/T/K split)
    published_peak = max(EPA_TABLE_7_1_PUBLISHED_RDII_CFS.values())
    assert res["rdii_peak_cfs"] == pytest.approx(published_peak, rel=0.10)


def test_registered():
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    assert "swmm_rdii_rtk_unit_hydrograph" in TOOL_REGISTRY
