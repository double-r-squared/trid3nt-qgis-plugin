"""Case-AOI pinning from a fetch bbox + the AOI default a later fetch snaps to."""

from __future__ import annotations

import logging
from trid3nt_contracts import now_utc
from trid3nt_server.server.dispatch.layer_reuse import bbox_encloses, bbox_equivalent, fetched_kind_for_tool
from trid3nt_server.server.session.persistence_ref import get_persistence
from trid3nt_server.server.session.state import SessionState
from trid3nt_server.server.spatial import _coerce_bbox4
from typing import Any

logger = logging.getLogger("trid3nt_server.server")

# The AOI is PINNED once: every follow-up fetch in the Case defaults to that one
# extent rather than a freehand bbox re-derived per tool call, so the layers on
# the map cover the same ground by construction.


def _bbox_round6(bbox: Any) -> tuple[float, float, float, float] | None:
    """Round a coerced bbox to 6 decimals (~0.11 m at the equator) for a TIGHT
    change comparison; ``None`` for a missing or malformed bbox."""
    # Deliberately far tighter than the reuse quant, which means "close enough
    # to be the same data": here only a literally repeated bbox is skipped, so a
    # real but small AOI move is never silently dropped.
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
    """Durably anchor the Case AOI from an ordinary bbox-taking FETCH call, so a
    Case of plain fetches still gets an anchor; it can seed, widen or move a pin
    but never silently shrink one. Best-effort and silent."""
    # The in-session anchor is set unconditionally so it stays in lockstep with
    # the persisted row, and the durable write is debounced on a tight 6-decimal
    # comparison so a repeated identical bbox never round-trips persistence twice.
    if fetched_kind_for_tool(tool_name) is None:
        return
    if not case_id:
        return
    coerced = _coerce_bbox4(params.get("bbox"))
    if coerced is None:
        return
    # Latest-wins: the in-session anchor is refreshed before the durable write,
    # which is best-effort and may no-op (debounce) or fail without undoing it.
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
    """True iff two WGS84 bboxes intersect, touching edges included; the snap
    rule uses it to tell a drifted box aimed at the pinned AOI from a genuinely
    different place."""
    from shapely.geometry import box

    pa = _coerce_bbox4(a)
    pb = _coerce_bbox4(b)
    if pa is None or pb is None:
        return False
    return box(*pa).intersects(box(*pb))

#: Near-exact tolerance (deg) for the fetch-default snap decision, deliberately
#: much tighter than the coarse reuse quant so a same-area-but-drifted box is
#: snapped to the pin rather than waved through as equivalent.
_AOI_DEFAULT_EQ_TOL_DEG = 1e-5

def _maybe_default_fetch_bbox_to_pinned_aoi(
    tool_name: str,
    params: dict,
    pinned_bbox: Any,
) -> dict:
    """Default a bbox-taking fetch tool to the pinned Case AOI: a bare, missing
    or drifted same-area bbox is REPLACED by the pin, while a bbox that encloses
    the pin or is disjoint from it is honored as the caller's intent."""
    # Pure: a NEW dict is returned only when ``bbox`` changes, and the input is
    # never mutated. Without the snap, a follow-up fetch free-hands a narrower
    # box for "the same area I just modeled" and the layers stop covering the AOI.
    if fetched_kind_for_tool(tool_name) is None:
        return params
    pin = _coerce_bbox4(pinned_bbox)
    if pin is None:
        return params
    supplied = _coerce_bbox4(params.get("bbox"))
    if supplied is not None:
        # TIGHT tolerance for the snap decision, not the coarse reuse
        # quantization: a same-area box a few thousandths of a degree off the
        # pin still under-covers the domain, and the reuse quant would call it
        # equivalent. Comparing near-exactly snaps those drifted boxes.
        if bbox_equivalent(supplied, pin, quant=_AOI_DEFAULT_EQ_TOL_DEG):
            return params  # already (essentially) the pin -> no needless copy
        # A genuinely DIFFERENT place (disjoint) is the user's intent -> honor it.
        if not _bbox_overlaps(supplied, pin):
            return params
        # An explicit WIDEN encloses the pin on all four edges. A drifted or
        # narrower same-area box CLIPS the pin somewhere, so it is not an
        # enclose and falls through to the snap.
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
