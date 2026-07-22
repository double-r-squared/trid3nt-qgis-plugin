"""#6 SYNC-TOOL OFF-LOAD -- in-code ALWAYS-OFFLOAD set for proven-heavy tools.

The staged ``GRACE2_SYNC_TOOL_OFFLOAD`` env flag ships dark (mode ``off``) and
flipping it to ``global`` on the box is a gated production-mode change. But a
coastal-flood turn died at code 1005 because the LLM-driven ``fetch_topobathy``
tool ran a ~61 s CUDEM tile merge + reproject + 189 MB COG materialize INLINE on
the asyncio loop (``_invoke_with_unique_layer_id``), starving the 12 s WS
data-heartbeat past the browser reconnect deadline (see
feedback_no_sync_blocking_on_asyncio_loop).

The fix is a pure-code ``_ALWAYS_OFFLOAD_SYNC_TOOLS`` frozenset: a TIGHT,
hand-audited set of proven-pathological emit-free heavy sync tools that off-load
to a worker thread REGARDLESS of the env flag. These tests pin:

1. ``fetch_topobathy`` (the root-cause tool) is in the always-set and
   ``_should_offload_sync_tool`` returns True for it EVEN in dark ``off`` mode;
2. every always-set member off-loads in ``off`` mode (the set is unconditional);
3. the startup guard runs its emit-free scan in ``off`` mode (because the
   always-set is non-empty) and does NOT raise (every member is emit-free);
4. the guard would REFUSE to start in ``off`` mode if an emitting tool were ever
   added to the always-set -- so the invariant can never silently regress;
5. both direct in-workflow ``fetch_topobathy`` call sites already run off-loop
   via ``asyncio.to_thread`` (their enclosing helpers are sync, dispatched off
   the loop), so the loop is never blocked by the workflow path either.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from grace2_agent import server
from grace2_agent import tools as agent_tools
from grace2_agent.tools import RegisteredTool
from grace2_contracts.tool_registry import AtomicToolMetadata

_SRC = pathlib.Path(server.__file__).resolve().parent
_WORKFLOWS = _SRC / "workflows"
_FLOOD = _WORKFLOWS / "model_flood_scenario.py"
_GEOCLAW = _WORKFLOWS / "model_dambreak_geoclaw_scenario.py"
_GLM_ANIM = _WORKFLOWS / "model_glm_lightning_animation.py"


def test_fetch_topobathy_in_always_set() -> None:
    assert "fetch_topobathy" in server._ALWAYS_OFFLOAD_SYNC_TOOLS


def test_goes_archive_animation_in_always_set() -> None:
    """LIVE 2026-06-25: fetch_goes_archive_animation looped over 78+ frames (each a
    ~54 MB netCDF download + reproject + COG write) ON the asyncio loop when the
    LLM called it directly (the historical fire-animation path), starving the WS
    heartbeat -> health-endpoint timeout + client connecting-loop. It must
    off-load like its sibling fetch_goes_animation."""
    assert "fetch_goes_archive_animation" in server._ALWAYS_OFFLOAD_SYNC_TOOLS
    assert server._should_offload_sync_tool("fetch_goes_archive_animation") is True


def test_goes_active_fire_in_always_set() -> None:
    """fetch_goes_active_fire reuses the SAME per-frame archive download +
    reproject + COG-write core (_fetch_archive_frame_cog_bytes) in a multi-frame
    sync loop, so it has the identical loop-block hazard and must off-load too."""
    assert "fetch_goes_active_fire" in server._ALWAYS_OFFLOAD_SYNC_TOOLS
    assert server._should_offload_sync_tool("fetch_goes_active_fire") is True


def test_always_set_offloads_even_in_off_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The always-set off-loads regardless of the env flag, including dark
    ``off`` mode -- that is the whole point of the in-code list."""
    monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", "off")
    # The root-cause tool specifically.
    assert server._should_offload_sync_tool("fetch_topobathy") is True
    # And EVERY member of the always-set.
    for name in server._ALWAYS_OFFLOAD_SYNC_TOOLS:
        assert server._should_offload_sync_tool(name) is True, name
    # A non-member sync tool still does NOT off-load in off mode (the set is
    # tight, not "off-load everything").
    assert server._should_offload_sync_tool("geocode_location") is False


