"""BUG 3 + BUG 4b — terminal closing narration + terminal-failure replay card.

Two server-side fixes, both driving the REAL ``_stream_gemini_reply`` /
``_dispatch_gemini_and_persist`` seams (no Gemini, no Playwright) against
file-backed persistence:

BUG 3 (missing closing narration): after a long tool/solve completes the turn
can exit with NO open narration segment on the wire. When narration was
accumulated across iterations but NO segment was ever streamed
(``segments_done == 0``), the turn used to end with no terminal ``done=True``
agent-message frame and no closing summary at all. The fix opens ONE final
agent-message segment, streams the accumulated narration, and closes it
``done=True`` — but ONLY when there IS accumulated narration (an empty-narration
turn still emits NO bubble: no job-0315 regression).

BUG 4b (terminal failure lost on reconnect): when a turn ends in a terminal
FAILURE on the model-generation path (``LLM_UNAVAILABLE`` / ``_send_error``),
the live error envelope marks the in-memory pipeline failed but persists NOTHING
to ``chat_history``. A WS reconnect / Case-reopen then replays the last tool card
still ``running`` forever. The fix persists a ``role="tool"`` FAILED tool-card
row (mirroring the existing tool-card shape) so the session-resume replay renders
the failed card and the user knows the turn STOPPED — never fabricating a success.
"""

from __future__ import annotations

import json

import pytest

from trid3nt_server import server
from trid3nt_server.adapter import (
    FunctionCallEvent,
    GeminiSettings,
    TextDeltaEvent,
)
from trid3nt_server import tools as agent_tools
from trid3nt_server.persistence import make_file_persistence
from trid3nt_server.tools import RegisteredTool
from trid3nt_contracts.case import CaseCommandEnvelopePayload
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.tool_registry import AtomicToolMetadata


class FakeWS:
    """Minimal ServerConnection stand-in: records every envelope sent."""

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


async def _create_case(ws, state, title="Terminal Narration Case") -> str:
    cmd = CaseCommandEnvelopePayload(command="create", args={"title": title})
    await server._handle_case_command(ws, state, cmd)
    case_id = state.active_case_id
    assert case_id, "create must bind the active case"
    return case_id


def _agent_chunks(ws):
    """Decode every ``agent-message-chunk`` envelope the server emitted."""
    out = []
    for raw in ws.sent:
        env = json.loads(raw)
        if env.get("type") == "agent-message-chunk":
            out.append(env["payload"])
    return out


async def _drive_real_stream(ws, state, fake_stream):
    """Drive REAL ``_stream_gemini_reply`` via ``_dispatch_gemini_and_persist``
    with a mocked ``stream_events_with_contents`` (``fake_stream``)."""
    from unittest.mock import patch

    from trid3nt_server import server as agent_server

    settings = GeminiSettings(
        model="m", project="p", location="us-central1", use_vertex=True
    )
    with patch.object(agent_server, "build_client", return_value=object()), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]), \
         patch.object(agent_server, "stream_events_with_contents", fake_stream):
        await agent_server._dispatch_gemini_and_persist(
            ws, state, settings, "do the thing", "research"
        )


