"""Offline tests for ``elmfire_crown_fire_active_ros_verification`` (ADR 0256) --
the Cruz et al. (2005) active crown-fire rate-of-spread exact-solution gate.

Pins the CLOSED-FORM reference + the verification contract in ISOLATION (no
solver, no docker, no LANDFIRE/DEM fetch). The live in-image computed-vs-closed-
form gate runs through the elmfire container (see the driver + ADR 0256 report).

Cruz, M.G., Alexander, M.E., Wagner, R.H. (2005) "Development and testing of
models for predicting crown fire rate of spread in conifer forest stands",
Can. J. For. Res. 35:1626-1639. Active crown-fire ROS:

    R_active = 11.02 * U10^0.90 * CBD^0.19 * exp(-0.17 * EFFM)      [m/min]

ELMFIRE (elmfire_spread_rate.f90:177-179) implements it verbatim with the 20-ft
-> 10-m open-wind conversion WS10KMPH = WS20MPH * (1.609 / 0.87).
"""

from __future__ import annotations

import math

import pytest


# ===========================================================================
# (1) Closed-form Cruz active-crown ROS -- exact equation + ELMFIRE conversion.
# ===========================================================================
def test_cruz_closed_form_matches_published_equation():
    from trid3nt_server.agent.workflows.elmfire.cruz_crown_fire import (
        MPH_20FT_TO_KMPH_10M,
        cruz_active_crown_ros_m_min,
    )

    # The 20-ft mph -> 10-m km/h conversion factor (ELMFIRE spread_rate.f90:140).
    assert MPH_20FT_TO_KMPH_10M == pytest.approx(1.609 / 0.87, rel=1e-9)

    wind_mph, cbd, effm = 20.0, 0.18, 3.0
    got = cruz_active_crown_ros_m_min(wind_mph, cbd, effm)

    # Recompute the published closed form independently.
    u10 = wind_mph * (1.609 / 0.87)  # km/h @10m
    expected = 11.02 * u10**0.90 * cbd**0.19 * math.exp(-0.17 * effm)
    assert got == pytest.approx(expected, rel=1e-9)
    # Sanity: this configuration is a fast active-crown run (~123 m/min).
    assert 100.0 < got < 150.0


def test_cruz_monotonic_in_wind_and_moisture():
    from trid3nt_server.agent.workflows.elmfire.cruz_crown_fire import (
        cruz_active_crown_ros_m_min,
    )

    # Faster with more wind; slower with more moisture (the exp(-0.17*EFFM) term).
    slow = cruz_active_crown_ros_m_min(10.0, 0.18, 3.0)
    fast = cruz_active_crown_ros_m_min(30.0, 0.18, 3.0)
    assert fast > slow
    dry = cruz_active_crown_ros_m_min(20.0, 0.18, 3.0)
    moist = cruz_active_crown_ros_m_min(20.0, 0.18, 12.0)
    assert moist < dry
    # The moisture ratio is exactly exp(-0.17*(12-3)).
    assert moist / dry == pytest.approx(math.exp(-0.17 * 9.0), rel=1e-9)


def test_cruz_adj_scales_linearly():
    from trid3nt_server.agent.workflows.elmfire.cruz_crown_fire import (
        cruz_active_crown_ros_m_min,
    )

    base = cruz_active_crown_ros_m_min(20.0, 0.18, 3.0, crown_fire_adj=1.0)
    scaled = cruz_active_crown_ros_m_min(20.0, 0.18, 3.0, crown_fire_adj=1.5)
    assert scaled == pytest.approx(1.5 * base, rel=1e-9)


# ===========================================================================
# (2) Contract round-trip -- the crown-ROS verification LayerURI.
# ===========================================================================
def test_crown_ros_verification_layer_is_firespread_subtype():
    from trid3nt_contracts.elmfire_contracts import (
        ElmfireCrownRosVerificationLayerURI,
        FireSpreadLayerURI,
    )

    layer = ElmfireCrownRosVerificationLayerURI(
        layer_id="elmfire-crown-ros-X",
        name="Fire arrival time (active crown-fire ROS verification)",
        layer_type="raster",
        uri="s3://runs/X/elmfire_toa.tif",
        style_preset="continuous_fire_arrival_hr",
        role="primary",
        burned_area_km2=3.4,
        fire_arrival_max_hr=0.4,
        duration_hours=0.4,
        numerical_ros_m_min=117.0,
        cruz_ros_m_min=123.2,
        rel_error=0.05,
        tolerance=0.15,
        passed=True,
        wind_speed_mph=20.0,
        cbd_kg_m3=0.18,
        effm_pct=3.0,
    )
    assert isinstance(layer, FireSpreadLayerURI)
    assert layer.passed is True
    assert layer.rel_error <= layer.tolerance


# ===========================================================================
# (3) Gate logic -- pass iff rel_error <= tolerance AND no edge touch.
# ===========================================================================
@pytest.mark.parametrize(
    "numerical, cruz, tol, edge, expect_pass",
    [
        (117.0, 123.2, 0.15, False, True),   # 5% error, inside -> pass
        (123.2, 123.2, 0.15, False, True),   # exact
        (150.0, 123.2, 0.15, False, False),  # 22% error -> fail
        (117.0, 123.2, 0.15, True, False),   # good error BUT edge touch -> fail
    ],
)
def test_gate_logic(numerical, cruz, tol, edge, expect_pass):
    rel_error = abs(numerical - cruz) / cruz
    passed = bool(rel_error <= tol and not edge)
    assert passed is expect_pass
