"""BUG 1 + BUG 2 (post-OPEN-14 acceptance rerun): the ContextWindowExceededError
abort path in server.py's ``_stream_gemini_reply`` / ``_dispatch_gemini_and_persist``.

Root cause (BUG 1): the OLD except-block sent the live error envelope FIRST and
persisted the typed terminal-failure card SECOND. Both proven reproductions
(sessions 01KXAGEJAAPWDH0YSEGYQK5QVG / 01KXAJ1WKWDC0XS7VW4RY6CVF6) had a
dead/detached client socket, and neither the persist's success INFO nor its own
exception log ever fired -- the persist call was never reached. ``_send_error``
-> ``_session_safe_send`` only catches ``Exception`` (not ``BaseException``), so
an await on a dead-socket send that surfaces as anything else escapes straight
past the persist and out of the except-block. Fix: persist FIRST (it never
touches the socket, so a dead/detached connection can never starve it), attempt
the best-effort socket send SECOND, both individually try/excepted with logging.
Also: the honest window-exceeded verdict is now appended directly onto the
turn's persisted partial-reply text (the streamed garbage is already
persisted -- the reader must see the abort verdict right after it), not only in
the transient error envelope a dead socket may drop.

Root cause (BUG 2): the fabrication backstop (``looks_like_fabricated_action_claim``)
is wired into the normal zero-tool-call terminal branch but was skipped
entirely on the exception (abort) path, so an abort mid-fabrication persisted
an unqualified false claim ("The hillshade has been generated..."). Fixed by
folding the same structural gate (zero tool calls this turn) + text regex into
the abort note builder (``context_budget.build_context_window_abort_note``).

These tests drive the REAL ``_stream_gemini_reply`` / ``_dispatch_gemini_and_persist``
seams (no Gemini, no Playwright) against file-backed persistence, mirroring
``tests/test_terminal_narration_and_failure_card.py``.
"""

from __future__ import annotations

import json
import logging

import pytest

from grace2_agent import server
from grace2_agent.adapter import GeminiSettings, TextDeltaEvent, FunctionCallEvent
from grace2_agent import tools as agent_tools
from grace2_agent.context_budget import (
    CONTEXT_WINDOW_ABORT_NOTE,
    ContextWindowExceededError,
    FABRICATION_CAVEAT,
)
from grace2_agent.persistence import make_file_persistence
from grace2_agent.tools import RegisteredTool
from grace2_contracts.case import CaseCommandEnvelopePayload
from grace2_contracts.common import new_ulid
from grace2_contracts.tool_registry import AtomicToolMetadata


class FakeWS:
    """Minimal ServerConnection stand-in: records every envelope sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class DeadWS:
    """A socket that is ALREADY dead (the 2x-reproduced repro shape: a
    detached turn whose client connection is gone). Every send raises --
    exactly the condition BUG 1's fix must not be starved by."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        raise ConnectionResetError("dead socket (detached turn)")


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


async def _create_case(ws, state, title="Context Window Abort Case") -> str:
    cmd = CaseCommandEnvelopePayload(command="create", args={"title": title})
    await server._handle_case_command(ws, state, cmd)
    case_id = state.active_case_id
    assert case_id, "create must bind the active case"
    return case_id


async def _drive_real_stream(ws, state, fake_stream):
    """Drive REAL ``_stream_gemini_reply`` via ``_dispatch_gemini_and_persist``
    with a mocked ``stream_events_with_contents`` (``fake_stream``)."""
    from unittest.mock import patch

    from grace2_agent import server as agent_server

    settings = GeminiSettings(
        model="m", project="p", location="us-central1", use_vertex=True
    )
    with patch.object(agent_server, "build_client", return_value=object()), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]), \
         patch.object(agent_server, "stream_events_with_contents", fake_stream):
        await agent_server._dispatch_gemini_and_persist(
            ws, state, settings, "do the thing", "research"
        )


def _persisted_rows(session_state):
    agent_rows = [m for m in session_state.chat_history if m.role == "agent"]
    tool_rows = [m for m in session_state.chat_history if m.role == "tool"]
    return agent_rows, tool_rows


# --------------------------------------------------------------------------- #
# BUG 1 -- terminal failure card ALWAYS persists, even on a dead socket, and
# the abort verdict lands on the persisted partial-reply row.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_abort_on_dead_socket_still_persists_failure_card(file_persistence) -> None:
    """The 2x-reproduced shape: the client socket is ALREADY dead when the
    abort fires. Pre-fix this silently dropped the terminal-failure card
    (neither its success INFO nor a failure exception log ever appeared).
    Post-fix: persist runs BEFORE the (now best-effort, failing) socket send,
    so the failed card lands regardless."""
    # Case setup rides a LIVE socket (its own raw ``websocket.send`` calls are
    # unrelated to the abort-path fix); the turn itself is then driven on a
    # socket that is ALREADY dead, matching the repro.
    setup_ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(setup_ws, state)
    state.current_turn_case_id = case_id
    ws = DeadWS()

    streamed_text = "Computing hillshade... " * 20

    async def fake_stream(*_args, **_kwargs):
        yield TextDeltaEvent(delta=streamed_text)
        raise ContextWindowExceededError(16384)

    # Must not raise out of the dispatch wrapper despite the dead socket.
    await _drive_real_stream(ws, state, fake_stream)

    session_state = await file_persistence.get_session_state(case_id)
    agent_rows, tool_rows = _persisted_rows(session_state)

    assert len(tool_rows) == 1, [m.role for m in session_state.chat_history]
    card = tool_rows[0].tool_card
    assert card is not None
    assert card.state == "failed"
    content = json.loads(tool_rows[0].content)
    assert content["error_code"] == "CONTEXT_WINDOW_EXCEEDED"

    # The partial narration is persisted too, WITH the abort note appended
    # right after the streamed text (reader sees the verdict immediately
    # after the unverified prose, not only in a dropped error envelope).
    assert len(agent_rows) == 1, [m.content for m in agent_rows]
    assert agent_rows[0].content == streamed_text.strip() + CONTEXT_WINDOW_ABORT_NOTE
    assert agent_rows[0].content.startswith(streamed_text.strip())


