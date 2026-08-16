"""usgs_volcano hooks: USGS HANS volcano alerts -> point features.

The irreducible step: a TWO-endpoint static request (the alert list keyed by vnum
+ the geographic list keyed by vnum) that the parse hook inner-joins on vnum, then
filters to the request bbox in-process (HANS has no server-side spatial query).
build_request returns both plans; the router GETs both and hands the two bodies to
parse_response in order.
"""

from __future__ import annotations

import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_empty_error, router_upstream_error

__all__ = ["build_request", "parse_response"]

MONITORED_URL = "https://volcanoes.usgs.gov/hans-public/api/volcano/getMonitoredVolcanoes"
US_VOLCANOES_URL = "https://volcanoes.usgs.gov/hans-public/api/volcano/getUSVolcanoes"

ALERT_LEVELS = ("NORMAL", "ADVISORY", "WATCH", "WARNING")
COLOR_CODES = ("GREEN", "YELLOW", "ORANGE", "RED")


@_hooks.register_hook("usgs_volcano.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """The two HANS endpoints (alert list + geographic list); bbox filters at parse."""
    ua = {"User-Agent": spec.auth.user_agent}
    return [
        _hooks.RequestPlan(url=MONITORED_URL, headers=ua),
        _hooks.RequestPlan(url=US_VOLCANOES_URL, headers=ua),
    ]


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


def _parse_alert_list(sc: str, obj: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(obj, list):
        raise router_upstream_error(sc, f"USGS HANS monitored-volcano response is not a JSON list: type={type(obj).__name__}")
    out: dict[str, dict[str, Any]] = {}
    for el in obj:
        if not isinstance(el, dict):
            continue
        vnum_raw = el.get("vnum")
        if vnum_raw in (None, "", "None"):
            continue
        vnum = str(vnum_raw).strip()
        if not vnum or vnum.lower() == "none":
            continue
        alert = el.get("alert_level")
        color = el.get("color_code")
        out[vnum] = {
            "vnum": vnum,
            "volcano_name": str(el.get("volcano_name") or "").strip() or None,
            "alert_level": (str(alert).strip().upper() if alert else None),
            "color_code": (str(color).strip().upper() if color else None),
            "observatory": str(el.get("obs_abbr") or "").strip() or None,
            "sent_utc": str(el.get("sent_utc") or "").strip() or None,
            "notice_url": str(el.get("notice_url") or "").strip() or None,
        }
    return out


def _parse_coord_list(sc: str, obj: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(obj, list):
        raise router_upstream_error(sc, f"USGS HANS US-volcano response is not a JSON list: type={type(obj).__name__}")
    out: dict[str, dict[str, Any]] = {}
    for el in obj:
        if not isinstance(el, dict):
            continue
        vnum_raw = el.get("vnum")
        if vnum_raw in (None, "", "None"):
            continue
        vnum = str(vnum_raw).strip()
        if not vnum or vnum.lower() == "none":
            continue
        try:
            lat = float(el.get("latitude"))
            lon = float(el.get("longitude"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        elev: float | None = None
        try:
            ev = el.get("elevation_meters")
            if ev is not None:
                fev = float(ev)
                elev = fev if math.isfinite(fev) else None
        except (TypeError, ValueError):
            elev = None
        out[vnum] = {
            "vnum": vnum,
            "volcano_name": str(el.get("volcano_name") or "").strip() or None,
            "lat": lat,
            "lon": lon,
            "elevation_m": elev,
            "region": str(el.get("region") or "").strip() or None,
            "volcano_url": str(el.get("volcano_url") or "").strip() or None,
            "nvews_threat": str(el.get("nvews_threat") or "").strip() or None,
        }
    return out


@_hooks.register_hook("usgs_volcano.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Inner-join alerts to coordinates on vnum, filter to bbox, sort by severity."""
    sc = spec.error_code_prefix
    alerts = _parse_alert_list(sc, _decode(sc, bodies[0] if bodies else b"", "monitored-volcano"))
    coords = _parse_coord_list(sc, _decode(sc, bodies[1] if len(bodies) > 1 else b"", "US-volcano"))

    if not alerts:
        raise router_empty_error(
            sc,
            "USGS HANS returned no monitored volcanoes. This is unexpected for the US volcano-"
            "observatory network; retry shortly.",
            spec.empty_error_suffix,
        )

    merged: list[dict[str, Any]] = []
    for vnum, a in alerts.items():
        c = coords.get(vnum)
        if c is None:
            continue
        merged.append(
            {
                "vnum": vnum,
                "volcano_name": c.get("volcano_name") or a.get("volcano_name"),
                "alert_level": a.get("alert_level"),
                "color_code": a.get("color_code"),
                "alert_rank": _alert_rank(a.get("alert_level")),
                "color_rank": _color_rank(a.get("color_code")),
                "elevation_m": c.get("elevation_m"),
                "region": c.get("region"),
                "observatory": a.get("observatory"),
                "sent_utc": a.get("sent_utc"),
                "notice_url": a.get("notice_url"),
                "volcano_url": c.get("volcano_url"),
                "nvews_threat": c.get("nvews_threat"),
                "lat": c["lat"],
                "lon": c["lon"],
            }
        )
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
        merged = [r for r in merged if west <= r["lon"] <= east and south <= r["lat"] <= north]
    if not merged:
        raise router_empty_error(
            sc,
            "No monitored US volcano falls within the requested bbox. The US volcano-observatory "
            "network monitors Alaska/Aleutians, Hawaii, the Cascade Range, the western CONUS, and "
            "a few Pacific/Mariana islands -- widen the bbox or run a global (bbox-less) snapshot.",
            spec.empty_error_suffix,
        )

    merged.sort(key=lambda r: (-(r["color_rank"]), -(r["alert_rank"]), r["volcano_name"] or ""))
    feats: list[dict[str, Any]] = []
    for r in merged:
        lon, lat = r.pop("lon"), r.pop("lat")
        r["alert_rank"] = int(r.get("alert_rank", -1))
        r["color_rank"] = int(r.get("color_rank", -1))
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": r})
    return feats
