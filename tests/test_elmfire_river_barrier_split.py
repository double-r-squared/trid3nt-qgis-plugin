"""Offline coverage for the real-data river-barrier CONNECTIVITY split (ADR 0239
real mode, meander-robust refinement).

Pure grid math - no LANDFIRE fetch, no solver. Proves the head/far-side split by
8-connected land components (exact for any river shape), the two-component reach
gate (straight river accepted, gooseneck rejected, land-bridge leak detected), the
still-original width measurement, the wind-axis handling, and the honest guards -
so the real-data spotting demo's measurement is regression-locked without a live
run.
"""
from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.workflows.elmfire.spotting.spotting import (
    _contiguous_band,
    _river_runs,
    check_river_separates_domain,
    measure_river_split,
    river_barrier_captions,
)

CELL = 30.0
WATER = 98
GRASS = 102


def _grid_with_river(ny=40, nx=100, lo=50, hi=54):
    """A straight, full-height N-S river band - the clean two-component case."""
    fbfm = np.full((ny, nx), GRASS, dtype=int)
    fbfm[:, lo : hi + 1] = WATER
    return fbfm


def _grid_with_gooseneck(ny=40, nx=100):
    """A horseshoe-bend river fully CONTAINED inside the domain (touches neither
    the top nor the bottom edge) - the LAND wraps around both open ends of the
    bend and reconnects, so the far bank is reachable from the near bank without
    crossing water: ONE land component (the machinery this refinement exists to
    catch - a contiguous front can thread between meanders/around a bend)."""
    fbfm = np.full((ny, nx), GRASS, dtype=int)
    fbfm[10:30, 50:55] = WATER    # down leg (interior only, rows 10-29)
    fbfm[25:30, 50:71] = WATER    # bend connecting the two legs
    fbfm[10:30, 66:71] = WATER    # up leg back toward (but not touching) the top
    return fbfm


def _grid_with_land_bridge(ny=40, nx=100, lo=50, hi=54, bridge_row=20):
    """A straight river with a single-row LAND gap (a bridge) punched through it -
    the banks merge into one component through the gap, so a reach that looks
    separated at a glance is structurally NOT (the leak the connectivity gate must
    catch that row-splitting could not)."""
    fbfm = _grid_with_river(ny=ny, nx=nx, lo=lo, hi=hi)
    fbfm[bridge_row, lo : hi + 1] = GRASS  # punch the bridge through the full width
    return fbfm


# --------------------------------------------------------------------------- #
# check_river_separates_domain - the reach-selection gate.
# --------------------------------------------------------------------------- #
def test_straight_river_is_two_components():
    sep = check_river_separates_domain(_grid_with_river())
    assert sep["two_component"] is True
    assert len(sep["significant_component_sizes_cells"]) == 2


def test_gooseneck_reach_is_one_component_and_rejected():
    sep = check_river_separates_domain(_grid_with_gooseneck())
    assert sep["two_component"] is False
    assert len(sep["significant_component_sizes_cells"]) == 1


def test_land_bridge_leak_is_structurally_detected():
    sep = check_river_separates_domain(_grid_with_land_bridge())
    assert sep["two_component"] is False
    assert len(sep["significant_component_sizes_cells"]) == 1


def test_small_river_island_does_not_break_the_two_component_read():
    # A single-cell land speck inside the water band is a THIRD, tiny component -
    # dropped by the significance floor, so a real river island doesn't spuriously
    # fail an otherwise clean two-component reach.
    fbfm = _grid_with_river()
    fbfm[20, 52] = GRASS  # one land cell stranded mid-river
    sep = check_river_separates_domain(fbfm)
    assert sep["num_components"] == 3
    assert sep["two_component"] is True


# --------------------------------------------------------------------------- #
# measure_river_split - connectivity head/far split (meander-robust).
# --------------------------------------------------------------------------- #
def test_river_runs_finds_contiguous_water_runs():
    row = np.array([0, 0, 1, 1, 1, 0, 1, 0, 0, 1, 1], dtype=bool)
    assert _river_runs(row) == [(2, 4), (6, 6), (9, 10)]
    assert _river_runs(np.zeros(5, dtype=bool)) == []


def test_contiguous_band_bridges_small_gaps_around_seed():
    assert _contiguous_band([1, 2, 3, 10, 11], 2, 2) == [1, 2, 3]
    assert _contiguous_band([1, 2, 4, 5], 1, 2) == [1, 2, 3, 4, 5]  # gap<=2 bridged
    assert _contiguous_band([1, 2, 10, 11], 10, 2) == [10, 11]  # nearest block to seed
    assert _contiguous_band([], 5, 2) == []


def test_split_off_holds_far_side_zero_and_measures_width():
    fbfm = _grid_with_river()
    toa = np.full(fbfm.shape, np.nan)
    toa[:, :50] = 100.0  # contiguous head fire fills west, stops at near bank
    res = measure_river_split(
        toa, fbfm, ign_rowcol=(20, 10), wind_dir_deg=270.0, cellsize_m=CELL
    )
    assert res["river_width_m"] == 5 * CELL  # 5 water cells * 30 m
    assert res["far_area_km2"] == 0.0
    assert res["head_area_km2"] > 0.0
    assert res["downwind_sign"] == 1.0
    assert res["num_land_components"] == 2.0


