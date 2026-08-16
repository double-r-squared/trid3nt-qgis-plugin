"""Offline tests for ``pelicun_hazus_lifeline_seismic_dl_run`` (ADR 0247).

Drives the three HAZUS lifeline-network fragility libraries (transportation
bridge, potable-water pipe, electric-power substation) through the real pelicun
``DL_calculation`` harness in-venv. No image, no network, no fetchers. Asserts the
auto-population engages, the returned damage numbers are pelicun outputs (never
free-generated), and the intensity response is physically monotonic.
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.data import TOOL_REGISTRY
from trid3nt_server.workflows.pelicun.hazus_lifeline_seismic_dl_run.hazus_lifeline_seismic_dl_run import (  # noqa: E501
    build_lifeline_aim,
    build_lifeline_demand_csv,
)

_FN = TOOL_REGISTRY["pelicun_hazus_lifeline_seismic_dl_run"].fn


def _run(**kwargs):
    kwargs.setdefault("realizations", 300)
    kwargs.setdefault("seed", 7)
    return asyncio.run(_FN(**kwargs))


def test_transportation_bridge_classifies_and_costs():
    r = _run(lifeline_class="transportation")
    assert r["status"] == "ok", r
    assert r["asset_subtype"] == "HwyBridge"
    # auto-population classified an HWB bridge type + assigned the GS component
    assert any(str(c).startswith("HWB.GS") for c in r["auto_populated_component"])
    assert r["component_database"] == "Hazus Earthquake - Transportation"
    ls = r["loss_summary"]
    for key in ("mean_repair_cost_ratio", "mean_repair_time_days",
                "collapse_probability"):
        assert isinstance(ls[key], float)
    assert 0.0 <= ls["mean_repair_cost_ratio"] <= 2.0
    # damage-state distribution is a proper (sub-)distribution over the ladder
    assert abs(sum(r["damage_state_probabilities"].values()) - 1.0) < 1e-6
    assert set(r["damage_state_probabilities"]) <= {
        "None", "Slight", "Moderate", "Extensive", "Complete"}


def test_potable_water_pipe_leak_break_taxonomy():
    r = _run(lifeline_class="potable_water")
    assert r["status"] == "ok", r
    assert r["asset_subtype"] == "Pipe"
    assert "aggregate" in r["auto_populated_component"]
    assert r["component_database"] == "Hazus Earthquake - Potable Water"
    # pipes use the HAZUS leak/break repair taxonomy, not the structural ladder
    assert set(r["damage_state_probabilities"]) <= {"None", "Leak", "Break"}
    assert abs(sum(r["damage_state_probabilities"].values()) - 1.0) < 1e-6
    # PGV + PGD are both demand inputs for the buried main
    assert "PGV" in r["demand_levels"] and "PGD" in r["demand_levels"]
    # no repair-cost consequence in the HAZUS potable-water dataset
    assert "loss_summary" not in r


def test_electric_power_substation_damage_states():
    r = _run(lifeline_class="electric_power")
    assert r["status"] == "ok", r
    assert r["asset_subtype"] == "Substation"
    assert any(str(c).startswith("EP.S") for c in r["auto_populated_component"])
    assert r["component_database"] == "Hazus Earthquake - Electric Power"
    assert set(r["demand_levels"]) == {"PGA"}
    assert abs(sum(r["damage_state_probabilities"].values()) - 1.0) < 1e-6


def test_substation_damage_increases_monotonically_with_pga():
    """P(undamaged) must fall as PGA rises - a geographic/physical-correctness gate
    (a fragility-sampling bug would break the monotonic ordering)."""
    none_probs = []
    for pga in (0.2, 0.6, 1.2):
        r = _run(lifeline_class="electric_power", pga_g=pga)
        assert r["status"] == "ok", r
        none_probs.append(r["damage_state_probabilities"].get("None", 0.0))
    assert none_probs[0] > none_probs[1] > none_probs[2], none_probs


def test_reproducible_seed():
    a = _run(lifeline_class="electric_power", seed=123)
    b = _run(lifeline_class="electric_power", seed=123)
    assert a["damage_state_probabilities"] == b["damage_state_probabilities"]


def test_invalid_class_and_realizations_typed_errors():
    bad = asyncio.run(_FN(lifeline_class="pipeline_dream"))
    assert bad["status"] == "error"
    assert bad["error_code"] == "PELICUN_LIFELINE_INVALID_CLASS"
    bad2 = asyncio.run(_FN(lifeline_class="electric_power", realizations=0))
    assert bad2["status"] == "error"
    assert bad2["error_code"] == "PELICUN_DL_CALCULATION_INVALID"


def test_build_lifeline_aim_keys_match_autopop_contract():
    """The AIM builder emits the exact GeneralInformation keys each bundled
    pelicun auto-pop script consumes."""
    common = dict(
        ground_failure=False, bridge_class=502, state_code=39, year_built=1965,
        num_spans=3, max_span_length_m=30.0, skew_deg=20, deck_width_m=12.0,
        structure_length_m=90.0, pipe_diameter_m=0.3, pipe_length_m=60.0,
        pipe_material="DI", substation_voltage="low", substation_anchored=False)
    br = build_lifeline_aim(lifeline_class="transportation", **common)
    gi = br["GeneralInformation"]
    assert gi["assetSubtype"] == "HwyBridge"
    for k in ("BridgeClass", "StateCode", "YearBuilt", "NumOfSpans",
              "MaxSpanLength", "Skew", "DeckWidth", "StructureLength", "units"):
        assert k in gi
    assert br["Applications"]["DL"]["ApplicationData"]["DL_Method"] == (
        "Hazus Earthquake - Transportation")

    pw = build_lifeline_aim(lifeline_class="potable_water", **common)
    assert pw["GeneralInformation"]["type"] == "Pipe"
    for k in ("Diam", "Len", "material", "year"):
        assert k in pw["GeneralInformation"]

    ep = build_lifeline_aim(lifeline_class="electric_power", **common)
    assert ep["GeneralInformation"]["type"] == "Substation"
    for k in ("Voltage", "Anchored"):
        assert k in ep["GeneralInformation"]


def test_build_demand_csv_columns_per_class(tmp_path):
    p = str(tmp_path / "d.csv")
    build_lifeline_demand_csv(
        lifeline_class="potable_water", ground_failure=False, realizations=5,
        intensities={"sa_1_0_g": 0.5, "sa_0_3_g": 0.9, "pga_g": 0.5,
                     "pgv_cmps": 30.0, "pgd_inch": 3.0},
        out_path=p)
    header = open(p, encoding="utf-8").readline().strip()
    assert "1-PGV-1-1" in header and "1-PGD-1-1" in header
    assert "1-PGA-1-1" not in header
