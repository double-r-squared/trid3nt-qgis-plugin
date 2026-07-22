"""Verify the agent service startup picks up the tool registry.

Acceptance criterion: ``python -m trid3nt_server --startup-only`` imports the
tools package (populating ``TOOL_REGISTRY``) and exits without binding the
WebSocket port. The test exercises the ``run([...])`` entry point directly.
"""

from __future__ import annotations

import logging

import pytest

from trid3nt_server import tools as agent_tools
from trid3nt_server.main import (
    _bind_worker_submitter,
    _import_tools_registry,
    run,
)


def test_import_tools_registry_populates_passthroughs():
    n = _import_tools_registry()
    assert n >= 2
    assert "qgis_process" in agent_tools.TOOL_REGISTRY
    assert "mongo_query" not in agent_tools.TOOL_REGISTRY


def test_run_startup_only_returns_zero_without_serving(caplog):
    """``run(['--startup-only'])`` returns 0 and logs the registered tools."""
    caplog.set_level(logging.INFO, logger="trid3nt_server.main")
    rc = run(["--startup-only"])
    assert rc == 0
    # Startup log line includes the registered tool names.
    joined = "\n".join(r.message for r in caplog.records)
    assert "tool registry loaded" in joined
    assert "qgis_process" in joined


# ---------------------------------------------------------------------------
# job-0308 (Q-discovery lane): qgis_process readiness probe at boot.
# ---------------------------------------------------------------------------


def test_bind_worker_submitter_logs_probe_ready(monkeypatch, caplog):
    """A healthy submitter (--version returncode 0) logs a READY line."""
    import trid3nt_server.main as main_mod
    from trid3nt_server.tools.meta import passthroughs

    calls: list[tuple] = []

    def _fake_submitter(args, timeout_s):
        calls.append((tuple(args), timeout_s))
        return {
            "stdout": "QGIS 3.40.3-Bratislava 'Bratislava' (abc123)\n",
            "stderr": "",
            "returncode": 0,
            "duration_s": 0.2,
            "qgis_bin": "qgis_process",
        }

    monkeypatch.delenv("TRID3NT_SKIP_WORKER_SUBMITTER", raising=False)
    # QGIS infra configured -> probe runs SYNCHRONOUSLY (and logs inline).
    monkeypatch.setenv("TRID3NT_QGIS_DOCKER_IMAGE", "grace2-qgis:ltr")
    monkeypatch.setattr(
        main_mod, "_default_qgis_process_submitter", lambda: _fake_submitter
    )

    saved = passthroughs._WORKER_SUBMITTER
    try:
        caplog.set_level(logging.INFO, logger="trid3nt_server.main")
        _bind_worker_submitter()
    finally:
        passthroughs._WORKER_SUBMITTER = saved  # type: ignore[attr-defined]

    # The probe invoked the submitter with --version.
    assert (("--version",), 30) in calls
    joined = "\n".join(r.message for r in caplog.records)
    assert "readiness probe OK" in joined
    assert "QGIS 3.40.3-Bratislava" in joined


def test_bind_worker_submitter_logs_not_ready_on_bad_returncode(monkeypatch, caplog):
    """A submitter that returns non-zero logs NOT-READY (non-fatal)."""
    import trid3nt_server.main as main_mod
    from trid3nt_server.tools.meta import passthroughs

    def _bad_submitter(args, timeout_s):
        return {
            "stdout": "",
            "stderr": "qgis_process: command not found in image\n",
            "returncode": 127,
            "duration_s": 0.01,
            "qgis_bin": "docker:grace2-qgis:ltr",
        }

    monkeypatch.delenv("TRID3NT_SKIP_WORKER_SUBMITTER", raising=False)
    # QGIS infra configured -> probe runs SYNCHRONOUSLY (and logs inline).
    monkeypatch.setenv("TRID3NT_QGIS_DOCKER_IMAGE", "grace2-qgis:ltr")
    monkeypatch.setattr(
        main_mod, "_default_qgis_process_submitter", lambda: _bad_submitter
    )

    saved = passthroughs._WORKER_SUBMITTER
    try:
        caplog.set_level(logging.WARNING, logger="trid3nt_server.main")
        # Non-fatal: must not raise.
        _bind_worker_submitter()
    finally:
        passthroughs._WORKER_SUBMITTER = saved  # type: ignore[attr-defined]

    joined = "\n".join(r.message for r in caplog.records)
    assert "readiness probe NOT-READY" in joined


