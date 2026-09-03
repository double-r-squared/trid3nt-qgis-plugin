#!/usr/bin/env python
"""Standing code-graph instrument: what is reachable from the process entry
points, what is only reachable from tests, and what nothing reaches at all.

Dev tooling, not product code. Library-first: grimp owns the import graph for
the three importable packages, vulture owns dead-symbol detection, stdlib ast
covers the non-package trees (workers/, scripts/, tests/) plus the two edge
classes a static import graph cannot see on its own -- dotted-path strings
resolved through importlib at runtime, and cross-module call-site counts.

Run:  venvs/agent/bin/python scripts/code_graph.py
Out:  docs/validation/code-graph/{graph.json,orphans.md,dead_symbols.md,SUMMARY.md}

Output is sorted end to end so a re-run produces a meaningful git diff.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "docs" / "validation" / "code-graph"

# ---------------------------------------------------------------------------
# ROOTS -- the process entry points. Reachability is measured FROM here and
# nowhere else. Tests are deliberately NOT roots: a module only tests reach is
# an anchored corpse, and naming it is the whole point of this instrument.
# ---------------------------------------------------------------------------
ROOTS: tuple[str, ...] = (
    "trid3nt_server.main",       # the agent daemon (python -m trid3nt_server.main)
    "trid3nt_server.__main__",   # python -m trid3nt_server, delegates to .main
    "trid3nt_server.tools",      # the tool registry: import-time @register_tool surface
    "workers.telemac.entrypoint",
    "workers.mesh.entrypoint",
    "plugin",                    # QGIS calls plugin.classFactory on load
)

#: Source trees that become graph nodes. Value = (path, is-a-grimp-package).
AREAS: dict[str, tuple[str, bool]] = {
    "trid3nt_server": ("trid3nt_server", True),
    "trid3nt_contracts": ("contracts/trid3nt_contracts", True),
    "plugin": ("plugin", True),
    "workers": ("workers", False),
    "scripts": ("scripts", False),
}

#: Test trees. Parsed as importers only -- never roots.
TEST_AREAS: tuple[str, ...] = ("tests", "contracts/tests")

SKIP_DIR_PARTS = {"__pycache__", ".pytest_cache", "node_modules", "sandbox_tmp"}

# ---------------------------------------------------------------------------
# Dead-symbol whitelist. Each rule is a FALSE-POSITIVE CLASS this repo's
# registry patterns produce, not a convenience mute. A rule that stops matching
# a real pattern must be deleted, not left as cover.
# ---------------------------------------------------------------------------

#: A callable reached only through a decorator table (@register_tool /
#: @register_hook / @register_* / pydantic + pytest hooks) has no static caller
#: by construction; vulture reads that as unused.
REGISTRY_DECORATOR_PREFIXES = (
    "register_", "field_validator", "model_validator", "root_validator",
    "validator", "hookimpl", "app.", "router.",
)

#: Names the interpreter, Qt/QGIS, or the io stack calls -- never repo code.
PROTOCOL_NAMES = frozenset({
    "__set_name__", "__get__", "__set__", "__delete__", "__init_subclass__",
    "__class_getitem__", "__enter__", "__exit__", "__aenter__", "__aexit__",
    "__post_init__", "__call__", "__getattr__", "__repr__", "__str__",
    "__eq__", "__hash__", "__len__", "__iter__", "__next__", "__contains__",
    "classFactory", "conftest", "pytest_configure", "pytest_collection_modifyitems",
    "model_config", "model_post_init", "Config",
    # Qt / QGIS call these off the plugin object and the map tools.
    "initGui", "unload", "highlightBlock", "eventFilter", "paintEvent",
    "canvasPressEvent", "canvasReleaseEvent", "canvasMoveEvent",
    "canvasDoubleClickEvent", "keyPressEvent", "resizeEvent", "showEvent",
    "closeEvent", "sizeHint",
    # io.RawIOBase / BufferedIOBase protocol, called by the io stack.
    "readinto", "readable", "seekable", "writable", "seek", "tell",
})

#: Declarative row/field DSLs: the attribute is read by the framework off the
#: class, never by a name lookup vulture can see.
DECLARATIVE_ATTR_OWNERS = ("declarations.py", "contracts.py", "_contracts.py", "spec.py")


@dataclass
class Node:
    module: str
    path: str
    package: str
    loc: int
    is_test: bool = False
    is_script: bool = False
    is_marker: bool = False
    reachable: bool = False
    test_only: bool = False
    script_only: bool = False
    unresolved_calls: int = 0


@dataclass
class Universe:
    nodes: dict[str, Node] = field(default_factory=dict)
    #: importer -> imported -> {"static": n, "dynamic": n, "refs": n}
    edges: dict[str, dict[str, dict[str, int]]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(int))))

    def add_edge(self, importer: str, imported: str, kind: str, count: int = 1) -> None:
        if importer == imported or importer not in self.nodes or imported not in self.nodes:
            return
        self.edges[importer][imported][kind] += count


# ---------------------------------------------------------------------------
# Filesystem -> module universe
# ---------------------------------------------------------------------------

def _module_name(rel: Path, area_root: Path, area_name: str) -> str:
    inner = rel.relative_to(area_root)
    parts = list(inner.parts)
    parts[-1] = parts[-1][: -len(".py")]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join([area_name, *parts]) if parts else area_name


def _walk_py(root: Path) -> Iterable[Path]:
    for p in sorted(root.rglob("*.py")):
        if SKIP_DIR_PARTS & set(p.parts):
            continue
        yield p


def _is_test_path(p: Path) -> bool:
    return "tests" in p.parts or p.name.startswith("test_") or p.name == "conftest.py"


def _package_of(module: str) -> str:
    parts = module.split(".")
    # trid3nt_server is large enough that a single bucket says nothing; cut at
    # the subpackage so the matrix reads as an architecture, not a blob.
    if parts[0] == "trid3nt_server" and len(parts) > 1:
        return ".".join(parts[:2])
    return parts[0]


def build_universe() -> Universe:
    u = Universe()
    seen_paths: set[Path] = set()

    def register(path: Path, module: str, *, is_script: bool) -> None:
        if path in seen_paths:
            return
        seen_paths.add(path)
        loc = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        u.nodes[module] = Node(
            module=module,
            path=str(path.relative_to(REPO)),
            package=_package_of(module),
            loc=loc,
            is_test=_is_test_path(path.relative_to(REPO)),
            is_script=is_script,
        )

    for area_name, (rel_root, _) in AREAS.items():
        root = REPO / rel_root
        for p in _walk_py(root):
            register(p, _module_name(p.relative_to(REPO), Path(rel_root), area_name),
                     is_script=area_name == "scripts")

    for rel_root in TEST_AREAS:
        root = REPO / rel_root
        if not root.exists():
            continue
        area_name = rel_root.replace("/", ".")
        for p in _walk_py(root):
            register(p, _module_name(p.relative_to(REPO), Path(rel_root), area_name), is_script=False)
            u.nodes[_module_name(p.relative_to(REPO), Path(rel_root), area_name)].is_test = True

    return u


# ---------------------------------------------------------------------------
# Import edges: grimp for the packages, ast for everything else
# ---------------------------------------------------------------------------

def add_grimp_edges(u: Universe) -> tuple[int, list[str]]:
    import grimp

    # grimp resolves packages off sys.path; the repo root is not on it when the
    # script runs from scripts/.
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    packages = [name for name, (_, is_pkg) in AREAS.items() if is_pkg]
    graph = grimp.build_graph(*packages, include_external_packages=False)
    added, unknown = 0, []
    for importer in sorted(graph.modules):
        if importer not in u.nodes:
            unknown.append(importer)
            continue
        for imported in sorted(graph.find_modules_directly_imported_by(importer)):
            if imported not in u.nodes:
                continue
            u.add_edge(importer, imported, "static")
            added += 1
    return added, sorted(unknown)


def _resolve_target(u: Universe, dotted: str, context: str | None = None) -> str | None:
    """Longest known-module prefix of a dotted path (``a.b.c`` may be module
    ``a.b.c`` or attribute ``c`` of module ``a.b``).

    ``context`` enables FLAT-NAMESPACE resolution: a worker payload is copied
    flat into the container's /app, so ``entrypoint.py`` writes
    ``import artemis_build`` for a module the repo stores as a sibling. An
    absolute-looking bare name that matches a sibling file IS that sibling.
    """
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        cand = ".".join(parts[:cut])
        if cand in u.nodes:
            return cand
    if context:
        sibling_pkg = context.rpartition(".")[0]
        for cut in range(len(parts), 0, -1):
            cand = f"{sibling_pkg}.{'.'.join(parts[:cut])}"
            if cand in u.nodes:
                return cand
    return None


def _abs_from_relative(module: str, node: ast.ImportFrom) -> str:
    base = module.split(".")
    # A package's own __init__ is addressed by the package name, so level 1
    # means "this package" for a package and "my parent" for a plain module.
    if module in _PACKAGE_MODULES:
        base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
    else:
        base = base[: len(base) - node.level]
    return ".".join([*base, node.module] if node.module else base)


_PACKAGE_MODULES: set[str] = set()


def parse_module(u: Universe, module: str, tree: ast.AST) -> dict[str, str]:
    """Static + dynamic edges out of one module. Returns its import table
    (local name -> target module) for the reference-edge pass."""
    table: dict[str, str] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve_target(u, alias.name, module)
                if target:
                    u.add_edge(module, target, "static")
                    table[(alias.asname or alias.name.split(".")[0])] = target
        elif isinstance(node, ast.ImportFrom):
            base = _abs_from_relative(module, node) if node.level else (node.module or "")
            if not base:
                continue
            base_target = _resolve_target(u, base, module)
            for alias in node.names:
                target = _resolve_target(u, f"{base}.{alias.name}", module) or base_target
                if target:
                    u.add_edge(module, target, "static")
                    table[alias.asname or alias.name] = target

    dotted, filenames = _dynamic_strings(tree)
    for path in dotted:
        target = _resolve_target(u, path)
        if target:
            u.add_edge(module, target, "dynamic")
    for target in _resolve_filenames(u, module, filenames):
        u.add_edge(module, target, "dynamic")

    return table


def _resolve_filenames(u: Universe, module: str, filenames: set[str]) -> list[str]:
    """A module named by BARE FILENAME is a real dependency: the sandbox and the
    mesh/telemac in-container drivers are shipped as files and executed as
    subprocesses, never imported. Resolve to the candidate sharing the longest
    package prefix with the referencing module."""
    out = []
    for fname in filenames:
        stem = fname[: -len(".py")]
        cands = [m for m, n in u.nodes.items() if Path(n.path).stem == stem]
        if not cands:
            continue
        ctx = module.split(".")
        out.append(max(cands, key=lambda c: len(
            [1 for a, b in zip(ctx, c.split(".")) if a == b])))
    return out


def _dynamic_strings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """String literals that name code: dotted module paths for the importlib
    seams (``workflows/lib/resolver._load``, ``fallbacks/walker``, the
    payload-warning estimator lookup), and bare ``<name>.py`` filenames for the
    ship-and-exec drivers. Module-level string constants are resolved so
    f-strings like ``f"{_HELPERS}.water_quality.x"`` land."""
    consts: dict[str, str] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    consts[tgt.id] = node.value.value

    prefixes = tuple(f"{a}." for a in AREAS)
    found: set[str] = set()
    filenames: set[str] = set()
    for node in ast.walk(tree):
        value: str | None = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
        elif isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for piece in node.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    parts.append(piece.value)
                elif isinstance(piece, ast.FormattedValue) and isinstance(piece.value, ast.Name) and piece.value.id in consts:
                    parts.append(consts[piece.value.id])
                else:
                    parts = []
                    break
            value = "".join(parts) if parts else None
        if not value:
            continue
        if value.startswith(prefixes) and " " not in value:
            found.add(value.rstrip("."))
        elif _BARE_PY_FILE.fullmatch(value):
            filenames.add(value)
    return found, filenames


#: A bare module filename -- a path or a glob is a different thing and is left alone.
_BARE_PY_FILE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\.py")


def count_reference_edges(u: Universe, module: str, tree: ast.AST, table: dict[str, str]) -> int:
    """Cross-module call sites resolved through the import table. Returns the
    count this pass could NOT attribute -- reported, never hidden."""
    unresolved = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Name):
            target = table.get(fn.id)
        elif isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            target = table.get(fn.value.id)
        else:
            target = None
        if target:
            u.add_edge(module, target, "refs")
        else:
            unresolved += 1
    return unresolved


def add_ast_edges(u: Universe) -> int:
    """Parse EVERY node: grimp misses the non-package trees entirely, and the
    dynamic/reference passes apply to the packages too."""
    parsed = 0
    for module in sorted(u.nodes):
        path = REPO / u.nodes[module].path
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        except SyntaxError:
            continue
        table = parse_module(u, module, tree)
        u.nodes[module].unresolved_calls = count_reference_edges(u, module, tree, table)
        u.nodes[module].is_marker = _is_package_marker(path, tree)
        parsed += 1
    return parsed


def _is_package_marker(path: Path, tree: ast.AST) -> bool:
    """An ``__init__.py`` carrying nothing but a docstring is a PACKAGE MARKER:
    the directory exists to hold data the runtime walks (``fetchers/**/source.yaml``
    is composed by directory, not by import), so the file has no code to be dead.
    Reporting it as an orphan buries the two real ones under a hundred shims."""
    if path.name != "__init__.py":
        return False
    body = [n for n in getattr(tree, "body", [])
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
            and not (isinstance(n, ast.ImportFrom) and n.module == "__future__")]
    return not body


# ---------------------------------------------------------------------------
# Reachability
# ---------------------------------------------------------------------------

def _reach(u: Universe, seeds: Iterable[str]) -> set[str]:
    out: set[str] = set()
    stack = [s for s in seeds if s in u.nodes]
    while stack:
        m = stack.pop()
        if m in out:
            continue
        out.add(m)
        # Importing a.b.c executes a and a.b, so ancestors ride along.
        parts = m.split(".")
        for cut in range(1, len(parts)):
            anc = ".".join(parts[:cut])
            if anc in u.nodes and anc not in out:
                stack.append(anc)
        for imported, kinds in u.edges.get(m, {}).items():
            if (kinds.get("static", 0) or kinds.get("dynamic", 0)) and imported not in out:
                stack.append(imported)
    return out


def classify(u: Universe) -> dict[str, set[str]]:
    live = _reach(u, ROOTS)
    # A bucket names what the seeds KEEP ALIVE, so the seeds drop out of it.
    tests = {m for m, n in u.nodes.items() if n.is_test}
    from_tests = _reach(u, tests) - live - tests
    scripts = {m for m, n in u.nodes.items() if n.is_script}
    from_scripts = _reach(u, scripts) - live - from_tests - scripts - tests

    for m, node in u.nodes.items():
        node.reachable = m in live
        node.test_only = m in from_tests
        node.script_only = m in from_scripts
    return {"live": live, "test_only": from_tests, "script_only": from_scripts}


# ---------------------------------------------------------------------------
# Dead symbols (vulture)
# ---------------------------------------------------------------------------

def _param_index(path: Path) -> dict[tuple[str, int], int]:
    """(name, lineno) -> the ``def`` line, for every function parameter in a file.

    Vulture types an unused parameter as a plain variable. A parameter is a
    different fact from a dead local -- it is a knob callers still pass -- so it
    is RECLASSIFIED rather than muted, and the def line is what carries the
    repo's own ``noqa: ARG`` marker for signatures an external API mandates."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return {}
    out: dict[tuple[str, int], int] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs,
                  *([args.vararg] if args.vararg else []),
                  *([args.kwarg] if args.kwarg else [])]:
            out[(a.arg, a.lineno)] = node.lineno
    return out


