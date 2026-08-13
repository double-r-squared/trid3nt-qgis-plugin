"""ADR 0216: GAIA v2 erodible-bed morphodynamics deck authoring.

Pins the write_gaia_deck branch: erodible_bed=True emits the bedload recipe
(BED LOAD FOR ALL SANDS = YES + LAYERS INITIAL THICKNESS > 0 + a bed-load
transport formula + MORPHOLOGICAL FACTOR, SUSPENSION off), while erodible_bed
False stays byte-identical to the v1 supply-limited suspended deck.
"""
from __future__ import annotations

import numpy as np
import pytest

from services.workers.telemac import telemac_river_dye_build as B


def _read(path):
    with open(path) as f:
        return f.read()


def _tiny_mesh():
    """6x3 ribbon along y=0 (mid-row interior) - enough for spill_point + author
    without a real solve; mirrors the WAQTEL/O2 offline deck tests."""
    xs = [0.0, 20.0, 40.0, 60.0, 80.0, 100.0]
    ys = [-15.0, 0.0, 15.0]
    X, Y, ring, ipob = [], [], [], []
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            X.append(x)
            Y.append(y)
            is_boundary = (i in (0, len(xs) - 1)) or (j in (0, len(ys) - 1))
            if is_boundary:
                ring.append(len(X) - 1)
            ipob.append(1 if is_boundary else 0)
    return {
        "X": np.array(X, dtype=float),
        "Y": np.array(Y, dtype=float),
        "ring": np.array(ring, dtype=int),
        "ipob": np.array(ipob, dtype=int),
        "centerline": np.array([[x, 0.0] for x in xs], dtype=float),
    }


def _author_t2d(cfg, workdir):
    cas_path = str(workdir / "t2d_river.cas")
    B.author_deck(cfg, _tiny_mesh(), "river.slf", "river.cli", "r2d_river.slf",
                  cas_path, ["inflow", "outflow"],
                  {"bed_top_m": 10.0, "bed_drop_m": 0.5})
    return _read(cas_path)


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


def test_erodible_sediment_couples_gaia_in_t2d_deck(tmp_path):
    # ADR 0216 false-green fix (deck-level proof): the config the server stages
    # for a SCOUR prompt - substance_class='sediment', erodible_bed=True - MUST
    # author the GAIA coupling into the t2d deck. The old mislabeled run staged
    # NO substance_class, so the deck carried ZERO GAIA keywords (a plain tracer
    # solve that only looked morphodynamic).
    cfg = B.ReachConfig(substance_class="sediment", erodible_bed=True,
                        bed_thickness_m=4.0, bedload_formula=1,
                        morphological_factor=20.0, workdir=str(tmp_path))
    cas = _author_t2d(cfg, tmp_path)
    assert "COUPLING WITH" in cas and "'GAIA'" in cas
    assert "GAIA STEERING FILE" in cas
    assert B.GAIA_STEERING_FILENAME in cas
    # v2 erodible-bed appends NO suspended tracer -> the dye stays the sole t2d
    # tracer (single-tracer graphic printouts, unlike v1 suspended's T2).
    assert "VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B,T1'" in cas
    # and the steering file carries the bedload recipe.
    steering = _read(tmp_path / B.GAIA_STEERING_FILENAME)
    assert "BED LOAD FOR ALL SANDS          = YES" in steering


@pytest.mark.parametrize("thick,expected", [(0.005, "0.01"), (5.0, "5")])
def test_gaia_v2_bed_thickness_floored(tmp_path, thick, expected):
    cfg = B.ReachConfig(substance_class="sediment", erodible_bed=True,
                        bed_thickness_m=thick, workdir=str(tmp_path))
    B.write_gaia_deck(cfg, "river.slf", "river.cli", str(tmp_path))
    deck = _read(tmp_path / B.GAIA_STEERING_FILENAME)
    assert f"LAYERS INITIAL THICKNESS        = {expected}" in deck


# --- ADR 0240: GAIA v3 multi-class graded sediment (grain sorting) ---------- #

def test_normalize_gradation_renormalizes_sorts_and_caps():
    # renormalizes weights to sum 1, sorts fine->coarse, drops <2-class specs
    g = B._normalize_gradation([[400, 1.0], [100, 1.0], [1000, 2.0]])
    assert [um for um, _ in g] == [100.0, 400.0, 1000.0]  # sorted
    assert abs(sum(fr for _, fr in g) - 1.0) < 1e-9        # renormalized
    assert B._normalize_gradation([[200, 1.0]]) == []      # 1 class -> single-path
    assert B._normalize_gradation([]) == []
    # d50 clamp to [5,2000] um
    g2 = B._normalize_gradation([[3, 0.5], [5000, 0.5]])
    assert [um for um, _ in g2] == [5.0, 2000.0]


def test_gaia_v3_multiclass_deck_emits_sorted_classes(tmp_path):
    cfg = B.ReachConfig(substance_class="sediment", erodible_bed=True,
                        bed_thickness_m=5.0, bedload_formula=1,
                        morphological_factor=20.0,
                        sediment_gradation=[[100.0, 0.3333], [400.0, 0.3333],
                                            [1200.0, 0.3334]],
                        workdir=str(tmp_path))
    B.write_gaia_deck(cfg, "river.slf", "river.cli", str(tmp_path))
    deck = _read(tmp_path / B.GAIA_STEERING_FILENAME)
    # multi-class arrays (3 NCO classes, semicolon-separated)
    assert "CLASSES TYPE OF SEDIMENT        = NCO;NCO;NCO" in deck
    assert "CLASSES SEDIMENT DIAMETERS      = 0.0001;0.0004;0.0012" in deck
    assert "CLASSES INITIAL FRACTION        = 0.3333;0.3333;0.3334" in deck
    # Egiazaroff hiding + bedload (sorting mechanism), D50 output var
    assert "HIDING FACTOR FORMULA           = 1" in deck
    assert "BED LOAD FOR ALL SANDS          = YES" in deck
    assert "SUSPENSION FOR ALL SANDS        = NO" in deck
    assert "D50" in deck  # surface mean-diameter output = sorting signature
    # DAMOCLES 72-char clamp holds even with the widened arrays
    assert all(len(ln) <= 72 for ln in deck.splitlines())


def test_gaia_single_class_gradation_falls_back(tmp_path):
    # a 1-class gradation is not a mixture -> single-class deck (no CLASSES arrays)
    cfg = B.ReachConfig(substance_class="sediment", erodible_bed=True,
                        sediment_gradation=[[300.0, 1.0]], workdir=str(tmp_path))
    B.write_gaia_deck(cfg, "river.slf", "river.cli", str(tmp_path))
    deck = _read(tmp_path / B.GAIA_STEERING_FILENAME)
    assert "NCO;NCO" not in deck
    assert "HIDING FACTOR FORMULA" not in deck
