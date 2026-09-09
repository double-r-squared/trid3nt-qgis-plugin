"""Tests for the in-chat model selector.

Covers:
  1. ``emit_tool_call_event`` persists ``model_id`` in the local JSONL file.
  2. ``_aggregate_records`` produces a ``by_model`` section with per-model stats.
  3. ``resolve_selected_model`` validates the per-turn id the client sends.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from unittest.mock import patch

import pytest

from trid3nt_server.adapters import model_selection as ms
from trid3nt_server.telemetry import compute_args_hash, emit_tool_call_event
from trid3nt_server.server.protocol.catalog_http import _aggregate_records, _normalize_record

# ---------------------------------------------------------------------------
# 1. emit_tool_call_event persists model_id in the local JSONL record
# ---------------------------------------------------------------------------


def _read_jsonl(path: str) -> list[dict]:
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


async def _emit_with_model_id(path: str, model_id: str | None) -> None:
    """Call emit_tool_call_event targeting ``path``, draining the event loop."""
    args = dict(
        session_id="test-session-model",
        ts="2026-06-17T10:00:00Z",
        tool_name="fetch_dem",
        source="llm",
        args_hash=compute_args_hash({"bbox": [0, 0, 1, 1]}),
        success=True,
        latency_ms=42.0,
        model_id=model_id,
    )
    with patch.dict(os.environ, {"TRID3NT_TELEMETRY_PATH": path}):
        await emit_tool_call_event(**args)
        await asyncio.sleep(0.1)  # drain fire-and-forget task


@pytest.mark.asyncio
async def test_emit_carries_model_id_in_jsonl():
    """model_id must appear in the persisted JSONL telemetry record."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        path = tf.name
    try:
        await _emit_with_model_id(path, "us.anthropic.claude-sonnet-4-6")
        records = _read_jsonl(path)
        assert len(records) == 1
        assert records[0].get("model_id") == "us.anthropic.claude-sonnet-4-6"
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_emit_model_id_none_is_stored_as_null():
    """model_id=None must be written as JSON null (key present, value null)."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        path = tf.name
    try:
        await _emit_with_model_id(path, None)
        records = _read_jsonl(path)
        assert len(records) == 1
        rec = records[0]
        assert "model_id" in rec
        assert rec["model_id"] is None
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# 2. _aggregate_records produces a by_model section
# ---------------------------------------------------------------------------


def _make_record(
    tool_name: str = "fetch_dem",
    success: bool = True,
    latency_ms: float = 100.0,
    model_id: str | None = "us.anthropic.claude-sonnet-4-6",
    routing_outcome: str | None = None,
) -> dict:
    """Build a normalized record (post-_normalize_record shape) for _aggregate_records."""
    raw = {
        "session_id": "s-test",
        "tool_name": tool_name,
        "source": "llm",
        "success": success,
        "latency_ms": latency_ms,
        "model_id": model_id,
        "error_code": None,
        "retry_attempt": 0,
        "cached_content_token_count": None,
        "result_usable": None,
        "routed_ok": None,
        "ts": "2026-06-17T10:00:00Z",
        "routing_outcome": routing_outcome,
    }
    return _normalize_record(raw)


def test_aggregate_by_model_section_present():
    records = [
        _make_record(model_id="us.anthropic.claude-sonnet-4-6"),
        _make_record(model_id="us.anthropic.claude-sonnet-4-6"),
        _make_record(model_id="us.deepseek.r1-v1:0"),
    ]
    summary = _aggregate_records(records)
    assert "by_model" in summary
    assert isinstance(summary["by_model"], list)


def test_aggregate_by_model_groups_correctly():
    records = [
        _make_record(model_id="us.anthropic.claude-sonnet-4-6", latency_ms=100.0),
        _make_record(model_id="us.anthropic.claude-sonnet-4-6", latency_ms=200.0),
        _make_record(model_id="us.deepseek.r1-v1:0", latency_ms=300.0),
    ]
    summary = _aggregate_records(records)
    by_model = {row["model_id"]: row for row in summary["by_model"]}

    assert "us.anthropic.claude-sonnet-4-6" in by_model
    assert "us.deepseek.r1-v1:0" in by_model

    claude = by_model["us.anthropic.claude-sonnet-4-6"]
    assert claude["count"] == 2
    assert claude["success_rate"] == pytest.approx(1.0)

    deepseek = by_model["us.deepseek.r1-v1:0"]
    assert deepseek["count"] == 1


def test_aggregate_by_model_null_model_id_becomes_unknown():
    records = [
        _make_record(model_id=None),
    ]
    summary = _aggregate_records(records)
    by_model = {row["model_id"]: row for row in summary["by_model"]}
    assert "unknown" in by_model
    assert by_model["unknown"]["count"] == 1


def test_aggregate_by_model_success_rate_partial_failure():
    records = [
        _make_record(model_id="us.anthropic.claude-haiku-4-5", success=True),
        _make_record(model_id="us.anthropic.claude-haiku-4-5", success=False),
    ]
    summary = _aggregate_records(records)
    by_model = {row["model_id"]: row for row in summary["by_model"]}
    haiku = by_model["us.anthropic.claude-haiku-4-5"]
    assert haiku["count"] == 2
    assert haiku["success_rate"] == pytest.approx(0.5)


def test_aggregate_by_model_sorted_by_count_descending():
    """by_model list is sorted by count descending (highest-use model first)."""
    records = (
        [_make_record(model_id="us.deepseek.r1-v1:0")] * 5
        + [_make_record(model_id="us.anthropic.claude-sonnet-4-6")] * 3
        + [_make_record(model_id="us.amazon.nova-lite-v1:0")] * 1
    )
    summary = _aggregate_records(records)
    counts = [row["count"] for row in summary["by_model"]]
    assert counts == sorted(counts, reverse=True)


def test_aggregate_by_model_latency_fields_present():
    records = [
        _make_record(model_id="us.anthropic.claude-sonnet-4-6", latency_ms=100.0),
        _make_record(model_id="us.anthropic.claude-sonnet-4-6", latency_ms=200.0),
    ]
    summary = _aggregate_records(records)
    row = summary["by_model"][0]
    assert "latency_p50_ms" in row
    assert "latency_p95_ms" in row
    assert row["latency_p50_ms"] is not None
    assert row["latency_p95_ms"] is not None


def test_aggregate_empty_records_by_model_is_empty_list():
    summary = _aggregate_records([])
    assert summary["by_model"] == []


# ---------------------------------------------------------------------------
# 3. resolve_selected_model - the per-turn model id the client sends
# ---------------------------------------------------------------------------


def test_resolve_none_is_silent_default(monkeypatch):
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    assert ms.resolve_selected_model(None) == (None, None)


def test_resolve_openai_provider_passes_local_id_verbatim(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    assert ms.resolve_selected_model("qwen3:8b-16k") == ("qwen3:8b-16k", None)


def test_resolve_openai_provider_local_default_placeholder_maps_to_default(
    monkeypatch,
):
    """The legacy 'local-default' web placeholder = 'use the server default'."""
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    assert ms.resolve_selected_model("local-default") == (None, None)


def test_resolve_openai_provider_none_still_silent(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    assert ms.resolve_selected_model(None) == (None, None)


def test_resolve_openai_provider_foreign_id_passes_through_to_adapter_guard(
    monkeypatch,
):
    """An id shaped for another provider passes through here;
    openai_adapter.openai_model ignores it (falls back to
    TRID3NT_OPENAI_MODEL), so the guard lives at the adapter boundary, not in
    resolve."""
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    got, notice = ms.resolve_selected_model("us.anthropic.claude-sonnet-4-6")
    assert got == "us.anthropic.claude-sonnet-4-6"
    assert notice is None
