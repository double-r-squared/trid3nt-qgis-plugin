"""F6 (live-feedback 2026-07-08): user-decision gates never time out locally.

``server._gate_wait_timeout`` is the single seam every gate wait uses
(payload-warning, solver-confirm/resolution, credential-request,
region-choice, spatial-input). Local build (the established
``GRACE2_SOLVER_BACKEND=local-docker`` seam) -> effectively unbounded (24h);
solver_backend() is hardwired local-docker so this holds regardless of the
dead GRACE2_SOLVER_BACKEND env var.

CARVE-OUT (live-feedback 2026-07-22): the CODE-EXEC gate no longer waits on
this seam. The QGIS plugin had zero handling for the ``code-exec-request``
card, so the F6 24h wait hung the turn; code-exec now waits on its own bounded
``_code_exec_approval_timeout_s()`` (default 180s, env
``GRACE2_CODE_EXEC_APPROVAL_TIMEOUT_S``) in EVERY lane and resolves the parked
tool call with the typed ``CodeExecApprovalTimeoutError``. See
tests/test_code_exec_tool.py for the timeout-path coverage.
"""

from __future__ import annotations

import grace2_agent.server as server


def test_local_gate_timeout_even_when_env_unset(monkeypatch):
    # solver_backend() is hardwired to local-docker; the env var is dead.
    monkeypatch.delenv("GRACE2_SOLVER_BACKEND", raising=False)
    assert server._gate_wait_timeout(300) == 24 * 3600.0
    assert server._gate_wait_timeout(60) == 24 * 3600.0


def test_local_gate_timeout_even_when_env_claims_aws_batch(monkeypatch):
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "aws-batch")
    assert server._gate_wait_timeout(300) == 24 * 3600.0


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

    The user-decision gates all wait on a pending future; each must wrap its
    timeout in ``_gate_wait_timeout`` -- EXCEPT the code-exec gate, which
    (2026-07-22 carve-out, see module docstring) waits on its own bounded
    ``_code_exec_approval_timeout_s()`` so an unanswered approval card can
    never hang the turn. Grep the module source for the known gate timeout
    expressions and assert none appear un-wrapped.
    """
    import inspect

    src = inspect.getsource(server)
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
