"""Case-AOI pinning from solves/tool bboxes + AOI-default backfill for fetch/solver args."""

from __future__ import annotations

import logging
from trid3nt_contracts import now_utc
from trid3nt_server.scenario_reuse import bbox_encloses, bbox_equivalent, fetched_kind_for_tool, scenario_type_for_tool
from trid3nt_server.server.session.persistence_ref import get_persistence
from trid3nt_server.server.session.state import SessionState
from trid3nt_server.server.spatial import _coerce_bbox4
from typing import Any

logger = logging.getLogger("trid3nt_server.server")

# The AOI is PINNED to the solve domain: the authoritative extent IS the
# solve domain (the peak depth / mesh LayerURI bbox the workflow already
# floors + stamps), not a freehand bbox re-derived per follow-up tool call.


def _scenario_produces_domain(tool_name: str) -> bool:
    """True when ``tool_name`` is an expensive solver whose result LayerURI bbox
    is the authoritative AOI to pin (the solver's domain extent).

    Any tool ``scenario_type_for_tool`` recognizes mints a domain-extent layer
    (flood-depth peak / plume) -- the SAME extent ``compute_layer_bounds`` returns
    for the produced handle. Reuses that taxonomy so a new solver auto-pins.
    """
    return scenario_type_for_tool(tool_name) is not None

async def _pin_case_aoi_from_solve(
    state: SessionState,
    *,
    case_id: str | None,
    bbox: Any,
) -> None:
    """Persist a completed solve's domain ``bbox`` as the Case AOI.

    Writes ``CaseSummary.bbox`` via ``upsert_case`` AND updates the durable
    in-session cache ``state.case_bbox`` so ``_turn_case_bbox`` returns the
    pinned extent for the rest of THIS session (every follow-up fetch
    defaults to it) and a later Case reopen rehydrates the SAME AOI from
    persistence.

    Best-effort: a missing/tombstoned Case or a Persistence hiccup is logged
    and never raised -- pinning is a side-effect, not the solve's happy
    path. Idempotent: a re-run at the SAME extent skips the round-trip (the
    persisted value already matches, within the bbox quantization
    tolerance).
    """
    coerced = _coerce_bbox4(bbox)
    if coerced is None or not case_id:
        return
    # Update the in-session anchor first -- it drives the fetch default below even
    # if the persistence write fails.
    state.case_bbox = list(coerced)
    p = get_persistence()
    if p is None:
        return
    try:
        case = await p.get_case(case_id)
    except Exception:  # noqa: BLE001 -- best-effort, never break the turn
        logger.exception("aoi-pin: get_case failed case=%s", case_id)
        return
    if case is None:
        logger.debug("aoi-pin: case=%s missing; skipping pin", case_id)
        return
    # Idempotent: skip the write when the persisted AOI already equals the solve
    # domain (a re-run at the same extent, or a second domain-producing tool).
    if case.bbox is not None and bbox_equivalent(list(case.bbox), list(coerced)):
        return
    updated = case.model_copy(
        update={"bbox": list(coerced), "updated_at": now_utc()}
    )
    try:
        await p.upsert_case(updated)
        logger.info(
            "aoi-pin: pinned Case AOI case=%s bbox=%s (solve domain)",
            case_id,
            list(coerced),
        )
    except Exception:  # noqa: BLE001 -- best-effort, never break the turn
        logger.exception("aoi-pin: upsert failed case=%s", case_id)

def _bbox_round6(bbox: Any) -> tuple[float, float, float, float] | None:
    """Round a coerced 4-tuple bbox to 6 decimal places (~0.11 m at the
    equator) for a TIGHT change-detection comparison.

    Used only by ``_pin_case_aoi_from_tool_bbox``'s durable-write debounce --
    deliberately much tighter than the coarse ~2 km ``_BBOX_QUANT_DEG``
    scenario-reuse quant (``bbox_equivalent``'s default): that quant is
    "close enough to be the same run", whereas here we only want to skip a
    literally-repeated bbox, not silently drop a real (if small) AOI move.
    Returns ``None`` for a missing / malformed bbox.
    """
    coerced = _coerce_bbox4(bbox)
    if coerced is None:
        return None
    return (
        round(coerced[0], 6),
        round(coerced[1], 6),
        round(coerced[2], 6),
        round(coerced[3], 6),
    )

