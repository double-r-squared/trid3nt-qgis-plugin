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
    """Seed the reconnect emitter from the active Case's persisted layers.

    The bare-reconnect half of the per-Case layer DURABILITY requirement.
    Resolves the session's active Case and seeds this connection's emitter
    from the Case's persisted snapshot so the caller's single
    emit_session_state re-renders every already-rendered layer WITHOUT a
    case-open. Reuses the case-open / _sync_case_context rehydration seam.

    No-ops (never crashes) when there is no active Case or Persistence is
    unbound. Best-effort: a Persistence failure logs and leaves the emitter
    as-is so the resume still completes.
    """
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
        # JOB 2: restore the Case AOI anchor on a bare reconnect so a follow-up
        # turn after a WS blip reuses the original extent (no Case re-open).
        _cache_case_bbox_from_session_state(state, session_state)
        state.emitter.reset_loaded_layers(session_state.loaded_layers)
        # Repopulate the inline-GeoJSON side-table so the replayed
        # session-state carries renderable vectors (the browser never fetches
        # object-store uris directly). Mirrors the case-open path.
        try:
            await state.emitter.reinline_vector_layers()
        except Exception:  # noqa: BLE001 -- re-inline is best-effort
            logger.warning(
                "session-resume vector re-inline failed session=%s case=%s",
                state.session_id,
                case_id,
            )
        # #147 reconnect-resync: seed the emitter's chat-history mirror from the
        # SAME persisted CaseSessionState already fetched above (do NOT
        # re-fetch) so a BARE reconnect re-renders the chat bubbles too, not
        # just the layers. Persisted CaseChatMessage list is serialized to the
        # wire dict shape SessionStatePayload.chat_history carries. Best-effort.
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
        # Seed the URI registry so handle-indirection resolves for layers
        # produced in a PRIOR session of this Case. REPLACE (not
        # additive-seed) -- same rationale as the case-switch call sites, so a
        # bare reconnect never leaves stale/evicted records lingering.
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
    """Copy the resolved auth identity into the SessionState.

    Separate from ``_handle_auth_token`` so tests can drive the bind
    directly without parsing an envelope.
    """
    state.authenticated_user_id = result.user.user_id
    state.is_anonymous = result.is_anonymous
    state.auth_handshake_complete = True

async def _touch_session_record(
    state: SessionState, *, case_id: str | None = None
) -> None:
    """D.6 session-record heartbeat.

    Upserts the agent's own ``sessions`` document: ``last_active_at`` +
    ``expires_at`` advance (TTL driver per ``SESSIONS_TTL``), the active
    Case lands in ``project_ids``. Fired on auth bind, Case open/create,
    and every persisted chat turn -- none of these touches is a confirmable
    write (the session-record carveout).

    Best-effort: a persistence hiccup is logged at WARNING and never
    reaches the caller.
    """
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
    """Persist the session's active-Case pointer.

    Writes ``last_active_case_id`` onto the ``sessions`` document so the
    active pointer survives a process restart that wipes the
    in-memory ``_SESSION_ACTIVE_CASE`` dict. The client-stamped ``case_id``
    stays the REAL authority; this is only the cold-start cache. Fired
    whenever the server re-binds the pointer to the client's Case, so a
    later restart's fresh SessionState reloads the right Case (see
    ``_reload_session_active_case``).

    Best-effort: a persistence hiccup is logged at WARNING and never
    reaches the caller's turn.
    """
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
    """Reload the persisted active-Case pointer into the in-memory registry.

    When a fresh SessionState is built after a process restart (or a
    brand-new process), the session-scoped ``_SESSION_ACTIVE_CASE`` dict is
    empty. This reloads the persisted ``last_active_case_id`` so the
    server's pointer is warm again BEFORE the first replay/turn. The
    client-stamped ``case_id`` still wins on any disagreement; this only
    seeds a sensible default for a bare resume (older client, no stamp).

    Idempotent + guarded: only seeds when the registry has NO entry for
    this session yet (a value already present is the live truth and is
    never overwritten). Best-effort: a missing record / persistence hiccup
    leaves the pointer None.
    """
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

# --------------------------------------------------------------------------- #
# Case lifecycle handlers
# --------------------------------------------------------------------------- #

#: OPEN-8: the last-emitted case-list content digest PER SESSION (not
#: per connection -- SessionState is a fresh per-connection object, and
#: a session can carry more than one live socket). A session-resume
#: keepalive ping was re-serializing + re-sending the FULL case list
#: even when nothing had changed since the last emit. Cleared when the
#: session's last live connection disconnects so a later reconnect
#: always gets a fresh unconditional emit.
_SESSION_CASE_LIST_HASH: "dict[str, str]" = {}

