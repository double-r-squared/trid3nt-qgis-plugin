"""Session/case state: active-case persistence, AOI/geometry payload, case-layer records."""

from __future__ import annotations

import hashlib
import math
import logging
from trid3nt_contracts import now_utc
from trid3nt_server.credentials.auth_handshake import AuthResult
from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
from trid3nt_server.emission.uri_registry import get_uri_registry
from trid3nt_server.server.session.persistence_ref import get_persistence
from trid3nt_server.server.session.state import SessionState, _SESSION_ACTIVE_CASE, _set_session_active_case
from typing import Any

logger = logging.getLogger("trid3nt_server.server")

async def _replay_active_case_layers(state: SessionState) -> None:
    """Seed this connection's emitter from the active Case's persisted layers,
    so the caller's single session-state emit re-renders every already-rendered
    layer with no case-open. Best-effort: the resume completes regardless."""
    if state.emitter is None:  # pragma: no cover -- _ensure_emitter always binds
        return
    case_id = state.active_case_id
    if case_id is None:
        return
    p = get_persistence()
    if p is None:
        return
    try:
        session_state = await p.get_session_state(case_id)
        # Restore the Case AOI anchor on a bare reconnect, so a follow-up turn
        # after a socket blip reuses the original extent with no re-open.
        _cache_case_bbox_from_session_state(state, session_state)
        state.emitter.reset_loaded_layers(session_state.loaded_layers)
        # Repopulate the inline-vector side-table so the replayed session state
        # carries renderable vectors; a client never fetches object-store uris
        # itself.
        try:
            await state.emitter.reinline_vector_layers()
        except Exception:  # noqa: BLE001 -- re-inline is best-effort
            logger.warning(
                "session-resume vector re-inline failed session=%s case=%s",
                state.session_id,
                case_id,
            )
        # Seed the emitter's chat mirror from the SAME state already fetched
        # above, never a second read, so a bare reconnect re-renders the chat
        # too and not only the layers. Best-effort.
        try:
            state.emitter.seed_chat_history(
                [m.model_dump(mode="json") for m in session_state.chat_history]
            )
        except Exception:  # noqa: BLE001 -- chat seed is best-effort
            logger.warning(
                "session-resume chat-history seed failed session=%s case=%s",
                state.session_id,
                case_id,
            )
        # Seed the URI registry so handle indirection resolves for layers
        # produced in a PRIOR session of this Case. It REPLACES rather than
        # adds, so a reconnect never leaves stale records lingering.
        await _seed_registry_for_case(
            state, case_id, session_state.loaded_layers
        )
        logger.info(
            "session-resume replayed active-case layers session=%s case=%s "
            "layers=%d",
            state.session_id,
            case_id,
            len(session_state.loaded_layers),
        )
    except Exception:  # noqa: BLE001 -- best-effort, never break the resume
        logger.exception(
            "session-resume layer replay failed session=%s case=%s",
            state.session_id,
            case_id,
        )

def _bind_auth_result(state: SessionState, result: AuthResult) -> None:
    """Copy the resolved auth identity into the SessionState."""
    state.authenticated_user_id = result.user.user_id
    state.is_anonymous = result.is_anonymous
    state.auth_handshake_complete = True

async def _touch_session_record(
    state: SessionState, *, case_id: str | None = None
) -> None:
    """Heartbeat the session record: the activity stamps advance and the active
    Case is recorded. Best-effort - a persistence hiccup never reaches the
    caller - and never a confirmable write."""
    p = get_persistence()
    if p is None:
        return
    active_case_id = case_id if case_id is not None else state.active_case_id
    try:
        await p.touch_session(
            state.session_id,
            case_id=active_case_id,
        )
    except Exception:  # noqa: BLE001 -- side effect, never bubble up
        logger.warning(
            "session-touch failed session=%s", state.session_id, exc_info=True
        )

async def _persist_session_active_case(
    state: SessionState, case_id: str | None
) -> None:
    """Persist the session's active-Case pointer so it survives a restart that
    wipes the in-memory registry; the client's stamp stays the authority and
    this is only the cold-start cache. Best-effort."""
    p = get_persistence()
    if p is None:
        return
    try:
        await p.set_session_active_case(state.session_id, case_id)
    except Exception:  # noqa: BLE001 -- side effect, never bubble up
        logger.warning(
            "persist active-case pointer failed session=%s",
            state.session_id,
            exc_info=True,
        )

async def _reload_session_active_case(state: SessionState) -> None:
    """Warm the in-memory pointer from the persisted one before the first replay
    or turn, only when the registry has NO entry for this session yet: a value
    already there is the live truth. A client stamp still wins on disagreement."""
    if state.session_id in _SESSION_ACTIVE_CASE:
        return
    p = get_persistence()
    if p is None:
        return
    try:
        persisted = await p.get_session_active_case(state.session_id)
    except Exception:  # noqa: BLE001 -- best-effort, never break resume
        logger.warning(
            "reload active-case pointer failed session=%s",
            state.session_id,
            exc_info=True,
        )
        return
    if persisted is not None and state.session_id not in _SESSION_ACTIVE_CASE:
        _set_session_active_case(state.session_id, persisted)
        logger.info(
            "reloaded persisted active case session=%s case=%s",
            state.session_id,
            persisted,
        )


