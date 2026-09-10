"""No history in code: dates, spec labels, attribution and memory filenames.

The scan is every comment token, trailing ones included, plus every docstring;
the docstring standard's own guard covers only full-line comment blocks.
"""

from __future__ import annotations

from tests.hygiene import _source as source


def test_no_history_markers_in_comments_or_docstrings() -> None:
    offences = []
    for path in source.python_files():
        prose = [(c.rel, c.lineno, c.text) for c in source.all_comments(path)]
        prose += [(d.rel, d.lineno, d.text) for d in source.docstrings(path)]
        for rel, lineno, text in prose:
            for offset, name, span in source.offences(text, source.HISTORY_CLASSES):
                offences.append(f"{rel}:{lineno + offset} [{name}] {span!r}")
    assert not offences, "history markers in code:\n" + "\n".join(sorted(set(offences)))
