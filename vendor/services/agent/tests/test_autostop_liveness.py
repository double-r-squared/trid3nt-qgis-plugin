"""Liveness-signal tests for the agent-box auto-stop/wake infra.

The ``infra/aws-autostop`` idle-check Lambda polls ``GET /api/health`` and reads
``active_connections`` + ``busy`` to decide whether the always-on agent EC2 box
may be stopped. These tests lock the three contracts that decision depends on:

  1. ``liveness_snapshot`` returns the exact shape the Lambda parses
     (``{"ok": True, "active_connections": int, "busy": bool}``) and is idle at
     rest.
  2. The connection registry register/deregister pair tracks live sockets, never
     goes negative (defensive double-deregister), and stays REPORTED in
     ``active_connections`` -- but, as of STAGE 3 (sleep/wake), an idle-but-open
     connection NO LONGER makes the box ``busy`` (an idle viewer must not pin
     the box forever).
  3. ``busy`` is the OR of: a detached in-flight turn and a solver dispatch in
     flight -- so the Lambda NEVER stops a box doing work, while it MAY stop a
     box whose only signal is an idle open tab.

These are pure in-process unit tests (no asyncio server, no network) -- the
counters are plain ints/sets mutated on the single asyncio loop, so the helpers
are synchronous and directly callable.
"""

from __future__ import annotations

import pytest

from grace2_agent import server


@pytest.fixture(autouse=True)
def _reset_liveness():
    """Reset all module-level liveness state before AND after each test.

    The registry + solver counter are process-global; isolate every test so an
    earlier test's leftover connection/solve cannot leak into a later one.
    """
    server._ACTIVE_WS_CONNECTIONS.clear()
    server._SESSION_LIVE_TURNS.clear()
    server._SOLVE_IN_FLIGHT = 0
    yield
    server._ACTIVE_WS_CONNECTIONS.clear()
    server._SESSION_LIVE_TURNS.clear()
    server._SOLVE_IN_FLIGHT = 0


class _FakeSocket:
    """Stand-in for a ServerConnection -- the registry only needs ``id()``."""


def test_snapshot_shape_idle_at_rest():
    snap = server.liveness_snapshot()
    assert snap == {"ok": True, "active_connections": 0, "busy": False}
    # The Lambda parses these exact keys/types; lock them.
    assert isinstance(snap["active_connections"], int)
    assert isinstance(snap["busy"], bool)
    assert snap["ok"] is True


def test_register_deregister_tracks_live_sockets():
    a, b = _FakeSocket(), _FakeSocket()
    server._register_active_connection(a)
    assert server.active_connection_count() == 1
    # STAGE 3: an open connection is REPORTED but does NOT make the box busy.
    assert server.is_busy() is False
    assert server.liveness_snapshot()["active_connections"] == 1

    server._register_active_connection(b)
    assert server.active_connection_count() == 2

    # Idempotent register -- re-registering the same socket does not double-count.
    server._register_active_connection(a)
    assert server.active_connection_count() == 2

    server._deregister_active_connection(a)
    assert server.active_connection_count() == 1
    server._deregister_active_connection(b)
    assert server.active_connection_count() == 0
    assert server.is_busy() is False


def test_open_idle_connection_is_not_busy():
    # STAGE 3 (sleep/wake): a merely-open, IDLE viewer connection (no in-flight
    # turn, no solve) must NOT pin the box -- the idle Lambda's streak may elapse
    # and stop it. The count stays honestly reported for observability.
    s = _FakeSocket()
    server._register_active_connection(s)
    assert server.active_connection_count() == 1
    assert server.is_busy() is False
    snap = server.liveness_snapshot()
    assert snap == {"ok": True, "active_connections": 1, "busy": False}
    server._deregister_active_connection(s)
    assert server.is_busy() is False


def test_running_turn_pins_box_even_with_zero_sockets():
    # A long turn SURVIVES a socket drop: with ZERO connections but a not-done
    # detached turn, the box stays busy (no mid-turn stop).
    class _NotDoneTask:
        def done(self) -> bool:
            return False

    assert server.active_connection_count() == 0
    server._SESSION_LIVE_TURNS["sess"] = {
        "turn-live": server._LiveTurn(task=_NotDoneTask(), emitter=None),
    }
    assert server.is_busy() is True
    assert server.liveness_snapshot()["busy"] is True


def test_double_deregister_never_negative():
    s = _FakeSocket()
    server._register_active_connection(s)
    server._deregister_active_connection(s)
    # Defensive double-call (e.g. crash path + finally) must not drive negative.
    server._deregister_active_connection(s)
    assert server.active_connection_count() == 0
    assert server.is_busy() is False


def test_busy_when_solver_in_flight_with_no_socket():
    # A solver dispatch keeps the box BUSY even with zero sockets attached
    # (e.g. a detached SFINCS solve whose socket dropped).
    assert server.active_connection_count() == 0
    server._solve_started()
    assert server.solve_in_flight_count() == 1
    assert server.is_busy() is True
    assert server.liveness_snapshot()["busy"] is True
    # ...and active_connections is still honestly zero (the Lambda must rely on
    # ``busy``, NOT just the connection count, to avoid stopping mid-solve).
    assert server.liveness_snapshot()["active_connections"] == 0
    server._solve_finished()
    assert server.solve_in_flight_count() == 0
    assert server.is_busy() is False


def test_solve_counter_clamped_at_zero():
    server._solve_finished()  # unbalanced finish
    assert server.solve_in_flight_count() == 0
    assert server.is_busy() is False


def test_busy_when_detached_inflight_turn():
    # A not-done detached turn in the live-turn registry keeps the box busy via
    # inflight_turn_count(), even with no socket and no solver dispatch.
    class _NotDoneTask:
        def done(self) -> bool:
            return False

    class _DoneTask:
        def done(self) -> bool:
            return True

    server._SESSION_LIVE_TURNS["sess"] = {
        "turnA": server._LiveTurn(task=_NotDoneTask(), emitter=None),
    }
    assert server.inflight_turn_count() == 1
    assert server.is_busy() is True

    # A DONE turn (awaiting its self-removing callback) must NOT count as busy.
    server._SESSION_LIVE_TURNS["sess"]["turnA"] = server._LiveTurn(
        task=_DoneTask(), emitter=None
    )
    assert server.inflight_turn_count() == 0
    assert server.is_busy() is False
