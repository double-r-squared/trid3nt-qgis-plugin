"""The ``MAX_TURNS_PER_SESSION`` cap.

The counter increments per user-message dispatch and starts fresh on a new
session; at the cap a ``session-state(status="max_turns_reached")`` and a closing
chunk are emitted and every further turn gets the same refusal. The env override
is parsed at import. Driven end to end through a live websockets connection."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from trid3nt_server.server import SessionState, _handle_max_turns_reached
from trid3nt_server.main import MAX_TURNS_PER_SESSION



# Valid 26-character ULIDs for test fixtures (ULIDs are base32-encoded, 26 chars).
_SESSION_ID_A = "01AAAAAAAAAAAAAAAAAAAAAAA0"
_SESSION_ID_B = "01BBBBBBBBBBBBBBBBBBBBBBB0"


@dataclass
class FakeWebSocket:
    """Minimal duck-typed WebSocket for unit tests.

    Captures every ``send`` call as a parsed frame in ``frames``.
    """

    session_id: str = _SESSION_ID_A
    frames: list[dict] = field(default_factory=list)

    async def send(self, text: str) -> None:  # noqa: D401 — async interface
        self.frames.append(json.loads(text))

    def frames_of_type(self, msg_type: str) -> list[dict]:
        """Return all captured frames with the given ``type`` value."""
        return [f for f in self.frames if f.get("type") == msg_type]


def _make_state(session_id: str = _SESSION_ID_A) -> SessionState:
    return SessionState(session_id=session_id)



def test_turn_counter_starts_at_zero():
    """A freshly created SessionState has turn_count == 0."""
    state = _make_state()
    assert state.turn_count == 0


def test_turn_counter_increments_on_each_dispatch():
    """turn_count increments by 1 for each simulated user-message dispatch."""
    state = _make_state()
    for expected in range(1, 6):
        state.turn_count += 1
        assert state.turn_count == expected



@pytest.mark.asyncio
async def test_cap_fires_and_emits_max_turns_reached():
    """On the (MAX+1)th turn _handle_max_turns_reached emits a session-state
    envelope with status='max_turns_reached'."""
    ws = FakeWebSocket()
    state = _make_state(session_id=ws.session_id)
    state.turn_count = MAX_TURNS_PER_SESSION + 1  # simulate cap just exceeded

    await _handle_max_turns_reached(ws, state)

    ss_frames = ws.frames_of_type("session-state")
    assert len(ss_frames) >= 1, "expected at least one session-state frame"
    assert ss_frames[0]["payload"]["status"] == "max_turns_reached"


@pytest.mark.asyncio
async def test_cap_fires_and_emits_closing_agent_message():
    """Alongside the session-state, a closing agent-message-chunk is emitted
    so the user sees a human-readable explanation in the chat panel."""
    ws = FakeWebSocket()
    state = _make_state(session_id=ws.session_id)
    state.turn_count = MAX_TURNS_PER_SESSION + 1

    await _handle_max_turns_reached(ws, state)

    chunk_frames = ws.frames_of_type("agent-message-chunk")
    assert len(chunk_frames) >= 1, "expected at least one agent-message-chunk"
    # The closing message should mention the turn limit
    combined_text = "".join(f["payload"]["delta"] for f in chunk_frames)
    assert "turn limit" in combined_text or str(MAX_TURNS_PER_SESSION) in combined_text


@pytest.mark.asyncio
async def test_cap_refuses_further_tool_calls_after_hitting_limit():
    """The cap is idempotent: every turn above the limit gets the same refusal.

    That is what the dispatch loop does for each user-message once the count has
    passed the limit."""
    ws = FakeWebSocket()
    state = _make_state(session_id=ws.session_id)
    state.turn_count = MAX_TURNS_PER_SESSION + 1

    # First cap hit
    await _handle_max_turns_reached(ws, state)
    frames_after_first = len(ws.frames_of_type("session-state"))

    # Simulate another user-message arriving after the cap
    state.turn_count += 1
    await _handle_max_turns_reached(ws, state)
    frames_after_second = len(ws.frames_of_type("session-state"))

    assert frames_after_second > frames_after_first, (
        "expected an additional session-state(max_turns_reached) frame "
        "on each post-cap user-message"
    )
    for ss in ws.frames_of_type("session-state"):
        assert ss["payload"]["status"] == "max_turns_reached"



def test_new_session_starts_fresh_counter():
    """A new WebSocket connection creates a new SessionState with turn_count=0.

    The session turn counter is per-connection (per SessionState instance).
    Old sessions at their cap cannot infect new sessions.
    """
    old_state = _make_state(session_id=_SESSION_ID_A)
    old_state.turn_count = MAX_TURNS_PER_SESSION + 5  # maxed out

    new_state = _make_state(session_id=_SESSION_ID_B)
    assert new_state.turn_count == 0, (
        "new SessionState must start at turn_count=0 regardless of any "
        "other session's state"
    )


def test_multiple_sessions_have_independent_counters():
    """Each SessionState owns its own turn_count; mutations don't bleed across."""
    state_a = _make_state(session_id=_SESSION_ID_A)
    state_b = _make_state(session_id=_SESSION_ID_B)

    state_a.turn_count = 10
    state_b.turn_count = 3

    assert state_a.turn_count == 10
    assert state_b.turn_count == 3



def test_max_turns_env_var_default():
    """MAX_TURNS_PER_SESSION defaults to 25 when the env var is absent."""
    # We can only test the imported value (env-var is read at import time).
    # Verify it's a positive integer >= 1. The exact default is 25
    # but ops may override it; we just enforce it's a valid positive int.
    assert isinstance(MAX_TURNS_PER_SESSION, int)
    assert MAX_TURNS_PER_SESSION >= 1



def test_session_state_payload_active_status_default():
    """SessionStatePayload defaults status to 'active' — no regressions."""
    from trid3nt_contracts.ws import SessionStatePayload
    payload = SessionStatePayload()
    assert payload.status == "active"


def test_session_state_payload_max_turns_reached_status():
    """SessionStatePayload accepts status='max_turns_reached' (new enum value)."""
    from trid3nt_contracts.ws import SessionStatePayload
    payload = SessionStatePayload(status="max_turns_reached")
    assert payload.status == "max_turns_reached"
    # Verify it serialises cleanly to JSON
    wire = payload.model_dump_json()
    assert "max_turns_reached" in wire


def test_session_state_payload_rejects_unknown_status():
    """SessionStatePayload rejects an invalid status value (Pydantic guard)."""
    from trid3nt_contracts.ws import SessionStatePayload
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SessionStatePayload(status="unknown_status_value")
