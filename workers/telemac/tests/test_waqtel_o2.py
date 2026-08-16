"""Offline unit tests: WAQTEL O2 do-sag deck authoring (no solve, no network).

WAQTEL O2 (WATER QUALITY PROCESS = 2) is the fifth TELEMAC substance class,
beside the dye tracer, oil, decay and GAIA sediment. It couples the O2 module,
which appends THREE tracers after the dye (DISSOLVED O2, ORGANIC LOAD / CBOD,
NH4 LOAD - nametrac_waqtel order, pinned by the in-image smoke), so author_deck
(a) adds T2,T3,T4 to the graphic printouts, (b) widens INITIAL VALUES OF TRACERS
to four values, (c) widens PRESCRIBED TRACERS VALUES to four-per-boundary with
the mixed CBOD + DO riding in at the inflow, and (d) appends the WAQTEL coupling
+ the O2 steering file. The real DO sag reproducing Streeter-Phelps needs the
image rebuild + a live solve - proven in the in-image V&V, out of scope here.

Run: python3 -m pytest workers/telemac/tests/ -q
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import telemac_river_dye_build as B
from telemac_river_dye_build import ReachConfig, WAQTEL_FILENAME


def _tiny_mesh():
    xs = [0.0, 20.0, 40.0, 60.0, 80.0, 100.0]
    ys = [-15.0, 0.0, 15.0]
    X, Y, ring, ipob = [], [], [], []
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            X.append(x); Y.append(y)
            is_boundary = (i in (0, len(xs) - 1)) or (j in (0, len(ys) - 1))
            ring.append(len(X) - 1) if is_boundary else None
            ipob.append(1 if is_boundary else 0)
    centerline = np.array([[x, 0.0] for x in xs], dtype=float)
    return {
        "X": np.array(X, dtype=float), "Y": np.array(Y, dtype=float),
        "ring": np.array([r for r in ring if r is not None], dtype=int),
        "ipob": np.array(ipob, dtype=int), "centerline": centerline,
    }


_BED = {"bed_top_m": 10.0, "bed_drop_m": 0.5}
_LB = ["inflow", "outflow"]


def _author(cfg, workdir):
    cas_path = str(Path(workdir) / "t2d_river.cas")
    B.author_deck(cfg, _tiny_mesh(), "river.slf", "river.cli", "r2d_river.slf",
                  cas_path, _LB, _BED)
    return Path(cas_path).read_text()


def test_do_sag_emits_waqtel_o2_block_and_steering(tmp_path):
    cfg = ReachConfig(substance_class="do_sag", do_sag_bod_mgl=25.0,
                      do_sag_upstream_do_mgl=8.0, do_sat_mgl=9.1,
                      do_k1_per_day=0.3, do_k2_per_day=0.9, do_k2_formula=0,
                      workdir=str(tmp_path))
    cas = _author(cfg, tmp_path)
    assert "COUPLING WITH" in cas and "'WAQTEL'" in cas
    assert "WAQTEL STEERING FILE" in cas
    assert "WATER QUALITY PROCESS           = 2" in cas
    # WAQTEL O2 appends 3 tracers -> outputs T2,T3,T4
    assert "VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B,T1,T2,T3,T4'" in cas
    # INITIAL VALUES widened to 4 (dye0, O2=upstream_do, OL0, NH4 0)
    assert "INITIAL VALUES OF TRACERS       = 0.;8;0.;0." in cas
    # PRESCRIBED widened to 4-per-boundary; the mixed CBOD + DO ride in at inflow
    pline = [ln for ln in cas.splitlines()
             if ln.startswith("PRESCRIBED TRACERS VALUES")][0]
    assert "25" in pline and "8" in pline       # BOD=25, DO=8 at the inflow
    assert pline.count(";") == 7                 # 8 values (2 boundaries x 4)
    # the steering file carries the verified O2 keywords + zeroed eutro terms
    waq = tmp_path / WAQTEL_FILENAME
    assert waq.exists()
    body = waq.read_text()
    assert "CONSTANT OF DEGRADATION OF ORGANIC LOAD K1    = 0.3" in body
    assert "K2 REAERATION COEFFICIENT                     = 0.9" in body
    assert "FORMULA FOR COMPUTING K2                      = 0" in body
    assert "O2 SATURATION DENSITY OF WATER (CS)           = 9.1" in body
    assert "BENTHIC DEMAND                                = 0." in body
    assert "PHOTOSYNTHESIS P                              = 0." in body
    assert "VEGETAL RESPIRATION R                         = 0." in body
    assert "CONSTANT OF NITRIFICATION KINETIC K4          = 0." in body
    assert "WATER SALINITY                                = 0." in body
    assert all(len(ln) <= 72 for ln in body.splitlines())
    # NO oil / GAIA coupling (mutually exclusive)
    assert "OIL SPILL STEERING FILE" not in cas
    assert "GAIA" not in cas


def test_do_sag_k2_formula_flows_through(tmp_path):
    cfg = ReachConfig(substance_class="do_sag", do_k2_formula=1,
                      do_k1_per_day=0.35, workdir=str(tmp_path))
    _author(cfg, tmp_path)
    body = (tmp_path / WAQTEL_FILENAME).read_text()
    assert "FORMULA FOR COMPUTING K2                      = 1" in body
    assert "CONSTANT OF DEGRADATION OF ORGANIC LOAD K1    = 0.35" in body


# --- non-do_sag classes emit NO WAQTEL O2 (byte-identity guarantee) --------- #
def test_tracer_emits_no_o2(tmp_path):
    cfg = ReachConfig(substance_class="tracer", workdir=str(tmp_path))
    cas = _author(cfg, tmp_path)
    assert "WATER QUALITY PROCESS           = 2" not in cas
    assert "T1,T2,T3,T4" not in cas
    # single-tracer default untouched
    assert "VARIABLES FOR GRAPHIC PRINTOUTS = 'U,V,H,S,B,T1'" in cas
    assert "INITIAL VALUES OF TRACERS       = 0." in cas


def test_reachconfig_do_sag_defaults():
    cfg = ReachConfig()
    assert cfg.substance_class == "tracer"
    assert cfg.do_sag_bod_mgl == 20.0
    assert cfg.do_sat_mgl == 9.0
    assert cfg.do_k1_per_day == 0.3
    assert cfg.do_k2_per_day == 0.9
    assert cfg.do_standard_mgl == 5.0
