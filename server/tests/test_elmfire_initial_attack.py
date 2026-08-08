"""ELMFIRE initial-attack containment probability (ADR 0190 row 2).

Offline coverage for the Hirsch (1998) closed-form POC model: the logistic POC,
the Byram ROS inversion, elliptical growth during the attack delay, the
POC-vs-delay ladder, registration + category mapping.
"""
from __future__ import annotations

import math

import pytest

from trid3nt_server.agent.workflows.elmfire.initial_attack.initial_attack import (
    byram_head_ros_m_per_min,
    build_poc_chart_spec,
    elmfire_initial_attack_containment_probability,
    fire_size_at_attack_ha,
    hirsch_poc,
    poc_vs_delay,
)


# --- Hirsch POC logistic ---------------------------------------------------- #
def test_poc_monotone_decreasing_in_size():
    """A bigger fire is harder to contain (POC falls with size)."""
    prev = 1.0
    for size in [0.1, 0.5, 1.0, 4.0, 10.0, 50.0]:
        p = hirsch_poc(size, 2000.0)
        assert 0.0 < p < 1.0
        assert p < prev
        prev = p


def test_poc_monotone_decreasing_in_intensity():
    """A more intense fire is harder to contain (POC falls with intensity)."""
    prev = 1.0
    for i in [500.0, 1000.0, 2000.0, 4000.0, 6000.0]:
        p = hirsch_poc(1.0, i)
        assert p < prev
        prev = p


def test_poc_matches_published_formula():
    """POC = E/(1+E), ln(E)=4.6835-0.7043*A-0.00041*I-0.000052*A*I (elmfire.io)."""
    import math

    def ref(a, i):
        e = math.exp(4.6835 - 0.7043 * a - 0.00041 * i - 0.000052 * a * i)
        return e / (1.0 + e)

    for a, i in [(0.1, 500), (1.0, 2000), (5.0, 4000), (10.0, 3000), (2.0, 1000)]:
        assert hirsch_poc(a, i) == pytest.approx(ref(a, i), rel=1e-9)


# --- Byram ROS -------------------------------------------------------------- #
def test_byram_ros_inversion():
    """ROS = I/(H*w); I=2700 kW/m, w=1.5 -> 0.1 m/s -> 6 m/min."""
    ros = byram_head_ros_m_per_min(2700.0, 1.5)
    assert ros == pytest.approx(2700.0 / (18000.0 * 1.5) * 60.0, rel=1e-9)
    assert ros == pytest.approx(6.0, rel=1e-6)


def test_byram_ros_scales_with_intensity():
    assert byram_head_ros_m_per_min(4000.0, 1.5) > byram_head_ros_m_per_min(2000.0, 1.5)


# --- elliptical growth ------------------------------------------------------ #
def test_zero_delay_returns_detection_size():
    assert fire_size_at_attack_ha(0.25, 6.0, 0.0, 2.5, 4.0) == pytest.approx(0.25)


def test_growth_increases_with_delay():
    s0 = fire_size_at_attack_ha(0.1, 6.0, 10.0, 2.5, 4.0)
    s1 = fire_size_at_attack_ha(0.1, 6.0, 30.0, 2.5, 4.0)
    assert s1 > s0 > 0.1


def test_growth_area_matches_ellipse_formula():
    ros, t, lb, hb = 6.0, 20.0, 2.5, 4.0
    length = (ros + ros / hb) * t
    area = math.pi / 4.0 * length * (length / lb) / 10000.0
    assert fire_size_at_attack_ha(0.0, ros, t, lb, hb) == pytest.approx(area, rel=1e-9)


# --- POC vs delay ----------------------------------------------------------- #
def test_poc_vs_delay_is_decreasing():
    curve = poc_vs_delay(2500.0, 0.1, 1.5, 2.5, 4.0, [0, 15, 30, 60, 120])
    pocs = [c["poc"] for c in curve]
    assert all(pocs[i] >= pocs[i + 1] for i in range(len(pocs) - 1))
    assert pocs[0] > pocs[-1]


def test_higher_intensity_falls_off_sooner():
    """A more intense fire loses containment probability faster with delay."""
    lo = poc_vs_delay(1000.0, 0.1, 1.5, 2.5, 4.0, [0, 30, 60])
    hi = poc_vs_delay(5000.0, 0.1, 1.5, 2.5, 4.0, [0, 30, 60])
    # at 60 min the high-intensity fire has a strictly lower POC
    assert hi[-1]["poc"] < lo[-1]["poc"]


# --- tool + registration ---------------------------------------------------- #
@pytest.mark.asyncio
async def test_tool_returns_scalars_no_emitter():
    res = await elmfire_initial_attack_containment_probability(
        head_fire_intensity_kw_m=2500.0, attack_delay_min=30.0)
    assert res["status"] == "ok"
    assert res["model"] == "hirsch_poc_elmfire"
    assert 0.0 < res["poc_at_nominal_delay"] < 1.0
    assert res["head_ros_m_per_min"] > 0
    assert res["fire_size_at_attack_ha"] >= res["detection_size_ha"]
    assert "critical_delay_min" in res
    assert res["chart_emitted"] is False  # no emitter bound


def test_chart_spec_shape():
    curves = {1000.0: poc_vs_delay(1000.0, 0.1, 1.5, 2.5, 4.0, [0, 30, 60]),
              4000.0: poc_vs_delay(4000.0, 0.1, 1.5, 2.5, 4.0, [0, 30, 60])}
    spec = build_poc_chart_spec(curves)
    assert spec["layer"][0]["encoding"]["y"]["field"] == "poc"
    assert any(r["intensity"] == "1000 kW/m" for r in spec["layer"][0]["data"]["values"])


def test_registered_and_categorized():
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    assert "elmfire_initial_attack_containment_probability" in TOOL_REGISTRY
    from trid3nt_server.agent.categories import PRIMARY_CATEGORY
    assert PRIMARY_CATEGORY["elmfire_initial_attack_containment_probability"] == "model_validation"
