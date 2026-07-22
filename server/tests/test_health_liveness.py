"""Liveness-probe tests for the agent-box auto-stop/wake infra.

The always-on agent EC2 box (t3.large, ``i-0251879a278df797f``) burns idle
money. A self-contained tofu root (``infra/aws-autostop/``) runs an idle-check
Lambda that polls ``GET /api/health`` and ``StopInstances`` after N consecutive
checks return ``busy == false``. STAGE 3 (sleep/wake): ``active_connections`` is
REPORTED but no longer gates ``busy`` -- an idle-but-open viewer no longer pins
the box. These tests pin the agent side of that contract so the auto-stop gate
is bulletproof:

  1. ``test_health_shape_at_rest`` — the ``/api/health`` body is exactly
     ``{"ok": True, "active_connections": 0, "busy": False}`` on a fresh box.
  2. ``test_connection_registry_register_deregister`` — a served socket bumps
     the count; deregister drops it; both are idempotent and never negative
     (a stuck/double call can NEVER drive the reported count negative).
  3. ``test_open_idle_connection_not_busy`` — STAGE 3: an attached but IDLE
     client (no turn, no solve) does NOT make the box busy; the count stays
     reported. A running turn/solve still pins the box (covered by 4 + 5).
  4. ``test_busy_when_solve_in_flight`` — a solver dispatch marker makes the
     box busy even with ZERO sockets (a detached solve must keep the box up);
     the release is clamped so a double-release can't read ``busy=false`` early.
  5. ``test_busy_when_inflight_turn_detached`` — a not-done detached turn in
     ``_SESSION_LIVE_TURNS`` keeps ``busy`` true even with zero sockets and zero
     solve markers; a done turn does not.
  6. ``test_health_route_reflects_live_state`` — the HTTP route serves the live
     snapshot (connection + solve), not a static ``{"ok":true}``.
  7. ``test_health_route_busy_on_snapshot_error`` — if the snapshot raises, the
     probe fails CONSERVATIVE (busy=true, non-zero count) so a transient glitch
     can never get a live box stopped.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grace2_agent import server, tool_catalog_http


class _FakeWS:
    """Stand-in WebSocket — only ``id(self)`` matters to the registry."""


class _FakeReader:
    def __init__(self, request: bytes):
        self._buf = [ln + b"\r\n" for ln in request.split(b"\r\n")]

    async def readline(self):
        return self._buf.pop(0) if self._buf else b""


class _FakeWriter:
    def __init__(self):
        self.buffer = bytearray()
        self.closed = False

    def write(self, data: bytes):
        self.buffer.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.closed = True


def _health_request() -> bytes:
    return b"GET /api/health HTTP/1.1\r\nHost: agent.local\r\n\r\n"


def _serve_health() -> dict:
    """Drive ``_handle_http`` for ``GET /api/health`` and parse the JSON body."""
    reader = _FakeReader(_health_request())
    writer = _FakeWriter()
    asyncio.run(tool_catalog_http._handle_http(reader, writer))
    out = bytes(writer.buffer)
    assert b"200 OK" in out, out
    body = out.split(b"\r\n\r\n", 1)[1]
    return json.loads(body)


@pytest.fixture(autouse=True)
def _reset_liveness():
    """Each test starts from a clean liveness state and leaves it clean.

    The signals are module-level singletons (one asyncio loop, one process);
    reset them around every test so order-independence holds and a leaked marker
    in one test cannot mask a regression in another.
    """
    server._ACTIVE_WS_CONNECTIONS.clear()
    server._SESSION_LIVE_TURNS.clear()
    server._SOLVE_IN_FLIGHT = 0
    yield
    server._ACTIVE_WS_CONNECTIONS.clear()
    server._SESSION_LIVE_TURNS.clear()
    server._SOLVE_IN_FLIGHT = 0


def test_health_shape_at_rest():
    assert server.liveness_snapshot() == {
        "ok": True,
        "active_connections": 0,
        "busy": False,
    }
    assert server.active_connection_count() == 0
    assert server.is_busy() is False


def test_connection_registry_register_deregister():
    ws = _FakeWS()
    server._register_active_connection(ws)
    assert server.active_connection_count() == 1
    # Idempotent: re-register is a no-op (set semantics).
    server._register_active_connection(ws)
    assert server.active_connection_count() == 1

    server._deregister_active_connection(ws)
    assert server.active_connection_count() == 0
    # Double-deregister can NEVER drive the count negative — the gate must not
    # read "idle" because of a defensive double call.
    server._deregister_active_connection(ws)
    assert server.active_connection_count() == 0


def test_open_idle_connection_not_busy():
    ws = _FakeWS()
    server._register_active_connection(ws)
    # STAGE 3 (sleep/wake): a live but IDLE tab (no in-flight turn, no solve) is
    # NOT busy -- an idle viewer must not pin the box. The count is REPORTED.
    assert server.is_busy() is False
    assert server.liveness_snapshot() == {
        "ok": True,
        "active_connections": 1,
        "busy": False,
    }
    server._deregister_active_connection(ws)
    assert server.is_busy() is False


def test_busy_when_solve_in_flight():
    # Zero sockets, but a solver dispatch is running → busy (a detached SFINCS /
    # MODFLOW solve must keep the box alive).
    assert server.active_connection_count() == 0
    server._solve_started()
    assert server.solve_in_flight_count() == 1
    assert server.is_busy() is True
    snap = server.liveness_snapshot()
    assert snap["active_connections"] == 0
    assert snap["busy"] is True

    server._solve_finished()
    assert server.solve_in_flight_count() == 0
    assert server.is_busy() is False
    # Clamp: a double-release cannot read busy=false "early" by going negative.
    server._solve_finished()
    assert server.solve_in_flight_count() == 0


def test_busy_when_inflight_turn_detached():
    # A not-done detached turn in the live-turn registry keeps the box busy with
    # zero sockets + zero solve markers (the disconnect-survived-solve path).
    async def _never():
        await asyncio.Event().wait()

    async def _drive():
        task = asyncio.ensure_future(_never())
        await asyncio.sleep(0)  # let it start
        server._register_live_turn("sess-A", "turn-1", task, emitter=None)
        try:
            assert server.inflight_turn_count() == 1
            assert server.is_busy() is True
            assert server.liveness_snapshot()["busy"] is True
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # A done/cancelled task is not counted (its self-removing callback runs).
        await asyncio.sleep(0)
        assert server.inflight_turn_count() == 0
        assert server.is_busy() is False

    asyncio.run(_drive())


def test_health_route_reflects_live_state():
    # At rest the route serves the zero-state snapshot, NOT a static {"ok":true}.
    assert _serve_health() == {
        "ok": True,
        "active_connections": 0,
        "busy": False,
    }
    # STAGE 3: an idle open connection alone is REPORTED but is NOT busy on the
    # route (an idle viewer must not pin the box).
    ws = _FakeWS()
    server._register_active_connection(ws)
    try:
        assert _serve_health() == {
            "ok": True,
            "active_connections": 1,
            "busy": False,
        }
        # An in-flight solve flips busy true even while the connection is open.
        server._solve_started()
        try:
            assert _serve_health() == {
                "ok": True,
                "active_connections": 1,
                "busy": True,
            }
        finally:
            server._solve_finished()
    finally:
        server._deregister_active_connection(ws)


def test_health_route_busy_on_snapshot_error(monkeypatch):
    # If the snapshot raises (defensive), the probe must fail CONSERVATIVE so a
    # transient glitch never tricks the auto-stop gate into stopping a live box.
    def _boom():
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(server, "liveness_snapshot", _boom)
    parsed = _serve_health()
    assert parsed["ok"] is True
    assert parsed["busy"] is True
    assert parsed["active_connections"] >= 1
