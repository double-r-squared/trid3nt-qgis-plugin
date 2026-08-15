"""Bbox / AOI helpers and the spatial pending-input registries.

Two groups:
(1) pure bbox/AOI coercion + turn zoom-to accumulator helpers (finite-4-tuple
guard, camera-snap dedupe) that never touch session state; and (2) the
session-scoped region-choice and spatial-input (``request_spatial_input``)
pending registries -- the same register/pop/resolve owner-checked shape as the
interaction gates, fail-open on timeout so an unanswered picker never hangs the
turn. The heavier drawn-geometry handlers (``_emit_spatial_input_and_wait``,
``_handle_request_spatial_input``, ``_set_drawn_geometry_from_payload``) stay in
``_core``: they are entangled with ``SessionState`` and the dispatch loop and
move with the session wave. Moved verbatim (behavior-preserving); ``_core``
re-imports these names so bare-global references and monkeypatch targets on
``trid3nt_server.server.<name>`` resolve exactly as the monolith's did.
"""

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
# job AGENT-AOI-RESIDUAL (#159): turn zoom-to accumulator helpers
# --------------------------------------------------------------------------- #
def _is_finite_bbox4(bbox: Any) -> bool:
    """True iff ``bbox`` is a 4-tuple/list of finite real numbers.

    Guards the LayerURI floored-bbox append so a None / wrong-length /
    NaN / inf bbox never lands a bad zoom-to in ``current_turn_map_commands``.
    """
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        return False
    for v in bbox:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
        if not math.isfinite(float(v)):
            return False
    return True


def _coerce_bbox4(value: Any) -> tuple[float, float, float, float] | None:
    """Coerce ``value`` into a finite 4-float bbox tuple, else ``None``.

    Shared by the LANE-C AOI-pin + fetch-default helpers. Tolerates list/tuple of
    4 numbers; rejects strings, wrong lengths, and non-finite values (so a bad
    extent never becomes a pinned AOI or a forced fetch bbox).
    """
    if not _is_finite_bbox4(value):
        return None
    return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))


def _aoi_zoom_to_bbox(
    result: Any, current_turn_map_commands: list[dict]
) -> tuple[float, float, float, float] | None:
    """Return the bbox the camera should snap to for a tool ``result``.

    Fires whenever an AOI/bbox is established, not only on a
    ``geocode_location`` result -- the user giving coordinates directly skips
    geocode, so without this the map never moved to "where we are" until a
    downstream layer with a bbox landed.

    Prefers a top-level ``bbox``, falling back to ``aoi_bbox`` (the
    request_spatial_input / draw result shape). Returns ``None`` when:
      - ``result`` is not a dict, or carries no finite 4-number extent, OR
      - the extent equals the turn's LAST zoom-to bbox (dedupe: a chain of
        bbox-bearing tools over the SAME AOI must not re-snap repeatedly).
    Pure + side-effect-free so the caller owns the emit + accumulator append.
    """
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
    """Return the bbox of the most-recent ``zoom-to`` entry, else None.

    Mirrors the web ``extractLastZoomTo`` newest-first walk so the dedupe
    here compares against the SAME bbox the client would replay.
    """
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
# Session-scoped pending-REGION-CHOICE registry (region-disambiguation picker)
# --------------------------------------------------------------------------- #
#
# Mirrors ``_PENDING_CREDENTIALS`` exactly, but for the region-choice flow: when
# a ``geocode_location`` result comes back as a state-bbox-fallback snap, the
# dispatch coroutine emits a ``region-choice-request`` envelope (whole-state
# default + candidate counties) and pauses on a future keyed by the choice
# ``request_id``. The inbound ``region-choice-provided`` handler (which may
# arrive on a DIFFERENT WebSocket connection of the same session -- StrictMode
# double-mount / reconnect) resolves the future, and the paused dispatch either
# narrows the geocode bbox to the picked region or keeps the whole-state bbox.
# Fail-open: on timeout / no client the whole-state bbox (already the geocode
# result) is used unchanged so the automated path never blocks. Tagged with the
# owning session_id so a cross-session region-choice-provided is refused.
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
    """Complete the pending region-choice future for ``provided.request_id``.

    Returns True when a live future was resolved. False when the request_id is
    unknown/already-resolved, or when the answering session is not the owner
    (refused loudly -- mirrors ``_resolve_pending_credential``).
    """
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
# Session-scoped pending-SPATIAL-INPUT registry (request_spatial_input)
# --------------------------------------------------------------------------- #
#
# Mirrors ``_PENDING_REGION_CHOICES`` exactly, but for the urban
# vector-draw flow: when the LLM (or the urban-flood flow) calls
# ``request_spatial_input``, the dispatch coroutine emits a
# ``spatial-input-request`` envelope (point / bbox / vector_draw) and pauses on a
# future keyed by the request ``request_id``. The inbound
# ``spatial-input-response`` handler (which may arrive on a DIFFERENT WebSocket
# connection of the same session -- StrictMode double-mount / reconnect) resolves
# the future with the drawn ``FeatureCollection`` (or a cancellation). Tagged
# with the owning session_id so a cross-session response is refused. Fail-open:
# on timeout / no client the gate resolves to ``None`` and the caller surfaces a
# typed "no geometry drawn" result (honest -- never a fabricated AOI/barriers).
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
    """Complete the pending spatial-input future for ``response.request_id``.

    Returns True when a live future was resolved. False when the request_id is
    unknown/already-resolved, or when the answering session is not the owner
    (refused loudly -- mirrors ``_resolve_pending_region_choice``).
    """
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
    """Fail the pending spatial-input future for ``request_id`` with a typed error.

    Used when an inbound ``spatial-input-response`` cannot be parsed/validated
    (e.g. a barrier feature missing ``barrier_type``). Resolves the future
    EAGERLY via ``set_exception`` so the awaiting ``request_spatial_input`` turn
    wakes immediately with a typed error result rather than hanging until the
    read TTL expires. Returns True when a live future was failed; False when the
    request_id is unknown/already-resolved, or the answering session is not the
    owner (refused loudly -- mirrors ``_resolve_pending_spatial_input``).
    """
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
