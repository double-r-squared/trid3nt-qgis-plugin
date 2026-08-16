"""Offline gate for the pelicun Assessment-API validation / sensitivity templates.

Covers the four registered templates (closed-form validation, mixed
fragility+loss correlation, replacement-threshold sweep, flood foundation
depth-damage) plus two triage-outcome regression assertions that are NOT
user-facing templates:

- row 11 (save/load consistency): a LossModel/DamageModel sample survives a
  save -> reload round-trip byte-for-byte. This is an internal-consistency
  regression, not a modeling question -> a test, not a template.
- row 6 (standalone vs coupled surge consistency): pelicun ships a SINGLE flood
  building alias, so a standalone flood loss and the storm-surge branch of the
  coupled hurricane module resolve to the SAME HAZUS v6.1 flood dataset -> the
  loss functions are identical by construction. The consistency is a resource-
  path identity fact -> a test, not a runnable sensitivity template.

ASCII only. All checks run in-process on synthetic / bundled inputs (no network,
no solver).
"""

from __future__ import annotations

import asyncio
import os

import numpy as np
import pytest

import trid3nt_server.main as _main

_main._import_tools_registry()
from trid3nt_server.agent.tools import TOOL_REGISTRY  # noqa: E402


def _call(name: str, **kw):
    return asyncio.run(TOOL_REGISTRY[name].fn(**kw))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_TEMPLATES = (
    "pelicun_closed_form_validation",
    "pelicun_mixed_fragility_loss_assessment",
    "pelicun_replacement_threshold_override_sweep",
    "pelicun_flood_foundation_depth_damage_sweep",
)


def test_templates_registered_as_pelicun_templates():
    for name in _TEMPLATES:
        entry = TOOL_REGISTRY[name]
        assert entry.metadata.engine == "pelicun"
        assert entry.metadata.tier == "template"
        assert callable(entry.fn)


# ---------------------------------------------------------------------------
# T1: closed-form validation (rows 1 + 7)
# ---------------------------------------------------------------------------


def test_closed_form_damage_state_probability_matches_analytic():
    """Monte-Carlo damage-state probabilities track the analytic lognormal form.

    n=40000 gives a sampling error near 0.002, comfortably under the 0.01 pass
    tolerance the template asserts.
    """
    r = _call("pelicun_closed_form_validation",
              check="damage_state_probability", sample_size=40000)
    assert r["status"] == "ok"
    assert r["passed"] is True
    assert r["max_abs_delta"] <= r["tolerance"]
    # Analytic reference for the default knobs: P(DS0)=0.5, and the three DS
    # probabilities are a valid distribution.
    assert abs(r["p_analytic"][0] - 0.5) < 1e-9
    assert abs(sum(r["p_montecarlo"]) - 1.0) < 1e-6


def test_closed_form_loss_function_identity_reproduces_edp():
    """A 1:1 loss function reproduces the input EDP median + log-dispersion exactly."""
    r = _call("pelicun_closed_form_validation",
              check="loss_function_identity", edp_median=0.50, edp_beta=0.90,
              sample_size=40000)
    assert r["status"] == "ok"
    assert r["passed"] is True
    assert r["delta_median"] <= r["tolerance"]
    assert r["delta_log_std"] <= r["tolerance"]


def test_closed_form_invalid_check_is_typed_error():
    r = _call("pelicun_closed_form_validation", check="not_a_check")
    assert r["status"] == "error"
    assert r["error_code"] == "PELICUN_VALIDATION_INVALID"


# ---------------------------------------------------------------------------
# T2: mixed fragility + loss, correlation sensitivity (rows 2 + 8)
# ---------------------------------------------------------------------------


def test_mixed_assessment_correlation_widens_spread():
    """Both correlation regimes run and produce finite aggregate loss; a perfect
    EDP correlation gives a spread at least as wide as the independent one."""
    r = _call("pelicun_mixed_fragility_loss_assessment", sample_size=6000)
    assert r["status"] == "ok"
    ind = r["regimes"]["independent"]
    perf = r["regimes"]["perfect"]
    for reg in (ind, perf):
        assert reg["mean"] > 0.0
        assert reg["p90"] >= reg["p10"]
    # perfect correlation does not narrow the spread relative to independent
    assert perf["spread_p90_p10"] >= ind["spread_p90_p10"] * 0.98


# ---------------------------------------------------------------------------
# T3: replacement-threshold override sweep + RID inference (rows 9 + 10)
# ---------------------------------------------------------------------------


def test_replacement_threshold_sweep_is_monotone_and_uses_inferred_rid():
    """Raising the excessiveRID capacity threshold reduces the fraction pushed to
    full replacement (fewer buildings exceed the higher capacity). RID is inferred
    from PID (no separate RID demand file)."""
    r = _call("pelicun_replacement_threshold_override_sweep",
              n_threshold_steps=4, sample_size=6000, rid_source="inferred")
    assert r["status"] == "ok"
    assert r["rid_source"] == "inferred"
    fracs = [p["frac_replaced"] for p in r["sweep"]]
    # non-increasing (allow tiny Monte-Carlo noise)
    assert all(fracs[i] >= fracs[i + 1] - 0.03 for i in range(len(fracs) - 1))
    assert r["summary"]["frac_replaced_at_min"] > r["summary"]["frac_replaced_at_max"]


