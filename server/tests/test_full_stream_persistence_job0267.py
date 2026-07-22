"""job-0267 — FULL-STREAM persistence: narration + tool cards replay on reopen.

User-verified bug: reopening a Case replayed ONLY the user's own messages.
Two root causes:

1. ``_dispatch_gemini_and_persist`` persisted the agent turn with
   ``content=""`` — the streamed deltas were never accumulated, so the web
   replay (rightly) rendered nothing for agent turns.
2. Tool dispatches persisted NO replayable record at all — the inline tool
   cards (``feedback_chat_tool_interleave``) were wire-only ``pipeline-state``
   envelopes, lost the moment the socket closed.

This suite drives the REAL server seams (no Gemini, no Playwright) against
both the file-backed dev substrate and the MockMCPClient:

- agent narration accumulates across stream iterations and persists as a
  ``role="agent"`` ``CaseChatMessage`` with the real text;
- every terminal tool dispatch persists a ``role="tool"`` row carrying a
  typed ``ToolCardRecord`` (state, started_at, duration_ms from the
  authoritative job-0264 emitter stamp, label);
- failed dispatches persist ``state="failed"``; cancelled dispatches persist
  nothing (Invariant 8);
- ``get_session_state`` returns the FULL stream ordered by ``created_at``
  (user -> tool -> agent), ULID tiebreak, regardless of backend sort;
- ``list_cases_for_user`` excludes ``deleted`` AND ``archived`` Cases
  SERVER-side (the user saw a deleted ghost in the left rail);
- the user-turn persist path is byte-shape unchanged;
- Gemini-free E2E: one full simulated turn (user msg -> tool dispatch ->
  narration) through ``_prepare_user_turn`` + ``_invoke_tool_via_emitter`` +
  ``_dispatch_gemini_and_persist`` against file persistence, then the
  rehydration envelope replays the complete ordered stream.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.persistence import make_file_persistence
from trid3nt_server.tools import RegisteredTool
from trid3nt_contracts.case import CaseCommandEnvelopePayload, CaseSummary
from trid3nt_contracts.common import new_ulid, now_utc
from trid3nt_contracts.tool_registry import AtomicToolMetadata

FAKE_TOOL = "job0267_fake_tool"
FAILING_TOOL = "job0267_failing_tool"


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


@pytest.fixture()
def fake_tool():
    """Register a trivial registry tool; deregister on teardown."""

    async def _fn() -> dict:
        return {"status": "ok", "rows": 3}

    meta = AtomicToolMetadata(
        name=FAKE_TOOL, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[FAKE_TOOL] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield FAKE_TOOL
    finally:
        agent_tools.TOOL_REGISTRY.pop(FAKE_TOOL, None)


@pytest.fixture()
def failing_tool():
    """Register a registry tool that always raises; deregister on teardown."""

    async def _fn() -> dict:
        raise RuntimeError("upstream exploded")

    meta = AtomicToolMetadata(
        name=FAILING_TOOL, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[FAILING_TOOL] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield FAILING_TOOL
    finally:
        agent_tools.TOOL_REGISTRY.pop(FAILING_TOOL, None)


async def _create_case(ws, state, title="Full Stream Case") -> str:
    cmd = CaseCommandEnvelopePayload(command="create", args={"title": title})
    await server._handle_case_command(ws, state, cmd)
    case_id = state.active_case_id
    assert case_id, "create must bind the active case"
    return case_id


# --------------------------------------------------------------------------- #
# 1. Agent narration persists with the REAL accumulated text
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_agent_narration_persists_and_replays(file_persistence) -> None:
    """The terminal agent row carries the accumulated stream text — the exact
    regression the user verified (only their own messages replayed)."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)
    await server._persist_chat_turn(state, role="user", content="hi agent")

    async def fake_stream(websocket, st, settings, user_text, research_mode, bedrock_model=None, **_kwargs):
        # Mirrors _stream_gemini_reply: reset, accumulate deltas across
        # iterations, terminal chat_history append on clean completion.
        st.current_turn_narration = []
        st.current_turn_narration.append("I fetched the DEM ")
        st.current_turn_narration.append("and added it to the map.")
        st.chat_history.append({"role": "user", "text": user_text})

    orig = server._stream_gemini_reply
    server._stream_gemini_reply = fake_stream
    try:
        await server._dispatch_gemini_and_persist(ws, state, None, "hi agent", "off")
    finally:
        server._stream_gemini_reply = orig

    session_state = await file_persistence.get_session_state(case_id)
    roles = [m.role for m in session_state.chat_history]
    assert roles == ["user", "agent"]
    agent_row = session_state.chat_history[1]
    assert agent_row.content == "I fetched the DEM and added it to the map."
    assert agent_row.tool_card is None