def _case_list_digest(cases: "list[CaseSummary]") -> str:
    """Stable content digest of a case list, order-independent.

    Built from the fields a client actually renders/reacts to (id, title,
    status, timestamps) rather than a raw model dump, so field additions
    that don't change client-visible state don't force spurious re-emits.
    Sorted by ``case_id`` so the digest is independent of listing order.
    """
    parts = sorted(
        f"{c.case_id}|{c.title}|{c.status}|{c.created_at}|{c.updated_at}"
        for c in cases
    )
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()

def _clear_case_list_hash(session_id: str) -> None:
    """Drop the cached case-list digest for ``session_id`` (best-effort).

    Called once the session's last live connection disconnects so a fresh
    reconnect later always gets an unconditional first emit rather than
    inheriting a stale digest from a prior connection's cache.
    """
    _SESSION_CASE_LIST_HASH.pop(session_id, None)

#: Cases already auto-named this process (avoid a get_case read
#: on every user turn -- only the first turn per Case checks the title).
_AUTONAMED_CASES: set[str] = set()

_TITLE_STOPWORDS = frozenset(
    "a an the and or of for with to in on at by from using use run model "
    "show me my please can you what how is are this that".split()
)

def _derive_case_title(prompt: str) -> str | None:
    """Heuristic 3-6 word Case title from the first user prompt.

    Significant tokens, title-cased, capped at ~48 chars. Returns None for
    degenerate prompts.
    """
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
    """The Case the current turn is bound to.

    Prefers the pin set by ``_prepare_user_turn`` at dispatch time; falls
    back to the live ``active_case_id`` for callers outside a prepared turn
    (direct tool invocations in tests, legacy paths). Without the pin, every
    persistence site reading ``active_case_id`` at WRITE time lets a
    ``case-command(select)`` arriving mid-stream re-aim in-flight writes at
    the newly selected Case.
    """
    return state.current_turn_case_id or state.active_case_id

def _turn_case_bbox(state: SessionState) -> Any:
    """The current turn's Case AOI bbox, or None.

    Used by the expensive-simulation reuse guard AND the fetch reuse guard as
    the AOI anchor when a request / persistence-seeded layer has no recorded
    bbox: a bbox-keyed re-run (or a bare follow-up fetch) in a single-result
    Case whose request bbox equals the Case AOI is a clear match.

    Reads ``state.case_bbox`` -- the durable cache of the active Case's
    persisted ``CaseSummary.bbox`` (set on case select / sync).
    """
    case_id = _turn_case_id(state)
    if not case_id:
        return None
    return state.case_bbox

