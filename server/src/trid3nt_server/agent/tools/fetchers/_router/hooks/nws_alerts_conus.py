"""nws_alerts_conus hooks (chained-resolution mode, ADR 0063): NWS active alerts +
per-alert zone-polygon enrichment.

The main fetch is a single ``/alerts/active`` GET (nationwide, or ``?area=<state>``);
``parse_response`` decodes the FeatureCollection, applies the client-side event-type
filter, projects the preserved props, and stashes each alert's zone references. Alerts
that carry NULL inline geometry (zone/county watches) are then ENRICHED: the router
fetches each distinct zone URL best-effort (deduped, capped), and ``enrich_merge``
attaches the union of the resolved zone polygons so they draw on the map. An alert
whose zones cannot be resolved keeps its row with NULL geometry (never fabricated,
never silently dropped). All I/O stays router-owned.
"""

from __future__ import annotations

import json
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_input_error, router_upstream_error
from ...us_states import resolve_state_code

__all__ = ["build_request", "parse_response", "enrich_plan", "enrich_merge"]

_NWS_BASE = "https://api.weather.gov"
_ALERTS_URL = f"{_NWS_BASE}/alerts/active"
_VALID_STATUSES = frozenset({"actual", "exercise", "system", "test", "draft"})

#: Properties preserved from each NWS alert feature (the twin's exact set).
_PRESERVED_PROPERTIES = (
    "event", "headline", "description", "severity", "urgency", "certainty",
    "effective", "onset", "ends", "expires", "senderName", "sender",
    "category", "messageType", "status", "areaDesc", "instruction",
    "response", "id",
)


def _headers(spec: SourceSpec) -> dict[str, str]:
    return {"User-Agent": spec.auth.user_agent, "Accept": "application/geo+json"}


def _resolve_area(sc: str, sfx: str, area: Any) -> str | None:
    """LLM area -> 2-letter NWS code, or None (unscoped), or a typed input error."""
    if area is None:
        return None
    if not isinstance(area, str):
        raise router_input_error(sc, f"area must be a US state name or 2-letter code (str); got {type(area).__name__}", sfx)
    if not area.strip():
        return None
    code = resolve_state_code(area)
    if code is None:
        raise router_input_error(
            sc,
            f"area={area!r} is not a recognized US state/territory name or "
            f"2-letter code. For county-FIPS or bbox scoping use "
            f"fetch_nws_event; omit area entirely for the nationwide sweep.",
            sfx,
        )
    return code


