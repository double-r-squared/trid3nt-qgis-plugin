"""The turn-complete envelope carries each model round's token counts, in round order."""

from __future__ import annotations

import asyncio
import json

import pytest

from trid3nt_server import server
from trid3nt_server.server.dispatch.persist import _TURN_USAGE_BY_TASK
from trid3nt_server.server.turn.wire import _emit_turn_complete
from trid3nt_contracts.common import new_ulid


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


def _complete(ws: FakeWS) -> dict:
    return next(m for m in ws.sent if m["type"] == "turn-complete")["payload"]


@pytest.mark.asyncio
async def test_emit_carries_usage_and_defaults_empty() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    rounds = [{"prompt": 9000, "completion": 40}, {"prompt": 9400, "completion": None}]
    await _emit_turn_complete(ws, state, pipeline_id="p", usage=rounds)
    assert _complete(ws)["usage"] == rounds
    ws2 = FakeWS()
    await _emit_turn_complete(ws2, state)
    assert _complete(ws2)["usage"] == []


@pytest.mark.asyncio
async def test_turn_usage_recorded_by_the_stream_reaches_turn_complete(monkeypatch) -> None:
    async def stream(websocket, st, settings, user_text, model_id=None, **_kw):
        task = asyncio.current_task()
        _TURN_USAGE_BY_TASK.setdefault(task, []).append({"prompt": 7, "completion": 3})

    monkeypatch.setattr(server, "_stream_model_reply", stream)
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await server._dispatch_model_turn_and_persist(ws, state, None, "hi", "off")
    assert _complete(ws)["usage"] == [{"prompt": 7, "completion": 3}]
