"""Unit tests for the pure oxygen-sag relation in ``helpers/``.

Covered: the profile as a genuine sag and the ``k1 == k2`` limit."""

from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.workflows.telemac.helpers.oxygen_sag import do_profile


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
