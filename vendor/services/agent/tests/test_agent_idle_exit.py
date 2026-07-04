"""Unit tests for the per-session agent SELF-IDLE-EXIT gate (belt to the reaper).

Covers the pure decision (``_idle_exit_decision``) + the env resolver:
  - idle past the threshold -> exit
  - a client connected -> stay (clock reset)
  - work in flight (busy) -> stay (clock reset)
  - below threshold -> stay, clock keeps running
  - disabled (0 / unset) -> never exit

No live AWS / network -- the decision is pure and the env resolver reads os.environ.
"""

from __future__ import annotations

from grace2_agent import server


# --------------------------------------------------------------------------- #
# Env resolver
# --------------------------------------------------------------------------- #
def test_idle_exit_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GRACE2_AGENT_IDLE_EXIT_SECONDS", raising=False)
    assert server._idle_exit_seconds() == 0


def test_idle_exit_seconds_from_env(monkeypatch):
    monkeypatch.setenv("GRACE2_AGENT_IDLE_EXIT_SECONDS", "1800")
    assert server._idle_exit_seconds() == 1800


def test_idle_exit_seconds_garbage_disables(monkeypatch):
    monkeypatch.setenv("GRACE2_AGENT_IDLE_EXIT_SECONDS", "not-a-number")
    assert server._idle_exit_seconds() == 0


# --------------------------------------------------------------------------- #
# Pure decision
# --------------------------------------------------------------------------- #
def test_disabled_never_exits():
    should, since = server._idle_exit_decision(
        idle_exit_seconds=0, busy=False, active_connections=0, idle_since=None, now=100.0
    )
    assert should is False and since is None


def test_idle_streak_starts_then_exits_past_threshold():
    # First idle sample starts the clock (no exit yet).
    should, since = server._idle_exit_decision(
        idle_exit_seconds=1800, busy=False, active_connections=0, idle_since=None, now=1000.0
    )
    assert should is False and since == 1000.0
    # Still idle, below threshold -> keep waiting, clock unchanged.
    should, since = server._idle_exit_decision(
        idle_exit_seconds=1800, busy=False, active_connections=0, idle_since=1000.0, now=2000.0
    )
    assert should is False and since == 1000.0
    # Idle past the full window -> exit.
    should, since = server._idle_exit_decision(
        idle_exit_seconds=1800, busy=False, active_connections=0, idle_since=1000.0, now=2800.0
    )
    assert should is True


def test_client_connected_resets_clock():
    should, since = server._idle_exit_decision(
        idle_exit_seconds=1800, busy=False, active_connections=1, idle_since=1000.0, now=3000.0
    )
    assert should is False and since is None  # a live client -> never exit


def test_busy_resets_clock():
    # In-flight work (detached turn / solve / pending snapshot) folds into busy.
    should, since = server._idle_exit_decision(
        idle_exit_seconds=1800, busy=True, active_connections=0, idle_since=1000.0, now=3000.0
    )
    assert should is False and since is None  # mid-work -> never exit


def test_busy_beats_zero_connections():
    # Zero sockets but a solve in flight (survives a socket drop) must NOT exit.
    should, _ = server._idle_exit_decision(
        idle_exit_seconds=1800, busy=True, active_connections=0, idle_since=1.0, now=1_000_000.0
    )
    assert should is False
