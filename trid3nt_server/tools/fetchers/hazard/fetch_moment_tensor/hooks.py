"""Moment-tensor hooks: the ComCat event record read into every published solution.

The products ride in the event GeoJSON, each with flat string properties; the centroid is the
product's ``derived-*`` origin, since ``latitude``/``longitude``/``depth`` are the hypocentre the
tensor was attached to.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import RequestPlan, register_hook

logger = logging.getLogger(__name__)

__all__ = ["build_request", "record"]


@register_hook("moment_tensor.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """The ComCat request for the event record."""
    return [RequestPlan(url=str(spec.endpoints["event"].url),
                        params={"eventid": _event(spec, params), "format": "geojson"})]


@register_hook("moment_tensor.record")
def record(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> dict[str, Any] | None:
    """The event record -> every moment-tensor solution it holds."""
    event = _event(spec, params)
    sc = spec.error_code_prefix
    try:
        products = ((json.loads(bodies[0].decode("utf-8")).get("properties") or {})
                    .get("products") or {}).get("moment-tensor") or []
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, IndexError) as exc:
        raise router_upstream_error(sc, f"the ComCat record of {event!r} is no GeoJSON event: {exc}")
    if not products:
        raise router_empty_error(sc, f"event {event!r} holds no moment-tensor product in ComCat.",
                                 spec.empty_error_suffix)
    solutions = [_solution(product, sc) for product in products]
    logger.info("moment_tensor: %s %d solution(s)", event, len(solutions))
    return {"event_id": event, "solutions": solutions}


def _number(props: dict[str, Any], key: str, code: str, sc: str, required: bool = True) -> float | None:
    raw = props.get(key)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value):
        if required:
            raise router_upstream_error(sc, f"the moment-tensor product {code!r} states no usable {key} ({raw!r}).")
        return None
    return value


def _solution(product: dict[str, Any], sc: str) -> dict[str, Any]:
    props = product.get("properties") or {}
    code = str(product.get("code") or "")
    planes = [{"strike": _number(props, f"nodal-plane-{n}-strike", code, sc),
               "dip": _number(props, f"nodal-plane-{n}-dip", code, sc),
               "rake": _number(props, f"nodal-plane-{n}-rake", code, sc)} for n in (1, 2)]
    lon = _number(props, "derived-longitude", code, sc, required=False)
    lat = _number(props, "derived-latitude", code, sc, required=False)
    depth = _number(props, "derived-depth", code, sc, required=False)
    centroid = ({"longitude": lon, "latitude": lat, "depth_km": depth}
                if None not in (lon, lat, depth) else None)
    return {"code": code, "source": str(product.get("source") or ""),
            "magnitude": _number(props, "derived-magnitude", code, sc, required=False),
            "magnitude_type": props.get("derived-magnitude-type"),
            "scalar_moment_nm": _number(props, "scalar-moment", code, sc),
            "centroid": centroid, "planes": planes,
            "duration_s": _number(props, "sourcetime-duration", code, sc, required=False),
            "updated_ms": int(product.get("updateTime") or 0)}


def _event(spec: SourceSpec, params: dict[str, Any]) -> str:
    event = str(params.get("event_id") or "").strip()
    if not event:
        raise router_input_error(spec.error_code_prefix, "event_id states no event: name the ComCat event id.",
                                 spec.input_error_suffix)
    return event
