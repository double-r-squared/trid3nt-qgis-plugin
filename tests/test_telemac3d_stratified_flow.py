"""Offline unit tests for the telemac3d_stratified_flow engine template (ADR 0241).

No solver / no network: registration shape + arg-guard rejection paths + mode
classification only. The physics-through-the-image proof lives in the Dockerfile
build-time smoke + the live E2E; this is the offline-suite guard that the tool is
registered as an engine template and rejects ill-posed args before any dispatch.
"""
from __future__ import annotations

import asyncio


def test_telemac3d_registered_as_engine_template():
    from trid3nt_server.data import TOOL_REGISTRY
    entry = TOOL_REGISTRY.get("telemac3d_stratified_flow")
    assert entry is not None, "telemac3d_stratified_flow must be registered"
    m = entry.metadata
    assert m.engine == "telemac" and m.tier == "template"
    assert m.cacheable is False and m.ttl_class == "live-no-cache"
    specs = {r.param for r in (m.resolution_specs or ())}
    assert "target_resolution_m" in specs


def test_telemac3d_solver_registered():
    from trid3nt_server.data.simulation.solver.solver import (
        LOCAL_SOLVER_SPEC_REGISTRY,
        SOLVER_WORKFLOW_REGISTRY,
    )
    assert "telemac3d_strat" in SOLVER_WORKFLOW_REGISTRY
    assert "telemac3d_strat" in LOCAL_SOLVER_SPEC_REGISTRY


def test_tool_rejects_neither_location_nor_bbox_for_lake_modes():
    from trid3nt_server.workflows.telemac.stratified_flow.stratified_flow import (
        telemac3d_stratified_flow,
    )
    out = asyncio.run(telemac3d_stratified_flow(flow_mode="stratification"))
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "TELEMAC3D_PARAMS_INCOMPLETE"


def test_tool_rejects_invalid_bbox():
    from trid3nt_server.workflows.telemac.stratified_flow.stratified_flow import (
        telemac3d_stratified_flow,
    )
    out = asyncio.run(telemac3d_stratified_flow(bbox=[1.0, 2.0]))  # too few numbers
    assert isinstance(out, dict) and out["status"] == "error"
    assert out["error_code"] == "TELEMAC3D_PARAMS_INVALID"


def test_mode_classification_from_prompt():
    from trid3nt_server.workflows.telemac.stratified_flow.stratified_flow import (
        _classify_mode,
    )
    assert _classify_mode("does this lake stratify and turn over", None) == "stratification"
    assert _classify_mode("wind-driven circulation and return flow", None) == "wind_circulation"
    assert _classify_mode("salt wedge intrusion in the estuary", None) == "salt_wedge"
    assert _classify_mode("anything", "wind_circulation") == "wind_circulation"  # explicit wins
