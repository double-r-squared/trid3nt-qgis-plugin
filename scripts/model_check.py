#!/usr/bin/env python3
"""Check a SysML v2 textual model against the tree it describes.

A model nobody can check rots. This reads the project's own subset of SysML v2
textual notation - part def, part, port def, port, interface def / item,
interface (connect), requirement def, satisfy, verify - and validates THREE
project conformance rules against the live code:

  (a) every declared interface item is WRITTEN by at least one of the interface's
      source blocks and READ by at least one of its target blocks, resolved by
      searching the modules those blocks are bound to;
  (b) every ``verify`` names a test that exists, resolved by parsing the named
      test file rather than importing it;
  (c) every ``forbid:`` dependency rule holds against the measured import graph.

SCOPE: this checker validates THIS PROJECT'S conformance rules against the tree.
It is not a SysML implementation - it resolves no inheritance, types no feature
and evaluates no expression. An item's type word is recorded for the view and
never interpreted.

Two doc-line conventions carry what the notation has no place for. A part usage
names the module it IS with ``code: <repo-relative path>``; a requirement def
states a dependency rule with ``forbid: <importer prefix> -> <imported prefix>``.

Output is deterministic and sorted. Every finding names the model element and
the code location, and the exit status is 1 when any finding stands.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_MODEL = REPO_ROOT / "docs" / "model" / "solve-seam.sysml"
DEFAULT_VIEW = REPO_ROOT / "docs" / "model" / "solve-seam-view.md"
DEFAULT_GRAPH = REPO_ROOT / "docs" / "validation" / "code-graph" / "graph.json"


# --------------------------------------------------------------------------- #
# The notation subset
# --------------------------------------------------------------------------- #

#: One ``doc /* ... */`` block. Captured whole: the conformance doc-lines are
#: read out of the body, and the rest is prose the view carries.
_DOC = re.compile(r"doc\s*/\*(.*?)\*/", re.DOTALL)
#: A ``key: value`` line inside a doc body. The two keys are the model's own
#: binding vocabulary; anything else in a doc body is prose.
_DOCLINE = re.compile(r"^\s*(code|forbid)\s*:\s*(\S.*?)\s*$", re.MULTILINE)
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
    rf"connect\s+({_NAME})\.({_NAME})\s+to\s+({_NAME})\.({_NAME})\s*;",
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
        interface_defs[match.group(1)] = InterfaceDef(
            match.group(1), _doc_of(body).strip(), items)

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

    return Model(
        package=package.group(1),
        part_defs=[m.group(1) for m in _PART_DEF.finditer(scan)],
        port_defs=[m.group(1) for m in _PORT_DEF.finditer(scan)],
        interface_defs=interface_defs,
        requirement_defs=requirement_defs,
        parts=parts,
        interfaces=[InterfaceUsage(*m) for m in _IFACE_USE.findall(scan)],
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
    """Rule (a): every declared item is written somewhere and read somewhere."""
    sources: dict[str, set[str]] = {}
    targets: dict[str, set[str]] = {}
    for use in model.interfaces:
        src, dst = model.parts.get(use.src_part), model.parts.get(use.dst_part)
        if src is None or dst is None:
            continue
        sources.setdefault(use.def_name, set()).add(use.src_part)
        # A hop whose two ends are the SAME module carries no evidence: a
        # module reading back what it wrote satisfies any key it happens to
        # mention, which is how a severed interface passes a check.
        if src.code != dst.code:
            targets.setdefault(use.def_name, set()).add(use.dst_part)

    cache: dict[str, frozenset[str]] = {}

    def read(part_name: str) -> tuple[str, frozenset[str]]:
        path = model.parts[part_name].code or ""
        if path not in cache:
            cache[path] = code_names(root / path)
        return path, cache[path]

    out: list[Finding] = []
    for def_name in sorted(model.interface_defs):
        spec = model.interface_defs[def_name]
        writers = sorted(sources.get(def_name, set()))
        readers = sorted(targets.get(def_name, set()))
        if not writers or not readers:
            out.append(Finding(
                "INTERFACE_UNCONNECTED", def_name, "docs/model",
                f"has {len(writers)} writer(s) and {len(readers)} consumer(s); "
                "an interface nothing connects describes nothing"))
            continue
        for missing_path in sorted(p for p in writers + readers
                                   if not (root / (model.parts[p].code or "")).is_file()):
            out.append(Finding(
                "BLOCK_CODE_MISSING", missing_path,
                model.parts[missing_path].code or "<unbound>",
                "the module this block is bound to is not in the tree"))
        for item in spec.items:
            written = [p for p in writers if item.name in read(p)[1]]
            consumed = [p for p in readers if item.name in read(p)[1]]
            if not written:
                out.append(Finding(
                    "ITEM_NO_WRITER", f"{def_name}.{item.name}",
                    ", ".join(read(p)[0] for p in writers),
                    "no source block of this interface names it"))
            if not consumed:
                out.append(Finding(
                    "ITEM_NO_CONSUMER", f"{def_name}.{item.name}",
                    ", ".join(read(p)[0] for p in readers),
                    "no target block of this interface names it, so nothing "
                    "reads what this side writes"))
    return out


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


def _dependency_findings(model: Model, graph_path: Path) -> list[Finding]:
    """Rule (c): every forbid rule holds against the measured import graph."""
    if not graph_path.is_file():
        return [Finding("GRAPH_MISSING", "forbid rules", str(graph_path),
                        "the measured import graph is not in the tree, so no "
                        "dependency rule can be checked")]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    edges: list[dict[str, Any]] = graph.get("edges") or []
    out: list[Finding] = []
    for name in sorted(model.requirement_defs):
        for importer_prefix, imported_prefix in model.requirement_defs[name].forbids:
            for edge in edges:
                importer, imported = str(edge.get("importer") or ""), str(
                    edge.get("imported") or "")
                if _under(importer, importer_prefix) and _under(imported,
                                                                imported_prefix):
                    out.append(Finding(
                        "DEPENDENCY_VIOLATION", name, f"{importer} -> {imported}",
                        f"forbidden by '{importer_prefix} -> {imported_prefix}'"))
    return out


def _under(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def check(model: Model, root: Path, graph_path: Path) -> list[Finding]:
    return sorted(_structure_findings(model)
                  + _interface_findings(model, root)
                  + _verify_findings(model, root)
                  + _dependency_findings(model, graph_path))


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
        lines.append(f"    {use.src_part} -- \"{use.def_name}\" --> {use.dst_part}")
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
    parser.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    parser.add_argument("--view", type=Path, nargs="?", const=DEFAULT_VIEW,
                        help="Write the derived view to this path and exit.")
    args = parser.parse_args(argv)

    try:
        model = parse_model(args.model)
    except ModelParseError as exc:
        print(f"MODEL_PARSE {args.model} [{args.model}] {exc}")
        return 1

    if args.view is not None:
        args.view.write_text(render_view(model, args.model), encoding="utf-8")
        print(f"view -> {args.view}")
        return 0

    findings = check(model, args.root, args.graph)
    for finding in findings:
        print(finding.render())
    print(f"checked {len(model.parts)} blocks, "
          f"{len(model.interface_defs)} interfaces, "
          f"{sum(len(i.items) for i in model.interface_defs.values())} items, "
          f"{len(model.requirement_defs)} requirements, "
          f"{len(set(model.verifies))} verifications: {len(findings)} finding(s)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
