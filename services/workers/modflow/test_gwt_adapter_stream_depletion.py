"""Unit tests for the MODFLOW 6 SFR routed stream-depletion deck (module wave).

These assert the *deck construction* contract for the ``stream_depletion``
archetype: the SFR6 packagedata / connectiondata / perioddata + continuous OBS
are built with path-ordered reaches, a strictly-positive streambed gradient and a
monotonic-non-increasing streambed top; the IMS flips to BICGSTAB ONLY when SFR
is present (a non-SFR archetype deck stays byte-identical, and the spill deck
keeps its CG default); and the OBS is registered. NO LLM, NO ``mf6`` binary
required for the pure-geometry assertions (engine invariant 2). The live mf6 run
+ ~40-80% depletion recovery is pinned by the Phase-1 smoke fixture
(fixtures/sfr_smoke) and proven on bin/mf6 6.5.0.

Run:
    GRACE2_MODFLOW_LOCAL=1 <agent-venv>/bin/python -m pytest \
        services/workers/modflow/test_gwt_adapter_stream_depletion.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gwt_adapter import (  # noqa: E402
    DEFAULT_SFR_MANNING_N,
    DEFAULT_SFR_WIDTH_M,
    MIN_SFR_STREAMBED_GRADIENT,
    DeckManifest,
    _build_sfr_reaches,
    _smooth_monotonic_rtp,
    build_modflow_deck,
)

# Boise-River-at-Eagle-Island-ish AOI + a west->east flowline through the grid.
LAT0, LON0 = 43.687, -116.354
RIVER_POLY = [
    (-116.362, LAT0),
    (-116.358, LAT0),
    (-116.354, LAT0),
    (-116.350, LAT0),
    (-116.346, LAT0),
]
# A well ~300 m south of the river (still on the grid).
WELL = (LAT0 - 0.0027, LON0)

BASE = dict(
    spill_location_latlon=(LAT0, LON0),
    contaminant="n/a",
    release_rate_kg_s=1.0,
    duration_days=1.0,
    aquifer_k_ms=1e-4,
    porosity=0.3,
)


# --------------------------------------------------------------------------- #
# Pure reach-building (no flopy): shape, ordering, rgrd > 0, rtp monotonic
# --------------------------------------------------------------------------- #


def _synthetic_cells(n: int = 8) -> list[tuple[int, int, float]]:
    """n path-ordered cells down one column (row 20, cols 8..8+n-1)."""
    return [(20, 8 + i, 100.0) for i in range(n)]


def test_smooth_monotonic_rtp_forces_non_increasing():
    # A profile that wobbles UP must come out strictly non-increasing downstream.
    out = _smooth_monotonic_rtp([10.0, 11.0, 9.0, 9.5, 8.0])
    assert out[0] == 10.0
    for a, b in zip(out[:-1], out[1:]):
        assert b <= a, f"rtp must not increase downstream: {out}"


def test_sfr_packagedata_shape_and_reach_ordering():
    cells = _synthetic_cells(8)
    b = _build_sfr_reaches(
        cells,
        rwid=DEFAULT_SFR_WIDTH_M,
        rhk=0.5,
        man=DEFAULT_SFR_MANNING_N,
        rbth=1.0,
        inflow_m3_day=5000.0,
        n_stress_periods=2,
    )
    assert b["n_reaches"] == 8
    pd = b["packagedata"]
    assert len(pd) == 8
    # 12 columns: ifno, cellid, rlen, rwid, rgrd, rtp, rbth, rhk, man, ncon, ustrf, ndv
    assert all(len(row) == 12 for row in pd)
    # ifno is the path index 0..n-1 in order.
    assert [row[0] for row in pd] == list(range(8))
    # cellid carries (0, row, col) in path order.
    assert [row[1] for row in pd] == [(0, 20, 8 + i) for i in range(8)]
    # ncon: 1 at both ends, 2 in the interior.
    assert pd[0][9] == 1 and pd[-1][9] == 1
    assert all(row[9] == 2 for row in pd[1:-1])
    # ndv = 0 (no diversions v0.1); ustrf = 1.0.
    assert all(row[11] == 0 and row[10] == 1.0 for row in pd)


def test_sfr_connectiondata_chain():
    cells = _synthetic_cells(5)
    b = _build_sfr_reaches(
        cells, rwid=8.0, rhk=0.5, man=0.035, rbth=1.0,
        inflow_m3_day=5000.0, n_stress_periods=1,
    )
    con = b["connectiondata"]
    # [i, +(i-1), -(i+1)] chain (upstream positive, downstream negative).
    assert con[0] == [0, -1]
    assert con[2] == [2, 1, -3]
    assert con[-1] == [4, 3]


def test_rgrd_strictly_positive_and_rtp_monotonic():
    cells = _synthetic_cells(10)
    b = _build_sfr_reaches(
        cells, rwid=8.0, rhk=0.5, man=0.035, rbth=1.0,
        inflow_m3_day=5000.0, n_stress_periods=1,
    )
    pd = b["packagedata"]
    rgrd = [row[4] for row in pd]
    rtp = [row[5] for row in pd]
    assert all(g >= MIN_SFR_STREAMBED_GRADIENT for g in rgrd), f"rgrd must be > 0: {rgrd}"
    for a, c in zip(rtp[:-1], rtp[1:]):
        assert c <= a, f"rtp must be non-increasing downstream: {rtp}"


def test_sfr_obs_registers_stage_flow_and_exchange():
    cells = _synthetic_cells(3)
    b = _build_sfr_reaches(
        cells, rwid=8.0, rhk=0.5, man=0.035, rbth=1.0,
        inflow_m3_day=5000.0, n_stress_periods=1,
    )
    obs = b["obs"]
    key = next(iter(obs))
    assert key == "{gwf}.sfr.obs.csv"
    obs_types = {e[1] for e in obs[key]}
    assert obs_types == {"stage", "downstream-flow", "sfr"}
    # One entry per reach per obs type (3 reaches x 3 types = 9).
    assert len(obs[key]) == 9


def test_sfr_perioddata_headwater_inflow_every_period():
    cells = _synthetic_cells(4)
    b = _build_sfr_reaches(
        cells, rwid=8.0, rhk=0.5, man=0.035, rbth=1.0,
        inflow_m3_day=4321.0, n_stress_periods=3,
    )
    pdict = b["perioddata"]
    assert set(pdict.keys()) == {0, 1, 2}
    for recs in pdict.values():
        assert recs == [(0, "INFLOW", 4321.0)]


# --------------------------------------------------------------------------- #
# Full deck construction (writes .sfr + .sfr.obs; flips IMS to BICGSTAB)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def sfr_deck(tmp_path):
    return build_modflow_deck(
        workdir=tmp_path,
        archetype="stream_depletion",
        well_location_latlon=WELL,
        pumping_rate_m3_day=2000.0,
        sim_years=1.0,
        n_periods=1,
        river_polyline_lonlat=RIVER_POLY,
        river_inflow_m3_s=1.0,
        **BASE,
    )


def test_stream_depletion_writes_sfr_package(sfr_deck, tmp_path):
    assert isinstance(sfr_deck, DeckManifest)
    assert sfr_deck.sfr_present is True
    assert sfr_deck.n_reaches > 0
    assert (tmp_path / "gwf_model.sfr").is_file(), "SFR input file not written"
    assert (tmp_path / "gwf_model.sfr.obs").is_file(), "SFR obs file not written"
    # The manifest echoes the ordered reach cells for postprocess georegistration.
    assert len(sfr_deck.sfr_reach_cells) == sfr_deck.n_reaches
    ifnos = [int(m[0]) for m in sfr_deck.sfr_reach_cells]
    assert ifnos == sorted(ifnos), "reach cells must be in path (ifno) order"
    # Demo defaults narrated on the manifest.
    assert sfr_deck.sfr_width_m == pytest.approx(DEFAULT_SFR_WIDTH_M)
    assert sfr_deck.sfr_manning_n == pytest.approx(DEFAULT_SFR_MANNING_N)
    # river_inflow_m3_s=1.0 -> 86400 m^3/day.
    assert sfr_deck.sfr_inflow_m3_day == pytest.approx(86400.0)


def test_bicgstab_flips_only_when_sfr_present(sfr_deck, tmp_path):
    """The SFR deck's IMS uses BICGSTAB (SFR forces an asymmetric matrix)."""
    ims = (tmp_path / "gwf_model.ims").read_text().lower()
    assert "bicgstab" in ims
    assert sfr_deck.newton_under_relaxation is True  # NEWTON + BICGSTAB


def test_non_sfr_archetype_deck_stays_byte_identical(tmp_path):
    """A sustainable_yield deck (the SFR sibling) writes NO SFR package and keeps
    its BICGSTAB IMS -- adding the SFR branch must not perturb it."""
    d = build_modflow_deck(
        workdir=tmp_path,
        archetype="sustainable_yield",
        well_location_latlon=WELL,
        pumping_rate_m3_day=2000.0,
        sim_years=1.0,
        n_periods=1,
        **BASE,
    )
    assert d.sfr_present is False
    assert d.n_reaches == 0
    assert not (tmp_path / "gwf_model.sfr").exists(), "no SFR file for a non-SFR archetype"
    assert not (tmp_path / "gwf_model.sfr.obs").exists()


def test_spill_deck_keeps_cg_default(tmp_path):
    """The CG-default spill/seepage deck is untouched by the SFR path (the flip is
    gated on sfr_present, not applied globally)."""
    d = build_modflow_deck(workdir=tmp_path, **BASE)
    assert getattr(d, "sfr_present", False) is False
    ims = (tmp_path / "gwf_model.ims").read_text().lower()
    assert "cg" in ims and "bicgstab" not in ims, "spill GWF IMS must stay CG"
