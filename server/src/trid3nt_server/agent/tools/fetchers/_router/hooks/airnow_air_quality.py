"""airnow_air_quality hooks (tier-3 http_json, ADR 0056/0065): EPA AirNow current-hour
AQI observations (keyed).

A single bounded-box GET folds onto the EXISTING http_json main-fetch path, zero new
machinery. ``build_request`` resolves the API key (api_key kwarg -> str secret_ref ->
``TRID3NT_AIRNOW_API_KEY`` env -- the twin's headless path; AirNow is NOT in TOOL_PROVIDER,
so this is byte-identical to the twin's runtime), validates the pollutant filter, and
builds the ``aq/data`` query with the key injected. When NO key resolves it raises a
credential-shaped ``AIRNOW_MISSING_KEY`` (``is_credential_shaped_error`` recognises the
``_MISSING_KEY`` suffix, so the server still surfaces the NAME-ONLY credential card).
``parse_response`` keeps the LATEST row per (lat, lon, parameter) and appends the derived
AQI-category + parameter-name columns; an empty result is an honest header-only FGB.

The key is NEVER registered/required by this wave: the parity surface proved is the
key-ABSENT typed error (byte-identical) + the input-validation errors. All I/O
(the aq/data GET, retry, cache, FGB serialize) stays router-owned.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response"]

_AIRNOW_BASE = "https://www.airnowapi.org/aq/data/"
_AIRNOW_KEY_ENV = "TRID3NT_AIRNOW_API_KEY"
_WINDOW_HOURS = 3

_VALID_PARAMETERS: dict[str, str] = {
    "pm25": "PM25", "pm2.5": "PM25", "pm10": "PM10", "ozone": "OZONE",
    "o3": "OZONE", "no2": "NO2", "so2": "SO2", "co": "CO",
}
_PARAMETER_LONG_NAME: dict[str, str] = {
    "PM25": "PM2.5 (fine particulate matter)", "PM10": "PM10 (coarse particulate matter)",
    "OZONE": "Ozone", "NO2": "Nitrogen dioxide", "SO2": "Sulfur dioxide", "CO": "Carbon monoxide",
}
_AQI_CATEGORY_NAMES: dict[int, str] = {
    1: "Good", 2: "Moderate", 3: "Unhealthy for Sensitive Groups", 4: "Unhealthy",
    5: "Very Unhealthy", 6: "Hazardous", 7: "Unavailable",
}
_PRESERVED_PROPERTIES = (
    "Latitude", "Longitude", "UTC", "Parameter", "Unit", "Value", "RawConcentration",
    "AQI", "Category", "SiteName", "AgencyName", "FullAQSCode", "IntlAQSCode",
)


def _resolve_api_key(sc: str, params: dict[str, Any]) -> str:
    """kwarg -> str secret_ref -> env; credential-shaped MISSING_KEY when none (pre-network)."""
    api_key = params.get("api_key")
    if api_key:
        return str(api_key)
    secret_ref = params.get("secret_ref")
    if isinstance(secret_ref, str) and secret_ref:
        return secret_ref
    env_key = os.environ.get(_AIRNOW_KEY_ENV)
    if env_key:
        return env_key
    raise router_input_error(
        sc,
        "no AirNow API key available: set the TRID3NT_AIRNOW_API_KEY env var (or add an "
        "AirNow api key). Register a free key at https://docs.airnowapi.org/account/request/.",
        "MISSING_KEY",
    )


def _validate_parameters(sc: str, raw: Any) -> list[str]:
    if raw is None:
        return ["PM25", "OZONE", "PM10"]
    items = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else None
    if items is None:
        raise router_input_error(sc, f"parameters must be a str or list of str; got {type(raw).__name__}")
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise router_input_error(sc, f"parameters entries must be str; got {item!r}")
        canon = _VALID_PARAMETERS.get(item.strip().lower())
        if canon is None:
            raise router_input_error(
                sc, f"parameter {item!r} is not a known AirNow pollutant; expected one of "
                    f"{sorted(set(_VALID_PARAMETERS.values()))}"
            )
        if canon not in out:
            out.append(canon)
    return out or ["PM25", "OZONE", "PM10"]


def _current_hour_window() -> tuple[str, str]:
    end_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start_hour = end_hour - timedelta(hours=_WINDOW_HOURS)
    return start_hour.strftime("%Y-%m-%dT%H"), end_hour.strftime("%Y-%m-%dT%H")


@_hooks.register_hook("airnow_air_quality.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Validate params + resolve the key, then build the aq/data bounded-box GET."""
    sc = spec.error_code_prefix
    param_norm = _validate_parameters(sc, params.get("parameters"))
    try:
        mt = int(params.get("monitor_type", 0))
    except (TypeError, ValueError):
        mt = 0
    api_key = _resolve_api_key(sc, params)  # raises MISSING_KEY pre-network when absent
    start_date, end_date = _current_hour_window()
    w, s, e, n = (float(v) for v in params["bbox"])
    q = {
        "startDate": start_date, "endDate": end_date, "parameters": ",".join(param_norm),
        "BBOX": f"{w},{s},{e},{n}", "dataType": "B", "format": "application/json",
        "verbose": "1", "monitorType": str(mt), "includerawconcentrations": "1", "API_KEY": api_key,
    }
    headers = {"User-Agent": spec.auth.user_agent}
    return [_hooks.RequestPlan(url=_AIRNOW_BASE, params=q, headers=headers)]


def _aqi_category_name(category: Any) -> str:
    try:
        return _AQI_CATEGORY_NAMES.get(int(category), "Unavailable")
    except (TypeError, ValueError):
        return "Unavailable"


@_hooks.register_hook("airnow_air_quality.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Keep the LATEST row per (lat, lon, parameter); append derived AQI columns."""
    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    try:
        body = json.loads(raw.decode("utf-8")) if raw else []
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"AirNow returned non-JSON: {exc}")
    if isinstance(body, dict) and "WebServiceError" in body:
        raise router_upstream_error(sc, f"AirNow returned a WebServiceError envelope: {json.dumps(body['WebServiceError'])[:300]}")
    if not isinstance(body, list):
        raise router_upstream_error(sc, f"AirNow response is not a JSON list: type={type(body).__name__}")

    latest: dict[tuple[float, float, str], dict[str, Any]] = {}
    for rec in body:
        if not isinstance(rec, dict):
            continue
        try:
            lat = float(rec.get("Latitude")); lon = float(rec.get("Longitude"))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        key = (round(lat, 6), round(lon, 6), str(rec.get("Parameter") or ""))
        utc = str(rec.get("UTC") or "")
        prev = latest.get(key)
        if prev is None or utc >= str(prev.get("UTC") or ""):
            latest[key] = rec

    out: list[dict[str, Any]] = []
    for rec in latest.values():
        lat = float(rec.get("Latitude")); lon = float(rec.get("Longitude"))
        props: dict[str, Any] = {}
        for k in _PRESERVED_PROPERTIES:
            v = rec.get(k)
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            props[k] = v
        tok = str(rec.get("Parameter") or "")
        props["ParameterName"] = _PARAMETER_LONG_NAME.get(tok, tok)
        props["AQICategoryName"] = _aqi_category_name(rec.get("Category"))
        out.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props})
    return out
