"""Every sync tool body runs off-loop, so the startup gate refuses a body that
names the loop-bound emitter API; the invariant is asserted against the REAL
registry."""

from __future__ import annotations

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.tools import RegisteredTool
from trid3nt_contracts.tool_registry import AtomicToolMetadata


def test_every_sync_tool_is_emit_free() -> None:
    """Every real sync tool body is emit-free.

    A failure means a sync tool now touches the loop-bound emitter; it cannot run
    off-loop until the compute and emit halves are split."""
    server._assert_sync_offload_safe()  # raises listing any offending tool(s)


def test_gate_refuses_emitting_sync_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The startup gate REFUSES to start if a sync tool would touch the emitter
    from a worker thread."""

    def _emitting_tool(**_ignored: object) -> None:
        # Body references the loop-bound emitter API -> unsafe to off-load.
        emitter = server.current_emitter()  # noqa: F841 — intentional offender
        return None

    name = "compute_zzz_fake_emitting_tool_stage0"
    meta = AtomicToolMetadata(name=name, ttl_class="live-no-cache", cacheable=False)
    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=_emitting_tool, module=__name__
    )
    try:
        with pytest.raises(RuntimeError) as exc:
            server._assert_sync_offload_safe()
        assert name in str(exc.value)
    finally:
        agent_tools.TOOL_REGISTRY.pop(name, None)
