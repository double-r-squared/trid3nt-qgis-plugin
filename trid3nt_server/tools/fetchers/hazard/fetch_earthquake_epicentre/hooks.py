"""Earthquake-epicentre hooks: the ComCat event record read into its preferred origin.

The event GeoJSON's own point is the preferred origin (longitude, latitude, depth in km); its
properties carry the origin time in epoch milliseconds and the preferred magnitude.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import RequestPlan, register_hook

logger = logging.getLogger(__name__)

__all__ = ["build_request", "record"]


@register_hook("earthquake_epicentre.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """The ComCat request for the event record."""
    return [RequestPlan(url=str(spec.endpoints["event"].url),
                        params={"eventid": _event(spec, params), "format": "geojson"})]


@register_hook("earthquake_epicentre.record")
def record(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> dict[str, Any] | None:
    """The event record -> its preferred origin and magnitude."""
    event = _event(spec, params)
    sc = spec.error_code_prefix
    try:
        body = json.loads(bodies[0].decode("utf-8"))
        props = body.get("properties") or {}
        coordinates = (body.get("geometry") or {}).get("coordinates") or []
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, IndexError) as exc:
        raise router_upstream_error(sc, f"the ComCat record of {event!r} is no GeoJSON event: {exc}")
    location = [_finite(v) for v in coordinates[:3]]
    if len(location) != 3 or None in location or props.get("time") is None:
        raise router_empty_error(sc, f"event {event!r} states no usable preferred origin in ComCat "
                                 f"(point {coordinates!r}, time {props.get('time')!r}).", spec.empty_error_suffix)
    lon, lat, depth = location
    when = datetime.fromtimestamp(int(props["time"]) / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    logger.info("earthquake_epicentre: %s at %.4f, %.4f, %.1f km", event, lon, lat, depth)
    return {"event_id": str(body.get("id") or event), "longitude": lon, "latitude": lat, "depth_km": depth, "time": when,
            "magnitude": _finite(props.get("mag")), "magnitude_type": props.get("magType"),
            "title": props.get("title"), "status": props.get("status")}


def _finite(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _event(spec: SourceSpec, params: dict[str, Any]) -> str:
    event = str(params.get("event_id") or "").strip()
    if not event:
        raise router_input_error(spec.error_code_prefix, "event_id states no event: name the ComCat event id.",
                                 spec.input_error_suffix)
    return event
