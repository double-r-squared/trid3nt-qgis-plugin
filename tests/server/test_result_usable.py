"""What the per-tool-call line records as ``result_usable``, and the live
solve-progress tick.

A status=ok result with no layer reads False; the heartbeat's payload validates
against the wire contract."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.adapters.adapter import classify_result_usable, summarize_tool_result
from trid3nt_server.workflows.solver import solve_progress
from trid3nt_contracts.ws import SolveProgressPayload


def test_classify_result_usable_renderable_layer_uri():
    class _LayerURI:
        layer_id = "flood-1"
        uri = "s3://bucket/flood.tif"

    res = _LayerURI()
    summary = summarize_tool_result("publish_layer", {"layer_uri": "s3://b/x.tif"})
    assert classify_result_usable("publish_layer", res, summary) is True


def test_classify_result_usable_modeled_empty_layers_is_false():
    """The HEADLINE honesty-floor case: a modeled envelope with status=ok but an
    EMPTY layers list is success=True yet result_usable=False."""
    # A solve-completed-but-render-dropped modeled envelope (NOT failure-tagged):
    # carries metrics but no layers. summarize_tool_result stamps it
    # status=error + NO_RENDERABLE_LAYER (honesty floor).
    result = {
        "envelope_type": "modeled",
        "workflow_name": "model_flood_scenario",
        "layers": [],
        "flood": {"metrics": {"flooded_area_km2": 12.0, "max_depth_m": 2.3}},
    }
    summary = summarize_tool_result("sfincs_flood", result)
    assert summary["status"] == "error"
    assert summary["error_code"] == "NO_RENDERABLE_LAYER"
    assert classify_result_usable("sfincs_flood", result, summary) is False


def test_classify_result_usable_failure_tagged_modeled_is_false():
    result = {
        "envelope_type": "modeled",
        "workflow_name": "model_flood_scenario:FAILED:SOLVER_TIMEOUT",
        "layers": [],
        "flood": {"metrics": {"solver_version": "failed:SOLVER_TIMEOUT"}},
    }
    summary = summarize_tool_result("sfincs_flood", result)
    assert classify_result_usable("sfincs_flood", result, summary) is False


def test_classify_result_usable_modeled_with_layers_is_true():
    result = {
        "envelope_type": "modeled",
        "workflow_name": "model_flood_scenario",
        "layers": [{"layer_id": "flood-1", "uri": "s3://b/flood.tif"}],
    }
    summary = summarize_tool_result("sfincs_flood", result)
    assert classify_result_usable("sfincs_flood", result, summary) is True


def test_classify_result_usable_data_payload_is_true():
    """A non-layer data tool returning a populated dict is a usable result."""
    result = {"count": 42, "mean_depth_m": 1.7}
    summary = summarize_tool_result("probe_point", result)
    assert classify_result_usable("probe_point", result, summary) is True


def test_classify_result_usable_none_result_is_none():
    """A meta/no-result path (None) has no usability notion -> None."""
    summary = summarize_tool_result("some_meta_tool", None)
    assert classify_result_usable("some_meta_tool", None, summary) is None


def test_classify_result_usable_empty_layer_key_is_false():
    """A layer-shaped dict whose only layer key is empty -> False (even with no
    envelope_type)."""
    result = {"layers": []}
    summary = summarize_tool_result("fetch_something", result)
    assert classify_result_usable("fetch_something", result, summary) is False


class _Emitter:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def emit_solve_progress(self, payload: dict) -> None:
        self.sent.append(payload)


async def _one_tick(**kw) -> dict:
    emitter = _Emitter()
    task = asyncio.create_task(solve_progress.drive_live_solve_progress(
        emitter=emitter, **kw))
    async def _first() -> None:
        while not emitter.sent:
            await asyncio.sleep(0)

    try:
        await asyncio.wait_for(_first(), timeout=2.0)
    finally:
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    return emitter.sent[0]


@pytest.mark.asyncio
async def test_the_heartbeat_tick_is_the_wire_payload():
    p = await _one_tick(run_id="r1", solver="sfincs", grid_resolution_m=30.0,
                        active_cell_count=100_000, vcpus=8, eta_seconds=300.0)
    assert set(p) == {"run_id", "solver", "grid_resolution_m",
                      "active_cell_count", "vcpus", "elapsed_seconds",
                      "eta_seconds"}
    payload = SolveProgressPayload(**p)
    assert payload.MESSAGE_TYPE == "solve-progress"
    assert payload.run_id == "r1"
    assert payload.eta_seconds == 300.0


@pytest.mark.asyncio
async def test_the_heartbeat_tick_states_none_for_what_is_unknown():
    p = await _one_tick(run_id="r2", solver="sfincs", grid_resolution_m=None,
                        active_cell_count=None, vcpus=None, eta_seconds=None)
    payload = SolveProgressPayload(**p)
    assert payload.eta_seconds is None
    assert payload.grid_resolution_m is None
