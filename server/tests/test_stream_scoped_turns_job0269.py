"""job-0269: root-deselect + stream-scoped turn concurrency.

Live failures (2026-06-10 demo) this guards against:

1. Navigating from a Case to the Cases root was CLIENT-ONLY — the server's
   session-scoped active Case kept pointing at the last-opened Case, so a
   prompt sent from the root view skipped auto-create and dispatched INTO
   the stale Case (a terrain prompt landed in the flood Case), and
   re-selecting that Case looked like a no-op. Fix: ``case-command(deselect)``.

2. The M1 "cancel any in-flight turn on a new user-message" policy killed
   cross-Case work: a root terrain prompt cancelled a running cloud SFINCS
   solve (``workflows.executions.cancel`` was issued mid-execution). Fix:
   cancellation is scoped to the stream the new turn targets; turns in other
   Cases keep running, with per-turn captured history/narration lists so
   concurrent turns cannot cross-contaminate LLM context or persisted rows.
"""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server import server
from trid3nt_server.persistence import make_file_persistence
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


# --------------------------------------------------------------------------- #
# 1. deselect — server-side root navigation
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_deselect_clears_active_case(file_persistence) -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_a = await _create_case(ws, state, "Case A")
    assert state.active_case_id == case_a

    cmd = CaseCommandEnvelopePayload(command="deselect")
    await server._handle_case_command(ws, state, cmd)

    assert state.active_case_id is None
    assert state.case_context_synced_to is None
    assert state.chat_history == []


@pytest.mark.asyncio
async def test_root_prompt_after_deselect_autocreates_fresh_case(
    file_persistence,
) -> None:
    """The exact live failure: with a Case open, navigate out, prompt from
    root — the prompt must land in a NEW auto-created Case, not the old one."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_a = await _create_case(ws, state, "Flood Case")

    # Without deselect (pre-fix) this turn would have dispatched into case_a.
    cmd = CaseCommandEnvelopePayload(command="deselect")
    await server._handle_case_command(ws, state, cmd)

    directive = await server._prepare_user_turn(
        ws, state, "Fetch a DEM for Asheville and compute a hillshade"
    )
    assert directive is None
    new_case = state.active_case_id
    assert new_case and new_case != case_a, "root prompt must auto-create"
    assert state.current_turn_case_id == new_case

    chat_a = (await file_persistence.get_session_state(case_a)).chat_history
    chat_new = (await file_persistence.get_session_state(new_case)).chat_history
    assert chat_a == [], "old Case must not receive the root prompt"
    assert [m.role for m in chat_new] == ["user"]


@pytest.mark.asyncio
async def test_reselect_after_deselect_reopens_case(file_persistence) -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_a = await _create_case(ws, state, "Case A")
    await server._handle_case_command(
        ws, state, CaseCommandEnvelopePayload(command="deselect")
    )
    await server._handle_case_command(
        ws, state, CaseCommandEnvelopePayload(command="select", case_id=case_a)
    )
    assert state.active_case_id == case_a


# --------------------------------------------------------------------------- #
# 2. stream-scoped cancellation
# --------------------------------------------------------------------------- #


def _gated_stream(release: asyncio.Event, narration: str):
    async def stream(websocket, st, settings, user_text, research_mode, bedrock_model=None, **_kwargs):
        st.current_turn_narration = []
        st.current_turn_narration.append(narration)
        await release.wait()
        st.chat_history.append({"role": "user", "text": user_text})

    return stream


@pytest.mark.asyncio
async def test_cross_case_turn_survives_new_root_prompt(
    file_persistence, monkeypatch
) -> None:
    """A turn running in Case A must KEEP RUNNING when the user deselects,
    prompts from root (auto-create Case B), and that new turn dispatches."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_a = await _create_case(ws, state, "Case A")

    release = asyncio.Event()
    monkeypatch.setattr(
        server, "_stream_gemini_reply", _gated_stream(release, "solving A")
    )

    await server._prepare_user_turn(ws, state, "model the flood in A")
    key_a = state.current_turn_case_id or server._ROOT_STREAM_KEY
    task_a = asyncio.create_task(
        server._dispatch_gemini_and_persist(ws, state, None, "model the flood in A", "off")
    )
    state.inflight_tasks[key_a] = task_a
    await asyncio.sleep(0.05)

    # User navigates out + prompts from root → fresh Case key, NO collision.
    await server._handle_case_command(
        ws, state, CaseCommandEnvelopePayload(command="deselect")
    )
    await server._prepare_user_turn(ws, state, "hillshade for Asheville")
    key_b = state.current_turn_case_id or server._ROOT_STREAM_KEY
    assert key_b != key_a

    prior = state.inflight_tasks.get(key_b)
    assert prior is None, "fresh auto-created Case must have no prior turn"
    assert not task_a.cancelled() and not task_a.done(), (
        "Case A's turn must still be running after the cross-Case prompt"
    )

    release.set()
    await task_a
    chat_a = (await file_persistence.get_session_state(case_a)).chat_history
    assert ("agent", "solving A") in [(m.role, m.content) for m in chat_a]


