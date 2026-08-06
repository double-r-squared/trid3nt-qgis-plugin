"""Offline gate for the pelicun DL_calculation CLI harness + its two templates.

Covers the ADR 0160 machinery front:

- ``_dl_calculation.run_dl_calculation``: the tempdir + serialized-cwd +
  seed-injected harness around pelicun's ``DL_calculation.run_pelicun`` (verified
  through the template, plus a direct cwd-restore assertion).
- ``pelicun_hazus_seismic_dl_run`` (row auto_populated_building_type_seismic_run):
  the e1 fixture reproduced -- auto-populated component + the 20-file output
  manifest + coupled-EDP demand reproduction + a seeded (reproducible) loss
  summary.
- ``pelicun_hazus_eq_version_comparison`` (row hazus_eq_v5_vs_v6_dataset_comparison):
  v5.1-by-path vs v6.1 at the Assessment API; the shared-type shift is zero and
  the v6.1 coverage delta is reported.

ASCII only. All checks run in-process on bundled pelicun inputs (no network, no
external solver).
"""

from __future__ import annotations

import asyncio
import os

import trid3nt_server.main as _main

_main._import_tools_registry()
from trid3nt_server.agent.tools import TOOL_REGISTRY  # noqa: E402


def _call(name: str, **kw):
    return asyncio.run(TOOL_REGISTRY[name].fn(**kw))


_TEMPLATES = (
    "pelicun_hazus_seismic_dl_run",
    "pelicun_hazus_eq_version_comparison",
)


def test_templates_registered_as_pelicun_templates():
    for name in _TEMPLATES:
        entry = TOOL_REGISTRY[name]
        assert entry.metadata.engine == "pelicun"
        assert entry.metadata.tier == "template"
        assert callable(entry.fn)


# ---------------------------------------------------------------------------
# Harness: cwd restore
# ---------------------------------------------------------------------------


def test_harness_restores_cwd_and_reproduces_e1_manifest():
    """The seismic DL run leaves cwd untouched and reproduces e1's 20-file
    manifest exactly (its input AIM maps to AIM.json/AIM_ap.json here)."""
    before = os.getcwd()
    r = _call("pelicun_hazus_seismic_dl_run", realizations=50)
    assert os.getcwd() == before
    assert r["status"] == "ok"
    assert r["output_file_count"] == 20
    assert r["manifest_matches_reference"] is True
    assert r["manifest_delta"]["missing_vs_reference"] == []
    assert r["manifest_delta"]["extra_vs_reference"] == []


# ---------------------------------------------------------------------------
# Row 1: auto-populated seismic DL run
# ---------------------------------------------------------------------------


def test_seismic_dl_run_auto_populates_e1_component():
    """Default (e1) attributes auto-populate the lifeline C1 low-rise pre-code
    component and resolve the v6.1 earthquake buildings dataset."""
    r = _call("pelicun_hazus_seismic_dl_run", realizations=100)
    assert r["status"] == "ok"
    assert r["auto_populated_component"] == ["LF.C1.L.PC"]
    assert r["component_database"] == "Hazus Earthquake - Buildings"
    # coupled_edp reproduces the input demand (used directly, not resampled)
    assert r["coupled_demand_max_abs_delta"] is not None
    assert r["coupled_demand_max_abs_delta"] < 1e-6
    ls = r["loss_summary"]
    assert 0.0 <= ls["mean_repair_cost_ratio"] <= 1.0
    assert 0.0 <= ls["collapse_probability"] <= 1.0


def test_seismic_dl_run_is_reproducible_under_fixed_seed():
    """Two runs at the same seed give a byte-identical seeded loss summary."""
    a = _call("pelicun_hazus_seismic_dl_run", realizations=100, seed=7)
    b = _call("pelicun_hazus_seismic_dl_run", realizations=100, seed=7)
    assert a["status"] == b["status"] == "ok"
    assert a["loss_summary"] == b["loss_summary"]


def test_seismic_dl_run_attribute_override_changes_component():
    """Overriding the building attributes re-drives auto-population to a different
    HAZUS component (wood-frame). The e1 demand is PGA-only, so the lifeline path
    (PGA-driven structural fragility) is the one this ground-motion demand feeds."""
    r = _call("pelicun_hazus_seismic_dl_run", structure_type="W1",
              height_class="Low-Rise", design_level="Moderate-Code",
              occupancy_class="RES1", lifeline_facility=True, realizations=60)
    assert r["status"] == "ok"
    assert r["auto_populated_component"]
    comp = r["auto_populated_component"][0]
    assert "W1" in comp


def test_seismic_dl_run_invalid_realizations_is_typed_error():
    r = _call("pelicun_hazus_seismic_dl_run", realizations=0)
    assert r["status"] == "error"
    assert r["error_code"] == "PELICUN_DL_CALCULATION_INVALID"


# ---------------------------------------------------------------------------
# Row 2: v5.1 vs v6.1 dataset comparison
# ---------------------------------------------------------------------------


def test_version_comparison_shared_type_has_zero_shift_and_reports_coverage():
    """For a building type present in both versions, the damage-state probabilities
    and mean repair cost are identical (the shared portfolio was not revised);
    v6.1's change is coverage -- it adds components over v5.1."""
    r = _call("pelicun_hazus_eq_version_comparison",
              building_type="STR.C1.L.PC", occupancy="STR.RES1",
              sample_size=4000, seed=42)
    assert r["status"] == "ok"
    assert len(r["ds_probs"]["v5.1"]) == len(r["ds_probs"]["v6.1"])
    assert r["max_ds_probability_shift"] == 0.0
    assert r["mean_repair_cost_shift"] == 0.0
    cov = r["coverage"]
    assert cov["v6_component_count"] > cov["v5_component_count"]
    assert cov["added_in_v6_count"] == 58


def test_version_comparison_v6_only_type_is_typed_error():
    """A v6.1-only design level (SC) has no v5.1 equivalent -> loud typed error."""
    r = _call("pelicun_hazus_eq_version_comparison",
              building_type="STR.W1.SC", occupancy="STR.RES1", sample_size=2000)
    assert r["status"] == "error"
    assert r["error_code"] == "PELICUN_VALIDATION_ERROR"
    assert "v5.1" in r["error_message"]


def test_version_comparison_invalid_sample_size_is_typed_error():
    r = _call("pelicun_hazus_eq_version_comparison", sample_size=0)
    assert r["status"] == "error"
    assert r["error_code"] == "PELICUN_VALIDATION_INVALID"