async def _pin_case_aoi_from_tool_bbox(
    state: SessionState,
    *,
    case_id: str | None,
    tool_name: str,
    params: dict,
) -> None:
    """Durably anchor the Case AOI from an ordinary bbox-taking FETCH call.

    Complements ``_pin_case_aoi_from_solve`` (above), which only fires for a
    domain-producing SOLVER -- a Case whose
    activity so far is plain fetches (``fetch_dem``, ``fetch_landcover``,
    ...) would otherwise never get an AOI anchor, leaving
    ``build_layers_present_note`` with no AOI line for a follow-up prompt to
    resolve against.

    Fires ONLY for recognized bbox-taking fetchers (``fetched_kind_for_tool``);
    domain-producing solvers are explicitly excluded -- they keep their own
    post-RESULT pin from the FLOORED solve-domain bbox
    (``_pin_case_aoi_from_solve``), which must win over a pre-solve REQUEST
    bbox. Called AFTER both AOI reuse guards have already read
    ``_turn_case_bbox`` for THIS dispatch (so it never perturbs this call's
    own reuse comparison) and AFTER ``_maybe_default_fetch_bbox_to_pinned_aoi``
    has already snapped a same-area drifted/narrower box onto any existing
    pin -- so this call can only WIDEN (an explicit enclose), MOVE (a
    disjoint bbox = a genuinely different place -- latest-wins, matching the
    solve-pin's unconditional overwrite semantics), or -- the common case --
    SEED (no pin yet) the anchor. It can never silently shrink an
    already-established AOI.

    Latest-wins in-session: ``state.case_bbox`` is set unconditionally (once
    a valid bbox is present) so the persisted Case row and the in-session
    cache stay in lockstep (the invariant: ``_turn_case_bbox`` at turn end
    == ``CaseSummary.bbox``). The durable Persistence write is debounced on
    a tight 6-decimal-place comparison (``_bbox_round6``, NOT the coarse
    scenario-reuse quant) so a repeated identical bbox never round-trips
    Persistence twice. Best-effort and silent: never raises, never blocks
    the turn -- a missing active Case, an unbound Persistence, or a
    Persistence hiccup just skips the write (existing bbox-less Cases
    self-heal on their NEXT turn with any bbox-carrying fetch).
    """
    if fetched_kind_for_tool(tool_name) is None:
        return
    if _scenario_produces_domain(tool_name):
        return  # solves are pinned post-result from the floored domain bbox
    if not case_id:
        return
    coerced = _coerce_bbox4(params.get("bbox"))
    if coerced is None:
        return
    # Latest-wins: always refresh the in-session anchor first, mirroring
    # _pin_case_aoi_from_solve -- the durable write below is best-effort and
    # may legitimately no-op (debounce) or fail without undoing this.
    state.case_bbox = list(coerced)
    p = get_persistence()
    if p is None:
        return
    try:
        case = await p.get_case(case_id)
    except Exception:  # noqa: BLE001 -- best-effort, never break the turn
        logger.exception("aoi-pin[fetch]: get_case failed case=%s", case_id)
        return
    if case is None:
        logger.debug("aoi-pin[fetch]: case=%s missing; skipping pin", case_id)
        return
    if _bbox_round6(case.bbox) == _bbox_round6(coerced):
        return  # debounce: the persisted AOI already matches this exact bbox
    updated = case.model_copy(
        update={"bbox": list(coerced), "updated_at": now_utc()}
    )
    try:
        await p.upsert_case(updated)
        logger.info(
            "aoi-pin[fetch]: pinned Case AOI case=%s bbox=%s (tool=%s)",
            case_id,
            list(coerced),
            tool_name,
        )
    except Exception:  # noqa: BLE001 -- best-effort, never break the turn
        logger.exception("aoi-pin[fetch]: upsert failed case=%s", case_id)

