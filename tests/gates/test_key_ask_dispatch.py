"""A keyed tool the provider asks a key for is asked of the person on a card and runs once more with it."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.ws import PayloadConfirmationEnvelopePayload
from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_server.inputs.gate.errors import UserDeclinedError
from trid3nt_server.inputs.gate.pending import _PENDING_CONFIRMATIONS
from trid3nt_server.model.credentials import resolver
from trid3nt_server.server import SessionState, _invoke_tool_via_emitter
from trid3nt_server.tools import TOOL_REGISTRY, RegisteredTool
from trid3nt_server.tools.fetchers._fetch_common import KeyRejectedError, KeyRequiredError

_NAME = "keyed_dummy_tool"
_TYPED = "typed-test-key-91c2"
_calls: list[str | None] = []


def _keyed_tool(**_kwargs: Any) -> dict:
    """Needs the airnow key; refuses the first stored one the way a provider would."""
    value = resolver.stored_value("airnow")
    _calls.append(value)
    if value is None:
        raise KeyRequiredError(_NAME, "airnow", "EPA AirNow")
    if value != _TYPED:
        raise KeyRejectedError(_NAME, "airnow", "EPA AirNow")
    return {"read_with_key": True}


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, raw: Any) -> None:
        self.sent.append(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)


@pytest.fixture(autouse=True)
def _tool(monkeypatch):
    monkeypatch.setattr("trid3nt_server.inputs.gate.confirm.GATE_WAIT_CAP_S", 5)
    monkeypatch.delenv("TRID3NT_AIRNOW_API_KEY", raising=False)
    resolver._SESSION_CREDENTIALS.clear()
    _calls.clear()
    TOOL_REGISTRY[_NAME] = RegisteredTool(
        metadata=AtomicToolMetadata(name=_NAME, ttl_class="live-no-cache", source_class="dummy", cacheable=False),
        fn=_keyed_tool, module=__name__)
    yield
    TOOL_REGISTRY.pop(_NAME, None)
    resolver._SESSION_CREDENTIALS.clear()


async def _answer_card(session_id: str, decision: str, store: str | None) -> None:
    for _ in range(1000):
        if _PENDING_CONFIRMATIONS:
            break
        await asyncio.sleep(0.005)
    wid = next(iter(_PENDING_CONFIRMATIONS))
    if store is not None:
        resolver.set_session_credential(session_id, "airnow", store)
    _PENDING_CONFIRMATIONS[wid][1].set_result(
        PayloadConfirmationEnvelopePayload(warning_id=wid, decision=decision))


def test_the_card_stores_the_key_and_the_call_runs_again_with_it() -> None:
    async def run():
        ws, state = _FakeWS(), SessionState(session_id=new_ulid())
        answer = asyncio.create_task(_answer_card(state.session_id, "proceed", _TYPED))
        out = await _invoke_tool_via_emitter(ws, state, _NAME, {})
        await answer
        return ws, out

    ws, out = asyncio.run(run())
    assert out == {"read_with_key": True}
    assert _calls == [None, _TYPED]
    assert _TYPED not in "".join(str(m) for m in ws.sent)
    assert any("tool-payload-warning" in str(m) for m in ws.sent)


def test_a_cancelled_card_is_a_decline_and_the_tool_ran_once() -> None:
    async def run():
        ws, state = _FakeWS(), SessionState(session_id=new_ulid())
        answer = asyncio.create_task(_answer_card(state.session_id, "cancel", None))
        try:
            with pytest.raises(UserDeclinedError):
                await _invoke_tool_via_emitter(ws, state, _NAME, {})
        finally:
            await answer

    asyncio.run(run())
    assert _calls == [None]


def test_a_key_the_provider_refuses_again_after_the_card_is_not_asked_twice() -> None:
    async def run():
        ws, state = _FakeWS(), SessionState(session_id=new_ulid())
        answer = asyncio.create_task(_answer_card(state.session_id, "proceed", "wrong-test-key"))
        try:
            with pytest.raises(KeyRejectedError):
                await _invoke_tool_via_emitter(ws, state, _NAME, {})
        finally:
            await answer

    asyncio.run(run())
    assert _calls == [None, "wrong-test-key"]
