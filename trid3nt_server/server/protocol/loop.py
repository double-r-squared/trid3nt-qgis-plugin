"""The WebSocket connection loop: per-connection handler + server bootstrap."""

from __future__ import annotations

import asyncio
import os
import logging
from pydantic import ValidationError
from trid3nt_contracts.case import CaseCommandEnvelopePayload
from trid3nt_contracts.payload_warning import PayloadConfirmationEnvelopePayload
from trid3nt_contracts.region_choice import RegionChoiceProvidedEnvelopePayload
from trid3nt_contracts.secrets import CredentialProvidedEnvelopePayload, SecretAddEnvelopePayload
from trid3nt_contracts.ws import CancelPayload, ErrorPayload, SessionResumePayload, SpatialInputResponsePayload, UserMessagePayload
from trid3nt_server.adapters.adapter import ModelSettings, load_settings
from trid3nt_server.gates.pending import _resolve_pending_confirmation
from trid3nt_server.main import MAX_TURNS_PER_SESSION
from trid3nt_server.server.dispatch.emitter import _assert_sync_offload_safe, _dispatch_tool_and_persist, _ensure_emitter
from trid3nt_server.server.interactions import _resolve_pending_credential, _resolve_pending_tool_choice
from trid3nt_server.server.protocol.auth import _ensure_auth_handshake, _handle_auth_token, _handle_session_resume
from trid3nt_server.server.protocol.connections import _deregister_session_connection, session_connection_count
from trid3nt_server.server.protocol.handlers import _BG_TASKS, _drain_bg_tasks, _handle_dev_tool_invoke, _handle_layer_delete, _handle_secret_add
from trid3nt_server.server.session.case_state import _clear_case_list_hash, _set_active_aoi_from_payload, _set_drawn_geometry_from_payload
from trid3nt_server.server.session.persistence_ref import init_persistence_from_env
from trid3nt_server.server.session.state import SessionState, _ROOT_STREAM_KEY
from trid3nt_server.server.spatial import _fail_pending_spatial_input, _resolve_pending_region_choice, _resolve_pending_spatial_input
from trid3nt_server.server.turn.cases import _handle_case_command
from trid3nt_server.server.turn.engine import _handle_max_turns_reached, _prepare_user_turn
from trid3nt_server.server.turn.live_turn import _SESSION_LIVE_TURNS, _any_live_turn, _find_live_turn, _rebind_live_turns, _register_live_turn
from trid3nt_server.server.turn.stream import _dispatch_model_turn_and_persist
from trid3nt_server.server.turn.wire import _heartbeat_loop, _new_envelope, _send_error
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

logger = logging.getLogger("trid3nt_server.server")

def inflight_turn_count() -> int:
    """Number of in-flight turns detached from a possibly dead connection: a
    long solver turn survives a socket drop, so this counts turns still running
    with zero sockets open. A done task awaits its self-removing callback."""
    total = 0
    for bucket in _SESSION_LIVE_TURNS.values():
        for live in bucket.values():
            try:
                if not live.task.done():
                    total += 1
            except Exception:  # noqa: BLE001 -- defensive; never break health
                continue
    return total

