"""raws_weather hooks (chained-resolution mode, ADR 0063/0065): Iowa Mesonet RAWS
fire-weather stations.

The nested station x day observation shape folds onto the EXISTING resolve + enrich
phases, zero new machinery. PHASE R (``resolve_build`` / ``resolve_parse``) is the
multi-state RAWS discovery: the router GETs the per-state DCP network GeoJSON for
every state overlapping the bbox, and ``resolve_parse`` keeps the RAWS-named stations
and merges them + the resolved date window into ``params`` (pre-cache-key, so the
now-relative default window enters the cache key -- the twin's contract). The MAIN
FETCH is a no-op (``build_request`` returns ``[]``; ``parse_response`` synthesizes one
station feature per resolved station). PHASE E (``enrich_plan`` / ``enrich_merge``) is
the per-station-per-day obhistory matrix: best-effort, deduped, bounded; ``enrich_merge``
EXPANDS the station features into one point per observation row (a station with no
obs contributes no rows; all-empty -> RAWS_WEATHER_EMPTY). All I/O stays router-owned.
"""

from __future__ import annotations

import json
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_empty_error, router_input_error

__all__ = ["resolve_build", "resolve_parse", "build_request", "parse_response", "enrich_plan", "enrich_merge"]

_IEM_NETWORK_GEOJSON = "https://mesonet.agron.iastate.edu/geojson/network/{state}_DCP.geojson"
_IEM_OBHISTORY_URL = "https://mesonet.agron.iastate.edu/api/1/obhistory.json"

_MAX_STATIONS = 50
_MAX_DATE_RANGE_DAYS = 14
_RAWS_NAME_MARKER = "RAWS"

_FGB_COLUMNS = (
    "station", "station_name", "state", "utc_valid", "lon", "lat", "elevation",
    "tmpf", "dwpf", "relh", "sknt", "drct", "gust", "solar_rad", "precip_in",
)

_IEM_DCP_STATES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC", "PR",
)

_STATE_BBOX: dict[str, tuple[float, float, float, float]] = {
    "AL": (-88.6, 30.1, -84.8, 35.0), "AK": (-180.0, 51.2, -129.9, 71.4),
    "AZ": (-114.9, 31.3, -109.0, 37.0), "AR": (-94.7, 33.0, -89.6, 36.5),
    "CA": (-124.5, 32.5, -114.1, 42.0), "CO": (-109.1, 36.9, -102.0, 41.0),
    "CT": (-73.7, 40.9, -71.7, 42.1), "DE": (-75.8, 38.4, -75.0, 39.9),
    "FL": (-87.7, 24.4, -79.9, 31.0), "GA": (-85.6, 30.4, -80.8, 35.0),
    "HI": (-160.3, 18.9, -154.8, 22.2), "ID": (-117.3, 41.9, -111.0, 49.0),
    "IL": (-91.5, 36.9, -87.0, 42.5), "IN": (-88.1, 37.7, -84.7, 41.8),
    "IA": (-96.7, 40.4, -90.1, 43.5), "KS": (-102.1, 36.9, -94.6, 40.0),
    "KY": (-89.6, 36.5, -81.9, 39.1), "LA": (-94.1, 28.9, -88.8, 33.0),
    "ME": (-71.1, 43.0, -66.9, 47.5), "MD": (-79.5, 37.9, -75.0, 39.7),
    "MA": (-73.5, 41.2, -69.9, 42.9), "MI": (-90.5, 41.7, -82.4, 48.3),
    "MN": (-97.2, 43.5, -89.5, 49.4), "MS": (-91.7, 30.2, -88.1, 35.0),
    "MO": (-95.8, 35.9, -89.1, 40.6), "MT": (-116.1, 44.4, -104.0, 49.0),
    "NE": (-104.1, 40.0, -95.3, 43.0), "NV": (-120.0, 35.0, -114.0, 42.0),
    "NH": (-72.6, 42.7, -70.6, 45.3), "NJ": (-75.6, 38.9, -73.9, 41.4),
    "NM": (-109.1, 31.3, -103.0, 37.0), "NY": (-79.8, 40.5, -71.8, 45.0),
    "NC": (-84.3, 33.8, -75.4, 36.6), "ND": (-104.1, 45.9, -96.6, 49.0),
    "OH": (-84.8, 38.4, -80.5, 42.3), "OK": (-103.0, 33.6, -94.4, 37.0),
    "OR": (-124.7, 41.9, -116.5, 46.3), "PA": (-80.5, 39.7, -74.7, 42.3),
    "RI": (-71.9, 41.1, -71.1, 42.0), "SC": (-83.4, 32.0, -78.5, 35.2),
    "SD": (-104.1, 42.5, -96.4, 45.9), "TN": (-90.3, 34.9, -81.6, 36.7),
    "TX": (-106.6, 25.8, -93.5, 36.5), "UT": (-114.1, 37.0, -109.0, 42.0),
    "VT": (-73.4, 42.7, -71.5, 45.0), "VA": (-83.7, 36.5, -75.3, 39.5),
    "WA": (-124.8, 45.5, -116.9, 49.0), "WV": (-82.7, 37.2, -77.7, 40.6),
    "WI": (-92.9, 42.5, -86.8, 47.1), "WY": (-111.1, 40.9, -104.0, 45.0),
    "DC": (-77.2, 38.8, -76.9, 39.0), "PR": (-67.3, 17.9, -65.2, 18.6),
}