def test_replacement_threshold_fixed_rid_path_runs():
    r = _call("pelicun_replacement_threshold_override_sweep",
              n_threshold_steps=2, sample_size=4000, rid_source="fixed",
              fixed_rid=0.008)
    assert r["status"] == "ok"
    assert r["rid_source"] == "fixed"


# ---------------------------------------------------------------------------
# T5: flood foundation depth-damage sweep (row 5)
# ---------------------------------------------------------------------------


def test_flood_foundation_curves_differ_by_foundation():
    """Each foundation variant yields a distinct HAZUS flood depth-damage curve,
    with monotonic non-decreasing loss vs depth."""
    r = _call("pelicun_flood_foundation_depth_damage_sweep")
    assert r["status"] == "ok"
    assert len(r["curves"]) == 4
    for c in r["curves"]:
        depths = c["depths_ft"]
        lrs = c["loss_ratios"]
        assert depths == sorted(depths)
        # loss ratio non-decreasing with depth
        assert all(lrs[i] <= lrs[i + 1] + 1e-9 for i in range(len(lrs) - 1))
    # the four foundation configurations do not all collapse to one curve
    loss4 = list(r["loss_at_4ft"].values())
    assert len(set(round(x, 3) for x in loss4)) >= 2


def test_flood_foundation_unknown_variant_is_typed_error():
    r = _call("pelicun_flood_foundation_depth_damage_sweep",
              foundation_variants=["no_such_foundation_config"])
    assert r["status"] == "error"
    assert r["error_code"] == "PELICUN_VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Row 11 (TEST-ONLY): LossModel/DamageModel save-load numerical identity.
# ---------------------------------------------------------------------------


def test_damage_sample_save_load_is_numerically_identical(tmp_path):
    """A damage-state sample survives a save -> reload round-trip byte-identical.

    Regression proxy for staged/distributed regional runs where the sample is
    persisted between stages. Uses pelicun's real Assessment API on a small
    synthetic component (mirrors the pelicun CI validation-v1 tail)."""
    import pandas as pd
    from pelicun import assessment, file_io

    a = assessment.Assessment({"PrintLog": False, "Seed": 42})
    demands = pd.DataFrame(
        {"Theta_0": [0.015], "Theta_1": [0.6], "Family": ["lognormal"], "Units": ["rad"]},
        index=pd.MultiIndex.from_tuples([("PID", "1", "1")]),
    )
    a.demand.load_model({"marginals": demands})
    a.demand.generate_sample({"SampleSize": 2000})
    a.stories = 1
    cmp = pd.DataFrame(
        {"Units": ["ea"], "Location": [1], "Direction": [1], "Theta_0": [1], "Blocks": [1]},
        index=["cmp.A"],
    )
    a.asset.load_cmp_model({"marginals": cmp})
    a.asset.generate_cmp_sample(2000)
    fd, dbpath = _mk_damage_db(file_io, a.unit_conversion_factors)
    a.damage.load_model_parameters([fd], set(a.asset.list_unique_component_ids()))
    a.damage.calculate()
    before = a.damage.ds_model.sample.copy()
    out = os.path.join(tmp_path, "dmg.csv")
    a.damage.save_sample(out)
    a.damage.load_sample(out)
    pd.testing.assert_frame_equal(before, a.damage.ds_model.sample)
    _ = dbpath  # path retained only for provenance


def _mk_damage_db(file_io, ucf):
    import pandas as pd
    import tempfile

    df = pd.DataFrame(
        {
            "Demand-Directional": [1], "Demand-Offset": [0],
            "Demand-Type": ["Story Drift Ratio"], "Demand-Unit": ["rad"],
            "LS1-Family": ["lognormal"], "LS1-Theta_0": [0.015], "LS1-Theta_1": [0.5],
            "LS2-Family": ["lognormal"], "LS2-Theta_0": [0.02], "LS2-Theta_1": [0.5],
        },
        index=["cmp.A"],
    )
    df.index.name = "ID"
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    df.to_csv(path)
    loaded = file_io.load_data(path, reindex=False, unit_conversion_factors=ucf)
    os.unlink(path)
    return loaded, path


# ---------------------------------------------------------------------------
# Row 6 (TEST-ONLY): standalone flood vs coupled surge dataset identity.
# ---------------------------------------------------------------------------


def test_standalone_flood_and_coupled_surge_resolve_to_same_dataset():
    """pelicun ships ONE flood building alias, so a standalone flood loss and the
    storm-surge branch of the coupled hurricane module use the SAME HAZUS v6.1
    flood loss functions -- consistency holds by construction (resource-path
    identity). A runnable numeric comparison would require the (unsurfaced)
    coupled wind+surge DL_calculation module; recorded here as the honest
    identity fact instead."""
    import json

    import pelicun

    p = os.path.join(os.path.dirname(pelicun.__file__),
                     "resources", "dlml_resource_paths.json")
    aliases = json.load(open(p))
    surge = aliases["Hazus Hurricane Storm Surge - Buildings"]
    # the only building-flood dataset in the resource map
    flood_paths = {k: v for k, v in aliases.items()
                   if v.strip("/").startswith("flood/building/portfolio")}
    assert surge.strip("/").startswith("flood/building/portfolio/Hazus v6.1")
    # the surge alias IS the sole flood building dataset -> standalone == surge branch
    assert set(flood_paths.values()) == {surge}
