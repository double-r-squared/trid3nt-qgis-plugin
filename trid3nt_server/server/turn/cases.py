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
    """Emit the ``case-list`` envelope, scoped to the handshake's user so a Case
    is visible only to its owner. Best-effort: the list is a derivable view and
    must never break the chat path."""
    # The default change-guard skips the send when the digest matches the last
    # emit for this SESSION, collapsing repeat keepalive resumes into a no-op; a
    # caller that may have mutated the list forces the send instead.
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
    """Refill the connection's history from a Case's PERSISTED messages, capped
    so a long Case cannot blow the context window; the state passed in belongs
    to exactly one Case, so this cannot leak across Cases."""
    try:
        # Pass the Case AOI bbox so the layers-present note carries the exact
        # extent: the note survives history capping, so a long Case whose head
        # turn named the place can still reuse the original AOI.
        case_bbox = getattr(getattr(session_state, "case", None), "bbox", None)
        history, dropped = rehydrate_history_from_case(
            session_state.chat_history,
            session_state.loaded_layers,
            case_bbox=case_bbox,
        )
        # REBIND rather than extend: a fresh object keeps an in-flight turn's
        # captured history untouched.
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
    Case, replacing its history and reseeding its emitter from the persisted
    Case, so dedup and the layer writes see the full persisted truth."""
    # History and the layer accumulator are per-connection, so a case command on
    # a sibling connection leaves them stale. Best-effort: a failed read leaves
    # the emitter seeded empty, and the merge on write keeps an unseeded
    # accumulator from clobbering the persisted layers.
    current = state.active_case_id
    if state.case_context_synced_to == current:
        return
    state.case_context_synced_to = current
    # Replace rather than reconcile: this connection's model context belongs to
    # whatever Case it was last driving. REBIND, never clear: an in-flight turn
    # holds the old list and must keep its own context intact.
    state.chat_history = []
    state.turn_count = 1  # count the in-flight turn that triggered the sync
    _ensure_emitter(websocket, state)
    if state.emitter is None:  # pragma: no cover -- _ensure_emitter always binds
        return
    if current is None:
        # No active Case means no AOI anchor.
        state.case_bbox = None
        state.emitter.reset_loaded_layers([])
        # No active Case means no resolvable handles either: clear whatever the
        # Case this connection last drove had registered.
        get_uri_registry(state.session_id).clear()
        return
    p = get_persistence()
    if p is None:
        state.emitter.reset_loaded_layers([])
        return
    try:
        session_state = await p.get_session_state(current)
        # Cache the Case AOI so this connection's turns have a durable anchor,
        # which is what feeds the reuse short-circuits and the per-turn note.
        _cache_case_bbox_from_session_state(state, session_state)
        state.emitter.reset_loaded_layers(session_state.loaded_layers)
        # Repopulate the inline-vector side-table, so this connection's next
        # session-state emission carries renderable vectors. Best-effort.
        try:
            await state.emitter.reinline_vector_layers()
        except Exception:  # noqa: BLE001
            logger.warning("case-context-sync vector re-inline failed")
        # Seed the URI registry from the persisted Case layers, so handle
        # indirection works for layers produced in PRIOR sessions of this Case:
        # the history was just cleared, and the registry is the only place the
        # id-to-uri association survives. It REPLACES rather than adds, because
        # this is a case-switch point and an additive seed would leak the
        # previous Case's handles into this one.
        await _seed_registry_for_case(
            state, current, session_state.loaded_layers
        )
        # Rehydrate this connection's model context from the SAME state already
        # fetched, never a second read: the clear above is the clean slate, and
        # refilling it lets a sibling or reconnect turn see prior work instead
        # of recomputing it.
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
    """Emit a ``case-open`` envelope hydrated from persistence, setting the
    active Case BEFORE the emit so later tool calls and chat writes carry it; a
    missing Case emits an empty state rather than failing."""
    state.active_case_id = case_id
    # This connection is about to run the full reset below, so recording the
    # sync marker lets its next message skip a redundant re-sync; a sibling
    # connection keeps its stale marker and catches up on its own dispatch.
    state.case_context_synced_to = case_id
    # A Case switch resets the per-connection conversation, not only the case
    # state: otherwise old turns keep reaching the model and prompts misroute to
    # the previous Case. The visible replay comes from the persisted history,
    # not this list. REBIND, never clear.
    state.chat_history = []
    state.turn_count = 0
    await _touch_session_record(state, case_id=case_id)  # session heartbeat
    # Persist the pointer on an explicit open or select, so the cold-start cache
    # is warm for a reconnect after a restart, even for a client that later
    # resumes with no Case stamp.
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
    # Cache the opened Case's AOI, so the very first turn in it already has the
    # anchor the reuse short-circuits and the per-turn note read.
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
    # launched, so a turn keyed to it that is still running gets its emitter
    # sink rebound onto this socket. Keyed by Case, so a concurrent solve
    # elsewhere is untouched.
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
        # Seed the URI registry from the SAME persisted layers the note
        # advertises: a fresh connection that opens an EXISTING Case reaches
        # here directly, and the registry is in-memory, so it would otherwise
        # start empty and the advertised handles would not resolve. It REPLACES
        # rather than adds, so a Case switch never leaks a prior Case's handles,
        # and it restores the persisted short-handle map.
        await _seed_registry_for_case(
            state, case_id, session_state.loaded_layers
        )
        # A persisted VECTOR layer carries no inline geometry, since the
        # side-table is in-memory, so the payload above rehydrated entries a
        # client cannot render. Re-inline from the artifact and emit one
        # follow-up session state so the vectors repaint.
        try:
            _reinlined = await state.emitter.reinline_vector_layers()
            if _reinlined:
                await state.emitter.emit_session_state()
        except Exception:  # noqa: BLE001 -- rehydration is best-effort
            logger.exception(
                "case-open vector re-inline failed case=%s", case_id
            )

    # Rehydrate the conversation from THIS Case's persisted messages, so a
    # follow-up turn in a reopened Case sees prior work instead of recomputing
    # it; the state above is already loaded and is never re-fetched.
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
    """Dispatch one Case lifecycle command - create, select, rename, archive or
    delete - persisting it and emitting the resulting open or list. A failure
    surfaces as an error envelope; none of these is a confirmation trigger."""
    # The client confirms a delete with the user before sending it, and the
    # server does not double-confirm.
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
        # An optional bbox lets the user pin the AOI extent BEFORE the first
        # prompt. It is coerced through the shared validator, so a malformed
        # value is dropped rather than crashing, and when present it persists on
        # the Case and seeds the in-session anchor, so the FIRST turn reuses the
        # user's extent instead of re-geocoding.
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
            # Stamp the creator as owner, so the Case is visible to them in the
            # listing. Cases are durable: no TTL stamp.
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
        # A fresh Case must NOT inherit the previous Case's AOI anchor, so the
        # reset happens BEFORE the conditional seed: a bbox-less create starts
        # with no anchor and the first prompt geocodes its own place name.
        state.case_bbox = None
        # Seed the anchor when the create carried a bbox, so the FIRST turn
        # returns the user's pre-set extent.
        if create_bbox is not None:
            state.case_bbox = list(create_bbox)
        # This connection is now synced to the new Case.
        state.case_context_synced_to = new_case_id
        # A fresh Case gets a fresh model context. REBIND, never clear.
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
        # A fresh Case starts with NO loaded layers, so the per-connection
        # accumulator is flushed and a later tool call cannot inherit layers
        # from the Case the user just left.
        _ensure_emitter(websocket, state)
        if state.emitter is not None:
            state.emitter.reset_loaded_layers([])
        # A fresh Case has no resolvable handles either: clear whatever the
        # previous Case registered.
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
        # Clear the cached Case AOI, so a root prompt - which auto-creates a
        # FRESH Case - does not reuse the just-exited Case's extent.
        state.case_bbox = None
        # REBIND, never clear.
        state.chat_history = []
        state.turn_count = 0
        if state.emitter is not None:
            state.emitter.reset_loaded_layers([])
        # No active Case means no resolvable handles from the just-exited Case
        # either.
        get_uri_registry(state.session_id).clear()
        # Clear the persisted pointer too, so a reconnect after a restart does
        # NOT re-seed the just-exited Case.
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
    """Name an untitled Case from its first user prompt, once per Case per
    process; best-effort and never raises."""
    case_id = state.active_case_id
    if not case_id or case_id in _AUTONAMED_CASES:
        return False
    p = get_persistence()
    if p is None:
        # Unbound persistence is NOT permanent, so the Case is left unmarked: a
        # later turn can still name it, and marking here would burn the single
        # naming attempt on a transient miss.
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
    """Mint and activate a Case for a prompt arriving with NO active Case,
    before the turn dispatches so every turn-scoped write lands in it; returns
    the new id, or ``None``, in which case the stateless path keeps working."""
    # Deliberately not the create-command reset path: the in-flight message IS
    # this Case's first turn, so the connection's context and turn count are
    # left untouched.
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
    """Emit the open and list for an auto-created Case, with NO context reset:
    the in-flight message IS this Case's first turn. Must run AFTER that user
    row is persisted, or the client's replace-on-open blanks the typed bubble."""
    # A skipped or null open would leave the client on the Cases root while the
    # turn dispatches into the new Case, so cards arrive stamped with a Case it
    # never opened. The failure branch therefore emits a MINIMAL non-null open
    # carrying just the Case summary, which is all the state model requires.
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
