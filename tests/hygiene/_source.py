"""Prose sources the hygiene guards read: docstrings, comments, markdown.

Scope is the tracked Python under the product trees plus the tracked markdown;
an untracked or gitignored file is not the repo's prose and is never scanned.
"""

from __future__ import annotations

import ast
import inspect
import io
import subprocess
import tokenize
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

PRODUCT_TREES = ("trid3nt_server", "plugin", "contracts", "scripts", "tests")

DOCSTRING_EXEMPT_MARKER = "# docstring-exempt:"

MODULE_CONTENT_LIMIT = 5
SYMBOL_CONTENT_LIMIT = 3
LLM_FRONT_BUDGET = 1000


@dataclass(frozen=True)
class Docstring:
    path: Path
    kind: str
    name: str
    lineno: int
    text: str
    llm_facing: bool
    exempt_reason: str | None
    header: int

    @property
    def rel(self) -> str:
        return str(self.path.relative_to(REPO_ROOT))

    @property
    def content_lines(self) -> int:
        return len([line for line in self.text.splitlines() if line.strip()])

    @property
    def limit(self) -> int:
        return MODULE_CONTENT_LIMIT if self.kind == "module" else SYMBOL_CONTENT_LIMIT

    @property
    def budget_chars(self) -> int:
        return len(inspect.cleandoc(self.text))


@dataclass(frozen=True)
class Prose:
    path: Path
    lineno: int
    text: str
    origin: str

    @property
    def rel(self) -> str:
        return str(self.path.relative_to(REPO_ROOT))


