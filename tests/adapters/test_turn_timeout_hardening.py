"""A failed model call must TERMINATE the turn, never wedge the loop.

An unbounded provider call that hangs leaves the producer thread stuck, the turn
coroutine unfinished and its live-turn entry pinned, so no model can respond.
Each adapter bounds its own call; this pins the SERVER side provider-neutrally,
an honest ``LLM_UNAVAILABLE`` and a drained registry. The solve path is unbounded."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from trid3nt_server.adapters.adapter import TextDeltaEvent


def _hung_call_error() -> TimeoutError:
    """What a bounded provider client raises once its read timeout fires."""
    return TimeoutError("read timeout on the provider endpoint")


# Server-level: a failed model call does NOT pin busy / wedge the loop.
# THIS IS THE LOAD-BEARING ASSERT for the live-down fix.


@dataclass
class _FakeSocket:
    """Minimal WebSocket shim that records every ``send`` payload."""

    sent: list[str] = field(default_factory=list)

    async def send(self, msg: str) -> None:  # noqa: D401 - protocol shim
        self.sent.append(msg)


def _make_text_stream(*chunks: str):
    """Build an async ``stream_events_with_contents`` stand-in yielding text."""

    async def _fake_stream(*_args: Any, **_kwargs: Any):
        for c in chunks:
            yield TextDeltaEvent(delta=c)

    return _fake_stream


def _make_raising_stream(exc: BaseException):
    """Build an async ``stream_events_with_contents`` stand-in that RAISES.

    Mirrors a hung-then-timed-out provider call surfacing through the adapter
    as a re-raised timeout."""

    async def _fake_stream(*_args: Any, **_kwargs: Any):
        if False:  # pragma: no cover - make this an async generator
            yield TextDeltaEvent(delta="")
        raise exc

    return _fake_stream


@pytest.mark.asyncio
async def test_failed_model_call_clears_busy_and_surfaces_error():
    """A timed-out model turn terminates, surfaces the error and clears the turn.

    The task is registered as a detached live turn, so a turn that never finished
    would pin the in-flight count; after it completes that count must be zero."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_contracts import new_ulid

    settings = agent_server.ModelSettings(
        model="test-model", project="test", location="local", use_vertex=False
    )
    state = SessionState(session_id=new_ulid())
    sock = _FakeSocket()

    raising = _make_raising_stream(_hung_call_error())

    # Sanity precondition: no live turn before this one.
    assert agent_server.inflight_turn_count() == 0

    with patch.object(agent_server, "stream_events_with_contents", raising), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        # Launch the turn as a TASK and register it as a detached live turn --
        # this is the exact path that pins the registry if the turn never completes.
        task = asyncio.ensure_future(
            agent_server._stream_model_reply(
                sock, state, settings, "switch to Nova and run it", "research"
            )
        )
        agent_server._register_live_turn(
            state.session_id, "_test_turn", task, None
        )
        # While running, the detached turn is in flight.
        assert agent_server.inflight_turn_count() >= 1

        await task  # _stream_model_reply swallows the error internally
        # Let the task's done-callback (_drop) run so the registry empties.
        await asyncio.sleep(0)

    # (a) An honest error envelope reached the wire (not a silent death).
    import json

    error_frames = [
        json.loads(m) for m in sock.sent if '"type": "error"' in m or "error" in m
    ]
    llm_errors = [
        f
        for f in error_frames
        if f.get("payload", {}).get("error_code") == "LLM_UNAVAILABLE"
    ]
    assert llm_errors, f"no LLM_UNAVAILABLE error envelope on wire: {sock.sent}"

    # (b) THE LOAD-BEARING ASSERT: the failed model call did NOT pin the turn.
    assert task.done()
    assert agent_server.inflight_turn_count() == 0, (
        "detached turn entry NOT cleared after a failed model call "
        "(the live-down wedge)"
    )


@pytest.mark.asyncio
async def test_normal_turn_path_unaffected():
    """A healthy text turn still completes + clears busy (no regression)."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_contracts import new_ulid

    settings = agent_server.ModelSettings(
        model="test-model", project="test", location="local", use_vertex=False
    )
    state = SessionState(session_id=new_ulid())
    sock = _FakeSocket()

    ok_stream = _make_text_stream("All ", "set.")

    with patch.object(agent_server, "stream_events_with_contents", ok_stream), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        task = asyncio.ensure_future(
            agent_server._stream_model_reply(
                sock, state, settings, "hello", "research"
            )
        )
        agent_server._register_live_turn(
            state.session_id, "_test_turn_ok", task, None
        )
        await task
        await asyncio.sleep(0)

    import json

    chunks = [json.loads(m) for m in sock.sent if "agent-message-chunk" in m]
    text = "".join(
        c["payload"]["delta"] for c in chunks if c["payload"].get("delta")
    )
    assert "All set." in text
    # No error envelope on the happy path.
    assert not any('"error_code": "LLM_UNAVAILABLE"' in m for m in sock.sent)
    # Turn registry cleared after a normal turn too.
    assert agent_server.inflight_turn_count() == 0


@pytest.mark.asyncio
async def test_solve_tool_path_unaffected_by_model_bound():
    """A tool-bearing turn dispatches the tool and completes cleanly.

    The model read-timeout bound does not reach into the tool dispatch path."""
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState
    from trid3nt_server.adapters.adapter import FunctionCallEvent
    from trid3nt_contracts import new_ulid

    settings = agent_server.ModelSettings(
        model="test-model", project="test", location="local", use_vertex=False
    )
    state = SessionState(session_id=new_ulid())
    sock = _FakeSocket()

    # Turn 1 emits a function_call; turn 2 narrates and ends.
    turn_events = iter(
        [
            [FunctionCallEvent(name="geocode_location", call_id="c1", args={"query": "X"})],
            [TextDeltaEvent(delta="Done.")],
        ]
    )

    async def _fake_stream(*_args: Any, **_kwargs: Any):
        for ev in next(turn_events):
            yield ev

    dispatched: list[str] = []

    async def _fake_invoke(_ws, _state, name, args):
        dispatched.append(name)
        return {"name": "X", "bbox": [0, 0, 1, 1], "precision_class": "precise"}

    with patch.object(agent_server, "stream_events_with_contents", _fake_stream), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        task = asyncio.ensure_future(
            agent_server._stream_model_reply(
                sock, state, settings, "where is X", "research"
            )
        )
        agent_server._register_live_turn(
            state.session_id, "_test_turn_tool", task, None
        )
        await task
        await asyncio.sleep(0)

    assert dispatched == ["geocode_location"]
    # The tool turn ran to a clean terminal: the live-turn registry clears.
    assert agent_server.inflight_turn_count() == 0
