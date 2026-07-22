"""Session durability fix - Jobs B + C (agent half, server.py).

Both jobs target the "session feels broken" cluster from live mobile use
(navigate-out/back). They are file-disjoint within ``server.py`` and serialized
as one agent thread (see ``reports/design/session_durability_fix.md``).

JOB B (WS connection accumulation): navigate-out/back piles up sockets - the
old socket lingers as a zombie until the slow ~20s transport ping reaps it, so a
single session accumulated ~20 live connections. Fix: a per-session connection
registry (``_SESSION_WS_CONNECTIONS``) plus eager reaping on each
``session-resume`` handshake - a new connection proactively closes any PRIOR
socket of the SAME session. CRITICAL invariant: the reap NEVER closes the
resuming connection's OWN live socket.

JOB C (active-case flap): two sockets of one session each send a 25s keepalive
``session-resume`` stamped with the Case THAT socket believes is active; pre-fix
every keepalive re-bound the shared ``_SESSION_ACTIVE_CASE`` pointer when its
stamp differed, so the sockets ping-ponged the pointer every 25s and each rebind
drove an authoritative layer replay that clobbered the displayed Case. Fix: gate
the resume-rebind so it fires only on the FIRST resume of a connection (a
genuine fresh resume), never on a keepalive ping. Explicit case-select /
user-message still rebind. The legitimate first-resume layer replay still fires
ONCE per connection.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grace2_agent import server
from grace2_agent.pipeline_emitter import PipelineEmitter
from grace2_contracts.case import CaseSessionState, CaseSummary
from grace2_contracts.common import new_ulid, now_utc


# --------------------------------------------------------------------------- #
# Test doubles (mirror the existing server-test FakeWS / _FakePersistence)
# --------------------------------------------------------------------------- #


class FakeWS:
    """Minimal WS stand-in with a ``close`` the JOB B reaper can call."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self.close_calls: list[tuple[int | None, str | None]] = []

    async def send(self, text: str) -> None:
        if self.closed:
            raise ConnectionError("socket closed")
        self.sent.append(text)

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.closed = True
        self.close_calls.append((code, reason))


class _RaisingCloseWS(FakeWS):
    """A prior socket that raises on close (already-closing) - reap is best-effort."""

    async def close(self, code: int | None = None, reason: str | None = None) -> None:
        self.close_calls.append((code, reason))
        raise ConnectionError("already closing")


class _FakePersistence:
    """Returns a fixed ``loaded_layers`` for one case_id (replay seam only)."""

    def __init__(self, case_id: str, loaded_layers: list[dict]) -> None:
        self._case_id = case_id
        self._loaded_layers = loaded_layers
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
        return CaseSessionState(case=case, loaded_layers=layers, chat_history=[])

    async def list_cases_for_user(self, user_id: str) -> list[CaseSummary]:
        return []

    async def set_session_active_case(self, session_id: str, case_id) -> None:
        return None

    async def get_session_active_case(self, session_id: str):
        return None


def _raster_layer(layer_id: str) -> dict:
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


def _make_emitter(ws: FakeWS, session_id: str) -> PipelineEmitter:
    async def _sink(text: str) -> None:
        try:
            await ws.send(text)
        except Exception:  # noqa: BLE001 - emitter swallows dead-socket sends
            pass

    return PipelineEmitter(session_id=session_id, sink=_sink)


def _case_list_envelopes(ws: FakeWS) -> list[dict]:
    out: list[dict] = []
    for raw in ws.sent:
        try:
            env = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if env.get("type") == "case-list":
            out.append(env)
    return out


def _session_states(ws: FakeWS) -> list[dict]:
    out: list[dict] = []
    for raw in ws.sent:
        try:
            env = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if env.get("type") == "session-state":
            out.append(env)
    return out


# Valid ULIDs (leading "0" keeps the 48-bit timestamp from overflowing).
CASE_A = "0" + "A" * 25
CASE_B = "0" + "B" * 25


