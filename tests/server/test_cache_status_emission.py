"""``cache-status`` emission and the cache-hit flag.

``_emit_cache_status`` serializes the expected shape to the socket sink, and a
failing send logs rather than propagating - an observability surface must not
break the agent loop. ``UsageMetadataEvent`` sets ``cache_hit`` from the
cached-token count."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from trid3nt_server.adapters.adapter import UsageMetadataEvent
from trid3nt_server.server import SessionState, _emit_cache_status


@pytest.mark.asyncio
async def test_cache_status_envelope_shape() -> None:
    """The cache-status envelope carries the expected JSON keys + values."""
    state = SessionState(session_id="01AAAAAAAAAAAAAAAAAAAAAAAA")
    state.model_cache_ref = "projects/p/locations/us-central1/cachedContents/x"
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
    assert p["model_cache_ref"] == state.model_cache_ref


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
