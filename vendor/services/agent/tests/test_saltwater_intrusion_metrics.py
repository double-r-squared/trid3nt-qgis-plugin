"""Regression test for ``compute_saltwater_intrusion_metrics`` (Wave-5).

Guards the blocking defect the adversarial review caught: the deck builder puts
col 0 = INLAND (WEL fresh) and col ncol-1 = SEAWARD (GHB salt, always pinned at
full salinity).  An earlier ``np.max(salty_cols)`` always returned the seaward
GHB cell, collapsing the intrusion to ~the full domain length regardless of the
true wedge penetration.  The metric must take the LOWEST-index salty bottom cell
(the true inland toe) and measure the distance FROM THE SEAWARD edge inland.

The worker real-run test re-implemented this inline, so the production function
was never exercised against a known-correct value -- this fills that gap.
"""

from __future__ import annotations

import numpy as np
import pytest

from grace2_agent.workflows.postprocess_modflow import (
    compute_saltwater_intrusion_metrics,
)


def _grid(nlay: int, ncol: int, bottom_salty_from_col: int, salt: float = 35.0):
    """A (nlay, ncol) grid: bottom row salty from ``bottom_salty_from_col`` to the
    seaward edge (col ncol-1); everything else fresh."""
    g = np.zeros((nlay, ncol), dtype="float64")
    g[-1, bottom_salty_from_col:] = salt  # bottom row salty inland of the toe
    return g


def test_intrusion_measured_from_seaward_edge():
    # ncol=10, delr=10 -> 100 m transect. Bottom row salty from col 4..9 (col 9 =
    # seaward GHB). Toe = col 4; intrusion = (10 - 4 - 0.5)*10 = 55 m inland.
    g = _grid(nlay=4, ncol=10, bottom_salty_from_col=4)
    intrusion, toe = compute_saltwater_intrusion_metrics(g, seawater_salinity_ppt=35.0, delr=10.0)
    assert intrusion == pytest.approx(55.0)
    assert toe == pytest.approx(55.0)  # alias


def test_only_seaward_ghb_cell_salty_is_minimal_intrusion():
    # Only the seaward boundary cell (col ncol-1) is salty -> the wedge has barely
    # entered: intrusion = (10 - 9 - 0.5)*10 = 5 m (NOT ~95 m, the old np.max bug).
    g = _grid(nlay=4, ncol=10, bottom_salty_from_col=9)
    intrusion, _ = compute_saltwater_intrusion_metrics(g, seawater_salinity_ppt=35.0, delr=10.0)
    assert intrusion == pytest.approx(5.0)


def test_fully_fresh_grid_is_zero():
    g = np.zeros((4, 10), dtype="float64")
    assert compute_saltwater_intrusion_metrics(g, seawater_salinity_ppt=35.0, delr=10.0) == (0.0, 0.0)


def test_fully_intruded_reaches_inland_edge():
    # Bottom row salty everywhere -> toe at col 0 -> intrusion = (10 - 0 - 0.5)*10 = 95 m.
    g = _grid(nlay=4, ncol=10, bottom_salty_from_col=0)
    intrusion, _ = compute_saltwater_intrusion_metrics(g, seawater_salinity_ppt=35.0, delr=10.0)
    assert intrusion == pytest.approx(95.0)


def test_threshold_is_half_seawater():
    # A cell at 0.4*salt is below the 50% isochlor; at 0.6*salt is above.
    g = np.zeros((2, 5), dtype="float64")
    g[-1, 2] = 0.4 * 35.0  # below threshold -> not counted
    g[-1, 3] = 0.6 * 35.0  # above threshold -> toe
    intrusion, _ = compute_saltwater_intrusion_metrics(g, seawater_salinity_ppt=35.0, delr=10.0)
    # toe col 3, ncol 5: (5 - 3 - 0.5)*10 = 15 m
    assert intrusion == pytest.approx(15.0)
