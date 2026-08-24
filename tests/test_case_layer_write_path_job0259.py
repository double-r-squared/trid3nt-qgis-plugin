"""job-0259 — Case layers not rehydrating: WRITE-path root-cause regression tests.

Root cause (diagnosed live, 2026-06-10): the web client mounts TWO WebSocket
connections per tab (Chat.tsx carries ``user-message``; App.tsx carries
``case-command`` — see web/src/ws.ts job-0159 hub comment), while the server
kept ``active_case_id`` on the per-connection ``SessionState``. The
``case-command(select|create)`` landed on App's connection; every tool
dispatch + persistence write ran on Chat's connection with
``active_case_id=None`` — so ``_persist_chat_turn`` and
``_persist_case_loaded_layers`` silently no-opped and a Case re-open showed
``chat=0 layers=0`` (verified against /tmp/agent_demo_ready.log: every single
case-open emitted by the live demo agent showed chat=0 layers=0 while 31-33
Cases existed).

Fix under test:

1. ``SessionState.active_case_id`` is now a property backed by the
   session-scoped ``_SESSION_ACTIVE_CASE`` registry — every connection of a
   session (including post-reconnect replacements) observes the same Case.
2. ``_sync_case_context`` catches a connection's in-memory context
   (chat_history + emitter loaded_layers seed) up to the session's active
   Case at user-message time.
3. The layer persist moved into the ``finally`` of
   ``_invoke_tool_via_emitter`` so a post-invoke envelope-send failure on a
   dying WebSocket no longer skips it (the round-3 plume scenario).
4. ``_persist_case_loaded_layers`` merges by ``layer_id`` (append/replace,
   never clobber) so an unseeded emitter cannot erase persisted layers.

All tests are Gemini-free and run against the REAL file-backed persistence
substrate (``make_file_persistence``) pointed at a pytest tmpdir.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import trid3nt_server.server as server
from trid3nt_server import tools as agent_tools
from trid3nt_server.persistence import make_file_persistence
from trid3nt_server.tools import RegisteredTool
from trid3nt_contracts.case import CaseCommandEnvelopePayload, CaseSummary
from trid3nt_contracts.common import new_ulid, now_utc
from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

FAKE_TOOL = "fake_layer_tool_job0259"


def _make_layer(layer_id: str = "L-plume-001") -> LayerURI:
    return LayerURI(
        layer_id=layer_id,
        name="plume concentration",
        layer_type="raster",
        uri=f"https://qgis.example/wms?LAYERS={layer_id}",
        style_preset="continuous_flood_depth",
        role="primary",
    )


class FakeWS:
    """Minimal ServerConnection stand-in: records every envelope sent."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, text: str) -> None:
        self.sent.append(text)


class DyingWS(FakeWS):
    """WS whose wire dies for session-state envelopes — models the browser
    reload mid-turn (round-3 plume scenario): pipeline-state writes succeed,
    then the post-invoke ``session-state`` emission raises."""

    async def send(self, text: str) -> None:
        if '"session-state"' in text:
            raise RuntimeError("ConnectionClosed: no close frame received or sent")
        await super().send(text)


@pytest.fixture()
def file_persistence(tmp_path, monkeypatch):
    """Bind REAL file-backed persistence (tmpdir) as the server singleton."""
    p = make_file_persistence(base_dir=tmp_path)
    server.set_persistence(p)
    try:
        yield p
    finally:
        server.set_persistence(None)