@pytest.mark.asyncio
async def test_agent_narration_persists_even_when_stream_dies(
    file_persistence,
) -> None:
    """Best-effort on error: whatever narration accumulated before the stream
    raised is still persisted (the finally-block path)."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)

    async def dying_stream(websocket, st, settings, user_text, research_mode, bedrock_model=None, **_kwargs):
        st.current_turn_narration = []
        st.current_turn_narration.append("Partial narration before the crash")
        raise RuntimeError("LLM_UNAVAILABLE")

    orig = server._stream_gemini_reply
    server._stream_gemini_reply = dying_stream
    try:
        with pytest.raises(RuntimeError):
            await server._dispatch_gemini_and_persist(ws, state, None, "x", "off")
    finally:
        server._stream_gemini_reply = orig

    session_state = await file_persistence.get_session_state(case_id)
    assert [m.role for m in session_state.chat_history] == ["agent"]
    assert (
        session_state.chat_history[0].content
        == "Partial narration before the crash"
    )


@pytest.mark.asyncio
async def test_no_agent_row_when_stream_dies_with_nothing_said(
    file_persistence,
) -> None:
    """No narration + no terminal completion = no phantom agent row."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)

    async def instant_death(websocket, st, settings, user_text, research_mode, bedrock_model=None, **_kwargs):
        st.current_turn_narration = []
        raise RuntimeError("died before the first token")

    orig = server._stream_gemini_reply
    server._stream_gemini_reply = instant_death
    try:
        with pytest.raises(RuntimeError):
            await server._dispatch_gemini_and_persist(ws, state, None, "x", "off")
    finally:
        server._stream_gemini_reply = orig

    session_state = await file_persistence.get_session_state(case_id)
    assert session_state.chat_history == []


# --------------------------------------------------------------------------- #
# 2. Tool-card rows persist with duration + label
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# FIX B — early input-only tool-io frame at dispatch START (#7 input + Running…)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def slow_arg_tool():
    """Register a tool that takes args + yields once so the early frame can be
    observed BEFORE the completion frame; deregister on teardown."""
    name = "fixb_slow_arg_tool"

    async def _fn(*, bbox=None) -> dict:
        # Yield control so the dispatch loop is genuinely mid-flight when the
        # early frame is asserted (the tool has not yet returned its response).
        await asyncio.sleep(0)
        return {"status": "ok", "rows": 7}

    meta = AtomicToolMetadata(name=name, ttl_class="live-no-cache", cacheable=False)
    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield name
    finally:
        agent_tools.TOOL_REGISTRY.pop(name, None)


def _tool_io_frames(ws) -> list[dict]:
    """Pull every ``tool-io`` envelope's payload off the FakeWS wire."""
    out = []
    for raw in ws.sent:
        env = json.loads(raw)
        if env.get("type") == "tool-io":
            out.append(env["payload"])
    return out