@_hooks.register_hook("nws_alerts_conus.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """The single /alerts/active GET (status + optional server-side ?area= state filter)."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    status = params.get("status") or "actual"
    if status not in _VALID_STATUSES:
        raise router_input_error(sc, f"status={status!r} not in {sorted(_VALID_STATUSES)}", sfx)
    area_code = _resolve_area(sc, sfx, params.get("area"))
    q: dict[str, Any] = {}
    if area_code:
        q["area"] = area_code
    q["status"] = status
    return [_hooks.RequestPlan(url=_ALERTS_URL, params=q, headers=_headers(spec))]


def _ugc_to_zone_url(ugc: Any) -> str | None:
    if not isinstance(ugc, str):
        return None
    code = ugc.strip().upper()
    if len(code) < 3:
        return None
    kind = code[2]
    if kind == "Z":
        return f"{_NWS_BASE}/zones/forecast/{code}"
    if kind == "C":
        return f"{_NWS_BASE}/zones/county/{code}"
    return None


def _zone_urls_for_feature(props: dict[str, Any]) -> list[str]:
    """Ordered, de-duplicated zone API URLs (affectedZones primary, geocode.UGC fallback)."""
    seen: set[str] = set()
    urls: list[str] = []
    affected = props.get("affectedZones")
    if isinstance(affected, list):
        for u in affected:
            if isinstance(u, str) and u.strip():
                clean = u.strip()
                if clean not in seen:
                    seen.add(clean)
                    urls.append(clean)
    geocode = props.get("geocode")
    if isinstance(geocode, dict):
        ugc_list = geocode.get("UGC")
        if isinstance(ugc_list, list):
            for ugc in ugc_list:
                url = _ugc_to_zone_url(ugc)
                if url is not None and url not in seen:
                    seen.add(url)
                    urls.append(url)
    return urls


def _matches_event_types(props: dict[str, Any], allowed: set[str]) -> bool:
    ev = props.get("event")
    return isinstance(ev, str) and ev in allowed


@_hooks.register_hook("nws_alerts_conus.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Decode the FeatureCollection, event-type filter, project props, stash zone refs."""
    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    if not raw:
        return []
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"NWS returned non-JSON: {exc}")
    if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
        raise router_upstream_error(
            sc,
            f"NWS response is not a GeoJSON FeatureCollection: "
            f"type={obj.get('type') if isinstance(obj, dict) else type(obj).__name__!r}",
        )
    event_types = params.get("event_types") or []
    allowed = {e.strip() for e in event_types if isinstance(e, str) and e.strip()}

    out: list[dict[str, Any]] = []
    for feat in obj.get("features") or []:
        if not isinstance(feat, dict):
            continue
        props = feat.get("properties") or {}
        if allowed and not _matches_event_types(props, allowed):
            continue
        row: dict[str, Any] = {}
        for key in _PRESERVED_PROPERTIES:
            v = props.get(key)
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            row[key] = v
        out.append({
            "type": "Feature",
            "geometry": feat.get("geometry"),
            "properties": row,
            "_zone_urls": _zone_urls_for_feature(props),
        })
    return out


@_hooks.register_hook("nws_alerts_conus.enrich_plan")
def enrich_plan(spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]]) -> list[tuple[str, "_hooks.RequestPlan"]]:
    """Emit (zone_url, GET) for every zone of every NULL-geometry alert (mode dedups + caps)."""
    plans: list[tuple[str, "_hooks.RequestPlan"]] = []
    for feat in features:
        if feat.get("geometry"):
            continue
        for url in feat.get("_zone_urls") or []:
            plans.append((url, _hooks.RequestPlan(url=url, headers=_headers(spec))))
    return plans


def _union_geometries(geoms: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0]
    polygons: list[Any] = []
    for geom in geoms:
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if gtype == "Polygon" and isinstance(coords, list):
            polygons.append(coords)
        elif gtype == "MultiPolygon" and isinstance(coords, list):
            polygons.extend(coords)
    if not polygons:
        return None
    return {"type": "MultiPolygon", "coordinates": polygons}


def _zone_geometry(body: bytes | None) -> dict[str, Any] | None:
    if not body:
        return None
    try:
        obj = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    geom = obj.get("geometry")
    if not isinstance(geom, dict) or not geom.get("type") or not geom.get("coordinates"):
        return None
    return geom


@_hooks.register_hook("nws_alerts_conus.enrich_merge")
def enrich_merge(
    spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]], results: dict[str, Any]
) -> list[dict[str, Any]]:
    """Attach the union of each NULL-geometry alert's resolved zone polygons; keep every row."""
    out: list[dict[str, Any]] = []
    for feat in features:
        props = feat.get("properties") or {}
        if feat.get("geometry"):
            out.append({"type": "Feature", "geometry": feat["geometry"], "properties": props})
            continue
        zone_geoms: list[dict[str, Any]] = []
        for url in feat.get("_zone_urls") or []:
            res = results.get(url)
            body = getattr(res, "body", None) if res is not None else None
            geom = _zone_geometry(body)
            if geom is not None:
                zone_geoms.append(geom)
        unioned = _union_geometries(zone_geoms)
        # Kept either way (property table survives); NULL geometry when unresolved.
        out.append({"type": "Feature", "geometry": unioned, "properties": props})
    return out
