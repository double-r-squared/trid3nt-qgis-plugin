"""The per-turn and per-tool-call log lines, and the usage they are built from.

One ``turn`` line per user-message turn, every outcome, and one ``tool_call``
line per dispatch, each a JSON object on the server logger. Offline: a scripted
provider and a captured log."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server.adapters.model_selection import ModelSettings
from trid3nt_server.adapters.scripted_adapter import set_script
from trid3nt_contracts import new_ulid


def _lines(caplog, kind: str) -> list[dict]:
    prefix = f"{kind} {{"
    return [json.loads(r.getMessage()[len(kind) + 1:]) for r in caplog.records
            if r.getMessage().startswith(prefix)]


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

    from trid3nt_server.adapters.adapter import UsageMetadataEvent
    from trid3nt_server.adapters.openai_adapter import _stream_one_round

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




@dataclass
class _FakeSocket:
    sent: list = field(default_factory=list)

    async def send(self, msg: str) -> None:
        try:
            self.sent.append(json.loads(msg))
        except (json.JSONDecodeError, TypeError):
            self.sent.append(msg)


def _settings() -> ModelSettings:
    return ModelSettings(
        model="gemini-2.5-pro"
    )


@pytest.fixture()
def _scripted(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "scripted")
    yield
    set_script(None)


@pytest.mark.asyncio
async def test_turn_loop_writes_one_turn_line_and_one_tool_call_line(
    _scripted, caplog
):
    """A scripted tool turn writes exactly one turn line carrying the dispatch
    count and a null error_class, and one tool_call line for the dispatch."""
    set_script(
        [
            {"tool_call": {"name": "fetch_dem", "args": {"bbox": [0, 0, 1, 1]}}},
            {"text": "Here is the DEM."},
        ]
    )

    async def _dispatch(_ws, _state, name, _args):
        return {"status": "ok"}

    caplog.set_level(logging.INFO, logger="trid3nt_server.server")
    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    with patch.object(agent_server, "_invoke_tool_via_emitter", side_effect=_dispatch), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_model_reply(
            sock, state, _settings(), "get a DEM", "research"
        )
    turns = _lines(caplog, "turn")
    assert len(turns) == 1, "exactly one turn line per turn"
    rec = turns[0]
    assert rec["session_id"] == state.session_id
    assert rec["turn_id"]  # the pipeline id
    assert rec["tool_dispatch_count"] == 1
    assert rec["error_class"] is None
    assert rec["turn_wall_ms"] is not None and rec["turn_wall_ms"] >= 0
    calls = _lines(caplog, "tool_call")
    assert [c["tool_name"] for c in calls] == ["fetch_dem"]
    assert calls[0]["turn_id"] == rec["turn_id"]
    assert calls[0]["success"] is True


@pytest.mark.asyncio
async def test_turn_line_on_stream_failure_is_internal(_scripted, caplog):
    """A non-provider crash in the stream classifies error_class=internal --
    upstream failures are the only thing allowed to claim upstream_provider."""

    async def _boom(*_a, **_k):
        raise RuntimeError("some internal bug")
        yield  # pragma: no cover -- makes this an async generator

    caplog.set_level(logging.INFO, logger="trid3nt_server.server")
    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    with patch.object(agent_server, "stream_events_with_contents", _boom), \
         patch.object(agent_server, "build_tool_declarations", return_value=[]), \
         patch.object(agent_server, "_persist_terminal_failure_card"), \
         patch.object(agent_server, "_send_error"):
        await agent_server._stream_model_reply(
            sock, state, _settings(), "hello", "research"
        )
    turns = _lines(caplog, "turn")
    assert len(turns) == 1
    assert turns[0]["error_class"] == "internal"
