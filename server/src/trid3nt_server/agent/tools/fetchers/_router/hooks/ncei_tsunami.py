"""ncei_tsunami hooks: NCEI Global Historical Tsunami DB -> point features.

The irreducible step: the NCEI hazard-service request (mode-selected endpoint,
year-window resolution + validation, bbox lat/lon params, page number) and the
per-mode JSON item decode (``events`` vs ``runups`` carry different source fields).
The paging LOOP is a declarative router mode (``ingest.http_source.paging``); this
hook builds one page's request and decodes the concatenated item bodies.
"""

from __future__ import annotations

import datetime as _dt
import math
import urllib.parse
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_empty_error, router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response"]

NCEI_TSUNAMI_BASE = "https://www.ngdc.noaa.gov/hazel/hazard-service/api/v1/tsunamis"
DEFAULT_MIN_YEAR = 1900
ITEMS_PER_PAGE = 200

#: Published NCEI / WDS cause classification (raw integer always preserved).
CAUSE_CODES: dict[int, str] = {
    0: "Unknown", 1: "Earthquake", 2: "Questionable Earthquake",
    3: "Earthquake and Landslide", 4: "Volcano and Earthquake",
    5: "Volcano, Earthquake, and Landslide", 6: "Volcano",
    7: "Volcano and Landslide", 8: "Landslide", 9: "Meteorological",
    10: "Explosion", 11: "Astronomical Tide",
}


def _resolve_mode(sc: str, sfx: str, observation_type: Any) -> str:
    if observation_type is None:
        return "events"
    mode = str(observation_type).strip().lower()
    if mode in ("event", "events", "source", "sources"):
        return "events"
    if mode in ("runup", "runups", "observation", "observations"):
        return "runups"
    raise router_input_error(
        sc, f"observation_type must be one of ('events', 'runups'); got {observation_type!r}", sfx
    )


def _resolve_year_window(sc: str, sfx: str, min_year: Any, max_year: Any) -> tuple[int, int]:
    cur = _dt.datetime.now(_dt.timezone.utc).year

    def _coerce(v: Any, label: str) -> int:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            raise router_input_error(sc, f"{label} must be an integer year; got {v!r}", sfx)
        if not (-2100 <= iv <= cur + 1):
            raise router_input_error(sc, f"{label}={iv} is outside the catalog range [-2100, {cur + 1}]", sfx)
        return iv

    lo = DEFAULT_MIN_YEAR if min_year is None else _coerce(min_year, "min_year")
    hi = cur if max_year is None else _coerce(max_year, "max_year")
    if lo > hi:
        raise router_input_error(sc, f"min_year must be <= max_year; got min_year={lo}, max_year={hi}", sfx)
    return lo, hi


@_hooks.register_hook("ncei_tsunami.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Build one page's NCEI tsunami query (the router pages via ingest.http_source)."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    mode = _resolve_mode(sc, sfx, params.get("observation_type"))
    lo, hi = _resolve_year_window(sc, sfx, params.get("min_year"), params.get("max_year"))
    page = int(params.get("page", 1) or 1)
    bbox = params.get("bbox")

    query: list[tuple[str, str]] = [
        ("minYear", str(lo)),
        ("maxYear", str(hi)),
        ("page", str(page)),
        ("itemsPerPage", str(ITEMS_PER_PAGE)),
    ]
    if bbox is not None:
        west, south, east, north = bbox
        query += [
            ("minLongitude", repr(float(west))),
            ("minLatitude", repr(float(south))),
            ("maxLongitude", repr(float(east))),
            ("maxLatitude", repr(float(north))),
        ]
    url = f"{NCEI_TSUNAMI_BASE}/{mode}?" + urllib.parse.urlencode(query)
    return [_hooks.RequestPlan(url=url, headers={"User-Agent": spec.auth.user_agent})]


def _f(value: Any) -> float | None:
    if value is None:
        return None
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    return fv if math.isfinite(fv) else None


def _i(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _s(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _cause_label(cause_code: Any) -> str:
    try:
        ci = int(cause_code)
    except (TypeError, ValueError):
        return "Unknown"
    return CAUSE_CODES.get(ci, "Unknown")


@_hooks.register_hook("ncei_tsunami.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Concatenate the paged NCEI item bodies and decode them for the mode."""
    import json

    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    mode = _resolve_mode(sc, sfx, params.get("observation_type"))

    items: list[Any] = []
    for raw in bodies:
        if not raw:
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise router_upstream_error(sc, f"NCEI tsunami response is not valid JSON: {exc}")
        if not isinstance(obj, dict):
            raise router_upstream_error(sc, f"NCEI tsunami response is not a JSON object: type={type(obj).__name__}")
        page_items = obj.get("items") or []
        if isinstance(page_items, list):
            items.extend(page_items)

    records = _records_from_items(items, mode)
    if not records:
        raise router_empty_error(
            sc,
            f"No tsunami {mode} matched the requested scope/year window. The NCEI Global "
            f"Historical Tsunami Database has no record for that scope. Widen the window, widen "
            f"the bbox, or pick a more tsunami-prone coastline (Pacific Rim, Japan, Indonesia, "
            f"Chile, Alaska, the Mediterranean).",
            spec.empty_error_suffix,
        )
    return records


def _records_from_items(items: list[Any], mode: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        lat = _f(it.get("latitude"))
        lon = _f(it.get("longitude"))
        if lat is None or lon is None:
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        if mode == "events":
            cause_code = _i(it.get("causeCode"))
            deaths = _i(it.get("deathsTotal"))
            if deaths is None:
                deaths = _i(it.get("deaths"))
            props = {
                "id": _i(it.get("id")),
                "year": _i(it.get("year")),
                "cause_code": cause_code,
                "cause": _cause_label(cause_code),
                "location_name": _s(it.get("locationName")),
                "country": _s(it.get("country")),
                "eq_magnitude": _f(it.get("eqMagnitude")),
                "max_water_height": _f(it.get("maxWaterHeight")),
                "deaths": deaths,
                "num_runups": _i(it.get("numRunups")),
                "dist_from_source_km": None,
                "observation_type": "event",
                "source": "NCEI/WDS Global Historical Tsunami Database",
            }
        else:
            cause_code = _i(it.get("sourceCauseCode"))
            props = {
                "id": _i(it.get("id")),
                "year": _i(it.get("year")),
                "cause_code": cause_code,
                "cause": _cause_label(cause_code),
                "location_name": _s(it.get("locationName")),
                "country": _s(it.get("country")),
                "eq_magnitude": _f(it.get("sourceEqMagnitude")),
                "max_water_height": _f(it.get("runupHt")),
                "deaths": _i(it.get("deaths")),
                "num_runups": None,
                "dist_from_source_km": _f(it.get("distFromSource")),
                "observation_type": "runup",
                "source": "NCEI/WDS Global Historical Tsunami Database",
            }
        out.append(
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": props}
        )
    return out
