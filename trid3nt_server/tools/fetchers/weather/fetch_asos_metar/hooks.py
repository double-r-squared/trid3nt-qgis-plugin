"""asos_metar hooks: IEM ASOS/METAR station observations.

PHASE R discovers stations: the router GETs the per-state network file for every state
overlapping the bbox, and the parse bbox-filters them into station ids plus the resolved
window. The MAIN FETCH is ONE bulk request, decoded to one Point per observation ROW."""

from __future__ import annotations

import io
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import hooks as _hooks
from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ...us_states import states_intersecting_bbox

__all__ = ["resolve_build", "resolve_parse", "build_request", "parse_response"]

_IEM_ASOS_CGI = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_IEM_NETWORK_GEOJSON = "https://mesonet.agron.iastate.edu/geojson/network/{state}_ASOS.geojson"

_MAX_STATIONS = 100

_DEFAULT_DATA_FIELDS = (
    "tmpf", "dwpf", "sknt", "drct", "gust", "alti", "mslp", "p01i",
    "vsby", "wxcodes", "skyc1", "skyl1",
)

_FGB_COLUMNS = (
    "station", "valid", "lon", "lat", "elevation",
    "tmpf", "dwpf", "sknt", "drct", "gust", "alti", "mslp", "p01i",
    "vsby", "wxcodes", "skyc1", "skyl1",
)

_IEM_ASOS_STATES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU",
)



def _headers(spec: SourceSpec) -> dict[str, str]:
    return {"User-Agent": spec.auth.user_agent}


