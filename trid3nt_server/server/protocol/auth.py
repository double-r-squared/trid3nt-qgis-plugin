"""Connect handshake + auth-token verification + session resume/replay."""

from __future__ import annotations

import logging
from pydantic import ValidationError
from trid3nt_contracts.auth import AuthTokenEnvelope
from trid3nt_server.credentials.auth_handshake import authenticate_token, build_auth_ack, derive_advertised_endpoints, verify_access_token
from trid3nt_server.server.dispatch.emitter import _ensure_emitter
from trid3nt_server.server.protocol.connections import _reap_prior_session_connections, _register_session_connection
from trid3nt_server.server.session.case_state import _bind_auth_result, _persist_session_active_case, _reload_session_active_case, _replay_active_case_layers, _touch_session_record
from trid3nt_server.server.session.persistence_ref import get_persistence
from trid3nt_server.server.session.state import SessionState, _CASE_SYNC_NEVER
from trid3nt_server.server.turn.cases import _emit_case_list
from trid3nt_server.server.turn.live_turn import _rebind_live_turns
from trid3nt_server.server.turn.wire import _emit_turn_complete, _new_envelope, _send_error
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")

# --------------------------------------------------------------------------- #
# Connect-handshake
# --------------------------------------------------------------------------- #


def _connection_local_host(websocket: "ServerConnection | Any") -> str | None:
    """The server-side socket's local host for THIS connection.

    Used to derive the advertised sibling endpoints (remote-daemon access):
    a client dialing over the tailnet connected TO that address on the
    server side, so local_address reflects the exact reachable host to hand
    back. Defensive: websocket.local_address is a (host, port) tuple on a
    real ServerConnection but absent on the test fakes -- returns None (env
    overrides still apply).
    """
    addr = getattr(websocket, "local_address", None)
    if isinstance(addr, (tuple, list)) and addr:
        host = addr[0]
        return host if isinstance(host, str) and host else None
    return None

async def _reject_auth_handshake(
    websocket: ServerConnection,
    session_id: str,
    message: str,
) -> None:
    """Reject a connection at the handshake with a typed AUTH_FAILED close.

    Remote-daemon access: the shared-token gate's rejection path.
    Emits the ``AUTH_FAILED`` error envelope, then closes the socket with
    the WebSocket policy-violation code (1008) -- the SAME close the client's
    ``is_auth_failure`` classifier recognizes, so the client stops its
    reconnect ladder instead of hammering a token-gated daemon forever. Never
    raises: a socket that is already down is fine.
    """
    await _send_error(websocket, session_id, "AUTH_FAILED", message)
    try:
        await websocket.close(code=1008, reason="AUTH_FAILED")
    except Exception:  # noqa: BLE001 -- socket may already be gone
        pass

async def _handle_auth_token(
    websocket: ServerConnection,
    state: SessionState,
    payload_dict: dict,
) -> None:
    """Process the client's ``auth-token`` envelope and emit ``auth-ack``.

    Per the connect-handshake contract:

    1. Validate the payload through ``AuthTokenEnvelope``.
    2. Call ``authenticate_token`` -> resolves to a ``User`` via Persistence
       (or provisions an anonymous fallback).
    3. Bind the resolved ``user_id`` + anonymous-flag into the
       SessionState -- every subsequent envelope is scoped to this user.
    4. Emit ``auth-ack`` so the client knows its session identity.
    """
    tok: AuthTokenEnvelope | None
    try:
        tok = AuthTokenEnvelope.model_validate(payload_dict)
    except ValidationError as ve:
        await _send_error(
            websocket,
            state.session_id,
            "AUTH_TOKEN_INVALID",
            f"auth-token validation failed: {ve.errors()[0]['msg']}",
        )
        # Even on validation failure we run the anonymous fallback so the
        # connection is still usable (per H.3).
        tok = None

    # REMOTE-DAEMON ACCESS: optional shared-token gate. When
    # TRID3NT_ACCESS_TOKEN is set, the client's presented token MUST match
    # (constant-time) or the connection is rejected with a typed
    # AUTH_FAILED close (the same close the client classifies as an auth
    # failure and stops its reconnect ladder on). Unset (default) ->
    # verify_access_token returns True and behavior is byte-identical anon.
    presented = tok.token if tok is not None else None
    if not verify_access_token(presented):
        logger.info(
            "auth-token rejected session=%s (access token missing/invalid)",
            state.session_id,
        )
        await _reject_auth_handshake(
            websocket,
            state.session_id,
            "access token required: the presented token is missing or invalid",
        )
        return

    result = await authenticate_token(tok, get_persistence())

    _bind_auth_result(state, result)
    await _touch_session_record(state)  # session heartbeat
    # REMOTE-DAEMON ACCESS: advertise the sibling endpoints derived
    # from THIS connection's local address (so a tailnet client learns the
    # data + HTTP bases automatically) plus any env override.
    endpoints = derive_advertised_endpoints(_connection_local_host(websocket))
    ack = build_auth_ack(result, endpoints=endpoints)
    await websocket.send(_new_envelope("auth-ack", state.session_id, ack))
    logger.info(
        "auth-ack session=%s user_id=%s anonymous=%s endpoints=%s",
        state.session_id,
        result.user.user_id,
        result.is_anonymous,
        endpoints.model_dump(mode="json") if endpoints else None,
    )

