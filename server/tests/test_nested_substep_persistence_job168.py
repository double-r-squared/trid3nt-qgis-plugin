"""task-168 -- READ-ONLY persistence of nested workflow sub-step cards.

The live nested sub-step cards (commit 256a587) surface a composer's internal
atomic-tool calls (``fetch_*`` / deck build / ``run_solver`` / ``postprocess_*`` /
``publish_layer``) as CHILD rows under the top-level workflow card, driven by
wire-only ``pipeline-state`` envelopes. Those were LOST on Case reopen and on the
box-off cold view. This suite proves the remaining work: the children now PERSIST
and replay READ-ONLY exactly like every other Case datum.

Drives the REAL server seams (no Gemini, no Playwright) against file-backed
persistence:

- a composer tool that opens ``substep`` children (one OK, one FAILED, with a
  failed child error_code) persists a ``ToolCardRecord`` whose ordered
  ``children`` survive a ``get_session_state`` round-trip (warm reopen);
- the parent's own tool-io (raw_args / function_response) is unchanged and the
  children ride alongside it;
- the BOX-OFF COLD VIEW: ``build_case_view_snapshot`` (the exact dict the
  signer-Lambda hands a cold browser from S3) carries the SAME children, additive
  JSON, no re-execution;
- a FAILED parent still nests its children (a successful fetch then a failed
  solve);
- a plain tool with NO substeps persists ``children == None`` (every pre-task-168
  path), and a legacy tool-card row literally missing the field still loads
  (backward compat).
"""

from __future__ import annotations

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.persistence import make_file_persistence
from trid3nt_server.pipeline_emitter import current_emitter, substep, begin_substeps
from trid3nt_server.tools import RegisteredTool
from trid3nt_contracts.case import (
    CaseChatMessage,
    CaseCommandEnvelopePayload,
    PersistedSubStepRecord,
    ToolCardRecord,
)
from trid3nt_contracts.common import new_ulid, now_utc
from trid3nt_contracts.tool_registry import AtomicToolMetadata


