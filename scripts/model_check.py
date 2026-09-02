#!/usr/bin/env python3
"""Check a SysML v2 textual model against the tree it describes.

A model nobody can check rots. This reads the project's own subset of SysML v2
textual notation - part def, part, port def, port, interface def / item,
interface (connect), requirement def, satisfy, verify - and validates FOUR
project conformance rules against the live code:

  (a) every non-optional item of every interface USAGE is named by the module at
      the hop's writer end and by the module at its consumer end. Per usage, not
      per definition: evidence pooled across hops leaves a single-module
      severance invisible, because a sibling hop keeps supplying the item;
  (b) every ``verify`` names a test that exists, resolved by parsing the named
      test file rather than importing it;
  (c) every ``forbid:`` dependency rule holds against the import edges of the
      modeled modules, computed here at check time;
  (d) every tree module that calls a modeled contract's constructor is bound to
      a usage of that contract - an author nobody modeled is a writer no
      severance check covers.

SCOPE: this checker validates THIS PROJECT'S conformance rules against the tree.
It is not a SysML implementation - it resolves no inheritance, types no feature
and evaluates no expression. An item's type word is recorded for the view and
never interpreted.

Four doc-line conventions carry what the notation has no place for. A part usage
names the module it IS with ``code: <repo-relative path>``; a requirement def
states a dependency rule with ``forbid: <importer prefix> -> <imported prefix>``;
an interface def names a function that builds it with ``constructor: <name>``; an
interface usage exempts a verbatim-forwarding end with
``pass-through: <part usage>``, which neither owes nor supplies item evidence.

Output is deterministic and sorted. Every finding names the model element and
the code location, and the exit status is 1 when any finding stands.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_MODEL = REPO_ROOT / "docs" / "model" / "solve-seam.sysml"

#: ``--view`` with no path: the view belongs beside the model it is derived from,
#: one per seam. A fixed default would write one seam's view over another's.
_BESIDE_THE_MODEL = Path("-")


# --------------------------------------------------------------------------- #
# The notation subset
# --------------------------------------------------------------------------- #

#: One ``doc /* ... */`` block. Captured whole: the conformance doc-lines are
#: read out of the body, and the rest is prose the view carries.
_DOC = re.compile(r"doc\s*/\*(.*?)\*/", re.DOTALL)
#: A ``key: value`` line inside a doc body. These keys are the model's own
#: binding vocabulary; anything else in a doc body is prose.
_DOCLINE = re.compile(
    r"^\s*(code|forbid|constructor|pass-through)\s*:\s*(\S.*?)\s*$", re.MULTILINE)
_FORBID = re.compile(r"^(\S+)\s*->\s*(\S+)$")

_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_MULT = r"(?:\[\s*\d+\s*\.\.\s*[\d*]+\s*\])"

_PACKAGE = re.compile(rf"^\s*package\s+({_NAME})\s*\{{", re.MULTILINE)
_PART_DEF = re.compile(rf"^\s*part\s+def\s+({_NAME})\s*(?:\{{|;)", re.MULTILINE)
_PORT_DEF = re.compile(rf"^\s*port\s+def\s+({_NAME})\s*(?:\{{|;)", re.MULTILINE)
_IFACE_DEF = re.compile(rf"^\s*interface\s+def\s+({_NAME})\s*\{{", re.MULTILINE)
_REQ_DEF = re.compile(rf"^\s*requirement\s+def\s+({_NAME})\s*\{{", re.MULTILINE)
_PART_USE = re.compile(rf"^\s*part\s+({_NAME})\s*:\s*({_NAME})\s*\{{", re.MULTILINE)
_ITEM = re.compile(
    rf"^\s*item\s+({_NAME})\s*:\s*({_NAME})\s*({_MULT})?\s*;", re.MULTILINE)
_PORT_USE = re.compile(rf"^\s*port\s+({_NAME})\s*:\s*({_NAME})\s*;", re.MULTILINE)
_IFACE_USE = re.compile(
    rf"^\s*interface\s+({_NAME})\s*:\s*({_NAME})\s*"
    rf"connect\s+({_NAME})\.({_NAME})\s+to\s+({_NAME})\.({_NAME})\s*(\{{|;)",
    re.MULTILINE)
_SATISFY = re.compile(
    rf"^\s*satisfy\s+requirement\s+({_NAME})\s+by\s+({_NAME})\s*;", re.MULTILINE)
_VERIFY = re.compile(
    rf'^\s*verify\s+requirement\s+({_NAME})\s+by\s*"([^"]+)"\s*;',
    re.MULTILINE | re.DOTALL)

#: Every construct the subset admits, as its opening keyword sequence. A line
#: opening with anything else is notation this checker does not read, and
#: silently skipping it would validate a model nobody has checked.
_KNOWN_OPENERS = (
    "package", "part def", "port def", "interface def", "requirement def",
    "part ", "port ", "item ", "interface ", "connect ", "satisfy ", "verify ",
    "doc ",
)


class ModelParseError(ValueError):
    """The model carries notation outside the subset this checker reads."""


@dataclass(frozen=True)
class Item:
    name: str
    type_word: str
    optional: bool


@dataclass
class InterfaceDef:
    name: str
    doc: str
    items: list[Item] = field(default_factory=list)
    #: Functions that BUILD this contract. A tree module calling one of them is
    #: an author of the contract and must be bound to a usage of it.
    constructors: list[str] = field(default_factory=list)


@dataclass
class PartUsage:
    name: str
    type_name: str
    doc: str
    code: str | None
    ports: dict[str, str] = field(default_factory=dict)


@dataclass
class InterfaceUsage:
    name: str
    def_name: str
    src_part: str
    src_port: str
    dst_part: str
    dst_port: str
    #: Ends that forward this contract verbatim. Such an end neither owes item
    #: evidence nor supplies any: a conduit names no key it carries.
    pass_through: frozenset[str] = frozenset()


@dataclass
class RequirementDef:
    name: str
    doc: str
    forbids: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Model:
    package: str
    part_defs: list[str]
    port_defs: list[str]
    interface_defs: dict[str, InterfaceDef]
    requirement_defs: dict[str, RequirementDef]
    parts: dict[str, PartUsage]
    interfaces: list[InterfaceUsage]
    satisfies: list[tuple[str, str]]
    verifies: list[tuple[str, str]]


def _body(text: str, open_at: int) -> str:
    """The braced body starting at the brace at or after ``open_at``."""
    start = text.index("{", open_at)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    raise ModelParseError(f"unclosed brace at offset {start}")


def _doc_of(body: str) -> str:
    found = _DOC.search(body)
    return found.group(1) if found else ""


def _own_doc(body: str) -> str:
    """The block's OWN doc: the one before any nested member opens.

    A nested part's doc would otherwise be read as its parent's, which is how a
    block silently inherits another block's code binding.
    """
    found = _DOC.search(body)
    if found is None:
        return ""
    nested = re.search(r"^\s*(?:part|port|item|interface)\s", body, re.MULTILINE)
    if nested is not None and nested.start() < found.start():
        return ""
    return found.group(1)


def _strip_docs(text: str) -> str:
    """The model with every doc body blanked, line count preserved.

    Doc prose is free text: scanning it for constructs would parse sentences.
    """
    return _DOC.sub(lambda m: "doc /*" + re.sub(r"[^\n]", " ", m.group(1)) + "*/",
                    text)


def _reject_unknown_notation(text: str) -> None:
    """Every STATEMENT opens with a construct of the subset, or the model refuses.

    Statements, not lines: a declaration wraps, and rejecting per line would
    read its continuation as notation of its own.
    """
    stripped = re.sub(r"//[^\n]*", "", text)
    stripped = _DOC.sub(" ", stripped)
    for statement in re.split(r"[;{}]", stripped):
        line = " ".join(statement.split())
        if not line:
            continue
        if any(line.startswith(opener) for opener in _KNOWN_OPENERS):
            continue
        raise ModelParseError(
            f"{line[:70]!r} is outside the subset this checker reads "
            f"({', '.join(o.strip() for o in _KNOWN_OPENERS)})")


def parse_model(path: Path) -> Model:
    """The model file -> the parsed subset. Anything else refuses."""
    text = path.read_text(encoding="utf-8")
    scan = _strip_docs(text)
    _reject_unknown_notation(scan)

    package = _PACKAGE.search(scan)
    if package is None:
        raise ModelParseError("the model declares no package")

    interface_defs: dict[str, InterfaceDef] = {}
    for match in _IFACE_DEF.finditer(scan):
        body = _body(text, match.start())
        items = [Item(name, type_word, bool(mult))
                 for name, type_word, mult in _ITEM.findall(_strip_docs(body))]
        doc = _doc_of(body)
        interface_defs[match.group(1)] = InterfaceDef(
            match.group(1), doc.strip(), items,
            [v for k, v in _DOCLINE.findall(doc) if k == "constructor"])

    requirement_defs: dict[str, RequirementDef] = {}
    for match in _REQ_DEF.finditer(scan):
        doc = _doc_of(_body(text, match.start()))
        forbids: list[tuple[str, str]] = []
        for key, value in _DOCLINE.findall(doc):
            if key != "forbid":
                continue
            rule = _FORBID.match(value)
            if rule is None:
                raise ModelParseError(
                    f"requirement {match.group(1)}: forbid rule {value!r} is not "
                    "'<importer prefix> -> <imported prefix>'")
            forbids.append((rule.group(1), rule.group(2)))
        requirement_defs[match.group(1)] = RequirementDef(
            match.group(1), _prose(doc), forbids)

    parts: dict[str, PartUsage] = {}
    for match in _PART_USE.finditer(scan):
        body = _body(text, match.start())
        doc = _own_doc(body)
        code = next((v for k, v in _DOCLINE.findall(doc) if k == "code"), None)
        parts[match.group(1)] = PartUsage(
            match.group(1), match.group(2), _prose(doc), code,
            dict(_PORT_USE.findall(_strip_docs(body))))

    interfaces: list[InterfaceUsage] = []
    for match in _IFACE_USE.finditer(scan):
        marks: list[str] = []
        if match.group(7) == "{":
            doc = _doc_of(_body(text, match.end() - 1))
            marks = [v for k, v in _DOCLINE.findall(doc) if k == "pass-through"]
        interfaces.append(InterfaceUsage(*match.groups()[:6], frozenset(marks)))

    return Model(
        package=package.group(1),
        part_defs=[m.group(1) for m in _PART_DEF.finditer(scan)],
        port_defs=[m.group(1) for m in _PORT_DEF.finditer(scan)],
        interface_defs=interface_defs,
        requirement_defs=requirement_defs,
        parts=parts,
        interfaces=interfaces,
        satisfies=_SATISFY.findall(scan),
        verifies=[(r, re.sub(r"\s+", "", t)) for r, t in _VERIFY.findall(scan)],
    )


def _prose(doc: str) -> str:
    """A doc body with the binding lines removed and the wrapping normalized."""
    body = _DOCLINE.sub("", doc)
    return " ".join(body.split())


# --------------------------------------------------------------------------- #
# Findings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, order=True)
class Finding:
    code: str
    element: str
    location: str
    message: str

    def render(self) -> str:
        return f"{self.code} {self.element} [{self.location}] {self.message}"


def _structure_findings(model: Model) -> list[Finding]:
    """The model referring to itself: every name it uses, it declares."""
    out: list[Finding] = []
    part_defs, port_defs = set(model.part_defs), set(model.port_defs)
    for part in model.parts.values():
        if part.type_name not in part_defs:
            out.append(Finding("MODEL_UNRESOLVED", part.name, "docs/model",
                               f"types as undeclared part def {part.type_name!r}"))
        if part.code is None:
            out.append(Finding("MODEL_UNBOUND", part.name, "docs/model",
                               "carries no 'code:' doc line, so nothing in the "
                               "tree can be checked against it"))
        for port, port_type in sorted(part.ports.items()):
            if port_type not in port_defs:
                out.append(Finding(
                    "MODEL_UNRESOLVED", f"{part.name}.{port}", "docs/model",
                    f"types as undeclared port def {port_type!r}"))
    for use in model.interfaces:
        if use.def_name not in model.interface_defs:
            out.append(Finding("MODEL_UNRESOLVED", use.name, "docs/model",
                               f"uses undeclared interface def {use.def_name!r}"))
        for part_name, port_name in ((use.src_part, use.src_port),
                                     (use.dst_part, use.dst_port)):
            part = model.parts.get(part_name)
            if part is None:
                out.append(Finding("MODEL_UNRESOLVED", use.name, "docs/model",
                                   f"connects undeclared part {part_name!r}"))
            elif port_name not in part.ports:
                out.append(Finding(
                    "MODEL_UNRESOLVED", use.name, "docs/model",
                    f"connects {part_name}.{port_name}, which that part has no "
                    "port for"))
        for marked in sorted(use.pass_through):
            if marked not in (use.src_part, use.dst_part):
                out.append(Finding(
                    "MODEL_UNRESOLVED", use.name, "docs/model",
                    f"marks {marked!r} pass-through, which is neither end of it"))
    for req, part_name in model.satisfies:
        if req not in model.requirement_defs:
            out.append(Finding("MODEL_UNRESOLVED", f"satisfy {req}", "docs/model",
                               "names no declared requirement def"))
        if part_name not in model.parts:
            out.append(Finding("SATISFY_UNKNOWN_BLOCK", f"satisfy {req}",
                               "docs/model", f"allocates to undeclared block "
                               f"{part_name!r}"))
    for req, _test in model.verifies:
        if req not in model.requirement_defs:
            out.append(Finding("MODEL_UNRESOLVED", f"verify {req}", "docs/model",
                               "names no declared requirement def"))
    for name in sorted(model.requirement_defs):
        if not any(req == name for req, _ in model.satisfies):
            out.append(Finding("REQUIREMENT_UNALLOCATED", name, "docs/model",
                               "no block satisfies it"))
        if not any(req == name for req, _ in model.verifies):
            out.append(Finding("REQUIREMENT_UNVERIFIED", name, "docs/model",
                               "no test verifies it"))
    return out


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Every node that IS a docstring, by identity.

    Prose is not a reader. A key a module only mentions in its own
    documentation is exactly the severed interface this check exists to find,
    so docstrings are excluded from what counts as naming an item.
    """
    marked: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        first = body[0] if body else None
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            marked.add(id(first.value))
    return marked