@lru_cache(maxsize=1)
def _tracked() -> tuple[str, ...]:
    listing = subprocess.run(
        ["git", "ls-files", "-z", *PRODUCT_TREES],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return tuple(p for p in listing.split("\0") if p)


def python_files() -> list[Path]:
    return [REPO_ROOT / p for p in _tracked() if p.endswith(".py")]


def markdown_files() -> list[Path]:
    listing = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    return [REPO_ROOT / p for p in listing.split("\0") if p]


def _decorator_base(node: ast.expr) -> str | None:
    target = node.func if isinstance(node, ast.Call) else node
    while isinstance(target, ast.Attribute):
        target = target.value
    return target.id if isinstance(target, ast.Name) else None


def _llm_facing_names(tree: ast.Module) -> set[str]:
    # Two seams reach the model: a body register_tool wraps (decorator or
    # explicit rebinding), and a docstring assigned through __doc__ - including
    # the function a tool's __doc__ is copied FROM, which the model reads too.
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                if _decorator_base(dec) == "register_tool":
                    names.add(node.name)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "__doc__":
                    base = _decorator_base(target.value)
                    if base:
                        names.add(base)
            for sub in ast.walk(node.value):
                if isinstance(sub, ast.Attribute) and sub.attr == "__doc__":
                    base = _decorator_base(sub.value)
                    if base:
                        names.add(base)
                if isinstance(sub, ast.Call) and _decorator_base(sub.func) == "register_tool":
                    names.update(a.id for a in sub.args if isinstance(a, ast.Name))
    return names


def _exempt_markers(source: str) -> dict[int, str]:
    """Marker -> the line it governs: the first code line under the comment block."""
    lines = source.splitlines()
    markers: dict[int, str] = {}
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped.startswith(DOCSTRING_EXEMPT_MARKER):
            index += 1
            continue
        parts = [stripped[len(DOCSTRING_EXEMPT_MARKER):].strip()]
        cursor = index + 1
        while cursor < len(lines):
            follow = lines[cursor].strip()
            if not follow.startswith("#") or follow.startswith(DOCSTRING_EXEMPT_MARKER):
                break
            parts.append(follow.lstrip("#").strip())
            cursor += 1
        governed = cursor
        while governed < len(lines) and not lines[governed].strip():
            governed += 1
        markers[governed + 1] = " ".join(part for part in parts if part)
        index = cursor
    return markers


def docstrings(path: Path) -> list[Docstring]:
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    llm = _llm_facing_names(tree)
    markers = _exempt_markers(source)
    found: list[Docstring] = []

    def add(kind: str, name: str, node: ast.AST, header: int) -> None:
        text = ast.get_docstring(node, clean=False)
        if text is None:
            return
        lineno = node.body[0].lineno
        found.append(
            Docstring(path, kind, name, lineno, text, name in llm, markers.get(header), header)
        )

    if tree.body:
        add("module", path.name, tree, tree.body[0].lineno)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            header = min([d.lineno for d in node.decorator_list] + [node.lineno])
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            add(kind, node.name, node, header)
    return found


def _comments(path: Path) -> list[tuple[int, int, str]]:
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []
    return [(t.start[0], t.start[1], t.string) for t in tokens if t.type == tokenize.COMMENT]


def comment_blocks(path: Path) -> list[Prose]:
    """Runs of adjacent full-line comments, joined; a trailing comment is not one."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    blocks: list[Prose] = []
    run: list[str] = []
    start = 0
    previous = -2
    for lineno, _col, text in _comments(path):
        if lineno > len(lines) or not lines[lineno - 1].lstrip().startswith("#"):
            continue
        if lineno != previous + 1:
            if run:
                blocks.append(Prose(path, start, "\n".join(run), "comment block"))
            run, start = [], lineno
        run.append(text)
        previous = lineno
    if run:
        blocks.append(Prose(path, start, "\n".join(run), "comment block"))
    return blocks


def all_comments(path: Path) -> list[Prose]:
    return [Prose(path, lineno, text, "comment") for lineno, _, text in _comments(path)]


# The prose classes the standard disallows. Each is a sweep guard over what the
# read-every-file pass already removed, never the census itself: a class is here
# only when a regex can name it without firing on a genuine constraint, which is
# why a bare date is not a marker and a dated stamp is.
_DATE = r"(?:(?:19|20)\d{2}-\d{2}-\d{2}|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+(?:19|20)\d{2})"
_STAMP_WORD = (
    r"live[- ]feedback|verified|confirmed|captured?|LIVE BUG|cull pass"
    r"|de-noise|qgis-ux-batch|docs/[\w.]+\.md|logs?/[\w./]+"
)
_ALLOWED_IN_STAMP = r"live|feedback|design|feature|user|verified|captured|confirmed|item"

DATED_HISTORY = (
    rf"(?im)(?:{_STAMP_WORD})[^\n]{{0,60}}{_DATE}"
    rf"|{_DATE}[^\n]{{0,60}}(?:{_STAMP_WORD})"
    rf"|\((?![^()\n]*\b(?!{_ALLOWED_IN_STAMP})[a-z]{{4,}})[^()\n]{{0,40}}{_DATE}[^()\n]{{0,40}}\)"
    rf"|^[ \t#:*.\d)-]*{_DATE}[ \t]*(?::|--|—)"
)

SPEC_NOTATION = (
    r"(?i)\bjob-?\d|\btask-?\d{2}|\bsprint-?\d|\bADR[- ]?\d|\bwave \d"
    r"|\bmilestone \d|\bFR-\d|\bNFR-\d|\bOQ-\d|\bOPEN-\d|§"
)

ATTRIBUTION = r"\bNATE\b|\bNate\b"

MEMORY_FILENAMES = r"\b(?:feedback|project|reference)_[a-z0-9_]{4,}\.md\b"

EXAMPLES = r"(?m)^[\s#:]*(?:>>>|Examples?:|Usage:)"

CROSS_REFERENCES = r"(?i)\bsee (?:also )?[`']?[\w./]+\.(?:py|md)\b|\bdefined in [`']?[\w./]+\.py\b"

HISTORY_CLASSES = {
    "dated history": DATED_HISTORY,
    "spec notation": SPEC_NOTATION,
    "attribution": ATTRIBUTION,
    "memory filenames": MEMORY_FILENAMES,
}

DISALLOWED_CLASSES = dict(
    HISTORY_CLASSES,
    **{"usage example": EXAMPLES, "architecture cross-reference": CROSS_REFERENCES},
)


def offences(text: str, classes: dict[str, str]) -> list[tuple[int, str, str]]:
    """Matches as (line offset within the text, class name, matched span)."""
    import re

    found = []
    for name, pattern in classes.items():
        for match in re.finditer(pattern, text):
            found.append((text.count("\n", 0, match.start()), name, match.group(0).strip()))
    return sorted(set(found))


def exemption_rows() -> list[tuple[str, str, str]]:
    """The exemption ledger as the tree states it: symbol, file:line, reason."""
    rows = []
    for path in python_files():
        for doc in docstrings(path):
            if doc.exempt_reason is not None:
                name = f"{doc.name} (module)" if doc.kind == "module" else doc.name
                rows.append((name, f"{doc.rel}:{doc.header}", doc.exempt_reason))
    return sorted(rows, key=lambda row: row[1])


LEDGER_PREAMBLE = """# Docstring exemptions

Every `# docstring-exempt: <reason>` marker in the tree, regenerated from the
markers themselves. A docstring past the limit (3 content lines for a function
or class, 5 for a module) carries one, and the reason names the contract the
limit cannot hold. Past roughly ten entries the limit is re-argued rather than
routed around.

| symbol | file:line | reason |
| --- | --- | --- |
"""


def render_ledger() -> str:
    body = "".join(f"| `{name}` | {where} | {reason} |\n" for name, where, reason in exemption_rows())
    return LEDGER_PREAMBLE + body