async def _ensure_auth_handshake(
    websocket: ServerConnection,
    state: SessionState,
) -> bool:
    """Synchronous fallback: if the handshake hasn't run, run it as anonymous.

    Called when a non-``auth-token`` envelope arrives before the handshake
    has completed (the client either didn't send auth-token, or another
    envelope raced ahead). Mirrors the 5-second timeout path from H.3 --
    instead of waiting 5 seconds we trip the anonymous fallback inline so
    the user is bound before their first real interaction.

    Returns ``True`` when the connection may proceed (handshake already
    complete, or the anonymous fallback bound successfully), ``False`` when the
    shared-token gate rejected the connection (it was closed) so the caller
    must NOT dispatch the pending envelope.
    """
    if state.auth_handshake_complete:
        return True
    # REMOTE-DAEMON ACCESS: a token-gated daemon must not accept a
    # connection that skipped the auth-token envelope entirely -- that would be
    # a trivial bypass of the token. This implicit path presents NO token, so
    # reject it with the same typed AUTH_FAILED close when a token is required.
    if not verify_access_token(None):
        logger.info(
            "implicit handshake rejected session=%s (access token required)",
            state.session_id,
        )
        await _reject_auth_handshake(
            websocket,
            state.session_id,
            "access token required: connect with a valid token",
        )
        return False
    # Implicit-anonymous path: the connection skipped the auth-token envelope.
    # Every connection resolves to the ONE fixed local user, so no client hint
    # is consulted.
    result = await authenticate_token(None, get_persistence())
    _bind_auth_result(state, result)
    await _touch_session_record(state)  # session heartbeat
    endpoints = derive_advertised_endpoints(_connection_local_host(websocket))
    ack = build_auth_ack(result, endpoints=endpoints)
    try:
        await websocket.send(_new_envelope("auth-ack", state.session_id, ack))
    except Exception:  # noqa: BLE001 -- socket may be down
        pass
    logger.info(
        "auth-ack(implicit-anonymous) session=%s user_id=%s",
        state.session_id,
        result.user.user_id,
    )
    return True

