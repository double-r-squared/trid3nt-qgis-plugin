"""Offline coverage for the real-data river-barrier split logic (ADR 0239 real mode).

Pure grid math - no LANDFIRE fetch, no solver. Proves the far-side / head split, the
grid-measured river width, the wind-axis handling, and the honest guards, so the
real-data spotting demo's measurement is regression-locked without a live run.
"""
from __future__ import annotations

import numpy as np
import pytest

from trid3nt_server.agent.workflows.elmfire.spotting.spotting import (
    _contiguous_band,
    _river_runs,
    measure_river_split,
)

CELL = 30.0


def _grid_with_river(ny=40, nx=100, lo=50, hi=54):
    fbfm = np.full((ny, nx), 102, dtype=int)  # burnable grass everywhere
    fbfm[:, lo : hi + 1] = 98  # LANDFIRE water class (the river)
    return fbfm


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


def test_north_south_wind_rejected():
    fbfm = _grid_with_river()
    toa = np.full(fbfm.shape, np.nan)
    toa[:, :50] = 100.0
    with pytest.raises(ValueError, match="E-W"):
        measure_river_split(
            toa, fbfm, ign_rowcol=(20, 10), wind_dir_deg=180.0, cellsize_m=CELL
        )


def test_no_river_downwind_raises():
    fbfm = np.full((40, 100), 102, dtype=int)  # no water anywhere
    toa = np.full(fbfm.shape, np.nan)
    toa[:, :50] = 100.0
    with pytest.raises(ValueError, match="no river"):
        measure_river_split(
            toa, fbfm, ign_rowcol=(20, 10), wind_dir_deg=270.0, cellsize_m=CELL
        )