def test_split_on_counts_far_side_spot_fire():
    fbfm = _grid_with_river()
    toa = np.full(fbfm.shape, np.nan)
    toa[:, :50] = 100.0
    toa[15:25, 60:71] = 200.0  # embers cross -> far-side spot fire (10x11 cells)
    res = measure_river_split(
        toa, fbfm, ign_rowcol=(20, 10), wind_dir_deg=270.0, cellsize_m=CELL
    )
    assert res["far_cells"] == 10 * 11
    assert res["far_area_km2"] == pytest.approx(10 * 11 * CELL * CELL / 1e6)


def test_split_wind_from_east_mirrors_column_direction():
    fbfm = _grid_with_river()
    toa = np.full(fbfm.shape, np.nan)
    toa[:, 55:] = 100.0  # head fire east of the river
    toa[15:25, 30:41] = 200.0  # far-side spot west of the near bank
    res = measure_river_split(
        toa, fbfm, ign_rowcol=(20, 90), wind_dir_deg=90.0, cellsize_m=CELL
    )
    assert res["downwind_sign"] == -1.0
    assert res["head_area_km2"] > 0.0
    assert res["far_area_km2"] > 0.0


def test_split_ignores_far_side_land_island_offshoot():
    # Meander-robustness proof: a spot fire on a tiny far-side land offshoot that
    # is itself only reachable by crossing water is still counted correctly by
    # component membership, not by row position (the old row-split's failure mode
    # on a threading river). Here a stray far-side burn sits well outside any
    # river-adjacent row band the old row-splitter would have scanned, but
    # connectivity still attributes it correctly because it is in the far land
    # component.
    fbfm = _grid_with_river()
    toa = np.full(fbfm.shape, np.nan)
    toa[:, :50] = 100.0
    toa[0:2, 90:92] = 200.0  # far corner, outside the old row-band's usual reach
    res = measure_river_split(
        toa, fbfm, ign_rowcol=(20, 10), wind_dir_deg=270.0, cellsize_m=CELL
    )
    assert res["far_cells"] == 4


def test_north_south_wind_rejected():
    fbfm = _grid_with_river()
    toa = np.full(fbfm.shape, np.nan)
    toa[:, :50] = 100.0
    with pytest.raises(ValueError, match="E-W"):
        measure_river_split(
            toa, fbfm, ign_rowcol=(20, 10), wind_dir_deg=180.0, cellsize_m=CELL
        )


def test_no_river_downwind_raises():
    fbfm = np.full((40, 100), GRASS, dtype=int)  # no water anywhere -> one component
    toa = np.full(fbfm.shape, np.nan)
    toa[:, :50] = 100.0
    with pytest.raises(ValueError, match="two land components"):
        measure_river_split(
            toa, fbfm, ign_rowcol=(20, 10), wind_dir_deg=270.0, cellsize_m=CELL
        )


def test_gooseneck_rejected_by_measure_river_split():
    fbfm = _grid_with_gooseneck()
    toa = np.full(fbfm.shape, np.nan)
    toa[:, :50] = 100.0
    with pytest.raises(ValueError, match="gooseneck"):
        measure_river_split(
            toa, fbfm, ign_rowcol=(20, 10), wind_dir_deg=270.0, cellsize_m=CELL
        )


def test_land_bridge_rejected_by_measure_river_split():
    fbfm = _grid_with_land_bridge()
    toa = np.full(fbfm.shape, np.nan)
    toa[:, :50] = 100.0
    with pytest.raises(ValueError, match="land-bridge"):
        measure_river_split(
            toa, fbfm, ign_rowcol=(20, 10), wind_dir_deg=270.0, cellsize_m=CELL
        )


def test_ignition_inside_river_rejected():
    fbfm = _grid_with_river()
    toa = np.full(fbfm.shape, np.nan)
    toa[:, :50] = 100.0
    with pytest.raises(ValueError, match="inside the river"):
        measure_river_split(
            toa, fbfm, ign_rowcol=(20, 52), wind_dir_deg=270.0, cellsize_m=CELL
        )


# --------------------------------------------------------------------------- #
# river_barrier_captions - verdict-consistent, no stale conditional templating.
# --------------------------------------------------------------------------- #
def test_captions_jumped_state_both_numbers():
    cap = river_barrier_captions(
        off_far_km2=0.0, on_far_km2=3.1, river_width_m=120.0, verdict="jumped"
    )
    assert "CROSSED" in cap["on"]
    assert "3.1" in cap["on"]
    assert "120" in cap["on"]


def test_captions_held_never_claims_a_cross():
    cap = river_barrier_captions(
        off_far_km2=0.0, on_far_km2=0.0, river_width_m=80.0, verdict="held"
    )
    assert "HELD" in cap["on"]
    assert "CROSSED" not in cap["on"]


def test_captions_leaked_does_not_say_holds_or_crosses_with_nonzero_off():
    # The bug this refinement fixes: printing "do NOT cross" beside a nonzero
    # far-side number. A leaked reach must never claim held/jumped in the caption.
    cap = river_barrier_captions(
        off_far_km2=0.024, on_far_km2=3.107, river_width_m=120.0, verdict="leaked"
    )
    assert "leaked" in cap["on"].lower()
    assert "HELD" not in cap["on"]
    assert "0.024" in cap["off"] or "0.024" in cap["on"]
