"""The Case AOI: the area a Case is about, pinned by the run that solved it."""

from __future__ import annotations

import logging
from trid3nt_contracts import now_utc
from trid3nt_server.inputs.extent import bbox_equivalent
from trid3nt_server.server.session.persistence_ref import get_persistence
from trid3nt_server.server.session.state import SessionState
from trid3nt_server.server.spatial import _coerce_bbox4
from typing import Any

logger = logging.getLogger("trid3nt_server.server")


async def pin_case_aoi_from_solve(
    state: SessionState,
    *,
    case_id: str | None,
    bbox: Any,
) -> None:
    """Anchor the Case AOI to the extent a completed run solved over, so a later
    fetch that states no area of its own fills from the ground the run covered and
    a reopen rehydrates the same anchor. Best-effort and silent."""
    # The in-session anchor is set before the durable write and independently of
    # it: the write is debounced at the same extent and may fail, and neither is a
    # reason for the rest of the turn to read a stale area.
    coerced = _coerce_bbox4(bbox)
    if coerced is None or not case_id:
        return
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
    if bbox_equivalent(case.bbox, coerced):
        return
    updated = case.model_copy(update={"bbox": coerced, "updated_at": now_utc()})
    try:
        await p.upsert_case(updated)
        logger.info(
            "aoi-pin: pinned Case AOI case=%s bbox=%s (solved extent)",
            case_id,
            list(coerced),
        )
    except Exception:  # noqa: BLE001 -- best-effort, never break the turn
        logger.exception("aoi-pin: upsert failed case=%s", case_id)
