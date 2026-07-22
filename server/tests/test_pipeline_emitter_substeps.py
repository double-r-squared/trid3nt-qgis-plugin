"""Unit tests for the nested sub-step timeline (task-168).

The CONTRACT + EMITTER lane surfaces a composer's INTERNAL atomic-tool calls
(``fetch_*`` / deck build / ``run_solver`` / ``postprocess_*`` /
``publish_layer`` / ``compute_*``) as CHILD steps nested under the parent
workflow card via ``PipelineEmitter.substep`` (and the module-level no-op-safe
``substep`` / ``begin_substeps`` wrappers).

Coverage (maps to the kickoff's acceptance #3):

1. ``test_parent_with_three_substeps_emits_one_parent_three_children`` — a
   parent step that runs 3 substeps emits exactly 1 parent + 3 child steps, all
   with UNIQUE ULID ids; each child carries ``parent_step_id`` == the parent's
   id and renders nested (never as a top-level card).
2. ``test_parent_breadcrumb_label_index_total_transitions`` — the PARENT carries
   ``substep_label`` (raw child name), 1-based ``substep_index``, and
   ``substep_total`` (from ``begin_substeps``) WHILE a child runs, then those
   fields CLEAR on the parent's terminal transition.
3. ``test_failing_substep_marks_child_failed_not_parent_green`` — a substep that
   raises marks the CHILD failed (red, honesty floor) and re-raises; the parent
   still reaches ``complete`` (green) and is never turned red by the child.
4. ``test_substep_is_noop_when_no_emitter_bound`` — the module-level
   ``substep(None, ...)`` wrapper yields ``None`` and mints nothing (the
   verify/CI direct-call path is unchanged); ``begin_substeps(None, ...)`` is a
   no-op too.
5. ``test_substep_noop_when_emitter_has_no_parent`` — ``emitter.substep`` yields
   ``None`` + mints nothing when no top-level ``emit_tool_call`` parent is bound.

The sink is a sync capture closure wrapped in an ``async def`` so the emitter
can ``await`` it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from grace2_contracts import new_ulid

from grace2_agent.pipeline_emitter import (
    PipelineEmitter,
    begin_substeps,
    substep,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


class _CapturingSink:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def __call__(self, text: str) -> None:
        self.frames.append(json.loads(text))


@pytest.fixture()
def sink() -> _CapturingSink:
    return _CapturingSink()


@pytest.fixture()
def emitter(sink: _CapturingSink) -> PipelineEmitter:
    return PipelineEmitter(session_id=new_ulid(), sink=sink)


def _pipeline_frames(sink: _CapturingSink) -> list[dict[str, Any]]:
    return [f for f in sink.frames if f["type"] == "pipeline-state"]


def _last_steps(sink: _CapturingSink) -> list[dict[str, Any]]:
    return _pipeline_frames(sink)[-1]["payload"]["steps"]


# --------------------------------------------------------------------------- #
# 1. One parent + three children, unique ids, parent_step_id linkage
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_parent_with_three_substeps_emits_one_parent_three_children(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """A parent workflow running 3 substeps emits 1 parent + 3 child steps, all
    with unique ULID ids; every child carries parent_step_id == the parent id."""

    async def workflow() -> str:
        # Inside emit_tool_call -> current_emitter() is bound; declare the plan.
        begin_substeps(emitter, 3)
        for raw in ("fetch_topobathy", "build_sfincs_deck", "publish_layer"):
            async with substep(emitter, raw) as child_id:
                assert child_id is not None
        return "ok"

    await emitter.emit_tool_call(
        name="Model coastal flood",
        tool_name="model_flood_scenario",
        invoke=workflow,
    )

    steps = _last_steps(sink)
    # 1 parent + 3 children.
    assert len(steps) == 4, steps
    parent = steps[0]
    children = steps[1:]

    # Every step id is a UNIQUE ULID.
    ids = [s["step_id"] for s in steps]
    assert len(set(ids)) == 4
    assert all(len(i) == 26 for i in ids)  # ULID length

    # The parent is top-level (no parent_step_id); each child links to it.
    assert parent["parent_step_id"] is None
    assert [c["tool_name"] for c in children] == [
        "fetch_topobathy",
        "build_sfincs_deck",
        "publish_layer",
    ]
    for c in children:
        assert c["parent_step_id"] == parent["step_id"]
        assert c["state"] == "complete"

    # Parent completed green; its breadcrumb cleared on terminal.
    assert parent["state"] == "complete"
    assert parent["substep_label"] is None
    assert parent["substep_index"] is None
    assert parent["substep_total"] is None


# --------------------------------------------------------------------------- #
# 2. Parent breadcrumb transitions (label / index / total) + terminal clear
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_parent_breadcrumb_label_index_total_transitions(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """WHILE each child runs the parent carries substep_label (raw name),
    1-based substep_index, and substep_total; the trio clears on terminal."""

    breadcrumbs: list[tuple[str | None, int | None, int | None]] = []

    async def workflow() -> str:
        begin_substeps(emitter, 2)
        for raw in ("fetch_dem", "run_solver"):
            async with substep(emitter, raw):
                # Snapshot the parent's breadcrumb on the LIVE frame (the
                # mark_running emit that just went out for this child).
                running = _last_steps(sink)
                parent = running[0]
                breadcrumbs.append(
                    (
                        parent["substep_label"],
                        parent["substep_index"],
                        parent["substep_total"],
                    )
                )
        return "ok"

    await emitter.emit_tool_call(
        name="Flood scenario", tool_name="model_flood_scenario", invoke=workflow
    )

    # First child: label fetch_dem, index 1, total 2. Second: run_solver, 2/2.
    assert breadcrumbs == [
        ("fetch_dem", 1, 2),
        ("run_solver", 2, 2),
    ]

    # After the parent's terminal transition the breadcrumb is cleared.
    parent_final = _last_steps(sink)[0]
    assert parent_final["state"] == "complete"
    assert parent_final["substep_label"] is None
    assert parent_final["substep_index"] is None
    assert parent_final["substep_total"] is None


@pytest.mark.asyncio
async def test_begin_substeps_none_total_degrades_to_label_only(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """begin_substeps(None) (unknown plan) leaves substep_total None so the web
    renders just the humanized label + index with no '/N'."""

    seen: list[tuple[str | None, int | None, int | None]] = []

    async def workflow() -> str:
        begin_substeps(emitter, None)  # plan unknown
        async with substep(emitter, "fetch_topobathy"):
            parent = _last_steps(sink)[0]
            seen.append(
                (
                    parent["substep_label"],
                    parent["substep_index"],
                    parent["substep_total"],
                )
            )
        return "ok"

    await emitter.emit_tool_call(
        name="Coastal", tool_name="model_flood_scenario", invoke=workflow
    )
    assert seen == [("fetch_topobathy", 1, None)]


# --------------------------------------------------------------------------- #
# 3. Failing substep -> child red, parent NOT turned green-by-mistake / red
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failing_substep_marks_child_failed_not_parent_green(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """A substep that raises marks the CHILD failed (honesty floor) and the
    parent still completes; a sibling stays green and the parent is never turned
    red by the child."""

    async def workflow() -> str:
        # Sibling 1 succeeds.
        async with substep(emitter, "fetch_dem"):
            pass
        # Sibling 2 fails -> caught here so the parent can still complete.
        try:
            async with substep(emitter, "run_solver"):
                raise RuntimeError("solver blew up")
        except RuntimeError:
            pass
        return "ok"

    await emitter.emit_tool_call(
        name="Flood scenario", tool_name="model_flood_scenario", invoke=workflow
    )

    steps = _last_steps(sink)
    parent, child_ok, child_fail = steps[0], steps[1], steps[2]

    assert child_ok["tool_name"] == "fetch_dem"
    assert child_ok["state"] == "complete"  # sibling stays green

    assert child_fail["tool_name"] == "run_solver"
    assert child_fail["state"] == "failed"  # red, honesty floor
    assert child_fail["parent_step_id"] == parent["step_id"]

    # The PARENT is green (its own clean return) — a failed CHILD never flips it.
    assert parent["state"] == "complete"
    assert parent["parent_step_id"] is None

    # The failed child carries a classified error_code on the persisted summary.
    snap = emitter.current_snapshot()
    assert snap is not None
    fail_summary = next(s for s in snap.steps if s.tool_name == "run_solver")
    assert fail_summary.state == "failed"
    assert fail_summary.error_code is not None


@pytest.mark.asyncio
async def test_failing_substep_reraises_to_caller(
    emitter: PipelineEmitter,
) -> None:
    """The substep CM re-raises the original exception after marking the child
    failed, so a composer that does NOT catch it sees normal propagation."""

    async def workflow() -> str:
        async with substep(emitter, "publish_layer"):
            raise ValueError("publish failed")

    with pytest.raises(ValueError, match="publish failed"):
        await emitter.emit_tool_call(
            name="Publish", tool_name="publish_layer_wf", invoke=workflow
        )


# --------------------------------------------------------------------------- #
# 4. No-op when no emitter bound (verify/CI direct-call path)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_substep_is_noop_when_no_emitter_bound() -> None:
    """The module-level substep(None, ...) wrapper yields None and mints
    nothing; begin_substeps(None, ...) is a no-op. The verify/CI direct-call
    paths keep working unchanged."""
    ran = False
    async with substep(None, "fetch_dem") as child_id:
        ran = True
        assert child_id is None
    assert ran

    # begin_substeps(None) must not raise.
    begin_substeps(None, 5)


@pytest.mark.asyncio
async def test_substep_noop_when_emitter_has_no_parent(
    emitter: PipelineEmitter, sink: _CapturingSink
) -> None:
    """emitter.substep yields None + mints nothing when called OUTSIDE an
    emit_tool_call body (no top-level parent step bound)."""
    async with emitter.substep("fetch_dem") as child_id:
        assert child_id is None
    # No step was minted; no pipeline-state frame emitted.
    assert _pipeline_frames(sink) == []
    assert emitter.current_snapshot() is None
