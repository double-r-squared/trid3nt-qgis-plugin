"""The hook loader's tree walk: every spec's hooks resolve, from the right module.

A fetcher package is self-contained - its spec, corpus and hooks all found by a
walk - so adding, moving or removing one edits no shared file. A hook module
lives beside the ONE spec that names it, and stays in the shared directory only
when SEVERAL specs name it."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from trid3nt_server.tools.fetchers._router import hooks as hooks_pkg
from trid3nt_server.tools.fetchers._router.spec import (
    _fetchers_root,
    compose_specs_from_tree,
)

_SPECS = compose_specs_from_tree()
_HOOKS_PKG = hooks_pkg.__name__
_FETCHERS_PKG = _HOOKS_PKG.rsplit(".", 2)[0]


def _declared(spec) -> dict[str, str]:
    """The spec's hook point -> registered name, however it is declared."""
    named = {}
    if spec.hooks is not None:
        named |= {p: v for p, v in spec.hooks.model_dump().items()
                  if isinstance(v, str) and v}
    join = spec.join if isinstance(spec.join, dict) else {}
    values = (join.get("values") or {}).get("values_hook") or {}
    named |= {f"join.{p}": v for p, v in values.items() if isinstance(v, str)}
    return named


_USERS: dict[str, set[str]] = defaultdict(set)
for _name, _spec in _SPECS.items():
    for _hook in _declared(_spec).values():
        _USERS[hooks_pkg.HOOK_REGISTRY[_hook].__module__].add(_name)


def _hook_module_files() -> list[Path]:
    root = _fetchers_root()
    shared = [p for p in (root / "_router" / "hooks").glob("*.py")
              if not p.stem.startswith("_")]
    return sorted(shared + [p for p in root.rglob("hooks.py") if p.parts[-3] != "_router"])


def _readers(module: str) -> set[str]:
    """Everything that reads this hook module: specs that name it, modules that import it."""
    stem = module.rsplit(".", 1)[-1]
    readers = set(_USERS[module])
    if module.startswith(f"{_HOOKS_PKG}."):
        for path in _hook_module_files():
            if path.stem == stem and path.parent.name == "hooks":
                continue
            text = path.read_text()
            if f"{_HOOKS_PKG}.{stem}" in text or f"from .{stem} import" in text:
                readers.add(str(path))
    return readers


def _package_of(spec_name: str) -> str:
    root = _fetchers_root()
    (path,) = [p for p in root.rglob("source.yaml")
               if f"name: {spec_name}\n" in p.read_text()]
    return f"{_FETCHERS_PKG}." + ".".join(path.parent.relative_to(root).parts)


@pytest.mark.parametrize("spec_name", sorted(_SPECS))
def test_every_declared_hook_resolves(spec_name: str) -> None:
    for point, name in _declared(_SPECS[spec_name]).items():
        assert hooks_pkg.has_hook(name), f"{spec_name}.{point} -> {name}"
        assert callable(hooks_pkg.resolve_hook(name))


@pytest.mark.parametrize("spec_name", sorted(n for n in _SPECS if _declared(_SPECS[n])))
def test_a_single_spec_hook_module_sits_beside_that_spec(spec_name: str) -> None:
    for point, name in _declared(_SPECS[spec_name]).items():
        module = hooks_pkg.resolve_hook(name).__module__
        if len(_readers(module)) > 1:
            assert module.startswith(f"{_HOOKS_PKG}."), (
                f"{name}: read by {sorted(_readers(module))}, so it belongs under "
                f"{_HOOKS_PKG}/, not at {module}")
            continue
        assert module == f"{_package_of(spec_name)}.hooks", (
            f"{spec_name}.{point} -> {name} resolves from {module}; the only spec "
            f"that names it is {spec_name}, so it belongs beside it")


def test_no_hook_module_beside_a_spec_serves_a_different_spec() -> None:
    root = _fetchers_root()
    for path in sorted(root.rglob("hooks.py")):
        parts = path.relative_to(root).parts
        if parts[0] == "_router":
            continue
        module = f"{_FETCHERS_PKG}." + ".".join(parts[:-1]) + ".hooks"
        assert _USERS[module] == {_SPECS[n].name for n in _USERS[module]}
        assert len(_USERS[module]) == 1, f"{module} serves {sorted(_USERS[module])}"
        assert _package_of(next(iter(_USERS[module]))) + ".hooks" == module


def test_the_walk_finds_every_hook_module_on_disk() -> None:
    imported = {Path(m.__file__).resolve() for m in _imported_hook_modules()}
    assert {p.resolve() for p in _hook_module_files()} == imported


def _imported_hook_modules() -> list:
    import sys
    return [m for n, m in sorted(sys.modules.items())
            if getattr(m, "__file__", None) and n != _HOOKS_PKG
            and (n.startswith(f"{_HOOKS_PKG}.")
                 or (n.startswith(f"{_FETCHERS_PKG}.") and n.endswith(".hooks")))
            and not n.rsplit(".", 1)[-1].startswith("_")]
