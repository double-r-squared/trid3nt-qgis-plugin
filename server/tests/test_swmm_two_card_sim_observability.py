"""Two-card sim observability (task-149) for the SWMM off-box (Batch) lane.

The urban-flood composer's OUT-OF-PROCESS lane (``GRACE2_SWMM_LOCAL=0`` /
``is_local_mode() -> False``) dispatches the staged deck through the generic
solver seam (``run_solver`` -> ``wait_for_completion`` -> Batch output). task-149
makes that lane mint TWO cards: a "Dispatch" tool card recording the submit (lands
complete) + a "Sim" compute card bound to the Batch jobId that the wait-loop
poller drives, with the terminal routed to the SIM card.

These tests drive the composer with a REAL ``PipelineEmitter`` (capturing sink)
so the two cards are actually emitted on the wire, and stub the heavy off-box
chain (deck build / manifest stage / solve / batch-output download / postprocess
/ publish) at the composer's module namespace — pyswmm-free, AWS-free.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from grace2_contracts import new_ulid
from grace2_contracts.swmm_contracts import SWMMDepthLayerURI, SWMMRunArgs

import grace2_agent.tools.solver as solver_mod
from grace2_agent import pipeline_emitter as pe
from grace2_agent.pipeline_emitter import PipelineEmitter
from grace2_agent.workflows import model_urban_flood_swmm as M


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _CapturingSink:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def __call__(self, text: str) -> None:
        self.frames.append(json.loads(text))


class _FakeStaging:
    def __init__(self) -> None:
        self.run_id = "RID"
        self.inp_path = "/tmp/does-not-exist/mesh.inp"
        self.build = type(
            "B", (), {"n_active_cells": 0, "resolution_m": 10.0}
        )()


class _FakeHandle:
    """ExecutionHandle-shaped fake carrying the Batch jobId + solver."""

    def __init__(self, job_id: str) -> None:
        self.workflows_execution_id = job_id
        self.workflow_name = "aws-batch"
        self.solver = M.SWMM_SOLVER_NAME
        self.run_id = "BATCH-RID"


def _depth_layer(layer_id: str, name: str, uri: str, role: str) -> SWMMDepthLayerURI:
    return SWMMDepthLayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="raster",
        uri=uri,
        style_preset="continuous_flood_depth",
        role=role,
        units="meters",
        bbox=[-88.0, 36.0, -87.99, 36.01],
        max_depth_m=1.25,
        flooded_area_km2=0.04,
        n_buildings_affected=2,
        barriers={"type": "FeatureCollection", "features": []},
    )


def _run_result(status: str, *, error_code: str | None = None) -> Any:
    return type(
        "R",
        (),
        {
            "status": status,
            "run_id": "BATCH-RID",
            "output_uri": "s3://runs/BATCH-RID/",
            "error_code": error_code,
            "error_message": None if status == "complete" else "boom",
            "cancellation_reason": None,
        },
    )()


def _install_offbox_chain(monkeypatch, *, job_id: str, run_result: Any):
    """Stub the off-box Batch lane so the composer runs without AWS/pyswmm."""
    staging = _FakeStaging()
    monkeypatch.setattr(M, "build_and_stage_swmm_deck", lambda *a, **k: staging)
    # Off-box lane: is_local_mode False -> run_solver / wait_for_completion path.
    monkeypatch.setattr(M, "is_local_mode", lambda: False)
    monkeypatch.setattr(M, "stage_swmm_manifest", lambda stg: "s3://runs/RID/manifest.json")

    handle = _FakeHandle(job_id)
    # run_solver + wait_for_completion are imported inside the composer from
    # ..tools.solver, so patch them on the solver module namespace.
    monkeypatch.setattr(solver_mod, "run_solver", lambda **k: handle)

    async def _fake_wait(h, *a, **k):  # noqa: ANN001
        return run_result

    monkeypatch.setattr(solver_mod, "wait_for_completion", _fake_wait)

    # Batch output download -> a run-shim with the continuity scalar + a tmp dir.
    run_shim = type("Run", (), {"continuity_error_pct": 0.5, "out_path": "/tmp/x.out"})()
    monkeypatch.setattr(
        M, "_download_batch_swmm_outputs", lambda rr, rid: (run_shim, "/tmp/batchout")
    )
    monkeypatch.setattr(M, "_cleanup_deck_dir", lambda d: None)

    peak = _depth_layer(
        "swmm-depth-peak-RID", "Peak flood depth",
        "s3://runs/RID/swmm_depth_peak.tif", "primary",
    )
    monkeypatch.setattr(
        M, "postprocess_swmm", lambda *a, **k: ([peak], {"max_depth_m": 1.25})
    )
    # Publish is sync-offloaded; return the peak unchanged (renderability is not
    # what this test asserts — only the two-card emission is).
    monkeypatch.setattr(M, "_publish_peak_layer", lambda p, rid: p)

    async def _emit_frames(em, frames, rid):  # noqa: ANN001
        return 0

    monkeypatch.setattr(M, "_emit_frame_layers", _emit_frames)
    # Keep the binding seam clean before/after.
    solver_mod.set_emitter_binding(None)
    return staging, handle


def _pipeline_frames(sink: _CapturingSink) -> list[dict[str, Any]]:
    return [f for f in sink.frames if f["type"] == "pipeline-state"]


def _final_steps(sink: _CapturingSink) -> list[dict[str, Any]]:
    return _pipeline_frames(sink)[-1]["payload"]["steps"]


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_swmm_offbox_emits_dispatch_and_compute_cards(monkeypatch):
    """The off-box lane mints a complete Dispatch tool card + a Sim compute card
    bound to the worker jobId, and routes the terminal (green) to the SIM card."""
    _install_offbox_chain(monkeypatch, job_id="batch-job-555", run_result=_run_result("complete"))

    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)
    token = pe._CURRENT_EMITTER.set(emitter)
    try:
        run_args = SWMMRunArgs(bbox=(-88.0, 36.0, -87.99, 36.01))
        peak = asyncio.run(
            M.model_urban_flood_swmm(
                run_args,
                dem_path="/tmp/synthetic.tif",  # skip the DEM fetch
                building_footprints=None,
                run_id="RID",
            )
        )
    finally:
        pe._CURRENT_EMITTER.reset(token)
        solver_mod.set_emitter_binding(None)

    assert isinstance(peak, SWMMDepthLayerURI)

    steps = _final_steps(sink)
    compute = [s for s in steps if s["role"] == "compute"]
    dispatch = [s for s in steps if s["role"] == "tool"]

    # Exactly one compute (Sim) card bound to the worker jobId, landed complete.
    assert len(compute) == 1
    assert compute[0]["batch_job_id"] == "batch-job-555"
    assert compute[0]["state"] == "complete"

    # At least one tool (Dispatch) card recording the submit, landed complete.
    assert any(s["state"] == "complete" for s in dispatch)
    # The dispatch card is a tool-kind card (no Batch binding).
    a_dispatch = next(s for s in dispatch if s["tool_name"].endswith(":dispatch"))
    assert a_dispatch["batch_job_id"] is None

    # The emitter binding was cleared after the wait (no leak).
    assert solver_mod._EMITTER_BINDING is None


def test_swmm_offbox_routes_terminal_failed_to_sim_card(monkeypatch):
    """A non-complete RunResult routes the SIM compute card to failed (red),
    even though the composer subsequently raises its typed workflow error."""
    _install_offbox_chain(
        monkeypatch,
        job_id="batch-job-999",
        run_result=_run_result("failed", error_code="SOLVER_TIMEOUT"),
    )

    sink = _CapturingSink()
    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)
    token = pe._CURRENT_EMITTER.set(emitter)
    try:
        run_args = SWMMRunArgs(bbox=(-88.0, 36.0, -87.99, 36.01))
        with pytest.raises(Exception):  # noqa: B017 — SWMMWorkflowError on non-complete
            asyncio.run(
                M.model_urban_flood_swmm(
                    run_args,
                    dem_path="/tmp/synthetic.tif",
                    building_footprints=None,
                    run_id="RID",
                )
            )
    finally:
        pe._CURRENT_EMITTER.reset(token)
        solver_mod.set_emitter_binding(None)

    steps = _final_steps(sink)
    compute = [s for s in steps if s["role"] == "compute"]
    assert len(compute) == 1
    assert compute[0]["batch_job_id"] == "batch-job-999"
    assert compute[0]["state"] == "failed"
