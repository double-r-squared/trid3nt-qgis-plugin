"""job-0268: turn-start Case binding — cross-Case contamination regression tests.

The job-0267 adversarial verifier proved (probes A+B in
``reports/inflight/job-0267-agent-20260610/verify/test_adversarial_job0267.py``)
that every persistence site read ``state.active_case_id`` at WRITE time, so a
``case-command(select)`` arriving mid-stream re-aimed in-flight writes: Case
A's narration and tool cards persisted into Case B permanently. The window is
minutes-long for SFINCS-class tools.

The fix pins the turn's Case once (``SessionState.current_turn_case_id``, set
by ``_prepare_user_turn`` after the auto-create hand-off) and threads it
through every turn-scoped write: chat rows, tool cards, layer attribution,
per-Case .qgs routing, and chart persistence. The dispatch wrappers capture
the binding at task entry so even a cancel-and-redispatch (new turn re-pins
while the old turn's finally-persist is still pending) cannot cross-paint.

These tests are the INVERSIONS of the job-0267 expected-bug probes — the
contaminated behavior those probes demonstrated must never come back.
"""

from __future__ import annotations

import asyncio

import pytest

from grace2_agent import server
from grace2_agent import tools as agent_tools
from grace2_agent.persistence import make_file_persistence
from grace2_agent.tools import RegisteredTool
from grace2_contracts.case import CaseCommandEnvelopePayload
from grace2_contracts.common import new_ulid
from grace2_contracts.tool_registry import AtomicToolMetadata


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


def _register_gated_tool(name: str, gate: asyncio.Event) -> None:
    async def _fn() -> dict:
        await gate.wait()
        return {"ok": True}

    meta = AtomicToolMetadata(
        name=name, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )


# --------------------------------------------------------------------------- #
# Inverted probe A: narration stays in the OWNING Case on a mid-stream switch.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_narration_stays_in_owning_case_on_midstream_switch(
    file_persistence, monkeypatch
) -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_a = await _create_case(ws, state, "Case A")

    release = asyncio.Event()

    async def slow_stream(websocket, st, settings, user_text, research_mode, bedrock_model=None, **_kwargs):
        st.current_turn_narration = []
        st.current_turn_narration.append("Case A flood narration.")
        await release.wait()
        st.chat_history.append({"role": "user", "text": user_text})

    monkeypatch.setattr(server, "_stream_gemini_reply", slow_stream)

    # Real pre-dispatch path: pins the turn binding + persists the user row.
    directive = await server._prepare_user_turn(ws, state, "flood in A")
    assert directive is None
    assert state.current_turn_case_id == case_a

    task = asyncio.create_task(
        server._dispatch_gemini_and_persist(ws, state, None, "flood in A", "off")
    )
    await asyncio.sleep(0.05)  # stream mid-flight, accumulator full

    # User clicks Case B in the left rail mid-stream -> case-command(select).
    case_b = await _create_case(ws, state, "Case B")
    sel = CaseCommandEnvelopePayload(command="select", case_id=case_b)
    await server._handle_case_command(ws, state, sel)

    release.set()
    await task

    chat_a = (await file_persistence.get_session_state(case_a)).chat_history
    chat_b = (await file_persistence.get_session_state(case_b)).chat_history

    assert [(m.role, m.content) for m in chat_a] == [
        ("user", "flood in A"),
        ("agent", "Case A flood narration."),
    ], f"Case A must own its full turn, got {[(m.role, m.content) for m in chat_a]}"
    assert chat_b == [], f"Case B must stay clean, got {chat_b}"


# --------------------------------------------------------------------------- #
# Inverted probe B: tool card stays in the OWNING Case on a mid-dispatch switch.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tool_card_stays_in_owning_case_on_middispatch_switch(
    file_persistence,
) -> None:
    name = "job0268_slow_tool"
    gate = asyncio.Event()
    _register_gated_tool(name, gate)
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        case_a = await _create_case(ws, state, "Case A")

        task = asyncio.create_task(
            server._invoke_tool_via_emitter(ws, state, name, {})
        )
        await asyncio.sleep(0.05)  # dispatch in flight under Case A

        case_b = await _create_case(ws, state, "Case B")
        sel = CaseCommandEnvelopePayload(command="select", case_id=case_b)
        await server._handle_case_command(ws, state, sel)

        gate.set()
        await task
    finally:
        agent_tools.TOOL_REGISTRY.pop(name, None)

    tools_a = [
        m
        for m in (await file_persistence.get_session_state(case_a)).chat_history
        if m.role == "tool"
    ]
    tools_b = [
        m
        for m in (await file_persistence.get_session_state(case_b)).chat_history
        if m.role == "tool"
    ]
    assert len(tools_a) == 1 and tools_a[0].tool_card.tool_name == name, (
        f"tool card must stay in Case A, got {tools_a}"
    )
    assert tools_b == [], f"Case B must not receive Case A's tool card: {tools_b}"