def test_bind_worker_submitter_probe_exception_is_non_fatal(monkeypatch, caplog):
    """A submitter that RAISES during the probe logs NOT-READY but the binding
    still stands and startup is not aborted."""
    import trid3nt_server.main as main_mod
    from trid3nt_server.tools.meta import passthroughs

    def _raising_submitter(args, timeout_s):
        raise RuntimeError("simulated probe blow-up")

    monkeypatch.delenv("TRID3NT_SKIP_WORKER_SUBMITTER", raising=False)
    # QGIS infra configured -> probe runs SYNCHRONOUSLY (and logs inline).
    monkeypatch.setenv("TRID3NT_QGIS_DOCKER_IMAGE", "grace2-qgis:ltr")
    monkeypatch.setattr(
        main_mod, "_default_qgis_process_submitter", lambda: _raising_submitter
    )

    saved = passthroughs._WORKER_SUBMITTER
    try:
        caplog.set_level(logging.WARNING, logger="trid3nt_server.main")
        _bind_worker_submitter()  # must not raise
        # Binding succeeded despite the probe failing.
        assert passthroughs._WORKER_SUBMITTER is _raising_submitter
    finally:
        passthroughs._WORKER_SUBMITTER = saved  # type: ignore[attr-defined]

    joined = "\n".join(r.message for r in caplog.records)
    assert "readiness probe NOT-READY" in joined


def test_bind_worker_submitter_probe_non_blocking_without_qgis_image(
    monkeypatch,
):
    """P0 cold-start: with TRID3NT_QGIS_DOCKER_IMAGE UNSET (the live box, no
    QGIS infra), the readiness probe must NOT block the WS port bind.

    The submitter is wired to BLOCK on an event so a synchronous probe would
    hang ``_bind_worker_submitter`` indefinitely. We assert the bind returns
    promptly (probe deferred to a daemon thread), then release the event and
    confirm the probe did eventually run off the hot path.
    """
    import threading
    import time

    import trid3nt_server.main as main_mod
    from trid3nt_server.tools.meta import passthroughs

    release = threading.Event()
    probe_started = threading.Event()
    probe_finished = threading.Event()

    def _blocking_submitter(args, timeout_s):
        # Mark that the probe reached the submitter, then block until released.
        probe_started.set()
        # Wait up to 5 s for the release; the test releases almost immediately.
        release.wait(timeout=5.0)
        probe_finished.set()
        return {
            "stdout": "QGIS 3.40.3-Bratislava 'Bratislava' (abc123)\n",
            "stderr": "",
            "returncode": 0,
            "duration_s": 0.0,
            "qgis_bin": "qgis_process",
        }

    monkeypatch.delenv("TRID3NT_SKIP_WORKER_SUBMITTER", raising=False)
    # The live box (no QGIS infra) has this UNSET -> probe must be non-blocking.
    monkeypatch.delenv("TRID3NT_QGIS_DOCKER_IMAGE", raising=False)
    monkeypatch.setattr(
        main_mod, "_default_qgis_process_submitter", lambda: _blocking_submitter
    )

    saved = passthroughs._WORKER_SUBMITTER
    try:
        start = time.monotonic()
        _bind_worker_submitter()
        elapsed = time.monotonic() - start
        # Bind returned WITHOUT waiting on the (still-blocked) probe. Generous
        # ceiling vs. the 5 s submitter block to stay non-flaky under load.
        assert elapsed < 2.0, (
            f"_bind_worker_submitter blocked {elapsed:.2f}s; the probe must "
            "be off the hot path when TRID3NT_QGIS_DOCKER_IMAGE is unset"
        )
        # The submitter binding still stands immediately (synchronous part).
        assert passthroughs._WORKER_SUBMITTER is _blocking_submitter
        # The probe is running on a background thread (it reached the submitter
        # and is blocked on the event). Allow a brief window for the thread to
        # spin up before asserting.
        assert probe_started.wait(timeout=2.0), "background probe never started"
        assert not probe_finished.is_set()
        # Release the probe and confirm it completes off the hot path.
        release.set()
        assert probe_finished.wait(timeout=2.0), "background probe never finished"
    finally:
        release.set()
        passthroughs._WORKER_SUBMITTER = saved  # type: ignore[attr-defined]


def test_bind_worker_submitter_skipped_by_env(monkeypatch):
    """``TRID3NT_SKIP_WORKER_SUBMITTER`` short-circuits binding + probe."""
    import trid3nt_server.main as main_mod
    from trid3nt_server.tools.meta import passthroughs

    def _should_not_run():
        raise AssertionError("submitter resolution must be skipped")

    monkeypatch.setenv("TRID3NT_SKIP_WORKER_SUBMITTER", "1")
    monkeypatch.setattr(
        main_mod, "_default_qgis_process_submitter", _should_not_run
    )

    saved = passthroughs._WORKER_SUBMITTER
    try:
        _bind_worker_submitter()  # no-op
    finally:
        passthroughs._WORKER_SUBMITTER = saved  # type: ignore[attr-defined]
