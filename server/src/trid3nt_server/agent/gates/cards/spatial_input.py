"""Spatial-input request-card builder + response-to-result translation (pure)."""
from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from trid3nt_contracts.ws import SpatialInputRequestPayload

from ..spatial_input import SpatialInputParseError, parse_spatial_input_features

logger = logging.getLogger("trid3nt_server.agent.gates.cards.spatial_input")


def _build_spatial_input_request_payload(
    *,
    request_id: str,
    call_args: dict[str, Any],
) -> "SpatialInputRequestPayload | None":
    """Build a validated ``spatial-input-request`` from the LLM tool args.

    ``call_args`` is what the LLM passed to ``request_spatial_input`` (mode /
    title / description / optional suggested_view + reference_layers). Returns
    ``None`` when the args cannot form a valid payload (the caller then surfaces a
    typed param error — never silently emits a malformed prompt).
    """
    mode = call_args.get("mode") or "vector_draw"
    title = str(call_args.get("title") or "Draw on the map")
    description = str(
        call_args.get("description")
        or "Draw the area of interest and any flood walls or flap gates."
    )
    payload_kwargs: dict[str, Any] = {
        "request_id": request_id,
        "mode": mode,
        "title": title[:200],
        "description": description[:1024],
    }
    # purpose (vector_draw only): "barrier" (default, SWMM walls/flap-gates),
    # "line" (a NEUTRAL elevation/section line for compute_terrain_profile), or
    # "aoi" (area-of-interest selection -- only rect/polygon tools, no line/tag).
    # Only forwarded when explicitly non-default so the wire default stays
    # "barrier" and the existing SWMM draw flow is byte-for-byte unchanged.
    raw_purpose = call_args.get("purpose")
    if raw_purpose in ("line", "aoi"):
        payload_kwargs["purpose"] = raw_purpose
    # suggested_view: {bbox: [..4..], zoom: float} — optional camera hint.
    sv = call_args.get("suggested_view")
    if isinstance(sv, dict) and isinstance(sv.get("bbox"), (list, tuple)):
        bbox = sv["bbox"]
        if len(bbox) == 4:
            try:
                payload_kwargs["suggested_view"] = {
                    "bbox": (
                        float(bbox[0]),
                        float(bbox[1]),
                        float(bbox[2]),
                        float(bbox[3]),
                    ),
                    "zoom": float(sv.get("zoom", 13.0)),
                }
            except (TypeError, ValueError):
                pass
    to = call_args.get("default_timeout_seconds")
    if isinstance(to, (int, float)) and to > 0:
        payload_kwargs["default_timeout_seconds"] = int(to)
    try:
        return SpatialInputRequestPayload(**payload_kwargs)
    except ValidationError:
        logger.warning(
            "spatial-input: request payload validation failed args=%s",
            call_args,
            exc_info=True,
        )
        return None