def _headers(spec: SourceSpec) -> dict[str, str]:
    return {"User-Agent": spec.auth.user_agent}


def _parse_date(sc: str, s: str, field: str) -> _date:
    for _fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(s).strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
    raise router_input_error(sc, f"{field}={s!r} is not a parseable date; use YYYY-MM-DD")


def _resolve_dates(sc: str, params: dict[str, Any]) -> tuple[_date, _date]:
    """Parse + default + gate the date window (the twin's body contract)."""
    today = datetime.now(timezone.utc).date()
    end_raw = params.get("end_time")
    end_d = today if end_raw is None else _parse_date(sc, str(end_raw), "end_time")
    start_raw = params.get("start_time")
    start_d = (end_d - timedelta(days=1)) if start_raw is None else _parse_date(sc, str(start_raw), "start_time")
    if start_d > today:
        raise router_input_error(sc, f"start_time={start_d} is in the future; RAWS is an observational archive (no forecasts)")
    if start_d > end_d:
        raise router_input_error(sc, f"start_time={start_d} must be on or before end_time={end_d}")
    n_days = (end_d - start_d).days + 1
    if n_days > _MAX_DATE_RANGE_DAYS:
        raise router_input_error(sc, f"Date range of {n_days} days exceeds maximum ({_MAX_DATE_RANGE_DAYS}); split into smaller windows or reduce the date range")
    return start_d, end_d


def _overlaps(bbox: tuple[float, ...], sb: tuple[float, float, float, float]) -> bool:
    w1, s1, e1, n1 = bbox
    w2, s2, e2, n2 = sb
    return not (e1 < w2 or w1 > e2 or n1 < s2 or s1 > n2)


# --------------------------------------------------------------------------- #
# PHASE R -- multi-state RAWS discovery + date-window resolution.
# --------------------------------------------------------------------------- #


@_hooks.register_hook("raws_weather.resolve_build")
def resolve_build(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Validate the window, then GET the per-state DCP GeoJSON for each overlapping state."""
    sc = spec.error_code_prefix
    _resolve_dates(sc, params)  # validate pre-network
    bbox = tuple(float(v) for v in params["bbox"])
    plans: list[_hooks.RequestPlan] = []
    for state in _IEM_DCP_STATES:
        sb = _STATE_BBOX.get(state)
        if sb is not None and _overlaps(bbox, sb):
            plans.append(_hooks.RequestPlan(url=_IEM_NETWORK_GEOJSON.format(state=state), headers=_headers(spec)))
    if not plans:
        raise router_empty_error(
            sc,
            f"No IEM-archived RAWS stations found inside bbox={list(params['bbox'])}; "
            f"RAWS coverage is heaviest in the western US fire belt",
            spec.empty_error_suffix,
        )
    return plans


@_hooks.register_hook("raws_weather.resolve_parse")
def resolve_parse(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> dict[str, Any]:
    """Keep RAWS-named stations in the bbox; merge stations + resolved dates."""
    sc = spec.error_code_prefix
    bbox = tuple(float(v) for v in params["bbox"])
    west, south, east, north = bbox
    # bodies arrive in resolve_build's per-overlapping-state order, so the state
    # each station's obhistory network= belongs to is the body's owning state.
    overlap_states = [s for s in _IEM_DCP_STATES
                      if _STATE_BBOX.get(s) is not None and _overlaps(bbox, _STATE_BBOX[s])]
    stations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for state, raw in zip(overlap_states, bodies):
        try:
            gj = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        for feat in gj.get("features", []):
            coords = (feat.get("geometry") or {}).get("coordinates")
            if not coords or len(coords) < 2:
                continue
            lon, lat = float(coords[0]), float(coords[1])
            if not (west <= lon <= east and south <= lat <= north):
                continue
            props = feat.get("properties") or {}
            sname = props.get("sname", "")
            if _RAWS_NAME_MARKER not in sname.upper():
                continue
            sid = feat.get("id") or props.get("sid")
            if not sid or str(sid) in seen:
                continue
            seen.add(str(sid))
            stations.append({
                "sid": str(sid), "lon": lon, "lat": lat, "sname": sname,
                "state": state, "elevation": props.get("elevation"),
            })
            if len(stations) >= _MAX_STATIONS:
                break
        if len(stations) >= _MAX_STATIONS:
            break
    if not stations:
        raise router_empty_error(
            sc,
            f"No IEM-archived RAWS stations found inside bbox={list(params['bbox'])}; "
            f"RAWS coverage is heaviest in the western US fire belt",
            spec.empty_error_suffix,
        )
    start_d, end_d = _resolve_dates(sc, params)
    return {"_stations": stations, "start_date": start_d.isoformat(), "end_date": end_d.isoformat()}


# --------------------------------------------------------------------------- #
# MAIN FETCH -- no round trip; synthesize one feature per resolved station.
# --------------------------------------------------------------------------- #


@_hooks.register_hook("raws_weather.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """No main-fetch round trip: the stations are already resolved in PHASE R."""
    return []


@_hooks.register_hook("raws_weather.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Synthesize one station Point feature per resolved station (obs land in enrich)."""
    feats: list[dict[str, Any]] = []
    for st in params.get("_stations") or []:
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [st["lon"], st["lat"]]},
            "properties": {"sid": st["sid"], "sname": st.get("sname", ""), "state": st.get("state", ""),
                           "elevation": st.get("elevation"), "lon": st["lon"], "lat": st["lat"]},
        })
    return feats


