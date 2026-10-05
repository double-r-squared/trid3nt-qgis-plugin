"""User-decision gates never time out locally.

``server._gate_wait_timeout`` is the single seam every gate wait uses, and on the
local build it is effectively unbounded. The CODE-EXEC gate is the carve-out: it
waits on its own bounded approval timeout in EVERY lane and resolves the parked
call with a typed error, since a client with no card for it would hang the turn."""

from __future__ import annotations

import trid3nt_server.server as server


def test_local_gate_timeout_even_when_env_unset(monkeypatch):
    # solver_backend() is hardwired to local-docker; the env var is dead.
    monkeypatch.delenv("TRID3NT_SOLVER_BACKEND", raising=False)
    assert server._gate_wait_timeout(300) == 24 * 3600.0
    assert server._gate_wait_timeout(60) == 24 * 3600.0


def test_local_backend_gets_24h(monkeypatch):
    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", "local-docker")
    assert server._gate_wait_timeout(300) == 24 * 3600.0
    # Every gate default is lifted the same way (spatial-input's 60/120s too).
    assert server._gate_wait_timeout(60) == 24 * 3600.0


def test_local_timeout_is_finite(monkeypatch):
    """'Effectively unbounded' still unwinds an abandoned process: finite."""
    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", "local-docker")
    value = server._gate_wait_timeout(300)
    assert value == float(server._LOCAL_GATE_TIMEOUT_SECONDS)
    assert value < float("inf")
