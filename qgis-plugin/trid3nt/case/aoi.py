"""Canvas-extent AOI helpers -- PURE PYTHON (no PyQGIS / PyQt imports).

Milestone 2 item 2: when the dock's "Use map canvas as area of interest"
toggle is ON, every outgoing user-message carries the current canvas extent
(transformed to EPSG:4326) as the AOI.

How the AOI actually rides the wire (studied against the live protocol,
NOT invented):

* The ONLY structured client->server AOI carrier in the protocol is the
  Case bbox: ``case-command create`` accepts ``args.bbox =
  [lon_min, lat_min, lon_max, lat_max]`` (EPSG:4326) -- the web's #170
  "AOI-first" path (vendor/web/src/hooks/useCases.ts createCase). The agent
  seeds ``CaseSummary.bbox`` + ``state.case_bbox`` from it, and
  ``_turn_case_bbox`` (services/agent server.py) anchors every turn's tool
  dispatch on that value. We mirror the exact field name (``bbox``) and
  element order (``[lon_min, lat_min, lon_max, lat_max]``).

* There is NO per-message bbox field: ``UserMessagePayload`` is
  ``extra="forbid"`` (contracts common.GraceModel), so an added field would
  be REJECTED by the live agent, not silently dropped. The web never sends
  one either (ws.ts sendUserMessage carries only text / research_mode /
  model_id / case_id). For the CURRENT canvas extent on each turn we
  therefore append an explicit bracketed context line to the message TEXT
  (the LLM is the consumer of an AOI-in-prompt; the agent's own server-side
  "[Case state]" notes use the same in-text convention). The line spells out
  the same ``bbox = [lon_min, lat_min, lon_max, lat_max]`` shape so the
  numbers survive verbatim into tool args.

Guard (honest-clamp culture): an extent wider than ``AOI_MAX_DEG`` per side
is NOT attached -- a whole-country canvas is not a usable simulation AOI, and
silently sending it would invite a giant fetch. The dock notes why instead.
"""

from __future__ import annotations

import math
from typing import Optional, Tuple

__all__ = [
    "AOI_MAX_DEG",
    "attach_aoi_to_text",
    "aoi_status_text",
    "bbox_span_deg",
    "bbox_within_guard",
    "choose_aoi",
    "extent_to_bbox4326",
    "format_bbox",
    "merc_to_lonlat",
]

#: Guard: maximum extent side (degrees) still attached as an AOI.
AOI_MAX_DEG = 2.0

# Spherical-mercator constants (EPSG:3857).
_EARTH_RADIUS_M = 6378137.0
_MERC_MAX = math.pi * _EARTH_RADIUS_M  # ~20037508.34


def merc_to_lonlat(x: float, y: float) -> Tuple[float, float]:
    """EPSG:3857 metres -> (lon, lat) degrees (spherical mercator inverse)."""
    lon = math.degrees(x / _EARTH_RADIUS_M)
    lat = math.degrees(2.0 * math.atan(math.exp(y / _EARTH_RADIUS_M)) - math.pi / 2.0)
    return lon, lat


def extent_to_bbox4326(
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    crs_authid: str,
) -> Optional[Tuple[float, float, float, float]]:
    """Canvas extent in ``crs_authid`` -> ``(lon_min, lat_min, lon_max, lat_max)``.

    PURE math for the two CRS that cover virtually every QGIS web-mapping
    project: EPSG:4326 (passthrough + clamp) and EPSG:3857. Any other CRS
    returns None -- the caller (dock) falls back to QGIS's own
    ``QgsCoordinateTransform``, which knows the full proj database. Degenerate
    or non-finite extents also return None (never a fabricated bbox).
    """
    values = (xmin, ymin, xmax, ymax)
    if any(not math.isfinite(v) for v in values):
        return None
    if xmax <= xmin or ymax <= ymin:
        return None
    authid = (crs_authid or "").strip().upper()
    if authid in ("EPSG:4326", "OGC:CRS84"):
        lon_min, lat_min, lon_max, lat_max = xmin, ymin, xmax, ymax
    elif authid == "EPSG:3857":
        # Clamp to the mercator world square before inverting.
        cx0 = max(-_MERC_MAX, min(_MERC_MAX, xmin))
        cx1 = max(-_MERC_MAX, min(_MERC_MAX, xmax))
        cy0 = max(-_MERC_MAX, min(_MERC_MAX, ymin))
        cy1 = max(-_MERC_MAX, min(_MERC_MAX, ymax))
        lon_min, lat_min = merc_to_lonlat(cx0, cy0)
        lon_max, lat_max = merc_to_lonlat(cx1, cy1)
    else:
        return None
    lon_min = max(-180.0, min(180.0, lon_min))
    lon_max = max(-180.0, min(180.0, lon_max))
    lat_min = max(-90.0, min(90.0, lat_min))
    lat_max = max(-90.0, min(90.0, lat_max))
    if lon_max <= lon_min or lat_max <= lat_min:
        return None
    return (lon_min, lat_min, lon_max, lat_max)


