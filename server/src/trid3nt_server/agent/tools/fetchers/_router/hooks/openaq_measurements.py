"""openaq_measurements hooks (chained-resolution mode, ADR 0063/0065): OpenAQ v3
global air-quality latest measurements (keyed).

The paginated-locations + per-location-latest + sensor->parameter join shape folds onto
the EXISTING offset-paging + enrich phases, zero new machinery. The MAIN FETCH is the
paginated ``/v3/locations`` sweep: ``build_request`` resolves the key (X-API-Key header)
and builds page 1; ``next_page`` walks pages until a short page or the 2000-station cap;
``parse_response`` collects the stations (id + coords + sensors). PHASE E is the
per-location latest fan-out: ``enrich_plan`` emits one ``/locations/{id}/latest`` ref per
station (best-effort, bounded); ``enrich_merge`` joins each latest sensor value to its
parameter/units via the station's sensor map, bbox-hard-filters, and EXPANDS into one
point per (station, parameter) latest reading.

The key resolves kwarg -> str secret_ref -> ``TRID3NT_OPENAQ_API_KEY`` env (the twin's
headless path; OpenAQ is NOT in TOOL_PROVIDER). No key -> credential-shaped
``OPENAQ_KEY_REQUIRED`` (recognised via the message-text credential detector, exactly as
the twin's own message is). The key is NEVER registered/required by this wave -- the
parity surface proved is the key-ABSENT typed error + input-validation errors. All I/O
(the locations pages, the latest fan-out, retry, cache, FGB serialize) stays router-owned.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_input_error, router_upstream_error

__all__ = ["build_request", "next_page", "parse_response", "enrich_plan", "enrich_merge"]

_OPENAQ_BASE = "https://api.openaq.org/v3"
_LOCATIONS_URL = f"{_OPENAQ_BASE}/locations"
_OPENAQ_KEY_ENV = "TRID3NT_OPENAQ_API_KEY"

_VALID_PARAMETERS = frozenset({"pm25", "pm10", "pm1", "no2", "no", "nox", "o3", "so2", "co", "co2", "bc", "ch4", "nh3"})
_DEFAULT_PARAMETERS = ("pm25", "pm10", "no2", "o3", "so2", "co")
_LOCATIONS_PAGE_SIZE = 200
_MAX_LOCATIONS = 2000

_PRESERVED_PROPERTIES = (
    "location_id", "location_name", "country", "parameter", "display_name",
    "value", "unit", "datetime_utc", "datetime_local", "sensor_id",
)


def _resolve_api_key(sc: str, params: dict[str, Any]) -> str:
    """kwarg -> str secret_ref -> env; credential-shaped OPENAQ_KEY_REQUIRED when none."""
    api_key = params.get("api_key")
    if api_key:
        return str(api_key)
    secret_ref = params.get("secret_ref")
    if isinstance(secret_ref, str) and secret_ref:
        return secret_ref
    env_key = os.environ.get(_OPENAQ_KEY_ENV)
    if env_key:
        return env_key
    raise router_input_error(
        sc,
        "no OpenAQ API key available: set the TRID3NT_OPENAQ_API_KEY env var (or add an "
        "OpenAQ api key). Register a free key at https://explore.openaq.org/ (account -> API keys).",
        "KEY_REQUIRED",
    )


def _validate_parameters(sc: str, raw: Any) -> list[str]:
    if raw is None:
        return list(_DEFAULT_PARAMETERS)
    items = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else None
    if items is None:
        raise router_input_error(sc, f"parameters must be a str or list of str; got {type(raw).__name__}", "INPUT_INVALID")
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise router_input_error(sc, f"parameters entries must be str; got {item!r}", "INPUT_INVALID")
        key = item.strip().lower()
        if not key:
            raise router_input_error(sc, "parameters entries must be non-empty", "INPUT_INVALID")
        if key not in _VALID_PARAMETERS:
            raise router_input_error(sc, f"parameter {item!r} is not a recognised OpenAQ pollutant; expected one of {sorted(_VALID_PARAMETERS)}", "INPUT_INVALID")
        if key not in out:
            out.append(key)
    return out or list(_DEFAULT_PARAMETERS)


def _key_headers(spec: SourceSpec, api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key, "User-Agent": spec.auth.user_agent, "Accept": "application/json"}


def _locations_plan(spec: SourceSpec, params: dict[str, Any], api_key: str, page: int) -> "_hooks.RequestPlan":
    w, s, e, n = (float(v) for v in params["bbox"])
    q = {"bbox": f"{w},{s},{e},{n}", "limit": str(_LOCATIONS_PAGE_SIZE), "page": str(page)}
    return _hooks.RequestPlan(url=_LOCATIONS_URL, params=q, headers=_key_headers(spec, api_key))


# --------------------------------------------------------------------------- #
# MAIN FETCH -- paginated /v3/locations sweep.
# --------------------------------------------------------------------------- #


@_hooks.register_hook("openaq_measurements.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Resolve the key + validate params, then build the locations page-1 request."""
    sc = spec.error_code_prefix
    _validate_parameters(sc, params.get("parameters"))
    api_key = _resolve_api_key(sc, params)  # raises KEY_REQUIRED pre-network when absent
    return [_locations_plan(spec, params, api_key, 1)]


def _results(body: bytes) -> list[dict[str, Any]]:
    try:
        obj = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    r = obj.get("results") if isinstance(obj, dict) else None
    return r if isinstance(r, list) else []