def code_names(path: Path) -> frozenset[str]:
    """Every name a module NAMES: identifiers, attributes, arguments, literals.

    Read structurally rather than by text search, so a comment or a docstring
    mentioning a key never passes for a writer or a reader of it.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    if path.suffix != ".py":
        return frozenset(_WORD.findall(text))
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return frozenset(_WORD.findall(text))
    docstrings = _docstring_nodes(tree)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                names.update(_WORD.findall(node.value))
    return frozenset(names)


def _interface_findings(model: Model, root: Path) -> list[Finding]:
    """Rule (a): every hop's two ends name every non-optional item it carries.

    Per USAGE. Pooling evidence across the hops that share an interface
    definition lets one module drop a key while a sibling hop keeps supplying
    it - which is the severed interface this check exists to catch.
    """
    cache: dict[str, frozenset[str]] = {}

    def named_by(part_name: str) -> tuple[str, frozenset[str]]:
        path = model.parts[part_name].code or ""
        if path not in cache:
            cache[path] = code_names(root / path)
        return path, cache[path]

    out: list[Finding] = []

    for def_name in sorted(model.interface_defs):
        if not any(use.def_name == def_name for use in model.interfaces):
            out.append(Finding(
                "INTERFACE_UNCONNECTED", def_name, "docs/model",
                "no usage connects it, so it describes nothing"))

    for use in sorted(model.interfaces, key=lambda u: u.name):
        spec = model.interface_defs.get(use.def_name)
        src, dst = model.parts.get(use.src_part), model.parts.get(use.dst_part)
        if spec is None or src is None or dst is None:
            continue
        ends = [("ITEM_NO_WRITER", use.src_part,
                 "the writer end of this hop does not name it")]
        # A hop whose two ends are the SAME module carries no consumer
        # evidence: a module reading back what it wrote satisfies any key it
        # happens to mention, which is how a severed interface passes a check.
        if src.code != dst.code:
            ends.append(("ITEM_NO_CONSUMER", use.dst_part,
                         "the consumer end of this hop does not name it, so "
                         "nothing reads what the writer states"))
        for code, part_name, message in ends:
            # A verbatim-forwarding end carries the contract without naming any
            # of it. It owes no evidence and supplies none.
            if part_name in use.pass_through:
                continue
            path, names = named_by(part_name)
            for item in spec.items:
                if item.optional or item.name in names:
                    continue
                out.append(Finding(code, f"{use.name}.{item.name}", path,
                                   message))

    for part_name in sorted(model.parts):
        path = model.parts[part_name].code
        if path is not None and not (root / path).is_file():
            out.append(Finding("BLOCK_CODE_MISSING", part_name, path,
                               "the module this block is bound to is not in "
                               "the tree"))
    return out


def _author_findings(model: Model, root: Path) -> list[Finding]:
    """Rule (d): every tree module that builds a modeled contract is modeled.

    Scoped to the top-level trees the model's own blocks live in, and to product
    modules: a test calls a contract's constructor to exercise it, not to author
    a live case.
    """
    constructors: dict[str, str] = {}
    for def_name in sorted(model.interface_defs):
        for name in model.interface_defs[def_name].constructors:
            constructors[name] = def_name
    if not constructors:
        return []

    bound: dict[str, set[str]] = {}
    for use in model.interfaces:
        for part_name in (use.src_part, use.dst_part):
            part = model.parts.get(part_name)
            if part is not None and part.code:
                bound.setdefault(part.code, set()).add(use.def_name)

    out: list[Finding] = []
    for path in _product_modules(model, root):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            if not any(name in text for name in constructors):
                continue
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            continue
        for name in sorted(_called_names(tree) & set(constructors)):
            def_name = constructors[name]
            if def_name in bound.get(rel, set()):
                continue
            out.append(Finding(
                "UNMODELED_AUTHOR", def_name, rel,
                f"calls {name}(), so it authors this contract, but no usage of "
                "it binds this module - its severances are unchecked"))
    return out


def _product_modules(model: Model, root: Path) -> list[Path]:
    """Every product module under the trees the model's own blocks live in."""
    roots = sorted({(part.code or "").split("/")[0]
                    for part in model.parts.values() if part.code})
    found: list[Path] = []
    for name in roots:
        for path in sorted((root / name).rglob("*.py")):
            parts = path.relative_to(root).parts
            if any(p in ("tests", "__pycache__") for p in parts):
                continue
            if path.name.startswith("test_"):
                continue
            found.append(path)
    return found