def _cache_case_bbox_from_session_state(
    state: SessionState, session_state: Any
) -> None:
    """Cache the active Case's AOI bbox onto ``state.case_bbox``.

    Reads ``session_state.case.bbox`` -- the persisted ``CaseSummary.bbox``
    that the layers-present note already consumes -- and stores it so
    ``_turn_case_bbox`` has a durable active-AOI anchor on every live turn
    (the reuse short-circuits + the per-turn [Case state] note both read
    it). Pydantic BBox models serialize to a plain list; coerced to a list
    so the value is a cheap, JSON-shaped ``[lon_min, lat_min, lon_max,
    lat_max]`` (or ``None``). Best-effort: a missing / malformed case leaves
    the cache untouched-to-None.
    """
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
    """Persist the session registry's short-handle map to the Case.

    Writes the ``{L<n>: uri}`` map as a storage-only ``layer_handles`` field
    on the cases doc (see ``Persistence.set_case_layer_handles``) so a
    reconnect / Case reopen (``_seed_registry_for_case``) restores the exact
    handles the LLM has already been shown. Skips when nothing new was
    minted since the last write (``shorts_dirty``). Best-effort: any failure
    is logged and swallowed -- the dispatch is never broken, and the registry
    stays dirty so the next dispatch retries the write.
    """
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
    """Reset the URI registry to a Case AND restore its handle map.

    The single reseed path for every case-open / case-switch / resume call
    site: replace-not-merge from the Case's persisted ``loaded_layers`` (the
    F32 contract), importing the Case's persisted ``{L<n>: uri}`` map FIRST
    so already-announced short handles keep their numbers and fresh layers
    mint past the persisted maximum. Best-effort on the persistence read --
    a hiccup degrades to fresh minting (stale L<n> references then reject
    typed with the current inventory, which is honest and retryable).
    """
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
    """Bind/clear the session's active canvas AOI.

    Called when a ``user-message`` payload carries the ``aoi_bbox`` key
    (``[min_lon, min_lat, max_lon, max_lat]`` EPSG:4326, ``None`` when no AOI
    is drawn). A valid bbox sets the active AOI; an explicit ``None`` clears
    it; a malformed value is logged and ignored (never blocks the turn, never
    clobbers a good AOI with garbage).
    """
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
    """Bind/clear the turn's user-drawn geometry.

    Called when a ``user-message`` payload carries the ``drawn_geometry`` key
    (``{"geometry_type": "rectangle", "bbox": [min_lon, min_lat, max_lon,
    max_lat]}`` EPSG:4326, ``None`` when nothing is drawn). A valid rectangle
    sets it; an explicit ``None`` clears it; a malformed value is logged and
    ignored (never blocks the turn). Stored as a plain dict; the turn dispatcher
    binds it into ``bind_turn_drawn_geometry`` so composer gates consume it.
    """
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
    """Sync the emitter's ``_loaded_layers`` onto the turn's ``CaseSummary``.

    Writes the current ``ProjectLayerSummary[]`` accumulator into
    ``Case.loaded_layer_summaries`` (full dicts for rehydration) and keeps
    ``Case.layer_summary`` (the lightweight ``layer_id[]`` projection) in
    lockstep. Idempotent and dedup-by-uri because the emitter already dedups
    upstream.

    Best-effort: a Persistence failure is logged but never raised. The Case
    lookup gates the write -- an archived/deleted Case is silently skipped
    (no surprise resurrection via this side-channel).

    ``case_id`` pins the target Case explicitly (callers inside a tool
    dispatch pass their entry-time capture); default resolves via
    ``_turn_case_id`` so a mid-turn Case switch never re-aims attribution.
    """
    target_case = case_id if case_id is not None else _turn_case_id(state)
    p = get_persistence()
    if p is None or state.emitter is None or not target_case:
        return
    try:
        case = await p.get_case(target_case)
    except Exception:  # noqa: BLE001
        logger.exception(
            "case-layer-persist: get_case failed case=%s",
            target_case,
        )
        return
    if case is None:
        logger.debug(
            "case-layer-persist: case=%s missing; skipping",
            target_case,
        )
        return

    loaded = state.emitter.loaded_layers  # defensive copy from the emitter
    emitter_dicts: list[dict] = [layer.model_dump(mode="json") for layer in loaded]

    # MERGE (append + replace-by-layer_id) instead of wholesale replace: an
    # emitter never seeded with the Case's persisted layers (fresh
    # connection, sync failure, sibling-socket dispatch) must never CLOBBER
    # previously persisted summaries down to its own partial view -- union
    # them, with the emitter's fresher entry winning on a layer_id collision.
    merged: list[dict] = [
        dict(d) for d in case.loaded_layer_summaries if isinstance(d, dict)
    ]

    index_by_layer_id = {
        d.get("layer_id"): i for i, d in enumerate(merged) if d.get("layer_id")
    }
    for d in emitter_dicts:
        lid = d.get("layer_id")
        pos = index_by_layer_id.get(lid)
        if pos is None:
            index_by_layer_id[lid] = len(merged)
            merged.append(d)
        else:
            merged[pos] = d
    layer_ids: list[str] = [
        d.get("layer_id") for d in merged if isinstance(d.get("layer_id"), str)
    ]

    # If nothing has changed, skip the round-trip.
    if (
        case.loaded_layer_summaries == merged
        and case.layer_summary == layer_ids
    ):
        return

    updated = case.model_copy(
        update={
            "loaded_layer_summaries": merged,
            "layer_summary": layer_ids,
            "updated_at": now_utc(),
        }
    )
    try:
        await p.upsert_case(updated)
        logger.debug(
            "case-layer-persist case=%s layers=%d",
            target_case,
            len(layer_ids),
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "case-layer-persist: upsert failed case=%s",
            target_case,
        )

async def _delete_case_loaded_layer(
    state: SessionState, layer_id: str, *, case_id: str | None = None
) -> None:
    """Persist a layer deletion AUTHORITATIVELY (replace, not union).

    Mirrors the in-memory emitter's drop of ``layer_id`` from
    ``_loaded_layers`` onto the persisted ``CaseSummary`` so it cannot
    RESURRECT on the next turn or a Case reopen.

    Deliberately bypasses ``_persist_case_loaded_layers`` (that path UNIONs
    the emitter view with ``case.loaded_layer_summaries``, which would re-add
    the deleted layer). Here we REMOVE ``layer_id`` from both
    ``loaded_layer_summaries`` and ``layer_summary`` and write the result.

    Best-effort: a Persistence failure is logged but never raised; a missing
    / tombstoned Case is silently skipped. ``case_id`` pins the target Case
    explicitly; default resolves via ``_turn_case_id`` (never the raw live
    ``active_case_id``).
    """
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
