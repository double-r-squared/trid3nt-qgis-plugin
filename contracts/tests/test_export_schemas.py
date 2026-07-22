"""Tests for the JSON Schema export script (export_schemas.py)."""

from __future__ import annotations

import json
from pathlib import Path

from trid3nt_contracts import ws
from trid3nt_contracts.export_schemas import export


def test_export_writes_one_file_per_top_level_contract(tmp_path: Path) -> None:
    written = export(tmp_path)
    # Every WS message payload appears as ws_<...>.json
    for msg_type in ws.ALL_PAYLOADS:
        stem = "ws_" + msg_type.replace("-", "_")
        assert (tmp_path / f"{stem}.json").exists(), f"missing schema for {msg_type}"
    # Spot checks across appendices
    expected_others = [
        "assessment_envelope",
        "event_metadata",
        "claim_set",
        "numeric_claim",
        "project_document",
        "run_document",
        "article_document",
        "event_document",
        "session_document",
        "catalog_entry",
        # sprint-08 — Mode 1 catalog substrate
        "catalog_entry_document",
        "catalog_audit_log_document",
        "model_setup",
        "execution_handle",
        "run_result",
        "layer_uri",
    ]
    for stem in expected_others:
        assert (tmp_path / f"{stem}.json").exists(), f"missing schema for {stem}"
    # The returned list matches the on-disk files
    assert sorted(p.name for p in written) == sorted(p.name for p in tmp_path.glob("*.json"))


def test_export_is_idempotent(tmp_path: Path) -> None:
    """Regeneration with no contract changes produces byte-identical output."""
    export(tmp_path)
    contents_a = {p.name: p.read_bytes() for p in sorted(tmp_path.glob("*.json"))}
    export(tmp_path)
    contents_b = {p.name: p.read_bytes() for p in sorted(tmp_path.glob("*.json"))}
    assert contents_a == contents_b


def test_each_exported_schema_is_valid_json(tmp_path: Path) -> None:
    export(tmp_path)
    for path in tmp_path.glob("*.json"):
        json.loads(path.read_text())  # raises if not valid JSON