def _bbox_overlaps(a: Any, b: Any) -> bool:
    """True iff two WGS84 bboxes have a non-empty intersection (LANE-C helper).

    Used by the fetch-default rule to distinguish a DRIFTED box targeting the
    pinned AOI (overlaps -> snap to the pin) from a genuinely DIFFERENT place
    (disjoint -> honor the LLM's box). Touching-edge counts as overlap.
    """
    pa = _coerce_bbox4(a)
    pb = _coerce_bbox4(b)
    if pa is None or pb is None:
        return False
    return pa[0] <= pb[2] and pb[0] <= pa[2] and pa[1] <= pb[3] and pb[1] <= pa[3]

#: Near-exact tolerance (deg) for the fetch-default snap decision. Deliberately
#: MUCH tighter than the coarse ~2 km ``_BBOX_QUANT_DEG`` scenario-reuse quant so a
#: same-area-but-drifted box (the live ~0.005-0.01 deg under-coverage) is snapped
#: to the pin rather than waved through as "equivalent". ~1.1 m at the equator.
_AOI_DEFAULT_EQ_TOL_DEG = 1e-5

def _maybe_default_fetch_bbox_to_pinned_aoi(
    tool_name: str,
    params: dict,
    pinned_bbox: Any,
) -> dict:
    """Default a bbox-taking fetch tool to the pinned Case AOI.

    The LLM free-hands a fresh (and usually NARROWER) bbox for every
    follow-up fetch even when it means "the same area I just modeled". When
    a domain has been pinned (``state.case_bbox`` set by a solve), force
    follow-up fetches onto that SAME extent so all layers cover the AOI by
    construction.

    PRECISE RULE (honor "a different place", fix "the same place, drifted box"):
      * Only applies to recognized bbox-taking fetchers (``fetched_kind_for_tool``).
      * No pinned AOI -> no-op (returns ``params`` unchanged).
      * No / invalid ``bbox`` supplied (bare follow-up) -> inject the pin.
      * Supplied bbox that OVERLAPS the pin but does NOT already enclose it (a
        narrower / drifted box for the same area) -> REPLACE with the pin.
      * Supplied bbox that already ENCLOSES the pin (an explicit larger area) ->
        HONOR it (the user asked to widen).
      * Supplied bbox DISJOINT from the pin (a genuinely different place) ->
        HONOR it (do not drag the new area back to the old AOI).

    Pure + conservative: returns a NEW dict only when it changes ``bbox``; never
    mutates the input dict in place.
    """
    if fetched_kind_for_tool(tool_name) is None:
        return params
    pin = _coerce_bbox4(pinned_bbox)
    if pin is None:
        return params
    supplied = _coerce_bbox4(params.get("bbox"))
    if supplied is not None:
        # TIGHT tolerance for the snap decision (NOT the coarse ~2 km scenario-
        # reuse quantization): the live bug was a same-area box only ~0.005-0.01
        # deg off the pin yet covering 87% width / 63% height of the domain, which
        # the reuse quant would call "equivalent". We compare near-exactly here so
        # those drifted same-area boxes are snapped, not waved through.
        if bbox_equivalent(supplied, pin, quant=_AOI_DEFAULT_EQ_TOL_DEG):
            return params  # already (essentially) the pin -> no needless copy
        # A genuinely DIFFERENT place (disjoint) is the user's intent -> honor it.
        if not _bbox_overlaps(supplied, pin):
            return params
        # An explicit WIDEN: the supplied box ENCLOSES the pin on all four edges
        # (it is at least as large as the pin everywhere, so the user asked for a
        # bigger area). A drifted / narrower same-area box CLIPS the pin on at
        # least one edge -> not an enclose -> falls through to the snap. The tight
        # tolerance keeps a near-equal box from masquerading as a widen.
        if bbox_encloses(supplied, pin, quant=_AOI_DEFAULT_EQ_TOL_DEG):
            return params
    # Bare follow-up OR a drifted/narrower same-area box -> snap to the pinned AOI.
    new_params = dict(params)
    new_params["bbox"] = list(pin)
    logger.info(
        "aoi-default: %s bbox -> pinned Case AOI %s (was %s)",
        tool_name,
        list(pin),
        list(supplied) if supplied is not None else None,
    )
    return new_params

