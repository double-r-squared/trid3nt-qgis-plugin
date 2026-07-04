"""Wave 4.11 M3 — telemetry MongoDB MCP backend tests.

Coverage:
    1. ``test_emits_to_mongo_when_mcp_bound`` — when Persistence singleton is
       bound, ``emit_tool_call_event`` calls ``Persistence._mcp.call_tool``
       (insert-one) with the correct collection and document shape.
    2. ``test_falls_back_to_local_file_when_mcp_unbound`` — when Persistence is
       ``None``, the record is written to the JSONL local-file path as before.
    3. ``test_validates_against_ToolCallTelemetryDocument_schema`` — malformed
       events (e.g. invalid source literal) raise ``ValidationError`` inside the
       coroutine; the error is caught so the dispatch loop never sees it.
    4. ``test_handles_persistence_failure_gracefully`` — a Persistence._mcp
       ``call_tool`` failure does not propagate out of ``emit_tool_call_event``.
    5. ``test_emits_required_fields`` — the Mongo document contains all required
       fields (session_id, called_at_utc, tool_name, source, result_ok,
       latency_ms; args_hash validated as 64-char hex).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from grace2_agent.telemetry import compute_args_hash, emit_tool_call_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_EMIT_KWARGS = dict(
    session_id="01TEST0000000000000000000",
    ts="2026-06-09T12:00:00Z",
    tool_name="fetch_dem",
    source="llm",
    args_hash=compute_args_hash({"bbox": [0, 0, 1, 1]}),
    success=True,
    latency_ms=55.5,
    error_code=None,
    retry_attempt=0,
    cached_content_token_count=None,
)


def _make_mock_persistence() -> MagicMock:
    """Return a mock Persistence whose _mcp.call_tool is an AsyncMock."""
    persistence = MagicMock()
    persistence._mcp = MagicMock()
    persistence._mcp.call_tool = AsyncMock(return_value={"insertedId": "some-id"})
    return persistence


async def _drain(n: int = 10) -> None:
    """Drain the event loop enough turns for fire-and-forget tasks to run."""
    for _ in range(n):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# 1. test_emits_to_mongo_when_mcp_bound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emits_to_mongo_when_mcp_bound() -> None:
    """When Persistence is bound, the event goes to Mongo (insert-one) not to file."""
    persistence = _make_mock_persistence()

    with patch("grace2_agent.telemetry.get_persistence", return_value=persistence):
        await emit_tool_call_event(**_DEFAULT_EMIT_KWARGS)
        await _drain()

    # call_tool was called exactly once with insert-one
    persistence._mcp.call_tool.assert_awaited_once()
    call_args = persistence._mcp.call_tool.call_args
    assert call_args[0][0] == "insert-one"
    payload = call_args[0][1]
    assert payload["collection"] == "tool_call_telemetry"
    # The document should carry a _id ULID
    doc = payload["document"]
    assert "_id" in doc
    assert len(doc["_id"]) > 0


# ---------------------------------------------------------------------------
# 2. test_falls_back_to_local_file_when_mcp_unbound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_falls_back_to_local_file_when_mcp_unbound() -> None:
    """When Persistence is None, event falls back to JSONL local file."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        path = tf.name
    try:
        with (
            patch("grace2_agent.telemetry.get_persistence", return_value=None),
            patch.dict(os.environ, {"GRACE2_TELEMETRY_PATH": path}),
        ):
            await emit_tool_call_event(**_DEFAULT_EMIT_KWARGS)
            # Drain enough turns for the executor-based file write to complete.
            await asyncio.sleep(0.1)

        with open(path, encoding="utf-8") as fh:
            line = fh.readline()
        record = json.loads(line)
        assert record["tool_name"] == "fetch_dem"
        assert record["success"] is True
        assert record["session_id"] == _DEFAULT_EMIT_KWARGS["session_id"]
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# 3. test_validates_against_ToolCallTelemetryDocument_schema
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validates_against_ToolCallTelemetryDocument_schema() -> None:
    """Malformed events (bad source literal) are caught; dispatch loop never sees the error.

    We pass an invalid ``source`` value.  Inside ``_write_to_mongo``,
    ``ToolCallTelemetryDocument(...)`` raises ``ValidationError``.  The
    ``except`` block catches it and logs at WARNING — ``emit_tool_call_event``
    itself must not raise.
    """
    persistence = _make_mock_persistence()

    bad_kwargs = dict(_DEFAULT_EMIT_KWARGS, source="robot")  # not a valid Literal

    with patch("grace2_agent.telemetry.get_persistence", return_value=persistence):
        # Must NOT raise even though the document is invalid.
        await emit_tool_call_event(**bad_kwargs)  # type: ignore[arg-type]
        await _drain()

    # insert-one was never called because validation failed before that point.
    persistence._mcp.call_tool.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4. test_handles_persistence_failure_gracefully
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handles_persistence_failure_gracefully() -> None:
    """A ``call_tool`` failure inside _write_to_mongo does not propagate out."""
    persistence = _make_mock_persistence()
    # Simulate a Mongo / MCP failure.
    persistence._mcp.call_tool = AsyncMock(
        side_effect=RuntimeError("connection refused")
    )

    with patch("grace2_agent.telemetry.get_persistence", return_value=persistence):
        # Must NOT raise.
        await emit_tool_call_event(**_DEFAULT_EMIT_KWARGS)
        await _drain()

    # call_tool was attempted (and failed silently).
    persistence._mcp.call_tool.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. test_emits_required_fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emits_required_fields() -> None:
    """The Mongo document contains all required telemetry fields with correct types."""
    persistence = _make_mock_persistence()

    kwargs = dict(
        _DEFAULT_EMIT_KWARGS,
        session_id="01SESS000000000000000000AA",
        tool_name="compute_slope",
        source="workflow",
        success=False,
        latency_ms=123.4,
        error_code="SLOPE_ERROR",
        retry_attempt=1,
        cached_content_token_count=4096,
    )

    with patch("grace2_agent.telemetry.get_persistence", return_value=persistence):
        await emit_tool_call_event(**kwargs)
        await _drain()

    persistence._mcp.call_tool.assert_awaited_once()
    call_args = persistence._mcp.call_tool.call_args
    doc = call_args[0][1]["document"]

    # Required fields present
    assert "session_id" in doc
    assert doc["session_id"] == kwargs["session_id"]

    # called_at_utc is the Mongo TTL field (serialized from UTCDatetime)
    assert "called_at_utc" in doc

    assert doc["tool_name"] == "compute_slope"
    assert doc["source"] == "workflow"

    # result_ok is the Mongo field name (maps from success=False)
    assert doc["result_ok"] is False

    assert doc["latency_ms"] == pytest.approx(123.4, rel=1e-3)
    assert doc["error_code"] == "SLOPE_ERROR"
    assert doc["retry_attempt"] == 1
    assert doc["cached_content_token_count"] == 4096

    # args_hash is a 64-char hex string
    assert isinstance(doc["args_hash"], str)
    assert len(doc["args_hash"]) == 64
    assert all(c in "0123456789abcdef" for c in doc["args_hash"])

    # _id should be a ULID (26-char base32)
    assert "_id" in doc
    assert len(doc["_id"]) == 26


# ---------------------------------------------------------------------------
# 6. Regression: get_persistence import failure falls through to local file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_error_falls_through_to_local_file() -> None:
    """If get_persistence can't be imported (early startup), fall back to file."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        path = tf.name
    try:
        with (
            patch(
                "grace2_agent.telemetry.get_persistence",
                side_effect=ImportError("not yet wired"),
            ),
            patch.dict(os.environ, {"GRACE2_TELEMETRY_PATH": path}),
        ):
            await emit_tool_call_event(**_DEFAULT_EMIT_KWARGS)
            await asyncio.sleep(0.1)

        with open(path, encoding="utf-8") as fh:
            line = fh.readline()
        record = json.loads(line)
        assert record["tool_name"] == "fetch_dem"
    finally:
        os.unlink(path)
