"""Unit tests for the two pure oxygen-sag relations in ``helpers/``.

Covered: the critical point at known values, the profile as a genuine sag, the
``k1 == k2`` limit, a zero load, and the released-mass relation beside it."""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.workflows.telemac.helpers.oxygen_sag import (
    critical_point,
    do_profile,
)
from trid3nt_server.workflows.telemac.helpers.released_mass import released_mass_kg


def test_the_critical_point_at_known_values() -> None:
    # k1=5, k2=10 /d, Cs=9, L0=20, D0=0: tc = ln(2)/5 d, min DO = 9 - 0.5*20*0.5.
    crit = critical_point(0.5, 9.0, 20.0, 0.0, 5.0, 10.0)
    assert crit["min_do_mgl"] == pytest.approx(4.0, abs=1e-6)
    assert crit["tc_day"] == pytest.approx(np.log(2.0) / 5.0, abs=1e-9)
    assert crit["xc_m"] == pytest.approx(0.5 * crit["tc_day"] * 86400.0)


def test_the_profile_is_a_genuine_sag() -> None:
    distance = list(np.linspace(0.0, 12000.0, 200))
    oxygen, deficit = do_profile(distance, 0.54, 9.0, 20.0, 0.0, 5.0, 10.0)
    oxygen = np.asarray(oxygen)
    lowest = int(oxygen.argmin())
    assert 0 < lowest < len(oxygen) - 1
    assert oxygen[0] > oxygen[lowest] < oxygen[-1]
    assert deficit[lowest] == pytest.approx(9.0 - oxygen[lowest])


def test_the_equal_rate_limit_is_finite() -> None:
    oxygen, _ = do_profile([0.0, 1000.0, 5000.0], 0.5, 9.0, 20.0, 0.0, 5.0, 5.0)
    assert all(np.isfinite(oxygen))
    assert oxygen[0] == pytest.approx(9.0)


def test_no_load_leaves_the_reach_at_its_opening_deficit() -> None:
    crit = critical_point(0.5, 9.0, 0.0, 1.5, 5.0, 10.0)
    assert crit == {"tc_day": 0.0, "xc_m": 0.0, "min_do_mgl": 7.5,
                    "max_deficit_mgl": 1.5}


@pytest.mark.parametrize("discharge,concentration,duration,kilograms", [
    (2.0, 500.0, 3600.0, 3600.0),
    (2.0, 0.0, 3600.0, 0.0),
    (2.0, -5.0, 3600.0, 0.0),
])
def test_the_released_mass(discharge: float, concentration: float,
                           duration: float, kilograms: float) -> None:
    assert released_mass_kg(discharge, concentration, duration) == kilograms
