"""The mesh front imports no engine, in every module it holds.

The model's dependency rule reads only the modules a block binds, so a module
the model does not bind could import an engine and the rule would pass while
reading nothing; this reads every module under the tree."""

from __future__ import annotations

import ast
from pathlib import Path

import trid3nt_server.mesh as mesh
import trid3nt_server.workflows as workflows

_TREE = Path(mesh.__file__).resolve().parent
_PACKAGE = "trid3nt_server.mesh"
#: An engine is a package under workflows that holds its own modules; the
#: runtime and the solver beside them are what every engine shares.
_ENGINES = tuple(
    f"trid3nt_server.workflows.{p.parent.parent.name}"
    for p in sorted(
        Path(workflows.__file__).resolve().parent.glob("*/modules/__init__.py")))


def _imports(path: Path) -> set[str]:
    module = ".".join(
        (_PACKAGE, *path.relative_to(_TREE).with_suffix("").parts))
    if path.name == "__init__.py":
        module = module.rsplit(".", 1)[0]
        base = module.split(".")
    else:
        base = module.split(".")[:-1]
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                up = base[: len(base) - node.level + 1]
                out.add(".".join(up + ([node.module] if node.module else [])))
            else:
                out.add(node.module or "")
    return out


def _modules() -> list[Path]:
    return [p for p in sorted(_TREE.rglob("*.py")) if "__pycache__" not in p.parts]


def test_the_sweep_reads_the_whole_tree():
    assert "trid3nt_server.workflows.telemac" in _ENGINES
    names = {str(p.relative_to(_TREE)) for p in _modules()}
    assert {"session.py", "shared/nodes.py", "meshers/om2d.py"} <= names


def test_no_module_of_the_mesh_front_imports_an_engine():
    offenders = sorted(
        f"{p.relative_to(_TREE)} -> {name}"
        for p in _modules() for name in _imports(p)
        for engine in _ENGINES
        if name == engine or name.startswith(engine + "."))
    assert not offenders, offenders
