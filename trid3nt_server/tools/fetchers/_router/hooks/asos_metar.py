"""asos_metar hooks (chained-resolution mode/0065): Iowa State IEM
ASOS/METAR station observations.

The station-observations shape folds onto the EXISTING resolve phase + main fetch,
zero new machinery. PHASE R (``resolve_build`` / ``resolve_parse``) is the
multi-state station discovery: the router GETs the per-state ASOS network GeoJSON
for every state overlapping the bbox, and ``resolve_parse`` bbox-filters the
features into station ids + the resolved observation window, merging both into
``params``. The MAIN FETCH is ONE bulk IEM CGI request for all discovered stations
over the window (``build_request``); ``parse_response`` decodes the comma-CSV into
one Point feature per OBSERVATION ROW. All I/O (the discovery GETs, the bulk CSV
download, retry, cache, FGB serialize) stays router-owned; these hooks only compute.
"""

from __future__ import annotations

import io
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_empty_error, router_input_error, router_upstream_error

__all__ = ["resolve_build", "resolve_parse", "build_request", "parse_response"]

_IEM_ASOS_CGI = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
_IEM_NETWORK_GEOJSON = "https://mesonet.agron.iastate.edu/geojson/network/{state}_ASOS.geojson"

_MAX_STATIONS = 100

_DEFAULT_DATA_FIELDS = (
    "tmpf", "dwpf", "sknt", "drct", "gust", "alti", "mslp",
    "vsby", "wxcodes", "skyc1", "skyl1",
)

_FGB_COLUMNS = (
    "station", "valid", "lon", "lat", "elevation",
    "tmpf", "dwpf", "sknt", "drct", "gust", "alti", "mslp",
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

#: Generous (~0.5 deg pad) per-state bboxes to skip non-overlapping states.
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
    "VI": (-65.1, 17.7, -64.6, 18.4), "GU": (144.6, 13.2, 145.0, 13.7),
}


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
    """Parse + default + gate the observation window (the twin's body contract)."""
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


def _overlaps(bbox: tuple[float, ...], sb: tuple[float, float, float, float]) -> bool:
    w1, s1, e1, n1 = bbox
    w2, s2, e2, n2 = sb
    return not (e1 < w2 or w1 > e2 or n1 < s2 or s1 > n2)


# --------------------------------------------------------------------------- #
# PHASE R -- multi-state station discovery (per-state GeoJSON -> station ids).
# --------------------------------------------------------------------------- #


@_hooks.register_hook("asos_metar.resolve_build")
def resolve_build(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Validate the window, then GET the per-state ASOS network GeoJSON for each
    state overlapping the bbox."""
    sc = spec.error_code_prefix
    _resolve_window(sc, params)  # validate window pre-network (future / inverted gates)
    bbox = tuple(float(v) for v in params["bbox"])
    plans: list[_hooks.RequestPlan] = []
    for state in _IEM_ASOS_STATES:
        sb = _STATE_BBOX.get(state)
        if sb is not None and _overlaps(bbox, sb):
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
    west, south, east, north = (float(v) for v in params["bbox"])
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


# --------------------------------------------------------------------------- #
# MAIN FETCH -- one bulk IEM CGI CSV request for all stations over the window.
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# PARSE -- comma-CSV -> one Point feature per observation row.
# --------------------------------------------------------------------------- #


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
    for col in ("elevation", "tmpf", "dwpf", "sknt", "drct", "gust", "alti", "mslp", "vsby", "skyl1"):
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
