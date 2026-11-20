"""usgs_volcano hooks: the join two HANS lists answer an alert point through.

The irreducible step is the parse: the alert list and the geographic list are two
whole lists inner-joined on vnum, then bbox-filtered and severity-ranked in process,
because HANS has no server-side spatial query and publishes the place in a second
list rather than as a detail keyed off the first. A volcano keeps both lists' records
whole, the alert record under ``alert.`` and the geographic one under ``us_volcano.``."""

from __future__ import annotations

import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import hooks as _hooks
from ..._router.errors import router_empty_error, router_upstream_error

__all__ = ["parse_response"]

ALERT_LEVELS = ("NORMAL", "ADVISORY", "WATCH", "WARNING")
COLOR_CODES = ("GREEN", "YELLOW", "ORANGE", "RED")


def _alert_rank(alert_level: str | None) -> int:
    if not alert_level:
        return -1
    try:
        return ALERT_LEVELS.index(str(alert_level).strip().upper())
    except ValueError:
        return -1


def _color_rank(color_code: str | None) -> int:
    if not color_code:
        return -1
    try:
        return COLOR_CODES.index(str(color_code).strip().upper())
    except ValueError:
        return -1


def _decode(sc: str, raw: bytes, what: str) -> Any:
    import json

    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"USGS HANS {what} response is not valid JSON: {exc}")


def _by_vnum(sc: str, obj: Any, what: str) -> dict[str, dict[str, Any]]:
    """One HANS list as its records, whole, keyed by volcano number."""
    if not isinstance(obj, list):
        raise router_upstream_error(sc, f"USGS HANS {what} response is not a JSON list: type={type(obj).__name__}")
    out: dict[str, dict[str, Any]] = {}
    for el in obj:
        if not isinstance(el, dict):
            continue
        vnum = str(el.get("vnum") or "").strip()
        if vnum and vnum.lower() != "none":
            out[vnum] = el
    return out


def _position(record: dict[str, Any]) -> tuple[float, float] | None:
    """The record's own ``(lon, lat)``, or ``None`` where it states none a map can hold."""
    try:
        lat, lon = float(record.get("latitude")), float(record.get("longitude"))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon) and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return lon, lat


@_hooks.register_hook("usgs_volcano.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Inner-join alerts to coordinates on vnum, filter to bbox, sort by severity."""
    sc = spec.error_code_prefix
    alerts = _by_vnum(sc, _decode(sc, bodies[0] if bodies else b"", "monitored-volcano"), "monitored-volcano")
    coords = _by_vnum(sc, _decode(sc, bodies[1] if len(bodies) > 1 else b"", "US-volcano"), "US-volcano")

    if not alerts:
        raise router_empty_error(
            sc,
            "USGS HANS returned no monitored volcanoes. This is unexpected for the US volcano-"
            "observatory network; retry shortly.",
            spec.empty_error_suffix,
        )

    merged: list[dict[str, Any]] = []
    for vnum, alert in alerts.items():
        place = coords.get(vnum)
        point = _position(place) if place is not None else None
        if point is None:
            continue
        merged.append({"alert": alert, "us_volcano": place, "point": point,
                       "alert_rank": _alert_rank(alert.get("alert_level")),
                       "color_rank": _color_rank(alert.get("color_code")),
                       "name": str(place.get("volcano_name") or alert.get("volcano_name") or "").strip()})
    if not merged:
        raise router_empty_error(
            sc,
            "No monitored US volcano could be matched to a coordinate (the HANS alert list and "
            "geographic list did not join on vnum). Retry shortly.",
            spec.empty_error_suffix,
        )

    bbox = params.get("bbox")
    if bbox is not None:
        west, south, east, north = bbox
        merged = [r for r in merged if west <= r["point"][0] <= east and south <= r["point"][1] <= north]
    if not merged:
        raise router_empty_error(
            sc,
            "No monitored US volcano falls within the requested bbox. The US volcano-observatory "
            "network monitors Alaska/Aleutians, Hawaii, the Cascade Range, the western CONUS, and "
            "a few Pacific/Mariana islands -- widen the bbox or run a global (bbox-less) snapshot.",
            spec.empty_error_suffix,
        )

    merged.sort(key=lambda r: (-r["color_rank"], -r["alert_rank"], r["name"]))
    return [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": list(r["point"])},
         "properties": {"alert": r["alert"], "us_volcano": r["us_volcano"]},
         "produced": {"alert_rank": r["alert_rank"], "color_rank": r["color_rank"]}}
        for r in merged
    ]