@pytest.mark.asyncio
async def test_early_input_only_tool_io_frame_at_dispatch_start(
    file_persistence, slow_arg_tool
) -> None:
    """FIX B: ``_invoke_tool_via_emitter`` emits an EARLY input-only ``tool-io``
    frame at dispatch START — SAME ToolIoPayload wire shape, raw_args populated,
    function_response empty (the 'Running…' placeholder), is_error False —
    keyed on THIS dispatch's running card. (The completion-time emit that fills
    function_response lives in the outer _stream_gemini_reply loop and re-keys
    the SAME step_id; it is not driven by this lower-level seam, so exactly the
    early frame rides the wire here.)"""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await _create_case(ws, state)

    args = {"bbox": [-82.0, 26.0, -81.0, 27.0]}
    result = await server._invoke_tool_via_emitter(ws, state, slow_arg_tool, args)
    assert result == {"status": "ok", "rows": 7}

    frames = _tool_io_frames(ws)
    assert len(frames) == 1, f"expected ONE early frame, got {len(frames)}"
    early = frames[0]
    assert early["tool_name"] == slow_arg_tool
    # Input present immediately.
    assert json.loads(early["raw_args"]) == args
    # Output empty -> serialized "null" (the client treats this as Running…).
    assert early["function_response"] in (None, "null")
    assert early["is_error"] is False
    # The frame is keyed on the SAME card the live pipeline used (the emitter's
    # terminal step), so the later completion emit merges (not duplicates).
    assert early["step_id"] == state.emitter.last_tool_step.step_id


@pytest.mark.asyncio
async def test_early_frame_for_failing_tool_still_input_only(
    file_persistence, failing_tool
) -> None:
    """FIX B: even a tool that RAISES still gets the early input-only frame (the
    input + Running… paints before the failure surfaces)."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await _create_case(ws, state)

    with pytest.raises(RuntimeError, match="upstream exploded"):
        await server._invoke_tool_via_emitter(ws, state, failing_tool, {"k": "v"})

    frames = _tool_io_frames(ws)
    assert len(frames) == 1
    early = frames[0]
    assert early["tool_name"] == failing_tool
    assert json.loads(early["raw_args"]) == {"k": "v"}
    assert early["function_response"] in (None, "null")
    assert early["is_error"] is False


@pytest.mark.asyncio
async def test_tool_card_persists_with_duration(
    file_persistence, fake_tool
) -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)

    result = await server._invoke_tool_via_emitter(ws, state, FAKE_TOOL, {})
    assert result == {"status": "ok", "rows": 3}

    session_state = await file_persistence.get_session_state(case_id)
    tool_rows = [m for m in session_state.chat_history if m.role == "tool"]
    assert len(tool_rows) == 1
    row = tool_rows[0]
    card = row.tool_card
    assert card is not None
    assert card.tool_name == FAKE_TOOL
    assert card.state == "complete"
    assert card.label == FAKE_TOOL  # registry display name
    assert card.started_at is not None
    assert card.duration_ms is not None and card.duration_ms >= 0
    # content is the JSON twin of the typed record.
    assert json.loads(row.content)["tool_name"] == FAKE_TOOL
    # pipeline link + no duplicated layer attribution on tool rows.
    assert row.pipeline_id is not None
    assert row.layer_emissions == []


@pytest.mark.asyncio
async def test_tool_card_failed_state_persists_and_raises(
    file_persistence, failing_tool
) -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)

    with pytest.raises(RuntimeError, match="upstream exploded"):
        await server._invoke_tool_via_emitter(ws, state, FAILING_TOOL, {})

    session_state = await file_persistence.get_session_state(case_id)
    tool_rows = [m for m in session_state.chat_history if m.role == "tool"]
    assert len(tool_rows) == 1
    card = tool_rows[0].tool_card
    assert card is not None
    assert card.state == "failed"
    assert card.tool_name == FAILING_TOOL
    assert card.duration_ms is not None and card.duration_ms >= 0


@pytest.mark.asyncio
async def test_cancelled_dispatch_persists_no_tool_card(file_persistence) -> None:
    """Invariant 8: cancellation is not a replayable outcome — no card row."""
    name = "job0267_cancelling_tool"

    async def _fn() -> dict:
        raise asyncio.CancelledError()

    meta = AtomicToolMetadata(name=name, ttl_class="live-no-cache", cacheable=False)
    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        case_id = await _create_case(ws, state)
        with pytest.raises(asyncio.CancelledError):
            await server._invoke_tool_via_emitter(ws, state, name, {})
        session_state = await file_persistence.get_session_state(case_id)
        assert [m for m in session_state.chat_history if m.role == "tool"] == []
    finally:
        agent_tools.TOOL_REGISTRY.pop(name, None)


@pytest.mark.asyncio
async def test_no_tool_card_write_without_active_case(
    file_persistence, fake_tool, tmp_path
) -> None:
    """No active Case -> dispatch succeeds, nothing lands in the chat store."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    assert state.active_case_id is None
    await server._invoke_tool_via_emitter(ws, state, FAKE_TOOL, {})
    chat_file = tmp_path / "trid3nt_dev" / "case_chat_messages.json"
    assert (not chat_file.exists()) or chat_file.read_text().strip() in ("{}", "")