@pytest.fixture(autouse=True)
def _clean_registries():
    saved_active = dict(server._SESSION_ACTIVE_CASE)
    saved_conns = {k: set(v) for k, v in server._SESSION_WS_CONNECTIONS.items()}
    saved_case_list_hash = dict(server._SESSION_CASE_LIST_HASH)
    saved_p = server.get_persistence()
    server._SESSION_ACTIVE_CASE.clear()
    server._SESSION_WS_CONNECTIONS.clear()
    server._SESSION_LIVE_TURNS.clear()
    server._SESSION_CASE_LIST_HASH.clear()
    server.set_persistence(None)
    try:
        yield
    finally:
        server.set_persistence(saved_p)
        server._SESSION_ACTIVE_CASE.clear()
        server._SESSION_ACTIVE_CASE.update(saved_active)
        server._SESSION_WS_CONNECTIONS.clear()
        server._SESSION_WS_CONNECTIONS.update(saved_conns)
        server._SESSION_LIVE_TURNS.clear()
        server._SESSION_CASE_LIST_HASH.clear()
        server._SESSION_CASE_LIST_HASH.update(saved_case_list_hash)


# =========================================================================== #
# JOB B: per-session connection registry + eager reaping
# =========================================================================== #


@pytest.mark.skip(reason="JOB B eager reaping DISABLED 2026-06-22: dual-socket-unsafe - reaped the legitimate sibling socket and killed in-flight turns with 4408; re-enable only dual-socket-aware + in-flight-safe. See server._reap_prior_session_connections note.")
@pytest.mark.asyncio
async def test_second_resume_closes_prior_socket_not_itself() -> None:
    """A 2nd session-resume from a NEW socket of the same session closes the
    PRIOR socket - and NEVER the resuming socket itself (the active tab)."""
    session_id = new_ulid()

    ws1 = FakeWS()
    state1 = server.SessionState(session_id=session_id)
    state1.emitter = _make_emitter(ws1, session_id)
    await server._handle_session_resume(ws1, state1)

    # First socket is registered, alive, never closed.
    assert ws1 in server._SESSION_WS_CONNECTIONS[session_id]
    assert ws1.closed is False
    assert server.session_connection_count(session_id) == 1

    # A NEW socket of the SAME session resumes (navigate-out/back replacement).
    ws2 = FakeWS()
    state2 = server.SessionState(session_id=session_id)
    state2.emitter = _make_emitter(ws2, session_id)
    await server._handle_session_resume(ws2, state2)

    # The prior socket was proactively closed; the resuming socket survives.
    assert ws1.closed is True
    assert ws1.close_calls[0][0] == server.SESSION_SUPERSEDED_CLOSE_CODE
    assert ws2.closed is False, "the resuming socket's OWN live socket is NEVER closed"
    # Registry now holds only the live socket (prior pruned), count stays low.
    assert server._SESSION_WS_CONNECTIONS[session_id] == {ws2}
    assert server.session_connection_count(session_id) == 1


@pytest.mark.skip(reason="JOB B eager reaping DISABLED 2026-06-22: dual-socket-unsafe - reaped the legitimate sibling socket and killed in-flight turns with 4408; re-enable only dual-socket-aware + in-flight-safe. See server._reap_prior_session_connections note.")
@pytest.mark.asyncio
async def test_connections_do_not_pile_up_across_many_reconnects() -> None:
    """Simulate ~10 navigate-out/back cycles: ``_SESSION_WS_CONNECTIONS`` does
    NOT grow unbounded - each fresh resume reaps the prior socket, so the live
    count stays at 1 (not ~20)."""
    session_id = new_ulid()
    sockets: list[FakeWS] = []
    for _ in range(10):
        ws = FakeWS()
        st = server.SessionState(session_id=session_id)
        st.emitter = _make_emitter(ws, session_id)
        await server._handle_session_resume(ws, st)
        sockets.append(ws)

    # Only the LAST socket is live; every prior one was reaped.
    assert server.session_connection_count(session_id) == 1
    assert server._SESSION_WS_CONNECTIONS[session_id] == {sockets[-1]}
    assert all(ws.closed for ws in sockets[:-1])
    assert sockets[-1].closed is False


