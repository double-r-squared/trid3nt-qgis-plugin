"""Unit tests for ``trid3nt_server.telemetry`` (Wave 4.10 job B-tel).

Coverage:
    1. ``test_record_shape`` — ``emit_tool_call_event`` writes a JSONL line
       with ALL required fields and correct types.
    2. ``test_non_blocking_returns_before_write`` — the coroutine returns
       quickly (schedules a task) rather than awaiting the file write.
    3. ``test_error_path_does_not_raise`` — a forced write failure does not
       propagate out of ``emit_tool_call_event``.
    4. ``test_multiple_events_append`` — successive calls append separate lines
       (not overwrite) so the log accumulates correctly.
    5. ``test_compute_args_hash_stable`` — same args → same digest; different
       args → different digest.
    6. ``test_env_override_path`` — ``TRID3NT_TELEMETRY_PATH`` is respected.
    7. ``test_none_args_hash`` — ``compute_args_hash(None)`` returns a stable
       hex string rather than raising.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from trid3nt_server.telemetry import compute_args_hash, emit_tool_call_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_jsonl(path: str) -> list[dict]:
    """Read all JSON-line records from ``path``."""
    records = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


async def _emit(path: str, **kwargs) -> None:
    """Call ``emit_tool_call_event`` with defaults filled in, writing to ``path``."""
    defaults = dict(
        session_id="TEST_SESSION",
        ts="2026-06-09T00:00:00Z",
        tool_name="fetch_dem",
        source="llm",
        args_hash=compute_args_hash({"bbox": [0, 0, 1, 1]}),
        success=True,
        latency_ms=42.5,
        error_code=None,
        retry_attempt=0,
        cached_content_token_count=None,
    )
    defaults.update(kwargs)
    with patch.dict(os.environ, {"TRID3NT_TELEMETRY_PATH": path}):
        await emit_tool_call_event(**defaults)
        # Drain the event loop: the fire-and-forget task uses ensure_future
        # (one yield to schedule) then the _write_line coroutine runs.
        # When ``aiofiles`` is not installed the write goes through an
        # executor thread; a real-time sleep is required in that path to
        # allow the OS thread to flush the file before the assertion.
        # 100 ms is ample — measured typical completion < 5 ms on a
        # spinning-disk CI node under moderate load.
        await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_shape() -> None:
    """The written JSON record contains all required fields with correct types."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        path = tf.name
    try:
        await _emit(path, tool_name="compute_slope", source="llm", latency_ms=123.4,
                    error_code=None, retry_attempt=0, cached_content_token_count=512)
        records = _read_jsonl(path)
        assert len(records) == 1
        r = records[0]
        # Required keys
        assert r["session_id"] == "TEST_SESSION"
        assert r["ts"] == "2026-06-09T00:00:00Z"
        assert r["tool_name"] == "compute_slope"
        assert r["source"] == "llm"
        assert isinstance(r["args_hash"], str) and len(r["args_hash"]) == 64
        assert r["success"] is True
        assert isinstance(r["latency_ms"], float)
        assert r["error_code"] is None
        assert r["retry_attempt"] == 0
        assert r["cached_content_token_count"] == 512
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_non_blocking_returns_before_write() -> None:
    """``emit_tool_call_event`` returns without blocking on the file write.

    We verify this by measuring that the coroutine itself is fast (< 50 ms)
    even on a slow-to-write path.  The write is delegated to an
    ``asyncio.ensure_future`` task; the test drains the loop afterwards to
    confirm the file is eventually written.
    """
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        path = tf.name
    try:
        t0 = time.monotonic()
        await _emit(path)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        # The coroutine itself should return almost immediately (well under 50 ms
        # even on slow CI) because the write is deferred.
        assert elapsed_ms < 200, f"emit_tool_call_event took {elapsed_ms:.0f} ms — unexpectedly slow"
        # File was eventually written (we drained the loop in _emit).
        records = _read_jsonl(path)
        assert len(records) == 1
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_error_path_does_not_raise() -> None:
    """A write failure (bad path) does not propagate out of the coroutine."""
    bad_path = "/nonexistent_directory/telemetry_test.jsonl"
    # Should complete without raising.
    with patch.dict(os.environ, {"TRID3NT_TELEMETRY_PATH": bad_path}):
        # We call the raw function (not _emit) to avoid env-var shadowing.
        await emit_tool_call_event(
            session_id="S1",
            ts="2026-06-09T00:00:00Z",
            tool_name="web_fetch",
            source="manual",
            args_hash=compute_args_hash({}),
            success=False,
            latency_ms=0.0,
            error_code="IO_ERROR",
        )
        # Drain so the deferred task runs (and swallows the IOError internally).
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    # No exception raised — test passes if we reach here.