#: The last-emitted case-list digest PER SESSION, not per connection, since a
#: session can carry more than one live socket: without it a keepalive ping
#: re-serializes and re-sends the whole list unchanged. Cleared when the
#: session's last connection disconnects, so a later reconnect emits
#: unconditionally.
_SESSION_CASE_LIST_HASH: "dict[str, str]" = {}

def _case_list_digest(cases: "list[CaseSummary]") -> str:
    """Stable, order-independent digest of a case list, over the fields a client
    renders rather than a raw model dump, so a field addition that changes
    nothing visible does not force a re-emit."""
    parts = sorted(
        f"{c.case_id}|{c.title}|{c.status}|{c.created_at}|{c.updated_at}"
        for c in cases
    )
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()

def _clear_case_list_hash(session_id: str) -> None:
    """Drop the cached case-list digest for ``session_id``, so a later reconnect
    emits unconditionally rather than inheriting a stale digest."""
    _SESSION_CASE_LIST_HASH.pop(session_id, None)

#: Cases already auto-named this process, so only the first turn per Case pays
#: a title read.
_AUTONAMED_CASES: set[str] = set()

_TITLE_STOPWORDS = frozenset(
    "a an the and or of for with to in on at by from using use run model "
    "show me my please can you what how is are this that".split()
)

def _derive_case_title(prompt: str) -> str | None:
    """A heuristic short Case title from the first user prompt: significant
    tokens, title-cased and capped; ``None`` for a degenerate prompt."""
    words = [
        w.strip(".,!?:;()[]\"'")
        for w in prompt.split()
    ]
    keep = [
        w for w in words if w and w.lower() not in _TITLE_STOPWORDS
    ][:6]
    if len(keep) < 2:
        return None
    title = " ".join(w if w[:1].isupper() else w.capitalize() for w in keep)
    return title[:48].rstrip() or None

def _turn_case_id(state: SessionState) -> str | None:
    """The Case the current turn is bound to: the dispatch-time pin, else the
    live pointer for a caller outside a prepared turn."""
    # Reading the live pointer at WRITE time would let a select arriving
    # mid-stream re-aim in-flight writes at the newly selected Case.
    return state.current_turn_case_id or state.active_case_id

def _turn_case_bbox(state: SessionState) -> Any:
    """The current turn's Case AOI bbox, or ``None``: the durable cache of the
    active Case's persisted bbox, which the reuse guards use as the AOI anchor
    when a request or seeded layer has no recorded bbox of its own."""
    case_id = _turn_case_id(state)
    if not case_id:
        return None
    return state.case_bbox

def _cache_case_bbox_from_session_state(
    state: SessionState, session_state: Any
) -> None:
    """Cache the active Case's persisted AOI bbox as a plain list, so every live
    turn has a durable AOI anchor; a missing or malformed case caches ``None``
    rather than raising."""
    try:
        case = getattr(session_state, "case", None)
        bbox = getattr(case, "bbox", None) if case is not None else None
        if bbox is None:
            state.case_bbox = None
            return
        state.case_bbox = list(bbox)
    except Exception:  # noqa: BLE001 -- best-effort cache, never break the turn
        state.case_bbox = None

async def _persist_case_layer_handles(
    state: SessionState, *, case_id: str | None
) -> None:
    """Persist the session registry's short-handle map onto the Case, so a
    reconnect restores the exact handles the model has already been shown.
    Skipped when nothing new was minted; a failure leaves the map dirty."""
    if not case_id:
        return
    reg = get_uri_registry(state.session_id)
    if not reg.shorts_dirty:
        return
    p = get_persistence()
    if p is None:
        return
    try:
        await p.set_case_layer_handles(case_id, reg.export_short_handles())
        reg.mark_shorts_persisted()
    except Exception:  # noqa: BLE001 -- best-effort, never break the dispatch
        logger.exception(
            "case layer-handle persist failed case=%s", case_id
        )

async def _seed_registry_for_case(
    state: SessionState, case_id: str | None, loaded_layers: Any
) -> None:
    """Reset the URI registry to a Case and restore its handle map: the one
    reseed path for every open, switch and resume. It REPLACES rather than
    merges, so announced handles keep their numbers."""
    # A failed read degrades to fresh minting, and a stale handle then draws a
    # typed rejection listing the current inventory - honest and retryable.
    reg = get_uri_registry(state.session_id)
    persisted: dict[str, str] | None = None
    p = get_persistence()
    if p is not None and case_id:
        try:
            persisted = await p.get_case_layer_handles(case_id)
        except Exception:  # noqa: BLE001 -- degrade to fresh minting
            logger.warning(
                "case layer-handle map read failed case=%s (fresh mint)",
                case_id,
                exc_info=True,
            )
    reg.replace_from_layers(loaded_layers, short_handles=persisted)