@pytest.mark.skip(reason="JOB B eager reaping DISABLED 2026-06-22: dual-socket-unsafe - reaped the legitimate sibling socket and killed in-flight turns with 4408; re-enable only dual-socket-aware + in-flight-safe. See server._reap_prior_session_connections note.")
@pytest.mark.asyncio
async def test_reap_is_best_effort_when_prior_close_raises() -> None:
    """A prior socket that raises on close (already closing) is still dropped
    from the registry - the reap never wedges and the resume completes."""
    session_id = new_ulid()

    ws1 = _RaisingCloseWS()
    state1 = server.SessionState(session_id=session_id)
    state1.emitter = _make_emitter(ws1, session_id)
    await server._handle_session_resume(ws1, state1)

    ws2 = FakeWS()
    state2 = server.SessionState(session_id=session_id)
    state2.emitter = _make_emitter(ws2, session_id)
    await server._handle_session_resume(ws2, state2)  # must not raise

    assert ws1.close_calls, "close was attempted on the raising prior socket"
    assert server._SESSION_WS_CONNECTIONS[session_id] == {ws2}
    assert server.session_connection_count(session_id) == 1


@pytest.mark.asyncio
async def test_different_sessions_are_not_cross_reaped() -> None:
    """A resume on session B never touches session A's sockets."""
    sid_a = new_ulid()
    sid_b = new_ulid()

    ws_a = FakeWS()
    st_a = server.SessionState(session_id=sid_a)
    st_a.emitter = _make_emitter(ws_a, sid_a)
    await server._handle_session_resume(ws_a, st_a)

    ws_b = FakeWS()
    st_b = server.SessionState(session_id=sid_b)
    st_b.emitter = _make_emitter(ws_b, sid_b)
    await server._handle_session_resume(ws_b, st_b)

    assert ws_a.closed is False, "a different session's socket is never reaped"
    assert server.session_connection_count(sid_a) == 1
    assert server.session_connection_count(sid_b) == 1


def test_deregister_prunes_empty_session_bucket() -> None:
    """Deregistering the last connection prunes the bucket so the registry
    cannot grow unbounded across long-lived sessions."""
    session_id = new_ulid()
    ws = FakeWS()
    server._register_session_connection(session_id, ws)
    assert session_id in server._SESSION_WS_CONNECTIONS
    server._deregister_session_connection(session_id, ws)
    assert session_id not in server._SESSION_WS_CONNECTIONS
    assert server.session_connection_count(session_id) == 0


# =========================================================================== #
# JOB C: single-writer active-case authority (no keepalive flap)
# =========================================================================== #


@pytest.mark.asyncio
async def test_first_resume_rebinds_and_replays_once() -> None:
    """The FIRST resume of a connection IS a genuine fresh resume: it rebinds
    the active Case to the client's stamp AND replays the Case layers once."""
    session_id = new_ulid()
    server._set_session_active_case(session_id, CASE_A)  # stale server pointer

    replayed_for: list[str | None] = []

    async def _fake_replay(state):
        replayed_for.append(state.active_case_id)

    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    state.emitter = _make_emitter(ws, session_id)

    orig = server._replay_active_case_layers
    server._replay_active_case_layers = _fake_replay  # type: ignore[assignment]
    try:
        await server._handle_session_resume(ws, state, client_case_id=CASE_B)
    finally:
        server._replay_active_case_layers = orig  # type: ignore[assignment]

    # Rebound to the client's Case on the FIRST resume...
    assert state.active_case_id == CASE_B
    # ...and the layer replay fired exactly once (durability invariant).
    assert replayed_for == [CASE_B]
    assert state.did_first_resume is True