def _called_names(tree: ast.AST) -> set[str]:
    """Every function name CALLED, plain or attribute."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def _test_names(path: Path) -> set[str]:
    """Every test function a file declares, read without importing it."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                names.add(node.name)
    return names


def _verify_findings(model: Model, root: Path) -> list[Finding]:
    """Rule (b): every verify names a test that exists."""
    out: list[Finding] = []
    for req, node_id in sorted(set(model.verifies)):
        file_part, _, test_part = node_id.partition("::")
        path = root / file_part
        if not path.is_file():
            out.append(Finding("VERIFY_TEST_MISSING", f"verify {req}", file_part,
                               "the test file does not exist"))
            continue
        if test_part.split("[")[0] not in _test_names(path):
            out.append(Finding("VERIFY_TEST_MISSING", f"verify {req}", node_id,
                               "the file declares no test by that name"))
    return out


def scoped_import_edges(model: Model, root: Path) -> list[tuple[str, str]]:
    """The import edges of the modeled modules, computed here.

    Fresh at check time and scoped to the blocks the model binds. A committed
    graph is an instrument's product: reading one makes the dependency rules
    decorative the moment the instrument was last run before the code moved.
    """
    edges: set[tuple[str, str]] = set()
    for part in model.parts.values():
        if not part.code:
            continue
        path = root / part.code
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        importer = re.sub(r"\.py$", "", part.code).replace("/", ".")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                edges.update((importer, alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                edges.add((importer, _imported_module(importer, node)))
    return sorted(edges)


def _imported_module(importer: str, node: ast.ImportFrom) -> str:
    """The dotted module an ``from ... import`` names, relative form resolved."""
    if not node.level:
        return node.module or ""
    base = importer.split(".")[:-node.level]
    return ".".join(base + ([node.module] if node.module else []))


def _dependency_findings(edges: list[tuple[str, str]],
                         model: Model) -> list[Finding]:
    """Rule (c): every forbid rule holds against the modeled modules' imports."""
    out: list[Finding] = []
    for name in sorted(model.requirement_defs):
        for importer_prefix, imported_prefix in model.requirement_defs[name].forbids:
            for importer, imported in edges:
                if _under(importer, importer_prefix) and _under(imported,
                                                                imported_prefix):
                    out.append(Finding(
                        "DEPENDENCY_VIOLATION", name, f"{importer} -> {imported}",
                        f"forbidden by '{importer_prefix} -> {imported_prefix}'"))
    return out


def _under(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def check(model: Model, root: Path) -> list[Finding]:
    return sorted(_structure_findings(model)
                  + _interface_findings(model, root)
                  + _author_findings(model, root)
                  + _verify_findings(model, root)
                  + _dependency_findings(scoped_import_edges(model, root), model))


# --------------------------------------------------------------------------- #
# The derived view
# --------------------------------------------------------------------------- #

def render_view(model: Model, source: Path) -> str:
    """The model as a page: the flow graph, the item tables, the allocations.

    Every line is derived, so a diagram can never describe a seam the model no
    longer states.
    """
    lines: list[str] = [
        f"# {model.package} - derived view",
        "",
        f"GENERATED from `{source.relative_to(REPO_ROOT).as_posix()}` by "
        "`scripts/model_check.py --view`. Never hand-edited: regenerate it, and "
        "`tests/test_model_conformance.py` fails while it is stale.",
        "",
        "## Blocks and flows",
        "",
        "```mermaid",
        "flowchart LR",
    ]
    for name in sorted(model.parts):
        part = model.parts[name]
        lines.append(f'    {name}["{part.type_name}<br/>{part.code}"]')
    for use in sorted(model.interfaces, key=lambda u: u.name):
        # The pass-through marks ride on the edge: an end that owes no item
        # evidence is exactly what a reader of this picture needs told.
        mark = (f" ({', '.join(sorted(use.pass_through))} pass through)"
                if use.pass_through else "")
        lines.append(
            f"    {use.src_part} -- \"{use.def_name}{mark}\" --> {use.dst_part}")
    lines += ["```", "", "## Interface items", ""]
    for def_name in sorted(model.interface_defs):
        spec = model.interface_defs[def_name]
        lines += [f"### `{def_name}`", "", _prose(spec.doc), "",
                  "| item | type | required |", "| --- | --- | --- |"]
        for item in spec.items:
            lines.append(f"| `{item.name}` | {item.type_word} | "
                         f"{'optional' if item.optional else 'required'} |")
        lines.append("")
    lines += ["## Requirements", "",
              "| requirement | satisfied by | verified by |",
              "| --- | --- | --- |"]
    for name in sorted(model.requirement_defs):
        blocks = ", ".join(f"`{p}`" for r, p in model.satisfies if r == name)
        tests = "<br/>".join(f"`{t}`" for r, t in model.verifies if r == name)
        lines.append(f"| **{name}** | {blocks} | {tests} |")
    lines += ["", "## What each requirement says", ""]
    for name in sorted(model.requirement_defs):
        lines += [f"- **{name}** - {model.requirement_defs[name].doc}"]
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="model_check",
        description="Check a SysML v2 textual model against the tree.")
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--view", type=Path, nargs="?", const=_BESIDE_THE_MODEL,
                        help="Write the derived view to this path and exit; with "
                             "no path, beside the model as <stem>-view.md.")
    args = parser.parse_args(argv)
    # The view names the model it came from, so the name a relative invocation
    # writes has to be the name an absolute one writes.
    args.model = args.model.resolve()

    try:
        model = parse_model(args.model)
    except ModelParseError as exc:
        print(f"MODEL_PARSE {args.model} [{args.model}] {exc}")
        return 1

    if args.view is not None:
        view = (args.model.with_name(f"{args.model.stem}-view.md")
                if args.view == _BESIDE_THE_MODEL else args.view)
        view.write_text(render_view(model, args.model), encoding="utf-8")
        print(f"view -> {view}")
        return 0

    findings = check(model, args.root)
    for finding in findings:
        print(finding.render())
    print(f"checked {len(model.parts)} blocks, "
          f"{len(model.interfaces)} interface usages, "
          f"{sum(len(i.items) for i in model.interface_defs.values())} items, "
          f"{len(model.requirement_defs)} requirements, "
          f"{len(set(model.verifies))} verifications: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