# --------------------------------------------------------------------------- #
# 3. Ordering: the rehydrated stream interleaves by created_at
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rehydrated_stream_orders_by_created_at(file_persistence) -> None:
    """Rows written out of order come back interleaved by created_at (ULID
    message_id breaks exact-timestamp ties in write order)."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)

    base = now_utc()
    from datetime import timedelta

    from trid3nt_contracts.case import CaseChatMessage, ToolCardRecord

    def _row(role, content, offset_s, card=None):
        return CaseChatMessage(
            message_id=new_ulid(),
            case_id=case_id,
            role=role,
            content=content,
            tool_card=card,
            created_at=base + timedelta(seconds=offset_s),
        )

    card = ToolCardRecord(tool_name="t", state="complete", duration_ms=5)
    # Deliberately INSERT out of chronological order.
    await file_persistence.append_chat_message(_row("agent", "done", 10))
    await file_persistence.append_chat_message(_row("user", "go", 0))
    await file_persistence.append_chat_message(
        _row("tool", card.model_dump_json(), 5, card=card)
    )

    session_state = await file_persistence.get_session_state(case_id)
    assert [m.role for m in session_state.chat_history] == ["user", "tool", "agent"]


# --------------------------------------------------------------------------- #
# 4. Server-side case-list hardening (deleted ghost)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_deleted_and_archived_cases_excluded_server_side(
    file_persistence,
) -> None:
    live = CaseSummary(
        case_id=new_ulid(), title="live", created_at=now_utc(), updated_at=now_utc()
    )
    ghost = CaseSummary(
        case_id=new_ulid(), title="ghost", created_at=now_utc(), updated_at=now_utc()
    )
    shelf = CaseSummary(
        case_id=new_ulid(), title="shelf", created_at=now_utc(), updated_at=now_utc()
    )
    # job-0252 (OQ-0115): Cases are owner-scoped (the $exists:false leak clause
    # is gone). Stamp the owner so the owner-scoped listing returns them; the
    # status filter is what this test actually exercises.
    for c in (live, ghost, shelf):
        await file_persistence.upsert_case(c, owner_user_id="anyone")
    await file_persistence.delete_case(ghost.case_id)
    await file_persistence.archive_case(shelf.case_id)

    listed = await file_persistence.list_cases_for_user("anyone")
    titles = {c.title for c in listed}
    assert titles == {"live"}


@pytest.mark.asyncio
async def test_emitted_case_list_envelope_excludes_tombstones(
    file_persistence,
) -> None:
    """The actual ``case-list`` wire emission carries no deleted/archived Case."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    # job-0252 (OQ-0115): bind the owner so the create stamps it and the
    # owner-scoped _emit_case_list lists by it.
    state.authenticated_user_id = new_ulid()
    case_id = await _create_case(ws, state)
    ghost = CaseSummary(
        case_id=new_ulid(), title="ghost", created_at=now_utc(), updated_at=now_utc()
    )
    await file_persistence.upsert_case(ghost, owner_user_id=state.authenticated_user_id)
    await file_persistence.delete_case(ghost.case_id)

    ws.sent.clear()
    # OPEN-8 change-guard: force=True — this assertion is about tombstone
    # FILTERING (the ghost Case must never appear), not about the guard's
    # skip-when-unchanged behavior. The ghost create+delete round-trip here
    # goes through Persistence directly (bypassing the server's
    # case-command handler), so the visible-case content coincidentally
    # matches what the earlier create step already cached; without
    # force=True this direct re-invocation would be a legitimate guard skip
    # rather than the tombstone-filtering check this test wants.
    await server._emit_case_list(ws, state, force=True)
    envelopes = [json.loads(t) for t in ws.sent]
    case_lists = [e for e in envelopes if e["type"] == "case-list"]
    assert len(case_lists) == 1
    listed_ids = [c["case_id"] for c in case_lists[0]["payload"]["cases"]]
    assert case_id in listed_ids
    assert ghost.case_id not in listed_ids