@pytest.mark.asyncio
async def test_same_case_reprompt_replaces_turn(file_persistence, monkeypatch) -> None:
    """Re-prompting in the SAME Case keeps the M1 replace semantics."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await _create_case(ws, state, "Case A")

    release = asyncio.Event()
    monkeypatch.setattr(
        server, "_stream_gemini_reply", _gated_stream(release, "first ask")
    )

    await server._prepare_user_turn(ws, state, "first ask")
    key = state.current_turn_case_id or server._ROOT_STREAM_KEY
    task1 = asyncio.create_task(
        server._dispatch_gemini_and_persist(ws, state, None, "first ask", "off")
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


@pytest.mark.asyncio
async def test_concurrent_turns_keep_narration_isolated(
    file_persistence, monkeypatch
) -> None:
    """Turn A's persisted narration must be A's own text even when turn B
    runs concurrently and re-points ``state.current_turn_narration``.

    Uses the REAL ``_stream_gemini_reply`` registration seam: the per-task
    registry is what isolates the wrapper's finally-join. Here we simulate
    it by having each fake register in the same way the real stream does.
    """
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_a = await _create_case(ws, state, "Case A")

    release_a = asyncio.Event()

    async def stream_a(websocket, st, settings, user_text, research_mode, bedrock_model=None, **_kwargs):
        st.current_turn_narration = []
        narr = st.current_turn_narration
        task = asyncio.current_task()
        if task is not None:
            server._TURN_NARRATION_BY_TASK[task] = narr
        narr.append("narration A")
        await release_a.wait()

    monkeypatch.setattr(server, "_stream_gemini_reply", stream_a)
    await server._prepare_user_turn(ws, state, "turn A")
    task_a = asyncio.create_task(
        server._dispatch_gemini_and_persist(ws, state, None, "turn A", "off")
    )
    await asyncio.sleep(0.05)

    # Turn B (different Case) re-points the narration field mid-A.
    await server._handle_case_command(
        ws, state, CaseCommandEnvelopePayload(command="deselect")
    )

    async def stream_b(websocket, st, settings, user_text, research_mode, bedrock_model=None, **_kwargs):
        st.current_turn_narration = []
        st.current_turn_narration.append("narration B")

    monkeypatch.setattr(server, "_stream_gemini_reply", stream_b)
    await server._prepare_user_turn(ws, state, "turn B")
    await server._dispatch_gemini_and_persist(ws, state, None, "turn B", "off")
    case_b = state.active_case_id
    assert case_b and case_b != case_a

    release_a.set()
    await task_a

    chat_a = (await file_persistence.get_session_state(case_a)).chat_history
    chat_b = (await file_persistence.get_session_state(case_b)).chat_history
    agent_a = [m.content for m in chat_a if m.role == "agent"]
    agent_b = [m.content for m in chat_b if m.role == "agent"]
    assert agent_a == ["narration A"], f"A must keep its own narration: {agent_a}"
    assert agent_b == ["narration B"], f"B must keep its own narration: {agent_b}"