# --------------------------------------------------------------------------- #
# PHASE E -- per-station-per-day obhistory (best-effort) -> obs rows.
# --------------------------------------------------------------------------- #


def _all_dates(start_iso: str, end_iso: str) -> list[str]:
    d0 = _date.fromisoformat(start_iso)
    d1 = _date.fromisoformat(end_iso)
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


@_hooks.register_hook("raws_weather.enrich_plan")
def enrich_plan(spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]]) -> list[tuple[str, "_hooks.RequestPlan"]]:
    """One obhistory ref per (station, day)."""
    dates = _all_dates(str(params["start_date"]), str(params["end_date"]))
    plans: list[tuple[str, "_hooks.RequestPlan"]] = []
    for feat in features:
        props = feat.get("properties") or {}
        sid = props.get("sid")
        state = props.get("state") or ""
        network = f"{state}_DCP"
        for d in dates:
            q = {"station": sid, "network": network, "date": d}
            plans.append((f"{sid}:{d}", _hooks.RequestPlan(url=_IEM_OBHISTORY_URL, params=q, headers=_headers(spec))))
    return plans


@_hooks.register_hook("raws_weather.enrich_merge")
def enrich_merge(spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]], results: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand station features into one point per observation row (best-effort join)."""
    sc = spec.error_code_prefix
    dates = _all_dates(str(params["start_date"]), str(params["end_date"]))
    out: list[dict[str, Any]] = []
    for feat in features:
        props = feat.get("properties") or {}
        sid = props.get("sid")
        lon, lat = props.get("lon"), props.get("lat")
        for d in dates:
            res = results.get(f"{sid}:{d}")
            body = getattr(res, "body", None) if res is not None else None
            if not body:
                continue  # failed / capped ref -> that station-day contributes no obs
            try:
                obs_list = (json.loads(body.decode("utf-8")) or {}).get("data", [])
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            for obs in obs_list:
                if lon is None or lat is None:
                    continue
                row = {
                    "station": sid, "station_name": props.get("sname", ""), "state": props.get("state", ""),
                    "utc_valid": obs.get("utc_valid"), "lon": lon, "lat": lat,
                    "elevation": _num(props.get("elevation")),
                    "tmpf": _num(obs.get("tmpf")), "dwpf": _num(obs.get("dwpf")),
                    "relh": _num(obs.get("URHRGZZ")), "sknt": _num(obs.get("sknt")), "drct": _num(obs.get("drct")),
                    "gust": _num(obs.get("VBIRGZZ")), "solar_rad": _num(obs.get("XRIRGZZ")), "precip_in": _num(obs.get("PCIRGZZ")),
                }
                out.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [float(lon), float(lat)]},
                    "properties": {c: row.get(c) for c in _FGB_COLUMNS},
                })
    if not out:
        raise router_empty_error(sc, "No RAWS observations collected for any station in the bbox/window", spec.empty_error_suffix)
    return out


def _num(v: Any) -> Any:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