# --------------------------------------------------------------------------- #
# Cancel-and-redispatch race: a NEW turn re-pins the binding while the OLD
# turn's finally-persist is still pending. Entry-time capture must hold.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_new_turn_repin_does_not_steal_old_turn_narration(
    file_persistence, monkeypatch
) -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case_a = await _create_case(ws, state, "Case A")

    release = asyncio.Event()
    narration_text = "Old turn narration for A."

    async def slow_stream(websocket, st, settings, user_text, research_mode, bedrock_model=None, **_kwargs):
        # Freeze the accumulator contents for the old turn only.
        st.current_turn_narration = [narration_text]
        await release.wait()

    monkeypatch.setattr(server, "_stream_gemini_reply", slow_stream)

    await server._prepare_user_turn(ws, state, "old turn in A")
    old_task = asyncio.create_task(
        server._dispatch_gemini_and_persist(ws, state, None, "old turn in A", "off")
    )
    await asyncio.sleep(0.05)  # old turn entry-capture done, stream parked

    # User switches to Case B and sends a NEW message — the real recv loop
    # cancels the old task then re-pins via _prepare_user_turn.
    case_b = await _create_case(ws, state, "Case B")
    sel = CaseCommandEnvelopePayload(command="select", case_id=case_b)
    await server._handle_case_command(ws, state, sel)
    await server._prepare_user_turn(ws, state, "new turn in B")
    assert state.current_turn_case_id == case_b  # binding re-pinned

    release.set()
    await old_task  # old turn's finally-persist runs AFTER the re-pin

    chat_a = (await file_persistence.get_session_state(case_a)).chat_history
    chat_b = (await file_persistence.get_session_state(case_b)).chat_history

    assert ("agent", narration_text) in [(m.role, m.content) for m in chat_a], (
        f"old narration must land in Case A, got {[(m.role, m.content) for m in chat_a]}"
    )
    assert [(m.role, m.content) for m in chat_b] == [("user", "new turn in B")], (
        f"Case B must hold only its own user row, got "
        f"{[(m.role, m.content) for m in chat_b]}"
    )


# --------------------------------------------------------------------------- #
# Auto-create hand-off guard (job-0267 probe D, unchanged semantics): a root
# prompt binds the auto-created Case BEFORE any write; user + tool + agent
# rows all land in it.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_auto_created_case_receives_full_stream(
    file_persistence, monkeypatch
) -> None:
    name = "job0268_root_tool"

    async def _fn() -> dict:
        return {"rows": 1}

    meta = AtomicToolMetadata(
        name=name, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        assert state.active_case_id is None  # Cases root

        directive = await server._prepare_user_turn(
            ws, state, "model the flood in fort myers"
        )
        assert directive is None
        auto_case = state.active_case_id
        assert auto_case, "auto-create did not bind a Case"
        assert state.current_turn_case_id == auto_case  # pin follows hand-off

        async def fake_stream(websocket, st, settings, user_text, research_mode, bedrock_model=None, **_kwargs):
            st.current_turn_narration = []
            st.current_turn_narration.append("Working. ")
            await server._invoke_tool_via_emitter(websocket, st, name, {})
            st.current_turn_narration.append("Done.")
            st.chat_history.append({"role": "user", "text": user_text})

        monkeypatch.setattr(server, "_stream_gemini_reply", fake_stream)
        await server._dispatch_gemini_and_persist(
            ws, state, None, "model the flood in fort myers", "off"
        )
    finally:
        agent_tools.TOOL_REGISTRY.pop(name, None)

    rows = (await file_persistence.get_session_state(auto_case)).chat_history
    assert [m.role for m in rows] == ["user", "tool", "agent"]
    assert rows[0].content == "model the flood in fort myers"
    assert rows[2].content == "Working. Done."


# --------------------------------------------------------------------------- #
# job-0281: the turn's zoom-to emissions persist on accumulator-snapshot rows
# (Case-reopen snap-to-location replays the LAST one — job-0280 web).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_map_command_emissions_persist_on_agent_row(
    file_persistence,
) -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    case = await _create_case(ws, state, "Snap Case")
    state.current_turn_map_commands = [
        {"command": "zoom-to", "args": {"bbox": [-105.3, 39.9, -105.1, 40.1]}}
    ]
    await server._persist_chat_turn(state, role="agent", content="done")
    # Tool rows snapshot NOTHING (mirrors layer_emissions=[]).
    await server._persist_chat_turn(
        state, role="tool", content="{}", layer_emissions=[]
    )
    chat = (await file_persistence.get_session_state(case)).chat_history
    agent_rows = [m for m in chat if m.role == "agent"]
    tool_rows = [m for m in chat if m.role == "tool"]
    assert agent_rows[0].map_command_emissions == [
        {"command": "zoom-to", "args": {"bbox": [-105.3, 39.9, -105.1, 40.1]}}
    ]
    assert tool_rows[0].map_command_emissions == []
