"""WS-30s STORM FIX: the per-connection server DATA heartbeat.

ROOT CAUSE (live 2026-06-22): the browser ``WebSocket`` API handles server
PROTOCOL-level PING control-frames transparently and never surfaces them to
``onmessage``, so the agent's ``ping_interval=20`` pings do NOT reset the web
client's inbound-activity / pong-deadline timer (ws.ts ``noteInboundActivity``
fires only on a DATA frame). Between turns the only data frame the client sees is
its own keepalive's ``session-state`` reply; if that reply is slow/stalls the
client force-reconnects -> a ~30s reconnect storm in which the user's prompts
never reach the turn handler.

FIX (primary): ``server._heartbeat_loop`` sends a lightweight ``heartbeat`` DATA
frame every ``HEARTBEAT_INTERVAL_SECONDS`` on a fast server clock so the client's
inbound-activity timer is reset regardless of how slow the resume reply is. The
client tolerates an unknown ``heartbeat`` type (it routes to a no-op
``default:`` after already calling ``noteInboundActivity()`` on EVERY frame), so
no web change is required.

These tests pin:
  (a) the loop emits well-formed ``heartbeat`` envelopes on the interval;
  (b) ``cancel()`` stops the loop cleanly (raises CancelledError, sends no more);
  (c) a transient per-send wire error does NOT tear the loop down (it keeps
      ticking) -- so a single half-closed-socket hiccup cannot kill liveness.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grace2_agent import server

_SESSION = "01J0000000000000000000HBEAT"


class FakeWS:
    """Minimal WS stand-in mirroring the existing server tests' FakeWS."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, text: str) -> None:
        if self.closed:
            raise ConnectionError("socket closed")
        self.sent.append(text)


class FlakyWS(FakeWS):
    """Raises on the first N sends, then succeeds (transient wire hiccup)."""

    def __init__(self, fail_first: int) -> None:
        super().__init__()
        self._fail_first = fail_first
        self.attempts = 0

    async def send(self, text: str) -> None:
        self.attempts += 1
        if self.attempts <= self._fail_first:
            raise ConnectionError("transient half-closed send")
        self.sent.append(text)


@pytest.fixture(autouse=True)
def _fast_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the interval so the loop ticks quickly in the test."""
    monkeypatch.setattr(server, "HEARTBEAT_INTERVAL_SECONDS", 0.02)


@pytest.mark.asyncio
async def test_heartbeat_emits_well_formed_frames_on_interval() -> None:
    ws = FakeWS()
    task = asyncio.create_task(server._heartbeat_loop(ws, _SESSION))
    try:
        # Wait long enough for several intervals to fire.
        await asyncio.sleep(0.12)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(ws.sent) >= 3, f"expected >=3 heartbeats, got {len(ws.sent)}"
    env = json.loads(ws.sent[0])
    assert env["type"] == "heartbeat"
    assert env["session_id"] == _SESSION
    # A pure transport-liveness frame is never routed to a Case stream.
    assert env["case_id"] is None
    # Payload is an object (the client requires ``typeof payload === "object"``)
    # carrying only a server timestamp.
    assert isinstance(env["payload"], dict)
    assert "ts" in env["payload"]
    assert "id" in env and "ts" in env


@pytest.mark.asyncio
async def test_heartbeat_cancels_cleanly_and_stops_sending() -> None:
    ws = FakeWS()
    task = asyncio.create_task(server._heartbeat_loop(ws, _SESSION))
    await asyncio.sleep(0.05)
    sent_before = len(ws.sent)
    assert sent_before >= 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()

    # No frames are emitted after cancellation settles.
    await asyncio.sleep(0.05)
    assert len(ws.sent) == sent_before


@pytest.mark.asyncio
async def test_heartbeat_survives_transient_send_error() -> None:
    """A single failed send must NOT kill the loop -- it keeps ticking so one
    half-closed-socket hiccup cannot silently end transport liveness."""
    ws = FlakyWS(fail_first=2)
    task = asyncio.create_task(server._heartbeat_loop(ws, _SESSION))
    try:
        await asyncio.sleep(0.16)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    # The first two sends raised; subsequent ones succeeded -> at least one
    # frame landed AND the loop attempted more than the two failures.
    assert ws.attempts > 2
    assert len(ws.sent) >= 1
