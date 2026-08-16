"""Offline tests for the TELEMAC rain-on-grid SCS-CN infiltration preprocessing.

Every numeric assertion is a HAND-COMPUTED fixture. The AMC and steep-slope
assertions double as a parity check against the exact formulas in the installed
TELEMAC ``runoff_scs_cn.f`` (the native path applies these itself; the
preprocessing path relies on this module reproducing them bit-for-bit).
"""

from __future__ import annotations

import math

import pytest

from trid3nt_server.workflows.telemac.rain_on_grid.cn_infiltration import (
    CNInfiltrationError,
    amc_convert_cn,
    huang_steep_slope_cn,
    landcover_cn_manning,
    node_curve_numbers,
    paper_exponential_steep_slope_cn,
    rainfall_excess_hyetograph,
    scs_potential_retention_mm,
    scs_runoff_mm,
)


def test_potential_retention_eq8():
    # S = 25400/CN - 254; CN=80 -> 317.5 - 254 = 63.5 mm
    assert scs_potential_retention_mm(80.0) == pytest.approx(63.5, abs=1e-9)
    # CN=100 -> S=0 (impervious, all rain runs off)
    assert scs_potential_retention_mm(100.0) == pytest.approx(0.0, abs=1e-9)


def test_runoff_eq7_hand():
    # P=100 mm, CN=80: S=63.5, Ia=0.2*63.5=12.7
    # Q=(100-12.7)^2/(100-12.7+63.5)=87.3^2/150.8
    expected = (100.0 - 12.7) ** 2 / (100.0 - 12.7 + 63.5)
    assert scs_runoff_mm(100.0, 80.0) == pytest.approx(expected, abs=1e-9)
    assert scs_runoff_mm(100.0, 80.0) == pytest.approx(50.5391, abs=1e-3)


def test_runoff_zero_below_initial_abstraction():
    # P <= Ia -> no runoff. CN=80 -> Ia=12.7 mm; P=10 mm < Ia.
    assert scs_runoff_mm(10.0, 80.0) == 0.0


def test_runoff_revised_ia_ratio():
    # ia_ratio 0.05 (TELEMAC OPTION FOR INITIAL ABSTRACTION RATIO = 2)
    s = 63.5
    ia = 0.05 * s
    expected = (100.0 - ia) ** 2 / (100.0 - ia + s)
    assert scs_runoff_mm(100.0, 80.0, ia_ratio=0.05) == pytest.approx(expected, abs=1e-9)


def test_amc_conversions_match_telemac_formulas():
    # AMC I (dry): 4.2*CN2/(10 - 0.058*CN2); CN2=80 -> 336/5.36
    assert amc_convert_cn(80.0, 1) == pytest.approx(336.0 / 5.36, abs=1e-9)
    # AMC II unchanged
    assert amc_convert_cn(80.0, 2) == 80.0
    # AMC III (wet): 23*CN2/(10 + 0.13*CN2); CN2=80 -> 1840/20.4
    assert amc_convert_cn(80.0, 3) == pytest.approx(1840.0 / 20.4, abs=1e-9)


def test_amc_invalid_raises():
    with pytest.raises(CNInfiltrationError):
        amc_convert_cn(80.0, 4)


def test_huang_steep_slope_matches_telemac_rational():
    # TELEMAC factor = (322.79 + 15.63*a)/(a + 323.52); a=0.5, CN2=80
    a = 0.5
    factor = (322.79 + 15.63 * a) / (a + 323.52)
    assert huang_steep_slope_cn(80.0, a) == pytest.approx(80.0 * factor, abs=1e-9)


def test_huang_no_correction_below_014():
    assert huang_steep_slope_cn(80.0, 0.10) == pytest.approx(80.0, abs=1e-12)


def test_huang_clamps_above_14_and_caps_at_100():
    a_hi = 3.0
    factor_14 = (322.79 + 15.63 * 1.4) / (1.4 + 323.52)
    assert huang_steep_slope_cn(80.0, a_hi) == pytest.approx(80.0 * factor_14, abs=1e-9)
    # A high CN2 with correction never exceeds 100.
    assert huang_steep_slope_cn(99.0, 1.0) <= 100.0


def test_paper_exponential_variant():
    assert paper_exponential_steep_slope_cn(80.0, 0.5) == pytest.approx(
        80.0 * math.exp(0.0065 * 0.5), abs=1e-9
    )


def test_landcover_table_forest_and_urban_and_fallback():
    cn, n, label = landcover_cn_manning(42)  # Evergreen Forest
    assert (cn, n, label) == (80.0, 0.200, "forest")
    cn, n, label = landcover_cn_manning(24)  # Developed High Intensity
    assert (cn, n, label) == (89.0, 0.100, "urban")
    cn, n, label = landcover_cn_manning(11)  # Open Water
    assert cn == 100.0
    # Unknown code -> open-land fallback (never a silent 0 or 100)
    assert landcover_cn_manning(999) == (75.0, 0.050, "open-land")


def test_node_curve_numbers_uniform_vs_distributed():
    codes = [42, 24, 11, 81]  # forest, urban, water, cropland
    uni = node_curve_numbers(codes, uniform_cn=70.0)
    assert uni == [70.0, 70.0, 70.0, 70.0]
    dist = node_curve_numbers(codes)
    assert dist == [80.0, 89.0, 100.0, 80.0]


def test_node_curve_numbers_steep_slope_needs_slopes():
    with pytest.raises(CNInfiltrationError):
        node_curve_numbers([42, 24], steep_slope_correction=True)
    out = node_curve_numbers(
        [42, 24], slopes_m_per_m=[0.5, 0.5], steep_slope_correction=True
    )
    assert out[0] == pytest.approx(huang_steep_slope_cn(80.0, 0.5), abs=1e-12)


def test_rainfall_excess_hyetograph_sums_to_total_runoff():
    # A 4-step hyetograph over CN=85; per-step excess sums to the cumulative
    # SCS-CN runoff at the full storm total (mass conservation of the transform).
    hyeto = [10.0, 30.0, 40.0, 20.0]  # total 100 mm
    excess = rainfall_excess_hyetograph(hyeto, 85.0)
    total_runoff = scs_runoff_mm(sum(hyeto), 85.0)
    assert sum(excess) == pytest.approx(total_runoff, abs=1e-9)
    # Non-negative and never exceeds the rainfall in any step.
    assert all(e >= -1e-12 for e in excess)
    assert all(e <= h + 1e-9 for e, h in zip(excess, hyeto))


def test_rainfall_excess_early_steps_zero_until_ia_satisfied():
    # CN=70 -> S=108.86 mm, Ia=21.77 mm; the first 20 mm produce no excess.
    excess = rainfall_excess_hyetograph([20.0, 50.0], 70.0)
    assert excess[0] == 0.0
    assert excess[1] > 0.0
