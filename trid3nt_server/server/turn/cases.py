"""Case lifecycle over the wire: list/open/command handlers + context sync + auto-naming."""

from __future__ import annotations

import logging
from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.case import CaseCommandEnvelopePayload, CaseListEnvelopePayload, CaseOpenEnvelopePayload, CaseSessionState, CaseSummary
from trid3nt_server.adapters.adapter import REHYDRATE_HISTORY_CAP, rehydrate_history_from_case
from trid3nt_server.emission.uri_registry import get_uri_registry
from trid3nt_server.server.dispatch.emitter import _ensure_emitter
from trid3nt_server.server.session.case_state import _AUTONAMED_CASES, _SESSION_CASE_LIST_HASH, _cache_case_bbox_from_session_state, _case_list_digest, _derive_case_title, _persist_session_active_case, _seed_registry_for_case, _touch_session_record
from trid3nt_server.server.session.persistence_ref import get_persistence
from trid3nt_server.server.session.state import SessionState
from trid3nt_server.server.spatial import _coerce_bbox4
from trid3nt_server.server.turn.live_turn import _rebind_live_turns
from trid3nt_server.server.turn.wire import _new_envelope, _send_error
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")

async def _emit_case_list(
    websocket: ServerConnection, state: SessionState, *, force: bool = False
) -> None:
    """Emit the ``case-list`` envelope for the client's left rail.

    Best-effort: skips silently if Persistence is unbound; logs + skips on
    a listing failure (the case-list is a derivable view and must not break
    the chat path).

    The list is scoped by ``state.authenticated_user_id`` (Firebase UID, or
    the sticky-anonymous ULID in dev), matching the owner stamped onto
    Cases at creation -- a Case is visible only to its owner. Falls back to
    ``session_id`` only when the handshake hasn't bound a user yet.

    Change-guard: ``force=False`` (default) skips the send when the list is
    byte-for-byte the same (by content digest, see ``_case_list_digest``)
    as the last emit for this SESSION, collapsing repeat keepalive/
    duplicate-socket resumes into a no-op. Callers that just performed (or
    may have performed) a mutation pass ``force=True`` so the client is
    never left with a stale list.
    """
    p = get_persistence()
    if p is None:
        logger.debug("case-list: Persistence unbound; skipping emit")
        return
    user_id = state.authenticated_user_id or state.session_id
    try:
        cases = await p.list_cases_for_user(user_id)
    except Exception:  # noqa: BLE001 -- best-effort
        logger.exception("case-list: list_cases_for_user failed")
        return
    digest = _case_list_digest(cases)
    if not force and _SESSION_CASE_LIST_HASH.get(state.session_id) == digest:
        logger.debug(
            "case-list unchanged session=%s user=%s count=%d — skipping emit",
            state.session_id,
            user_id,
            len(cases),
        )
        return
    _SESSION_CASE_LIST_HASH[state.session_id] = digest
    payload = CaseListEnvelopePayload(cases=cases)
    await websocket.send(_new_envelope("case-list", state.session_id, payload))
    logger.info(
        "case-list emitted session=%s user=%s count=%d",
        state.session_id,
        user_id,
        len(cases),
    )

def _rehydrate_case_history(
    state: SessionState,
    session_state: CaseSessionState,
    case_id: str,
) -> None:
    """Refill ``state.chat_history`` from a Case's PERSISTED messages.

    Called right after the ``state.chat_history = []`` reset in both
    ``_emit_case_open`` and ``_sync_case_context``. Converts the per-Case
    persisted ``CaseChatMessage`` list into the lightweight TEXT-turn dict
    shape ``build_contents_from_history`` consumes, appends a compact
    "layers already present" model turn, and bounds the replay to the last
    ``REHYDRATE_HISTORY_CAP`` rows so a long Case cannot blow the context
    window. Best-effort: any failure leaves the (empty) reset history
    intact.

    Guardrail: ``session_state`` belongs to exactly ONE ``case_id`` (the
    persisted store is keyed by Case), so this cannot reintroduce a
    cross-case leak.
    """
    try:
        # F20 / panel-fix: pass the Case AOI bbox so the layers-present note
        # carries the exact extent. It survives history capping, so a long
        # Case whose head turn (which named the place) was dropped can still
        # reuse the original AOI for follow-up fetch/clip instead of
        # re-geocoding / mis-scoping.
        case_bbox = getattr(getattr(session_state, "case", None), "bbox", None)
        history, dropped = rehydrate_history_from_case(
            session_state.chat_history,
            session_state.loaded_layers,
            case_bbox=case_bbox,
        )
        # REBIND, never extend the entry-captured list -- assigning a
        # fresh object keeps an in-flight turn's captured history untouched.
        state.chat_history = history
        if dropped:
            logger.info(
                "case-history-rehydrate session=%s case=%s dropped_head=%d "
                "kept=%d (cap=%d)",
                state.session_id,
                case_id,
                dropped,
                len(history),
                REHYDRATE_HISTORY_CAP,
            )
    except Exception:  # noqa: BLE001 -- rehydration is best-effort
        logger.exception(
            "case-history-rehydrate failed session=%s case=%s",
            state.session_id,
            case_id,
        )