class FakeWS:
    """Minimal ServerConnection stand-in: records every envelope sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture()
def file_persistence(tmp_path):
    """Bind REAL file-backed persistence (tmpdir) as the server singleton."""
    p = make_file_persistence(base_dir=tmp_path)
    server.set_persistence(p)
    try:
        yield p
    finally:
        server.set_persistence(None)


@pytest.fixture(autouse=True)
def _clean_session_registry():
    """Keep the session-scoped Case registry hermetic per test."""
    server._SESSION_ACTIVE_CASE.clear()
    yield
    server._SESSION_ACTIVE_CASE.clear()


def _register(name, fn):
    meta = AtomicToolMetadata(name=name, ttl_class="live-no-cache", cacheable=False)
    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=fn, module=__name__
    )


async def _create_case(ws, state, title="Nested Substep Case") -> str:
    cmd = CaseCommandEnvelopePayload(command="create", args={"title": title})
    await server._handle_case_command(ws, state, cmd)
    case_id = state.active_case_id
    assert case_id, "create must bind the active case"
    return case_id


# --------------------------------------------------------------------------- #
# Composer tools: a registry fn whose body opens substep children.
# ``_invoke_tool_via_emitter`` wraps the fn in ``emit_tool_call`` which binds
# ``current_emitter()`` for the lifetime of the invoke, so the body's
# ``substep(current_emitter(), ...)`` mints CHILD steps under the parent card --
# exactly the live composer shape.
# --------------------------------------------------------------------------- #


COMPOSER_OK = "job168_composer_ok"
COMPOSER_FAILS = "job168_composer_fails"


@pytest.fixture()
def composer_ok_tool():
    async def _fn(*, bbox=None) -> dict:
        em = current_emitter()
        begin_substeps(em, 2)
        async with substep(em, "fetch_topobathy"):
            pass
        # A failed CHILD must NOT fail the parent (honesty floor): swallow here
        # so the parent returns success and the failed child still persists RED.
        try:
            async with substep(em, "run_solver"):
                raise ValueError("solver bbox blew up")
        except ValueError:
            pass
        return {"status": "ok", "rows": 5}

    _register(COMPOSER_OK, _fn)
    try:
        yield COMPOSER_OK
    finally:
        agent_tools.TOOL_REGISTRY.pop(COMPOSER_OK, None)


@pytest.fixture()
def composer_fails_tool():
    async def _fn() -> dict:
        em = current_emitter()
        begin_substeps(em, 2)
        async with substep(em, "fetch_topobathy"):
            pass  # OK child
        # This child raises AND is allowed to propagate -> the PARENT fails too.
        async with substep(em, "run_solver"):
            raise RuntimeError("solver process died")

    _register(COMPOSER_FAILS, _fn)
    try:
        yield COMPOSER_FAILS
    finally:
        agent_tools.TOOL_REGISTRY.pop(COMPOSER_FAILS, None)


# --------------------------------------------------------------------------- #
# 1. Warm reopen: a composer's children round-trip through get_session_state.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_complete_parent_persists_ordered_children(
    file_persistence, composer_ok_tool
) -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)

    args = {"bbox": [-82.0, 26.0, -81.0, 27.0]}
    result = await server._invoke_tool_via_emitter(ws, state, COMPOSER_OK, args)
    assert result == {"status": "ok", "rows": 5}

    session_state = await file_persistence.get_session_state(case_id)
    tool_rows = [m for m in session_state.chat_history if m.role == "tool"]
    assert len(tool_rows) == 1
    card = tool_rows[0].tool_card
    assert card is not None
    assert card.state == "complete"
    assert card.tool_name == COMPOSER_OK

    # The ordered children survived persistence + reload.
    assert card.children is not None
    assert [c.tool_name for c in card.children] == ["fetch_topobathy", "run_solver"]
    fetch_child, solver_child = card.children
    # OK child: complete, no error, real duration.
    assert fetch_child.state == "complete"
    assert fetch_child.error_code is None
    assert fetch_child.duration_ms is not None and fetch_child.duration_ms >= 0
    # FAILED child: red, with a classified error_code + message (honesty floor).
    assert solver_child.state == "failed"
    assert solver_child.error_code  # SCREAMING_SNAKE_CASE code present
    assert solver_child.error_message
    # Children are proper typed records on the rehydrated envelope.
    assert all(isinstance(c, PersistedSubStepRecord) for c in card.children)

    # The parent's OWN tool-io is unaffected (C1 path); children ride alongside.
    assert card.raw_args is not None and "bbox" in card.raw_args
    assert card.function_response is not None

    # The content JSON twin carries the children too (belt-and-suspenders).
    import json

    twin = json.loads(tool_rows[0].content)
    assert [c["tool_name"] for c in twin["children"]] == [
        "fetch_topobathy",
        "run_solver",
    ]


# --------------------------------------------------------------------------- #
# 2. BOX-OFF COLD VIEW: the case-view snapshot carries the children unchanged.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cold_view_snapshot_carries_children(
    file_persistence, composer_ok_tool
) -> None:
    """The serverless box-off path signs a URL for the case-view snapshot that
    ``build_case_view_snapshot`` materializes to S3. That snapshot embeds the
    SAME ``get_session_state`` chat history, so the persisted children ride it
    READ-ONLY (additive JSON; the signer Lambda is a pure pass-through)."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)

    await server._invoke_tool_via_emitter(
        ws, state, COMPOSER_OK, {"bbox": [-82.0, 26.0, -81.0, 27.0]}
    )

    snapshot = await file_persistence.build_case_view_snapshot(case_id)
    ss = snapshot["session_state"]
    chat = ss["chat_history"]
    tool_rows = [m for m in chat if m["role"] == "tool"]
    assert len(tool_rows) == 1
    card = tool_rows[0]["tool_card"]
    assert card["state"] == "complete"
    children = card["children"]
    assert [c["tool_name"] for c in children] == ["fetch_topobathy", "run_solver"]
    assert children[1]["state"] == "failed"
    assert children[1]["error_code"]

    # The cold snapshot is byte-equivalent to the warm reopen for the children
    # (no re-execution, no shape drift): re-validate it as a CaseChatMessage.
    rebuilt = CaseChatMessage.model_validate(tool_rows[0])
    assert rebuilt.tool_card is not None
    assert [c.tool_name for c in rebuilt.tool_card.children] == [
        "fetch_topobathy",
        "run_solver",
    ]


