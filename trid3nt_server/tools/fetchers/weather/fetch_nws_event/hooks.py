"""nws_event hooks: NWS active alerts as alert polygons.

The one irreducible step is the request construction -- canonicalizing the area string
to a 2-letter state code or a 5-digit county FIPS, then building the query -- and the
FeatureCollection decode."""

# The decode projects each alert to the preserved property set, JSON-coerces the nested
# props, and DROPS the geometry-less zone-only alerts the writer cannot write. An empty
# result is a header-only FGB, not a typed empty. ``area`` is a string only: the tool
# schema collapses a union to str, so a bbox tuple could never reach here anyway.

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import hooks as _hooks
from ..._router.errors import router_input_error, router_upstream_error
from ...us_states import NWS_AREA_CODES, resolve_state_code

__all__ = ["build_request", "parse_response"]

NWS_ALERTS_URL = "https://api.weather.gov/alerts/active"

#: 5-digit county FIPS pattern (NWS ``?area=`` accepts a FIPS the same as a
#: state code via zone lookup).
_FIPS_PATTERN = re.compile(r"^\d{5}$")

#: Properties preserved from each NWS alert feature.
_PRESERVED_PROPERTIES = (
    "event", "headline", "description", "severity", "urgency", "certainty",
    "effective", "onset", "ends", "expires", "senderName", "sender",
    "category", "messageType", "status", "areaDesc", "instruction",
    "response", "id",
)


def _canonicalize_area(sc: str, sfx: str, area: Any) -> str:
    """Reduce the ``area`` string to the query's own value: a 2-letter state or
    marine-zone code, a 5-digit county FIPS, or a full state name resolved to its code.
    An unrecognized value is a source-stamped input error."""
    s = str(area).strip().upper()
    if _FIPS_PATTERN.match(s):
        return s
    if s in NWS_AREA_CODES:
        return s
    name_code = resolve_state_code(str(area))
    if name_code is not None:
        return name_code
    raise router_input_error(
        sc,
        f"area={area!r} is not a recognized US state name, 2-letter state code, "
        f"or 5-digit county FIPS",
        sfx,
    )


@_hooks.register_hook("nws_event.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Canonicalize the area and build the /alerts/active query URL (single GET)."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    area_value = _canonicalize_area(sc, sfx, params.get("area"))
    status = params.get("status") or "actual"
    message_type = params.get("message_type") or "alert"
    event_types = params.get("event_types") or []

    query: list[tuple[str, str]] = [
        ("area", area_value),
        ("status", status),
        ("message_type", message_type),
    ]
    for et in event_types:
        query.append(("event", et))
    url = NWS_ALERTS_URL + "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
    return [
        _hooks.RequestPlan(
            url=url,
            headers={"User-Agent": spec.auth.user_agent, "Accept": "application/geo+json"},
        )
    ]


@_hooks.register_hook("nws_event.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Decode the NWS FeatureCollection, project the preserved props, drop null geometry."""
    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    if not raw:
        return []
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"NWS response is not valid JSON: {exc}")
    if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
        raise router_upstream_error(
            sc,
            f"NWS response is not a GeoJSON FeatureCollection: "
            f"type={obj.get('type') if isinstance(obj, dict) else type(obj).__name__!r}",
        )

    out: list[dict[str, Any]] = []
    for feat in obj.get("features") or []:
        if not isinstance(feat, dict):
            continue
        geom = feat.get("geometry")
        # NWS zone-based alerts (statewide watches) carry NULL geometry; pyogrio
        # rejects them and they have no map footprint, so they are dropped.
        if geom is None:
            continue
        props = feat.get("properties") or {}
        row: dict[str, Any] = {}
        for key in _PRESERVED_PROPERTIES:
            v = props.get(key)
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            row[key] = v
        out.append({"type": "Feature", "geometry": geom, "properties": row})
    return out
