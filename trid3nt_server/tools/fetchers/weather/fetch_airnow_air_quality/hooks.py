"""airnow_air_quality hooks: EPA AirNow current-hour AQI observations, keyed.

``build_request`` reads the API key, validates the pollutant filter and builds the
bounded-box query; ``parse_response`` keeps the LATEST row per (lat, lon, parameter). The
declared ``column_map`` derives ``ParameterName`` / ``AQICategoryName`` off the raw
``Parameter`` / ``Category`` this hands it."""

# With no key stored the build asks for one before the network; an empty result is an honest header-only FGB.

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec
from trid3nt_server.model.credentials.resolver import credential_value

from ..._router import hooks as _hooks
from ..._router.errors import router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response"]

_AIRNOW_BASE = "https://www.airnowapi.org/aq/data/"
_WINDOW_HOURS = 3

_VALID_PARAMETERS: dict[str, str] = {
    "pm25": "PM25", "pm2.5": "PM25", "pm10": "PM10", "ozone": "OZONE",
    "o3": "OZONE", "no2": "NO2", "so2": "SO2", "co": "CO",
}
_PRESERVED_PROPERTIES = (
    "Latitude", "Longitude", "UTC", "Parameter", "Unit", "Value", "RawConcentration",
    "AQI", "Category", "SiteName", "AgencyName", "FullAQSCode", "IntlAQSCode",
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
    """Validate params + read the key, then build the aq/data bounded-box GET."""
    sc = spec.error_code_prefix
    param_norm = _validate_parameters(sc, params.get("parameters"))
    try:
        mt = int(params.get("monitor_type", 0))
    except (TypeError, ValueError):
        mt = 0
    api_key = credential_value(spec)
    start_date, end_date = _current_hour_window()
    w, s, e, n = (float(v) for v in params["bbox"])
    q = {
        "startDate": start_date, "endDate": end_date, "parameters": ",".join(param_norm),
        "BBOX": f"{w},{s},{e},{n}", "dataType": "B", "format": "application/json",
        "verbose": "1", "monitorType": str(mt), "includerawconcentrations": "1", "API_KEY": api_key,
    }
    headers = {"User-Agent": spec.auth.user_agent}
    return [_hooks.RequestPlan(url=_AIRNOW_BASE, params=q, headers=headers)]


@_hooks.register_hook("airnow_air_quality.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Keep the LATEST row per (lat, lon, parameter). The raw ``Parameter`` / ``Category``
    columns ride through for the declared column_map's lookups to derive the display
    names off."""
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
        out.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props})
    return out
