"""Tests for the in-chat model selector feature (NATE 2026-06-17).

Covers:
  1. ``model_supports_cache`` helper — Anthropic-only allowlist (True for Claude,
     False for Nova / DeepSeek-R1 / any non-Anthropic id).
  2. ``_build_converse_kwargs`` per-model cachePoint gate:
       - DeepSeek-R1 AND Nova → NO cachePoint even when env is ON.
       - Claude Sonnet 4.6 → cachePoint present when env is ON.
       - Env=OFF → no cachePoint regardless of model.
  3. ``emit_tool_call_event`` persists ``model_id`` in the local JSONL file.
  4. ``_aggregate_records`` produces a ``by_model`` section with per-model stats.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from grace2_agent import bedrock_adapter as ba
from grace2_agent.telemetry import compute_args_hash, emit_tool_call_event
from grace2_agent.tool_catalog_http import _aggregate_records, _normalize_record

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

_SYS = "You are TRID3NT. " * 60  # large static system prompt

_BEDROCK_TOOL = {"toolSpec": {"name": "fetch_dem", "inputSchema": {"json": {}}}}


@pytest.fixture(autouse=True)
def _stub_converters(monkeypatch):
    """Stub out the genai→Bedrock converters so tests only exercise the
    cachePoint-gating logic, not the conversion of contract objects."""
    monkeypatch.setattr(ba, "contents_to_bedrock_messages", lambda c: ([], []))
    monkeypatch.setattr(
        ba,
        "tool_declarations_to_bedrock_tools",
        lambda t: [dict(_BEDROCK_TOOL)] if t else [],
    )


def _has_cache_point(items: list) -> bool:
    return bool(items) and isinstance(items[-1], dict) and "cachePoint" in items[-1]


def _tools():
    return ["<one declaration>"]  # truthy; converter stubbed above


# ---------------------------------------------------------------------------
# 1. model_supports_cache helper
# ---------------------------------------------------------------------------


def test_model_supports_cache_true_for_claude():
    assert ba.model_supports_cache("us.anthropic.claude-sonnet-4-6") is True


def test_model_supports_cache_true_for_claude_haiku():
    assert ba.model_supports_cache("us.anthropic.claude-haiku-4-5") is True


def test_model_supports_cache_false_for_nova_lite():
    # cachePoint is an Anthropic-family feature; Nova REJECTS it (live error:
    # "extraneous key [cachePoint] is not permitted"). Allowlist semantics.
    assert ba.model_supports_cache("us.amazon.nova-lite-v1:0") is False


def test_model_supports_cache_false_for_nova_pro():
    assert ba.model_supports_cache("us.amazon.nova-pro-v1:0") is False


def test_model_supports_cache_false_for_deepseek():
    assert ba.model_supports_cache("us.deepseek.r1-v1:0") is False


def test_model_supports_cache_false_for_unknown_model():
    # Allowlist: an UNKNOWN (non-Anthropic) model id defaults to NO cache. The
    # earlier "unknown -> assume supported" default wrongly enabled cachePoint
    # for Nova and broke every non-Sonnet model — flipped to Anthropic-only.
    assert ba.model_supports_cache("us.some.future-model-v1:0") is False


def test_model_supports_cache_true_for_future_claude_profile():
    # Provider substring match covers future Claude profile ids without an edit.
    assert ba.model_supports_cache("us.anthropic.claude-opus-4-8") is True


# ---------------------------------------------------------------------------
# 2. _build_converse_kwargs per-model cachePoint gate
# ---------------------------------------------------------------------------


def test_deepseek_no_cachepoint_even_when_env_on(monkeypatch):
    """DeepSeek-R1 must produce NO cachePoint regardless of BEDROCK_PROMPT_CACHE."""
    monkeypatch.delenv("BEDROCK_PROMPT_CACHE", raising=False)  # env default = ON
    kw = ba._build_converse_kwargs([], _tools(), _SYS, "us.deepseek.r1-v1:0")

    # system block must NOT end with cachePoint
    assert not _has_cache_point(kw["system"])
    # tool list must NOT end with cachePoint
    assert not _has_cache_point(kw["toolConfig"]["tools"])


def test_claude_has_cachepoint_when_env_on(monkeypatch):
    """Claude Sonnet 4.6 must produce cachePoints when BEDROCK_PROMPT_CACHE is ON."""
    monkeypatch.delenv("BEDROCK_PROMPT_CACHE", raising=False)
    kw = ba._build_converse_kwargs([], _tools(), _SYS, "us.anthropic.claude-sonnet-4-6")

    assert _has_cache_point(kw["system"])
    assert _has_cache_point(kw["toolConfig"]["tools"])


def test_nova_no_cachepoint_even_when_env_on(monkeypatch):
    """Amazon Nova Pro must produce NO cachePoint regardless of the env flag.

    Regression for NATE's live error: selecting Nova Pro threw
    "Malformed input request: #/toolConfig/tools/93: extraneous key
    [cachePoint] is not permitted". Nova rejects cachePoint, so it must never
    be added for a Nova request even with BEDROCK_PROMPT_CACHE ON.
    """
    monkeypatch.delenv("BEDROCK_PROMPT_CACHE", raising=False)  # env default = ON
    kw = ba._build_converse_kwargs([], _tools(), _SYS, "us.amazon.nova-pro-v1:0")

    assert not _has_cache_point(kw["system"])
    assert not _has_cache_point(kw["toolConfig"]["tools"])


def test_claude_no_cachepoint_when_env_off(monkeypatch):
    """Global env switch overrides model capability — env OFF = no cachePoint."""
    monkeypatch.setenv("BEDROCK_PROMPT_CACHE", "0")
    kw = ba._build_converse_kwargs([], _tools(), _SYS, "us.anthropic.claude-sonnet-4-6")

    assert not _has_cache_point(kw["system"])
    assert not _has_cache_point(kw["toolConfig"]["tools"])


def test_deepseek_no_cachepoint_when_env_off_too(monkeypatch):
    """Both conditions false: env OFF + no model support. Result is still no cachePoint."""
    monkeypatch.setenv("BEDROCK_PROMPT_CACHE", "0")
    kw = ba._build_converse_kwargs([], _tools(), _SYS, "us.deepseek.r1-v1:0")

    assert not _has_cache_point(kw["system"])
    assert not _has_cache_point(kw["toolConfig"]["tools"])


# ---------------------------------------------------------------------------
# 3. emit_tool_call_event persists model_id in the local JSONL record
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
    with patch.dict(os.environ, {"GRACE2_TELEMETRY_PATH": path}):
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
# 4. _aggregate_records produces a by_model section
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
# 5. resolve_selected_model - provider-aware validation (F2, live-feedback
#    2026-07-08: local hot-swap). Cloud (bedrock/default) keeps the Bedrock
#    allowlist byte-identical; MODEL_PROVIDER=openai passes local ids verbatim.
# ---------------------------------------------------------------------------


def test_resolve_none_is_silent_default(monkeypatch):
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    assert ba.resolve_selected_model(None) == (None, None)


def test_resolve_bedrock_known_id_passes(monkeypatch):
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    got, notice = ba.resolve_selected_model("us.anthropic.claude-sonnet-4-6")
    assert got == "us.anthropic.claude-sonnet-4-6"
    assert notice is None


def test_resolve_bedrock_unknown_id_falls_back_with_notice(monkeypatch):
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    got, notice = ba.resolve_selected_model("qwen3:8b-16k")
    assert got is None
    assert notice is not None and "qwen3:8b-16k" in notice


def test_resolve_openai_provider_passes_local_id_verbatim(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    assert ba.resolve_selected_model("qwen3:8b-16k") == ("qwen3:8b-16k", None)


def test_resolve_openai_provider_local_default_placeholder_maps_to_default(
    monkeypatch,
):
    """The legacy 'local-default' web placeholder = 'use the server default'."""
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    assert ba.resolve_selected_model("local-default") == (None, None)


def test_resolve_openai_provider_none_still_silent(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    assert ba.resolve_selected_model(None) == (None, None)


def test_resolve_openai_provider_bedrock_id_passes_through_to_adapter_guard(
    monkeypatch,
):
    """A stale Bedrock id is passed through here; openai_adapter.openai_model
    ignores Bedrock-shaped ids (falls back to GRACE2_OPENAI_MODEL), so the
    guard lives at the adapter boundary, not in resolve."""
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    got, notice = ba.resolve_selected_model("us.anthropic.claude-sonnet-4-6")
    assert got == "us.anthropic.claude-sonnet-4-6"
    assert notice is None