@pytest.mark.asyncio
async def test_pre_status_case_docs_stay_listed(file_persistence) -> None:
    """Backward-compat: docs that pre-date the status field are live."""
    legacy_id = new_ulid()
    # Write a raw doc with NO status key at all (pre-CaseStatus record).
    # job-0252 (OQ-0115): the doc carries a user_id so it survives the now
    # owner-scoped listing — this test exercises pre-*status*-field
    # backward-compat, not the (now removed) pre-Auth owner leak.
    await file_persistence._mcp.call_tool(
        "insert-one",
        {
            "database": file_persistence._db,
            "collection": "projects",
            "document": {
                "_id": legacy_id,
                "schema_version": "v1",
                "case_id": legacy_id,
                "title": "legacy",
                "user_id": "anyone",
                "created_at": now_utc().isoformat().replace("+00:00", "Z"),
                "updated_at": now_utc().isoformat().replace("+00:00", "Z"),
            },
        },
    )
    listed = await file_persistence.list_cases_for_user("anyone")
    assert [c.title for c in listed] == ["legacy"]


# --------------------------------------------------------------------------- #
# 5. User-turn path unchanged
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_user_turn_shape_unchanged(file_persistence) -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)
    state.current_turn_layer_ids = ["L-1"]
    await server._persist_chat_turn(state, role="user", content="model the flood")

    session_state = await file_persistence.get_session_state(case_id)
    assert len(session_state.chat_history) == 1
    row = session_state.chat_history[0]
    assert row.role == "user"
    assert row.content == "model the flood"
    assert row.layer_emissions == ["L-1"]  # accumulator default preserved
    assert row.tool_card is None
    assert row.pipeline_id is None


