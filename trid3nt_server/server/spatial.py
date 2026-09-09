"""Bbox / AOI helpers and the spatial pending-input registries.

The coercion and zoom-to helpers never touch session state; the region-choice
and spatial-input registries take the owner-checked register/pop/resolve shape
and fail open on timeout, so an unanswered picker never hangs the turn."""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from .errors import SpatialInputInvalidResponseError

if TYPE_CHECKING:
    from trid3nt_contracts.region_choice import (
        RegionChoiceProvidedEnvelopePayload,
    )
    from trid3nt_contracts.ws import SpatialInputResponsePayload

logger = logging.getLogger("trid3nt_server.server")


# --------------------------------------------------------------------------- #
# Turn zoom-to accumulator helpers
# --------------------------------------------------------------------------- #
def _is_finite_bbox4(bbox: Any) -> bool:
    """True iff ``bbox`` is a 4-tuple or list of finite real numbers, so a
    None, wrong-length or non-finite bbox never lands a bad zoom-to."""
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        return False
    for v in bbox:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
        if not math.isfinite(float(v)):
            return False
    return True


def _coerce_bbox4(value: Any) -> tuple[float, float, float, float] | None:
    """Coerce ``value`` into a finite 4-float bbox tuple, else ``None``; a
    string, a wrong length or a non-finite value is rejected so a bad extent
    never becomes a pinned AOI or a forced fetch bbox."""
    if not _is_finite_bbox4(value):
        return None
    return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))


def _aoi_zoom_to_bbox(
    result: Any, current_turn_map_commands: list[dict]
) -> tuple[float, float, float, float] | None:
    """Return the bbox the camera should snap to for a tool ``result``, or
    ``None`` when there is no finite extent or the extent repeats this turn's
    last zoom-to. Pure: the caller owns the emit and the accumulator append."""
    # A top-level ``bbox`` wins over ``aoi_bbox``, and the fire is on any
    # established extent, not only a geocode: coordinates given directly skip
    # geocoding, and the map must still move to where the work is.
    if not isinstance(result, dict):
        return None
    raw = result.get("bbox")
    if not _is_finite_bbox4(raw):
        raw = result.get("aoi_bbox")
    aoi = _coerce_bbox4(raw)
    if aoi is None:
        return None
    last = _last_zoom_to_bbox(current_turn_map_commands)
    if last is not None and list(aoi) == list(last):
        return None  # already snapped to this exact AOI this turn.
    return aoi


def _last_zoom_to_bbox(commands: list[dict]) -> list | None:
    """Return the bbox of the most-recent ``zoom-to`` entry, else ``None``; the
    walk is newest-first so the dedupe compares against the same bbox the client
    would replay."""
    for cmd in reversed(commands):
        if isinstance(cmd, dict) and cmd.get("command") == "zoom-to":
            args = cmd.get("args")
            if isinstance(args, dict):
                bbox = args.get("bbox")
                if isinstance(bbox, (tuple, list)):
                    return list(bbox)
            return None
    return None


# --------------------------------------------------------------------------- #
# Session-scoped pending-REGION-CHOICE registry
# --------------------------------------------------------------------------- #
#
# A geocode that lands on a state-bbox fallback emits a region-choice request
# and pauses on a future keyed by the choice ``request_id``; the reply, which
# may arrive on a sibling connection of the same session, either narrows the
# bbox to the picked region or keeps the whole state, and a cross-session reply
# is refused. Fail-open: on timeout the whole-state bbox is used unchanged so
# the automated path never blocks.
_PENDING_REGION_CHOICES: dict[str, tuple[str, asyncio.Future]] = {}


def _register_pending_region_choice(
    session_id: str, request_id: str, fut: "asyncio.Future"
) -> None:
    _PENDING_REGION_CHOICES[request_id] = (session_id, fut)


def _pop_pending_region_choice(request_id: str) -> None:
    _PENDING_REGION_CHOICES.pop(request_id, None)


def _resolve_pending_region_choice(
    session_id: str, provided: "RegionChoiceProvidedEnvelopePayload"
) -> bool:
    """Complete the pending region-choice future for ``provided.request_id``,
    True when a live future was resolved; an unknown, already-resolved or
    cross-session request_id is refused."""
    entry = _PENDING_REGION_CHOICES.get(provided.request_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "region-choice-provided REFUSED: session=%s is not the owner "
            "(owner=%s) for request_id=%s",
            session_id,
            owner_session,
            provided.request_id,
        )
        return False
    if fut.done():
        _PENDING_REGION_CHOICES.pop(provided.request_id, None)
        return False
    fut.set_result(provided)
    _PENDING_REGION_CHOICES.pop(provided.request_id, None)
    return True


# --------------------------------------------------------------------------- #
# Session-scoped pending-SPATIAL-INPUT registry
# --------------------------------------------------------------------------- #
#
# A ``request_spatial_input`` call emits its envelope and pauses on a future
# keyed by the request ``request_id``; the inbound response, which may arrive on
# a sibling connection of the same session, resolves it with the drawn features
# or a cancellation, and a cross-session response is refused. Fail-open: on
# timeout the gate resolves to ``None`` and the caller surfaces a typed "no
# geometry drawn" result, never a fabricated AOI.
_PENDING_SPATIAL_INPUTS: dict[str, tuple[str, asyncio.Future]] = {}


def _register_pending_spatial_input(
    session_id: str, request_id: str, fut: "asyncio.Future"
) -> None:
    _PENDING_SPATIAL_INPUTS[request_id] = (session_id, fut)


def _pop_pending_spatial_input(request_id: str) -> None:
    _PENDING_SPATIAL_INPUTS.pop(request_id, None)


def _resolve_pending_spatial_input(
    session_id: str, response: "SpatialInputResponsePayload"
) -> bool:
    """Complete the pending spatial-input future for ``response.request_id``,
    True when a live future was resolved; an unknown, already-resolved or
    cross-session request_id is refused."""
    entry = _PENDING_SPATIAL_INPUTS.get(response.request_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "spatial-input-response REFUSED: session=%s is not the owner "
            "(owner=%s) for request_id=%s",
            session_id,
            owner_session,
            response.request_id,
        )
        return False
    if fut.done():
        _PENDING_SPATIAL_INPUTS.pop(response.request_id, None)
        return False
    fut.set_result(response)
    _PENDING_SPATIAL_INPUTS.pop(response.request_id, None)
    return True


def _fail_pending_spatial_input(
    session_id: str,
    request_id: str,
    error_code: str,
    error_message: str,
) -> bool:
    """Fail the pending spatial-input future with a typed error so the awaiting
    turn wakes at once instead of hanging until the read TTL; False when the
    request_id is unknown, already resolved, or owned by another session."""
    entry = _PENDING_SPATIAL_INPUTS.get(request_id)
    if entry is None:
        return False
    owner_session, fut = entry
    if owner_session != session_id:
        logger.warning(
            "spatial-input-response (invalid) REFUSED: session=%s is not the "
            "owner (owner=%s) for request_id=%s",
            session_id,
            owner_session,
            request_id,
        )
        return False
    if fut.done():
        _PENDING_SPATIAL_INPUTS.pop(request_id, None)
        return False
    fut.set_exception(
        SpatialInputInvalidResponseError(error_code, error_message)
    )
    _PENDING_SPATIAL_INPUTS.pop(request_id, None)
    return True
