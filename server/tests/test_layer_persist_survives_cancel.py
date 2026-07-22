"""Layer-publish DURABILITY under cancellation (live bug 2026-06-23).

Root cause: a long solver dispatch (SFINCS+SnapWave, ~9 min) persists its
computed layer accumulator to the ``grace2_cases`` DynamoDB record ONLY in the
``finally`` of ``_invoke_tool_via_emitter`` -- with a BARE ``await``. That
``finally`` runs on the cancellation path too (a same-stream re-prompt
supersede, the stop button, or any cancel reaching the detached turn), but a
bare ``await persist(...)`` in a ``finally`` is cancel-fragile: the pending
``CancelledError`` re-raises at the persist's first suspension point and SKIPS
the DynamoDB write -- so a fully-computed layer (its COGs already on S3) is lost
from the Case. Live evidence: run 01KVSTC80F wrote 100+ flood_depth_frame COGs
to S3 yet Case 01KVSTBCG3 persisted ``loaded_layer_summaries = []`` after a
transient WS drop during the solve.

Fix under test: ``server._run_to_completion_shielded`` wraps the layer persist
(and the cold-view snapshot + manifest) so a parent cancel cannot interrupt the
write; the write runs to completion, THEN the cancel re-raises (Invariant 8
preserved). These tests are LLM-free and run against the REAL file-backed
persistence substrate pointed at a pytest tmpdir.
"""

from __future__ import annotations

import asyncio

import pytest

import trid3nt_server.server as server
from trid3nt_server import tools as agent_tools
from trid3nt_server.pipeline_emitter import current_emitter
from trid3nt_server.persistence import make_file_persistence
from trid3nt_server.tools import RegisteredTool
from trid3nt_contracts.case import CaseCommandEnvelopePayload
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

SLOW_TOOL = "slow_layer_tool_cancel_durability"


def _make_layer(layer_id: str = "L-flood-cancel-001") -> LayerURI:
    return LayerURI(
        layer_id=layer_id,
        name="Flood depth peak",
        layer_type="raster",
        uri=f"https://qgis.example/wms?LAYERS={layer_id}",
        style_preset="continuous_flood_depth",
        role="primary",
    )


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture()
def file_persistence(tmp_path):
    p = make_file_persistence(base_dir=tmp_path)
    server.set_persistence(p)
    try:
        yield p
    finally:
        server.set_persistence(None)


@pytest.fixture(autouse=True)
def _clean_session_registry():
    server._SESSION_ACTIVE_CASE.clear()
    yield
    server._SESSION_ACTIVE_CASE.clear()


@pytest.fixture()
def slow_layer_tool():
    """A tool that ADDS a layer to the emitter (mirroring the solver workflow's
    out-of-band ``add_loaded_layer`` publish), then blocks -- modeling the long
    publish loop / wait that runs after the COGs are already on S3. A cancel
    that lands while it blocks must NOT lose the already-added layer.
    """
    started = asyncio.Event()

    async def _fn(_emitter=None):  # noqa: ANN001 — test stub
        # The real workflow emits frames OUT-OF-BAND via the bound emitter
        # (current_emitter()); emit_tool_call binds self as the current emitter
        # for the lifetime of the invoke, so add a layer that way.
        emitter = current_emitter()
        assert emitter is not None, "emit_tool_call must bind the current emitter"
        await emitter.add_loaded_layer(_make_layer())
        started.set()
        # Block as the long publish loop / wait_for_completion would.
        await asyncio.sleep(3600)
        return _make_layer()  # unreached

    _fn._started = started  # type: ignore[attr-defined]
    meta = AtomicToolMetadata(
        name=SLOW_TOOL, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[SLOW_TOOL] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield SLOW_TOOL
    finally:
        agent_tools.TOOL_REGISTRY.pop(SLOW_TOOL, None)


async def _create_case(ws, state, title="Coastal SFINCS Cancel") -> str:
    cmd = CaseCommandEnvelopePayload(command="create", args={"title": title})
    await server._handle_case_command(ws, state, cmd)
    case_id = state.active_case_id
    assert case_id, "create must bind the active case"
    return case_id


@pytest.mark.asyncio
async def test_layer_persists_when_dispatch_task_cancelled_mid_publish(
    file_persistence, slow_layer_tool
) -> None:
    """THE live regression: a layer added BEFORE a cancel must persist.

    Spawn the dispatch as a real task (as the server does), let it add the
    layer, then CANCEL the task (modeling a same-stream supersede / stop button
    / cancel reaching the detached turn). The Case record MUST carry the layer.
    """
    session_id = new_ulid()
    ws, state = FakeWS(), server.SessionState(session_id=session_id)
    case_id = await _create_case(ws, state)

    started = agent_tools.TOOL_REGISTRY[SLOW_TOOL].fn._started  # type: ignore[attr-defined]

    task = asyncio.ensure_future(
        server._invoke_tool_via_emitter(ws, state, SLOW_TOOL, {})
    )
    # Wait until the tool has added the layer and is blocking.
    await asyncio.wait_for(started.wait(), timeout=5.0)

    # Cancel the in-flight dispatch -- the cancel-fragile bug dropped the write.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The fully-computed layer MUST survive the cancellation.
    session_state = await file_persistence.get_session_state(case_id)
    assert len(session_state.loaded_layers) == 1, (
        "a layer added before a cancel must persist to the Case "
        "(layer-publish-survives-disconnect durability)"
    )
    assert session_state.loaded_layers[0]["layer_id"] == "L-flood-cancel-001"
    assert session_state.case.layer_summary == ["L-flood-cancel-001"]


@pytest.mark.asyncio
async def test_run_to_completion_shielded_runs_write_then_reraises_cancel() -> None:
    """Unit test for the durability primitive: the shielded coroutine COMPLETES
    even when the awaiting task is cancelled, and the cancellation still
    propagates afterward (Invariant 8)."""
    completed = asyncio.Event()

    async def _write() -> None:
        # A real suspension point (like the Dynamo round-trip) -- this is exactly
        # where a bare await would re-raise the pending cancel and skip the write.
        await asyncio.sleep(0.05)
        completed.set()

    async def _caller() -> None:
        await server._run_to_completion_shielded(_write())

    task = asyncio.ensure_future(_caller())
    await asyncio.sleep(0)  # let it enter the shield
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The write ran to completion despite the cancel.
    assert completed.is_set(), (
        "shielded write must complete even when the parent task is cancelled"
    )


@pytest.mark.asyncio
async def test_normal_completion_still_persists(
    file_persistence,
) -> None:
    """No-regression: a tool that returns a LayerURI normally still persists
    (the shield is transparent on the happy path)."""
    fast_tool = "fast_layer_tool_cancel_durability"

    async def _fn() -> LayerURI:
        return _make_layer("L-fast-001")

    meta = AtomicToolMetadata(
        name=fast_tool, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[fast_tool] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        session_id = new_ulid()
        ws, state = FakeWS(), server.SessionState(session_id=session_id)
        case_id = await _create_case(ws, state, title="Happy Path")
        result = await server._invoke_tool_via_emitter(ws, state, fast_tool, {})
        assert isinstance(result, LayerURI)
        session_state = await file_persistence.get_session_state(case_id)
        assert len(session_state.loaded_layers) == 1
        # F97: the dispatch mints a unique layer_id for the freshly-fetched layer.
        assert session_state.loaded_layers[0]["layer_id"] == result.layer_id
    finally:
        agent_tools.TOOL_REGISTRY.pop(fast_tool, None)