@pytest.mark.asyncio
async def test_keepalive_resume_does_not_rebind_or_replay() -> None:
    """A keepalive resume (a LATER resume on the same connection) stamped with a
    DIFFERENT Case must NOT rebind the shared pointer NOR re-replay layers - this is the flap fix."""
    session_id = new_ulid()
    server._set_session_active_case(session_id, CASE_A)

    replayed_for: list[str | None] = []

    async def _fake_replay(state):
        replayed_for.append(state.active_case_id)

    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    state.emitter = _make_emitter(ws, session_id)

    orig = server._replay_active_case_layers
    server._replay_active_case_layers = _fake_replay  # type: ignore[assignment]
    try:
        # First (fresh) resume: stamp agrees with the server, so no rebind, but
        # the one-time replay fires.
        await server._handle_session_resume(ws, state, client_case_id=CASE_A)
        assert state.active_case_id == CASE_A
        assert replayed_for == [CASE_A]
        # KEEPALIVE on the SAME connection, stamped with a DIFFERENT Case (the
        # other socket's stale belief). Must be ignored - no rebind, no replay.
        await server._handle_session_resume(ws, state, client_case_id=CASE_B)
    finally:
        server._replay_active_case_layers = orig  # type: ignore[assignment]

    assert state.active_case_id == CASE_A, (
        "a keepalive ping must NOT rebind the shared active-case pointer "
        "(the flap fix)"
    )
    assert replayed_for == [CASE_A], (
        "a keepalive ping must NOT re-replay the Case layers (no re-paint/blink)"
    )
    assert state.did_first_resume is True


@pytest.mark.asyncio
async def test_two_sockets_keepalive_do_not_pingpong_the_pointer() -> None:
    """Two sockets of one session each keepalive with their OWN (differing)
    Case stamp: after both connections' FIRST resume, the keepalives do not
    ping-pong the shared ``_SESSION_ACTIVE_CASE`` pointer every cycle."""
    session_id = new_ulid()

    async def _noop_replay(state):
        return None

    orig = server._replay_active_case_layers
    server._replay_active_case_layers = _noop_replay  # type: ignore[assignment]
    try:
        # Socket 1 (App.tsx) first-resumes in CASE_A - fresh resume rebinds.
        ws1 = FakeWS()
        st1 = server.SessionState(session_id=session_id)
        st1.emitter = _make_emitter(ws1, session_id)
        await server._handle_session_resume(ws1, st1, client_case_id=CASE_A)
        assert server._SESSION_ACTIVE_CASE[session_id] == CASE_A

        # Socket 2 (Chat.tsx) first-resumes in CASE_A too - the JOB B reap would
        # close ws1, but the per-session pointer is shared and now CASE_A.
        ws2 = FakeWS()
        st2 = server.SessionState(session_id=session_id)
        st2.emitter = _make_emitter(ws2, session_id)
        await server._handle_session_resume(ws2, st2, client_case_id=CASE_A)
        assert server._SESSION_ACTIVE_CASE[session_id] == CASE_A

        # Now BOTH sockets keepalive every cycle, each stamping a DIFFERENT Case
        # (the classic two-source-of-truth drift). The shared pointer must NOT
        # ping-pong - every keepalive is gated out of the rebind.
        for _ in range(5):
            await server._handle_session_resume(ws1, st1, client_case_id=CASE_B)
            await server._handle_session_resume(ws2, st2, client_case_id=CASE_A)
        assert server._SESSION_ACTIVE_CASE[session_id] == CASE_A, (
            "keepalive resumes from two sockets must never ping-pong the shared "
            "active-case pointer"
        )
    finally:
        server._replay_active_case_layers = orig  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_explicit_select_rebinds_even_after_first_resume() -> None:
    """An EXPLICIT case-select (``_prepare_user_turn`` rebind path) still
    rebinds the active Case - only the keepalive *resume* is gated, not the
    deliberate user-intent paths."""
    session_id = new_ulid()
    server._set_session_active_case(session_id, CASE_A)

    async def _fake_sync(ws, state):
        return None

    async def _fake_persist_turn(state, *, role, content, **kw):
        return None

    orig_sync = server._sync_case_context
    orig_persist = server._persist_chat_turn
    server._sync_case_context = _fake_sync  # type: ignore[assignment]
    server._persist_chat_turn = _fake_persist_turn  # type: ignore[assignment]
    try:
        state = server.SessionState(session_id=session_id)
        # Simulate the connection has already done its first resume + keepalives.
        state.did_first_resume = True

        directive = await server._prepare_user_turn(
            FakeWS(), state, "switch to the new case", client_case_id=CASE_B
        )
    finally:
        server._sync_case_context = orig_sync  # type: ignore[assignment]
        server._persist_chat_turn = orig_persist  # type: ignore[assignment]

    assert directive is None  # LLM path
    assert state.active_case_id == CASE_B, (
        "an explicit user-message / case-select still rebinds the active Case "
        "even after the connection's first resume (deliberate user intent)"
    )
    assert state.current_turn_case_id == CASE_B