async def _sync_case_context(
    websocket: ServerConnection, state: SessionState
) -> None:
    """Catch this CONNECTION's in-memory context up to the session's active
    Case.

    chat_history and the emitter's loaded_layers accumulator are
    per-connection state, so a case-command on a sibling connection (or a
    fresh reconnect) leaves them out of sync with the session's active
    Case. Called at the top of every user-message dispatch: on a Case
    change, replace (not reconcile) chat_history and reseed the emitter
    from the persisted Case, so add_loaded_layer dedup and
    _persist_case_loaded_layers writes operate on the full persisted
    truth set.

    Best-effort: a Persistence failure leaves the emitter seeded empty;
    the merge in _persist_case_loaded_layers prevents an unseeded
    accumulator from clobbering previously persisted layers.
    """
    current = state.active_case_id
    if state.case_context_synced_to == current:
        return
    state.case_context_synced_to = current
    # Replace-not-reconcile (applied cross-connection): this
    # connection's LLM context belongs to whatever Case it was last driving.
    # REBIND, never clear() -- an in-flight turn holds the old list
    # (captured at its stream entry) and must keep its own context intact.
    state.chat_history = []
    state.turn_count = 1  # count the in-flight turn that triggered the sync
    _ensure_emitter(websocket, state)
    if state.emitter is None:  # pragma: no cover -- _ensure_emitter always binds
        return
    if current is None:
        # JOB 2: no active Case -> no AOI anchor.
        state.case_bbox = None
        state.emitter.reset_loaded_layers([])
        # F32: no active Case -> no resolvable handles either (clears any
        # leftover registrations from whatever Case this connection last
        # drove).
        get_uri_registry(state.session_id).clear()
        return
    p = get_persistence()
    if p is None:
        state.emitter.reset_loaded_layers([])
        return
    try:
        session_state = await p.get_session_state(current)
        # JOB 2: cache the Case AOI so ``_turn_case_bbox`` has a durable
        # active-AOI anchor on this connection's turns (kills repeat-fetch /
        # re-geocode by feeding the reuse short-circuits + the per-turn note).
        _cache_case_bbox_from_session_state(state, session_state)
        state.emitter.reset_loaded_layers(session_state.loaded_layers)
        # Repopulate the inline-GeoJSON side-table
        # so this connection's next session-state emission carries renderable
        # vectors (mirrors the case-open path; best-effort).
        try:
            await state.emitter.reinline_vector_layers()
        except Exception:  # noqa: BLE001
            logger.warning("case-context-sync vector re-inline failed")
        # Seed the URI registry from the persisted Case layers so
        # handle-indirection works for layers produced in PRIOR sessions of this
        # Case (the LLM history was just cleared; the registry is the only
        # place the layer_id -> uri association survives). REPLACE, not
        # additive-seed -- this IS a case-switch point; an additive seed would
        # leak the previous Case's handles/URIs into this Case's resolution.
        # Also restores the Case's persisted L<n> short-handle map.
        await _seed_registry_for_case(
            state, current, session_state.loaded_layers
        )
        # F17: rehydrate this connection's LLM context from the SAME persisted
        # per-Case store. state.chat_history = [] above is the cross-connection
        # clean-slate; refilling it from current's persisted messages (already
        # fetched; do NOT re-fetch) lets a sibling-connection / reconnect turn
        # see prior work and stop recomputing. Per-Case store => case-correct.
        _rehydrate_case_history(state, session_state, current)
        logger.info(
            "case-context-sync session=%s case=%s layers=%d rehydrated=%d",
            state.session_id,
            current,
            len(session_state.loaded_layers),
            len(state.chat_history),
        )
    except Exception:  # noqa: BLE001 -- best-effort, never break the turn
        logger.exception(
            "case-context-sync failed session=%s case=%s",
            state.session_id,
            current,
        )
        state.emitter.reset_loaded_layers([])

