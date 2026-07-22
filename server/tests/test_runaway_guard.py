"""Runaway-agent guard (#186): per-turn step cap + wall-clock + loop watchdog.

Pins the guard that stops a single hung / runaway session from pegging the
shared event loop and starving other users (live incident 2026-06-25). Two
layers:

  * unit tests for the small ``runaway_guard`` module (thresholds, cheap-model
    halving, the ``LoopWatchdog`` state machine, honest abort messages);
  * integration tests that drive ``server._stream_gemini_reply`` and assert the
    WALL-CLOCK and STEP-CAP guards each fire a clean honest abort -- while a
    normal short turn is left untouched.

Run:
    cd services/agent && .venv/bin/python -m pytest tests/test_runaway_guard.py -q
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server.runaway_guard import (
    ABORT_LOOP_WATCHDOG,
    ABORT_STEP_CAP,
    ABORT_WALL_CLOCK,
    LoopWatchdog,
    abort_message,
    is_cheap_model,
    max_agent_steps,
    max_turn_seconds,
    step_cap_for_model,
)

# --------------------------------------------------------------------------- #
# Module unit tests (pure, deterministic)
# --------------------------------------------------------------------------- #


def test_abort_messages_are_honest_and_distinct():
    for code in (ABORT_STEP_CAP, ABORT_WALL_CLOCK, ABORT_LOOP_WATCHDOG):
        msg = abort_message(code)
        assert isinstance(msg, str) and msg
        assert "stopping to protect" in msg.lower() or "stopped" in msg.lower()
    # An unknown code still returns a safe honest sentence (never a KeyError).
    assert abort_message("WHATEVER") == "Agent stopped to protect the session."


def test_defaults_and_env_overrides(monkeypatch):
    monkeypatch.delenv("TRID3NT_MAX_AGENT_STEPS", raising=False)
    monkeypatch.delenv("TRID3NT_MAX_TURN_SECONDS", raising=False)
    assert max_agent_steps() == 30
    assert max_turn_seconds() == 420.0
    # Overrides take.
    monkeypatch.setenv("TRID3NT_MAX_AGENT_STEPS", "12")
    monkeypatch.setenv("TRID3NT_MAX_TURN_SECONDS", "900")
    assert max_agent_steps() == 12
    assert max_turn_seconds() == 900.0
    # Garbage / non-positive falls back to the safe default (never 0 / None).
    monkeypatch.setenv("TRID3NT_MAX_AGENT_STEPS", "not-a-number")
    monkeypatch.setenv("TRID3NT_MAX_TURN_SECONDS", "0")
    assert max_agent_steps() == 30
    assert max_turn_seconds() == 420.0


def test_cheap_model_gets_tighter_step_cap(monkeypatch):
    monkeypatch.delenv("TRID3NT_MAX_AGENT_STEPS", raising=False)
    # Full-tier (Sonnet / default None) keeps the full cap.
    assert is_cheap_model("anthropic.claude-3-5-sonnet") is False
    assert step_cap_for_model("anthropic.claude-3-5-sonnet") == max_agent_steps()
    assert step_cap_for_model(None) == max_agent_steps()
    # Cheap / loop-prone tiers (Nova, Haiku) get HALF (floored), never above full.
    for cheap in ("amazon.nova-lite-v1", "anthropic.claude-3-haiku"):
        assert is_cheap_model(cheap) is True
        cap = step_cap_for_model(cheap)
        assert cap < max_agent_steps()
        assert cap >= 6  # _CHEAP_STEP_FLOOR -- a real short chain still fits


def test_loop_watchdog_trips_on_identical_repeats():
    wd = LoopWatchdog(threshold=3)
    sig = [("fetch_dem", "h1")]
    assert wd.record_round(sig) is None  # 1
    assert wd.record_round(sig) is None  # 2
    assert wd.record_round(sig) == ABORT_LOOP_WATCHDOG  # 3 -> trip
    assert wd.tripped() == ABORT_LOOP_WATCHDOG


def test_loop_watchdog_resets_on_progress_and_variation():
    wd = LoopWatchdog(threshold=3)
    sig = [("fetch_dem", "h1")]
    wd.record_round(sig)
    wd.record_round(sig)
    # A producing round (made_progress) resets the no-progress streak.
    assert wd.record_round(sig, made_progress=True) is None
    assert wd.tripped() is None
    # A DIFFERENT signature also resets.
    wd.record_round(sig)
    assert wd.record_round([("publish_layer", "h2")]) is None
    # A text-only (empty calls) round resets -- narration is progress.
    wd.record_round(sig)
    assert wd.record_round([]) is None
    assert wd.tripped() is None


# --------------------------------------------------------------------------- #
# Integration: the guards fire inside the per-turn driver
# --------------------------------------------------------------------------- #


@dataclass
class _FakeSocket:
    sent: list[str] = field(default_factory=list)

    async def send(self, msg: str) -> None:  # noqa: D401 - protocol shim
        self.sent.append(msg)


def _fake_call_chunk(name: str, args: dict, call_id: str):
    fn_call = MagicMock()
    fn_call.name = name
    fn_call.id = call_id
    fn_call.args = args
    part = MagicMock()
    part.function_call = fn_call
    part.text = None
    content = MagicMock()
    content.parts = [part]
    cand = MagicMock()
    cand.content = content
    chunk = MagicMock()
    chunk.candidates = [cand]
    chunk.text = None
    return chunk


def _abort_codes(sock: _FakeSocket) -> list[str]:
    out: list[str] = []
    for m in sock.sent:
        try:
            d = json.loads(m)
        except (ValueError, TypeError):
            continue
        if d.get("type") == "loop_exhausted":
            code = d.get("payload", {}).get("error_code")
            if code:
                out.append(code)
    return out


def _settings():
    from trid3nt_server.server import GeminiSettings

    return GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )


@pytest.mark.asyncio
async def test_wall_clock_guard_aborts_a_slow_turn(monkeypatch):
    """A turn that overruns its wall-clock budget aborts with AGENT_TURN_TIMEOUT.

    Round 1 dispatches normally (proving the in-flight round is NOT killed
    mid-await -- e.g. a legitimate long Batch poll completes); the deadline is
    only re-checked at the TOP of round 2, where it fires. The session stays
    alive (a terminal frame is emitted) -- the loop is freed, not crashed."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_contracts import new_ulid

    def _infinite_calls():
        i = 0
        while True:
            i += 1
            yield iter([_fake_call_chunk("fetch_dem", {"bbox": [0, 0, 1, 1]}, f"c{i}")])

    chunks = _infinite_calls()
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **_: next(chunks)

    dispatched = 0

    async def _slow_invoke(_ws, _state, name, args):
        nonlocal dispatched
        dispatched += 1
        # The dispatched round takes longer than the (tiny) wall-clock budget --
        # like a long poll. It is NOT interrupted mid-flight; the deadline bites
        # at the TOP of the next round.
        await asyncio.sleep(0.1)
        return {"ok": True}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_slow_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]), \
         patch.object(agent_server, "max_turn_seconds", return_value=0.05):
        await agent_server._stream_gemini_reply(sock, state, _settings(), "x", "research")

    # Round 1 fully ran (the in-flight await was not killed); the wall-clock then
    # aborted before a runaway could continue.
    assert dispatched == 1
    assert ABORT_WALL_CLOCK in _abort_codes(sock)
    # Busy released -- the loop is free, the process did not crash.
    assert agent_server.inflight_turn_count() == 0


