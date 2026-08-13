"""Offline unit tests for the tomawac_wave_field engine template (ADR 0236).

No solver / no network: registration shape + arg-guard rejection paths only.
The physics-through-the-image proof lives in the Dockerfile build-time smoke +
the live E2E; this is the offline-suite guard that the tool is registered as an
engine template and rejects ill-posed args before any dispatch.
"""
from __future__ import annotations

import asyncio


def test_tomawac_wave_field_registered_as_engine_template():
    from trid3nt_server.agent.tools import TOOL_REGISTRY
    entry = TOOL_REGISTRY.get("tomawac_wave_field")
    assert entry is not None, "tomawac_wave_field must be registered"
    m = entry.metadata
    assert m.engine == "telemac" and m.tier == "template"
    assert m.cacheable is False and m.ttl_class == "live-no-cache"
    specs = {r.param for r in (m.resolution_specs or ())}
    assert "target_resolution_m" in specs


def test_tomawac_solver_registered():
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        LOCAL_SOLVER_SPEC_REGISTRY,
        SOLVER_WORKFLOW_REGISTRY,
    )
    assert "tomawac_wave" in SOLVER_WORKFLOW_REGISTRY
    assert "tomawac_wave" in LOCAL_SOLVER_SPEC_REGISTRY


def test_tool_rejects_neither_location_nor_bbox():
    from trid3nt_server.agent.workflows.telemac.wave_field.wave_field import tomawac_wave_field
    out = asyncio.run(tomawac_wave_field())
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "TOMAWAC_PARAMS_INCOMPLETE"


def test_tool_rejects_invalid_bbox():
    from trid3nt_server.agent.workflows.telemac.wave_field.wave_field import tomawac_wave_field
    out = asyncio.run(tomawac_wave_field(bbox=[1.0, 2.0]))  # too few numbers
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "TOMAWAC_PARAMS_INVALID"


def test_mode_classification_from_prompt():
    from trid3nt_server.agent.workflows.telemac.wave_field.wave_field import _classify_mode
    assert _classify_mode("swell shoaling at the beach", None) == "shoaling"
    assert _classify_mode("opposing current at the inlet", None) == "wave_current"
    assert _classify_mode("bottom friction on the shelf", None) == "bottom_friction"
    assert _classify_mode("how big do the waves get", None) == "fetch_growth"
    assert _classify_mode("anything", "wave_current") == "wave_current"  # explicit wins
