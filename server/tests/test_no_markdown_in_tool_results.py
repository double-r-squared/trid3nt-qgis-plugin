"""Guard: no markdown syntax in LLM-bound tool RESULT strings.

NATE rule (lane audit, 2026-07): markdown belongs in DOCSTRINGS (read by the
LLM at tool-selection time) -- it is WASTE in tool RESULTS, which must be
JSON / plaintext.

This is a best-effort static lint, not a runtime check: it AST-walks every
``.py`` file under ``trid3nt_server/tools`` and ``trid3nt_server/workflows``
and flags NON-docstring string constants (incl. f-strings) that carry
markdown markers:

- headers  -- a line starting ``# `` .. ``#### ``
- bold     -- ``**text**``
- tables   -- ``| --- |`` separator rows
- fences   -- triple backticks

Docstrings are excluded (markdown is CORRECT there). Plain ``- item`` dash
bullets are NOT flagged: a dash-prefixed list is legitimate plaintext (and
matching it would false-positive on negative numbers / prose hyphens).

Allowlist
=========

Files whose markdown-bearing strings are legitimately markdown because they
build a USER-FACING document, never an LLM-bound result:

- ``tools/meta/compose_case_report.py`` -- writes a markdown situation-report
  FILE to the case artifacts dir (the export_case_to_qgis convention); its
  registered tool returns a markdown-free JSON dict (path + counts). Verified
  by ``test_compose_case_report_llm_result_is_markdown_free`` below so the
  allowlist entry cannot silently start leaking markdown to the LLM.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "trid3nt_server"
SCAN_DIRS = ("tools", "workflows")

#: Repo-relative (to SRC_ROOT) files allowed to build markdown strings.
#: Every entry MUST document why in the module docstring above.
ALLOWLIST = {
    "tools/meta/compose_case_report.py",  # user-facing .md artifact on disk
}

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


def test_compose_case_report_llm_result_is_markdown_free() -> None:
    """The allowlisted file's LLM-bound RETURN dict stays markdown-free.

    ``compose_case_report`` may build markdown for its on-disk report file,
    but the dict it returns to the LLM must remain plain JSON. Static check:
    the ``return`` statement of the registered coroutine must be a dict
    literal whose string values carry no markdown markers.
    """
    path = SRC_ROOT / "tools" / "meta" / "compose_case_report.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "compose_case_report"
    )
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert returns, "compose_case_report has no return statement"
    for ret in returns:
        assert isinstance(ret.value, ast.Dict), (
            "compose_case_report must return a dict literal (LLM-bound JSON), "
            f"got {ast.dump(ret.value)[:80]} at line {ret.lineno}"
        )
        for value in ret.value.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                for name, rx in _MARKERS:
                    assert not rx.search(value.value), (
                        f"markdown [{name}] in compose_case_report return "
                        f"value at line {value.lineno}: {value.value!r}"
                    )