async def _emit_case_open(
    websocket: ServerConnection,
    state: SessionState,
    case_id: str,
) -> None:
    """Emit a ``case-open`` envelope hydrating ``CaseSessionState`` from Mongo.

    Sets ``state.active_case_id`` BEFORE emitting so subsequent tool calls
    (and chat persistence) carry the Case context. If the Case is missing
    or Persistence is unbound, emits a ``case-open`` with ``session_state=None``
    so the client falls back to the empty state per
    ``CaseOpenEnvelopePayload`` semantics.
    """
    state.active_case_id = case_id
    # This connection runs the full case-open reset below, so its
    # context is (about to be) synced to ``case_id`` -- record it so the next
    # ``user-message`` on THIS connection skips the redundant re-sync.
    # Sibling connections of the same session keep their stale marker and
    # catch up via ``_sync_case_context`` on their next dispatch.
    state.case_context_synced_to = case_id
    # A Case switch must reset the per-connection LLM conversation, not just
    # the case state -- otherwise build_contents_from_history keeps feeding
    # old turns to the model and prompts misroute to the previous Case's
    # composer. Clean slate per Case (the replace-not-reconcile rule,
    # applied server-side); the visible chat replay comes from the
    # persisted Case history, not this list. REBIND, never clear() -- see
    # _sync_case_context.
    state.chat_history = []
    state.turn_count = 0
    await _touch_session_record(state, case_id=case_id)  # session heartbeat
    # Persist the active-Case pointer on explicit
    # case-open/select so the cold-start cache (``last_active_case_id``) is warm
    # for a reconnect after a process restart -- even for an older client that
    # later resumes with no ``case_id`` stamp.
    await _persist_session_active_case(state, case_id)
    p = get_persistence()
    if p is None:
        logger.warning(
            "case-open session=%s case=%s: Persistence unbound; emitting empty",
            state.session_id,
            case_id,
        )
        payload = CaseOpenEnvelopePayload(session_state=None)
        await websocket.send(
            _new_envelope("case-open", state.session_id, payload)
        )
        return
    try:
        session_state = await p.get_session_state(case_id)
    except Exception:  # noqa: BLE001
        logger.exception(
            "case-open: get_session_state failed for case=%s", case_id
        )
        payload = CaseOpenEnvelopePayload(session_state=None)
        await websocket.send(
            _new_envelope("case-open", state.session_id, payload)
        )
        return
    # JOB 2: cache the opened Case's AOI so the very first turn in this Case
    # already has the active-AOI anchor (reuse short-circuits + per-turn note).
    _cache_case_bbox_from_session_state(state, session_state)
    payload = CaseOpenEnvelopePayload(session_state=session_state)
    await websocket.send(_new_envelope("case-open", state.session_id, payload))

    # Seed the emitter with the persisted loaded_layers so any subsequent
    # session-state emission carries them rather than overwriting with an
    # empty list -- the emitter's _loaded_layers is the truth set the next
    # add_loaded_layer dedups against; without seeding, a republish of an
    # existing layer would be treated as a fresh append.
    _ensure_emitter(websocket, state)
    # Opening THIS Case is the user returning to where a long solve was
    # launched. If a turn keyed to this Case is still running (detached on a
    # prior socket close), rebind its emitter sink onto the freshly-opened
    # socket so the in-flight solve's progress + terminal session-state
    # reach the live connection. Keyed to case_id so a concurrent solve in
    # another Case is untouched.
    rebound = _rebind_live_turns(
        state.session_id, state.emitter, only_turn_key=case_id
    )
    if rebound:
        logger.info(
            "case-open rebound %d live turn(s) onto reconnect session=%s case=%s",
            rebound,
            state.session_id,
            case_id,
        )
    if state.emitter is not None:
        state.emitter.reset_loaded_layers(session_state.loaded_layers)
        # F32 (live-reported): seed the URI registry from the SAME persisted
        # layers the emitter/build_layers_present_note advertise. This is the
        # missing half of the explicit case-open path -- a fresh connection
        # (e.g. a QGIS dock reconnect) that opens an EXISTING Case via
        # case-command(select) reaches THIS function directly, never
        # _sync_case_context / _replay_active_case_layers. The registry is
        # session-scoped in-memory state, so on a genuinely fresh connection it
        # starts empty regardless of how many layers the Case has persisted.
        # Without this seed, the per-turn [Case state] note advertised handles
        # the registry could not resolve. REPLACE (not additive) so a Case
        # switch on this connection never leaks a prior Case's handles. ADR
        # 0014: also restores the Case's persisted L<n> short-handle map.
        await _seed_registry_for_case(
            state, case_id, session_state.loaded_layers
        )
        # Persisted VECTOR layers carry no inline GeoJSON (the side-table is
        # in-memory only), so the case-open payload above rehydrated entries the
        # browser cannot render. Re-inline from the artifact and emit one
        # follow-up session-state through the proven merge path so vectors
        # repaint.
        try:
            _reinlined = await state.emitter.reinline_vector_layers()
            if _reinlined:
                await state.emitter.emit_session_state()
        except Exception:  # noqa: BLE001 -- rehydration is best-effort
            logger.exception(
                "case-open vector re-inline failed case=%s", case_id
            )

    # F17: rehydrate the LLM conversation from THIS Case's persisted
    # messages so a follow-up turn in a reopened Case sees prior work and
    # stops recomputing. state.chat_history = [] above is the cross-case
    # clean-slate; refill from the PER-CASE persisted store (session_state
    # -- already loaded; do NOT re-fetch), which is inherently case-correct.
    _rehydrate_case_history(state, session_state, case_id)

    logger.info(
        "case-open session=%s case=%s chat=%d layers=%d rehydrated=%d",
        state.session_id,
        case_id,
        len(session_state.chat_history),
        len(session_state.loaded_layers),
        len(state.chat_history),
    )

