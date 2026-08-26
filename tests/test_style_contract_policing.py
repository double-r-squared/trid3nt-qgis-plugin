"""The style contract is ONE file, and this gate is what keeps it one.

``contracts/trid3nt_contracts/styles.yaml`` declares what every preset IS -
its kind, its colormap, its scale policy, its units and its legend label - and
which preset a published QUANTITY gets. A second table in code that maps preset
names to anything (labels, colormaps, ramps, units, families) is a MIRROR: it
drifts silently, and the two answers disagree about the same picture.

So the rule this gate enforces is structural rather than a review habit: no
module under the emission or tool surfaces may hold a literal whose keys or
members are preset names. Any code that needs a property of a preset asks
``trid3nt_server/emission/styles.py``, which reads the contract.

The preset name set is loaded FROM the contract, so the gate tracks it
automatically: a preset added to the yaml immediately starts policing code that
tries to key a literal on it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from trid3nt_contracts.styles import presets

#: The surfaces a preset mapping could plausibly be rebuilt on: the publish /
#: style path and the tool library that feeds it.
SCANNED_PACKAGES = ("trid3nt_server/emission", "trid3nt_server/tools")

#: Two preset names as the keys of ONE literal is a table. A single mention is a
#: module referring to a preset it publishes under, which is not a mapping.
TABLE_THRESHOLD = 2

REPO_ROOT = Path(__file__).resolve().parents[1]


def _preset_names() -> frozenset[str]:
    return frozenset(presets())


def _scanned_files() -> list[Path]:
    found: list[Path] = []
    for package in SCANNED_PACKAGES:
        root = REPO_ROOT / package
        assert root.is_dir(), f"{root} is not a directory - fix SCANNED_PACKAGES"
        found.extend(sorted(p for p in root.rglob("*.py")))
    return found


def _literal_strings(node: ast.expr) -> list[str]:
    """The string constants a literal container keys on, in source order.

    A ``Dict``'s KEYS and a ``Set`` / ``List`` / ``Tuple``'s MEMBERS are the
    positions a preset name occupies when code is mapping FROM a preset. Values
    are deliberately not read: a table that maps some other key TO a preset name
    is a routing decision, not a restatement of what a preset is.
    """
    if isinstance(node, ast.Dict):
        parts = node.keys
    elif isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        parts = node.elts
    else:
        return []
    return [
        part.value
        for part in parts
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    ]


def _mirrors_in(path: Path, names: frozenset[str]) -> list[tuple[int, list[str]]]:
    """``(line, presets)`` for every literal in ``path`` keyed on preset names."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, list[str]]] = []
    for statement in tree.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        if statement.value is None:
            continue
        for node in ast.walk(statement.value):
            if not isinstance(node, (ast.Dict, ast.Set, ast.List, ast.Tuple)):
                continue
            hits = sorted({s for s in _literal_strings(node) if s in names})
            if len(hits) >= TABLE_THRESHOLD:
                found.append((node.lineno, hits))
    return found


def test_no_preset_mapping_lives_outside_the_style_contract() -> None:
    names = _preset_names()
    offences: list[str] = []
    for path in _scanned_files():
        for line, hits in _mirrors_in(path, names):
            offences.append(
                f"{path.relative_to(REPO_ROOT)}:{line} keys a literal on "
                f"{', '.join(hits)}"
            )
    assert not offences, (
        "A preset mapping was found in code. Preset properties belong in the "
        "style contract (contracts/trid3nt_contracts/styles.yaml), which is one "
        "file so that a mirror is impossible; read them back through "
        "trid3nt_server/emission/styles.py instead of restating them here:\n  "
        + "\n  ".join(offences)
    )


def test_the_gate_recognizes_a_mirror(tmp_path: Path) -> None:
    """The gate must FAIL on a real mirror, or it polices nothing."""
    names = _preset_names()
    sample = sorted(names)[:TABLE_THRESHOLD]
    mirror = tmp_path / "mirror.py"
    body = ", ".join(f'"{name}": "Label"' for name in sample)
    mirror.write_text(f"_LABELS = {{{body}}}\n", encoding="utf-8")

    found = _mirrors_in(mirror, names)
    assert found and found[0][1] == sample


def test_a_single_preset_mention_is_not_a_table(tmp_path: Path) -> None:
    """One preset name in a literal is a reference, not a mapping - the gate
    must not fire on it, or every publishing module becomes an offence."""
    names = _preset_names()
    one = sorted(names)[0]
    sample = tmp_path / "single.py"
    sample.write_text(f'_DEFAULTS = {{"preset": "{one}"}}\n', encoding="utf-8")

    assert _mirrors_in(sample, names) == []


@pytest.mark.parametrize("name", sorted(presets()))
def test_every_preset_declares_a_label(name: str) -> None:
    """The contract carries the label, so every preset must actually have one -
    a blank label sends the layer name and the legend title back to guessing."""
    label = presets()[name].label
    assert label and label.strip(), (
        f"preset {name!r} declares no label; the layer name and the legend "
        "title are read from it"
    )
