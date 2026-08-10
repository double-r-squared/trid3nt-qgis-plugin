"""ADR 0216: GAIA v2 erodible-bed morphodynamics deck authoring.

Pins the write_gaia_deck branch: erodible_bed=True emits the bedload recipe
(BED LOAD FOR ALL SANDS = YES + LAYERS INITIAL THICKNESS > 0 + a bed-load
transport formula + MORPHOLOGICAL FACTOR, SUSPENSION off), while erodible_bed
False stays byte-identical to the v1 supply-limited suspended deck.
"""
from __future__ import annotations

import pytest

from services.workers.telemac import telemac_river_dye_build as B


def _read(path):
    with open(path) as f:
        return f.read()


def test_gaia_v1_supply_limited_deck_unchanged(tmp_path):
    cfg = B.ReachConfig(substance_class="sediment", grain_size_um=200.0,
                        dye_conc_mgl=100.0, erodible_bed=False,
                        workdir=str(tmp_path))
    B.write_gaia_deck(cfg, "river.slf", "river.cli", str(tmp_path))
    deck = _read(tmp_path / B.GAIA_STEERING_FILENAME)
    assert "SUSPENSION FOR ALL SANDS        = YES" in deck
    assert "BED LOAD FOR ALL SANDS          = NO" in deck
    assert "LAYERS INITIAL THICKNESS        = 0." in deck
    assert "MORPHOLOGICAL FACTOR" not in deck


def test_gaia_v2_erodible_deck_enables_bedload_and_stock(tmp_path):
    cfg = B.ReachConfig(substance_class="sediment", grain_size_um=300.0,
                        erodible_bed=True, bed_thickness_m=4.0,
                        bedload_formula=1, morphological_factor=20.0,
                        workdir=str(tmp_path))
    B.write_gaia_deck(cfg, "river.slf", "river.cli", str(tmp_path))
    deck = _read(tmp_path / B.GAIA_STEERING_FILENAME)
    assert "BED LOAD FOR ALL SANDS          = YES" in deck
    assert "BED-LOAD TRANSPORT FORMULA FOR ALL SANDS = 1" in deck
    assert "LAYERS INITIAL THICKNESS        = 4" in deck
    assert "MORPHOLOGICAL FACTOR            = 20" in deck
    # bedload-only: no suspended class / supply source keyword
    assert "SUSPENSION FOR ALL SANDS        = NO" in deck
    assert "SUSPENDED SEDIMENTS CONCENTRATION VALUES AT THE SOURCES" not in deck
    # DAMOCLES 72-char clamp holds
    assert all(len(ln) <= 72 for ln in deck.splitlines())


@pytest.mark.parametrize("thick,expected", [(0.005, "0.01"), (5.0, "5")])
def test_gaia_v2_bed_thickness_floored(tmp_path, thick, expected):
    cfg = B.ReachConfig(substance_class="sediment", erodible_bed=True,
                        bed_thickness_m=thick, workdir=str(tmp_path))
    B.write_gaia_deck(cfg, "river.slf", "river.cli", str(tmp_path))
    deck = _read(tmp_path / B.GAIA_STEERING_FILENAME)
    assert f"LAYERS INITIAL THICKNESS        = {expected}" in deck