def bbox_span_deg(bbox: Tuple[float, float, float, float]) -> Tuple[float, float]:
    """(lon span, lat span) of a ``[lon_min, lat_min, lon_max, lat_max]`` bbox."""
    return (bbox[2] - bbox[0], bbox[3] - bbox[1])


def bbox_within_guard(
    bbox: Tuple[float, float, float, float], max_deg: float = AOI_MAX_DEG
) -> bool:
    """True when BOTH sides are within the guard (attachable as an AOI)."""
    dlon, dlat = bbox_span_deg(bbox)
    return dlon <= max_deg and dlat <= max_deg


def choose_aoi(
    selection_bbox: Optional[Tuple[float, float, float, float]],
    canvas_bbox: Optional[Tuple[float, float, float, float]],
    prefer_selection: bool,
) -> Tuple[Optional[Tuple[float, float, float, float]], Optional[str]]:
    """Pick which AOI rides this send: ``(bbox, source)``.

    Milestone 3 item 4 (selected-polygon AOI): when the selection toggle is
    ON and an actual selection resolved, the SELECTION bbox wins (v1: the
    bbox of the selection, not the exact ring -- the agent's only structured
    AOI carrier is ``args.bbox`` / the in-text bbox line, both 4-number
    boxes; ``UserMessagePayload`` is extra=forbid so no ring field exists).
    Otherwise the canvas extent (when resolved) is used. ``source`` is
    ``"selection"`` / ``"canvas"`` / None -- the status line and the in-text
    context line both name it so the user always knows WHICH extent went out.

    The 2-deg guard is deliberately NOT applied here: a too-large selection
    must surface as "selection ... too large", not silently fall back to the
    canvas the user explicitly overrode.
    """
    if prefer_selection and selection_bbox is not None:
        return selection_bbox, "selection"
    if canvas_bbox is not None:
        return canvas_bbox, "canvas"
    return None, None


def format_bbox(bbox: Tuple[float, float, float, float], precision: int = 6) -> str:
    """``[lon_min, lat_min, lon_max, lat_max]`` with fixed precision -- the
    exact element order the agent's ``args.bbox`` / ``_coerce_bbox4`` expect."""
    return "[" + ", ".join(f"{v:.{precision}f}" for v in bbox) + "]"


def attach_aoi_to_text(
    text: str,
    bbox: Tuple[float, float, float, float],
    source: str = "canvas",
) -> str:
    """Append the AOI context line to an outgoing user-message.

    See the module docstring: the wire contract forbids a per-message bbox
    FIELD, so the per-turn carrier is an explicit in-text context line using
    the same ``bbox = [lon_min, lat_min, lon_max, lat_max]`` shape the
    structured Case AOI uses. ``source`` names the origin ("canvas" default;
    "selection" for the milestone 3 selected-polygon AOI, which is honestly
    labelled a bbox OF the selection, not the exact ring).
    """
    origin = (
        "QGIS selected-feature AOI (bbox of the selection, EPSG:4326)"
        if source == "selection"
        else "QGIS map canvas AOI (EPSG:4326)"
    )
    return (
        f"{text}\n\n"
        f"[{origin}: bbox = {format_bbox(bbox)}. "
        "Use this extent as the area of interest unless the message names a "
        "different location.]"
    )


def aoi_status_text(
    bbox: Optional[Tuple[float, float, float, float]],
    enabled: bool,
    max_deg: float = AOI_MAX_DEG,
    source: str = "canvas",
) -> str:
    """The dock's one-line AOI status.

    - toggle off               -> "AOI: off"
    - no resolvable bbox       -> honest unresolved note
    - within guard             -> "AOI: canvas 0.12 x 0.09 deg"
      (or "AOI: selection ..." when the selection override supplied it)
    - exceeds guard            -> "... too large (> 2.0 deg/side), sent without AOI"
    """
    label = "selection" if source == "selection" else "canvas"
    if not enabled:
        return "AOI: off"
    if bbox is None:
        return f"AOI: {label} extent unavailable (CRS not resolved) -- sent without AOI"
    dlon, dlat = bbox_span_deg(bbox)
    if not bbox_within_guard(bbox, max_deg):
        return (
            f"AOI: {label} {dlon:.2f} x {dlat:.2f} deg -- too large "
            f"(> {max_deg:g} deg/side), sent without AOI"
        )
    return f"AOI: {label} {dlon:.2f} x {dlat:.2f} deg"
