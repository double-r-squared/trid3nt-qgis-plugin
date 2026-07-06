"""Unit tests for the per-session agent SELF-IDLE-EXIT gate and route-row
heartbeat writer.

Covers the pure decision (``_idle_exit_decision``) + the env resolver:
  - idle past the threshold -> exit
  - a client connected -> stay (clock reset)
  - work in flight (busy) -> stay (clock reset)
  - below threshold -> stay, clock keeps running
  - disabled (0 / unset) -> never exit

Also covers the heartbeat writer helpers (_hb_interval_seconds, _hb_route_key,
_hb_write_once) -- mocked DynamoDB, no live AWS.
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


# --------------------------------------------------------------------------- #
# Heartbeat writer -- env helpers
# --------------------------------------------------------------------------- #
def test_hb_interval_disabled_by_default(monkeypatch):
    monkeypatch.delenv("GRACE2_ROUTE_HEARTBEAT_SECONDS", raising=False)
    assert server._hb_interval_seconds() == 0


def test_hb_interval_from_env(monkeypatch):
    monkeypatch.setenv("GRACE2_ROUTE_HEARTBEAT_SECONDS", "60")
    assert server._hb_interval_seconds() == 60


def test_hb_interval_garbage_disables(monkeypatch):
    monkeypatch.setenv("GRACE2_ROUTE_HEARTBEAT_SECONDS", "nope")
    assert server._hb_interval_seconds() == 0


def test_hb_interval_negative_disabled(monkeypatch):
    monkeypatch.setenv("GRACE2_ROUTE_HEARTBEAT_SECONDS", "-5")
    assert server._hb_interval_seconds() == 0


def test_hb_route_key_missing_returns_none(monkeypatch):
    monkeypatch.delenv("GRACE2_ROUTE_USER_ULID", raising=False)
    monkeypatch.delenv("GRACE2_ROUTE_SESSION_ID", raising=False)
    assert server._hb_route_key() is None


def test_hb_route_key_partial_returns_none(monkeypatch):
    monkeypatch.setenv("GRACE2_ROUTE_USER_ULID", "U1")
    monkeypatch.delenv("GRACE2_ROUTE_SESSION_ID", raising=False)
    assert server._hb_route_key() is None


def test_hb_route_key_both_present(monkeypatch):
    monkeypatch.setenv("GRACE2_ROUTE_USER_ULID", "U1")
    monkeypatch.setenv("GRACE2_ROUTE_SESSION_ID", "S1")
    assert server._hb_route_key() == ("U1", "S1")


# --------------------------------------------------------------------------- #
# Heartbeat writer -- _hb_write_once (mocked DynamoDB)
# --------------------------------------------------------------------------- #
class _FakeDDBClient:
    """Minimal fake matching the update_item surface used by _hb_write_once."""

    def __init__(self):
        self.calls: list[dict] = []
        self.raise_on_call = False

    def update_item(self, **kwargs):
        if self.raise_on_call:
            raise RuntimeError("simulated DDB error")
        self.calls.append(kwargs)


def test_hb_write_once_calls_dynamo():
    fake = _FakeDDBClient()
    # Patch the module-level cached client so we bypass boto3 entirely.
    original = server._HB_DDB_CLIENT
    server._HB_DDB_CLIENT = fake
    try:
        server._hb_write_once(
            "U1",
            "S1",
            busy=True,
            active_connections=2,
            inflight_batch=1,
            table="grace2_session_routes",
            region="us-west-2",
        )
    finally:
        server._HB_DDB_CLIENT = original

    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["TableName"] == "grace2_session_routes"
    assert call["Key"] == {
        "user_ulid": {"S": "U1"},
        "session_id": {"S": "S1"},
    }
    vals = call["ExpressionAttributeValues"]
    assert vals[":b"] == {"BOOL": True}
    assert vals[":ac"] == {"N": "2"}
    assert vals[":ib"] == {"N": "1"}
    # hb_last_seen must be a recent epoch int
    last_seen = int(vals[":ls"]["N"])
    import time as _time
    assert abs(last_seen - int(_time.time())) < 5


def test_hb_write_once_busy_false():
    fake = _FakeDDBClient()
    original = server._HB_DDB_CLIENT
    server._HB_DDB_CLIENT = fake
    try:
        server._hb_write_once(
            "U2",
            "S2",
            busy=False,
            active_connections=0,
            inflight_batch=0,
            table="t",
            region="us-west-2",
        )
    finally:
        server._HB_DDB_CLIENT = original
    vals = fake.calls[0]["ExpressionAttributeValues"]
    assert vals[":b"] == {"BOOL": False}
    assert vals[":ac"] == {"N": "0"}
    assert vals[":ib"] == {"N": "0"}


def test_hb_write_once_swallows_ddb_error():
    """A DynamoDB error must NOT propagate -- heartbeat is best-effort."""
    fake = _FakeDDBClient()
    fake.raise_on_call = True
    original = server._HB_DDB_CLIENT
    server._HB_DDB_CLIENT = fake
    try:
        # Should not raise:
        server._hb_write_once(
            "U3",
            "S3",
            busy=False,
            active_connections=0,
            inflight_batch=0,
            table="t",
            region="us-west-2",
        )
    finally:
        server._HB_DDB_CLIENT = original  # no assertion needed -- success = no raise
