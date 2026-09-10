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


def test_every_gate_wait_site_uses_the_seam():
    """Source-level guard: no gate ``asyncio.wait_for`` bypasses the seam.

    Every user-decision gate wraps its timeout in ``_gate_wait_timeout`` except the
    code-exec gate, which waits on its own bounded approval timeout."""
    import inspect

    from trid3nt_server.gates import confirm as _gates_confirm

    src = inspect.getsource(_gates_confirm)
    for bare in (
        "timeout=warning_payload.ttl_seconds",
        "timeout=CODE_EXEC_CONFIRM_TIMEOUT_SECONDS",
        "timeout=payload.default_timeout_seconds",
    ):
        assert bare not in src, f"gate wait bypasses _gate_wait_timeout: {bare}"
    assert src.count("_gate_wait_timeout(") >= 6  # def + 5 call sites
    # The code-exec carve-out: its wait is the bounded approval window, live-read.
    assert "timeout=approval_timeout_s" in src
    assert "_code_exec_approval_timeout_s()" in src
