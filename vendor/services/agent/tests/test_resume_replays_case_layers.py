"""job-0356: a RENDERED layer must survive any WS reconnect — without a
case-open.

The AGENT half of the per-Case layer DURABILITY requirement (NATE, hard
requirement): once a layer is rendered in a Case it must stay rendered across
any WS reconnect; the user must NEVER have to exit/re-enter a Case to get layers
back.

job-0355 made an in-flight solve SURVIVE a disconnect (the live-turn registry +
``rebind_sink``) and ``_handle_session_resume`` rebinds those LIVE turns onto the
reconnecting socket. But a layer that COMPLETED + rendered BEFORE the disconnect
has NO live turn, so a bare reconnect (``session-resume`` with no in-flight turn)
replayed an EMPTY session-state and the user's already-rendered layers vanished
until an explicit case-open.

These tests pin the fix in ``_handle_session_resume`` /
``_replay_active_case_layers``:

  (a) a BARE reconnect with an active Case + NO live turn replays the Case's
      persisted ``loaded_layers`` snapshot to the new socket (the A.7
      replace-not-reconcile ``session-state`` the client already renders).
  (b) reconnect-WHILE-SOLVING delivers exactly ONE set of session-state frames
      (the rebound live-turn emitter is the single writer — the resume must NOT
      also seed + emit a second snapshot through the new connection's emitter).
  (c) reconnect with NO active Case replays nothing (no crash; empty snapshot,
      exactly as before).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grace2_agent import server
from grace2_agent.pipeline_emitter import PipelineEmitter
from grace2_contracts.case import CaseChatMessage, CaseSessionState, CaseSummary
from grace2_contracts.common import new_ulid, now_utc


class FakeWS:
    """Minimal WS stand-in mirroring the existing server tests' FakeWS."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    async def send(self, text: str) -> None:
        if self.closed:
            raise ConnectionError("socket closed")
        self.sent.append(text)


class _FakePersistence:
    """Returns a fixed ``CaseSessionState.loaded_layers`` for one case_id.

    Implements ONLY the methods ``_handle_session_resume`` touches:
    ``get_session_state`` (the replay seam) and ``list_cases_for_user``
    (``_emit_case_list``, best-effort)."""

    def __init__(
        self,
        case_id: str,
        loaded_layers: list[dict],
        chat_history: list[CaseChatMessage] | None = None,
    ) -> None:
        self._case_id = case_id
        self._loaded_layers = loaded_layers
        self._chat_history = chat_history or []
        self.get_session_state_calls = 0

    async def get_session_state(self, case_id: str) -> CaseSessionState:
        self.get_session_state_calls += 1
        case = CaseSummary(
            case_id=case_id,
            title="Fort Myers flood",
            created_at=now_utc(),
            updated_at=now_utc(),
            status="active",
        )
        layers = self._loaded_layers if case_id == self._case_id else []
        chat = self._chat_history if case_id == self._case_id else []
        return CaseSessionState(case=case, loaded_layers=layers, chat_history=chat)

    async def list_cases_for_user(self, user_id: str) -> list[CaseSummary]:
        return []


def _raster_layer(layer_id: str) -> dict:
    """A valid ProjectLayerSummary-shaped dict (raster — no inline re-read)."""
    return {
        "layer_id": layer_id,
        "name": f"Flood depth {layer_id}",
        "layer_type": "raster",
        "uri": f"s3://grace2-runs/{layer_id}/cog.tif",
        "style_preset": "flood_depth",
        "visible": True,
        "role": "primary",
        "temporal": False,
    }


@pytest.fixture(autouse=True)
def _clean_registries():
    server._SESSION_ACTIVE_CASE.clear()
    server._SESSION_LIVE_TURNS.clear()
    saved = server.get_persistence()
    yield
    server.set_persistence(saved)
    server._SESSION_ACTIVE_CASE.clear()
    server._SESSION_LIVE_TURNS.clear()


def _make_emitter(ws: FakeWS, session_id: str) -> PipelineEmitter:
    async def _sink(text: str) -> None:
        try:
            await ws.send(text)
        except Exception:  # noqa: BLE001 — emitter swallows dead-socket sends
            pass

    return PipelineEmitter(session_id=session_id, sink=_sink)


