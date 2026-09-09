"""Verify the agent service startup picks up the tool registry.

Acceptance criterion: ``python -m trid3nt_server --startup-only`` imports the
tools package (populating ``TOOL_REGISTRY``) and exits without binding the
WebSocket port. The test exercises the ``run([...])`` entry point directly.
"""

from __future__ import annotations

import logging

from trid3nt_server import tools as agent_tools
from trid3nt_server.main import _import_tools_registry, run


def test_import_tools_registry_populates_startup_only_tools():
    """The eager block registers what a bare ``import trid3nt_server.tools``
    leaves out - the solver-dispatch pair is registered only here."""
    n = _import_tools_registry()
    assert n >= 2
    assert "run_solver" in agent_tools.TOOL_REGISTRY
    assert "mongo_query" not in agent_tools.TOOL_REGISTRY


def test_run_startup_only_returns_zero_without_serving(caplog):
    """``run(['--startup-only'])`` returns 0 and logs the registered tools."""
    caplog.set_level(logging.INFO, logger="trid3nt_server.main")
    rc = run(["--startup-only"])
    assert rc == 0
    # Startup log line includes the registered tool names.
    joined = "\n".join(r.message for r in caplog.records)
    assert "tool registry loaded" in joined
    assert "run_solver" in joined
