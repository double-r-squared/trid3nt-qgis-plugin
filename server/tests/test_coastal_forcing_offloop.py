"""COASTAL-path event-loop off-loading coverage for ``model_flood_scenario``.

THE BUG (caught live): driving a COASTAL flood (run_model_flood_scenario with
coastal=True / surge_forcing) the agent reached run_model_flood_scenario, then
~39s in the WebSocket died with code 1005 and the turn died. Root cause: the
coastal-specific heavy SYNCHRONOUS steps ran INLINE on the asyncio event loop --
the surge/wave forcing adapter (geopandas/rasterio/pandas reads + file writes),
the best-effort OSM-footprint / NHDPlus-river fetches (network I/O), and the
cht_sfincs deck-build spec compose+upload (pyproj reproject + sync boto3
put_object). While the loop is blocked NO asyncio task runs -- including the 12s
DATA heartbeat (server.py _heartbeat_loop) -- so the client sees ~30s of silence
and force-reconnects, killing the turn's socket (1005) BEFORE the SFINCS Batch
solve dispatches. The pluvial path is lighter and survives.

THE FIX: wrap each offending coastal-specific synchronous helper in
``asyncio.to_thread`` (matching the 11 pre-existing to_thread call sites in the
file, e.g. build_sfincs_model). Off-thread keeps the loop -- and therefore the WS
heartbeat + keepalive -- responsive through the (inherently long) work.

This module asserts the FIX structurally (AST over the source: the named helper
calls inside ``model_flood_scenario`` are arguments to ``asyncio.to_thread``, NOT
direct inline calls) AND dynamically (a slow synthetic forcing-resolver does NOT
starve a concurrent keepalive coroutine -- the off-loop proof). No heavy deps
(no geopandas/rasterio/solver) are required to run these.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import time
from pathlib import Path

import grace2_agent.workflows.model_flood_scenario as mfs


# --------------------------------------------------------------------------- #
# Structural (AST) proof: the coastal heavy helpers are dispatched via
# asyncio.to_thread, never called inline on the loop.
# --------------------------------------------------------------------------- #
#: The coastal-specific SYNCHRONOUS helpers that MUST run off the loop. Each
#: does heavy blocking work (forcing adapter geopandas/rasterio/pandas, OSM /
#: NHDPlus network fetches, pyproj reproject + sync boto3 put_object).
_OFFLOOP_REQUIRED = {
    "_resolve_surge_forcing_from_fetchers",
    "_resolve_building_obstacle_uri",
    "_resolve_quadtree_rivers_uri",
    "_compose_and_upload_deckbuild_spec",
}


def _module_source_tree() -> ast.Module:
    src = Path(inspect.getsourcefile(mfs)).read_text(encoding="utf-8")
    return ast.parse(src)


def _is_to_thread_call(node: ast.AST) -> bool:
    """True for ``asyncio.to_thread(...)`` (or a bare ``to_thread(...)``)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "to_thread":
        return True
    if isinstance(func, ast.Name) and func.id == "to_thread":
        return True
    return False


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _collect_calls(tree: ast.AST) -> tuple[list[ast.Call], set[int]]:
    """Return every Call node + the id()s of Calls that are the FIRST positional
    argument of an ``asyncio.to_thread(...)`` (i.e. dispatched off-loop)."""
    all_calls: list[ast.Call] = []
    offloop_targets: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            all_calls.append(node)
            if _is_to_thread_call(node) and node.args:
                # to_thread(fn, *args): fn is the first positional. The helper is
                # passed as a bare reference (ast.Name), NOT called -- so it will
                # NOT appear as a Call node. Record the referenced name instead.
                first = node.args[0]
                if isinstance(first, ast.Name):
                    offloop_targets.add(id(first))
    return all_calls, offloop_targets


def test_coastal_heavy_helpers_dispatched_via_to_thread() -> None:
    """Each coastal heavy helper is passed to ``asyncio.to_thread`` and is NEVER
    invoked as a direct inline call on the loop within the module."""
    tree = _module_source_tree()

    # 1) Every offending helper appears as the first positional REFERENCE to an
    #    asyncio.to_thread call at least once.
    to_thread_refs: dict[str, int] = {name: 0 for name in _OFFLOOP_REQUIRED}
    inline_calls: dict[str, int] = {name: 0 for name in _OFFLOOP_REQUIRED}

    for node in ast.walk(tree):
        if _is_to_thread_call(node) and node.args:
            first = node.args[0]
            if isinstance(first, ast.Name) and first.id in to_thread_refs:
                to_thread_refs[first.id] += 1
        if isinstance(node, ast.Call):
            name = _called_name(node)
            if name in inline_calls:
                inline_calls[name] += 1

    # The DEFINITIONS of the helpers are FunctionDefs (not Calls) so they never
    # count as inline calls. Any inline Call to one of these names is a loop-block
    # regression.
    for name in _OFFLOOP_REQUIRED:
        assert to_thread_refs[name] >= 1, (
            f"{name} is never dispatched via asyncio.to_thread -- a coastal "
            f"loop-block regression (the WS heartbeat would starve)."
        )
        assert inline_calls[name] == 0, (
            f"{name} is called INLINE on the event loop ({inline_calls[name]} "
            f"direct call site(s)); it MUST go through asyncio.to_thread so the "
            f"WS heartbeat + keepalive stay alive on the coastal path."
        )


def test_no_inline_helper_call_outside_to_thread() -> None:
    """Belt-and-suspenders: no Call node in the module invokes a forbidden helper
    directly (every reference to these names is either the def or a to_thread arg)."""
    tree = _module_source_tree()
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _called_name(node)
            if name in _OFFLOOP_REQUIRED:
                offenders.append(name)
    assert offenders == [], (
        f"inline (on-loop) calls to coastal heavy helpers found: {offenders}"
    )


# --------------------------------------------------------------------------- #
# Dynamic proof: a slow synthetic forcing-resolver run via asyncio.to_thread does
# NOT starve a concurrent keepalive coroutine (stand-in for the WS heartbeat).
# Mirrors test_urban_flood_publish_offloop.test_solve_runs_off_the_event_loop.
# --------------------------------------------------------------------------- #
def test_to_thread_keeps_loop_responsive_during_slow_blocking_work() -> None:
    """A ~0.6s synchronous blocking call dispatched via asyncio.to_thread lets a
    concurrent keepalive coroutine keep ticking; an inline call would starve it."""
    SOLVE_SECONDS = 0.6
    TICK_INTERVAL = 0.02

    def _slow_blocking_forcing_resolve() -> str:
        # Stand-in for the forcing adapter's synchronous geopandas/rasterio churn.
        # time.sleep releases the GIL, so a to_thread worker lets the loop run; an
        # inline await of this would block the loop for the whole duration.
        time.sleep(SOLVE_SECONDS)
        return "materialised"

    ticks = {"n": 0}

    async def _keepalive(stop: asyncio.Event) -> None:
        while not stop.is_set():
            ticks["n"] += 1
            await asyncio.sleep(TICK_INTERVAL)

    async def _drive() -> str:
        stop = asyncio.Event()
        ka = asyncio.create_task(_keepalive(stop))
        try:
            result = await asyncio.to_thread(_slow_blocking_forcing_resolve)
        finally:
            stop.set()
            await ka
        return result

    result = asyncio.run(_drive())
    assert result == "materialised"
    expected_min = int((SOLVE_SECONDS / TICK_INTERVAL) * 0.5)
    assert ticks["n"] >= expected_min, (
        f"loop was starved during the blocking work: only {ticks['n']} keepalive "
        f"ticks (expected >= {expected_min}); the work is NOT off-loop"
    )
