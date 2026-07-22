"""Tests for ``cache-status`` envelope emission + telemetry cache plumbing
(Wave 4.10 job-B6).

Coverage:
    1. ``test_cache_status_envelope_shape`` — ``_emit_cache_status`` serializes
       the expected JSON shape to the WebSocket sink.
    2. ``test_cache_status_failure_does_not_raise`` — a failing websocket send
       logs but does not propagate (observability surface must not break the
       agent loop).
    3. ``test_telemetry_records_cached_tokens`` — ``emit_tool_call_event``
       writes the ``cached_content_token_count`` field to the JSONL log so
       the 90%-discount empirical proof is observable downstream.
    4. ``test_usage_metadata_event_cache_hit_flag`` — ``UsageMetadataEvent``
       sets ``cache_hit=True`` when cached_content_token_count > 0, False
       otherwise.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from grace2_agent.adapter import UsageMetadataEvent
from grace2_agent.server import SessionState, _emit_cache_status
from grace2_agent.telemetry import compute_args_hash, emit_tool_call_event


@pytest.mark.asyncio
async def test_cache_status_envelope_shape() -> None:
    """The cache-status envelope carries the expected JSON keys + values."""
    state = SessionState(session_id="01AAAAAAAAAAAAAAAAAAAAAAAA")
    state.gemini_cache_name = "projects/p/locations/us-central1/cachedContents/x"
    usage = UsageMetadataEvent(
        cached_content_token_count=10_500,
        total_token_count=11_200,
        prompt_token_count=10_800,
        candidates_token_count=400,
        cache_hit=True,
    )
    sent: list[str] = []

    ws = MagicMock()
    async def _send(text: str) -> None:
        sent.append(text)
    ws.send = _send

    await _emit_cache_status(ws, state, usage)
    assert len(sent) == 1
    parsed = json.loads(sent[0])
    assert parsed["type"] == "cache-status"
    assert parsed["session_id"] == state.session_id
    p = parsed["payload"]
    assert p["cache_hit"] is True
    assert p["cached_tokens"] == 10_500
    assert p["total_tokens"] == 11_200
    assert p["prompt_tokens"] == 10_800
    assert p["candidates_tokens"] == 400
    assert p["cache_name"] == state.gemini_cache_name


@pytest.mark.asyncio
async def test_cache_status_failure_does_not_raise() -> None:
    """A wire-side send failure logs but does not propagate."""
    state = SessionState(session_id="01BBBBBBBBBBBBBBBBBBBBBBBB")
    usage = UsageMetadataEvent(
        cached_content_token_count=0,
        total_token_count=1000,
        cache_hit=False,
    )

    ws = MagicMock()
    async def _send(text: str) -> None:
        raise RuntimeError("connection closed")
    ws.send = _send

    # MUST NOT raise — observability is best-effort.
    await _emit_cache_status(ws, state, usage)


@pytest.mark.asyncio
async def test_telemetry_records_cached_tokens() -> None:
    """``emit_tool_call_event`` round-trips ``cached_content_token_count``."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        path = tf.name
    try:
        with patch.dict(os.environ, {"GRACE2_TELEMETRY_PATH": path}):
            await emit_tool_call_event(
                session_id="SESSION_X",
                ts="2026-06-09T00:00:00Z",
                tool_name="fetch_dem",
                source="llm",
                args_hash=compute_args_hash({"bbox": [0, 0, 1, 1]}),
                success=True,
                latency_ms=12.3,
                error_code=None,
                cached_content_token_count=9_876,
            )
            # Drain the fire-and-forget write before the patch context exits.
            # The write is dispatched via an executor — a yielding sleep(0) is
            # not enough; a real-time sleep is required so the executor thread
            # finishes flushing to disk.
            await asyncio.sleep(0.2)
        # Read the JSONL line.
        with open(path, encoding="utf-8") as fh:
            line = fh.readline()
        record = json.loads(line)
        assert record["cached_content_token_count"] == 9_876
        assert record["tool_name"] == "fetch_dem"
        assert record["success"] is True
    finally:
        os.unlink(path)


def test_usage_metadata_event_cache_hit_flag_true() -> None:
    ev = UsageMetadataEvent(
        cached_content_token_count=100,
        total_token_count=500,
        cache_hit=True,
    )
    assert ev.cache_hit is True
    assert ev.cached_content_token_count == 100


def test_usage_metadata_event_cache_hit_flag_false() -> None:
    ev = UsageMetadataEvent(
        cached_content_token_count=0,
        total_token_count=500,
        cache_hit=False,
    )
    assert ev.cache_hit is False
    assert ev.cached_content_token_count == 0


def test_usage_metadata_event_all_none() -> None:
    """Default constructor (no kwargs) is a no-usage event — safe baseline."""
    ev = UsageMetadataEvent()
    assert ev.cached_content_token_count is None
    assert ev.total_token_count is None
    assert ev.cache_hit is False