# --------------------------------------------------------------------------- #
# 6. Gemini-free E2E: full turn -> complete ordered stream on reopen
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_e2e_full_turn_replays_complete_stream(
    file_persistence, fake_tool
) -> None:
    """One simulated turn through the REAL seams: ``_prepare_user_turn``
    (user persist) -> fake Gemini stream that narrates, dispatches a real
    registry tool via ``_invoke_tool_via_emitter``, narrates again ->
    ``_dispatch_gemini_and_persist`` terminal persist. The rehydration
    envelope must replay user -> tool -> agent, in order, with content."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)

    async def fake_stream(websocket, st, settings, user_text, research_mode, bedrock_model=None, **_kwargs):
        st.current_turn_narration = []
        st.current_turn_narration.append("I'm fetching the data now. ")
        await server._invoke_tool_via_emitter(websocket, st, FAKE_TOOL, {})
        st.current_turn_narration.append("Done — 3 rows fetched.")
        st.chat_history.append({"role": "user", "text": user_text})

    directive = await server._prepare_user_turn(ws, state, "fetch the data")
    assert directive is None  # Gemini path

    orig = server._stream_gemini_reply
    server._stream_gemini_reply = fake_stream
    try:
        await server._dispatch_gemini_and_persist(
            ws, state, None, "fetch the data", "off"
        )
    finally:
        server._stream_gemini_reply = orig

    # Fresh "browser": reopen the Case and replay the full stream.
    session_state = await file_persistence.get_session_state(case_id)
    rows = session_state.chat_history
    assert [m.role for m in rows] == ["user", "tool", "agent"]
    assert rows[0].content == "fetch the data"
    assert rows[1].tool_card is not None
    assert rows[1].tool_card.tool_name == FAKE_TOOL
    assert rows[1].tool_card.state == "complete"
    assert rows[1].tool_card.duration_ms is not None
    assert rows[2].content == "I'm fetching the data now. Done — 3 rows fetched."
    # created_at strictly non-decreasing — the web interleave key.
    stamps = [m.created_at for m in rows]
    assert stamps == sorted(stamps)


# --------------------------------------------------------------------------- #
# 7. job-0315 — narration SEGMENTS interleave with tool rows in creation order
# --------------------------------------------------------------------------- #


from trid3nt_server.adapter import (  # noqa: E402 — grouped with the job-0315 test
    FunctionCallEvent,
    GeminiSettings,
    TextDeltaEvent,
)


async def _drive_real_stream(ws, state, turn_events):
    """Drive the REAL _stream_gemini_reply (via _dispatch_gemini_and_persist)
    with a mocked ``stream_events_with_contents`` yielding ``turn_events`` —
    a list of per-turn event lists. Uses the REAL ``_invoke_tool_via_emitter``
    so tool-card rows persist mid-turn (the whole point of the interleave)."""
    from unittest.mock import patch

    from trid3nt_server import server as agent_server

    turns = iter(turn_events)

    async def _fake_stream(*_args, **_kwargs):
        for evt in next(turns):
            yield evt

    settings = GeminiSettings(
        model="m", project="p", location="us-central1", use_vertex=True
    )
    with patch.object(agent_server, "build_client", return_value=object()), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]), \
         patch.object(agent_server, "stream_events_with_contents", _fake_stream):
        await agent_server._dispatch_gemini_and_persist(
            ws, state, settings, "two segments two tools", "research"
        )


@pytest.mark.asyncio
async def test_segment_rows_interleave_with_tool_rows(file_persistence) -> None:
    """job-0315: text -> tool -> text -> tool -> text persists FIVE agent/tool
    rows interleaved in creation order: [user, agent, tool, agent, tool, agent].
    Only the LAST agent row carries the layer accumulator; segment rows carry []."""
    # Two fresh registry tools, both allowed via record_explicit.
    async def _t1() -> dict:
        return {"status": "ok"}

    async def _t2() -> dict:
        return {"status": "ok"}

    for nm, fn in (("job0315_tool_a", _t1), ("job0315_tool_b", _t2)):
        meta = AtomicToolMetadata(name=nm, ttl_class="live-no-cache", cacheable=False)
        agent_tools.TOOL_REGISTRY[nm] = RegisteredTool(metadata=meta, fn=fn, module=__name__)

    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        case_id = await _create_case(ws, state)
        # Pin the turn's Case (mirrors _prepare_user_turn) and a layer the
        # accumulator should attribute to the FINAL narration row only.
        state.current_turn_case_id = case_id
        state.current_turn_layer_ids = ["L-final"]
        state.allowed_tool_set.add_tools(["job0315_tool_a", "job0315_tool_b"])
        await server._persist_chat_turn(state, role="user", content="go")

        # Round 1: text + call A. Round 2: text + call B. Round 3: text, no call.
        turn_events = [
            [TextDeltaEvent("Fetching A. "),
             FunctionCallEvent(name="job0315_tool_a", args={}, call_id="c1")],
            [TextDeltaEvent("Now fetching B. "),
             FunctionCallEvent(name="job0315_tool_b", args={}, call_id="c2")],
            [TextDeltaEvent("All done.")],
        ]
        await _drive_real_stream(ws, state, turn_events)

        session_state = await file_persistence.get_session_state(case_id)
        rows = session_state.chat_history
        roles = [m.role for m in rows]
        assert roles == ["user", "agent", "tool", "agent", "tool", "agent"], roles

        # Each narration segment persisted ONLY its own contiguous run.
        agent_rows = [m for m in rows if m.role == "agent"]
        assert agent_rows[0].content == "Fetching A."
        assert agent_rows[1].content == "Now fetching B."
        assert agent_rows[2].content == "All done."

        # Tool rows in dispatch order.
        tool_rows = [m for m in rows if m.role == "tool"]
        assert [t.tool_card.tool_name for t in tool_rows] == [
            "job0315_tool_a", "job0315_tool_b"
        ]

        # ONLY the last (terminal) agent row carries the layer accumulator;
        # the two non-terminal segment rows carry [].
        assert agent_rows[0].layer_emissions == []
        assert agent_rows[1].layer_emissions == []
        assert agent_rows[2].layer_emissions == ["L-final"]

        # created_at strictly non-decreasing — the web interleave key.
        stamps = [m.created_at for m in rows]
        assert stamps == sorted(stamps)
    finally:
        agent_tools.TOOL_REGISTRY.pop("job0315_tool_a", None)
        agent_tools.TOOL_REGISTRY.pop("job0315_tool_b", None)


@pytest.mark.asyncio
async def test_narration_less_completed_turn_writes_single_marker(
    file_persistence,
) -> None:
    """job-0315 edge: a completed turn with ZERO agent text + no tools still
    writes exactly ONE marker agent row (content="") — replay row count is
    unchanged from the pre-fix single-row contract."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)
    state.current_turn_case_id = case_id

    # One turn, no text, no function calls -> clean terminal, zero segments.
    turn_events = [[]]
    await _drive_real_stream(ws, state, turn_events)

    session_state = await file_persistence.get_session_state(case_id)
    rows = session_state.chat_history
    assert [m.role for m in rows] == ["agent"]
    assert rows[0].content == ""


