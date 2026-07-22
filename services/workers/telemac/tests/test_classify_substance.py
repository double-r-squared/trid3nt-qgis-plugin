"""Offline unit tests: WAQTEL v1a substance routing (pure function; no network).

``classify_substance`` (agent-side, in model_river_dye_release_scenario) is the
ONE seam that routes a substance string to a TELEMAC substance class. M3 added
the oil class; WAQTEL v1a adds the third: a first-order DECAY class (sewage /
E. coli / effluent) that rides the UNCHANGED dye tracer but couples WAQTEL with
WATER QUALITY PROCESS = 17 so the plume ALSO decays. This pins the routing:

  * oil-family words still classify as ('oil', preset) - byte-identical,
  * decaying / bacterial words classify as ('decay', {law, coef}) with the
    WAQTEL degradation law + a literature-default coefficient,
  * a plain dye (or anything else) stays the conservative ('tracer', None).

Order matters: oil is matched FIRST, then decay, else tracer - so 'crude oil'
stays oil and a bare 'dye' stays a conservative tracer. Both period-stripped
variants ("e coli"/"ecoli", from the run_telemac alnum sanitize) and the raw
("e. coli") route to decay so classify matches on either path.

The function lives in the agent package (the in-repo server tree); we shim the
agent + contracts src onto sys.path so this imports the tree being edited, NOT a
vendored copy. If the agent deps are unavailable on the runner, the module is
skipped rather than failing the worker suite.

Run: python3 -m pytest services/workers/telemac/tests/ -q
"""
import sys
from pathlib import Path

import pytest

# Shim the canonical agent + contracts src to the FRONT so classify_substance
# resolves to the tree under edit (parents[4] == the repo root).
_ROOT = Path(__file__).resolve().parents[4]
for _p in (_ROOT / "services" / "agent" / "src",
           _ROOT / "packages" / "contracts" / "src"):
    sp = str(_p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

try:
    from grace2_agent.workflows.model_river_dye_release_scenario import (
        classify_substance,
    )
except Exception as exc:  # noqa: BLE001 - agent deps absent -> skip, never fail
    pytest.skip(f"agent package unimportable ({exc})", allow_module_level=True)


# --- decay class: bacterial / decaying substances --------------------------- #
@pytest.mark.parametrize("s", [
    "sewage", "e. coli", "e.coli", "e coli", "ecoli", "coliform", "coli",
    "bacteria", "bacterial", "effluent", "wastewater", "die-off",
    "raw sewage discharge", "fecal coliform bacteria",
])
def test_bacterial_substances_are_decay_t90(s):
    cls, payload = classify_substance(s)
    assert cls == "decay"
    assert isinstance(payload, dict)
    # bacterial keywords default to the T90 die-off law (1) with a ~2 h default.
    assert payload["law"] == 1
    assert payload["coef"] == pytest.approx(2.0)


@pytest.mark.parametrize("s", ["decaying", "half-life", "a decaying pollutant"])
def test_generic_decay_is_first_order(s):
    cls, payload = classify_substance(s)
    assert cls == "decay"
    assert isinstance(payload, dict)
    # generic decay keywords fall to a mild first-order k in h^-1 (law 2).
    assert payload["law"] == 2
    assert payload["coef"] > 0.0


# --- oil class stays oil (regression pin) ----------------------------------- #
@pytest.mark.parametrize("s,preset", [
    ("oil", "light_crude"),
    ("crude oil", "light_crude"),
    ("diesel", "diesel"),
    ("gasoline spill", "diesel"),
    ("heavy fuel oil", "heavy_fuel"),
    ("bunker fuel", "heavy_fuel"),
])
def test_oil_family_stays_oil(s, preset):
    assert classify_substance(s) == ("oil", preset)


# --- plain dye / anything else stays a conservative tracer ------------------ #
@pytest.mark.parametrize("s", ["dye", "tracer", "", None, "water", "salt",
                               "red dye", "some chemical"])
def test_plain_substance_stays_tracer(s):
    assert classify_substance(s) == ("tracer", None)


def test_oil_beats_decay_on_order():
    # A string carrying BOTH an oil word and a decay word must classify as oil
    # (oil is matched first) - proves the branch ORDER the task requires.
    assert classify_substance("oily sewage")[0] == "oil"


# --- GAIA sediment class: settling sediment substances ---------------------- #
@pytest.mark.parametrize("s,exp_type", [
    ("sediment", "sand"),
    ("sand", "sand"),
    ("silt", "silt"),
    ("mud", "mud"),
    ("slurry", "sand"),
    ("tailings", "silt"),
    ("sediment-laden runoff", "silt"),
    ("fine sand washing downstream", "sand"),
    ("a mud slug in the river", "mud"),
])
def test_sediment_substances_are_sediment(s, exp_type):
    cls, payload = classify_substance(s)
    assert cls == "sediment"
    assert isinstance(payload, dict)
    assert payload["type"] == exp_type
    # each type carries a default d50 in microns (fine sand ~200, silt ~20-30,
    # mud ~8) - a demo default the run_telemac grain_size_um param can override.
    assert payload["grain_size"] > 0.0


def test_sediment_does_not_shadow_oil_or_decay():
    # oil + decay still win over sediment (branch order oil -> decay -> sediment):
    # 'oily sand' is oil, 'sewage sediment' is decay, plain 'sand' is sediment.
    assert classify_substance("oily sand")[0] == "oil"
    assert classify_substance("sewage sediment")[0] == "decay"
    assert classify_substance("sand")[0] == "sediment"
