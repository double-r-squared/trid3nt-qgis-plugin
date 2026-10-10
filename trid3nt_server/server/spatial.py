"""Zoom-to helpers: the bbox the camera snaps to and the dedupe against this turn."""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_server.inputs.extent import as_bbox

logger = logging.getLogger("trid3nt_server.server")


def _aoi_zoom_to_bbox(
    result: Any, current_turn_map_commands: list[dict]
) -> tuple[float, float, float, float] | None:
    """Return the bbox the camera should snap to for a tool ``result``, or ``None`` when there is
    no finite extent or the extent repeats this turn's last zoom-to. Pure: the caller owns the emit and the append."""
    # A top-level ``bbox`` wins over ``aoi_bbox``, and the fire is on any
    # established extent, not only a geocode: coordinates given directly skip
    # geocoding, and the map must still move to where the work is.
    if not isinstance(result, dict):
        return None
    aoi = as_bbox(result.get("bbox")) or as_bbox(result.get("aoi_bbox"))
    if aoi is None:
        return None
    last = _last_zoom_to_bbox(current_turn_map_commands)
    if last is not None and list(aoi) == list(last):
        return None  # already snapped to this exact AOI this turn.
    return aoi


def _last_zoom_to_bbox(commands: list[dict]) -> list | None:
    """Return the bbox of the most-recent ``zoom-to`` entry, else ``None``; the walk is
    newest-first so the dedupe compares against the same bbox the client would replay."""
    for cmd in reversed(commands):
        if isinstance(cmd, dict) and cmd.get("command") == "zoom-to":
            args = cmd.get("args")
            if isinstance(args, dict):
                bbox = args.get("bbox")
                if isinstance(bbox, (tuple, list)):
                    return list(bbox)
            return None
    return None