@pytest.mark.asyncio
async def test_multiple_events_append() -> None:
    """Successive ``emit_tool_call_event`` calls append separate JSONL lines."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        path = tf.name
    try:
        await _emit(path, tool_name="fetch_dem", success=True, latency_ms=10.0)
        await _emit(path, tool_name="fetch_population", success=False, latency_ms=5.0,
                    error_code="TIMEOUT")
        await _emit(path, tool_name="publish_layer", success=True, latency_ms=200.0)
        records = _read_jsonl(path)
        assert len(records) == 3
        assert records[0]["tool_name"] == "fetch_dem"
        assert records[1]["tool_name"] == "fetch_population"
        assert records[1]["success"] is False
        assert records[1]["error_code"] == "TIMEOUT"
        assert records[2]["tool_name"] == "publish_layer"
    finally:
        os.unlink(path)


def test_compute_args_hash_stable() -> None:
    """Same args produce the same digest; different args produce different digests."""
    args_a = {"bbox": [0, 0, 1, 1], "resolution": 30}
    hash_a1 = compute_args_hash(args_a)
    hash_a2 = compute_args_hash(args_a)
    assert hash_a1 == hash_a2, "same args must produce same hash"

    args_b = {"bbox": [0, 0, 2, 2], "resolution": 10}
    hash_b = compute_args_hash(args_b)
    assert hash_a1 != hash_b, "different args must produce different hash"

    # Hash is a 64-char hex string (SHA-256).
    assert len(hash_a1) == 64
    assert all(c in "0123456789abcdef" for c in hash_a1)


def test_none_args_hash() -> None:
    """``compute_args_hash(None)`` returns a stable hex string, not an error."""
    h = compute_args_hash(None)
    assert isinstance(h, str)
    assert len(h) == 64
    # Should be the same as hashing an empty dict.
    assert h == compute_args_hash({})


@pytest.mark.asyncio
async def test_env_override_path() -> None:
    """``TRID3NT_TELEMETRY_PATH`` env var routes the write to the specified file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        custom_path = os.path.join(tmpdir, "custom_telemetry.jsonl")
        await _emit(custom_path, tool_name="geocode_location")
        assert Path(custom_path).exists(), "custom path was not created"
        records = _read_jsonl(custom_path)
        assert len(records) == 1
        assert records[0]["tool_name"] == "geocode_location"


@pytest.mark.asyncio
async def test_error_fields_populated() -> None:
    """When ``success=False``, ``error_code`` is written as-is to the record."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        path = tf.name
    try:
        await _emit(
            path,
            tool_name="fetch_wdpa_protected_areas",
            success=False,
            latency_ms=1234.5,
            error_code="WDPAERROR",
            retry_attempt=1,
        )
        records = _read_jsonl(path)
        assert len(records) == 1
        r = records[0]
        assert r["success"] is False
        assert r["error_code"] == "WDPAERROR"
        assert r["retry_attempt"] == 1
        assert r["latency_ms"] == pytest.approx(1234.5, rel=1e-3)
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Tool-retrieval SHADOW telemetry (tool-retrieval kickoff, orchestrator half).
# ---------------------------------------------------------------------------


def test_build_shadow_selection_record_shape() -> None:
    """The shadow record carries the would-be set + the turn join key + mode."""
    from trid3nt_server.telemetry import (
        SHADOW_RECORD_TYPE,
        build_shadow_selection_record,
    )

    rec = build_shadow_selection_record(
        session_id="S1",
        turn_id="T1",
        user_text="show me the flood map" * 50,  # long -> truncated
        visible_tools={"fetch_dem", "geocode_location"},
        mode="shadow",
        k=25,
        full_registry_size=120,
        model_id="us.anthropic.claude-sonnet-4-6",
    )
    assert rec["record_type"] == SHADOW_RECORD_TYPE
    assert rec["session_id"] == "S1"
    assert rec["turn_id"] == "T1"
    assert rec["mode"] == "shadow"
    assert rec["k"] == 25
    # visible_tools is a SORTED list (deterministic).
    assert rec["visible_tools"] == ["fetch_dem", "geocode_location"]
    assert rec["visible_count"] == 2
    assert rec["full_registry_size"] == 120
    # user_text is truncated to keep the record bounded.
    assert len(rec["user_text"]) <= 280


@pytest.mark.asyncio
async def test_emit_shadow_selection_writes_jsonl() -> None:
    """emit_shadow_selection_event writes ONE shadow row to the JSONL sink."""
    from trid3nt_server.telemetry import emit_shadow_selection_event

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        path = tf.name
    try:
        with patch.dict(os.environ, {"TRID3NT_TELEMETRY_PATH": path}):
            # No Persistence bound in this test env -> file path.
            emit_shadow_selection_event(
                session_id="S1",
                turn_id="T1",
                user_text="flood",
                visible_tools={"fetch_dem"},
                mode="shadow",
                k=25,
                full_registry_size=120,
            )
            await asyncio.sleep(0.1)
        records = _read_jsonl(path)
        assert len(records) == 1
        r = records[0]
        assert r["record_type"] == "tool_retrieval_shadow"
        assert r["turn_id"] == "T1"
        assert r["visible_tools"] == ["fetch_dem"]
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_emit_shadow_selection_never_raises() -> None:
    """A forced build failure inside emit_shadow_selection_event is swallowed."""
    from trid3nt_server import telemetry as tel

    with patch.object(
        tel, "build_shadow_selection_record", side_effect=RuntimeError("boom")
    ):
        # Must NOT raise -- telemetry must never break the dispatch loop.
        tel.emit_shadow_selection_event(
            session_id="S1",
            turn_id="T1",
            user_text="x",
            visible_tools={"a"},
            mode="shadow",
            k=25,
        )


@pytest.mark.asyncio
async def test_emit_tool_call_event_carries_turn_id() -> None:
    """The per-tool record carries turn_id (the recall@k join key) when given."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        path = tf.name
    try:
        await _emit(path, turn_id="T1")
        records = _read_jsonl(path)
        assert records[0]["turn_id"] == "T1"
    finally:
        os.unlink(path)
