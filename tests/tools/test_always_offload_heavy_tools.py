"""Heavy sync fetch bodies never run on the asyncio loop: no workflow calls one
directly from the loop."""

from __future__ import annotations

import ast
import pathlib

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.tools import RegisteredTool
from trid3nt_contracts.tool_registry import AtomicToolMetadata

_SRC = pathlib.Path(server.__file__).resolve().parent.parent
#: The trees a composer or a mesher lives in.
_SWEPT = (_SRC / "workflows", _SRC / "tools" / "mesh")
#: The heavy sync fetch this file's sweep guards. It is named by a recipe's bed
#: row rather than called from a coroutine anywhere in the tree, so the sweep
#: asserts ABSENCE of an on-loop call rather than a particular offload.
_HEAVY_FETCH = "fetch_cudem("


def _tree(rel: pathlib.PurePath) -> str:
    """The swept tree a module lives in, else its top-level folder."""
    for path in _SWEPT:
        swept = path.relative_to(_SRC)
        if rel.parts[:len(swept.parts)] == swept.parts:
            return str(swept)
    return rel.parts[0]


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
    for path in (p for tree in _SWEPT for p in tree.rglob("*.py")):
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
                offenders.append(f"{path.relative_to(_SRC)}::{node.name}")
    assert not offenders, (
        "these coroutines call the heavy sync fetch directly, which blocks the "
        "event loop; wrap it in asyncio.to_thread:\n  " + "\n  ".join(offenders)
    )


def test_the_sweep_reaches_every_tree_a_composing_tool_is_defined_in() -> None:
    """A composer's tree left out of the sweep is a sweep that passes while
    reading nothing: every tree a registered tool is defined in, outside the
    fetch side (tools) and the card tool (inputs), is swept."""
    import inspect

    trees = {
        _tree(pathlib.Path(inspect.getsourcefile(inspect.unwrap(t.fn))).resolve()
              .relative_to(_SRC))
        for t in agent_tools.TOOL_REGISTRY.values()
    } - {"tools", "inputs"}
    assert "tools/mesh" in trees
    assert not trees - {str(p.relative_to(_SRC)) for p in _SWEPT}, sorted(trees)
