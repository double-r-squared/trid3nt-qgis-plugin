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
    """The server-side socket's local host for THIS connection, used to derive
    the advertised sibling endpoints; ``None`` when the attribute is absent, in
    which case only the env overrides apply."""
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
    """Reject a connection at the handshake with a typed ``AUTH_FAILED`` error
    and a policy-violation close, the close the client classifies as an auth
    failure so it stops its reconnect ladder. Never raises."""
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
    """Process the client's ``auth-token`` envelope and emit ``auth-ack``: the
    token resolves to a user, or provisions an anonymous fallback, and that
    identity is bound into the session for every later envelope."""
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
        # Even on a validation failure the anonymous fallback runs, so the
        # connection stays usable.
        tok = None

    # Optional shared-token gate: when ``TRID3NT_ACCESS_TOKEN`` is set the
    # presented token must match in constant time or the connection is rejected
    # with the typed close the client stops its reconnect ladder on. Unset, the
    # verification passes and the anonymous path is unchanged.
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
    """Bind an anonymous user inline when a non-auth envelope arrives before the
    handshake ran. True when the connection may proceed, False when the
    shared-token gate rejected and closed it, so the caller must not dispatch."""
    if state.auth_handshake_complete:
        return True
    # A token-gated daemon must not accept a connection that skipped the
    # auth-token envelope: that would be a trivial bypass. This implicit path
    # presents NO token, so it is rejected with the same typed close.
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
    # Implicit-anonymous path: the connection skipped the auth-token envelope,
    # and every connection resolves to the one fixed local user.
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
    """Reply with a fresh session-state snapshot, plus a case list so the client
    can render its Case rail. ``client_case_id`` is the AUTHORITY: a differing
    stamp re-binds the server pointer BEFORE the layer replay."""
    _ensure_emitter(websocket, state)
    # Record THIS socket as a live connection of the session, then reap any
    # prior socket of the same session; the keeper is excluded by identity, so
    # the active client's own socket is never closed.
    _register_session_connection(state.session_id, websocket)
    await _reap_prior_session_connections(state.session_id, keeper=websocket)
    # A keepalive resume is any resume AFTER the first on THIS connection. The
    # verdict is captured before the latch flips, because a fresh SessionState
    # is built per connection.
    is_keepalive = state.did_first_resume
    state.did_first_resume = True
    # Warm the in-memory pointer from the persisted last-active Case first; it
    # is a no-op when this session already has a live pointer, but after a
    # process restart the cache is empty and a bare resume would lose the Case.
    # A client stamp still overrides this seed on any disagreement.
    await _reload_session_active_case(state)
    # Re-bind the server's active-Case pointer to the client's Case BEFORE the
    # replay resolves it: the client is the authority and the in-memory pointer
    # is a cache that may be stale or cold. Only a genuine change to a non-None
    # Case rebinds, so a bare resume with no stamp leaves the pointer alone; the
    # write goes through every connection's view and is persisted, and the sync
    # marker is invalidated so the next message re-syncs.
    #
    # A keepalive ping must NEVER rebind the shared pointer: with two sockets per
    # session each stamping its own Case, an ungated rebind ping-pongs the
    # pointer and each rebind drives an authoritative replay that clobbers the
    # displayed Case. The pointer moves on a connection's FIRST resume here, and
    # otherwise only on explicit user intent.
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
    # A freshly opened socket sends session-resume first. If a turn from a
    # now-closed socket of this SAME session is still running, its emitter sink
    # rebinds onto THIS socket, so the remaining progress and terminal frames
    # land on the user's live connection.
    rebound = _rebind_live_turns(state.session_id, state.emitter)
    if rebound:
        logger.info(
            "session-resume rebound %d live turn(s) onto reconnect session=%s",
            rebound,
            state.session_id,
        )
    # A rendered layer must survive a reconnect with no explicit case-open, and
    # the live-turn rebind only covers in-flight turns, so a BARE reconnect
    # re-seeds this emitter from the Case's persisted layers before emitting.
    #
    # When something was rebound, that live turn's emitter is already the writer
    # for this session-state, so seeding here too would put two emitters on one
    # sink and deliver duplicate frames.
    #
    # The replay runs ONCE per connection, on the first bare resume: a re-seed on
    # every keepalive ping re-painted the Case's layers and un-hid a
    # user-hidden layer. The flag flips only when this connection's emitter was
    # actually seeded, so a rebound connection still gets its one-time replay
    # later, once it stops being a live turn's writer.
    did_replay_now = False
    if rebound == 0 and not state.did_fresh_resume:
        await _replay_active_case_layers(state)
        state.did_fresh_resume = True
        did_replay_now = True
    await state.emitter.emit_session_state()
    # Force an unconditional emit only on a genuine first, non-keepalive resume
    # of THIS connection; a later ping or a sibling socket's resume goes through
    # the change guard, so an unchanged case list is not re-sent every cycle.
    await _emit_case_list(websocket, state, force=not is_keepalive)
    # Re-emit turn-complete ONLY on the genuine fresh-socket resume, never on a
    # keepalive ping and never on a rebound turn, which is still streaming and
    # emits its own terminal frames. On a real reconnect the card the user last
    # saw spinning may have finished while the socket was down, and this
    # force-completes any card the client still believes is running.
    if did_replay_now:
        await _emit_turn_complete(websocket, state)
