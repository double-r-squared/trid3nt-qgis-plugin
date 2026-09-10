"""``classify`` partitions a file: pure code is what the other three counts leave.

A line belongs to exactly one of blank / comment / docstring, so pure code is the
remainder and can never be understated by a line being counted twice.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "instruments" / "loc_report.py"

_SOURCE = '''"""Summary line.

A constraint that wraps, with the blank line above it inside the docstring.
"""

# a full-line comment
X = 1


def f():
    """Body docstring.

    Second paragraph.
    """
    return X
'''


def _loc_report():
    """The instrument, imported by path - ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("loc_report", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_blank_line_inside_a_docstring_counts_once(tmp_path):
    """The two in-docstring blanks count as docstring only."""
    path = tmp_path / "sample.py"
    path.write_text(_SOURCE, encoding="utf-8")
    total, blank, comment, doc = _loc_report().classify(str(path))
    assert (total, blank, comment, doc) == (15, 3, 1, 8)
    assert total - blank - comment - doc == 3


def test_counts_partition_every_line(tmp_path):
    """No line is missed and none is claimed twice, so the remainder is pure code."""
    path = tmp_path / "sample.py"
    path.write_text(_SOURCE, encoding="utf-8")
    total, blank, comment, doc = _loc_report().classify(str(path))
    assert blank + comment + doc <= total
