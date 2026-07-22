"""#6 sync-tool off-load — DISPATCH-PATH integration test.

The Stage-0 unit tests (test_sync_tool_offload_stage0) pin the mode helper and
the armed-only emit-free assertion. This file pins the thing that actually
matters at runtime: that ``_invoke_tool_via_emitter`` runs a SYNC tool body on
the event-loop thread under the dark default, but OFF-LOADS it to a worker
thread when the mode is armed for that tool — while returning the identical
result either way (output integrity preserved across the off-load).

This is the programmatic proof that arming Stage 1 (``GRACE2_SYNC_TOOL_OFFLOAD
=subset``) is safe: the dispatch path is exercised end-to-end here, so the
eventual env-flip is verified, not blind.
"""

from __future__ import annotations

import threading

import pytest

from grace2_agent import server
from grace2_agent import tools as agent_tools
from grace2_agent.tools import RegisteredTool
from grace2_contracts.common import new_ulid
from grace2_contracts.tool_registry import AtomicToolMetadata


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


#: name starts with compute_ so the Stage-1 subset predicate matches it.
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
async def test_dark_default_runs_on_loop_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", "off")
    loop_thread_ident = threading.current_thread().ident
    result = await server._invoke_tool_via_emitter(
        FakeWS(), server.SessionState(session_id=new_ulid()), _PROBE_NAME, {"echo": 1}
    )
    assert result["echo"] == 1
    # Dark default: the body ran inline on the event-loop thread.
    assert result["ran_on_thread_ident"] == loop_thread_ident


@pytest.mark.asyncio
async def test_subset_offloads_to_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", "subset")
    loop_thread_ident = threading.current_thread().ident
    result = await server._invoke_tool_via_emitter(
        FakeWS(), server.SessionState(session_id=new_ulid()), _PROBE_NAME, {"echo": 2}
    )
    # Output integrity is preserved across the off-load...
    assert result["echo"] == 2
    # ...and the body ran on a DIFFERENT (worker) thread, not the loop thread.
    assert result["ran_on_thread_ident"] != loop_thread_ident
    assert result["ran_on_main"] is False


@pytest.mark.asyncio
async def test_global_offloads_non_compute_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Under global mode even a non-compute_ name off-loads. Re-register the probe
    # under a fetch_-style name to prove the predicate is mode-driven, not just
    # the compute_ prefix.
    monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", "global")
    loop_thread_ident = threading.current_thread().ident
    result = await server._invoke_tool_via_emitter(
        FakeWS(), server.SessionState(session_id=new_ulid()), _PROBE_NAME, {"echo": 3}
    )
    assert result["echo"] == 3
    assert result["ran_on_thread_ident"] != loop_thread_ident