@pytest.mark.asyncio
async def test_first_resume_replays_layers_through_real_emitter() -> None:
    """End-to-end with the REAL replay seam: the first resume of a connection
    re-renders the Case's persisted layers exactly once; the keepalive does
    NOT re-paint them (no blink)."""
    session_id = new_ulid()
    case_id = new_ulid()
    layers = [_raster_layer("L_flood_001"), _raster_layer("L_flood_002")]
    server.set_persistence(_FakePersistence(case_id, layers))
    server._set_session_active_case(session_id, case_id)

    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    state.emitter = _make_emitter(ws, session_id)

    # FIRST resume: replays the two persisted layers.
    await server._handle_session_resume(ws, state)
    states = _session_states(ws)
    assert len(states) == 1
    ids = sorted(layer["layer_id"] for layer in states[0]["payload"]["loaded_layers"])
    assert ids == ["L_flood_001", "L_flood_002"]
    first_calls = server.get_persistence().get_session_state_calls
    assert first_calls == 1, "the fresh resume hit the replay seam once"

    # KEEPALIVE: emits a session-state pong but does NOT re-read persistence /
    # re-replay the layers (the gate keeps the loop off Dynamo + kills the blink).
    await server._handle_session_resume(ws, state)
    assert server.get_persistence().get_session_state_calls == first_calls, (
        "a keepalive resume must NOT re-hit the replay seam (no re-paint blink)"
    )


# =========================================================================== #
# OPEN-8: case-list emission storm - server-side change-guard
# =========================================================================== #
#
# Root cause (live evidence, trid3nt-local/logs/agent.log): ``_emit_case_list``
# had NO change-detection - every ``session-resume`` (the client's ~25s
# keepalive ping, OR any one of several sockets sharing a session_id
# independently resuming) re-serialized + re-sent the full case list
# (~190 cases live) even when nothing had changed, observed as multi-per-
# minute chatter on long-lived sessions. Fix: a per-session content-digest
# guard (``_SESSION_CASE_LIST_HASH`` / ``_case_list_digest``) skips the send
# when unchanged; ``force=True`` (a genuine first resume, or any explicit
# case mutation) always emits.


@pytest.mark.asyncio
async def test_first_resume_forces_case_list_keepalive_skips_unchanged() -> None:
    """The genuine FIRST resume of a connection always emits ``case-list``
    (force=True); a later keepalive resume with an UNCHANGED list is a
    no-op — no repeat serialize/send of the same content."""
    session_id = new_ulid()
    case_id = new_ulid()
    server.set_persistence(_FakePersistence(case_id, []))

    ws = FakeWS()
    state = server.SessionState(session_id=session_id)
    state.emitter = _make_emitter(ws, session_id)

    await server._handle_session_resume(ws, state)
    assert len(_case_list_envelopes(ws)) == 1, "first resume forces an emit"

    # KEEPALIVE x3: the list is unchanged, so no further case-list emits.
    await server._handle_session_resume(ws, state)
    await server._handle_session_resume(ws, state)
    await server._handle_session_resume(ws, state)
    assert len(_case_list_envelopes(ws)) == 1, (
        "repeat keepalive resumes with an unchanged list must not re-emit"
    )


@pytest.mark.asyncio
async def test_case_list_change_guard_cleared_on_disconnect(monkeypatch) -> None:
    """Once a session's last live connection deregisters, its cached
    case-list digest is dropped — a later reconnect (fresh SessionState)
    gets an honest first-resume emit rather than inheriting a stale guard
    from the closed connection."""
    session_id = new_ulid()
    case_id = new_ulid()
    server.set_persistence(_FakePersistence(case_id, []))
    server._SESSION_CASE_LIST_HASH[session_id] = "stale-digest-from-a-dead-connection"

    assert server.session_connection_count(session_id) == 0
    server._clear_case_list_hash(session_id)
    assert session_id not in server._SESSION_CASE_LIST_HASH
