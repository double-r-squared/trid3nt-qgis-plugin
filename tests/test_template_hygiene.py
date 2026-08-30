"""Mechanical hygiene gate: tier=template docstrings + module comments must be
PURELY FUNCTIONAL.

Every registered ``tier="template"`` tool must present a contract-only surface: its
docstring and its module's comments state what the tool does, its params, errors,
fidelity/off-scope, and data sources -- never history/archaeology (no rename/fold
provenance, no scenario-era ``model_*_scenario`` naming, no north-star verbiage) and
ASCII only (no em/en dashes or typographic quotes).

Scope is deliberately narrow and EASY TO WIDEN: ``_LOCI`` lists the surfaces scanned
(the tool ``__doc__``, the template module's module-level docstring, and the module's
``#`` comment lines). Helper-function docstrings and the general (non-template) repo
are a separate scope decision -- widening is one edit to ``_LOCI``.

ASCII only.
"""

from __future__ import annotations

import ast
import inspect
import re

import trid3nt_server.main as _main

_main._import_tools_registry()
from trid3nt_server.tools import TOOL_REGISTRY  # noqa: E402

#: The banned patterns. Extend by adding a row (name -> compiled regex).
BANNED: dict[str, re.Pattern[str]] = {
    "north_star": re.compile(r"north.?star", re.I),
    "formerly": re.compile(r"formerly", re.I),
    "renamed_from": re.compile(r"renamed from", re.I),
    "folded_from": re.compile(r"folded (?:in )?from", re.I),
    "scenario_era_name": re.compile(r"model_\w+_scenario"),
    "non_ascii": re.compile(r"[^\x00-\x7f]"),
}


def _template_tools() -> list[tuple[str, object]]:
    return sorted(
        (n, e)
        for n, e in TOOL_REGISTRY.items()
        if getattr(e.metadata, "tier", None) == "template"
    )


def _module_docstring(src: str) -> str:
    try:
        return ast.get_docstring(ast.parse(src)) or ""
    except SyntaxError:
        return ""


def _comment_lines(src: str) -> str:
    return "\n".join(
        line for line in src.splitlines() if line.lstrip().startswith("#")
    )


#: The scanned surfaces. ``fn`` -> the text to lint. WIDEN HERE (one row) to add
#: helper docstrings or the general repo later.
_LOCI: dict[str, callable] = {
    "tool __doc__": lambda fn, src: inspect.getdoc(fn) or "",
    "module docstring": lambda fn, src: _module_docstring(src),
    "# comments": lambda fn, src: _comment_lines(src),
}


def _scan(text: str) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for ln, line in enumerate(text.splitlines(), 1):
        for bkey, rx in BANNED.items():
            if rx.search(line):
                out.append((bkey, ln, line.strip()[:110]))
    return out


def test_template_docstrings_and_comments_are_functional() -> None:
    """No tier=template docstring/comment carries a banned (archaeology/non-ASCII)
    pattern. Failure names the tool, locus, pattern, line, and offending text."""
    failures: list[str] = []
    for name, entry in _template_tools():
        fn = entry.fn
        src_file = inspect.getsourcefile(fn)
        src = ""
        if src_file:
            with open(src_file, encoding="utf-8") as fh:
                src = fh.read()
        for locus, extract in _LOCI.items():
            text = extract(fn, src)
            for bkey, ln, snippet in _scan(text):
                rel = (src_file or "?").split("/workflows/")[-1]
                failures.append(f"{name} [{rel} :: {locus} L{ln}] {bkey}: {snippet!r}")
    assert not failures, (
        "template hygiene violations (docstrings/comments must be contract-only, "
        "ASCII, no archaeology):\n" + "\n".join(failures)
    )


def test_hygiene_gate_covers_all_templates() -> None:
    """The gate must see EVERY registered template - a scope regression that
    narrowed it to a handful would make the lint vacuous without failing."""
    from tests.test_door_dissolution import EXPECTED_TEMPLATES

    assert {name for name, _ in _template_tools()} == EXPECTED_TEMPLATES