# --------------------------------------------------------------------------- #
# BUG 3 — missing closing narration
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_accumulated_narration_no_open_segment_emits_terminal_done(
    file_persistence,
) -> None:
    """BUG 3: a turn that ACCUMULATED narration but never opened a wire segment
    (``segments_done == 0``, ``current_message_id is None`` at exit) must emit a
    terminal ``done=True`` agent-message carrying that narration, and persist it
    as the closing ``role="agent"`` row.

    Reachability: the fake stream appends to the job-0267 narration accumulator
    (``state.current_turn_narration`` — the same list the loop fills) and yields
    NO events, so no per-segment bubble ever opens. Pre-fix the closing summary
    was lost; post-fix it surfaces on the wire AND in chat history.
    """
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)
    state.current_turn_case_id = case_id

    closing = "The flood model finished. Peak depth is 1.8 m near the river."

    async def fake_stream(*_args, **_kwargs):
        # Simulate narration accumulated across iterations WITHOUT opening a
        # per-segment wire bubble (segments_done stays 0).
        state.current_turn_narration.append(closing)
        return
        yield  # noqa: F811 — make this an async generator

    ws.sent.clear()
    await _drive_real_stream(ws, state, fake_stream)

    # Wire: exactly one agent bubble (one message_id), terminated done=True,
    # carrying the recovered narration.
    chunks = _agent_chunks(ws)
    assert chunks, "a terminal agent-message-chunk must be emitted"
    ids = {c["message_id"] for c in chunks}
    assert len(ids) == 1, f"recovery must open exactly ONE bubble, got ids={ids}"
    assert any(c["delta"] == closing for c in chunks), chunks
    assert chunks[-1]["done"] is True, "the closing frame must be done=True"

    # Persistence: a single closing agent row carries the recovered narration.
    session_state = await file_persistence.get_session_state(case_id)
    agent_rows = [m for m in session_state.chat_history if m.role == "agent"]
    assert len(agent_rows) == 1, [m.role for m in session_state.chat_history]
    assert agent_rows[0].content == closing


@pytest.mark.asyncio
async def test_empty_narration_turn_emits_no_bubble(file_persistence) -> None:
    """BUG 3 guard (no job-0315 regression): a turn with NO accumulated
    narration and no open segment must emit NO closing agent-message-chunk and
    write NO phantom non-empty bubble (the narration-less marker row is allowed
    but it carries empty content — never a fabricated summary)."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)
    state.current_turn_case_id = case_id

    async def fake_stream(*_args, **_kwargs):
        # No narration, no tool calls -> clean terminal, zero segments.
        return
        yield  # noqa: F811 — async generator

    ws.sent.clear()
    await _drive_real_stream(ws, state, fake_stream)

    # No recovered narration -> the only agent-message-chunk frames allowed are
    # empty-delta terminators (never a non-empty fabricated summary).
    chunks = _agent_chunks(ws)
    assert all(c["delta"] == "" for c in chunks), chunks

    # The narration-less completed-turn marker is the single empty agent row
    # (pre-fix one-row contract) — NO phantom non-empty bubble.
    session_state = await file_persistence.get_session_state(case_id)
    agent_rows = [m for m in session_state.chat_history if m.role == "agent"]
    assert all(r.content == "" for r in agent_rows), [
        r.content for r in agent_rows
    ]


@pytest.mark.asyncio
async def test_segments_already_streamed_no_double_narration(
    file_persistence,
) -> None:
    """BUG 3 anti-double-emission: when narration segment(s) were ALREADY
    streamed+finalized this turn (``segments_done > 0``) and the turn then ends
    tool-terminal, the recovery branch must NOT re-stream the accumulated
    narration (that would DOUBLE the closing text on the wire and duplicate the
    chat rows). Only ONE narration bubble survives."""

    async def _tool() -> dict:
        return {"status": "ok"}

    meta = AtomicToolMetadata(
        name="bug3_tool", ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY["bug3_tool"] = RegisteredTool(
        metadata=meta, fn=_tool, module=__name__
    )
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        case_id = await _create_case(ws, state)
        state.current_turn_case_id = case_id
        state.allowed_tool_set.add_tools(["bug3_tool"])

        # Round 1: text "Working on it." + tool call (segment finalized ->
        # segments_done == 1). Round 2: tool call only, no trailing text
        # (tool-terminal). Round 3: nothing -> break.
        rounds = iter(
            [
                [
                    TextDeltaEvent("Working on it."),
                    FunctionCallEvent(name="bug3_tool", args={}, call_id="c1"),
                ],
                [FunctionCallEvent(name="bug3_tool", args={}, call_id="c2")],
                [],
            ]
        )

        async def fake_stream(*_args, **_kwargs):
            for evt in next(rounds):
                yield evt

        ws.sent.clear()
        await _drive_real_stream(ws, state, fake_stream)

        # Exactly one NON-EMPTY narration delta on the wire (the finalized
        # segment). No second "Working on it." re-stream from the recovery.
        chunks = _agent_chunks(ws)
        nonempty = [c for c in chunks if c["delta"]]
        assert [c["delta"] for c in nonempty] == ["Working on it."], chunks

        # Persistence: exactly one non-empty agent row.
        session_state = await file_persistence.get_session_state(case_id)
        narration_rows = [
            m
            for m in session_state.chat_history
            if m.role == "agent" and m.content
        ]
        assert len(narration_rows) == 1
        assert narration_rows[0].content == "Working on it."
    finally:
        agent_tools.TOOL_REGISTRY.pop("bug3_tool", None)


# --------------------------------------------------------------------------- #
# BUG 4b — terminal failure persisted so reconnect replay surfaces it
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_terminal_model_failure_persists_failed_tool_card(
    file_persistence,
) -> None:
    """BUG 4b: a model-generation failure (LLM_UNAVAILABLE) persists a
    ``role="tool"`` row whose ``tool_card.state == "failed"``, so a later Case
    reopen / WS reconnect replays the FAILED card instead of a card stuck
    ``running`` forever. The A.6 error_code + message ride in the row content
    (the ToolCardRecord contract has no error fields)."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)
    state.current_turn_case_id = case_id

    async def exploding_stream(*_args, **_kwargs):
        raise RuntimeError("bedrock 500")
        yield  # noqa: F811 — async generator

    ws.sent.clear()
    # The model-stream exception is caught + surfaced as an error envelope (no
    # re-raise) inside _stream_gemini_reply.
    await _drive_real_stream(ws, state, exploding_stream)

    # An error envelope was emitted live (existing behavior).
    envelopes = [json.loads(t) for t in ws.sent]
    errors = [e for e in envelopes if e.get("type") == "error"]
    assert any(e["payload"]["error_code"] == "LLM_UNAVAILABLE" for e in errors), (
        envelopes
    )

    # Reconnect replay: a persisted role="tool" FAILED card now surfaces the
    # terminal failure (pre-fix: nothing persisted -> card spins forever).
    session_state = await file_persistence.get_session_state(case_id)
    tool_rows = [m for m in session_state.chat_history if m.role == "tool"]
    assert len(tool_rows) == 1, [m.role for m in session_state.chat_history]
    card = tool_rows[0].tool_card
    assert card is not None
    assert card.state == "failed", card
    # The error reason rides in the JSON-twin content (and the label).
    content = json.loads(tool_rows[0].content)
    assert content["error_code"] == "LLM_UNAVAILABLE"
    assert "bedrock 500" in content["message"]
    assert "LLM_UNAVAILABLE" in (card.label or "")
    # Mirror the tool-card row shape exactly (no layer attribution on tool rows).
    assert tool_rows[0].layer_emissions == []