def _parse_dt(sc: str, s: str, field: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M%z", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            continue
    raise router_input_error(
        sc, f"{field}={s!r} is not a parseable date/datetime string; use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ"
    )


def _resolve_window(sc: str, params: dict[str, Any]) -> tuple[datetime, datetime]:
    """Parse, default and gate the observation window."""
    now_utc = datetime.now(timezone.utc)
    end_raw = params.get("end_time")
    end_dt = now_utc if end_raw is None else _parse_dt(sc, str(end_raw), "end_time")
    start_raw = params.get("start_time")
    start_dt = (end_dt - timedelta(hours=24)) if start_raw is None else _parse_dt(sc, str(start_raw), "start_time")
    if start_dt > now_utc:
        raise router_input_error(
            sc, f"start_time={start_dt.isoformat()} is in the future; ASOS is an observational archive (no forecasts)"
        )
    if start_dt >= end_dt:
        raise router_input_error(
            sc, f"start_time={start_dt.isoformat()} must be before end_time={end_dt.isoformat()}"
        )
    return start_dt, end_dt


def _discovery_bbox(params: dict[str, Any]) -> tuple[float, float, float, float]:
    """The request bbox grown by search_radius_km, in degrees.

    An airport stands where the runway would fit rather than on the water or in
    the town a question is about, so a box the size of the area asked about
    routinely holds no station at all."""
    west, south, east, north = (float(v) for v in params["bbox"])
    pad = float(params.get("search_radius_km") or 0.0) / 111.0
    pad_lon = pad / max(0.2, math.cos(math.radians(0.5 * (south + north))))
    return (west - pad_lon, south - pad, east + pad_lon, north + pad)




@_hooks.register_hook("asos_metar.resolve_build")
def resolve_build(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Validate the window, then GET the per-state ASOS network GeoJSON for each
    state overlapping the bbox."""
    sc = spec.error_code_prefix
    _resolve_window(sc, params)  # validate window pre-network (future / inverted gates)
    bbox = _discovery_bbox(params)
    plans: list[_hooks.RequestPlan] = []
    for state in states_intersecting_bbox(bbox, among=_IEM_ASOS_STATES):
        plans.append(_hooks.RequestPlan(url=_IEM_NETWORK_GEOJSON.format(state=state), headers=_headers(spec)))
    if not plans:
        raise router_empty_error(
            sc,
            f"No IEM ASOS stations found inside bbox={list(params['bbox'])}; either no ASOS "
            f"stations cover this area or all are currently offline",
            spec.empty_error_suffix,
        )
    return plans


@_hooks.register_hook("asos_metar.resolve_parse")
def resolve_parse(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> dict[str, Any]:
    """bbox-filter every state's GeoJSON into station ids; merge ids + window."""
    import json

    sc = spec.error_code_prefix
    west, south, east, north = _discovery_bbox(params)
    station_ids: list[str] = []
    seen: set[str] = set()
    for raw in bodies:
        try:
            gj = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # one malformed state list never aborts discovery
        for feat in gj.get("features", []):
            coords = (feat.get("geometry") or {}).get("coordinates")
            if not coords or len(coords) < 2:
                continue
            lon, lat = float(coords[0]), float(coords[1])
            if not (west <= lon <= east and south <= lat <= north):
                continue
            sid = feat.get("id") or (feat.get("properties") or {}).get("sid")
            if not sid or sid in seen:
                continue
            seen.add(str(sid))
            station_ids.append(str(sid))
            if len(station_ids) >= _MAX_STATIONS:
                break
        if len(station_ids) >= _MAX_STATIONS:
            break
    if not station_ids:
        raise router_empty_error(
            sc,
            f"No IEM ASOS stations found inside bbox={list(params['bbox'])}; either no ASOS "
            f"stations cover this area or all are currently offline",
            spec.empty_error_suffix,
        )
    start_dt, end_dt = _resolve_window(sc, params)
    return {
        "_station_ids": station_ids,
        "start_time": start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_time": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }




@_hooks.register_hook("asos_metar.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Build the single ASOS CGI bulk-CSV request (repeated station + data params)."""
    sc = spec.error_code_prefix
    ids = params.get("_station_ids") or []
    start_dt = _parse_dt(sc, str(params["start_time"]), "start_time")
    end_dt = _parse_dt(sc, str(params["end_time"]), "end_time")
    q: dict[str, Any] = {
        "station": list(ids),
        "data": list(_DEFAULT_DATA_FIELDS),
        "year1": str(start_dt.year), "month1": str(start_dt.month), "day1": str(start_dt.day),
        "hour1": str(start_dt.hour), "minute1": str(start_dt.minute),
        "year2": str(end_dt.year), "month2": str(end_dt.month), "day2": str(end_dt.day),
        "hour2": str(end_dt.hour), "minute2": str(end_dt.minute),
        "tz": "UTC", "format": "onlycomma", "latlon": "yes", "elev": "yes",
        "missing": "null", "trace": "T", "direct": "no", "report_type": "3",
    }
    return [_hooks.RequestPlan(url=_IEM_ASOS_CGI, params=q, headers=_headers(spec))]




@_hooks.register_hook("asos_metar.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Decode the IEM ASOS comma-CSV into one point feature per observation row."""
    sc = spec.error_code_prefix
    import pandas as pd  # type: ignore[import-not-found]

    text = (bodies[0] if bodies else b"").decode("utf-8", errors="replace")
    clean = "\n".join(ln for ln in text.splitlines() if not ln.startswith("#"))
    try:
        df = pd.read_csv(
            io.StringIO(clean), dtype=str, low_memory=False,
            keep_default_na=False, na_values=["null", "M", ""],
        )
    except pd.errors.ParserError as exc:
        raise router_upstream_error(sc, f"ASOS CSV parse failed: {exc}")
    if df.empty:
        raise router_empty_error(
            sc, "IEM ASOS returned zero data rows for the requested stations/period",
            spec.empty_error_suffix,
        )
    missing = {"station", "valid"} - set(df.columns)
    if missing:
        raise router_upstream_error(sc, f"ASOS CSV missing required columns: {sorted(missing)}")
    if "lon" in df.columns and "lat" in df.columns:
        df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
        df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    else:
        raise router_empty_error(sc, "All ASOS observation rows lack valid coordinates", spec.empty_error_suffix)
    df = df.dropna(subset=["lon", "lat"]).copy()
    df = df[df["lon"].between(-180.0, 180.0) & df["lat"].between(-90.0, 90.0)].copy()
    if df.empty:
        raise router_empty_error(sc, "All ASOS observation rows have out-of-range coordinates", spec.empty_error_suffix)
    for col in ("elevation", "tmpf", "dwpf", "sknt", "drct", "gust", "alti", "mslp", "p01i", "vsby", "skyl1"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    keep = [c for c in _FGB_COLUMNS if c in df.columns]
    lons = df["lon"].tolist()
    lats = df["lat"].tolist()
    col_vals = {c: df[c].tolist() for c in keep}
    out: list[dict[str, Any]] = []
    for i in range(len(df)):
        lon, lat = float(lons[i]), float(lats[i])
        if not (math.isfinite(lon) and math.isfinite(lat)):
            continue
        props = {}
        for c in keep:
            v = col_vals[c][i]
            if isinstance(v, float) and math.isnan(v):
                v = None
            props[c] = v
        out.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": props,
        })
    return out