def _make_handler(settings: ModelSettings):
    """Build the per-connection coroutine, closing over the resolved settings."""

    async def handler(websocket: ServerConnection) -> None:
        # The session_id will be set on the first inbound envelope; we surface
        # an error if the client speaks before establishing one.
        state: SessionState | None = None

        # Start the per-connection data heartbeat so the client's inbound-activity
        # timer is reset on a fast server clock, independent of the possibly slow
        # session-resume reply; it is cancelled in the finally on every exit path.
        # The heartbeat frame's session_id is cosmetic - a client routes a
        # liveness frame by transport - so a placeholder is fine until the first
        # inbound envelope binds the real one.
        heartbeat_task = asyncio.create_task(
            _heartbeat_loop(websocket, "00000000000000000000000000")
        )

        try:
            async for raw in websocket:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")

                # Pre-validate the envelope. Bad shapes get a typed error.
                try:
                    # We don't know the payload type yet; parse generically.
                    import json as _json

                    parsed = _json.loads(raw)
                    msg_type = parsed.get("type")
                    session_id = parsed.get("session_id")
                except Exception as exc:  # noqa: BLE001
                    await websocket.send(
                        _new_envelope(
                            "error",
                            "00000000000000000000000000",
                            ErrorPayload(
                                error_code="INTERNAL_ERROR",
                                message=f"malformed envelope: {exc}",
                            ),
                        )
                    )
                    continue

                if state is None:
                    state = SessionState(session_id=session_id)
                elif state.session_id != session_id:
                    await _send_error(
                        websocket,
                        state.session_id,
                        "INTERNAL_ERROR",
                        "session_id changed mid-connection",
                    )
                    continue

                payload_dict = parsed.get("payload", {})

                # Log EVERY inbound frame's type BEFORE routing, so "did the
                # prompt arrive?" is visible in the journal. The client's
                # keepalive resume is DEBUG so it does not flood the stream.
                if msg_type == "session-resume":
                    logger.debug(
                        "ws-recv session=%s type=%s", session_id, msg_type
                    )
                else:
                    logger.info(
                        "ws-recv session=%s type=%s", session_id, msg_type
                    )

                # Dispatch on message type. Every payload is re-validated
                # through its concrete trid3nt_contracts model.
                try:
                    # The auth-token envelope is the connect handshake; anything
                    # else arriving first trips the anonymous fallback inline, so
                    # the user is bound before any user-scoped action runs.
                    if msg_type == "auth-token":
                        await _handle_auth_token(
                            websocket, state, payload_dict
                        )
                        continue
                    # Implicit anonymous fallback when any other envelope arrives
                    # before the handshake, so a client that sends no auth-token
                    # still works. When a token gate is set the implicit path
                    # rejects and closes the socket, and the pending envelope
                    # must NOT be dispatched.
                    if not state.auth_handshake_complete:
                        if not await _ensure_auth_handshake(websocket, state):
                            continue

                    if msg_type == "session-resume":
                        sr = SessionResumePayload.model_validate(payload_dict)
                        await _handle_session_resume(
                            websocket, state, client_case_id=sr.case_id
                        )

                    elif msg_type == "user-message":
                        um = UserMessagePayload.model_validate(payload_dict)
                        # Structured canvas AOI, read defensively off the raw
                        # payload. Key-present semantics: a bbox SETS the active
                        # AOI, an explicit null CLEARS it, an absent key leaves
                        # the prior AOI.
                        if "aoi_bbox" in payload_dict:
                            _set_active_aoi_from_payload(
                                state, payload_dict.get("aoi_bbox")
                            )
                        # The drawn rubber-band rectangle, same key-present
                        # semantics: a value SETS it, an explicit null CLEARS it,
                        # an absent key leaves the prior state.
                        if "drawn_geometry" in payload_dict:
                            _set_drawn_geometry_from_payload(
                                state, payload_dict.get("drawn_geometry")
                            )
                        # Routing-visibility mode, read defensively: a set value
                        # updates the session's sticky mode and absent leaves the
                        # prior one.
                        _tcm = payload_dict.get("tool_choice_mode")
                        if isinstance(_tcm, str) and _tcm.strip().lower() in (
                            "auto",
                            "ask",
                        ):
                            state.routing_mode = _tcm.strip().lower()
                        # Check the turn cap BEFORE dispatching, incrementing
                        # first so the cap fires one past the limit. A session
                        # already at the cap is refused on every later message.
                        state.turn_count += 1
                        if (
                            MAX_TURNS_PER_SESSION > 0
                            and state.turn_count > MAX_TURNS_PER_SESSION
                        ):
                            await _handle_max_turns_reached(websocket, state)
                            continue
                        # Reset the per-turn accumulators before dispatch, so the
                        # persisted row captures only this turn's emissions.
                        # KNOWN LIMIT: the slots are session-shared, so a turn
                        # running concurrently in ANOTHER Case can interleave
                        # attribution on the closing row; Case targeting itself
                        # stays safe through the turn pin.
                        state.current_turn_layer_ids = []
                        state.current_turn_pipeline_id = None
                        state.current_turn_map_commands = []
                        # The pre-dispatch sequence runs BEFORE the turn task
                        # starts, so chat and layer attribution land on the right,
                        # possibly brand-new, Case. It returns the parsed
                        # directive; None streams through the model instead.
                        directive = await _prepare_user_turn(
                            websocket, state, um.text, client_case_id=um.case_id
                        )
                        # Stream-scoped cancellation: only a re-prompt in the
                        # SAME stream replaces that stream's in-flight turn, and
                        # an auto-created Case mints a fresh key that cannot
                        # collide with a running turn.
                        turn_key = (
                            state.current_turn_case_id or _ROOT_STREAM_KEY
                        )
                        # A same-stream re-prompt SUPERSEDES the prior turn, even
                        # one detached to the module registry by an earlier socket
                        # close, so this connection is checked before the
                        # session-scoped registry.
                        prior = state.inflight_tasks.get(turn_key)
                        if prior is None or prior.done():
                            prior = _find_live_turn(state.session_id, turn_key)
                        if prior is not None and not prior.done():
                            prior.cancel()
                        for _done_key in [
                            k
                            for k, t in state.inflight_tasks.items()
                            if t.done()
                        ]:
                            state.inflight_tasks.pop(_done_key, None)
                        # A fresh socket may rebind onto a prior, still-running
                        # turn of this session, so its progress and terminal
                        # frames reach the new socket. Harmless when there is
                        # none.
                        _ensure_emitter(websocket, state)
                        _rebind_live_turns(state.session_id, state.emitter)
                        # In-chat model selector: a non-None model_id overrides
                        # the session default for this turn, None keeps whatever
                        # was last chosen. An unknown id resolves to the capable
                        # default with a logged notice, so the turn runs rather
                        # than crashing.
                        if um.model_id is not None:
                            from trid3nt_server.adapters.model_selection import (
                                resolve_selected_model as _resolve_selected_model,
                            )

                            _effective_model, _model_notice = _resolve_selected_model(
                                um.model_id
                            )
                            if _model_notice is not None:
                                logger.warning(
                                    "model selector: %s (requested=%r session=%s)",
                                    _model_notice,
                                    um.model_id,
                                    state.session_id,
                                )
                            state.selected_model = _effective_model
                        _turn_model_id = state.selected_model
                        if directive is not None:
                            tool_name, params = directive
                            task = asyncio.create_task(
                                _dispatch_tool_and_persist(
                                    websocket, state, tool_name, params, um.text
                                )
                            )
                        else:
                            task = asyncio.create_task(
                                _dispatch_model_turn_and_persist(
                                    websocket,
                                    state,
                                    settings,
                                    um.text,
                                    model_id=_turn_model_id,
                                    show_thinking=bool(um.show_thinking),
                                )
                            )
                        state.inflight_tasks[turn_key] = task
                        # Register this turn in the module registry NOW, not only
                        # on disconnect, with a self-removing callback: a later
                        # socket close then drops only the per-connection
                        # reference while the running task stays durable, and a
                        # reconnect rebinds the recorded emitter's sink.
                        _register_live_turn(
                            state.session_id, turn_key, task, state.emitter
                        )

                    elif msg_type == "dev-tool-invoke":
                        # Direct tool invocation: the client parsed the ``!run``
                        # line and sent a structured name and args, which run
                        # OUTSIDE the model loop through the same emission, gate
                        # and persistence seam. Read defensively off the raw
                        # dict; the handler validates the wire shape and routes
                        # an unknown tool through the not-found envelope. The
                        # code-exec hard gate still fires through the shared
                        # invoke seam.
                        await _handle_dev_tool_invoke(
                            websocket, state, payload_dict
                        )

                    elif msg_type == "case-command":
                        # Case lifecycle dispatch. The envelope is validated
                        # through its model, so an unknown command surfaces a
                        # typed params error through the outer block.
                        cmd = CaseCommandEnvelopePayload.model_validate(
                            payload_dict
                        )
                        await _handle_case_command(websocket, state, cmd)

                    elif msg_type == "layer-delete":
                        # Per-layer delete: drops the layer from the live
                        # accumulator, emits a fresh session state, and persists
                        # the survivors AUTHORITATIVELY - a union merge would
                        # resurrect the deleted layer. Payload is loosely shaped
                        # and read inline.
                        await _handle_layer_delete(
                            websocket, state, payload_dict
                        )

                    elif msg_type == "secret-add":
                        # Credential push: the plugin brokers a key VALUE over
                        # this seam, and the value lands in the in-memory
                        # resolver cache, never persisted or echoed back.
                        sa = SecretAddEnvelopePayload.model_validate(
                            payload_dict
                        )
                        await _handle_secret_add(websocket, state, sa)

                    elif msg_type == "cancel":
                        CancelPayload.model_validate(payload_dict)
                        logger.info("cancel session=%s", state.session_id)
                        # Target the VISIBLE stream's turn, since the stop
                        # control lives in the active Case; fall back to any live
                        # turn so a stop still cancels the run when the binding
                        # has moved.
                        cancel_key = (
                            state.active_case_id or _ROOT_STREAM_KEY
                        )
                        cancel_task = state.inflight_tasks.get(cancel_key)
                        if cancel_task is None or cancel_task.done():
                            live = [
                                t
                                for t in state.inflight_tasks.values()
                                if not t.done()
                            ]
                            cancel_task = live[-1] if live else None
                        # The targeted turn may have been DETACHED to the
                        # module-level registry by an earlier socket close, and
                        # the explicit stop must still reach it: try the keyed
                        # entry, then any live detached turn of the session.
                        if cancel_task is None or cancel_task.done():
                            cancel_task = _find_live_turn(
                                state.session_id, cancel_key
                            ) or _any_live_turn(state.session_id)
                        if cancel_task is not None and not cancel_task.done():
                            cancel_task.cancel()
                            # Wait briefly so the cancel completes
                            # deterministically; the terminal cancelled frame is
                            # emitted from inside the task's own cancel branch.
                            try:
                                await asyncio.wait_for(cancel_task, timeout=5.0)
                            except (asyncio.CancelledError, asyncio.TimeoutError):
                                pass

                    elif msg_type == "tool-payload-confirmation":
                        # Route the confirmation to the paused dispatch. The
                        # envelope is validated here, so a malformed payload
                        # cannot poison the future.
                        try:
                            conf = (
                                PayloadConfirmationEnvelopePayload.model_validate(
                                    payload_dict
                                )
                            )
                        except ValidationError as ve:
                            await _send_error(
                                websocket,
                                state.session_id,
                                "TOOL_PARAMS_INVALID",
                                f"tool-payload-confirmation invalid: {ve.errors()[0]['msg']}",
                            )
                            continue
                        # Resolve through the SESSION-scoped registry: the gate
                        # may have been registered on a sibling connection of
                        # this same session.
                        if not _resolve_pending_confirmation(
                            state.session_id, conf
                        ):
                            logger.warning(
                                "tool-payload-confirmation for unknown/closed "
                                "warning_id=%s session=%s",
                                conf.warning_id,
                                state.session_id,
                            )
                            continue
                        logger.info(
                            "tool-payload-confirmation accepted session=%s "
                            "warning_id=%s decision=%s",
                            state.session_id,
                            conf.warning_id,
                            conf.decision,
                        )

                    elif msg_type == "credential-provided":
                        # Resolves the paused dispatch's future once the user
                        # saves or declines a requested credential: the tool
                        # retries, or re-raises the original typed error. This
                        # envelope carries NO key material - the key itself
                        # arrived on the secret-add path.
                        try:
                            cp = (
                                CredentialProvidedEnvelopePayload.model_validate(
                                    payload_dict
                                )
                            )
                        except ValidationError as ve:
                            await _send_error(
                                websocket,
                                state.session_id,
                                "TOOL_PARAMS_INVALID",
                                f"credential-provided invalid: {ve.errors()[0]['msg']}",
                            )
                            continue
                        if not _resolve_pending_credential(state.session_id, cp):
                            logger.warning(
                                "credential-provided for unknown/closed "
                                "request_id=%s session=%s",
                                cp.request_id,
                                state.session_id,
                            )
                            continue
                        logger.info(
                            "credential-provided accepted session=%s "
                            "request_id=%s provided=%s",
                            state.session_id,
                            cp.request_id,
                            cp.provided,
                        )

                    elif msg_type == "region-choice-provided":
                        # The user narrowed a state-bbox-fallback geocode to a
                        # sub-region, or kept the whole state; resolving the
                        # paused future applies that choice. May arrive on a
                        # sibling connection of the session.
                        try:
                            rc = (
                                RegionChoiceProvidedEnvelopePayload.model_validate(
                                    payload_dict
                                )
                            )
                        except ValidationError as ve:
                            await _send_error(
                                websocket,
                                state.session_id,
                                "TOOL_PARAMS_INVALID",
                                f"region-choice-provided invalid: {ve.errors()[0]['msg']}",
                            )
                            continue
                        if not _resolve_pending_region_choice(
                            state.session_id, rc
                        ):
                            logger.warning(
                                "region-choice-provided for unknown/closed "
                                "request_id=%s session=%s",
                                rc.request_id,
                                state.session_id,
                            )
                            continue
                        logger.info(
                            "region-choice-provided accepted session=%s "
                            "request_id=%s choice=%s",
                            state.session_id,
                            rc.request_id,
                            rc.choice,
                        )

                    elif msg_type == "spatial-input-response":
                        # The user finished or cancelled the draw surface, and
                        # resolving the paused future lets the dispatch parse the
                        # drawn features into the AOI, points and section line.
                        # May arrive on a sibling connection of the session.
                        try:
                            spatial_resp = (
                                SpatialInputResponsePayload.model_validate(
                                    payload_dict
                                )
                            )
                        except ValidationError as ve:
                            # The reply ARRIVED but failed structural
                            # validation, so besides notifying the user the
                            # pending future must be FAILED eagerly: otherwise
                            # the paused turn hangs until its timeout and then
                            # degrades to a timeout error instead of an in-band
                            # typed one. The request_id is parsed defensively,
                            # since a totally malformed envelope may carry none.
                            err_msg = ve.errors()[0]["msg"]
                            await _send_error(
                                websocket,
                                state.session_id,
                                "TOOL_PARAMS_INVALID",
                                f"spatial-input-response invalid: {err_msg}",
                            )
                            req_id = None
                            if isinstance(payload_dict, dict):
                                rid = payload_dict.get("request_id")
                                if isinstance(rid, str) and rid:
                                    req_id = rid
                            if req_id is not None and _fail_pending_spatial_input(
                                state.session_id,
                                req_id,
                                "SPATIAL_INPUT_BAD_BARRIER_TYPE",
                                err_msg,
                            ):
                                logger.info(
                                    "spatial-input-response invalid: FAILED "
                                    "pending future session=%s request_id=%s "
                                    "(no timeout wait)",
                                    state.session_id,
                                    req_id,
                                )
                            else:
                                logger.warning(
                                    "spatial-input-response invalid with no "
                                    "resolvable pending request_id=%s session=%s "
                                    "(notified only)",
                                    req_id,
                                    state.session_id,
                                )
                            continue
                        if not _resolve_pending_spatial_input(
                            state.session_id, spatial_resp
                        ):
                            logger.warning(
                                "spatial-input-response for unknown/closed "
                                "request_id=%s session=%s",
                                spatial_resp.request_id,
                                state.session_id,
                            )
                            continue
                        logger.info(
                            "spatial-input-response accepted session=%s "
                            "request_id=%s cancelled=%s geometry_type=%s",
                            state.session_id,
                            spatial_resp.request_id,
                            spatial_resp.cancelled,
                            spatial_resp.geometry_type,
                        )

                    elif msg_type == "tool-choice":
                        # The user's reply to a pending tool-candidates card,
                        # parsed defensively as a loose dict; it resolves the
                        # paused turn's future and may arrive on a sibling
                        # connection of the session.
                        if not isinstance(payload_dict, dict) or not isinstance(
                            payload_dict.get("request_id"), str
                        ):
                            await _send_error(
                                websocket,
                                state.session_id,
                                "TOOL_PARAMS_INVALID",
                                "tool-choice requires a request_id",
                            )
                            continue
                        if not _resolve_pending_tool_choice(
                            state.session_id, payload_dict
                        ):
                            logger.warning(
                                "tool-choice for unknown/closed request_id=%s "
                                "session=%s",
                                payload_dict.get("request_id"),
                                state.session_id,
                            )
                            continue
                        logger.info(
                            "tool-choice accepted session=%s request_id=%s "
                            "tool=%r free_text=%s",
                            state.session_id,
                            payload_dict.get("request_id"),
                            payload_dict.get("tool_name"),
                            bool(payload_dict.get("free_text")),
                        )

                    elif msg_type == "session-config":
                        # Per-session settings, currently the routing-visibility
                        # mode, read defensively off the raw dict; an unknown
                        # field is ignored for forward-compatibility.
                        if isinstance(payload_dict, dict):
                            _cfg_mode = payload_dict.get("mode")
                            if isinstance(_cfg_mode, str) and _cfg_mode.strip().lower() in (
                                "auto",
                                "ask",
                            ):
                                state.routing_mode = _cfg_mode.strip().lower()
                                logger.info(
                                    "session-config: routing mode=%s session=%s",
                                    state.routing_mode,
                                    state.session_id,
                                )
                            elif _cfg_mode is not None:
                                logger.warning(
                                    "session-config: unknown mode %r ignored "
                                    "session=%s",
                                    _cfg_mode,
                                    state.session_id,
                                )
                            # Arms or disarms the bench tool-block config:
                            # absent leaves it untouched, a dict arms, null
                            # disarms. A normal client never sends this key.
                            if "bench_tool_block" in payload_dict:
                                from trid3nt_server.gates.tool_gating import parse_bench_block_config

                                _bench_cfg = parse_bench_block_config(payload_dict)
                                state.bench_block_config = _bench_cfg
                                logger.info(
                                    "session-config: bench_tool_block %s "
                                    "session=%s (allow=%d always=%d block=%d)",
                                    "armed" if _bench_cfg else "disarmed",
                                    state.session_id,
                                    len(_bench_cfg.allow) if _bench_cfg else 0,
                                    len(_bench_cfg.always_allowed) if _bench_cfg else 0,
                                    len(_bench_cfg.block_at_invocation)
                                    if _bench_cfg
                                    else 0,
                                )

                    else:
                        await _send_error(
                            websocket,
                            state.session_id,
                            "INTERNAL_ERROR",
                            f"unknown message type: {msg_type!r}",
                        )

                except ValidationError as ve:
                    await _send_error(
                        websocket,
                        state.session_id,
                        "TOOL_PARAMS_INVALID",
                        f"payload validation failed: {ve.errors()[0]['msg']}",
                    )

        except (ConnectionClosedError, ConnectionClosedOK) as exc:
            # A peer close - pong timeout, client exit, network blip - is not a
            # crash, so it is one quiet line rather than a traceback. The close
            # code and reason are logged so "why did the socket die?" is
            # answerable from the journal.
            logger.info(
                "ws-close session=%s code=%s reason=%r",
                getattr(state, "session_id", None),
                getattr(exc, "code", None),
                getattr(exc, "reason", None),
            )
        except Exception:
            logger.exception("connection handler crashed")
        finally:
            # Drop this socket from the per-session registry on EVERY exit path,
            # so the reaper never targets a connection already gone and the
            # registry cannot grow unbounded. A socket that closed before its
            # first envelope never bound a session_id; a re-drop is harmless.
            if state is not None:
                _deregister_session_connection(state.session_id, websocket)
                # Once the session's LAST live socket is gone the cached
                # case-list digest goes too: otherwise a later reconnect, which
                # builds a fresh state unaware of the stale digest, could inherit
                # an emit-skip decision from a connection that no longer exists.
                if session_connection_count(state.session_id) == 0:
                    _clear_case_list_hash(state.session_id)
            # Stop the per-connection data heartbeat on EVERY exit path so the
            # background task never outlives its socket. Cancel and await, so the
            # cancellation is observed rather than logged as a destroyed pending
            # task; an already-done task is a harmless no-op.
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                # CancelledError is the expected clean-stop path; any other error
                # from the dying task must not mask the disconnect handling below.
                pass
            # A socket close must NOT cancel an in-flight turn: on disconnect it
            # DETACHES instead. Each turn is registered in the module-level
            # registry at spawn with a self-removing callback, so it survives
            # this connection; a reconnecting socket rebinds its emitter sink,
            # and a fully disconnected solve still publishes and persists its
            # layer. Genuine cancellation - a stop, a same-stream supersede -
            # still cancels; only the disconnect path stops cancelling.
            if state:
                for _turn_key, _t in list(state.inflight_tasks.items()):
                    if _t.done():
                        continue
                    # Re-assert the durable registry entry defensively for any
                    # path that populated inflight_tasks without registering.
                    # This DETACHES and keeps the turn RUNNING: it never clears
                    # the emitter, so the live turn keeps driving its own, whose
                    # sink silently no-ops on the dead socket until a reconnect
                    # rebinds it.
                    if _find_live_turn(state.session_id, _turn_key) is not _t:
                        _register_live_turn(
                            state.session_id, _turn_key, _t, state.emitter
                        )
                    logger.info(
                        "connection closed with in-flight turn session=%s "
                        "turn_key=%s: DETACHED (kept running), not cancelled",
                        state.session_id,
                        _turn_key,
                    )

    return handler

