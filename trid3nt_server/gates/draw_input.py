"""The DRAW gate: ask the user to draw ONE declared param's value on the canvas.

The gate never invents a geometry: no live session, a decline, or a wait that
runs out all produce the same typed refusal naming the param that stayed empty.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

from trid3nt_contracts import new_ulid
from trid3nt_contracts.ws import SpatialInputRequestPayload

logger = logging.getLogger("trid3nt_server.gates.draw_input")

__all__ = ["DrawGeometry", "DrawOutcome", "gate_draw_input"]

#: The four declarable draw kinds, and the client affordance each rides.
#: ``point``/``rectangle`` are the pick modes; the two multi-vertex kinds ride the
#: vector-draw surface with the purpose that shows only the tool they need.
DrawGeometry = Literal["point", "polyline", "polygon", "rectangle"]

_AFFORDANCE: dict[str, tuple[str, str | None]] = {
    "point": ("point", None),
    "rectangle": ("bbox", None),
    "polygon": ("vector_draw", "aoi"),
    "polyline": ("vector_draw", "line"),
}

#: Wait cap. Same read-decision TTL as every other card gate; running out is a
#: typed refusal, never a silent default.
_DEFAULT_TTL_SECONDS = 300


@dataclass(frozen=True, slots=True)
class DrawOutcome:
    """What the canvas gave back: a value, or a reason there is none."""

    value: Any = None
    reason: str | None = None

    @property
    def drawn(self) -> bool:
        return self.value is not None


async def gate_draw_input(
    *,
    tool_name: str,
    param: str,
    geometry: str,
    prompt: str,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
) -> DrawOutcome:
    """Present the draw card and WAIT for the geometry.

    Returns an unstamped value, or a ``reason`` for the caller's typed refusal."""
    affordance = _AFFORDANCE.get(geometry)
    if affordance is None:
        return DrawOutcome(reason=f"{geometry!r} is not a draw kind")
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    emitter = current_emitter()
    if emitter is None:
        return DrawOutcome(reason="there is no live map session to draw on")

    mode, purpose = affordance
    request_id = new_ulid()
    payload = SpatialInputRequestPayload(
        request_id=request_id,
        mode=mode,
        title=f"Draw {param.replace('_', ' ')}",
        description=(prompt or f"Draw the {geometry} that gives {param}.")[:1024],
        **({"purpose": purpose} if purpose else {}),
        default_timeout_seconds=int(ttl_seconds),
    )

    from trid3nt_server.server.spatial import (
        _pop_pending_spatial_input,
        _register_pending_spatial_input,
    )

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_spatial_input(emitter.session_id, request_id, fut)
    try:
        await emitter.send_envelope("spatial-input-request", payload)
        logger.info("draw gate emitted session=%s tool=%s param=%s geometry=%s "
                    "request_id=%s", emitter.session_id, tool_name, param, geometry,
                    request_id)
        response = await asyncio.wait_for(fut, timeout=float(ttl_seconds))
    except asyncio.TimeoutError:
        logger.warning("draw gate timeout session=%s tool=%s param=%s request_id=%s",
                       emitter.session_id, tool_name, param, request_id)
        return DrawOutcome(reason="nothing was drawn before the gate timed out")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - a bad reply is a refusal, never a crash
        logger.warning("draw gate failed session=%s tool=%s param=%s request_id=%s",
                       emitter.session_id, tool_name, param, request_id, exc_info=True)
        return DrawOutcome(reason=f"the drawn geometry could not be used: {exc}")
    finally:
        _pop_pending_spatial_input(request_id)

    if response.cancelled:
        return DrawOutcome(reason="the drawing was cancelled")
    value = await _value_from(response, geometry)
    if value is None:
        return DrawOutcome(
            reason=f"the reply carried no {geometry} geometry to read {param} from")
    logger.info("draw gate answered session=%s tool=%s param=%s geometry=%s",
                emitter.session_id, tool_name, param, geometry)
    return DrawOutcome(value=value)


async def _value_from(response: Any, geometry: str) -> Any:
    """The PARAM value inside the reply - a handful of vertices, never a dataset.

    Read through the SAME ingestions a typed wire value passes, so the drawn
    vocabulary cannot drift from the typed one; a point keeps the name it was
    picked under."""
    # The imports are function-local: the declarative library's interpreter
    # imports this module, and the package edge is the cycle.
    from trid3nt_server.inputs.point import point
    from trid3nt_server.workflows.runtime.user_input import (
        lonlat_bbox,
        polygon_ring,
        polyline_coords,
    )

    if geometry == "point":
        coords = response.coordinates or []
        if len(coords) < 2:
            return None
        return await point({"coordinates": coords[:2], "name": response.name},
                           label="the point")
    if geometry == "rectangle":
        coords = response.coordinates or []
        return lonlat_bbox(coords[:4] if len(coords) >= 4 else None,
                           label="the rectangle")

    from trid3nt_server.gates.spatial_input import parse_spatial_input_features

    if not isinstance(response.features, dict):
        return None
    parsed = parse_spatial_input_features(response.features)
    if geometry == "polyline":
        return polyline_coords(parsed.line_coords or None, label="the line")
    return polygon_ring(_first_ring(parsed.aoi_features) or None, label="the polygon")


def _first_ring(features: list[dict[str, Any]]) -> list[list[float]]:
    """The outer ring of the first drawn polygon, still in the reply's own shape."""
    for feature in features:
        coords = ((feature.get("geometry") or {}).get("coordinates") or [])
        if coords and isinstance(coords[0], list):
            return coords[0]
    return []
