"""Unit tests for the MODFLOW 6 CSUB land-subsidence deck (module wave).

These assert the *deck construction* contract for the ``land_subsidence``
archetype: the CSUB packagedata + continuous OBS are built with one no-delay
HEAD_BASED interbed per pumped footprint cell; the STO specific storage ``ss`` is
dropped to 0 ONLY when land_subsidence is active (the mf6-enforced storage
double-count guard - a non-CSUB archetype deck keeps its ss and writes NO CSUB
package); and the compaction + z-displacement outputs are registered. NO LLM, NO
``mf6`` binary required for the pure-geometry assertions (engine invariant 2). The
live mf6 run + the STO-fix head-decline match + the compaction file/tag/sign are
pinned by the Phase-1 smoke fixture (fixtures/csub_smoke) and proven on
bin/mf6 6.5.0.

Run:
    TRID3NT_MODFLOW_LOCAL=1 <agent-venv>/bin/python -m pytest \
        services/workers/modflow/test_gwt_adapter_land_subsidence.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gwt_adapter import (  # noqa: E402
    CSUB_STO_SS_FLOOR,
    DEFAULT_CSUB_INTERBED_THICK_FRAC,
    DEFAULT_CSUB_SSE_ELASTIC,
    DEFAULT_CSUB_SSV_INELASTIC,
    DeckManifest,
    _build_csub_interbeds,
    _footprint_cells_around,
    build_modflow_deck,
)

# San-Joaquin-Valley-ish AOI + a pumping well (Mendota corridor).
LAT0, LON0 = 36.75, -120.38
WELL = (LAT0, LON0)

BASE = dict(
    spill_location_latlon=(LAT0, LON0),
    contaminant="n/a",
    release_rate_kg_s=1.0,
    duration_days=1.0,
    aquifer_k_ms=1e-4,
    porosity=0.3,
)


# --------------------------------------------------------------------------- #
# Pure interbed-building (no flopy): footprint, shape, boundnames, OBS types
# --------------------------------------------------------------------------- #


def test_footprint_is_well_cell_plus_8_neighbours():
    cells = _footprint_cells_around(20, 20, nrow=40, ncol=40)
    assert len(cells) == 9
    assert (20, 20) in cells
    # 3x3 block centred on (20, 20).
    assert set(cells) == {(r, c) for r in (19, 20, 21) for c in (19, 20, 21)}


def test_footprint_clamps_at_grid_edge():
    # A well in the corner drops out-of-bounds neighbours (still valid, smaller).
    cells = _footprint_cells_around(0, 0, nrow=40, ncol=40)
    assert (0, 0) in cells
    assert all(0 <= r < 40 and 0 <= c < 40 for (r, c) in cells)
    assert len(cells) == 4  # the corner 2x2 block


def test_csub_packagedata_shape_and_boundnames():
    cells = [(20, 20), (20, 21), (21, 20)]
    b = _build_csub_interbeds(
        cells,
        ssv=DEFAULT_CSUB_SSV_INELASTIC,
        sse=DEFAULT_CSUB_SSE_ELASTIC,
        thick_frac=DEFAULT_CSUB_INTERBED_THICK_FRAC,
        theta=0.3,
    )
    assert b["n_interbeds"] == 3
    pd = b["packagedata"]
    assert len(pd) == 3
    # 12 columns: icsubno, cellid, cdelay, pcs0, thick, rnb, ssv_cc, sse_cr,
    #             theta, kv, h0, boundname
    assert all(len(row) == 12 for row in pd)
    # icsubno is the path index 0..n-1.
    assert [row[0] for row in pd] == [0, 1, 2]
    # cellid carries (0, row, col).
    assert [row[1] for row in pd] == [(0, 20, 20), (0, 20, 21), (0, 21, 20)]
    # cdelay is "nodelay" (v1) and pcs0 == 0.0 (initial head = preconsolidation).
    assert all(row[2] == "nodelay" for row in pd)
    assert all(row[3] == 0.0 for row in pd)
    # Ssv >> Sse (the elastic/inelastic contrast IS the subsidence physics).
    assert all(row[6] == pytest.approx(DEFAULT_CSUB_SSV_INELASTIC) for row in pd)
    assert all(row[7] == pytest.approx(DEFAULT_CSUB_SSE_ELASTIC) for row in pd)
    assert DEFAULT_CSUB_SSV_INELASTIC > 10 * DEFAULT_CSUB_SSE_ELASTIC
    # boundnames sub_r{i} (mf6 UPPERCASES into SUB_R{i} in the OBS csv).
    assert [row[11] for row in pd] == ["sub_r0", "sub_r1", "sub_r2"]


def test_csub_obs_registers_total_inelastic_elastic_per_interbed():
    b = _build_csub_interbeds(
        [(20, 20), (20, 21)],
        ssv=2e-3, sse=5e-5, thick_frac=0.5, theta=0.3,
    )
    obs = b["obs"]
    key = next(iter(obs))
    assert key == "{gwf}.csub.obs.csv"
    obs_types = {e[1] for e in obs[key]}
    assert obs_types == {
        "interbed-compaction",
        "inelastic-compaction",
        "elastic-compaction",
    }
    # One entry per interbed per obs type (2 interbeds x 3 types = 6).
    assert len(obs[key]) == 6


def test_csub_interbeds_requires_at_least_one_cell():
    with pytest.raises(ValueError):
        _build_csub_interbeds([], ssv=2e-3, sse=5e-5, thick_frac=0.5, theta=0.3)


# --------------------------------------------------------------------------- #
# Full deck construction (writes .csub + .csub.obs; STO ss floored to 0)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def csub_deck(tmp_path):
    return build_modflow_deck(
        workdir=tmp_path,
        archetype="land_subsidence",
        well_location_latlon=WELL,
        pumping_rate_m3_day=4000.0,
        sim_years=10.0,
        n_periods=10,
        **BASE,
    )


def test_land_subsidence_writes_csub_package(csub_deck, tmp_path):
    assert isinstance(csub_deck, DeckManifest)
    assert csub_deck.csub_present is True
    assert csub_deck.n_interbeds > 0
    assert (tmp_path / "gwf_model.csub").is_file(), "CSUB input file not written"
    assert (tmp_path / "gwf_model.csub.obs").is_file(), "CSUB obs file not written"
    # The manifest echoes the ordered interbed cells for postprocess georegistration.
    assert len(csub_deck.csub_interbed_cells) == csub_deck.n_interbeds
    icsubnos = [int(m[0]) for m in csub_deck.csub_interbed_cells]
    assert icsubnos == sorted(icsubnos), "interbed cells must be in icsubno order"
    # Demo defaults narrated on the manifest.
    assert csub_deck.csub_ssv_inelastic_m == pytest.approx(DEFAULT_CSUB_SSV_INELASTIC)
    assert csub_deck.csub_sse_elastic_m == pytest.approx(DEFAULT_CSUB_SSE_ELASTIC)
    assert csub_deck.csub_interbed_thick_frac == pytest.approx(
        DEFAULT_CSUB_INTERBED_THICK_FRAC
    )


def test_csub_registers_compaction_and_zdisplacement_output(csub_deck, tmp_path):
    """The CSUB input file records the compaction + z-displacement filerecords
    (the postprocess-parse targets)."""
    csub_txt = (tmp_path / "gwf_model.csub").read_text().lower()
    # mf6 writes "COMPACTION FILEOUT <file>" / "ZDISPLACEMENT FILEOUT <file>".
    assert "compaction" in csub_txt and "fileout" in csub_txt
    assert "gwf_model.csub.compaction.bin" in csub_txt
    assert "zdisplacement" in csub_txt
    assert "gwf_model.csub.zdisp.bin" in csub_txt
    # HEAD_BASED formulation (v1) is on.
    assert "head_based" in csub_txt


def test_sto_ss_dropped_to_zero_only_when_csub_present(csub_deck, tmp_path):
    """The STO ss is floored to 0 when CSUB is present (mf6-enforced double-count
    guard): every STO ss token is the floor value."""
    assert CSUB_STO_SS_FLOOR == 0.0
    sto_txt = (tmp_path / "gwf_model.sto").read_text().lower()
    assert "ss" in sto_txt
    # The ss GRIDDATA constant must be 0 (no non-zero skeletal Ss under CSUB).
    # flopy writes "ss\n  constant  0.00000000" for a scalar ss.
    assert "constant" in sto_txt
    # No non-zero ss constant survives (the plain default 1e-5 would print as
    # 1.00000000E-05); assert the CSUB deck carries a zero ss constant instead.
    import re

    ss_block = sto_txt.split("ss", 1)[1][:120]
    nums = re.findall(r"[0-9]+\.[0-9]+(?:e[+-][0-9]+)?", ss_block)
    assert nums, "no ss constant found in the STO file"
    assert float(nums[0]) == pytest.approx(0.0), f"CSUB STO ss must be 0, got {nums[0]}"


def test_non_csub_archetype_deck_stays_byte_identical(tmp_path):
    """A sustainable_yield deck (the CSUB sibling) writes NO CSUB package and keeps
    its non-zero STO ss -- adding the CSUB branch must not perturb it."""
    d = build_modflow_deck(
        workdir=tmp_path,
        archetype="sustainable_yield",
        well_location_latlon=WELL,
        pumping_rate_m3_day=4000.0,
        sim_years=10.0,
        n_periods=10,
        **BASE,
    )
    assert d.csub_present is False
    assert d.n_interbeds == 0
    assert not (tmp_path / "gwf_model.csub").exists(), "no CSUB file for a non-CSUB archetype"
    assert not (tmp_path / "gwf_model.csub.obs").exists()
    # STO ss is the plain default (non-zero), NOT floored.
    import re

    sto_txt = (tmp_path / "gwf_model.sto").read_text().lower()
    ss_block = sto_txt.split("ss", 1)[1][:120]
    nums = re.findall(r"[0-9]+\.[0-9]+(?:e[+-][0-9]+)?", ss_block)
    assert nums and float(nums[0]) > 0.0, "sustainable_yield STO ss must stay non-zero"
