"""Guard: no markdown syntax in LLM-bound tool RESULT strings.

Markdown belongs in a docstring, which the model reads at selection time; a
RESULT is JSON or plaintext. A static lint AST-walks the tool and workflow trees
and flags non-docstring string constants carrying a header, bold, a table
separator or a fence; a dash bullet is legitimate plaintext and is not flagged."""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "trid3nt_server"
SCAN_DIRS = ("tools", "data", "workflows", "mesh")

#: Repo-relative (to SRC_ROOT) files allowed to build markdown strings.
#: Every entry MUST document why in the module docstring above.
ALLOWLIST: set[str] = set()

_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("header", re.compile(r"(^|\n)#{1,4} ")),
    ("bold", re.compile(r"\*\*[^*\n]+\*\*")),
    ("table-rule", re.compile(r"\|\s*-{2,}")),
    ("code-fence", re.compile(r"```")),
]


def _docstring_linenos(tree: ast.AST) -> set[int]:
    """Line numbers of every docstring constant in ``tree``."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                out.add(body[0].value.lineno)
    return out


def _markdown_hits(path: Path) -> list[str]:
    """``file:line [marker] snippet`` for every markdown-bearing string."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover - repo files must parse anyway
        return []
    doc_lines = _docstring_linenos(tree)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.lineno in doc_lines:
                continue
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
        else:
            continue
        for name, rx in _MARKERS:
            if rx.search(text):
                snippet = text.replace("\n", "\\n")[:80]
                hits.append(f"{path}:{node.lineno} [{name}] {snippet!r}")
                break
    return hits


def test_no_markdown_in_tool_result_strings() -> None:
    offenders: list[str] = []
    for sub in SCAN_DIRS:
        for path in sorted((SRC_ROOT / sub).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(SRC_ROOT).as_posix()
            if rel in ALLOWLIST:
                continue
            offenders.extend(_markdown_hits(path))
    assert not offenders, (
        "Markdown markers found in NON-docstring strings of tool/workflow "
        "result-building code. Markdown belongs in docstrings (LLM reads it "
        "at selection time); tool RESULTS must be JSON/plaintext. Move the "
        "data into structured result fields or strip the markup -- or, ONLY "
        "for a user-facing document artifact, add the file to ALLOWLIST with "
        "a documented reason.\n" + "\n".join(offenders)
    )


def test_allowlist_entries_exist() -> None:
    """A stale allowlist entry (file moved/deleted) must fail loudly."""
    for rel in ALLOWLIST:
        assert (SRC_ROOT / rel).is_file(), f"stale ALLOWLIST entry: {rel}"


def test_the_scan_reaches_every_tree_a_composing_tool_is_defined_in() -> None:
    """A tree that moved out of SCAN_DIRS is a scan that passes while reading
    nothing: every tree a registered tool is defined in is scanned. The gates
    tree holds the one card tool and is outside this scan's reach."""
    import inspect

    from trid3nt_server.tools import TOOL_REGISTRY

    trees = {
        Path(inspect.getsourcefile(inspect.unwrap(t.fn))).resolve()
        .relative_to(SRC_ROOT).parts[0]
        for t in TOOL_REGISTRY.values()
    }
    assert "mesh" in trees
    assert not trees - set(SCAN_DIRS) - {"gates"}, sorted(trees - set(SCAN_DIRS))
