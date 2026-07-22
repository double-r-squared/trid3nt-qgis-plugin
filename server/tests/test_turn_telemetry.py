"""PER-TURN telemetry (LANE CORE, 2026-07-22).

One record per user-message turn -- {turn_id, session_id, case_id, model_id,
provider, prompt_tokens, completion_tokens, reasoning_tokens, turn_wall_ms,
tool_dispatch_count, error_class|null} -- persisted beside the tool telemetry
on its own JSONL sink (follows record_solve_telemetry's own-sink pattern) via
``telemetry.emit_turn_telemetry`` (fire-and-forget, off-loop write), plus the
per-model aggregates section folded into /api/telemetry/summary
(``telemetry.build_turn_summary`` -> ``turns_by_model``).

Offline: scripted provider + tmp JSONL sinks; no network, no model.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server import telemetry as tel
from trid3nt_server.adapter import GeminiSettings
from trid3nt_server.scripted_adapter import set_script
from trid3nt_contracts import new_ulid


# ---------------------------------------------------------------------------
# Record shape (pure builder)
# ---------------------------------------------------------------------------


def test_build_turn_record_shape():
    rec = tel.build_turn_telemetry_record(
        turn_id="T1",
        session_id="S1",
        case_id="C1",
        model_id="qwen3:8b",
        provider="openai",
        prompt_tokens=1000,
        completion_tokens=200,
        reasoning_tokens=50,
        turn_wall_ms=1234.56,
        tool_dispatch_count=3,
        error_class=None,
    )
    assert rec["record_type"] == tel.TURN_RECORD_TYPE == "turn"
    assert rec["turn_id"] == "T1"
    assert rec["session_id"] == "S1"
    assert rec["case_id"] == "C1"
    assert rec["model_id"] == "qwen3:8b"
    assert rec["provider"] == "openai"
    assert rec["prompt_tokens"] == 1000
    assert rec["completion_tokens"] == 200
    assert rec["reasoning_tokens"] == 50
    assert rec["turn_wall_ms"] == 1234.6
    assert rec["tool_dispatch_count"] == 3
    assert rec["error_class"] is None
    assert isinstance(rec["ts"], str) and rec["ts"].endswith("Z")


def test_build_turn_record_tolerates_absent_usage_as_null():
    """A provider that reports no usage yields nulls -- never fabricated 0s."""
    rec = tel.build_turn_telemetry_record(
        turn_id="T1",
        session_id="S1",
        case_id=None,
        model_id=None,
        provider=None,
        prompt_tokens=None,
        completion_tokens=None,
        reasoning_tokens=None,
        turn_wall_ms=None,
        tool_dispatch_count=0,
        error_class="upstream_provider",
    )
    assert rec["prompt_tokens"] is None
    assert rec["completion_tokens"] is None
    assert rec["reasoning_tokens"] is None
    assert rec["turn_wall_ms"] is None
    assert rec["error_class"] == "upstream_provider"


# ---------------------------------------------------------------------------
# Emit -> JSONL sink (async, fire-and-forget) + reader
# ---------------------------------------------------------------------------


def _read_jsonl(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.mark.asyncio
async def test_emit_turn_telemetry_writes_one_jsonl_row():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        path = tf.name
    os.unlink(path)  # emit must create it
    try:
        with patch.dict(os.environ, {"TRID3NT_TURN_TELEMETRY_PATH": path}):
            rec = tel.emit_turn_telemetry(
                turn_id="T1",
                session_id="S1",
                case_id="C1",
                model_id="m",
                provider="openai",
                prompt_tokens=10,
                completion_tokens=5,
                reasoning_tokens=None,
                turn_wall_ms=42.0,
                tool_dispatch_count=1,
                error_class=None,
            )
            assert rec is not None
            await asyncio.sleep(0.1)  # drain the fire-and-forget write task
        rows = _read_jsonl(path)
        assert len(rows) == 1
        assert rows[0]["record_type"] == "turn"
        assert rows[0]["turn_id"] == "T1"
        assert rows[0]["reasoning_tokens"] is None
        # The reader returns exactly this row.
        loaded = tel.load_turn_records(path)
        assert len(loaded) == 1 and loaded[0]["turn_id"] == "T1"
    finally:
        if os.path.exists(path):
            os.unlink(path)


@pytest.mark.asyncio
async def test_emit_turn_telemetry_never_raises():
    """A forced record-build failure is swallowed (telemetry never breaks
    the turn loop)."""
    with patch.object(
        tel, "build_turn_telemetry_record", side_effect=RuntimeError("boom")
    ):
        assert (
            tel.emit_turn_telemetry(
                turn_id="T1",
                session_id="S1",
                case_id=None,
                model_id=None,
                provider=None,
                prompt_tokens=None,
                completion_tokens=None,
                reasoning_tokens=None,
                turn_wall_ms=None,
                tool_dispatch_count=0,
            )
            is None
        )


def test_load_turn_records_missing_file_returns_empty():
    assert tel.load_turn_records("/nonexistent/turns.jsonl") == []


# ---------------------------------------------------------------------------
# Per-model summary aggregation
# ---------------------------------------------------------------------------


def _rec(model, provider="openai", prompt=None, completion=None, reasoning=None,
         wall=None, error=None):
    return tel.build_turn_telemetry_record(
        turn_id=new_ulid(),
        session_id="S",
        case_id=None,
        model_id=model,
        provider=provider,
        prompt_tokens=prompt,
        completion_tokens=completion,
        reasoning_tokens=reasoning,
        turn_wall_ms=wall,
        tool_dispatch_count=0,
        error_class=error,
    )


def test_build_turn_summary_per_model_aggregates():
    records = [
        _rec("a", prompt=100, completion=10, wall=1000.0),
        _rec("a", prompt=300, completion=30, wall=3000.0),
        _rec("a", error="upstream_provider"),
        _rec("b", prompt=50, completion=5, reasoning=7, wall=500.0,
             error="internal"),
    ]
    summary = tel.build_turn_summary(records)
    assert summary["total_turns"] == 4
    by_id = {m["model_id"]: m for m in summary["models"]}
    a, b = by_id["a"], by_id["b"]
    assert a["turns"] == 3
    # Means are over the turns that REPORTED the figure (the error row's null
    # tokens do not drag the mean to zero).
    assert a["mean_prompt_tokens"] == 200.0
    assert a["mean_completion_tokens"] == 20.0
    assert a["mean_reasoning_tokens"] is None  # never reported -> honest null
    assert a["mean_wall_ms"] == 2000.0
    assert a["upstream_error_count"] == 1
    assert a["error_count"] == 1
    assert b["turns"] == 1
    assert b["mean_reasoning_tokens"] == 7.0
    assert b["upstream_error_count"] == 0
    assert b["error_count"] == 1
    # Sorted by turns desc.
    assert summary["models"][0]["model_id"] == "a"


def test_empty_turn_summary_zero_state():
    assert tel.build_turn_summary([]) == {"total_turns": 0, "models": []}
    assert tel.empty_turn_summary() == {"total_turns": 0, "models": []}


@pytest.mark.asyncio
async def test_summary_endpoint_folds_turns_by_model():
    """/api/telemetry/summary carries the ``turns_by_model`` section, read
    from the turn-telemetry JSONL sink."""
    from trid3nt_server.tool_catalog_http import build_telemetry_summary

    with tempfile.NamedTemporaryFile(
        suffix=".jsonl", delete=False, mode="w", encoding="utf-8"
    ) as tf:
        tf.write(json.dumps(_rec("m1", prompt=10, completion=2, wall=100.0)) + "\n")
        tf.write(json.dumps(_rec("m1", error="upstream_provider")) + "\n")
        path = tf.name
    try:
        env = {
            "TRID3NT_TURN_TELEMETRY_PATH": path,
            # Point the tool-call sink somewhere empty so the rest of the
            # summary is the zero state (fast + deterministic).
            "TRID3NT_TELEMETRY_PATH": path + ".none",
            "TRID3NT_SOLVE_TELEMETRY_PATH": path + ".none",
        }
        with patch.dict(os.environ, env):
            summary = await build_telemetry_summary()
        section = summary["turns_by_model"]
        assert section["total_turns"] == 2
        m1 = section["models"][0]
        assert m1["model_id"] == "m1"
        assert m1["upstream_error_count"] == 1
        assert m1["mean_prompt_tokens"] == 10.0
        assert m1["mean_wall_ms"] == 100.0
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Reasoning-token capture at the openai usage seam
# ---------------------------------------------------------------------------


class _Namespace:
    """Attribute bag WITHOUT MagicMock's auto-attributes, so absent fields
    genuinely read as absent."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.mark.asyncio
