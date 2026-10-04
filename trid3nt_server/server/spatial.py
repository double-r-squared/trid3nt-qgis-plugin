"""Bbox / AOI helpers: coercion and zoom-to dedupe, never touching session state."""

from __future__ import annotations

import logging
import math
from typing import Any

logger = logging.getLogger("trid3nt_server.server")


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