# --------------------------------------------------------------------------- #
# 3. A FAILED parent still nests its children.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failed_parent_still_persists_children(
    file_persistence, composer_fails_tool
) -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)

    with pytest.raises(RuntimeError, match="solver process died"):
        await server._invoke_tool_via_emitter(ws, state, COMPOSER_FAILS, {})

    session_state = await file_persistence.get_session_state(case_id)
    tool_rows = [m for m in session_state.chat_history if m.role == "tool"]
    assert len(tool_rows) == 1
    card = tool_rows[0].tool_card
    assert card is not None
    assert card.state == "failed"
    # The failed parent STILL nests its sub-step timeline: an OK fetch then a
    # failed solve.
    assert card.children is not None
    assert [c.tool_name for c in card.children] == ["fetch_topobathy", "run_solver"]
    assert card.children[0].state == "complete"
    assert card.children[1].state == "failed"
    assert card.children[1].error_code


# --------------------------------------------------------------------------- #
# 4. Backward compat: plain tool -> None children; legacy row (no field) loads.
# --------------------------------------------------------------------------- #


PLAIN_TOOL = "job168_plain_tool"


@pytest.fixture()
def plain_tool():
    async def _fn() -> dict:
        return {"status": "ok"}

    _register(PLAIN_TOOL, _fn)
    try:
        yield PLAIN_TOOL
    finally:
        agent_tools.TOOL_REGISTRY.pop(PLAIN_TOOL, None)


@pytest.mark.asyncio
async def test_plain_tool_persists_no_children(file_persistence, plain_tool) -> None:
    """A non-composer dispatch (no substeps) persists ``children == None`` --
    byte-identical to every pre-task-168 row, and replays as a plain card."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)

    await server._invoke_tool_via_emitter(ws, state, PLAIN_TOOL, {})

    session_state = await file_persistence.get_session_state(case_id)
    tool_rows = [m for m in session_state.chat_history if m.role == "tool"]
    assert len(tool_rows) == 1
    assert tool_rows[0].tool_card is not None
    assert tool_rows[0].tool_card.children is None


@pytest.mark.asyncio
async def test_legacy_tool_card_row_without_children_loads(
    file_persistence,
) -> None:
    """A persisted tool-card document literally MISSING the ``children`` field
    (a pre-task-168 row) still validates + replays unchanged (additive contract).
    Written through the raw MCP insert so no current code stamps the new field."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)

    legacy_doc = {
        "_id": new_ulid(),
        "schema_version": "v1",
        "message_id": new_ulid(),
        "case_id": case_id,
        "role": "tool",
        "content": "{}",
        # A tool_card with NO children key at all (pre-task-168 shape).
        "tool_card": {
            "schema_version": "v1",
            "tool_name": "fetch_3dep_dem",
            "state": "complete",
            "duration_ms": 12,
            "label": "fetch_3dep_dem",
        },
        "layer_emissions": [],
        "map_command_emissions": [],
        "created_at": now_utc().isoformat().replace("+00:00", "Z"),
    }
    await file_persistence._mcp.call_tool(
        "insert-one",
        {
            "database": file_persistence._db,
            "collection": "case_chat_messages",
            "document": legacy_doc,
        },
    )

    session_state = await file_persistence.get_session_state(case_id)
    tool_rows = [m for m in session_state.chat_history if m.role == "tool"]
    assert len(tool_rows) == 1
    card = tool_rows[0].tool_card
    assert card is not None
    assert card.tool_name == "fetch_3dep_dem"
    assert card.state == "complete"
    # Absent field -> None default; the chevron stays absent (no fabrication).
    assert card.children is None