@pytest.mark.asyncio
async def test_step_cap_guard_aborts_a_varied_runaway(monkeypatch):
    """A varied-tool runaway (watchdog never trips) is fail-stopped at the step cap.

    With the cap TIGHTENED below MAX_TURN_ITERATIONS (the cheap-tier shape) the
    abort is the distinct AGENT_STEP_LIMIT_REACHED code."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_contracts import new_ulid

    def _varied_calls():
        i = 0
        while True:
            i += 1
            # A different tool+args each round so the loop watchdog never trips
            # -- only the STEP CAP can stop this runaway.
            yield iter([_fake_call_chunk("fetch_dem", {"n": i}, f"c{i}")])

    chunks = _varied_calls()
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **_: next(chunks)

    dispatched = 0

    async def _invoke(_ws, _state, name, args):
        nonlocal dispatched
        dispatched += 1
        return {"ok": True}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]), \
         patch.object(agent_server, "step_cap_for_model", return_value=3):
        await agent_server._stream_gemini_reply(sock, state, _settings(), "x", "research")

    # Stopped at the (tightened) step cap, not before, not infinitely.
    assert dispatched == 3
    assert ABORT_STEP_CAP in _abort_codes(sock)
    assert agent_server.inflight_turn_count() == 0


@pytest.mark.asyncio
async def test_normal_turn_not_aborted_by_guards(monkeypatch):
    """A normal short turn (one tool, then narrate) is NOT touched by any guard."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_server.adapter import FunctionCallEvent, TextDeltaEvent
    from trid3nt_contracts import new_ulid

    # Turn 1: one function call; turn 2: narrate + end.
    rounds = iter([
        [FunctionCallEvent(name="geocode_location", call_id="c1", args={"q": "X"})],
        [TextDeltaEvent(delta="All done.")],
    ])

    async def _fake_stream(*_a, **_k):
        for ev in next(rounds):
            yield ev

    async def _invoke(_ws, _state, name, args):
        return {"name": "X", "bbox": [0, 0, 1, 1], "precision_class": "precise"}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())

    with patch.object(agent_server, "stream_events_with_contents", _fake_stream), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]), \
         patch.object(agent_server, "build_client", return_value=None):
        await agent_server._stream_gemini_reply(sock, state, _settings(), "where is X", "research")

    # No guard abort on the wire; the narration landed.
    assert _abort_codes(sock) == []
    text = "".join(
        json.loads(m)["payload"].get("delta", "")
        for m in sock.sent
        if "agent-message-chunk" in m
    )
    assert "All done." in text
    assert agent_server.inflight_turn_count() == 0