def _session_states(ws: FakeWS) -> list[dict]:
    """All ``session-state`` envelopes the socket received, parsed."""
    out: list[dict] = []
    for raw in ws.sent:
        try:
            env = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if env.get("type") == "session-state":
            out.append(env)
    return out


# --------------------------------------------------------------------------- #
# (a) bare reconnect + active Case + NO live turn -> replays loaded_layers
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bare_resume_replays_active_case_layers() -> None:
    session_id = new_ulid()
    case_id = new_ulid()
    layers = [_raster_layer("L_flood_001"), _raster_layer("L_flood_002")]
    server.set_persistence(_FakePersistence(case_id, layers))
    # The session has an active Case (set on a prior connection's case-open),
    # which is what survives across the reconnect via _SESSION_ACTIVE_CASE.
    server._set_session_active_case(session_id, case_id)
    assert not server._SESSION_LIVE_TURNS.get(session_id)  # NO live turn

    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    state.emitter = _make_emitter(ws, session_id)

    await server._handle_session_resume(ws, state)

    states = _session_states(ws)
    assert len(states) == 1, "bare resume emits exactly one session-state"
    loaded = states[0]["payload"]["loaded_layers"]
    ids = sorted(layer["layer_id"] for layer in loaded)
    assert ids == ["L_flood_001", "L_flood_002"], (
        "the reconnect must replay the Case's persisted loaded_layers so the "
        "client re-renders every rendered layer without a case-open"
    )
    # The emitter accumulator was seeded (so a subsequent emission/dedup is
    # against the persisted truth set, like case-open).
    assert len(state.emitter.loaded_layers) == 2


# --------------------------------------------------------------------------- #
# (b) reconnect-while-solving -> exactly ONE set of frames (no dup writer)
# --------------------------------------------------------------------------- #


async def _gated_turn(release: asyncio.Event, emitter: PipelineEmitter) -> None:
    await emitter.emit_session_state()  # progress proxy
    await release.wait()
    await emitter.emit_session_state()  # terminal proxy (the published layer)


@pytest.mark.asyncio
async def test_resume_while_solving_single_emitter_no_dup() -> None:
    session_id = new_ulid()
    case_id = new_ulid()
    layers = [_raster_layer("L_flood_001")]
    server.set_persistence(_FakePersistence(case_id, layers))
    server._set_session_active_case(session_id, case_id)

    # A solve is in-flight (detached from a now-dead socket): live turn keyed by
    # session, driven by ITS OWN emitter (sink points at the dead socket).
    ws_old = FakeWS()
    emitter_old = _make_emitter(ws_old, session_id)
    release = asyncio.Event()
    turn_key = "case-flood"
    task = asyncio.create_task(_gated_turn(release, emitter_old))
    server._register_live_turn(session_id, turn_key, task, emitter_old)
    await asyncio.sleep(0.02)  # first progress frame lands on the (old) sink
    ws_old.closed = True  # launching socket is gone

    # The user reconnects WHILE the solve runs.
    ws_new = FakeWS()
    state_new = server.SessionState(session_id=session_id)
    state_new.emitter = _make_emitter(ws_new, session_id)
    await server._handle_session_resume(ws_new, state_new)

    # Requirement 2 (dedup): because a LIVE turn was rebound onto this socket,
    # the resume MUST NOT also seed + emit a second snapshot through the new
    # connection's emitter. The live turn's (rebound) emitter is the single
    # writer for this socket — the resume itself emits exactly one frame and
    # there is no duplicate session-state.
    resume_states = _session_states(ws_new)
    assert len(resume_states) == 1, (
        "reconnect-while-solving must not double-emit session-state — exactly "
        "one writer to the socket"
    )

    # The rebound live-turn emitter now drives the terminal frame onto the new
    # socket (and NOTHING further to the dead socket).
    old_after = len(ws_old.sent)
    release.set()
    await task
    new_states = _session_states(ws_new)
    assert len(new_states) == 2, (
        "the single (rebound) live-turn emitter delivers the terminal frame to "
        "the reconnected socket — no second writer, no dup"
    )
    assert len(ws_old.sent) == old_after, "dead socket receives nothing further"


