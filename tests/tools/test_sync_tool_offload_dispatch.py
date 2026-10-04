"""Sync-tool off-load, on the DISPATCH path.

``_invoke_tool_via_emitter`` runs every SYNC tool body in a worker thread with no
environment set, and returns the result the body produced."""

from __future__ import annotations

import os
import threading

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.tools import RegisteredTool
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.tool_registry import AtomicToolMetadata


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


_PROBE_NAME = "compute_offload_probe"


@pytest.fixture(autouse=True)
def _register_probe():
    """A sync tool that records the thread it executed on."""
    original = agent_tools.TOOL_REGISTRY.get(_PROBE_NAME)

    def _fn(**_kw) -> dict:
        return {
            "ran_on_thread_ident": threading.current_thread().ident,
            "ran_on_main": threading.current_thread() is threading.main_thread(),
            "echo": _kw.get("echo"),
        }

    meta = AtomicToolMetadata(
        name=_PROBE_NAME, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[_PROBE_NAME] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield
    finally:
        if original is not None:
            agent_tools.TOOL_REGISTRY[_PROBE_NAME] = original
        else:
            agent_tools.TOOL_REGISTRY.pop(_PROBE_NAME, None)


@pytest.mark.asyncio
async def test_sync_body_runs_off_loop_with_no_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in [k for k in os.environ if "OFFLOAD" in k]:
        monkeypatch.delenv(key)
    loop_thread_ident = threading.current_thread().ident
    result = await server._invoke_tool_via_emitter(
        FakeWS(), server.SessionState(session_id=new_ulid()), _PROBE_NAME, {"echo": 2}
    )
    # Output integrity is preserved across the off-load...
    assert result["echo"] == 2
    # ...and the body ran on a DIFFERENT (worker) thread, not the loop thread.
    assert result["ran_on_thread_ident"] != loop_thread_ident
    assert result["ran_on_main"] is False


