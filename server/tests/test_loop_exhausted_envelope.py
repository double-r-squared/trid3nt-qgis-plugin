"""Loop-exhausted envelope tests (job-B9, Wave 4.10 Stage 3).

Covers:
    1. ``_send_loop_exhausted`` emits a JSON envelope with type="loop_exhausted"
       and the expected payload shape.
    2. ``error_code="MAX_ITERATIONS_REACHED"`` is present in the payload.
    3. ``retryable=False`` in the payload.
    4. The message references the iteration limit.
    5. End-to-end: a multi-turn loop that always emits tool calls (never
       terminates naturally) hits MAX_TURN_ITERATIONS and emits the
       loop_exhausted envelope — NOT a generic error envelope.
    6. After loop_exhausted, the terminal agent-message-chunk (done=True) is
       still sent so the client doesn't hang waiting for the stream to close.
    7. ``_send_loop_exhausted`` is safe to call even if the socket send fails
       (best-effort, never raises).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from grace2_agent.adapter import (
    GeminiSettings,
    MAX_TURN_ITERATIONS,
)
from grace2_agent.server import _send_loop_exhausted, SessionState
from grace2_contracts import new_ulid


# ---------------------------------------------------------------------------
# Minimal socket helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeSocket:
    """Captures all sent messages as parsed JSON dicts (or raw strings on decode fail)."""
    sent: list = field(default_factory=list)

    async def send(self, msg: str) -> None:
        try:
            self.sent.append(json.loads(msg))
        except (json.JSONDecodeError, TypeError):
            self.sent.append(msg)


@dataclass
class _BrokenSocket:
    """Always raises on send — used to test best-effort handling."""
    async def send(self, _msg: str) -> None:
        raise OSError("connection reset")


# ---------------------------------------------------------------------------
# Test 1-4: _send_loop_exhausted envelope shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_loop_exhausted_type():
    """Emitted envelope has type='loop_exhausted'."""
    sock = _FakeSocket()
    await _send_loop_exhausted(sock, "session-123")
    assert len(sock.sent) == 1
    assert sock.sent[0]["type"] == "loop_exhausted"


@pytest.mark.asyncio
async def test_send_loop_exhausted_session_id():
    """Emitted envelope carries the correct session_id."""
    sock = _FakeSocket()
    sid = new_ulid()
    await _send_loop_exhausted(sock, sid)
    assert sock.sent[0]["session_id"] == sid


@pytest.mark.asyncio
async def test_send_loop_exhausted_error_code():
    """Payload error_code is 'MAX_ITERATIONS_REACHED'."""
    sock = _FakeSocket()
    await _send_loop_exhausted(sock, "s1")
    payload = sock.sent[0]["payload"]
    assert payload["error_code"] == "MAX_ITERATIONS_REACHED"


@pytest.mark.asyncio
async def test_send_loop_exhausted_retryable_false():
    """Payload retryable is False."""
    sock = _FakeSocket()
    await _send_loop_exhausted(sock, "s1")
    payload = sock.sent[0]["payload"]
    assert payload["retryable"] is False


@pytest.mark.asyncio
async def test_send_loop_exhausted_message_references_limit():
    """Message mentions the MAX_TURN_ITERATIONS limit."""
    sock = _FakeSocket()
    await _send_loop_exhausted(sock, "s1")
    payload = sock.sent[0]["payload"]
    assert str(MAX_TURN_ITERATIONS) in payload["message"]


@pytest.mark.asyncio
async def test_send_loop_exhausted_status_field():
    """Payload has status='loop_exhausted'."""
    sock = _FakeSocket()
    await _send_loop_exhausted(sock, "s1")
    payload = sock.sent[0]["payload"]
    assert payload["status"] == "loop_exhausted"


@pytest.mark.asyncio
async def test_send_loop_exhausted_is_not_error_envelope():
    """The envelope type is 'loop_exhausted', NOT 'error'.

    The web UI distinguishes 'agent ran out of steps' from
    'Gemini API unavailable' using this type discriminator.
    """
    sock = _FakeSocket()
    await _send_loop_exhausted(sock, "s1")
    assert sock.sent[0]["type"] != "error"


# ---------------------------------------------------------------------------
# Test 7: _send_loop_exhausted is best-effort (never raises)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_loop_exhausted_best_effort_on_broken_socket():
    """_send_loop_exhausted must not raise even if the socket send fails."""
    broken = _BrokenSocket()
    # Must complete without raising.
    try:
        await _send_loop_exhausted(broken, "s1")
    except Exception as exc:
        pytest.fail(
            f"_send_loop_exhausted raised on broken socket: {type(exc).__name__}: {exc}"
        )


# ---------------------------------------------------------------------------
# Fake chunk helpers for end-to-end tests
# ---------------------------------------------------------------------------


def _make_fake_chunk_with_function_call(name: str, args: dict, call_id: str):
    fn_call = MagicMock()
    fn_call.name = name
    fn_call.id = call_id
    fn_call.args = args
    fake_part = MagicMock()
    fake_part.function_call = fn_call
    fake_part.text = None
    fake_content = MagicMock()
    fake_content.parts = [fake_part]
    fake_candidate = MagicMock()
    fake_candidate.content = fake_content
    fake_chunk = MagicMock()
    fake_chunk.candidates = [fake_candidate]
    fake_chunk.text = None
    return fake_chunk


# ---------------------------------------------------------------------------
# Test 5: end-to-end — loop exhaustion emits loop_exhausted envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_gemini_reply_emits_loop_exhausted_on_cap():
    """A loop that always requests tool calls hits the cap and emits loop_exhausted.

    Gemini is mocked to always emit one function_call per turn; the tool is
    mocked to always return a result; eventually MAX_TURN_ITERATIONS is hit
    and the distinct 'loop_exhausted' envelope must appear on the wire.
    """
    from grace2_agent import server as agent_server

    # Always-looping generator: each call → another function_call chunk.
    def _always_loop(**kwargs):
        i = kwargs.get("_iter", 0)  # not a real kwarg, just documentation
        # Infinite supply: return a function_call every turn.
        return iter([
            _make_fake_chunk_with_function_call(
                "fetch_dem", {"bbox": [0, 0, 1, 1]}, f"call-{id(kwargs)}"
            )
        ])

    call_count = {"n": 0}
    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **kw: _always_loop(**kw)

    async def _always_succeed(_ws, _state, name, args):
        call_count["n"] += 1
        return {"wms_url": "http://example.com", "layer_id": "dem-x"}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_always_succeed), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "never terminates", "research"
        )

    # The loop must have reached the cap.
    assert call_count["n"] == MAX_TURN_ITERATIONS, (
        f"Expected exactly {MAX_TURN_ITERATIONS} tool calls at cap; "
        f"got {call_count['n']}"
    )

    # A 'loop_exhausted' envelope must appear.
    exhausted = [m for m in sock.sent if m.get("type") == "loop_exhausted"]
    assert exhausted, (
        f"Expected a 'loop_exhausted' envelope; "
        f"got types: {[m.get('type') for m in sock.sent]!r}"
    )
    payload = exhausted[0]["payload"]
    assert payload["error_code"] == "MAX_ITERATIONS_REACHED"
    assert payload["retryable"] is False
    assert str(MAX_TURN_ITERATIONS) in payload["message"]


# ---------------------------------------------------------------------------
# Test 6: terminal agent-message-chunk (done=True) still fires after loop_exhausted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_gemini_reply_terminal_chunk_after_loop_exhausted():
    """After loop_exhausted, the terminal agent-message-chunk (done=True) is emitted.

    The client waits for done=True to close the stream. If the loop exits via
    the cap-hit path without emitting it, the client hangs indefinitely.
    """
    from grace2_agent import server as agent_server

    fake_client = MagicMock()
    fake_client.models.generate_content_stream.side_effect = lambda **kw: iter([
        _make_fake_chunk_with_function_call("fetch_dem", {"bbox": [0, 0, 1, 1]}, "c1")
    ])

    async def _succeed(_ws, _state, name, args):
        return {"wms_url": "http://example.com", "layer_id": "x"}

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )

    with patch.object(agent_server, "build_client", return_value=fake_client), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_succeed), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, settings, "x", "research"
        )

    # Find the terminal agent-message-chunk (done=True).
    terminal_chunks = [
        m for m in sock.sent
        if m.get("type") == "agent-message-chunk" and m.get("payload", {}).get("done") is True
    ]
    assert terminal_chunks, (
        "Expected a terminal agent-message-chunk(done=True) after loop exhausted; "
        f"sent types: {[m.get('type') for m in sock.sent]!r}"
    )
