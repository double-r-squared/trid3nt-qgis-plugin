"""Root deselect, and turn cancellation scoped to one stream.

``case-command(deselect)`` clears the server's session-scoped active Case, so a
prompt from the Cases root auto-creates instead of dispatching into the last
Case. Cancellation is scoped to the stream the new turn targets, and each turn
captures its own history and narration, so concurrent turns cannot cross."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server import server
from trid3nt_server.store.cases import make_file_persistence
from trid3nt_contracts.case import CaseCommandEnvelopePayload
from trid3nt_contracts.common import new_ulid


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


@pytest.fixture()
def file_persistence(tmp_path):
    p = make_file_persistence(base_dir=tmp_path)
    server.set_persistence(p)
    try:
        yield p
    finally:
        server.set_persistence(None)


@pytest.fixture(autouse=True)
def _clean_session_registry():
    server._SESSION_ACTIVE_CASE.clear()
    yield
    server._SESSION_ACTIVE_CASE.clear()


async def _create_case(ws, state, title) -> str:
    cmd = CaseCommandEnvelopePayload(command="create", args={"title": title})
    await server._handle_case_command(ws, state, cmd)
    case_id = state.active_case_id
    assert case_id
    return case_id


def _gated_stream(release: asyncio.Event, narration: str):
    async def stream(websocket, st, settings, user_text, model_id=None, **_kwargs):
        st.current_turn_narration = []
        st.current_turn_narration.append(narration)
        await release.wait()
        st.chat_history.append({"role": "user", "text": user_text})

    return stream


@pytest.mark.asyncio
async def test_same_case_reprompt_replaces_turn(file_persistence, monkeypatch) -> None:
    """Re-prompting in the SAME Case keeps the M1 replace semantics."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await _create_case(ws, state, "Case A")

    release = asyncio.Event()
    monkeypatch.setattr(
        server, "_stream_model_reply", _gated_stream(release, "first ask")
    )

    await server._prepare_user_turn(ws, state, "first ask")
    key = state.current_turn_case_id or server._ROOT_STREAM_KEY
    task1 = asyncio.create_task(
        server._dispatch_model_turn_and_persist(ws, state, None, "first ask", "off")
    )
    state.inflight_tasks[key] = task1
    await asyncio.sleep(0.05)

    # Same-stream re-prompt → the recv-loop policy cancels the prior task.
    await server._prepare_user_turn(ws, state, "second ask")
    key2 = state.current_turn_case_id or server._ROOT_STREAM_KEY
    assert key2 == key
    prior = state.inflight_tasks.get(key2)
    assert prior is task1
    prior.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task1


