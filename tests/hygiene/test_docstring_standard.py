"""The docstring standard: the content-line limits, the LLM-facing budget, the
exemption ledger, and the prose classes the standard disallows.

A file with no docstring and no comment is in scope and simply contributes
nothing; scope is every tracked Python file under the product trees.
"""

from __future__ import annotations

import pytest

from tests.hygiene import _source as source

LEDGER = source.REPO_ROOT / "docs/validation/docstring-exemptions.md"


def _all_docstrings():
    return [d for path in source.python_files() for d in source.docstrings(path)]


def test_internal_docstrings_stay_within_the_content_limit() -> None:
    over = [
        f"{d.rel}:{d.lineno} {d.kind} {d.name}: {d.content_lines} content lines > {d.limit}"
        for d in _all_docstrings()
        if not d.llm_facing and d.exempt_reason is None and d.content_lines > d.limit
    ]
    assert not over, "docstrings past the limit without an exemption marker:\n" + "\n".join(over)


def test_llm_facing_docstrings_stay_within_the_front_budget() -> None:
    over = [
        f"{d.rel}:{d.lineno} {d.name}: {d.budget_chars} chars > {source.LLM_FRONT_BUDGET}"
        for d in _all_docstrings()
        if d.llm_facing and d.exempt_reason is None and d.budget_chars > source.LLM_FRONT_BUDGET
    ]
    assert not over, "LLM-facing docstrings past the routing budget:\n" + "\n".join(over)


def test_exemption_markers_sit_on_docstrings_that_need_one() -> None:
    idle = [
        f"{d.rel}:{d.lineno} {d.name}"
        for d in _all_docstrings()
        if d.exempt_reason is not None
        and d.content_lines <= d.limit
        and d.budget_chars <= source.LLM_FRONT_BUDGET
    ]
    assert not idle, "exemption markers on docstrings already within the limit:\n" + "\n".join(idle)


def test_the_exemption_ledger_is_the_tree() -> None:
    """The ledger is rendered from the markers, so a drifted row fails here."""
    assert LEDGER.read_text(encoding="utf-8") == source.render_ledger()


def test_the_ledger_stays_short_enough_to_argue_about() -> None:
    count = len(source.exemption_rows())
    assert count <= 10, f"{count} exemptions - re-argue the limit rather than routing around it"


@pytest.mark.parametrize("origin", ["docstring", "comment block"])
def test_no_disallowed_prose_classes(origin: str) -> None:
    offences = []
    for path in source.python_files():
        items = (
            [(d.rel, d.lineno, d.text) for d in source.docstrings(path)]
            if origin == "docstring"
            else [(b.rel, b.lineno, b.text) for b in source.comment_blocks(path)]
        )
        for rel, lineno, text in items:
            for offset, name, span in source.offences(text, source.DISALLOWED_CLASSES):
                offences.append(f"{rel}:{lineno + offset} [{name}] {span!r}")
    assert not offences, f"disallowed classes in a {origin}:\n" + "\n".join(sorted(set(offences)))
