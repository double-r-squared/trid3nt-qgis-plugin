"""Drift gate: the committed JSON Schemas must equal the live Python models.

``contracts/schemas/*.json`` is a MIRROR of the pydantic contract models. A
mirror is only safe while something proves it still matches, so this module
renders every schema in memory via ``export_schemas.render_schemas`` and diffs
the result against the committed bytes.

Two invariants, both enforced by WALKING ``contracts/schemas`` rather than by a
hand-listed set (a hand-listed set is the same mirror defect one level up):

  * the committed file SET equals the exported file set (no orphan file left
    behind by a deleted model, no model missing its committed schema);
  * every committed file is byte-identical to what the model renders today.

The gate is READ-ONLY: it renders in memory and never writes into the repo.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any

import pytest

from trid3nt_contracts.export_schemas import (
    REGEN_COMMAND,
    default_output_dir,
    render_schemas,
)

SCHEMA_DIR = default_output_dir()

_FIX = f"Regenerate with:\n    {REGEN_COMMAND}"


def _committed_filenames() -> list[str]:
    return sorted(p.name for p in SCHEMA_DIR.glob("*.json"))


def _property_drift(committed: dict[str, Any], rendered: dict[str, Any]) -> list[str]:
    """Property-level drift lines for the top-level object and every ``$defs``."""
    lines: list[str] = []

    def compare(scope: str, old: dict[str, Any], new: dict[str, Any]) -> None:
        old_props = old.get("properties", {}) or {}
        new_props = new.get("properties", {}) or {}
        for name in sorted(set(old_props) - set(new_props)):
            lines.append(f"  {scope}: property REMOVED from the model: {name!r}")
        for name in sorted(set(new_props) - set(old_props)):
            lines.append(f"  {scope}: property ADDED by the model: {name!r}")
        for name in sorted(set(old_props) & set(new_props)):
            if old_props[name] != new_props[name]:
                lines.append(f"  {scope}: property CHANGED: {name!r}")
        old_req = set(old.get("required", []) or [])
        new_req = set(new.get("required", []) or [])
        if old_req != new_req:
            lines.append(
                f"  {scope}: required CHANGED: committed={sorted(old_req)} "
                f"model={sorted(new_req)}"
            )

    compare("<root>", committed, rendered)
    old_defs = committed.get("$defs", {}) or {}
    new_defs = rendered.get("$defs", {}) or {}
    for name in sorted(set(old_defs) - set(new_defs)):
        lines.append(f"  $defs: definition REMOVED from the model: {name!r}")
    for name in sorted(set(new_defs) - set(old_defs)):
        lines.append(f"  $defs: definition ADDED by the model: {name!r}")
    for name in sorted(set(old_defs) & set(new_defs)):
        if old_defs[name] != new_defs[name]:
            compare(f"$defs.{name}", old_defs[name], new_defs[name])
    return lines


def test_schema_dir_is_not_empty() -> None:
    # A vanished schema dir would make every parametrized case vacuously pass.
    assert _committed_filenames(), f"no committed schemas under {SCHEMA_DIR}"


def test_committed_schema_set_matches_the_models() -> None:
    committed = set(_committed_filenames())
    exported = set(render_schemas())
    orphans = sorted(committed - exported)
    missing = sorted(exported - committed)
    assert not orphans and not missing, "\n".join(
        [
            f"committed schema set drifted from the models under {SCHEMA_DIR}:",
            *(f"  ORPHAN (no model exports it -- delete it): {n}" for n in orphans),
            *(f"  MISSING (model exports it, file absent): {n}" for n in missing),
            _FIX,
        ]
    )


@pytest.mark.parametrize("filename", _committed_filenames())
def test_committed_schema_matches_the_model(filename: str) -> None:
    rendered = render_schemas().get(filename)
    if rendered is None:
        pytest.fail(
            f"{filename}: committed but no model exports it (orphan mirror). "
            f"Delete the file, or restore the model that exported it.\n{_FIX}"
        )
    committed_text = (SCHEMA_DIR / filename).read_text()
    if committed_text == rendered:
        return

    detail = _property_drift(json.loads(committed_text), json.loads(rendered))
    if not detail:
        detail = [
            "  (drift is outside properties/required -- unified diff follows)",
            *(
                f"  {line.rstrip()}"
                for line in difflib.unified_diff(
                    committed_text.splitlines(),
                    rendered.splitlines(),
                    fromfile=f"committed/{filename}",
                    tofile=f"model/{filename}",
                    lineterm="",
                    n=2,
                )
            ),
        ]
    pytest.fail(
        "\n".join(
            [
                f"committed schema DRIFTED from its model: {SCHEMA_DIR / filename}",
                *detail,
                _FIX,
            ]
        )
    )