@pytest.mark.asyncio
async def test_terminal_failure_card_skipped_without_active_case(
    file_persistence, tmp_path
) -> None:
    """BUG 4b guard: no active Case -> the terminal-failure helper writes
    nothing (the M1 in-memory path keeps working; no orphan rows)."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    assert state.active_case_id is None

    await server._persist_terminal_failure_card(
        state, error_code="LLM_UNAVAILABLE", message="boom", case_id=None
    )

    chat_file = tmp_path / "trid3nt_dev" / "case_chat_messages.json"
    assert (not chat_file.exists()) or chat_file.read_text().strip() in ("{}", "")


@pytest.mark.asyncio
async def test_terminal_failure_card_never_fabricates_success(
    file_persistence,
) -> None:
    """Honesty floor: the persisted terminal-failure card is ALWAYS state
    ``failed`` — it must never read as a completed/success card on replay."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)
    state.current_turn_case_id = case_id

    await server._persist_terminal_failure_card(
        state,
        error_code="SOLVE_FAILED",
        message="SFINCS exited non-zero",
        case_id=case_id,
    )

    session_state = await file_persistence.get_session_state(case_id)
    tool_rows = [m for m in session_state.chat_history if m.role == "tool"]
    assert len(tool_rows) == 1
    assert tool_rows[0].tool_card.state == "failed"
    assert tool_rows[0].tool_card.state != "complete"