def _decorators_of(item: Any, source_lines: list[str]) -> list[str]:
    """Decorator names attached to a reported item. Vulture reports a decorated
    function at its FIRST decorator line, so the window runs forward from there
    to the ``def``/``class`` it belongs to."""
    out: list[str] = []
    for offset in range(0, 12):
        idx = item.first_lineno - 1 + offset
        if idx >= len(source_lines):
            break
        stripped = source_lines[idx].lstrip()
        if stripped.startswith(("def ", "async def ", "class ")):
            break
        if stripped.startswith("@"):
            out.append(stripped[1:])
    # A decorator may also sit above the reported line for undecorated-item kinds.
    if item.first_lineno >= 2 and source_lines[item.first_lineno - 2].lstrip().startswith("@"):
        out.append(source_lines[item.first_lineno - 2].lstrip()[1:])
    return out


def _whitelisted(item: Any, source_lines: list[str], params: dict[tuple[str, int], int]) -> str | None:
    """Return the whitelist RULE that mutes this item, or None to report it."""
    name = item.name
    if name in PROTOCOL_NAMES:
        return "protocol/framework-called name"
    if name.startswith("__") and name.endswith("__"):
        return "dunder: interpreter-called"
    if name.endswith("_for_tests"):
        return "test-support hook (tests are excluded from the scavenge)"
    def_line = params.get((name, item.first_lineno))
    if def_line is not None and "ARG" in source_lines[def_line - 1]:
        return "signature mandated by an external API (repo's own noqa: ARG)"
    for deco in _decorators_of(item, source_lines):
        if deco.startswith(REGISTRY_DECORATOR_PREFIXES) or "register" in deco:
            return "registry decorator: no static caller by construction"
        if deco.startswith(("property", "staticmethod", "classmethod", "abstractmethod",
                            "overload", "cached_property")):
            return "descriptor/typing decorator"
    if item.typ in {"attribute", "variable", "class"} and Path(item.filename).name.endswith(DECLARATIVE_ATTR_OWNERS):
        return "declarative row/field DSL: read off the class by the framework"
    return None