@pytest.fixture()
def fake_layer_tool():
    """Register a registry tool returning a LayerURI; deregister on teardown."""

    async def _fn() -> LayerURI:
        return _make_layer()

    meta = AtomicToolMetadata(
        name=FAKE_TOOL, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[FAKE_TOOL] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield FAKE_TOOL
    finally:
        agent_tools.TOOL_REGISTRY.pop(FAKE_TOOL, None)


@pytest.fixture(autouse=True)
def _clean_session_registry():
    """Keep the session-scoped Case registry hermetic per test."""
    server._SESSION_ACTIVE_CASE.clear()
    yield
    server._SESSION_ACTIVE_CASE.clear()


async def _create_case_via_command(ws, state, title="Split Brain Demo") -> str:
    cmd = CaseCommandEnvelopePayload(command="create", args={"title": title})
    await server._handle_case_command(ws, state, cmd)
    case_id = state.active_case_id
    assert case_id, "create must bind the active case"
    return case_id


@pytest.mark.asyncio
async def test_split_brain_two_connections_layer_and_chat_persist(
    file_persistence, fake_layer_tool
) -> None:
    """THE root-cause regression: case-command on socket A, tool on socket B.

    Models the production web client exactly: App.tsx's connection receives
    ``case-command(create)``; Chat.tsx's connection (a DIFFERENT SessionState
    bound to the SAME session_id) dispatches the tool. Pre-fix, socket B saw
    ``active_case_id=None`` and persisted nothing.
    """
    session_id = new_ulid()
    ws_a, ws_b = FakeWS(), FakeWS()
    state_a = server.SessionState(session_id=session_id)  # App.tsx socket
    state_b = server.SessionState(session_id=session_id)  # Chat.tsx socket

    case_id = await _create_case_via_command(ws_a, state_a)

    # Socket B observes the session-scoped binding without any case-command.
    assert state_b.active_case_id == case_id

    # user-message path on socket B: sync, persist user turn, dispatch tool.
    await server._sync_case_context(ws_b, state_b)
    await server._persist_chat_turn(state_b, role="user", content="model the plume")
    result = await server._invoke_tool_via_emitter(ws_b, state_b, FAKE_TOOL, {})
    assert isinstance(result, LayerURI)
    # F97: the dispatch mints a UNIQUE layer_id for the freshly-fetched layer,
    # so the persisted id is the MINTED id (not the tool's source-derived one).
    minted_id = result.layer_id

    # Rehydration read-back — the layer AND the chat turn must round-trip.
    session_state = await file_persistence.get_session_state(case_id)
    assert len(session_state.loaded_layers) == 1
    assert session_state.loaded_layers[0]["layer_id"] == minted_id
    assert session_state.case.layer_summary == [minted_id]
    # job-0267: the tool dispatch now ALSO persists a replayable tool-card
    # row (role="tool"), interleaved after the user turn by created_at.
    assert len(session_state.chat_history) == 2
    assert session_state.chat_history[0].role == "user"
    assert session_state.chat_history[0].content == "model the plume"
    assert session_state.chat_history[1].role == "tool"
    assert session_state.chat_history[1].tool_card is not None
    assert session_state.chat_history[1].tool_card.tool_name == FAKE_TOOL
    assert session_state.chat_history[1].tool_card.state == "complete"


@pytest.mark.asyncio
async def test_case_open_after_reconnect_rehydrates_layers(
    file_persistence, fake_layer_tool
) -> None:
    """Full loop: create + publish on one session, reopen on a fresh one."""
    session_id = new_ulid()
    ws, state = FakeWS(), server.SessionState(session_id=session_id)
    case_id = await _create_case_via_command(ws, state)
    published = await server._invoke_tool_via_emitter(ws, state, FAKE_TOOL, {})
    assert isinstance(published, LayerURI)
    # F97: the persisted/rehydrated id is the MINTED unique id, stable across
    # the reconnect (no re-fetch on reopen — durability holds).
    minted_id = published.layer_id

    # Fresh "browser" — new session, new connection, case-open select.
    ws2 = FakeWS()
    state2 = server.SessionState(session_id=new_ulid())
    await server._emit_case_open(ws2, state2, case_id)
    assert state2.emitter is not None
    seeded = state2.emitter.loaded_layers
    assert [layer.layer_id for layer in seeded] == [minted_id]


@pytest.mark.asyncio
async def test_no_write_without_active_case(
    file_persistence, fake_layer_tool, tmp_path
) -> None:
    """No active Case → no projects write, and the dispatch never raises."""
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    assert state.active_case_id is None
    await server._sync_case_context(ws, state)
    result = await server._invoke_tool_via_emitter(ws, state, FAKE_TOOL, {})
    assert isinstance(result, LayerURI)
    projects = tmp_path / "trid3nt_dev" / "projects.json"
    assert (not projects.exists()) or projects.read_text().strip() in ("{}", "")


@pytest.mark.asyncio
async def test_persist_fires_even_when_post_invoke_emission_fails(
    file_persistence, fake_layer_tool
) -> None:
    """Round-3 plume scenario: tool succeeds, the WS dies before the
    session-state emission. The layer MUST still persist.

    CONTRACT UPDATE (terminal-pipeline-card hardening, Fix 3 / Gap 1): the
    emitter sink (server._ensure_emitter._sink) now SWALLOWS a mid-close
    ``websocket.send`` failure best-effort instead of letting ConnectionClosed
    escape. Previously a dying WS let the RuntimeError propagate out of the
    dispatch (the layer persisted only via the ``finally`` path). Now the
    dispatch COMPLETES cleanly — the terminal pipeline frame is never lost to a
    closing socket, the CancelledError/terminal path is never derailed by a
    send error, and the layer still persists. Both contracts guarantee
    persistence; the new one additionally guarantees the dispatch does not raise
    on a transient socket close."""
    session_id = new_ulid()
    ws_create = FakeWS()
    state = server.SessionState(session_id=session_id)
    case_id = await _create_case_via_command(ws_create, state)

    # Replace the emitter with one bound to a dying wire.
    dying = DyingWS()
    state.emitter = None
    server._ensure_emitter(dying, state)

    # Fix 3: the swallowing sink means a dying WS no longer raises out of the
    # dispatch. The tool result (a LayerURI) is returned normally.
    result = await server._invoke_tool_via_emitter(dying, state, FAKE_TOOL, {})
    assert isinstance(result, LayerURI)

    session_state = await file_persistence.get_session_state(case_id)
    assert len(session_state.loaded_layers) == 1, (
        "published layer must persist even when the post-invoke envelope "
        "send failed on a dying WebSocket"
    )


@pytest.mark.asyncio
async def test_merge_preserves_previously_persisted_layers(
    file_persistence, fake_layer_tool
) -> None:
    """An UNSEEDED emitter (no sync) must never clobber persisted layers."""
    session_id = new_ulid()
    ws, state = FakeWS(), server.SessionState(session_id=session_id)
    case_id = await _create_case_via_command(ws, state)

    # Persist a pre-existing layer directly on the Case record (as a prior
    # session would have).
    case = await file_persistence.get_case(case_id)
    prior = _make_layer("L-prior-000").model_dump(mode="json")
    prior_summary = {
        "layer_id": "L-prior-000",
        "name": prior["name"],
        "layer_type": prior["layer_type"],
        "uri": prior["uri"],
        "style_preset": prior["style_preset"],
        "visible": True,
        "role": "primary",
        "temporal": False,
    }
    await file_persistence.upsert_case(
        case.model_copy(
            update={
                "loaded_layer_summaries": [prior_summary],
                "layer_summary": ["L-prior-000"],
                "updated_at": now_utc(),
            }
        )
    )

    # New connection, same session — deliberately SKIP _sync_case_context so
    # the emitter starts empty (models a sync failure / legacy path).
    ws2 = FakeWS()
    state2 = server.SessionState(session_id=session_id)
    published = await server._invoke_tool_via_emitter(ws2, state2, FAKE_TOOL, {})
    assert isinstance(published, LayerURI)
    # F97: the freshly-published layer carries a MINTED unique id; the merge must
    # union it with the previously-persisted "L-prior-000" rather than clobber.
    minted_id = published.layer_id

    session_state = await file_persistence.get_session_state(case_id)
    ids = sorted(d["layer_id"] for d in session_state.loaded_layers)
    assert ids == sorted([minted_id, "L-prior-000"]), (
        "merge-by-layer_id must union persisted + emitter layers, "
        f"got {ids}"
    )


@pytest.mark.asyncio
async def test_sync_clears_stale_llm_context_on_cross_socket_case_switch(
    file_persistence,
) -> None:
    """OQ-0245 through the two-socket lens: when the case switches on a
    sibling socket, the chat socket's next dispatch resets its LLM context."""
    session_id = new_ulid()
    ws_a, ws_b = FakeWS(), FakeWS()
    state_a = server.SessionState(session_id=session_id)
    state_b = server.SessionState(session_id=session_id)

    case_1 = await _create_case_via_command(ws_a, state_a, title="Case One")
    await server._sync_case_context(ws_b, state_b)
    state_b.chat_history.append({"role": "user", "text": "old case context"})

    case_2 = await _create_case_via_command(ws_a, state_a, title="Case Two")
    assert case_2 != case_1
    assert state_b.active_case_id == case_2  # session-scoped binding

    await server._sync_case_context(ws_b, state_b)
    assert state_b.chat_history == [], (
        "case switch on a sibling socket must reset this socket's LLM context"
    )
    assert state_b.case_context_synced_to == case_2

    # Idempotent: a second sync for the same case must not re-clear.
    state_b.chat_history.append({"role": "user", "text": "new case turn"})
    await server._sync_case_context(ws_b, state_b)
    assert len(state_b.chat_history) == 1


@pytest.mark.asyncio
async def test_session_binding_survives_reconnect(file_persistence) -> None:
    """A reconnect (new SessionState, same session_id) keeps the active Case."""
    session_id = new_ulid()
    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    case_id = await _create_case_via_command(ws, state)

    reconnected = server.SessionState(session_id=session_id)
    assert reconnected.active_case_id == case_id