def _set_active_aoi_from_payload(state: SessionState, raw: Any) -> None:
    """Bind or clear the session's active canvas AOI: a valid bbox sets it, an
    explicit ``None`` clears it, and a malformed value is logged and ignored
    rather than clobbering a good AOI."""
    if raw is None:
        if state.active_aoi_bbox is not None:
            logger.info(
                "active-aoi cleared session=%s", state.session_id
            )
        state.active_aoi_bbox = None
        return
    coerced = coerce_bbox_value(raw)
    if (
        coerced is None
        or not all(math.isfinite(v) for v in coerced)
        or not (coerced[0] < coerced[2] and coerced[1] < coerced[3])
    ):
        logger.warning(
            "active-aoi ignoring malformed aoi_bbox=%r session=%s",
            raw,
            state.session_id,
        )
        return
    state.active_aoi_bbox = coerced
    logger.info(
        "active-aoi set session=%s bbox=%s", state.session_id, coerced
    )

def _set_drawn_geometry_from_payload(state: SessionState, raw: Any) -> None:
    """Bind or clear the turn's user-drawn geometry: a valid rectangle sets it,
    an explicit ``None`` clears it, and a malformed value is logged and ignored.
    Stored as a plain dict for the turn dispatcher to bind."""
    if raw is None:
        state.drawn_geometry = None
        return
    if not isinstance(raw, dict):
        logger.warning(
            "drawn-geometry ignoring non-dict payload=%r session=%s",
            raw, state.session_id,
        )
        return
    coerced = coerce_bbox_value(raw.get("bbox"))
    if (
        coerced is None
        or not all(math.isfinite(v) for v in coerced)
        or not (coerced[0] < coerced[2] and coerced[1] < coerced[3])
    ):
        logger.warning(
            "drawn-geometry ignoring malformed bbox in %r session=%s",
            raw, state.session_id,
        )
        return
    gtype = str(raw.get("geometry_type") or "rectangle")
    state.drawn_geometry = {"geometry_type": gtype, "bbox": list(coerced)}
    logger.info(
        "drawn-geometry set session=%s type=%s bbox=%s",
        state.session_id, gtype, coerced,
    )

async def _persist_case_loaded_layers(
    state: SessionState, *, case_id: str | None = None
) -> None:
    """Sync the emitter's layer accumulator onto the turn's Case. Best-effort,
    and an archived or deleted Case is skipped rather than resurrected."""
    # ``case_id`` pins the target Case explicitly - a caller inside a dispatch
    # passes its entry-time capture - so a mid-turn switch never re-aims the
    # attribution.
    target_case = case_id if case_id is not None else _turn_case_id(state)
    p = get_persistence()
    if p is None or state.emitter is None or not target_case:
        return
    summaries = [layer.model_dump(mode="json") for layer in state.emitter.loaded_layers]
    try:
        found = await p.merge_case_layers(target_case, summaries)
    except Exception:  # noqa: BLE001
        logger.exception("case-layer-persist: failed case=%s", target_case)
        return
    if not found:
        logger.debug("case-layer-persist: case=%s missing; skipping", target_case)
        return
    logger.debug(
        "case-layer-persist case=%s layers=%d", target_case, len(summaries)
    )

async def _delete_case_loaded_layer(
    state: SessionState, layer_id: str, *, case_id: str | None = None
) -> None:
    """Persist a layer deletion AUTHORITATIVELY, removing the layer from both
    the persisted summaries and their projection, so it cannot resurrect on the
    next turn or a reopen. Best-effort; a tombstoned Case is skipped."""
    # It deliberately bypasses the sync path above, which UNIONs the emitter
    # view with the persisted summaries and would re-add the deleted layer.
    target_case = case_id if case_id is not None else _turn_case_id(state)
    p = get_persistence()
    if p is None or not target_case:
        return
    try:
        case = await p.get_case(target_case)
    except Exception:  # noqa: BLE001
        logger.exception(
            "layer-delete-persist: get_case failed case=%s", target_case
        )
        return
    if case is None:
        logger.debug(
            "layer-delete-persist: case=%s missing; skipping", target_case
        )
        return

    surviving_summaries: list[dict] = [
        dict(d)
        for d in case.loaded_layer_summaries
        if isinstance(d, dict) and d.get("layer_id") != layer_id
    ]
    surviving_ids: list[str] = [
        d.get("layer_id")
        for d in surviving_summaries
        if isinstance(d.get("layer_id"), str)
    ]

    # Nothing referenced this layer_id in the persisted set -- no write needed.
    if (
        case.loaded_layer_summaries == surviving_summaries
        and case.layer_summary == surviving_ids
    ):
        return

    updated = case.model_copy(
        update={
            "loaded_layer_summaries": surviving_summaries,
            "layer_summary": surviving_ids,
            "updated_at": now_utc(),
        }
    )
    try:
        await p.upsert_case(updated)
        logger.debug(
            "layer-delete-persist case=%s layer=%s remaining=%d",
            target_case,
            layer_id,
            len(surviving_ids),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "layer-delete-persist: upsert failed case=%s layer=%s",
            target_case,
            layer_id,
        )