async def test_openai_usage_carries_reasoning_tokens_when_reported():
    """usage.completion_tokens_details.reasoning_tokens -> the
    UsageMetadataEvent's reasoning_token_count; absent -> None (never
    fabricated)."""
    from unittest.mock import AsyncMock, MagicMock

    from trid3nt_server.adapter import UsageMetadataEvent
    from trid3nt_server.openai_adapter import _stream_one_round

    def _usage_chunk(details):
        return _Namespace(
            choices=[],
            usage=_Namespace(
                prompt_tokens=100,
                completion_tokens=20,
                total_tokens=120,
                completion_tokens_details=details,
            ),
        )

    async def _collect(chunk):
        async def _aiter():
            yield chunk

        stream = MagicMock()
        stream.__aenter__ = AsyncMock(return_value=_aiter())
        stream.__aexit__ = AsyncMock(return_value=False)
        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=stream)
        return [ev async for ev in _stream_one_round(client, {"model": "m"})]

    # Provider reports the figure -> captured.
    events = await _collect(_usage_chunk(_Namespace(reasoning_tokens=33)))
    usage_events = [e for e in events if isinstance(e, UsageMetadataEvent)]
    assert len(usage_events) == 1
    assert usage_events[0].reasoning_token_count == 33
    # Provider omits it -> honest None.
    events = await _collect(_usage_chunk(None))
    usage_events = [e for e in events if isinstance(e, UsageMetadataEvent)]
    assert usage_events[0].reasoning_token_count is None