def test_guard_runs_and_passes_in_off_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a non-empty always-set the startup guard must run its emit-free scan
    even in ``off`` mode (the always-set off-loads regardless), and it must NOT
    raise because every member is emit-free."""
    monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", "off")
    server._assert_sync_offload_safe()  # raises if any always-set tool emits


def test_guard_refuses_emitting_tool_in_always_set_off_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a future EMITTING sync tool is ever added to the always-set, the guard
    must refuse to start EVEN in off mode (the always-set off-loads regardless,
    so its emit-free invariant must hold there too)."""

    def _emitting_tool(**_ignored: object) -> None:
        emitter = server.current_emitter()  # noqa: F841 -- intentional offender
        return None

    name = "fetch_zzz_fake_emitting_heavy_tool"
    meta = AtomicToolMetadata(name=name, ttl_class="live-no-cache", cacheable=False)
    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=_emitting_tool, module=__name__
    )
    augmented = frozenset(server._ALWAYS_OFFLOAD_SYNC_TOOLS | {name})
    monkeypatch.setattr(server, "_ALWAYS_OFFLOAD_SYNC_TOOLS", augmented)
    try:
        monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", "off")
        with pytest.raises(RuntimeError) as exc:
            server._assert_sync_offload_safe()
        assert name in str(exc.value)
    finally:
        agent_tools.TOOL_REGISTRY.pop(name, None)


def _calls_to_thread_with(src: str, fn_name: str) -> bool:
    """True if ``src`` contains an ``asyncio.to_thread(<callable>, ...)`` whose
    FIRST positional arg is a name/attr ending in ``fn_name`` (e.g.
    ``asyncio.to_thread(_fetcher_chain)`` is a wrapper that calls fetch_topobathy
    inside its body), OR a direct ``asyncio.to_thread(fetch_topobathy, ...)``."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_to_thread = (
            isinstance(func, ast.Attribute)
            and func.attr == "to_thread"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        )
        if not is_to_thread or not node.args:
            continue
        first = node.args[0]
        target = None
        if isinstance(first, ast.Name):
            target = first.id
        elif isinstance(first, ast.Attribute):
            target = first.attr
        if target == fn_name:
            return True
    return False


def test_flood_topobathy_runs_off_loop() -> None:
    """The flood workflow's fetch_topobathy call lives inside the sync
    ``_fetcher_chain`` closure, which is dispatched via
    ``asyncio.to_thread(_fetcher_chain)`` -- so the loop is never blocked."""
    src = _FLOOD.read_text()
    # fetch_topobathy is invoked in the file...
    assert "fetch_topobathy(" in src
    # ...and the fetcher chain that runs it is off-loaded.
    assert _calls_to_thread_with(src, "_fetcher_chain"), (
        "model_flood_scenario must run its fetcher chain (which calls "
        "fetch_topobathy) via asyncio.to_thread"
    )


def test_geoclaw_topobathy_runs_off_loop() -> None:
    """The geoclaw workflow's fetch_topobathy call lives inside the sync
    ``_fetch_topo_for_geoclaw`` helper, dispatched via
    ``asyncio.to_thread(_fetch_topo_for_geoclaw, bbox)``."""
    src = _GEOCLAW.read_text()
    assert "fetch_topobathy(" in src
    assert _calls_to_thread_with(src, "_fetch_topo_for_geoclaw"), (
        "model_dambreak_geoclaw_scenario must run its topo helper (which calls "
        "fetch_topobathy) via asyncio.to_thread"
    )


def test_glm_lightning_per_frame_bake_runs_off_loop() -> None:
    """The GLM lightning composer's heavy per-frame work -- the ~54 MB GLM-granule
    + visible-base netCDF download + GED bin + raster bake + COG write inside the
    sync ``_emit_baked_frame`` helper -- must be dispatched via
    ``asyncio.to_thread(_emit_baked_frame, ...)`` so the per-frame loop yields to
    the asyncio loop between frames and the WS heartbeat can fire (the fire/lightning
    connecting-loop blocker, LIVE 2026-06-25)."""
    src = _GLM_ANIM.read_text()
    # The sync per-frame bake helper exists + does the heavy work...
    assert "def _emit_baked_frame(" in src
    # ...and the composer dispatches it off the loop, once per frame.
    assert _calls_to_thread_with(src, "_emit_baked_frame"), (
        "model_glm_lightning_animation must run each frame's _emit_baked_frame "
        "(the ~54 MB download + reproject + COG bake) via asyncio.to_thread so "
        "the loop yields between frames and the WS heartbeat stays live"
    )
    # The standalone GED overlay fetch is also off-loop (the fetcher is sync).
    assert _calls_to_thread_with(src, "fetcher"), (
        "model_glm_lightning_animation must dispatch fetch_glm_lightning "
        "(standalone GED overlay) via asyncio.to_thread"
    )