async def _handle_case_command(
    websocket: ServerConnection,
    state: SessionState,
    cmd: CaseCommandEnvelopePayload,
) -> None:
    """Dispatch one ``case-command`` (Case lifecycle).

    Commands:

    - ``create`` -- generate a new ``CaseSummary``, persist via
      ``Persistence.upsert_case``, set as active, emit ``case-open`` with
      the fresh (empty) session state, then refresh ``case-list``.
    - ``select`` -- load the persisted ``CaseSessionState`` and emit
      ``case-open`` with the full rehydration (chat history, loaded
      layers, pipeline history -- the chat-replay default).
    - ``rename`` -- update ``CaseSummary.title``, persist, emit
      ``case-list`` updated.
    - ``archive`` -- soft-archive via ``Persistence.archive_case``, emit
      ``case-list`` updated.
    - ``delete`` -- soft-delete via ``Persistence.delete_case``, emit
      ``case-list`` updated. Memory rule: the web UI confirms with the
      user BEFORE firing this command; the server does not double-confirm.

    Errors surface as ``error`` envelopes with ``error_code=INTERNAL_ERROR``
    (the case-lifecycle commands are NOT a confirmation trigger;
    only solver runs and non-session-collection Mongo writes are).
    """
    p = get_persistence()
    if p is None:
        await _send_error(
            websocket,
            state.session_id,
            "INTERNAL_ERROR",
            "case-command requires Persistence; the agent service was started "
            "with TRID3NT_DEV_PERSISTENCE=0 and cannot satisfy Case persistence.",
        )
        return

    command = cmd.command

    if command == "create":
        # Generate a fresh ULID and persist. ``args.title`` is an optional hint.
        new_case_id = new_ulid()
        title = (cmd.args or {}).get("title") or "Untitled Case"
        if not isinstance(title, str) or not title.strip():
            title = "Untitled Case"
        # AOI-first: an optional ``args.bbox`` lets the user pin the AOI
        # extent BEFORE the first prompt (draw-on-map / numeric coords).
        # Coerced via the shared validator so a None / wrong-length /
        # non-finite value is dropped silently rather than crashing. When
        # present it persists on ``CaseSummary.bbox`` and seeds
        # ``state.case_bbox`` so the FIRST turn's ``_turn_case_bbox``
        # returns the user's extent and the LLM is told to REUSE it (no
        # re-geocode).
        create_bbox = _coerce_bbox4((cmd.args or {}).get("bbox"))
        now = now_utc()
        case = CaseSummary(
            case_id=new_case_id,
            title=title.strip(),
            created_at=now,
            updated_at=now,
            status="active",
            bbox=list(create_bbox) if create_bbox is not None else None,
        )
        try:
            # Stamp the creator as owner so the Case is visible to them via
            # ``list_cases_for_user``. ``authenticated_user_id`` is the fixed
            # local user; None only on the unbound-Persistence path. Cases are
            # durable -- no TTL stamp.
            await p.upsert_case(
                case,
                owner_user_id=state.authenticated_user_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("case-command(create) upsert failed: %s", exc)
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                f"case create failed: {exc}",
            )
            return
        state.active_case_id = new_case_id
        # A fresh Case must NOT inherit the previous Case's AOI anchor.
        # Reset the in-session bbox to None BEFORE the conditional seed
        # (mirrors the select/deselect handlers) so a bbox-less create
        # starts with no anchor -> ``_turn_case_bbox`` re-geocodes from the
        # place name in the first prompt instead of reusing the prior
        # Case's extent.
        state.case_bbox = None
        # #170 AOI-first: seed the in-session AOI anchor so the FIRST turn's
        # _turn_case_bbox returns the user's pre-set extent (mirrors
        # _pin_case_aoi_from_solve). Absent/invalid bbox => leave as-is (None).
        if create_bbox is not None:
            state.case_bbox = list(create_bbox)
        # See _emit_case_open -- this connection is now synced.
        state.case_context_synced_to = new_case_id
        # Fresh Case = fresh LLM context (see _emit_case_open note).
        # REBIND, never clear() -- see _sync_case_context.
        state.chat_history = []
        state.turn_count = 0
        await _touch_session_record(state, case_id=new_case_id)  # session heartbeat
        # Emit case-open with the empty session state for the fresh Case.
        payload = CaseOpenEnvelopePayload(
            session_state=await p.get_session_state(new_case_id)
        )
        await websocket.send(
            _new_envelope("case-open", state.session_id, payload)
        )
        # A fresh Case starts with NO loaded layers; flush
        # the emitter's per-connection accumulator so a subsequent tool call
        # in this Case doesn't accidentally inherit layers from whatever Case
        # the user just left (replace-not-reconcile applied server-side).
        _ensure_emitter(websocket, state)
        if state.emitter is not None:
            state.emitter.reset_loaded_layers([])
        # F32: a fresh Case starts with no resolvable handles either -- clear
        # any leftover registrations from whatever Case this connection last
        # drove (mirrors the emitter flush immediately above).
        get_uri_registry(state.session_id).clear()
        await _emit_case_list(websocket, state, force=True)
        logger.info(
            "case-command create session=%s case=%s title=%r",
            state.session_id,
            new_case_id,
            title,
        )
        return

    if command == "select":
        if not cmd.case_id:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                "case-command(select) requires case_id",
            )
            return
        await _emit_case_open(websocket, state, cmd.case_id)
        return

    if command == "deselect":
        # The client navigated OUT of the active Case to the Cases root.
        # Without this command the session-scoped active Case silently kept
        # pointing at the last-opened Case: prompts sent from the root view
        # skipped auto-create and dispatched INTO the stale Case, and
        # re-selecting that same Case looked like a no-op. Clears the
        # binding + this connection's LLM context so the next root prompt
        # auto-creates a fresh Case. Does NOT touch any in-flight turn -- its
        # persistence follows the turn pin, not this binding.
        prev = state.active_case_id
        state.active_case_id = None
        state.case_context_synced_to = None
        # JOB 2: clear the cached Case AOI so a root prompt (which auto-creates
        # a FRESH Case) does not reuse the just-exited Case's extent.
        state.case_bbox = None
        # REBIND, never clear() -- see _sync_case_context.
        state.chat_history = []
        state.turn_count = 0
        if state.emitter is not None:
            state.emitter.reset_loaded_layers([])
        # F32: no active Case -> no resolvable handles from the just-exited
        # Case either (mirrors _sync_case_context's current-is-None branch).
        get_uri_registry(state.session_id).clear()
        # job-CASE-AUTHORITY: clear the persisted pointer too, so a reconnect
        # after restart does NOT re-seed the just-exited Case.
        await _persist_session_active_case(state, None)
        logger.info(
            "case-command deselect session=%s prev_case=%s",
            state.session_id,
            prev,
        )
        return

    if command == "rename":
        if not cmd.case_id:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                "case-command(rename) requires case_id",
            )
            return
        new_title = (cmd.args or {}).get("title")
        if not isinstance(new_title, str) or not new_title.strip():
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                "case-command(rename) requires args.title (non-empty string)",
            )
            return
        existing = await p.get_case(cmd.case_id)
        if existing is None:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                f"case-command(rename): case {cmd.case_id!r} not found",
            )
            return
        updated = existing.model_copy(
            update={"title": new_title.strip(), "updated_at": now_utc()}
        )
        try:
            await p.upsert_case(updated)
        except Exception as exc:  # noqa: BLE001
            logger.exception("case-command(rename) upsert failed: %s", exc)
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                f"case rename failed: {exc}",
            )
            return
        await _emit_case_list(websocket, state, force=True)
        logger.info(
            "case-command rename session=%s case=%s title=%r",
            state.session_id,
            cmd.case_id,
            new_title,
        )
        return

    if command == "set-bbox":
        # Persistent per-case AOI: the plugin's draw/edit tool sends the
        # user's rectangle here so ``CaseSummary.bbox`` is durably the
        # user's chosen extent -- not None until a tool happens to pin it.
        # The agent already injects ``state.case_bbox`` into EVERY turn
        # (``_turn_case_bbox`` -> ``build_layers_present_note``) and snaps
        # fetch bbox params to it, so a set case bbox is exactly what stops
        # the model re-deriving/geocoding the area every turn. Clones the
        # rename branch: write the field, re-snapshot the view + thin
        # manifest, re-emit the case-list; ALSO update ``state.case_bbox``
        # when this is the OPEN case so the very next turn's in-prompt AOI
        # line is correct with no reopen.
        if not cmd.case_id:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                "case-command(set-bbox) requires case_id",
            )
            return
        # The "Clear AOI" control sends set-bbox with an EXPLICIT null/empty
        # bbox to RESET the case AOI. An explicitly-present-but-empty
        # ``bbox`` (None or []) = CLEAR (``CaseSummary.bbox`` -> None,
        # ``state.case_bbox`` -> None); a MISSING bbox key or a
        # non-empty-but-malformed bbox stays the honest error below. This
        # lets the plugin's Clear-AOI actually stop the agent anchoring on
        # the old extent every turn.
        raw_args = cmd.args or {}
        has_bbox_key = "bbox" in raw_args
        raw_bbox = raw_args.get("bbox")
        clear = has_bbox_key and (raw_bbox is None or raw_bbox == [])
        bbox = None if clear else _coerce_bbox4(raw_bbox)
        if not clear and bbox is None:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                "case-command(set-bbox) requires args.bbox = "
                "[min_lon, min_lat, max_lon, max_lat] (or an empty bbox to clear)",
            )
            return
        existing = await p.get_case(cmd.case_id)
        if existing is None:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                f"case-command(set-bbox): case {cmd.case_id!r} not found",
            )
            return
        new_bbox = None if clear else list(bbox)
        updated = existing.model_copy(
            update={"bbox": new_bbox, "updated_at": now_utc()}
        )
        try:
            await p.upsert_case(updated)
        except Exception as exc:  # noqa: BLE001
            logger.exception("case-command(set-bbox) upsert failed: %s", exc)
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                f"case set-bbox failed: {exc}",
            )
            return
        # Open case: refresh the durable in-session pin so the next turn's
        # AOI line + fetch-bbox snapping use the new extent immediately (a
        # CLEAR nulls it so the model re-derives the area from the prompt).
        if cmd.case_id == state.active_case_id:
            state.case_bbox = new_bbox
        await _emit_case_list(websocket, state, force=True)
        logger.info(
            "case-command set-bbox session=%s case=%s bbox=%s",
            state.session_id,
            cmd.case_id,
            new_bbox,
        )
        return

    if command == "archive":
        if not cmd.case_id:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                "case-command(archive) requires case_id",
            )
            return
        try:
            await p.archive_case(cmd.case_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("case-command(archive) failed: %s", exc)
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                f"case archive failed: {exc}",
            )
            return
        await _emit_case_list(websocket, state, force=True)
        logger.info(
            "case-command archive session=%s case=%s",
            state.session_id,
            cmd.case_id,
        )
        return

    if command == "delete":
        if not cmd.case_id:
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                "case-command(delete) requires case_id",
            )
            return
        try:
            await p.delete_case(cmd.case_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("case-command(delete) failed: %s", exc)
            await _send_error(
                websocket,
                state.session_id,
                "INTERNAL_ERROR",
                f"case delete failed: {exc}",
            )
            return
        # If the deleted Case was the active one, clear the context -- any
        # subsequent publish will fall through to the single-tenant default
        # rather than mutate a soft-deleted ``.qgs``.
        if state.active_case_id == cmd.case_id:
            state.active_case_id = None
            # Preserve pre-existing behavior on THIS connection (no
            # chat clear on delete); siblings re-sync on their next dispatch.
            state.case_context_synced_to = None
        await _emit_case_list(websocket, state, force=True)
        logger.info(
            "case-command delete session=%s case=%s",
            state.session_id,
            cmd.case_id,
        )
        return

    # Closed enum guard -- pydantic should have rejected before we got here.
    await _send_error(
        websocket,
        state.session_id,
        "INTERNAL_ERROR",
        f"unknown case-command: {command!r}",
    )

async def _maybe_autoname_case(state: SessionState, prompt: str) -> bool:
    """Name an 'Untitled Case' from its first user prompt.

    Accumulated untitled Cases are otherwise indistinguishable in the left
    rail. Best-effort, once per Case per process; never raises.
    """
    case_id = state.active_case_id
    if not case_id or case_id in _AUTONAMED_CASES:
        return False
    p = get_persistence()
    if p is None:
        # Persistence unbound is NOT a permanent state -- do NOT mark the case
        # "named" (a later turn, once bound, can still name it from its first
        # prompt). Marking it here would burn the one-and-only naming attempt on
        # any early miss (transient error / fresh-case read race).
        return False
    try:
        case = await p.get_case(case_id)
        if case is None:
            # Fresh case not visible in Persistence yet (create-then-read race)
            # -> TRANSIENT: leave unmarked so the next turn retries the name.
            return False
        if case.title != "Untitled Case":
            _AUTONAMED_CASES.add(case_id)  # already named -> stop checking
            return False
        title = _derive_case_title(prompt)
        if not title:
            # First prompt is degenerate/unnameable -> DEFINITIVE (the first
            # message defines the name); mark so later turns do not re-read.
            _AUTONAMED_CASES.add(case_id)
            return False
        await p.upsert_case(case.model_copy(update={"title": title}))
        _AUTONAMED_CASES.add(case_id)  # mark ONLY after the name actually landed
        logger.info("case auto-named case=%s title=%r", case_id, title)
        return True
    except Exception:  # noqa: BLE001 -- naming is a nicety; TRANSIENT -> unmarked,
        # so a persistence hiccup does not permanently forfeit the name.
        logger.debug("case auto-name failed case=%s", case_id, exc_info=True)
    return False

async def _auto_create_case_from_root(
    websocket: ServerConnection,
    state: SessionState,
    prompt: str,
) -> str | None:
    """Create + activate a Case for a chat prompt arriving with NO active Case.

    When a non-directive ``user-message`` arrives and the session has no
    active Case, mint one server-side BEFORE the turn dispatches so
    ``_persist_chat_turn`` + ``_persist_case_loaded_layers`` +
    ``ensure_case_qgs`` + the ``publish_layer`` case_id injection all land in
    it. The Case is named from the prompt via ``_derive_case_title``
    ("Untitled Case" fallback for degenerate prompts).

    Deliberately NOT the ``case-command(create)`` reset path: the in-flight
    message IS the Case's first turn, so the per-connection LLM context
    (``chat_history``) and the ``turn_count`` are left untouched.

    Returns the new ``case_id``, or ``None`` when Persistence is unbound or
    the upsert fails -- the stateless path keeps working either way.
    """
    p = get_persistence()
    if p is None:
        return None
    title = _derive_case_title(prompt) or "Untitled Case"
    now = now_utc()
    case = CaseSummary(
        case_id=new_ulid(),
        title=title,
        created_at=now,
        updated_at=now,
        status="active",
    )
    try:
        # Stamp the creator as owner so the auto-created Case is visible to
        # them via ``list_cases_for_user``. Cases are durable -- no TTL stamp.
        await p.upsert_case(
            case,
            owner_user_id=state.authenticated_user_id,
        )
    except Exception:  # noqa: BLE001 -- fall back to the stateless path
        logger.exception(
            "auto-create-case upsert failed session=%s", state.session_id
        )
        return None
    state.active_case_id = case.case_id
    # This connection's in-memory context IS the new Case's context (the
    # triggering message is its first turn) -- mark synced so the next
    # dispatch skips the _sync_case_context reset.
    state.case_context_synced_to = case.case_id
    # The creating prompt already named the Case -- skip the
    # first-turn rename probe (it would be a wasted get_case round-trip).
    _AUTONAMED_CASES.add(case.case_id)
    await _touch_session_record(state, case_id=case.case_id)  # session heartbeat
    # Fresh Case starts with zero layers -- flush the per-connection
    # accumulator (replace-not-reconcile server-side; mirrors
    # ``case-command(create)``).
    _ensure_emitter(websocket, state)
    if state.emitter is not None:
        state.emitter.reset_loaded_layers([])
    logger.info(
        "auto-created case from root session=%s case=%s title=%r",
        state.session_id,
        case.case_id,
        title,
    )
    return case.case_id

async def _emit_auto_case_open(
    websocket: ServerConnection,
    state: SessionState,
    case_id: str,
) -> None:
    """Emit ``case-open`` + ``case-list`` for an auto-created Case.

    Distinct from ``_emit_case_open``: NO context reset (no ``chat_history``
    clear, no ``turn_count`` reset, no emitter re-seed) -- the in-flight user
    message IS the first turn of this Case and
    ``_auto_create_case_from_root`` already established the connection
    context. Must be called AFTER the user turn is persisted so the
    rehydration payload carries it: Chat.tsx's case-open handler is
    replace-not-reconcile (it flushes the local message buffer and
    re-renders from ``session_state.chat_history``), so emitting before the
    persist would blank the just-typed message bubble. The client's ws.ts
    hub fans ``case-open`` out to App.tsx's socket (SESSION_SCOPED_TYPES),
    where ``useCases.onCaseOpen`` sets ``activeCaseId`` and the left rail
    flips from the Cases root into the Case view.

    A skipped (or ``session_state=None``) case-open on a rehydration
    failure would leave the client's ``activeCaseId`` unchanged -- stuck on
    the Cases root while the turn dispatches with the new case bound, so
    cards flow stamped with a ``case_id`` the client never opened and
    nothing renders until a reload. The rehydration-failure branch instead
    emits a MINIMAL non-null case-open whose ``session_state.case`` is the
    just-upserted ``CaseSummary`` (re-fetched, or a bare
    ``CaseSummary(case_id=...)`` if even that read fails).
    ``CaseSessionState`` only requires ``case`` (other fields default
    empty), so this guarantees the client flips out of the Cases root even
    when the richer rehydration momentarily fails.
    """
    p = get_persistence()
    if p is not None:
        try:
            payload = CaseOpenEnvelopePayload(
                session_state=await p.get_session_state(case_id)
            )
            await websocket.send(
                _new_envelope("case-open", state.session_id, payload)
            )
        except Exception:  # noqa: BLE001 -- emission is best-effort
            logger.exception(
                "auto-case-open emission failed session=%s case=%s",
                state.session_id,
                case_id,
            )
            # Fall back to a minimal non-null case-open so the
            # client still leaves the Cases root (never a null session_state).
            try:
                case = await p.get_case(case_id)
            except Exception:  # noqa: BLE001 -- re-fetch is best-effort
                case = None
            if case is None:
                # Last-resort minimal summary so session_state.case is non-null.
                now = now_utc()
                case = CaseSummary(
                    case_id=case_id,
                    title="Untitled Case",
                    created_at=now,
                    updated_at=now,
                    status="active",
                )
            try:
                fallback = CaseOpenEnvelopePayload(
                    session_state=CaseSessionState(case=case)
                )
                await websocket.send(
                    _new_envelope("case-open", state.session_id, fallback)
                )
            except Exception:  # noqa: BLE001 -- fallback emit is best-effort
                logger.exception(
                    "auto-case-open minimal fallback failed session=%s case=%s",
                    state.session_id,
                    case_id,
                )
    await _emit_case_list(websocket, state, force=True)