@_hooks.register_hook("openaq_measurements.next_page")
def next_page(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> "_hooks.RequestPlan | None":
    """Next locations page until a short page or the 2000-station cap."""
    last = _results(bodies[-1]) if bodies else []
    if len(last) < _LOCATIONS_PAGE_SIZE:
        return None
    total = sum(len(_results(b)) for b in bodies)
    if total >= _MAX_LOCATIONS:
        return None
    api_key = _resolve_api_key(spec.error_code_prefix, params)
    return _locations_plan(spec, params, api_key, len(bodies) + 1)


@_hooks.register_hook("openaq_measurements.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Collect stations (id + coords + sensors) as intermediate features for enrich."""
    stations: list[dict[str, Any]] = []
    for b in bodies:
        for st in _results(b):
            if isinstance(st, dict) and isinstance(st.get("id"), int):
                stations.append(st)
                if len(stations) >= _MAX_LOCATIONS:
                    break
        if len(stations) >= _MAX_LOCATIONS:
            break
    # Intermediate features: raw station stashed in properties (never serialized; the
    # enrich_merge output is the measurement rows).
    return [{"type": "Feature", "geometry": None, "properties": {"_station": st}} for st in stations]


# --------------------------------------------------------------------------- #
# PHASE E -- per-location latest fan-out + sensor->parameter join (expanding).
# --------------------------------------------------------------------------- #


@_hooks.register_hook("openaq_measurements.enrich_plan")
def enrich_plan(spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]]) -> list[tuple[str, "_hooks.RequestPlan"]]:
    """One /locations/{id}/latest ref per station."""
    api_key = _resolve_api_key(spec.error_code_prefix, params)
    plans: list[tuple[str, "_hooks.RequestPlan"]] = []
    for feat in features:
        st = (feat.get("properties") or {}).get("_station") or {}
        loc_id = st.get("id")
        if isinstance(loc_id, int):
            url = f"{_OPENAQ_BASE}/locations/{loc_id}/latest"
            plans.append((f"loc:{loc_id}", _hooks.RequestPlan(url=url, headers=_key_headers(spec, api_key))))
    return plans


def _sensor_param_map(station: dict[str, Any]) -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    for sensor in station.get("sensors") or []:
        if not isinstance(sensor, dict):
            continue
        sid = sensor.get("id")
        if not isinstance(sid, int):
            continue
        param = sensor.get("parameter") if isinstance(sensor.get("parameter"), dict) else {}
        out[sid] = {
            "parameter": str(param.get("name") or ""),
            "display_name": str(param.get("displayName") or param.get("name") or ""),
            "unit": str(param.get("units") or ""),
        }
    return out


@_hooks.register_hook("openaq_measurements.enrich_merge")
def enrich_merge(spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]], results: dict[str, Any]) -> list[dict[str, Any]]:
    """Join latest values to sensor params, bbox-filter, expand to one point per (station, param)."""
    sc = spec.error_code_prefix
    west, south, east, north = (float(v) for v in params["bbox"])
    param_set = set(_validate_parameters(sc, params.get("parameters")))
    out: list[dict[str, Any]] = []
    for feat in features:
        station = (feat.get("properties") or {}).get("_station") or {}
        loc_id = station.get("id")
        res = results.get(f"loc:{loc_id}") if isinstance(loc_id, int) else None
        body = getattr(res, "body", None) if res is not None else None
        latest_records = _results(body) if body else []  # failed ref -> no measurements (best-effort)
        loc_name = str(station.get("name") or "")
        c_obj = station.get("country") or {}
        country = str(c_obj.get("code") or c_obj.get("name") or "") if isinstance(c_obj, dict) else ""
        st_coords = station.get("coordinates") or {}
        st_lon = st_coords.get("longitude") if isinstance(st_coords, dict) else None
        st_lat = st_coords.get("latitude") if isinstance(st_coords, dict) else None
        smap = _sensor_param_map(station)
        for rec in latest_records:
            if not isinstance(rec, dict):
                continue
            meta = smap.get(rec.get("sensorsId")) if isinstance(rec.get("sensorsId"), int) else None
            if meta is None or not meta.get("parameter"):
                continue
            pname = meta["parameter"].lower()
            if pname not in param_set:
                continue
            rc = rec.get("coordinates") or {}
            lon = rc.get("longitude") if isinstance(rc, dict) and rc.get("longitude") is not None else st_lon
            lat = rc.get("latitude") if isinstance(rc, dict) and rc.get("latitude") is not None else st_lat
            if lon is None or lat is None:
                continue
            try:
                lon_f, lat_f = float(lon), float(lat)
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(lon_f) and math.isfinite(lat_f)):
                continue
            if not (west <= lon_f <= east and south <= lat_f <= north):
                continue
            v_raw = rec.get("value")
            try:
                v_f = float(v_raw) if v_raw is not None else None
            except (TypeError, ValueError):
                v_f = None
            dt = rec.get("datetime") if isinstance(rec.get("datetime"), dict) else {}
            props = {
                "location_id": int(loc_id) if isinstance(loc_id, int) else -1,
                "location_name": loc_name, "country": country, "parameter": pname,
                "display_name": meta["display_name"], "value": v_f, "unit": meta["unit"],
                "datetime_utc": str(dt.get("utc") or ""), "datetime_local": str(dt.get("local") or ""),
                "sensor_id": int(rec["sensorsId"]) if isinstance(rec.get("sensorsId"), int) else -1,
            }
            out.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon_f, lat_f]},
                        "properties": {c: props.get(c) for c in _PRESERVED_PROPERTIES}})
    return out