@pytest.mark.asyncio
async def test_abort_appends_note_with_caveat_ordering_when_fabricated(
    file_persistence,
) -> None:
    """BUG 2: zero tool calls this turn + a partial reply that claims a
    completed geospatial action -> the appended note must LEAD with the
    fabrication caveat, then the context-window explanation."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)
    state.current_turn_case_id = case_id

    fabricated_text = (
        "The hillshade has been generated and added to the map."
    )

    async def fake_stream(*_args, **_kwargs):
        yield TextDeltaEvent(delta=fabricated_text)
        raise ContextWindowExceededError(16384)

    await _drive_real_stream(ws, state, fake_stream)

    session_state = await file_persistence.get_session_state(case_id)
    agent_rows, _ = _persisted_rows(session_state)
    assert len(agent_rows) == 1
    content = agent_rows[0].content
    assert content.startswith(fabricated_text)
    assert FABRICATION_CAVEAT in content
    assert CONTEXT_WINDOW_ABORT_NOTE in content
    # Caveat LEADS -- appears before the context-window explanation.
    assert content.index(FABRICATION_CAVEAT) < content.index(CONTEXT_WINDOW_ABORT_NOTE)
    assert content.index(fabricated_text) < content.index(FABRICATION_CAVEAT)


@pytest.mark.asyncio
async def test_abort_after_a_real_tool_call_never_adds_fabrication_caveat(
    file_persistence,
) -> None:
    """The fabrication backstop must stay scoped to a ZERO-tool-call turn
    (context_budget.looks_like_fabricated_action_claim's own contract): a
    turn that dispatched a real tool this turn and THEN aborted on a later
    round must get the plain abort note, never the caveat, even if the
    closing text happens to match the claim-shaped regex."""

    async def _tool() -> dict:
        return {"status": "ok"}

    meta = AtomicToolMetadata(
        name="abort_tool", ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY["abort_tool"] = RegisteredTool(
        metadata=meta, fn=_tool, module=__name__
    )
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        case_id = await _create_case(ws, state)
        state.current_turn_case_id = case_id
        state.allowed_tool_set.add_tools(["abort_tool"])

        rounds = iter(
            [
                [FunctionCallEvent(name="abort_tool", args={}, call_id="c1")],
            ]
        )

        async def fake_stream(*_args, **_kwargs):
            try:
                events = next(rounds)
            except StopIteration:
                yield TextDeltaEvent(
                    delta="The hillshade has been generated and added to the map."
                )
                raise ContextWindowExceededError(16384)
            for evt in events:
                yield evt

        await _drive_real_stream(ws, state, fake_stream)

        session_state = await file_persistence.get_session_state(case_id)
        agent_rows, tool_rows = _persisted_rows(session_state)
        assert any(r.tool_card is not None for r in tool_rows)  # the real call ran

        # The last agent row is the aborted round's narration.
        abort_rows = [r for r in agent_rows if CONTEXT_WINDOW_ABORT_NOTE in r.content]
        assert len(abort_rows) == 1, [r.content for r in agent_rows]
        assert FABRICATION_CAVEAT not in abort_rows[0].content
    finally:
        agent_tools.TOOL_REGISTRY.pop("abort_tool", None)


# --------------------------------------------------------------------------- #
# BUG 1 -- both persist-path and send-path failures are individually logged.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_both_failure_logs_fire_when_persist_and_send_each_raise(
    file_persistence, monkeypatch, caplog
) -> None:
    """Defense-in-depth: even if ``_persist_terminal_failure_card`` or
    ``_send_error`` themselves raise (bypassing their own internal
    catch-alls), the except-ContextWindowExceededError handler's own
    try/excepts must log EACH failure individually -- never silently
    swallow one, and never let one skip the other."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_id = await _create_case(ws, state)
    state.current_turn_case_id = case_id

    async def _boom_persist(*_a, **_kw):
        raise RuntimeError("persistence backend down")

    async def _boom_send(*_a, **_kw):
        raise RuntimeError("socket layer exploded")

    monkeypatch.setattr(server, "_persist_terminal_failure_card", _boom_persist)
    monkeypatch.setattr(server, "_send_error", _boom_send)

    async def fake_stream(*_args, **_kwargs):
        yield TextDeltaEvent(delta="partial text")
        raise ContextWindowExceededError(16384)

    with caplog.at_level(logging.ERROR, logger="grace2_agent.server"):
        # Must not raise out of the dispatch wrapper -- both failures are
        # caught + logged, never propagated.
        await _drive_real_stream(ws, state, fake_stream)

    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "terminal-failure card persist raised" in messages
    assert "error-envelope send raised" in messages
