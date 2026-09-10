"""The in-code ALWAYS-OFFLOAD set for proven-heavy sync tools.

``_ALWAYS_OFFLOAD_SYNC_TOOLS`` is a tight hand-audited frozenset whose members
off-load to a worker thread REGARDLESS of the staged env flag, because a heavy
sync body on the asyncio loop starves the WS heartbeat. Every member must be
emit-free: the startup guard refuses to start if an emitting tool joins the set."""

from __future__ import annotations

import ast
import pathlib

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.tools import RegisteredTool
from trid3nt_contracts.tool_registry import AtomicToolMetadata

_SRC = pathlib.Path(server.__file__).resolve().parent.parent
_WORKFLOWS = _SRC / "workflows"
#: The heavy sync fetch this file's sweep guards. It is named by the mesher's
#: ``bed`` field rather than called from a coroutine anywhere in the tree, so the
#: sweep asserts ABSENCE of an on-loop call rather than a particular offload.
_HEAVY_FETCH = "fetch_topobathy("


def test_fetch_topobathy_in_always_set() -> None:
    assert "fetch_topobathy" in server._ALWAYS_OFFLOAD_SYNC_TOOLS


def test_goes_archive_animation_in_always_set() -> None:
    """``fetch_goes_archive_animation`` off-loads like its sibling.

    Called directly it loops over dozens of frames, each a large netCDF download,
    reproject and COG write, which on the loop starves the WS heartbeat."""
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
    """True when ``src`` holds an ``asyncio.to_thread(<callable>, ...)`` whose FIRST
    positional argument is a name or attribute ending in ``fn_name`` - the wrapper
    case - or a direct ``asyncio.to_thread(fn_name, ...)``."""
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


def test_no_workflow_calls_the_heavy_fetch_on_the_loop() -> None:
    """A synchronous heavy fetch inside a coroutine BLOCKS the loop.

    The sweep is the guard: a new caller either stays synchronous or wraps itself in
    ``asyncio.to_thread``, and this fails the moment one does neither."""
    offenders: list[str] = []
    for path in _WORKFLOWS.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        src = path.read_text(encoding="utf-8")
        if _HEAVY_FETCH not in src:
            continue
        if "asyncio.to_thread" in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.AsyncFunctionDef) and _HEAVY_FETCH[:-1] in ast.dump(
                node
            ):
                offenders.append(f"{path.relative_to(_WORKFLOWS)}::{node.name}")
    assert not offenders, (
        "these coroutines call the heavy sync fetch directly, which blocks the "
        "event loop; wrap it in asyncio.to_thread:\n  " + "\n  ".join(offenders)
    )
