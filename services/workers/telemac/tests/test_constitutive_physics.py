"""Offline unit tests: TELEMAC-PHYS-1 constitutive-physics override authoring.

The TELEMAC river-dye deck historically pinned four constitutive constants as
bare f-string literals (cookie-cutter, exposed nowhere):

    LAW OF BOTTOM FRICTION          = 3
    FRICTION COEFFICIENT            = 33.
    VELOCITY DIFFUSIVITY            = 1.E-1
    COEFFICIENT FOR DIFFUSION OF TRACERS     = 1.E-1

They are now optional ReachConfig overrides (friction_law / friction_coefficient
/ velocity_diffusivity / tracer_diffusivity), each defaulting to None.

SAFETY INVARIANT (the whole point): when the override is None (unset) the deck
MUST emit the EXACT historical literal string, byte-identical to every prior run.
A set value flows to the deck line. These tests drive author_deck over the tiny
synthetic mesh (no gmsh, no solve, no network) and assert BOTH directions.

Run: python3 -m pytest services/workers/telemac/tests/test_constitutive_physics.py -q
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


# The EXACT historical deck lines (spacing preserved from the f-string template).
_HIST_FRIC_LAW = "LAW OF BOTTOM FRICTION          = 3"
_HIST_FRIC_COEF = "FRICTION COEFFICIENT            = 33."
_HIST_VEL_DIFF = "VELOCITY DIFFUSIVITY            = 1.E-1"
_HIST_TRACER_DIFF = "COEFFICIENT FOR DIFFUSION OF TRACERS     = 1.E-1"


# --- (a) UNSET -> byte-identical historical literals ------------------------ #
def test_unset_physics_emits_historical_literals(tmp_path):
    """Default ReachConfig (all four overrides None) -> the EXACT old lines."""
    cfg = ReachConfig(workdir=str(tmp_path))
    # sanity: the fields really are unset (None), so this is the default path.
    assert cfg.friction_coefficient is None
    assert cfg.friction_law is None
    assert cfg.velocity_diffusivity is None
    assert cfg.tracer_diffusivity is None

    cas = _author(cfg, tmp_path)

    assert _HIST_FRIC_LAW in cas
    assert _HIST_FRIC_COEF in cas
    assert _HIST_VEL_DIFF in cas
    assert _HIST_TRACER_DIFF in cas
    # and NOTHING leaked a re-formatted default (e.g. "33.0" / "0.1").
    assert "FRICTION COEFFICIENT            = 33.0" not in cas
    assert "VELOCITY DIFFUSIVITY            = 0.1" not in cas
    assert "COEFFICIENT FOR DIFFUSION OF TRACERS     = 0.1" not in cas


def test_unset_deck_is_byte_identical_to_pinned_historical_block(tmp_path):
    """Stronger: the friction/diffusion block of the default deck is byte-for-
    byte the pinned historical fragment (order + spacing + values)."""
    cfg = ReachConfig(workdir=str(tmp_path))
    cas = _author(cfg, tmp_path)
    historical_block = (
        f"{_HIST_FRIC_LAW}\n{_HIST_FRIC_COEF}\n{_HIST_VEL_DIFF}\n"
    )
    assert historical_block in cas
    assert _HIST_TRACER_DIFF in cas


# --- (b) SET -> deck carries the user value --------------------------------- #
def test_set_physics_flows_user_values_to_deck(tmp_path):
    cfg = ReachConfig(
        workdir=str(tmp_path),
        friction_coefficient=40.0,
        friction_law=4,
        velocity_diffusivity=0.5,
        tracer_diffusivity=0.25,
    )
    cas = _author(cfg, tmp_path)
    assert "LAW OF BOTTOM FRICTION          = 4" in cas
    assert "FRICTION COEFFICIENT            = 40" in cas
    assert "VELOCITY DIFFUSIVITY            = 0.5" in cas
    assert "COEFFICIENT FOR DIFFUSION OF TRACERS     = 0.25" in cas
    # the historical defaults are GONE when overridden.
    assert _HIST_FRIC_COEF not in cas
    assert _HIST_TRACER_DIFF not in cas


def test_partial_set_leaves_unset_lines_historical(tmp_path):
    """Setting only friction leaves the two diffusivity lines byte-identical."""
    cfg = ReachConfig(workdir=str(tmp_path), friction_coefficient=50.0)
    cas = _author(cfg, tmp_path)
    assert "FRICTION COEFFICIENT            = 50" in cas
    # diffusivities untouched -> historical literals preserved.
    assert _HIST_VEL_DIFF in cas
    assert _HIST_TRACER_DIFF in cas
    # friction_law unset -> still the historical default.
    assert _HIST_FRIC_LAW in cas