@pytest.mark.asyncio
async def test_text_only_turn_single_segment_row(file_persistence) -> None:
    """job-0315 edge: a text-only turn (no tools) persists exactly ONE agent
    row with the full narration — byte-identical to the pre-fix single-row."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)
    state.current_turn_case_id = case_id
    state.current_turn_layer_ids = ["L-1"]

    turn_events = [[TextDeltaEvent("Here "), TextDeltaEvent("is the answer.")]]
    await _drive_real_stream(ws, state, turn_events)

    session_state = await file_persistence.get_session_state(case_id)
    rows = session_state.chat_history
    assert [m.role for m in rows] == ["agent"]
    assert rows[0].content == "Here is the answer."
    # Terminal segment carries the accumulator (job-0259/0281).
    assert rows[0].layer_emissions == ["L-1"]


def _extract_last_zoom_to(rows):
    """Python mirror of web ``extractLastZoomTo`` (case_zoom.ts): walk rows
    newest-first, each row's ``map_command_emissions`` last-entry-first, and
    return the first ``zoom-to`` carrying a bbox. This is exactly what the
    Case-reopen camera snap (job-0280) runs over the rehydrated chat history,
    so a green here proves the snap survives a tool-terminal turn."""
    for msg in reversed(rows):
        emissions = msg.map_command_emissions or []
        for cmd in reversed(emissions):
            if isinstance(cmd, dict) and cmd.get("command") == "zoom-to":
                args = cmd.get("args") if isinstance(cmd.get("args"), dict) else {}
                if isinstance(args.get("bbox"), list):
                    return cmd
    return None


@pytest.mark.asyncio
async def test_tool_terminal_turn_persists_zoom_to_accumulator(
    file_persistence,
) -> None:
    """job-0315 CONTRACT REGRESSION (panel blocker): a turn whose FINAL
    generation round ends in tool calls with NO trailing narration (the
    COMMON flood/publish shape — e.g. ...-> publish_layer is the last call)
    must STILL persist a chat row carrying the turn's zoom-to / layer
    accumulator, so the web ``extractLastZoomTo(chat_history)`` snaps the
    Case-reopen camera to the AOI (job-0259/0280/0281).

    Pre-fix: the in-loop terminal finalize only fires when the turn ends in
    narration (``current_message_id is not None``); a tool-terminal turn left
    NO row carrying ``layer_emissions`` / ``map_command_emissions``, so the
    snap found nothing and the camera never moved.
    """

    async def _publishish() -> dict:
        return {"status": "ok"}

    meta = AtomicToolMetadata(
        name="job0315_pub", ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY["job0315_pub"] = RegisteredTool(
        metadata=meta, fn=_publishish, module=__name__
    )
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        case_id = await _create_case(ws, state)
        state.current_turn_case_id = case_id
        state.allowed_tool_set.add_tools(["job0315_pub"])
        await server._persist_chat_turn(state, role="user", content="flood it")

        # The turn accumulated a geocode zoom-to (job-0281) + a published layer
        # (job-0259) earlier; then its FINAL round emits text + a tool call and
        # ENDS — no trailing narration after the tool round (current_message_id
        # is None at close, so the in-loop terminal finalize does NOT fire).
        zoom_bbox = [-82.0, 26.5, -81.7, 26.8]
        state.current_turn_map_commands = [
            {"command": "zoom-to", "args": {"bbox": list(zoom_bbox)}}
        ]
        state.current_turn_layer_ids = ["flood-depth-L1"]

        turn_events = [
            [
                TextDeltaEvent("Publishing the flood layer. "),
                FunctionCallEvent(name="job0315_pub", args={}, call_id="c1"),
            ],
            # Final round: a tool call with NO trailing text -> tool-terminal.
            [FunctionCallEvent(name="job0315_pub", args={}, call_id="c2")],
        ]
        await _drive_real_stream(ws, state, turn_events)

        session_state = await file_persistence.get_session_state(case_id)
        rows = session_state.chat_history
        roles = [m.role for m in rows]
        # Interleave is intact: the one narration segment + two tool rows, plus
        # the closing accumulator-bearing marker row. No re-bunching.
        assert roles == ["user", "agent", "tool", "tool", "agent"], roles

        # The first agent row is the real narration segment; it must NOT carry
        # the accumulator (it is non-terminal) — no re-bunching regression.
        narration_rows = [
            m for m in rows if m.role == "agent" and m.content
        ]
        assert len(narration_rows) == 1
        assert narration_rows[0].content == "Publishing the flood layer."
        assert narration_rows[0].layer_emissions == []
        assert narration_rows[0].map_command_emissions == []

        # The closing marker row is empty text (no phantom bubble on replay)
        # but CARRIES the layer + zoom-to accumulator (the whole fix).
        marker = rows[-1]
        assert marker.role == "agent"
        assert marker.content == ""
        assert marker.layer_emissions == ["flood-depth-L1"]
        assert _extract_last_zoom_to(rows) == {
            "command": "zoom-to",
            "args": {"bbox": list(zoom_bbox)},
        }

        # created_at strictly non-decreasing — the web interleave key.
        stamps = [m.created_at for m in rows]
        assert stamps == sorted(stamps)
    finally:
        agent_tools.TOOL_REGISTRY.pop("job0315_pub", None)


@pytest.mark.asyncio
async def test_tool_terminal_turn_without_accumulator_writes_no_phantom(
    file_persistence,
) -> None:
    """job-0315 guard: a tool-terminal turn that emitted NO zoom-to AND NO
    layer accumulator AND no trailing narration must write NOTHING extra —
    the contract fix must not resurrect phantom empty bubbles for turns that
    carry no accumulator and no text."""

    async def _noop() -> dict:
        return {"status": "ok"}

    meta = AtomicToolMetadata(
        name="job0315_noop", ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY["job0315_noop"] = RegisteredTool(
        metadata=meta, fn=_noop, module=__name__
    )
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        case_id = await _create_case(ws, state)
        state.current_turn_case_id = case_id
        state.allowed_tool_set.add_tools(["job0315_noop"])
        await server._persist_chat_turn(state, role="user", content="just check")
        # No accumulator populated this turn.
        assert not state.current_turn_layer_ids
        assert not state.current_turn_map_commands

        turn_events = [
            [
                TextDeltaEvent("Checking. "),
                FunctionCallEvent(name="job0315_noop", args={}, call_id="c1"),
            ],
            # Final round: tool call, no trailing narration, no accumulator.
            [FunctionCallEvent(name="job0315_noop", args={}, call_id="c2")],
        ]
        await _drive_real_stream(ws, state, turn_events)

        session_state = await file_persistence.get_session_state(case_id)
        rows = session_state.chat_history
        # user, the one narration segment, two tool rows — NO closing marker.
        assert [m.role for m in rows] == ["user", "agent", "tool", "tool"], [
            m.role for m in rows
        ]
        # No phantom empty agent bubble appended.
        assert rows[-1].role == "tool"
    finally:
        agent_tools.TOOL_REGISTRY.pop("job0315_noop", None)
