"""``request_spatial_input`` - ask the user to DRAW on the map and pause the turn.

The body validates its arguments and returns a SENTINEL: the turn loop, which is
where the live socket is reachable, detects it and REPLACES the result with the
parsed drawn geometry. An interactive gate, never cached."""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

logger = logging.getLogger("trid3nt_server.tools.meta.spatial_input_tool.spatial_input_tool")

__all__ = ["request_spatial_input", "SPATIAL_INPUT_SENTINEL_KEY"]

# The turn loop checks this EXACT key to know it must run the websocket
# pause/resume, so the two definitions stay in lock-step.
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
    # Open-world because the answer comes from outside - the user's drawing - and
    # not idempotent because each call mints its own request id.
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
    # Absorb model-invented kwargs; the normalizer already strips most of them.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Ask the user to DRAW geometry on the map, then PAUSE until they finish.

    ROUTING: the user must physically draw rather than describe - an AOI outline for
    any bbox-taking tool (`mode="vector_draw"`, `purpose="aoi"`, the default; the
    returned `aoi_bbox` passes straight through), an elevation/section LINE
    (`purpose="line"`), a single click (`mode="point"`) or a drag rectangle
    (`mode="bbox"`). NOT when a clear place name, address or bbox was already given
    in text - geocode that instead.

    `title`/`description` are the prompt heading and draw instruction,
    `suggested_view` an optional {"bbox", "zoom"} camera hint, and
    `default_timeout_seconds` the wait window (300).

    Returns once the user finishes: vector_draw gives {status, geometry_type,
    aoi_bbox?, points, n_aoi, n_lines, line?, linestring?}; bbox gives {status,
    geometry_type, coordinates}; point gives {status, geometry_type, coordinates,
    name} - pass {"coordinates": ..., "name": ...} VERBATIM as the Point argument
    of the template (its release, outfall, pour point), so the name the user typed
    travels with the point. A cancel gives status="cancelled", a timeout or
    malformed answer status="error". NEVER invent an AOI on error.
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
    # This body deliberately does NOT touch the websocket: a tool invoked through
    # the emitter has no socket. The SENTINEL carries the validated args back so the
    # turn loop can build the request from them and swap in the real result.
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
