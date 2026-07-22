"""F6 (live-feedback 2026-07-08): user-decision gates never time out locally.

``server._gate_wait_timeout`` is the single seam every gate wait uses
(payload-warning, code-exec, solver-confirm/resolution, credential-request,
region-choice, spatial-input). Local build (the established
``GRACE2_SOLVER_BACKEND=local-docker`` seam) -> effectively unbounded (24h);
cloud (unset / aws-batch) -> the caller's default, byte-identical.
"""

from __future__ import annotations

import grace2_agent.server as server


def test_cloud_default_passthrough_when_backend_unset(monkeypatch):
    monkeypatch.delenv("GRACE2_SOLVER_BACKEND", raising=False)
    assert server._gate_wait_timeout(300) == 300.0
    assert server._gate_wait_timeout(60) == 60.0


def test_cloud_default_passthrough_when_backend_aws_batch(monkeypatch):
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    assert server._gate_wait_timeout(300) == 300.0


def test_local_backend_gets_24h(monkeypatch):
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    assert server._gate_wait_timeout(300) == 24 * 3600.0
    # Every gate default is lifted the same way (spatial-input's 60/120s too).
    assert server._gate_wait_timeout(60) == 24 * 3600.0


def test_local_timeout_is_finite(monkeypatch):
    """'Effectively unbounded' still unwinds an abandoned process: finite."""
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    value = server._gate_wait_timeout(300)
    assert value == float(server._LOCAL_GATE_TIMEOUT_SECONDS)
    assert value < float("inf")


def test_every_gate_wait_site_uses_the_seam():
    """Source-level guard: no gate ``asyncio.wait_for`` bypasses the seam.

    The six user-decision gates all wait on a pending future; each must wrap
    its timeout in ``_gate_wait_timeout``. Grep the module source for the
    known gate timeout expressions and assert none appear un-wrapped.
    """
    import inspect

    src = inspect.getsource(server)
    for bare in (
        "timeout=warning_payload.ttl_seconds",
        "timeout=CODE_EXEC_CONFIRM_TIMEOUT_SECONDS",
        "timeout=payload.default_timeout_seconds",
    ):
        assert bare not in src, f"gate wait bypasses _gate_wait_timeout: {bare}"
    assert src.count("_gate_wait_timeout(") >= 7  # def + 6 call sites
