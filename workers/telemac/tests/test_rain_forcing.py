"""Distributed on-mesh RAINFALL / EVAPORATION knob (row 1).

author_deck emits the native TELEMAC-2D RAIN OR EVAPORATION source term ONLY
when ReachConfig.rain_or_evap_mm_per_day is set (non-None); unset leaves the
deck byte-identical (no rain keywords). Signed: positive = rain, negative =
evaporation. These tests drive author_deck over the tiny mesh (no solver).
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import telemac_river_dye_build as B
from telemac_river_dye_build import ReachConfig


def _tiny_mesh():
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
    centerline = np.array([[x, 0.0] for x in xs], dtype=float)
    return {
        "X": np.array(X, dtype=float),
        "Y": np.array(Y, dtype=float),
        "ring": np.array(ring, dtype=int),
        "ipob": np.array(ipob, dtype=int),
        "centerline": centerline,
    }


_BED = {"bed_top_m": 10.0, "bed_drop_m": 0.5}
_LB = ["inflow", "outflow"]


def _author(cfg, workdir):
    cas_path = str(Path(workdir) / "t2d_river.cas")
    B.author_deck(cfg, _tiny_mesh(), "river.slf", "river.cli", "r2d_river.slf",
                  cas_path, _LB, _BED)
    return Path(cas_path).read_text()


def test_default_reachconfig_has_no_rain_field_set(tmp_path):
    cfg = ReachConfig(workdir=str(tmp_path))
    assert cfg.rain_or_evap_mm_per_day is None


def test_unset_emits_no_rain_block(tmp_path):
    """Default (None) -> the deck carries NO rain keywords (byte-identical)."""
    cfg = ReachConfig(workdir=str(tmp_path))
    cas = _author(cfg, tmp_path)
    assert "RAIN OR EVAPORATION" not in cas


def test_positive_rain_emits_signed_source_term(tmp_path):
    """A positive rate -> RAIN OR EVAPORATION = YES + the mm/day line."""
    cfg = ReachConfig(workdir=str(tmp_path), rain_or_evap_mm_per_day=120.0)
    cas = _author(cfg, tmp_path)
    assert "RAIN OR EVAPORATION             = YES" in cas
    assert "RAIN OR EVAPORATION IN MM PER DAY = 120." in cas
    # single dye tracer -> one rainwater tracer value (clean rain)
    assert "VALUES OF TRACERS IN THE RAIN   = 0." in cas


def test_negative_rate_is_evaporation(tmp_path):
    """A negative net rate models evaporation (water loss); the signed value
    flows verbatim to the deck line."""
    cfg = ReachConfig(workdir=str(tmp_path), rain_or_evap_mm_per_day=-8.0)
    cas = _author(cfg, tmp_path)
    assert "RAIN OR EVAPORATION             = YES" in cas
    assert "RAIN OR EVAPORATION IN MM PER DAY = -8." in cas


def test_rain_block_is_additive_only(tmp_path):
    """Setting rain does not disturb the friction/diffusion literals (the knob
    is purely additive to the physics block)."""
    cfg = ReachConfig(workdir=str(tmp_path), rain_or_evap_mm_per_day=50.0)
    cas = _author(cfg, tmp_path)
    assert "LAW OF BOTTOM FRICTION          = 3" in cas
    assert "FRICTION COEFFICIENT            = 33." in cas
    assert "VELOCITY DIFFUSIVITY            = 1.E-1" in cas
