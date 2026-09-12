"""One executor over one file per engine, held by the import graph itself.

The executor stages, launches, supervises and polls; an engine contributes its
spec, its verdict on an exit and its wait, and reaches the executor to dispatch.
Every import is read, in-function ones included: a lazy import is still an edge,
and a rule a spelling evades is a rule with a spelling.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXECUTOR = REPO_ROOT / "trid3nt_server" / "workflows" / "solver"
ENGINE_FILE = REPO_ROOT / "trid3nt_server" / "workflows" / "telemac" / "engine.py"

_EXECUTOR_PACKAGE = "trid3nt_server.workflows.solver"
_ENGINE_PACKAGE = "trid3nt_server.workflows.telemac"


def _imported(path: Path) -> set[str]:
    """Every module this file imports, as a dotted path, relative ones resolved."""
    module = path.relative_to(REPO_ROOT).with_suffix("").as_posix().replace("/", ".")
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join(module.split(".")[:-node.level]
                                + ([node.module] if node.module else []))
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _under(name: str, package: str) -> bool:
    return name == package or name.startswith(f"{package}.")


def test_the_executor_imports_no_engine():
    """Nothing under the executor names an engine package, however it spells it."""
    leaks = {
        f"{path.relative_to(REPO_ROOT).as_posix()} -> {name}"
        for path in sorted(EXECUTOR.rglob("*.py"))
        for name in _imported(path)
        if _under(name, _ENGINE_PACKAGE)
    }
    assert leaks == set(), f"the executor imports an engine: {sorted(leaks)}"


def test_the_engine_file_reaches_the_executor():
    """The engine file dispatches through the executor rather than around it."""
    assert any(_under(name, _EXECUTOR_PACKAGE) for name in _imported(ENGINE_FILE))
