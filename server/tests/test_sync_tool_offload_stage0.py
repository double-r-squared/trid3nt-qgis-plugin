"""#6 STAGED SYNC-TOOL DISPATCH OFF-LOAD — Stage 0 (ships dark).

Stage 0 lands the mechanism for off-loading synchronous atomic-tool bodies to a
worker thread (so a slow sync tool can no longer stall the WS keepalive — see
feedback_no_sync_blocking_on_asyncio_loop) behind the ``GRACE2_SYNC_TOOL_OFFLOAD``
env var, DEFAULT OFF. These tests pin:

1. the staged mode helper (``off``/``subset``/``global``) resolves correctly;
2. the armed-only startup safety gate is a no-op under the dark default;
3. the headline #6 invariant — EVERY sync tool the off-load would touch is
   emit-free (its body never references the loop-bound PipelineEmitter API) — is
   actually true against the REAL registry, for BOTH the Stage-1 ``subset`` and
   the Stage-2 ``global`` cohorts. If a future sync tool starts emitting, the
   global assertion (and this test) fails before we can ever arm it;
4. the gate REFUSES to arm when a candidate sync tool would touch the emitter.
"""

from __future__ import annotations

import pytest

from grace2_agent import server
from grace2_agent import tools as agent_tools
from grace2_agent.tools import RegisteredTool
from grace2_contracts.tool_registry import AtomicToolMetadata


def test_should_offload_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dark default + unknown values -> never off-load (EXCEPT the in-code
    # _ALWAYS_OFFLOAD_SYNC_TOOLS set, which off-loads regardless of the env mode;
    # use a light tool that is NOT in that set + NOT a compute_*/clip_* prefix to
    # probe the pure env-mode behaviour).
    for off in ("off", "", "maybe", "0", "false"):
        monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", off)
        assert server._should_offload_sync_tool("compute_slope") is False
        assert server._should_offload_sync_tool("geocode_location") is False
        # ...but the always-set off-loads even in off/unknown mode.
        assert server._should_offload_sync_tool("fetch_topobathy") is True

    # Subset -> the compute_*/clip_* families (plus the always-set).
    monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", "subset")
    assert server._should_offload_sync_tool("compute_slope") is True
    assert server._should_offload_sync_tool("clip_raster_to_bbox") is True
    assert server._should_offload_sync_tool("geocode_location") is False
    assert server._should_offload_sync_tool("run_model_flood_scenario") is False

    # Global aliases -> every tool.
    for glob in ("global", "all", "on", "1", "true", "yes"):
        monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", glob)
        assert server._should_offload_sync_tool("compute_slope") is True
        assert server._should_offload_sync_tool("fetch_era5_reanalysis") is True


def test_assert_safe_dark_default_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", "off")
    # Must not raise. (With a non-empty _ALWAYS_OFFLOAD_SYNC_TOOLS the guard now
    # runs its emit-free scan even in off mode -- but it stays a no-op in the
    # sense that it never raises, because every always-set member is emit-free.)
    server._assert_sync_offload_safe()


def test_real_subset_is_emit_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage-1 cohort: every real compute_*/clip_* sync tool is emit-free."""
    monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", "subset")
    server._assert_sync_offload_safe()  # raises if any subset tool would emit


def test_real_global_is_emit_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage-2 cohort: EVERY real sync tool body is emit-free.

    This is the core #6 claim ("sync tool bodies are emit-free, so the off-load
    is safe"). If this fails, a sync tool now touches the loop-bound emitter and
    global mode must NOT be armed until it is fixed (compute/emit split).
    """
    monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", "global")
    server._assert_sync_offload_safe()  # raises listing any offending tool(s)


def test_gate_refuses_emitting_sync_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """An armed mode must REFUSE to start if a candidate sync tool would touch
    the emitter from a worker thread."""

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
        monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", "subset")
        with pytest.raises(RuntimeError) as exc:
            server._assert_sync_offload_safe()
        assert name in str(exc.value)
    finally:
        agent_tools.TOOL_REGISTRY.pop(name, None)