def dead_symbols(min_confidence: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Returns (report at ``min_confidence``, the callable tier, muted-rule counts).

    Vulture scores unused functions/classes at 60, so the callable tier -- the
    only tier that can name a dead FUNCTION -- sits below any 80 floor and is
    carried separately rather than dropped."""
    from vulture import Vulture

    v = Vulture(verbose=False)
    targets = [str(REPO / "trid3nt_server"), str(REPO / "plugin")]
    v.scavenge(targets, exclude=["*__pycache__*", "*/tests/*", "*/test_*"])

    muted: dict[str, int] = defaultdict(int)
    lines_cache: dict[str, list[str]] = {}
    params_cache: dict[str, dict[tuple[str, int], int]] = {}
    primary: list[dict[str, Any]] = []
    callables: list[dict[str, Any]] = []

    for item in sorted(v.get_unused_code(min_confidence=60),
                       key=lambda i: (str(i.filename), i.first_lineno, i.name)):
        fn = str(item.filename)
        if fn not in lines_cache:
            lines_cache[fn] = Path(fn).read_text(encoding="utf-8", errors="replace").splitlines()
            params_cache[fn] = _param_index(Path(fn))
        rule = _whitelisted(item, lines_cache[fn], params_cache[fn])
        if rule:
            muted[rule] += 1
            continue
        kind = "parameter" if (item.name, item.first_lineno) in params_cache[fn] else item.typ
        row = {
            "symbol": item.name,
            "type": kind,
            "file": str(Path(fn).relative_to(REPO)),
            "line": item.first_lineno,
            "confidence": item.confidence,
            "loc": item.last_lineno - item.first_lineno + 1,
        }
        if item.confidence >= min_confidence:
            primary.append(row)
        elif item.typ in {"function", "method", "class", "property"}:
            callables.append(row)

    primary.sort(key=lambda d: (-d["loc"], d["file"], d["line"]))
    callables.sort(key=lambda d: (-d["loc"], d["file"], d["line"]))
    return primary, callables, dict(muted)


# ---------------------------------------------------------------------------
# Honesty check: this arc's culled modules must be GONE, not merely orphaned
# ---------------------------------------------------------------------------
KNOWN_CULLED: tuple[str, ...] = (
    "trid3nt_server.workflows.shared.soil_hydraulics",
    "trid3nt_server.workflows.shared.water_table_interp",
    "trid3nt_server.workflows.shared.discharge_resolve",
    "trid3nt_server.workflows.shared.publish_quantities",
    "trid3nt_server.workflows.telemac.coastal_tidal_surge",
    "trid3nt_server.workflows.telemac.wave_field",
    "trid3nt_server.workflows.telemac.authoring.coastal",
    "trid3nt_server.workflows.telemac.authoring.wave",
    "trid3nt_server.tools.meta.passthroughs",
    "trid3nt_server.tools.search.qgis_discovery",
    "trid3nt_server.tools.resolution_declared",
    "trid3nt_contracts.swmm_contracts",
    "trid3nt_contracts.modflow_contracts",
    "trid3nt_contracts.geoclaw_contracts",
    "trid3nt_contracts.openquake_contracts",
)


def honesty_check(u: Universe) -> list[str]:
    return sorted(m for m in KNOWN_CULLED if m in u.nodes)


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------

def _evidence(u: Universe, module: str) -> str:
    importers = sorted(imp for imp, tgts in u.edges.items() if module in tgts)
    if not importers:
        return "no importer in any scanned tree"
    return f"imported only by {', '.join(importers[:4])}" + (" ..." if len(importers) > 4 else "")


def _table(rows: list[tuple[str, ...]], header: tuple[str, ...]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def write_outputs(u: Universe, buckets: dict[str, set[str]], dead: list[dict[str, Any]],
                  callables: list[dict[str, Any]], muted: dict[str, int],
                  culled_present: list[str], grimp_unknown: list[str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    edges_out = []
    for importer in sorted(u.edges):
        for imported in sorted(u.edges[importer]):
            kinds = u.edges[importer][imported]
            edges_out.append({
                "importer": importer,
                "imported": imported,
                "static": kinds.get("static", 0),
                "dynamic": kinds.get("dynamic", 0),
                "refs": kinds.get("refs", 0),
            })

    orphans = sorted(
        (n for n in u.nodes.values()
         if not (n.reachable or n.test_only or n.script_only or n.is_test or n.is_marker)),
        key=lambda n: (-n.loc, n.module),
    )
    markers = [n for n in u.nodes.values() if n.is_marker and not n.reachable]
    prod_orphans = [n for n in orphans if not n.is_script]
    script_orphans = [n for n in orphans if n.is_script]
    test_only = sorted((u.nodes[m] for m in buckets["test_only"]), key=lambda n: (-n.loc, n.module))
    script_only = sorted((u.nodes[m] for m in buckets["script_only"]), key=lambda n: (-n.loc, n.module))

    (OUT_DIR / "graph.json").write_text(json.dumps({
        "roots": list(ROOTS),
        "nodes": [
            {
                "module": n.module, "package": n.package, "path": n.path, "loc": n.loc,
                "reachable": n.reachable, "test_only": n.test_only, "script_only": n.script_only,
                "is_test": n.is_test, "is_script": n.is_script, "is_marker": n.is_marker,
                "unresolved_calls": n.unresolved_calls,
            }
            for n in sorted(u.nodes.values(), key=lambda n: n.module)
        ],
        "edges": edges_out,
    }, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    # -- orphans.md
    lines = ["# Orphans -- unreachable from every declared root", "",
             f"Roots: `{'`, `'.join(ROOTS)}`", "",
             "Bucket precedence is roots > tests > scripts, so a module reachable",
             "from both a test and a script is reported as test-only.", "",
             "Unreachable, not imported by any test, not imported by any script.",
             "These are the corpses with nothing holding them up.", "",
             f"Excluded: {len(markers)} unreachable docstring-only `__init__.py` package",
             "markers (directories the runtime walks for data, not modules to import).", ""]
    lines.append(_table(
        [(f"`{n.module}`", str(n.loc), n.path, _evidence(u, n.module)) for n in prod_orphans],
        ("module", "loc", "path", "evidence"),
    ) if prod_orphans else "None.")
    lines += ["", "## Test-only-reachable -- the anchor class", "",
              "Reachable from `tests/` but from no root. The test is the only",
              "thing keeping the module alive; deleting both is one move.", ""]
    lines.append(_table(
        [(f"`{n.module}`", str(n.loc), n.path, _evidence(u, n.module)) for n in test_only],
        ("module", "loc", "path", "evidence"),
    ) if test_only else "None.")
    lines += ["", "## Script-only-reachable", "",
              "Reachable from `scripts/` but from no root and no test -- product",
              "code that survives only because a proof driver imports it.", ""]
    lines.append(_table(
        [(f"`{n.module}`", str(n.loc), n.path, _evidence(u, n.module)) for n in script_only],
        ("module", "loc", "path", "evidence"),
    ) if script_only else "None.")
    lines += ["", "## scripts/ entry modules with no importer", "",
              "Standalone drivers are entry points by design; listed for staleness",
              "review (a driver for a deleted seam is dead), not as a defect.", ""]
    lines.append(_table(
        [(f"`{n.module}`", str(n.loc), n.path) for n in script_orphans],
        ("module", "loc", "path"),
    ) if script_orphans else "None.")
    (OUT_DIR / "orphans.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # -- dead_symbols.md
    lines = ["# Dead symbols -- vulture, min-confidence 80", "",
             "Scope: `trid3nt_server/` + `plugin/`, tests excluded.",
             "Vulture scores unused imports at 90, unused variables/unreachable code at 100,",
             "and unused functions/classes at 60 -- so an 80 floor is an import/variable/",
             "unreachable-code report by construction, and the callable tier is carried",
             "separately below. An unused parameter is reclassified from `variable` to",
             "`parameter`: it is a knob callers still pass, not a dead local.", ""]
    lines.append(_table(
        [(f"`{d['symbol']}`", d["type"], f"{d['file']}:{d['line']}", str(d["confidence"]), str(d["loc"]))
         for d in dead],
        ("symbol", "kind", "file:line", "confidence", "loc"),
    ) if dead else "None.")
    lines += ["", "## Callable tier (confidence 60): unused functions, methods, classes", "",
              "Below the 80 floor because vulture cannot distinguish a dead callable from",
              "one reached dynamically. Treat as candidates, not verdicts.", ""]
    lines.append(_table(
        [(f"`{d['symbol']}`", d["type"], f"{d['file']}:{d['line']}", str(d["loc"]))
         for d in callables],
        ("symbol", "kind", "file:line", "loc"),
    ) if callables else "None.")
    lines += ["", "## Whitelisted false-positive classes", ""]
    lines.append(_table([(rule, str(count)) for rule, count in sorted(muted.items())],
                        ("rule", "muted")) if muted else "None.")
    (OUT_DIR / "dead_symbols.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # -- SUMMARY.md
    matrix: dict[tuple[str, str], int] = defaultdict(int)
    for e in edges_out:
        if e["static"] or e["dynamic"]:
            src, dst = _package_of(e["importer"]), _package_of(e["imported"])
            if src != dst:
                matrix[(src, dst)] += 1

    total_loc = sum(n.loc for n in u.nodes.values())
    live_loc = sum(n.loc for n in u.nodes.values() if n.reachable)
    dyn = sum(1 for e in edges_out if e["dynamic"])
    unresolved = sum(n.unresolved_calls for n in u.nodes.values())
    refs = sum(e["refs"] for e in edges_out)

    lines = [
        "# Code graph -- summary", "",
        "Generated by `scripts/code_graph.py`. Re-run:", "",
        "```", "venvs/agent/bin/python scripts/code_graph.py", "```", "",
        "## Counts", "",
        _table([
            ("modules scanned", str(len(u.nodes))),
            ("total loc", str(total_loc)),
            ("reachable from roots", f"{len(buckets['live'])} ({live_loc} loc)"),
            ("test-only-reachable", str(len(buckets["test_only"]))),
            ("script-only-reachable", str(len(buckets["script_only"]))),
            ("orphans (product)", str(len(prod_orphans))),
            ("package markers excluded from orphans", str(len(markers))),
            ("orphans (scripts/ entry modules)", str(len(script_orphans))),
            ("test modules", str(sum(1 for n in u.nodes.values() if n.is_test))),
            ("import edges", str(sum(1 for e in edges_out if e["static"]))),
            ("dynamic (string-resolved) edges", str(dyn)),
            ("reference call-site edges", str(refs)),
            ("unattributed call sites", str(unresolved)),
            ("dead symbols (conf >= 80)", str(len(dead))),
            ("unused callables (conf 60 tier)", str(len(callables))),
            ("vulture findings muted by whitelist", str(sum(muted.values()))),
        ], ("metric", "value")),
        "", "## Honesty checks", "",
        f"- Known-culled modules from this arc still present: **{len(culled_present)}**"
        + (f" -- {', '.join(culled_present)}" if culled_present else " (all confirmed gone)"),
        f"- grimp modules with no file in the scanned universe: {len(grimp_unknown)}"
        + (f" -- {', '.join(grimp_unknown[:8])}" if grimp_unknown else ""),
        f"- Call sites the import table could not attribute: {unresolved}"
        " (builtins, locals, methods on non-imported objects -- counted, not guessed).",
        "- Out of scope, so its imports anchor nothing: `experiments/`, `third_party/`,"
        " `docs/proof/**/*.py`.",
        "", "## False-positive classes handled", "",
        _table([
            ("flat-namespace worker payload", "`entrypoint.py` does `import artemis_build` for a"
             " sibling flattened into the container's /app; resolved against siblings"),
            ("ship-and-exec driver", "sandbox / mesh / cas drivers are named by bare"
             " `<name>.py` string and run as subprocesses, never imported"),
            ("importlib dotted string", "declaration `resolve=` paths and estimator lookups;"
             " module-level string constants are folded so f-strings resolve"),
            ("docstring-only package marker", "`fetchers/**/__init__.py` marks a directory the"
             " spec walk reads for `source.yaml`; no code to be dead"),
            ("registry decorator (vulture)", "`@register_tool` / `@register_hook` callables have"
             " no static caller by construction"),
        ], ("class", "why the naive graph gets it wrong")),
        "", "## Top 20 orphans by loc", "",
        _table([(f"`{n.module}`", str(n.loc), n.path) for n in prod_orphans[:20]],
               ("module", "loc", "path")) if prod_orphans else "None.",
        "", "## Top 20 test-only-reachable by loc", "",
        _table([(f"`{n.module}`", str(n.loc), n.path) for n in test_only[:20]],
               ("module", "loc", "path")) if test_only else "None.",
        "", "## Top 20 dead symbols by loc", "",
        _table([(f"`{d['symbol']}`", d["type"], f"{d['file']}:{d['line']}", str(d["loc"]))
                for d in dead[:20]], ("symbol", "kind", "file:line", "loc")) if dead else "None.",
        "", "## Package-level edge matrix (cross-package import edges)", "",
    ]
    pkgs = sorted({p for pair in matrix for p in pair})
    header = ("from \\ to", *pkgs)
    rows = [(src, *[str(matrix.get((src, dst), 0)) or "." for dst in pkgs]) for src in pkgs]
    rows = [tuple(c if c != "0" else "." for c in r) for r in rows]
    lines.append(_table(rows, header) if pkgs else "None.")
    (OUT_DIR / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    u = build_universe()
    _PACKAGE_MODULES.update(
        m for m, n in u.nodes.items() if Path(n.path).name == "__init__.py"
    )
    _, grimp_unknown = add_grimp_edges(u)
    add_ast_edges(u)
    buckets = classify(u)
    dead, callables, muted = dead_symbols(min_confidence=80)
    culled_present = honesty_check(u)
    write_outputs(u, buckets, dead, callables, muted, culled_present, grimp_unknown)
    print(f"modules={len(u.nodes)} live={len(buckets['live'])} "
          f"test_only={len(buckets['test_only'])} script_only={len(buckets['script_only'])} "
          f"dead_symbols={len(dead)} -> {OUT_DIR.relative_to(REPO)}")
    return 1 if culled_present else 0


if __name__ == "__main__":
    sys.exit(main())