#: Expensive-solver scenario types whose domain IS an AOI bbox (areal solvers).
#: ``scenario_type_for_tool`` also recognizes a POINT-driven groundwater
#: scenario (-> ``"plume"``) which takes NO bbox param -- its domain is a well
#: / source point, not a rectangle. The AOI-snap below must NOT inject a bbox
#: into those (it would be a spurious, ignored key today and latent
#: wrong-extent debt tomorrow), so the guard is restricted to these
#: bbox-driven scenario types.
_BBOX_DRIVEN_SOLVER_SCENARIOS: frozenset[str] = frozenset({"flood-depth", "swmm-depth"})

def _maybe_default_solver_bbox_to_pinned_aoi(
    tool_name: str,
    params: dict,
    pinned_bbox: Any,
) -> dict:
    """Pin an expensive SOLVER's bbox to the active Case AOI.

    The solve must compute ONLY within the active AOI bbox unless
    something requires it to expand. This snaps the SOLVE domain back onto
    the active AOI by the SAME conservative rule the fetch default
    (``_maybe_default_fetch_bbox_to_pinned_aoi``) uses.

    PRECISE RULE (identical to the fetch default -- honor real expansion, fix the
    drifted same-area box; "required expansion is allowed, only UN-required
    expansion is the bug"):
      * Only applies to the bbox-driven AREAL solvers (flood / urban depth).
        POINT-driven solvers (a plume scenario) take no bbox and are skipped.
      * No pinned AOI -> no-op. The FIRST solve in a Case (no AOI pinned yet)
        DEFINES the domain from the LLM's bbox; the pin is written AFTER it.
      * No / invalid ``bbox`` supplied -> inject the pin (solve the active AOI).
      * Supplied bbox that OVERLAPS the pin but does NOT enclose it (a wider /
        drifted same-area box that pokes outside the displayed AOI) -> REPLACE
        with the pin: solve ONLY within the active AOI.
      * Supplied bbox that already ENCLOSES the pin (an explicit larger area the
        user asked to model) -> HONOR it. REQUIRED expansion is allowed.
      * Supplied bbox DISJOINT from the pin (a genuinely different place) ->
        HONOR it.

    The areal-solver scenario-coverage archetypes (fluvial / compound / wind /
    infiltration / levee / tsunami) and coastal runs are selected by FORCING
    FLAGS (``coastal=`` / ``river=`` / ``tsunami=`` ...), NOT by an
    enclosing-wider bbox, and an explicit enclose / disjoint bbox is always
    honored -- so none of those decks are clipped by this guard.

    Pure + conservative: returns a NEW dict only when it changes ``bbox``; never
    mutates the input dict in place. Shares the exact tolerance / enclose / overlap
    semantics of the fetch default for a single, auditable AOI-snap policy.
    """
    if scenario_type_for_tool(tool_name) not in _BBOX_DRIVEN_SOLVER_SCENARIOS:
        # Non-solver, or a POINT-driven solver (a plume scenario) that takes no
        # bbox -- never inject one. Only the areal (bbox-driven) flood/urban solvers
        # have an AOI rectangle to snap.
        return params
    pin = _coerce_bbox4(pinned_bbox)
    if pin is None:
        return params
    supplied = _coerce_bbox4(params.get("bbox"))
    if supplied is not None:
        # Already (essentially) the active AOI -> no needless copy.
        if bbox_equivalent(supplied, pin, quant=_AOI_DEFAULT_EQ_TOL_DEG):
            return params
        # A genuinely DIFFERENT place (disjoint) is the user's intent -> honor it.
        if not _bbox_overlaps(supplied, pin):
            return params
        # An explicit WIDEN (encloses the pin on all four edges) is REQUIRED
        # expansion the user asked for -> honor it.
        if bbox_encloses(supplied, pin, quant=_AOI_DEFAULT_EQ_TOL_DEG):
            return params
    # Bare follow-up OR a drifted / wider same-area box that pokes outside the
    # displayed AOI -> snap the SOLVE domain to the active AOI bbox.
    new_params = dict(params)
    new_params["bbox"] = list(pin)
    logger.info(
        "aoi-solve-default: %s solve bbox -> active Case AOI %s (was %s)",
        tool_name,
        list(pin),
        list(supplied) if supplied is not None else None,
    )
    return new_params