# --------------------------------------------------------------------------- #
# (c) reconnect with NO active Case -> replays nothing (no crash)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_bare_resume_no_active_case_replays_nothing() -> None:
    session_id = new_ulid()
    case_id = new_ulid()
    # Persistence HAS layers for some Case, but THIS session has no active Case.
    server.set_persistence(_FakePersistence(case_id, [_raster_layer("L_x")]))
    assert server._SESSION_ACTIVE_CASE.get(session_id) is None

    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    state.emitter = _make_emitter(ws, session_id)

    # Must not raise.
    await server._handle_session_resume(ws, state)

    states = _session_states(ws)
    assert len(states) == 1, "still emits one (empty) session-state"
    assert states[0]["payload"]["loaded_layers"] == [], (
        "no active Case -> nothing replayed (the M1 empty-snapshot path)"
    )
    assert state.emitter.loaded_layers == []


@pytest.mark.asyncio
async def test_bare_resume_persistence_unbound_no_crash() -> None:
    """An active Case but Persistence unbound: replay no-ops, resume completes."""
    session_id = new_ulid()
    case_id = new_ulid()
    server.set_persistence(None)
    server._set_session_active_case(session_id, case_id)

    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    state.emitter = _make_emitter(ws, session_id)

    await server._handle_session_resume(ws, state)

    states = _session_states(ws)
    assert len(states) == 1
    assert states[0]["payload"]["loaded_layers"] == []


# --------------------------------------------------------------------------- #
# (d) #147 reconnect-resync GAP B1: a bare reconnect ALSO seeds the emitter's
#     chat-history mirror so the replayed session-state ships non-empty
#     chat_history (the chat bubbles re-render, not just the layers).
# --------------------------------------------------------------------------- #


def _chat_msg(case_id: str, role: str, content: str) -> CaseChatMessage:
    return CaseChatMessage(
        message_id=new_ulid(),
        case_id=case_id,
        role=role,  # type: ignore[arg-type]
        content=content,
        created_at=now_utc(),
    )


@pytest.mark.asyncio
async def test_bare_resume_replays_active_case_chat_history() -> None:
    """A bare reconnect seeds emitter chat_history from the persisted Case.

    #147 Feature B GAP B1: pre-fix, the bare-reconnect replay re-rendered
    layers but shipped an EMPTY chat_history, so the transcript vanished until
    an explicit case-open. The replay now seeds ``emitter.seed_chat_history``
    from the SAME persisted ``CaseSessionState`` so ``emit_session_state``
    ships the chat bubbles too.
    """
    session_id = new_ulid()
    case_id = new_ulid()
    layers = [_raster_layer("L_flood_001")]
    history = [
        _chat_msg(case_id, "user", "flood Fort Myers"),
        _chat_msg(case_id, "agent", "Modeled the flood depth."),
    ]
    server.set_persistence(_FakePersistence(case_id, layers, chat_history=history))
    server._set_session_active_case(session_id, case_id)

    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    state.emitter = _make_emitter(ws, session_id)

    await server._handle_session_resume(ws, state)

    states = _session_states(ws)
    assert len(states) == 1, "bare resume emits exactly one session-state"
    chat = states[0]["payload"]["chat_history"]
    assert len(chat) == 2, (
        "the reconnect must seed the emitter chat-history mirror so the "
        "replayed session-state re-renders the chat bubbles, not just layers"
    )
    assert [m["role"] for m in chat] == ["user", "agent"]
    assert [m["content"] for m in chat] == [
        "flood Fort Myers",
        "Modeled the flood depth.",
    ]


@pytest.mark.asyncio
async def test_bare_resume_empty_chat_history_ships_empty() -> None:
    """No persisted chat -> the seeded mirror is empty (no crash, byte-clean)."""
    session_id = new_ulid()
    case_id = new_ulid()
    server.set_persistence(_FakePersistence(case_id, [_raster_layer("L_x")]))
    server._set_session_active_case(session_id, case_id)

    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    state.emitter = _make_emitter(ws, session_id)

    await server._handle_session_resume(ws, state)

    states = _session_states(ws)
    assert len(states) == 1
    assert states[0]["payload"]["chat_history"] == []
