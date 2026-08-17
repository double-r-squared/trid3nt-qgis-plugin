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

# --------------------------------------------------------------------------- #
# Per-session connection registry: session_id -> set of live ServerConnection.
# --------------------------------------------------------------------------- #
# Reap invariant: never close the keeper (the resuming connection) - it is
# identified by object identity and excluded before any close (mis-targeting
# kills the active tab). Single asyncio loop, one process -> a plain dict/set
# mutated from coroutine context needs no lock. The value-set is keyed by the
# connection object so a re-register is a no-op; an empty bucket is pruned so
# the dict cannot grow unbounded (session durability).

#: Application close code for a prior socket reaped because a newer connection
#: of the SAME session resumed. 4xxx is the WebSocket spec's reserved
#: application range; the client treats it like any other close.
def inflight_turn_count() -> int:
    """Number of in-flight turns detached from a (possibly-dead) connection.

    A long solver turn survives a socket drop (``_SESSION_LIVE_TURNS``); this
    counts the turns still running even if zero sockets are open. Kept as the
    observability probe over the live-turn registry (tests assert turn
    lifecycle through it). Counts only not-yet-done tasks (a done task is
    awaiting its self-removing callback).
    """
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

        # WS-30s STORM FIX (primary): start the per-connection data heartbeat so
        # the client's inbound-activity timer is reset on a fast server clock
        # (every HEARTBEAT_INTERVAL_SECONDS), independent of the possibly-slow
        # session-resume reply. Cancelled in the finally below on EVERY exit path.
        # The session_id is bound on the first inbound envelope; the heartbeat
        # frame's session_id is purely cosmetic (the client routes by transport,
        # not session, for a liveness frame) so a pre-handshake placeholder ULID
        # of zeros is fine until ``state`` is set.
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

                # Log EVERY inbound frame's type BEFORE
                # routing so "did the user's prompt arrive?" is directly visible
                # in the agent journal. The high-frequency keepalive (the client's
                # ``session-resume`` ping ~every 25s) is logged at DEBUG so it does
                # not flood the INFO stream; everything else (user-message,
                # case-command, ...) is logged at INFO. The server-sent
                # ``heartbeat`` is OUTBOUND only and never reaches this point.
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
                    # The auth-token envelope is the
                    # connect-handshake. Anything else, before the handshake
                    # completes, trips the anonymous fallback inline so
                    # ``SessionState.authenticated_user_id`` is bound before
                    # any user-scoped action runs.
                    if msg_type == "auth-token":
                        await _handle_auth_token(
                            websocket, state, payload_dict
                        )
                        continue
                    # Implicit anonymous fallback when any other envelope
                    # arrives before the handshake -- keeps the legacy
                    # no-auth-token clients working. Remote-daemon access:
                    # when a token gate is set, the implicit path
                    # rejects + closes the socket (returns False) so we must
                    # NOT dispatch the pending envelope.
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
                        # Structured canvas AOI. Read the
                        # optional ``aoi_bbox`` DEFENSIVELY off the raw
                        # payload dict -- the UserMessagePayload contract field
                        # lands in the client lane; this seam works the moment
                        # the field arrives and is a no-op for clients that
                        # never send it. Key-present semantics: a bbox SETS
                        # the active AOI, an explicit null CLEARS it, an
                        # absent key (older client) leaves the prior AOI.
                        if "aoi_bbox" in payload_dict:
                            _set_active_aoi_from_payload(
                                state, payload_dict.get("aoi_bbox")
                            )
                        # The dock's 'Draw region' rubber-band
                        # rectangle. Same key-present semantics as aoi_bbox: a
                        # value SETS the drawn geometry, an explicit null CLEARS
                        # it, an absent key leaves the prior state (no-op for
                        # clients that never send it).
                        if "drawn_geometry" in payload_dict:
                            _set_drawn_geometry_from_payload(
                                state, payload_dict.get("drawn_geometry")
                            )
                        # Routing-visibility mode, carried as the
                        # user-message's ``tool_choice_mode`` field. Read
                        # defensively off the raw dict; a set value updates
                        # the session's sticky mode, absent/None leaves the
                        # prior mode (env default otherwise -- see
                        # _session_routing_mode). session-config below is the
                        # alternate config path.
                        _tcm = payload_dict.get("tool_choice_mode")
                        if isinstance(_tcm, str) and _tcm.strip().lower() in (
                            "auto",
                            "ask",
                        ):
                            state.routing_mode = _tcm.strip().lower()
                        # Check the turn cap BEFORE dispatching.
                        # Increment first so "26th turn" fires on turn_count ==
                        # MAX_TURNS_PER_SESSION + 1. Sessions that already hit
                        # the cap are refused on every subsequent user-message
                        # with the same cap-hit envelope.
                        state.turn_count += 1
                        if (
                            MAX_TURNS_PER_SESSION > 0
                            and state.turn_count > MAX_TURNS_PER_SESSION
                        ):
                            await _handle_max_turns_reached(websocket, state)
                            continue
                        # Reset the per-turn layer accumulator before dispatch
                        # so the CaseChatMessage write captures only this
                        # turn's emissions. KNOWN LIMIT: these slots are still
                        # session-shared -- a turn running concurrently in
                        # ANOTHER Case may interleave layer/pipeline-id
                        # attribution on the closing agent row (Case targeting
                        # itself stays safe via the turn pin).
                        state.current_turn_layer_ids = []
                        state.current_turn_pipeline_id = None
                        state.current_turn_map_commands = []
                        # Pre-dispatch sequence (see ``_prepare_user_turn``):
                        # sibling-connection Case sync, AUTO-CREATE Case for a
                        # non-directive prompt from the Cases root (named via
                        # _derive_case_title; case-open + case-list emitted so
                        # the UI flips into the Case view), and the user-turn
                        # chat persist -- all BEFORE the turn task starts so
                        # chat + layer attribution land on the right (possibly
                        # brand-new) Case. Returns the parsed ``/invoke``
                        # directive; None streams through the model.
                        directive = await _prepare_user_turn(
                            websocket, state, um.text, client_case_id=um.case_id
                        )
                        # Stream-scoped cancellation: only a re-prompt in the
                        # SAME stream (Case, or root) replaces that stream's
                        # in-flight turn; turns in other Cases keep running.
                        # The key comes from the turn pin set by
                        # _prepare_user_turn (auto-created Cases mint a fresh
                        # ULID so they never collide with a running turn).
                        turn_key = (
                            state.current_turn_case_id or _ROOT_STREAM_KEY
                        )
                        # A same-stream re-prompt SUPERSEDES (cancels) the
                        # prior turn, even one DETACHED to the module-level
                        # registry by a prior socket close. Check this
                        # connection first, then the session-scoped live-turn
                        # registry.
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
                        # turn for this session (e.g. a live solve launched on
                        # a now-closed socket) so its progress + terminal
                        # frames reach the new socket. Harmless when no live
                        # turns exist.
                        _ensure_emitter(websocket, state)
                        _rebind_live_turns(state.session_id, state.emitter)
                        # In-chat model selector: hot-swap the model per turn.
                        # A non-None model_id in the message overrides the
                        # session default; None means "keep whatever was last
                        # chosen" (or the env default if never set).
                        #
                        # VALIDATE before use: a stale client (or a removed /
                        # access-disabled / non-tool-capable id) must NEVER reach
                        # ConverseStream -- an invalid id throws a raw
                        # ValidationException ("provided model identifier is
                        # invalid"). resolve_selected_model maps an unknown id to
                        # None (use the capable default) and returns a notice we
                        # log; the turn then runs on the default rather than
                        # crashing.
                        if um.model_id is not None:
                            from trid3nt_server.adapters.bedrock_adapter import (
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
                        # Register this turn in the module registry NOW (not
                        # only on disconnect), keyed by (session_id, turn_key)
                        # with a self-removing done-callback, so a subsequent
                        # socket close just drops the per-connection ref while
                        # the running task stays durable; a reconnect rebinds
                        # the recorded emitter's sink.
                        _register_live_turn(
                            state.session_id, turn_key, task, state.emitter
                        )

                    elif msg_type == "dev-tool-invoke":
                        # !run direct tool invocation: the plugin
                        # parsed ``!run <tool>(...)`` client-side and sent
                        # structured {name, args}. Runs the registry closure
                        # OUTSIDE the LLM loop through the SAME emission +
                        # gate + persistence seam as /invoke -- see
                        # _handle_dev_tool_invoke. Read defensively off the raw
                        # dict (no new contract model, mirroring turn-complete
                        # / aoi_bbox); the handler validates the wire shape and
                        # routes an unknown tool through the TOOL_NOT_FOUND
                        # envelope. Always-on in local mode (the tailnet is the
                        # trust boundary; the code-exec HARD gate still fires
                        # for code_exec_request via the shared invoke seam).
                        await _handle_dev_tool_invoke(
                            websocket, state, payload_dict
                        )

                    elif msg_type == "case-command":
                        # Case lifecycle dispatch. The
                        # envelope is validated through the pydantic model
                        # so an unknown command raises ValidationError and
                        # surfaces TOOL_PARAMS_INVALID via the outer block
                        # (closed enum -- see CaseCommand Literal).
                        cmd = CaseCommandEnvelopePayload.model_validate(
                            payload_dict
                        )
                        await _handle_case_command(websocket, state, cmd)

                    elif msg_type == "layer-delete":
                        # Per-layer delete: drops ``layer_id`` from the live
                        # emitter's loaded_layers, emits a fresh session-state
                        # (Map.tsx replace-not-reconcile removes the overlay),
                        # and persists the post-deletion list AUTHORITATIVELY
                        # (replace, not the union merge, which would
                        # resurrect it) -- including the loaded-layers note
                        # source so ``build_layers_present_note`` stops
                        # listing it. Payload is loosely-shaped; read inline.
                        await _handle_layer_delete(
                            websocket, state, payload_dict
                        )

                    elif msg_type == "secret-add":
                        # Credential push: the plugin brokers a QgsAuthManager
                        # key VALUE over this seam (connect-time per provider, or
                        # on a credential-request retry). The value lands in the
                        # in-memory resolver session cache; never persisted or
                        # echoed back (wire isolation from persisted state).
                        sa = SecretAddEnvelopePayload.model_validate(
                            payload_dict
                        )
                        await _handle_secret_add(websocket, state, sa)

                    elif msg_type == "cancel":
                        CancelPayload.model_validate(payload_dict)
                        logger.info("cancel session=%s", state.session_id)
                        # Target the VISIBLE stream's turn (the
                        # stop button lives in the active Case's composer);
                        # fall back to any live turn so the pre-0269
                        # "cancel cancels the run" contract still holds
                        # when the binding moved.
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
                        # module-level live-turn registry by a prior socket
                        # close (disconnect stops cancelling but the task
                        # keeps running). The explicit stop button must still
                        # reach it -- try the keyed entry, then any live
                        # detached turn for the session.
                        if cancel_task is None or cancel_task.done():
                            cancel_task = _find_live_turn(
                                state.session_id, cancel_key
                            ) or _any_live_turn(state.session_id)
                        if cancel_task is not None and not cancel_task.done():
                            cancel_task.cancel()
                            # Wait briefly so the cancel completes deterministically
                            # within a 30s budget. The pipeline-state
                            # cancelled frame is emitted from inside the task's
                            # CancelledError branch.
                            try:
                                await asyncio.wait_for(cancel_task, timeout=5.0)
                            except (asyncio.CancelledError, asyncio.TimeoutError):
                                pass

                    elif msg_type == "tool-payload-confirmation":
                        # Route the confirmation to the paused
                        # dispatch coroutine. Validate the envelope here so
                        # malformed payloads don't poison the future.
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
                        # Resolve via the SESSION-scoped module
                        # registry -- the gate may have been registered on a
                        # DIFFERENT WebSocket connection of this same session
                        # (StrictMode double-mount / reconnect).
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
                        # Resolves the paused dispatch coroutine's future once
                        # the user saves/declines a requested credential -- the
                        # tool retries (provided=True) or re-raises the
                        # original typed error (provided=False). Carries NO
                        # key material; the key itself was saved
                        # via ``secret-add`` on its own envelope path.
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
                        # region-disambiguation picker: the user narrowed the
                        # state-bbox-fallback geocode to a sub-region (or kept
                        # the whole state). Resolve the paused dispatch
                        # coroutine's future so it applies the picked bbox (or
                        # keeps the state bbox). Mirrors credential-provided --
                        # may arrive on a sibling connection of the session.
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
                        # The user finished (or cancelled)
                        # the terra-draw surface. Resolve the paused
                        # request_spatial_input future so the dispatch coroutine
                        # parses the drawn FeatureCollection into engine-ready
                        # barriers / AOI / points. Mirrors region-choice-provided
                        # -- may arrive on a sibling connection of the session.
                        try:
                            spatial_resp = (
                                SpatialInputResponsePayload.model_validate(
                                    payload_dict
                                )
                            )
                        except ValidationError as ve:
                            # Untagged-barrier mismatch (the critical
                            # correctness fix): the reply ARRIVED but failed
                            # structural validation (e.g. a barrier feature
                            # missing barrier_type). The user-facing notification
                            # stays, but we MUST also FAIL the pending future
                            # eagerly so the paused request_spatial_input turn
                            # wakes IN-BAND with a typed error result instead of
                            # hanging until default_timeout_seconds (~300s) then
                            # degrading to SPATIAL_INPUT_TIMEOUT. The request_id
                            # is parsed defensively from the raw payload (it may
                            # itself be absent/garbage on a totally malformed
                            # envelope -- then we just notify + continue, no crash).
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
                        # The user's reply to a pending
                        # ``tool-candidates`` card, parsed defensively as a
                        # loose dict (until the typed contracts model lands).
                        # Resolves the paused turn's future -- may arrive on a
                        # sibling connection of the session.
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
                        # Per-session settings. Currently
                        # the routing-visibility ``mode`` ('auto' | 'ask') --
                        # read DEFENSIVELY off the raw dict (the contracts
                        # lane declares the typed model). Unknown fields are
                        # ignored for forward-compat.
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
                            # BENCH pre-dispatch block hook: arms/disarms the
                            # bench tool-block config (absent=untouched,
                            # dict=arm, null/false=disarm). Bench-only -- a
                            # normal client never sends this key.
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

                    elif msg_type in (
                        "confirm-response",
                        "disambiguation-response",
                        "clarification-response",
                    ):
                        # Scaffolding only -- no triggers yet. Log and
                        # acknowledge without acting.
                        logger.info("noop M1 message_type=%s", msg_type)

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
            # Normal/abnormal peer closes (pong timeout, tab/mobile close,
            # network blip, StrictMode socket churn) are not crashes - log a
            # quiet one-liner instead of a full traceback.
            #
            # Log the close code + reason at INFO so the
            # "why did the socket die?" question is directly answerable from the
            # journal (e.g. a 1006/no-close-frame storm vs a clean 1000/1001 tab
            # close). This is one line per disconnect, not a per-frame flood.
            logger.info(
                "ws-close session=%s code=%s reason=%r",
                getattr(state, "session_id", None),
                getattr(exc, "code", None),
                getattr(exc, "reason", None),
            )
        except Exception:
            logger.exception("connection handler crashed")
        finally:
            # Drop this socket from the per-session connection registry on EVERY
            # exit path so the reaper never targets a connection already gone and
            # the registry cannot grow unbounded. Guard on ``state`` - a socket
            # that closed before its first envelope never bound a session_id.
            # Idempotent: a socket already reaped by a sibling's resume is a
            # harmless discard.
            if state is not None:
                _deregister_session_connection(state.session_id, websocket)
                # OPEN-8: once the session's LAST live socket is gone, drop the
                # cached case-list digest too -- otherwise a later reconnect
                # (fresh SessionState, unaware of the stale digest) could
                # inherit an emit-skip decision from a connection that no
                # longer exists. ``session_connection_count`` is 0 only when
                # every sibling socket of this session has also deregistered.
                if session_connection_count(state.session_id) == 0:
                    _clear_case_list_hash(state.session_id)
            # WS-30s STORM FIX: stop the per-connection data heartbeat on EVERY
            # exit path (normal close, crash, cancellation, loop exhaustion) so
            # the background task never outlives its socket. Cancel + await so the
            # CancelledError is observed (no "Task was destroyed but it is pending"
            # warning); a never-started/already-done task is a harmless no-op.
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                # CancelledError is the expected clean-stop path; any other error
                # from the dying task must not mask the disconnect handling below.
                pass
            # A socket close must NOT cancel an in-flight turn: on disconnect,
            # DETACH rather than cancel. Each turn is registered in the
            # module-level ``_SESSION_LIVE_TURNS`` registry at spawn (keyed by
            # (session_id, turn_key)) with a self-removing done-callback, so it
            # survives the death of this connection; a reconnecting socket
            # rebinds the live turn's emitter sink so progress + terminal
            # frames still reach the user, and a fully-disconnected solve
            # still publishes + persists its layer (rehydrates on the next
            # case-open). ``wait_for_completion``'s own 1800s budget bounds a
            # stuck solve. Genuine cancellation (stop button, same-stream
            # supersede) still cancels -- only the disconnect path stops
            # cancelling. Cheap LLM-only turns are simply left to finish;
            # their done-callback removes them from the registry.
            if state:
                for _turn_key, _t in list(state.inflight_tasks.items()):
                    if _t.done():
                        continue
                    # Ensure the durable registry holds it (it was registered at
                    # spawn for user-message turns; re-assert for any path that
                    # populated inflight_tasks without registering -- defensive,
                    # idempotent). NB: this finally DETACHES and KEEPS the turn
                    # RUNNING -- it never sets ``state.emitter = None``. The live
                    # turn keeps driving its OWN emitter, whose ``_sink`` still
                    # closes over THIS (now-dead) socket and silently no-ops on
                    # send, until a reconnecting socket rebinds that emitter's
                    # sink (``_rebind_live_turns``) so the remaining progress +
                    # terminal frames land on the user's live connection.
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
    """Serve forever. Override port via ``TRID3NT_AGENT_PORT``.

    Best-effort inits the ``Persistence`` singleton; if persistence is unbound
    the agent starts anyway (the in-memory chat/pipeline path keeps working,
    and any caller requiring persistence raises a clear error). Also mounts the
    read-only HTTP catalog endpoint at
    ``TRID3NT_AGENT_HTTP_PORT`` (default 8766) as a sibling of the WS server
    (same loop, same process); a failure to start it logs but does not abort
    WS startup.
    """
    if port is None:
        port = int(os.environ.get("TRID3NT_AGENT_PORT", "8765"))
    # Bind host override so the dev agent is reachable from the
    # LAN / tailnet (phone demos). Default stays loopback-only; opt in via
    # TRID3NT_AGENT_HOST=0.0.0.0. The real public surface is a later increment.
    host = os.environ.get("TRID3NT_AGENT_HOST", host)
    settings = load_settings()
    # Log the ACTUAL active provider + its real model, never the settings
    # default. Under MODEL_PROVIDER=openai this prints the OpenAI model; under
    # bedrock the Bedrock model id; scripted/replay/fake fall back to the
    # settings model.
    from trid3nt_server.adapters.bedrock_adapter import (
        model_provider as _active_model_provider,
        bedrock_model_id as _active_default_model_id,
    )

    _active_provider = _active_model_provider()
    if _active_provider == "openai":
        from trid3nt_server.adapters import openai_adapter as _active_oa
        _active_model = _active_oa.openai_model(None)
    elif _active_provider == "bedrock":
        _active_model = _active_default_model_id()
    else:
        _active_model = settings.model
    logger.info(
        "starting agent server host=%s port=%d provider=%s model=%s",
        host,
        port,
        _active_provider,
        _active_model,
    )
    # Loop-safety: armed-only emit-free safety gate for the staged
    # sync-tool dispatch off-load. No-op (one log line) under the dark default;
    # raises and aborts startup if TRID3NT_SYNC_TOOL_OFFLOAD is armed for a tool
    # whose body would touch the loop-bound emitter from a worker thread.
    _assert_sync_offload_safe()
    try:
        await init_persistence_from_env()
    except Exception as exc:  # noqa: BLE001 -- startup must not abort on persistence issues
        logger.warning("Persistence init failed (continuing without MCP): %s", exc)

    # TOOL-RETRIEVAL INDEX WARM-AT-STARTUP: enforce is the unconditional
    # surfacing path, so build the discover index off-loop NOW instead of lazily
    # on the first search_tools tool call. Without this every turn's
    # _discover_topk sees a COLD index and FAIL-OPENS to the full registry --
    # harmless for 200k-context cloud models, but a SMALL-CONTEXT local model
    # (offline build, e.g. 16k Ollama) gets its request silently truncated, so it
    # cannot see tool schemas and guesses argument names. Fire-and-forget: a
    # failed warm just leaves the documented fail-open behavior in place; never
    # delays serving.
    async def _warm_discover_index() -> None:
        try:
            from trid3nt_server.data.search.search_tools import search_tools as _dd_warm
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
        # Graceful-shutdown drain of any outstanding detached background tasks.
        # A SIGTERM (graceful process stop) cancels ``await asyncio.Future()``
        # and unwinds here while fire-and-forget tasks may still be pending in
        # ``_BG_TASKS`` (e.g. the startup discover-index warm). gather them with
        # a bounded timeout so the flush cannot hang shutdown indefinitely.
        await _drain_bg_tasks()
        if http_server is not None:
            http_server.close()
            try:
                await http_server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
