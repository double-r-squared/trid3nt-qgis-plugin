"""The staged sync-tool off-load mechanism, shipping dark.

``TRID3NT_SYNC_TOOL_OFFLOAD`` resolves ``off`` / ``subset`` / ``global``; the
startup safety gate is a no-op under the dark default and REFUSES to arm when a
candidate sync tool would touch the emitter. The headline invariant - every sync
tool the off-load touches is emit-free - is asserted against the REAL registry."""

from __future__ import annotations

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.tools import RegisteredTool
from trid3nt_contracts.tool_registry import AtomicToolMetadata


def test_should_offload_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Dark default + unknown values -> never off-load (EXCEPT the in-code
    # _ALWAYS_OFFLOAD_SYNC_TOOLS set, which off-loads regardless of the env mode;
    # use a light tool that is NOT in that set + NOT a compute_*/clip_* prefix to
    # probe the pure env-mode behaviour).
    for off in ("off", "", "maybe", "0", "false"):
        monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", off)
        assert server._should_offload_sync_tool("compute_cross_section") is False
        assert server._should_offload_sync_tool("geocode_location") is False
        # ...but the always-set off-loads even in off/unknown mode.
        assert server._should_offload_sync_tool("fetch_topobathy") is True

    # Subset -> the compute_* family (plus the always-set).
    monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", "subset")
    assert server._should_offload_sync_tool("compute_cross_section") is True
    assert server._should_offload_sync_tool("compute_cross_section") is True
    assert server._should_offload_sync_tool("geocode_location") is False
    assert server._should_offload_sync_tool("sfincs_flood") is False

    # Global aliases -> every tool.
    for glob in ("global", "all", "on", "1", "true", "yes"):
        monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", glob)
        assert server._should_offload_sync_tool("compute_cross_section") is True
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
    """Every real sync tool body is emit-free.

    A failure means a sync tool now touches the loop-bound emitter, and global mode
    must not be armed until the compute and emit halves are split."""
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