async def run_server(host: str = "127.0.0.1", port: int | None = None) -> None:
    """Serve forever; the port comes from ``TRID3NT_AGENT_PORT``. Persistence
    and the sibling HTTP catalog listener are both best-effort: a failure to
    start either logs and leaves the WebSocket server running."""
    if port is None:
        port = int(os.environ.get("TRID3NT_AGENT_PORT", "8765"))
    # Bind-host override so the agent is reachable off the loopback interface;
    # the default stays loopback-only.
    host = os.environ.get("TRID3NT_AGENT_HOST", host)
    settings = load_settings()
    # Log the ACTUAL active provider and its real model, never the settings
    # default, which the scripted and replay paths alone fall back to.
    from trid3nt_server.adapters.model_selection import (
        model_provider as _active_model_provider,
    )

    _active_provider = _active_model_provider()
    if _active_provider == "openai":
        from trid3nt_server.adapters import openai_adapter as _active_oa
        _active_model = _active_oa.openai_model(None)
    else:
        _active_model = settings.model
    logger.info(
        "starting agent server host=%s port=%d provider=%s model=%s",
        host,
        port,
        _active_provider,
        _active_model,
    )
    # Armed-only emit-free gate for the sync-tool off-load: one log line when
    # disabled, and an abort at startup when an armed tool's body would touch the
    # loop-bound emitter from a worker thread.
    _assert_sync_offload_safe()
    try:
        await init_persistence_from_env()
    except Exception as exc:  # noqa: BLE001 -- startup must not abort on persistence issues
        logger.warning("Persistence init failed (continuing without MCP): %s", exc)

    # Warm the tool-retrieval index off-loop at startup rather than lazily on the
    # first search: a COLD index fails open to the full registry, which is
    # harmless for a large-context model but silently truncates a small-context
    # one, leaving it unable to see tool schemas. Fire-and-forget - a failed warm
    # just leaves the fail-open behaviour in place and never delays serving.
    async def _warm_discover_index() -> None:
        try:
            from trid3nt_server.tools.search.search_tools import search_tools as _dd_warm
            await asyncio.to_thread(_dd_warm._get_index)
            logger.info("tool_retrieval: discover index warmed at startup")
        except Exception:  # noqa: BLE001 -- warm is best-effort
            logger.warning(
                "tool_retrieval: startup index warm failed; fail-open stays",
                exc_info=True,
            )
    _warm_task = asyncio.create_task(_warm_discover_index())
    _BG_TASKS.add(_warm_task)
    _warm_task.add_done_callback(_BG_TASKS.discard)

    handler = _make_handler(settings)

    # Best-effort mount of the catalog HTTP listener.
    http_server = None
    try:
        from trid3nt_server.server.protocol.catalog_http import serve_catalog_http

        http_server = await serve_catalog_http(host=host)
    except Exception:  # noqa: BLE001 -- discovery surface, never blocks WS
        logger.exception(
            "tool-catalog HTTP listener failed to start; "
            "continuing without /api/tool-catalog"
        )

    try:
        # EXPLICIT SERVE KEEPALIVE: a bare ``serve(handler, host, port)`` leaves
        # websockets on its defaults, so the server emits no protocol-level
        # pings and gives a stalled send the default close grace.
        # Pin ping_interval/ping_timeout (~20s/20s) so the SERVER actively
        # probes liveness and reaps a truly-dead peer on its own clock (the
        # client's app-level session-resume keepalive is the belt; this is the
        # suspenders), and a sane ``close_timeout`` so a terminal frame written
        # onto a half-closed socket doesn't hang the handler. These are
        # deliberately looser than the client's 25s/10s app keepalive so the
        # two layers don't fight (the client force-reconnects first on a real
        # stall; the server ping just keeps an otherwise-idle-but-alive socket
        # from being culled by an intermediary).
        async with serve(
            handler,
            host,
            port,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        ):
            await asyncio.Future()  # serve forever
    finally:
        # Graceful-shutdown drain of any outstanding detached background tasks:
        # a stop signal unwinds here while fire-and-forget tasks may still be
        # pending, so they are gathered under a bounded timeout that cannot hang
        # the exit.
        await _drain_bg_tasks()
        if http_server is not None:
            http_server.close()
            try:
                await http_server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