def _spatial_response_to_result(
    response: "SpatialInputResponsePayload | None",
) -> dict[str, Any]:
    """Translate a ``spatial-input-response`` into the tool result the LLM reads.

    The result the LLM sees after ``request_spatial_input`` resumes:

    - timeout / no client (``response is None``)  ->
      ``{status: "error", error_code: "SPATIAL_INPUT_TIMEOUT", ...}``.
    - explicit cancellation                       ->
      ``{status: "cancelled", ...}``.
    - point / bbox reply                          ->
      ``{status: "ok", geometry_type, coordinates}``.
    - vector_draw reply                           ->
      ``{status: "ok", geometry_type: "vector_draw", aoi_bbox, barriers,
         n_walls, n_flap_gates, points, n_aoi, n_lines}`` -- ``barriers`` is the
      clean engine-ready FeatureCollection (pass straight to
      ``swmm_urban_flood(barriers=...)``). When a NEUTRAL line was drawn
      (purpose="line"), ``line`` (``[[lon,lat],...]``) + ``linestring`` (a
      GeoJSON LineString) carry it for ``compute_terrain_profile(line=...)``.
    - structurally invalid drawn FC               ->
      ``{status: "error", error_code: "SPATIAL_INPUT_<...>", ...}`` (honesty
      floor — malformed geometry NEVER reads as a success).
    """
    if response is None:
        return {
            "status": "error",
            "error_code": "SPATIAL_INPUT_TIMEOUT",
            "error_message": (
                "No drawing was received from the user (the spatial-input "
                "request timed out or no interactive client was connected). "
                "Ask the user to draw the area / barriers, or proceed without "
                "them — do not invent a geometry."
            ),
        }
    if response.cancelled:
        return {
            "status": "cancelled",
            "message": (
                "The user cancelled the drawing. No area or barriers were "
                "provided; do not fabricate any — ask how they want to proceed."
            ),
        }
    gtype = response.geometry_type
    if gtype in ("point", "bbox"):
        if not response.coordinates:
            return {
                "status": "error",
                "error_code": "SPATIAL_INPUT_MISSING_COORDINATES",
                "error_message": (
                    f"spatial-input-response geometry_type={gtype!r} carried no "
                    f"coordinates."
                ),
            }
        return {
            "status": "ok",
            "geometry_type": gtype,
            "coordinates": list(response.coordinates),
        }
    if gtype == "vector_draw":
        if not isinstance(response.features, dict):
            return {
                "status": "error",
                "error_code": "SPATIAL_INPUT_MISSING_FEATURES",
                "error_message": (
                    "vector_draw response carried no features FeatureCollection."
                ),
            }
        try:
            parsed = parse_spatial_input_features(response.features)
        except SpatialInputParseError as exc:
            # Honesty floor: a malformed drawn FeatureCollection degrades to a
            # TYPED error result, never a silent success.
            return {
                "status": "error",
                "error_code": exc.error_code,
                "error_message": (
                    f"The drawn geometry could not be used: {exc}. Ask the "
                    f"user to redraw; do not fabricate barriers or an AOI."
                ),
            }
        result: dict[str, Any] = {
            "status": "ok",
            "geometry_type": "vector_draw",
            "n_walls": parsed.n_walls,
            "n_flap_gates": parsed.n_flap_gates,
            "n_aoi": len(parsed.aoi_features),
            "n_lines": parsed.n_lines,
            "points": parsed.points,
        }
        if parsed.aoi_bbox is not None:
            result["aoi_bbox"] = list(parsed.aoi_bbox)
        # Generalized drawn roles -- surfaced so the LLM can pass them
        # to whichever engine composer accepts them (SFINCS/GeoClaw breach_point,
        # the MODFLOW DISV / TELEMAC refine_region sizing, breaklines, boundaries).
        if parsed.breach_points:
            # Interior levee/dam-breach source(s): pass the FIRST straight to
            # sfincs_flood(breach_point=...) / a GeoClaw breach point (drawn value
            # PREFERRED over a plain tuple arg when a breach was drawn).
            result["breach_points"] = [list(p) for p in parsed.breach_points]
            result["breach_point"] = list(parsed.breach_points[0])
        if parsed.refine_regions:
            # Per-region mesh sizing polygons: {polygon, target_size_m, bbox}.
            result["refine_regions"] = [
                {
                    "polygon": r["polygon"],
                    "target_size_m": r["target_size_m"],
                    "bbox": list(r["bbox"]),
                }
                for r in parsed.refine_regions
            ]
            result["n_refine_regions"] = len(parsed.refine_regions)
        if parsed.breaklines:
            result["breaklines"] = [
                [list(pt) for pt in ln] for ln in parsed.breaklines
            ]
        if parsed.boundary_lines:
            result["boundary_lines"] = [
                {
                    "coords": [list(pt) for pt in b["coords"]],
                    "boundary_type": b["boundary_type"],
                }
                for b in parsed.boundary_lines
            ]
        if parsed.barriers is not None:
            # The clean, engine-ready barriers FeatureCollection — pass straight
            # to swmm_urban_flood(barriers=...). It validates field-for-field
            # against SWMMRunArgs.barriers.
            result["barriers"] = parsed.barriers
        if parsed.line_coords is not None:
            # A NEUTRAL drawn elevation/section line (purpose="line"): surface the
            # plain LineString vertices so the LLM can pass them straight to
            # compute_terrain_profile(line=...) / compute_cross_section(line=...).
            # `line` is the bare [[lon,lat],...] vertex list; `linestring` is the
            # GeoJSON LineString geometry -- both resolve via _resolve_line_coords.
            result["line"] = [list(pt) for pt in parsed.line_coords]
            result["linestring"] = {
                "type": "LineString",
                "coordinates": [list(pt) for pt in parsed.line_coords],
            }
        return result
    return {
        "status": "error",
        "error_code": "SPATIAL_INPUT_UNKNOWN_GEOMETRY",
        "error_message": (
            f"spatial-input-response had unknown geometry_type={gtype!r}."
        ),
    }
