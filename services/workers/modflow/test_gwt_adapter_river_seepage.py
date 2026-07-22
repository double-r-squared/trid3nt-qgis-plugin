"""Unit tests for the MODFLOW 6 RIV river-seepage deck extension (sprint-17 J9).

These tests assert the *deck construction* contract for the river-coupled deck:
the RIV (river<->aquifer flux) input file + the along-river SRC are written with
the correct stage / rbot / conductance from a synthetic grid + river polyline,
and the pure river-draping geometry maps a projected polyline onto the right
grid cells — NO LLM, NO ``mf6`` binary required (engine invariant 2). The live
mf6 run + non-zero RIV leakage budget is proven by
``spikes/test_riv_src_spike.py`` (Phase 0 GO) and the job evidence.

Run:
    services/agent/.venv/bin/python -m pytest \
        services/workers/modflow/test_gwt_adapter_river_seepage.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gwt_adapter import (  # noqa: E402
    DEFAULT_RIVER_STAGE_DEPTH_M,
    DEFAULT_STREAMBED_CONDUCTANCE_M2_DAY,
    DeckManifest,
    build_modflow_deck,
    build_riv_records,
    _drape_polyline_onto_grid,
    _easting_northing_to_cell,
)

# Fort-Myers-area demo: the river runs roughly west->east through the spill.
LAT0, LON0 = 26.64, -81.87
# A west->east flowline crossing the grid centre (a few hundred m of reach).
RIVER_POLY = [
    (-81.878, LAT0),
    (-81.873, LAT0),
    (-81.868, LAT0),
    (-81.863, LAT0),
]
BASE = dict(
    spill_location_latlon=(LAT0, LON0),
    contaminant="TCE",
    release_rate_kg_s=0.01,
    duration_days=30,
    aquifer_k_ms=1e-4,
    porosity=0.3,
)


# --------------------------------------------------------------------------- #
# Pure river-draping geometry (no flopy)
# --------------------------------------------------------------------------- #


def test_easting_northing_to_cell_maps_corner_and_centre():
    # 10x10 grid, 50 m cells, origin at (1000, 2000). Row 0 is the NORTH row.
    kw = dict(xorigin=1000.0, yorigin=2000.0, delr=50.0, delc=50.0, nrow=10, ncol=10)
    # A point just inside the SW corner -> bottom-left cell = (row 9, col 0).
    assert _easting_northing_to_cell(1001.0, 2001.0, **kw) == (9, 0)
    # A point just inside the NE corner -> top-right cell = (row 0, col 9).
    north_top = 2000.0 + 10 * 50.0
    assert _easting_northing_to_cell(1499.0, north_top - 1.0, **kw) == (0, 9)
    # Out of bounds -> None.
    assert _easting_northing_to_cell(900.0, 2001.0, **kw) is None


def test_drape_polyline_horizontal_line_hits_one_row():
    # A horizontal line along the middle row should touch a contiguous set of
    # cells all in the same row, in west->east order.
    kw = dict(xorigin=0.0, yorigin=0.0, delr=50.0, delc=50.0, nrow=10, ncol=20)
    # Middle row 5 -> northing band [ (10-6)*50, (10-5)*50 ) = [200,250).
    north = 225.0
    verts = [(60.0, north), (940.0, north)]
    cells = _drape_polyline_onto_grid(verts, **kw)
    assert cells, "expected non-empty draped cells"
    rows = {r for (r, c, _l) in cells}
    assert rows == {5}, f"horizontal line should hit one row, got {rows}"
    cols = [c for (_r, c, _l) in cells]
    assert cols == sorted(cols), "cells should be in west->east order"
    # Cumulative in-cell reach length should roughly equal the segment length.
    total_len = sum(l for (_r, _c, l) in cells)
    assert total_len == pytest.approx(880.0, rel=0.05)


def test_drape_polyline_drops_out_of_grid_vertices():
    kw = dict(xorigin=0.0, yorigin=0.0, delr=50.0, delc=50.0, nrow=10, ncol=10)
    # Line that starts off-grid (negative easting) and ends inside.
    verts = [(-200.0, 225.0), (400.0, 225.0)]
    cells = _drape_polyline_onto_grid(verts, **kw)
    cols = {c for (_r, c, _l) in cells}
    assert all(0 <= c < 10 for c in cols), "off-grid cells must be dropped"


# --------------------------------------------------------------------------- #
# RIV record construction (stage / rbot / conductance + CHD skip)
# --------------------------------------------------------------------------- #


def test_build_riv_records_skips_chd_boundary_columns():
    cells = [(5, 0, 50.0), (5, 1, 50.0), (5, 2, 50.0), (5, 9, 50.0)]
    recs = build_riv_records(
        cells,
        conductance_m2_day=50.0,
        stage_fn=lambda r, c: 9.5,
        rbot_fn=lambda r, c: 8.0,
        chd_cols=(0, 9),
        ncol=10,
    )
    written_cols = {rec[0][2] for rec in recs}
    assert 0 not in written_cols and 9 not in written_cols, "CHD cols must be skipped"
    assert written_cols == {1, 2}


def test_build_riv_records_carries_stage_cond_rbot():
    cells = [(5, 3, 50.0)]
    recs = build_riv_records(
        cells,
        conductance_m2_day=42.0,
        stage_fn=lambda r, c: 9.5,
        rbot_fn=lambda r, c: 8.0,
        chd_cols=(0, 19),
        ncol=20,
    )
    assert len(recs) == 1
    cellid, stage, cond, rbot = recs[0]
    assert cellid == (0, 5, 3)
    assert stage == pytest.approx(9.5)
    assert cond == pytest.approx(42.0)
    assert rbot == pytest.approx(8.0)


def test_build_riv_records_clamps_stage_above_rbot():
    # A stage at/below rbot is bumped to rbot + the default depth so the reach
    # cell is a real head-dependent boundary (not a degenerate no-op).
    cells = [(5, 3, 50.0)]
    recs = build_riv_records(
        cells,
        conductance_m2_day=10.0,
        stage_fn=lambda r, c: 7.0,  # below rbot
        rbot_fn=lambda r, c: 8.0,
        ncol=20,
    )
    _cellid, stage, _cond, rbot = recs[0]
    assert stage > rbot
    assert stage == pytest.approx(rbot + DEFAULT_RIVER_STAGE_DEPTH_M)


# --------------------------------------------------------------------------- #
# Full river-coupled deck construction (writes .riv + along-river .src)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def river_deck(tmp_path):
    return build_modflow_deck(
        workdir=tmp_path,
        river_polyline_lonlat=RIVER_POLY,
        along_river_source=True,
        **BASE,
    )


def test_riv_input_file_written_with_records(river_deck, tmp_path):
    riv = tmp_path / "gwf_model.riv"
    assert riv.is_file(), "RIV input file not written"
    text = riv.read_text().lower()
    assert "begin period" in text
    # The default per-cell conductance (50 m^2/day) appears in scientific form.
    assert "5.00000000e+01" in text or "50.0" in text


def test_manifest_reports_river_coupling(river_deck):
    assert isinstance(river_deck, DeckManifest)
    assert river_deck.river_coupled is True
    assert river_deck.river_cell_count > 0
    assert river_deck.river_reach_len_m > 0.0
    assert river_deck.river_conductance_m2_day == pytest.approx(
        DEFAULT_STREAMBED_CONDUCTANCE_M2_DAY
    )
    assert river_deck.along_river_source is True


def test_riv_record_count_matches_manifest(river_deck, tmp_path):
    """Count RIV records in the FIRST period block only (the deck writes the same
    records for both the steady-state + transient periods, so a whole-file count
    would double it)."""
    riv_text = (tmp_path / "gwf_model.riv").read_text().lower()
    n_riv = 0
    in_period = False
    for line in riv_text.splitlines():
        s = line.strip()
        if s.startswith("begin period"):
            in_period = True
            n_riv = 0
            continue
        if s.startswith("end period"):
            break  # first period block only
        if in_period and s and s.split()[0].isdigit():
            n_riv += 1
    assert n_riv == river_deck.river_cell_count


def test_along_river_src_placed_on_reach_cells(river_deck, tmp_path):
    """With along_river_source=True the SRC records are the RIV reach cells, NOT
    the single spill cell, and split the total mass rate evenly across them."""
    # Parse the RIV cellids (1-based in the written file).
    riv_text = (tmp_path / "gwf_model.riv").read_text()
    riv_cells = set()
    in_period = False
    for line in riv_text.splitlines():
        s = line.strip().lower()
        if s.startswith("begin period"):
            in_period = True
            continue
        if s.startswith("end period"):
            in_period = False
            continue
        if in_period and line.strip() and line.split()[0].isdigit():
            tok = line.split()
            riv_cells.add((int(tok[0]), int(tok[1]), int(tok[2])))

    # Parse the transient-period SRC records.
    src_text = (tmp_path / "gwt_model.src").read_text()
    src_cells = set()
    rates = []
    in_p2 = False
    for line in src_text.splitlines():
        s = line.strip().lower()
        if s.startswith("begin period") and s.split()[-1] == "2":
            in_p2 = True
            continue
        if s.startswith("end period"):
            in_p2 = False
            continue
        if in_p2 and line.strip() and line.split()[0].isdigit():
            tok = line.split()
            src_cells.add((int(tok[0]), int(tok[1]), int(tok[2])))
            rates.append(float(tok[3]))

    assert src_cells, "no along-river SRC records found"
    # Every SRC cell is a RIV reach cell (the source enters along the reach).
    assert src_cells == riv_cells
    # The per-cell rate * cell-count == the total mass rate the spill carries.
    total = sum(rates)
    assert total == pytest.approx(river_deck.mass_rate_g_per_day)
    # Evenly split.
    assert rates[0] == pytest.approx(
        river_deck.mass_rate_g_per_day / len(src_cells)
    )


def test_no_river_keeps_pure_spill_deck(tmp_path):
    """Without a river polyline the deck is the original spill-only deck: NO RIV
    file, the SRC sits at the single spill cell, and the manifest river fields
    stay at their no-river defaults (regression guard)."""
    d = build_modflow_deck(workdir=tmp_path, **BASE)
    assert not (tmp_path / "gwf_model.riv").exists(), "no RIV file for a pure spill"
    assert d.river_coupled is False
    assert d.river_cell_count == 0
    assert d.along_river_source is False
    # SRC: exactly one record at the spill cell.
    src_text = (tmp_path / "gwt_model.src").read_text()
    n_src = 0
    in_p2 = False
    for line in src_text.splitlines():
        s = line.strip().lower()
        if s.startswith("begin period") and s.split()[-1] == "2":
            in_p2 = True
            continue
        if s.startswith("end period"):
            in_p2 = False
            continue
        if in_p2 and line.strip() and line.split()[0].isdigit():
            n_src += 1
    assert n_src == 1


def test_spill_cell_source_when_along_river_false(tmp_path):
    """along_river_source=False keeps the SRC at the spill cell even with a
    river draped (RIV still written, but the source is the point spill)."""
    d = build_modflow_deck(
        workdir=tmp_path,
        river_polyline_lonlat=RIVER_POLY,
        along_river_source=False,
        **BASE,
    )
    assert d.river_coupled is True  # RIV still draped
    assert d.along_river_source is False
    assert (tmp_path / "gwf_model.riv").is_file()
    src_text = (tmp_path / "gwt_model.src").read_text()
    n_src = sum(
        1
        for line in src_text.splitlines()
        if line.strip()
        and line.strip().split()[0].isdigit()
        and "maxbound" not in line.lower()
    )
    # One spill-cell record in the transient period (and possibly the empty
    # steady-state period header carries maxbound 0, excluded above).
    assert n_src == 1


def test_explicit_conductance_and_stage_override(tmp_path):
    d = build_modflow_deck(
        workdir=tmp_path,
        river_polyline_lonlat=RIVER_POLY,
        streambed_conductance_m2_day=123.0,
        river_stage_m=9.9,
        along_river_source=True,
        **BASE,
    )
    assert d.river_conductance_m2_day == pytest.approx(123.0)
    riv_text = (tmp_path / "gwf_model.riv").read_text().lower()
    assert "1.23000000e+02" in riv_text  # conductance 123
    assert "9.90000000e+00" in riv_text  # explicit stage 9.9