# ---------------------------------------------------------------------------
# End-to-end: the turn loop emits exactly ONE record per turn
# ---------------------------------------------------------------------------


@dataclass
class _FakeSocket:
    sent: list = field(default_factory=list)

    async def send(self, msg: str) -> None:
        try:
            self.sent.append(json.loads(msg))
        except (json.JSONDecodeError, TypeError):
            self.sent.append(msg)


def _settings() -> GeminiSettings:
    return GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )


@pytest.fixture()
def _scripted(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "scripted")
    yield
    set_script(None)


@pytest.mark.asyncio
async def test_turn_loop_emits_one_turn_record(_scripted, monkeypatch):
    """A scripted tool turn emits exactly one per-turn record carrying the
    dispatch count and a null error_class."""
    set_script(
        [
            {"tool_call": {"name": "fetch_dem", "args": {"bbox": [0, 0, 1, 1]}}},
            {"text": "Here is the DEM."},
        ]
    )
    captured: list[dict] = []

    def _capture(**kw):
        captured.append(kw)
        return kw

    async def _dispatch(_ws, _state, name, _args):
        return {"status": "ok"}

    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    with patch.object(agent_server, "emit_turn_telemetry", _capture), \
         patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_dispatch), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_gemini_reply(
            sock, state, _settings(), "get a DEM", "research"
        )
    assert len(captured) == 1, "exactly one turn record per turn"
    rec = captured[0]
    assert rec["session_id"] == state.session_id
    assert rec["turn_id"]  # the pipeline id
    assert rec["tool_dispatch_count"] == 1
    assert rec["error_class"] is None
    assert rec["turn_wall_ms"] is not None and rec["turn_wall_ms"] >= 0


@pytest.mark.asyncio
async def test_turn_loop_record_on_stream_failure_is_internal(
    _scripted, monkeypatch
):
    """A non-provider crash in the stream classifies error_class=internal --
    upstream failures are the only thing allowed to claim upstream_provider."""
    captured: list[dict] = []

    def _capture(**kw):
        captured.append(kw)
        return kw

    async def _boom(*_a, **_k):
        raise RuntimeError("some internal bug")
        yield  # pragma: no cover -- makes this an async generator

    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    with patch.object(agent_server, "emit_turn_telemetry", _capture), \
         patch.object(agent_server, "stream_events_with_contents", _boom), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]), \
         patch.object(agent_server, "_persist_terminal_failure_card"), \
         patch.object(agent_server, "_send_error"):
        await agent_server._stream_gemini_reply(
            sock, state, _settings(), "hello", "research"
        )
    assert len(captured) == 1
    assert captured[0]["error_class"] == "internal"