async def _handle_session_resume(
    websocket: ServerConnection,
    state: SessionState,
    *,
    client_case_id: str | None = None,
) -> None:
    """Reply with a fresh session-state snapshot.

    Routes through the emitter so the initial session-state is
    snapshot-shaped. Also emits a case-list so the client renders the
    left-rail Case list on initial connect; best-effort -- skipped if
    Persistence is unbound.

    ``client_case_id`` is the Case the CLIENT is currently in and is the
    AUTHORITY: when it differs from ``state.active_case_id`` we RE-BIND the
    server pointer to it BEFORE the layer replay, so a reconnect replays
    the Case the user is actually in, never a stale server pointer. A
    resume with NO ``case_id`` (older client) keeps current behavior
    untouched. We are correcting WHICH Case the replay targets, not
    removing replay -- a genuine fresh reconnect still replays the active
    Case's rendered layers.
    """
    _ensure_emitter(websocket, state)
    # Record THIS socket as a live connection of the session, then reap any
    # prior socket of the SAME session. The keeper (THIS websocket) is excluded
    # by identity so the active tab's own socket is never closed. Idempotent: a
    # keepalive resume re-registers (no-op) and reaps any newly-stale sibling.
    _register_session_connection(state.session_id, websocket)
    await _reap_prior_session_connections(state.session_id, keeper=websocket)
    # JOB C (active-case flap): a keepalive resume is any resume AFTER the
    # first one on THIS connection. Capture the keepalive verdict BEFORE
    # flipping the per-connection latch: a fresh SessionState is built per
    # connection, so the FIRST resume here is the real fresh-socket resume
    # and every later one is a keepalive ping.
    is_keepalive = state.did_first_resume
    state.did_first_resume = True
    # Warm the in-memory pointer from the persisted last_active_case_id
    # first (no-op if this session already has a live pointer this
    # process). After a process restart the _SESSION_ACTIVE_CASE
    # cache is empty; without this a bare resume from an older client would
    # lose the Case. The client stamp below still overrides this seed on
    # any disagreement.
    await _reload_session_active_case(state)
    # Re-bind the server's active-Case pointer to the client's current Case
    # BEFORE the replay below resolves it -- the client is the authority;
    # the in-memory _SESSION_ACTIVE_CASE pointer is a cache that may be
    # stale or cold (process restart). Only re-bind on a genuine change to a
    # non-None Case so an older client's bare resume (no stamp) leaves the
    # pointer alone. The active_case_id setter writes through
    # _set_session_active_case so EVERY connection observes the corrected
    # Case; also persist the pointer so it survives the next restart. A
    # change here invalidates this connection's case-context sync marker so
    # the next user-message re-syncs to the corrected Case.
    #
    # Gate the rebind on ``not is_keepalive`` -- the 25s keepalive ping must
    # NEVER rebind the shared _SESSION_ACTIVE_CASE pointer (with two sockets
    # per session each stamping its own Case, an ungated keepalive rebind
    # ping-pongs the pointer every 25s and each rebind drives an
    # authoritative layer replay that clobbers the displayed Case). The
    # pointer is rebound only on a genuine FIRST resume of a connection
    # here, and on explicit case-command(select) / user-message elsewhere --
    # the deliberate user-intent paths.
    if (
        not is_keepalive
        and client_case_id is not None
        and client_case_id != state.active_case_id
    ):
        logger.info(
            "session-resume re-binding active case session=%s server=%s client=%s",
            state.session_id,
            state.active_case_id,
            client_case_id,
        )
        state.active_case_id = client_case_id
        state.case_context_synced_to = _CASE_SYNC_NEVER
        await _persist_session_active_case(state, client_case_id)
    # Canonical reconnect entry: a freshly-opened socket sends
    # session-resume first. If a turn from a now-closed socket of this SAME
    # session is still running (a live SFINCS solve detached on disconnect),
    # rebind its emitter sink onto THIS socket so its remaining progress +
    # terminal frames land on the user's live connection.
    rebound = _rebind_live_turns(state.session_id, state.emitter)
    if rebound:
        logger.info(
            "session-resume rebound %d live turn(s) onto reconnect session=%s",
            rebound,
            state.session_id,
        )
    # Per-Case layer DURABILITY: a BARE reconnect (no live turn for this
    # session) must STILL re-render every layer already on the map -- the
    # live-turn rebind only covers in-flight turns, so a layer that completed
    # before the disconnect has no live turn. A rendered layer must survive any
    # WS reconnect without an explicit case-open.
    #
    # Resolve the session's active Case and seed THIS reconnect's emitter
    # from the Case's persisted loaded_layers BEFORE emitting (the same
    # case-open / _sync_case_context seam), so the single
    # emit_session_state below carries the full A.7 replace-not-reconcile
    # snapshot the client already knows how to render.
    #
    # Dedup: when rebound > 0 a LIVE turn's emitter was just pointed at
    # THIS socket's sink and IS the writer for this session-state -- we
    # must NOT also seed + emit on the new connection's emitter (that would
    # put two emitters on the same sink and deliver duplicate frames), so
    # the bare-resume replay runs ONLY when nothing was rebound.
    #
    # Replay the active Case's layers ONCE per connection -- on the first
    # BARE (non-rebound) resume, never on the 25s keepalive ping (a
    # keepalive re-seed on every ping re-painted the active Case's layers
    # and un-hid a user-hidden layer). did_fresh_resume gates the replay to
    # the first bare resume; the flag flips ONLY when this connection's
    # emitter was actually seeded, so a rebound connection performs the
    # one-time seed+replay later once it stops being a live turn's writer.
    did_replay_now = False
    if rebound == 0 and not state.did_fresh_resume:
        await _replay_active_case_layers(state)
        state.did_fresh_resume = True
        did_replay_now = True
    await state.emitter.emit_session_state()
    # OPEN-8: force an unconditional emit only on a genuine first
    # (non-keepalive) resume of THIS connection. A later keepalive ping (or
    # a sibling socket independently resuming) goes through the
    # change-guard so an unchanged ~190-case list is not re-serialized +
    # re-sent every cycle.
    await _emit_case_list(websocket, state, force=not is_keepalive)
    # C2 (re-emit on resume): ONLY on the genuine fresh-socket resume
    # (first bare resume that just seeded + replayed this connection's
    # layers), never on a keepalive ping and never on a rebound (a rebound
    # live turn is still streaming and emits its OWN terminal frames). On a
    # real reconnect the card the user last saw spinning may have finished
    # while the socket was down; this bare whole-turn idle is the
    # belt-and-suspenders that force-completes any card the client still
    # believes is running.
    if did_replay_now:
        await _emit_turn_complete(websocket, state)
