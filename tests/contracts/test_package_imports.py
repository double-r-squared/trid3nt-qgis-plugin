"""Imports run one way: trid3nt imports neither other package, trid3nt_harness imports trid3nt only.

Every import statement in the three packages is read off the source, a relative one resolved against
its own package, and so is every string that names a module of a higher package by its dotted path
(an importlib target, a provider reference): none may reach up. The SDK also imports whole in a
fresh interpreter where neither higher package can be imported at all.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAYERS = {"trid3nt": 0, "trid3nt_harness": 1, "trid3nt_server": 2}

#: A whole string that is a dotted module path, optionally ``:attr`` (the provider-reference form).
_MODULE_STRING = re.compile(r"(?P<module>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?::[A-Za-z_]\w*)?")


def _is_module(dotted: str) -> bool:
    path = REPO.joinpath(*dotted.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def _named_module(text: str) -> str | None:
    """The module a string names whole by its dotted path, or its attribute's module; else None."""
    match = _MODULE_STRING.fullmatch(text)
    if match is None or match["module"].split(".")[0] not in LAYERS:
        return None
    module = match["module"]
    while module and not _is_module(module):
        module = module.rpartition(".")[0]
    return module or None


def _logger_names(tree: ast.AST) -> set[int]:
    # A logger's name is a log stream's label, not a module reference.
    return {id(node.args[0]) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and node.args
            and getattr(node.func, "attr", getattr(node.func, "id", None)) == "getLogger"}


def _imported(path: Path) -> set[str]:
    rel = path.relative_to(REPO).with_suffix("")
    package = list(rel.parts[:-1])
    tree = ast.parse(path.read_text(encoding="utf-8"))
    loggers = _logger_names(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join(base + (node.module.split(".") if node.module else []))
            for alias in node.names:
                sub = f"{module}.{alias.name}"
                found.add(sub if _is_module(sub) else module)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in loggers):
            named = _named_module(node.value)
            if named is not None:
                found.add(named)
    return found


def upward_edges() -> set[tuple[str, str]]:
    edges = set()
    for top, layer in LAYERS.items():
        for path in sorted((REPO / top).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for module in _imported(path):
                if LAYERS.get(module.split(".")[0], -1) > layer:
                    edges.add((path.relative_to(REPO).as_posix(), module))
    return edges


def test_no_import_reaches_a_higher_package() -> None:
    upward = sorted(upward_edges())
    assert not upward, f"imports reaching a higher package: {upward}"


def test_a_string_naming_a_higher_module_is_an_edge() -> None:
    tree = ast.parse('x = "trid3nt_harness.inputs.gate.pending:PendingReplies"\n'
                     'import logging\nlog = logging.getLogger("trid3nt_server.server")\n'
                     '"""It imports neither trid3nt_harness nor trid3nt_server."""\n')
    strings = [node for node in ast.walk(tree)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    loggers = _logger_names(tree)
    named = {_named_module(node.value) for node in strings if id(node) not in loggers}
    assert named == {"trid3nt_harness.inputs.gate.pending", None}


_STANDALONE = r"""
import importlib, pkgutil, sys, traceback
sys.modules["trid3nt_harness"] = None
sys.modules["trid3nt_server"] = None
import trid3nt
failed = []
def _seen(name):
    failed.append((name, traceback.format_exc().strip().splitlines()[-1]))
for info in pkgutil.walk_packages(trid3nt.__path__, "trid3nt.", onerror=_seen):
    if info.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(info.name)
    except BaseException:
        _seen(info.name)
for name, why in failed:
    print(name, "|", why)
print("imported", len(sys.modules), "failed", len(failed))
sys.exit(1 if failed else 0)
"""


def test_the_sdk_imports_whole_with_neither_higher_package_importable() -> None:
    done = subprocess.run([sys.executable, "-c", _STANDALONE], cwd=REPO,
                          capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout[-4000:] + done.stderr[-4000:]
    # A registration that swallows an import error logs its traceback and imports on, short of a row.
    assert "Traceback" not in done.stderr, done.stderr[-4000:]
