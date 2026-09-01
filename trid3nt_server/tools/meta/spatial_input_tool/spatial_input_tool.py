"""Atomic tool ``request_spatial_input`` -- draw on the map, pause the turn.

The LLM-facing surface that PAUSES the turn and asks the user to DRAW on the map:
an area of interest, an elevation/section line, or a simple point / bbox pick.
The drawn geometry comes back as a role-tagged GeoJSON ``FeatureCollection``
which the agent splits by role.

ARCHITECTURE NOTE (why this tool body is a thin sentinel): the actual
websocket pause/resume -- emit ``spatial-input-request``, await
``spatial-input-response``, parse the drawn FeatureCollection -- lives in
``server.py`` (``_handle_request_spatial_input``), where the live socket and the
session-scoped pending-future registry are reachable. A catalog tool runs in
isolation via ``_invoke_tool_via_emitter`` and has no socket, so this body just
returns a SENTINEL dict; the server turn loop detects the sentinel for
``request_spatial_input`` and REPLACES the result with the real, parsed drawn
geometry. This mirrors the ``geocode_location`` -> region-choice interception
pattern. The sentinel key is kept in lock-step with
``server.SPATIAL_INPUT_SENTINEL_KEY``.

``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` -- an interactive gate, never cached.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

logger = logging.getLogger("trid3nt_server.tools.meta.spatial_input_tool.spatial_input_tool")

__all__ = ["request_spatial_input", "SPATIAL_INPUT_SENTINEL_KEY"]

# Kept in lock-step with ``server.SPATIAL_INPUT_SENTINEL_KEY`` (the turn loop
# checks this exact key to know it must run the websocket pause/resume).
SPATIAL_INPUT_SENTINEL_KEY = "_request_spatial_input"

_VALID_MODES = ("point", "bbox", "vector_draw")
_VALID_PURPOSES = ("aoi", "line")


_REQUEST_SPATIAL_INPUT_METADATA = AtomicToolMetadata(
    name="request_spatial_input",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _REQUEST_SPATIAL_INPUT_METADATA,
    # readOnlyHint=True (asks the user for input; mutates no stored state),
    # openWorldHint=True (the answer comes from outside -- the user's drawing),
    # destructiveHint=False, idempotentHint=False (each call mints a request_id).
    read_only_hint=True,
    open_world_hint=True,
    destructive_hint=False,
    idempotent_hint=False,
)
async def request_spatial_input(
    mode: str = "vector_draw",
    title: str | None = None,
    description: str | None = None,
    purpose: str = "aoi",
    suggested_view: dict[str, Any] | None = None,
    default_timeout_seconds: int | None = None,
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Ask the user to DRAW geometry on the map, then PAUSE until they finish.

    Use this when the user must physically draw on the map rather than
    describe an area in words: an AOI/region outline for any bbox-taking tool
    (``mode="vector_draw"``, ``purpose="aoi"``, the default -- ``aoi_bbox``
    passes straight to any tool taking a ``bbox``); a neutral elevation/section
    LINE (``purpose="line"`` -- result's ``line``/``linestring`` passes to
    ``compute_cross_section``); or a single click (``mode="point"``) /
    drag-rectangle (``mode="bbox"``).
    Do NOT use when the user already gave a clear place name/address/bbox
    in text (geocode instead).

    Params:
        mode: ``"vector_draw"`` (default), ``"point"``, or ``"bbox"``.
        title/description: prompt heading + one-line draw instruction.
        purpose: ``vector_draw`` only -- ``"aoi"`` (default; area selection, for
            "show me X over Y" / "flood risk in this area") or ``"line"``
            (a plain elevation/section line).
        suggested_view: optional ``{"bbox", "zoom"}`` camera hint.
        default_timeout_seconds: wait window (default 300).

    Returns (after the user finishes -- the turn PAUSES until then):
        vector_draw: ``{"status": "ok", "geometry_type": "vector_draw",
        "aoi_bbox"?, "points", "n_aoi", "n_lines", "line"?, "linestring"?}``.
        point/bbox: ``{"status": "ok", "geometry_type", "coordinates"}``.
        Cancelled: ``{"status": "cancelled", ...}``. Timeout/no client/
        malformed: ``{"status": "error", "error_code": "SPATIAL_INPUT_...",
        "error_message"}`` -- never invent an AOI on error.
    """
    norm_mode = (mode or "vector_draw").strip()
    if norm_mode not in _VALID_MODES:
        # Honest typed error -- never silently coerce to a different mode.
        return {
            "status": "error",
            "error_code": "SPATIAL_INPUT_PARAMS_INVALID",
            "error_message": (
                f"mode must be one of {list(_VALID_MODES)}, got {mode!r}."
            ),
        }
    norm_purpose = (purpose or "aoi").strip()
    if norm_purpose not in _VALID_PURPOSES:
        return {
            "status": "error",
            "error_code": "SPATIAL_INPUT_PARAMS_INVALID",
            "error_message": (
                f"purpose must be one of {list(_VALID_PURPOSES)}, got {purpose!r}."
            ),
        }
    # This body intentionally does NOT touch the websocket (a catalog tool has
    # no socket). It returns a SENTINEL the server.py turn loop detects and
    # replaces with the real drawn-geometry result via the websocket pause. The
    # validated args ride back so the server builds the request from them.
    logger.info(
        "request_spatial_input sentinel mode=%s purpose=%s",
        norm_mode,
        norm_purpose,
    )
    return {
        SPATIAL_INPUT_SENTINEL_KEY: True,
        "mode": norm_mode,
        "title": title,
        "description": description,
        "purpose": norm_purpose,
        "suggested_view": suggested_view,
        "default_timeout_seconds": default_timeout_seconds,
    }
