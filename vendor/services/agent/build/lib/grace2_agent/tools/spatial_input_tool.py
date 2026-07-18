"""Atomic tool ``request_spatial_input`` — FR-AS-10 / FR-WC-16 urban vector-draw.

The LLM-facing surface that PAUSES the turn and asks the user to DRAW on the map
(a terra-draw surface in the web client): an area of interest, structural flood
WALLS (red, water is dammed) and FLAP GATES (green, one-way drains), or a simple
point / bbox pick. The drawn geometry comes back as a role-tagged GeoJSON
``FeatureCollection``; the agent splits it by role and the ``role=="barrier"``
features become the ``barriers`` FeatureCollection that feeds
``run_swmm_urban_flood(barriers=...)`` straight into the existing PySWMM engine
seam (wall = omitted overland conduit; flap_gate = one-way SWMM orifice).

ARCHITECTURE NOTE (why this tool body is a thin sentinel): the actual
websocket pause/resume — emit ``spatial-input-request``, await
``spatial-input-response``, parse the drawn FeatureCollection — lives in
``server.py`` (``_handle_request_spatial_input``), where the live socket and the
session-scoped pending-future registry are reachable. A catalog tool runs in
isolation via ``_invoke_tool_via_emitter`` and has no socket, so this body just
returns a SENTINEL dict; the server turn loop detects the sentinel for
``request_spatial_input`` and REPLACES the result with the real, parsed drawn
geometry. This mirrors the ``geocode_location`` -> region-choice interception
pattern. The sentinel key is kept in lock-step with
``server.SPATIAL_INPUT_SENTINEL_KEY``.

FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` — an interactive gate, never cached.
"""

from __future__ import annotations

import logging
from typing import Any

from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool

logger = logging.getLogger("grace2_agent.tools.spatial_input_tool")

__all__ = ["request_spatial_input", "SPATIAL_INPUT_SENTINEL_KEY"]

# Kept in lock-step with ``server.SPATIAL_INPUT_SENTINEL_KEY`` (the turn loop
# checks this exact key to know it must run the websocket pause/resume).
SPATIAL_INPUT_SENTINEL_KEY = "_request_spatial_input"

_VALID_MODES = ("point", "bbox", "vector_draw")
_VALID_PURPOSES = ("barrier", "line")


_REQUEST_SPATIAL_INPUT_METADATA = AtomicToolMetadata(
    name="request_spatial_input",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _REQUEST_SPATIAL_INPUT_METADATA,
    # readOnlyHint=True (asks the user for input; mutates no stored state),
    # openWorldHint=True (the answer comes from outside — the user's drawing),
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
    purpose: str = "barrier",
    suggested_view: dict[str, Any] | None = None,
    default_timeout_seconds: int | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Ask the user to DRAW geometry on the map, then PAUSE until they finish.

    Use this when you need the user to physically draw on the map rather than
    describe an area in words:

        - Urban flood (SWMM): the user wants to place flood WALLS / FLAP GATES,
          or outline the exact block / neighborhood AOI. Call this with
          ``mode="vector_draw"`` BEFORE ``run_swmm_urban_flood`` — the result's
          ``barriers`` field is the FeatureCollection you pass straight to
          ``run_swmm_urban_flood(barriers=...)``, and ``aoi_bbox`` is the
          ``bbox`` to model.
        - A NEUTRAL elevation/section LINE (``mode="vector_draw"`` +
          ``purpose="line"``): the user draws a single plain LineString -- an
          elevation profile / cross-section line -- with NO wall/flap-gate
          tagging. Pass the result's ``line`` (``[[lon,lat],...]``) or
          ``linestring`` (a GeoJSON LineString) straight to
          ``compute_terrain_profile(line=...)`` / ``compute_cross_section``.
        - A single map click (``mode="point"``) or a drag-rectangle
          (``mode="bbox"``) for a precise location the user could not name.

    Do NOT use this when the user already gave a clear place name / address /
    bbox in text (geocode it instead), or when no map is in front of the user.

    Params:
        mode: ``"vector_draw"`` (DEFAULT — the terra-draw surface for AOIs +
            tagged walls/flap-gates), ``"point"`` (single click), or ``"bbox"``
            (drag-rectangle).
        title: short prompt heading shown over the draw surface.
        description: one-line instruction telling the user what to draw.
        purpose: ``vector_draw`` only. ``"barrier"`` (DEFAULT -- drawn lines are
            structural SWMM walls / flap gates that the user MUST tag) or
            ``"line"`` (drawn line is a NEUTRAL elevation/section line for
            ``compute_terrain_profile`` -- submitted plain, no tagging). Use
            ``"line"`` when you need an elevation-profile / cross-section line.
        suggested_view: OPTIONAL ``{"bbox": [minLon, minLat, maxLon, maxLat],
            "zoom": <float>}`` camera hint so the map jumps to the right place
            before drawing.
        default_timeout_seconds: OPTIONAL wait window (seconds). Default 300.

    Returns (after the user finishes — the turn is PAUSED until then):
        On a ``vector_draw`` reply: ``{"status": "ok", "geometry_type":
        "vector_draw", "aoi_bbox": [minLon,minLat,maxLon,maxLat] | absent,
        "barriers": <FeatureCollection> | absent, "n_walls": int,
        "n_flap_gates": int, "points": [[lon,lat],...], "n_aoi": int,
        "n_lines": int, "line": [[lon,lat],...] | absent, "linestring":
        <GeoJSON LineString> | absent}``. For SWMM, pass ``barriers`` straight
        to ``run_swmm_urban_flood(barriers=...)`` and ``aoi_bbox`` as its
        ``bbox``. For a ``purpose="line"`` request, pass ``line`` (or
        ``linestring``) straight to ``compute_terrain_profile(line=...)``.

        On a ``point`` / ``bbox`` reply: ``{"status": "ok", "geometry_type":
        ..., "coordinates": [...]}``.

        If the user cancels: ``{"status": "cancelled", ...}``. On timeout / no
        interactive client / malformed drawing: ``{"status": "error",
        "error_code": "SPATIAL_INPUT_...", "error_message": ...}``. NEVER invent
        an AOI or barriers when the result is an error or cancellation — ask the
        user or proceed without them.

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"``.
    """
    norm_mode = (mode or "vector_draw").strip()
    if norm_mode not in _VALID_MODES:
        # Honest typed error — never silently coerce to a different mode.
        return {
            "status": "error",
            "error_code": "SPATIAL_INPUT_PARAMS_INVALID",
            "error_message": (
                f"mode must be one of {list(_VALID_MODES)}, got {mode!r}."
            ),
        }
    norm_purpose = (purpose or "barrier").strip()
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
