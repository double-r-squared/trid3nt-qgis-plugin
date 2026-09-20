"""usbr_hydromet hooks: a named RISE location, walked to its own elevation item.

RISE answers no query over location or parameter directly (locationId= and search=
on /catalog-item are both ignored server-side), so the id is found by following the
location's own relationships: catalogRecords, each one's catalogItems, until one
states parameterName "Lake/Reservoir Elevation". The resolved item id, the station's
coordinates and its own stated datums merge into params before the cache key.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from trid3nt_contracts.coverage import CoveragePoint
from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import RequestPlan, register_hook
from ..._router.transport import TransportError, get_bytes, get_client

logger = logging.getLogger(__name__)

__all__ = ["stations", "resolve_build", "resolve_parse", "build_request",
           "parse_response"]

#: The exact RISE parameterName a forebay-elevation catalog item states, matched
#: case-insensitively. A location's other items (storage, inflow, wind, ...) share
#: the same relationships walk and are passed over rather than guessed between.
_ELEVATION_PARAMETER = "lake/reservoir elevation"

#: How many of a location's own catalog records, and how many catalog items total
#: across them, this walks before giving up. A refusal here names what it found
#: rather than hanging on a location whose catalog is unusually large.
_MAX_CATALOG_RECORDS = 25
_MAX_CATALOG_ITEM_FETCHES = 60

#: The result page size asked for, and the ceiling `parse_response` trusts it
#: filled: a window whose true count exceeds this is a truncated page, refused
#: rather than silently short.
_MAX_RESULTS = 2000

#: The location listing's page size and the pages read. RISE lists its whole
#: network in one bounded document; the ceiling is what keeps a grown network a
#: short refusal rather than an unbounded walk.
_LOCATIONS_PER_PAGE = 100
_MAX_LOCATION_PAGES = 12

#: The listing's URL and the station a call names: RISE answers the location
#: search by NAME, so the name is the id a coverage row carries, not the _id the
#: catalog walk resolves to.
_LOCATION_LISTING = "https://data.usbr.gov/rise/api/location"


@register_hook("usbr_hydromet.stations")
def stations() -> list[CoveragePoint]:
    """Every RISE location, by the name a fetch is called with and its own zero."""
    listed: list[CoveragePoint] = []
    for page in range(1, _MAX_LOCATION_PAGES + 1):
        url = (f"{_LOCATION_LISTING}?itemsPerPage={_LOCATIONS_PER_PAGE}"
               f"&page={page}")
        body, _ct, _url = get_bytes(
            get_client(), url, headers={"Accept": "application/vnd.api+json"})
        rows = (json.loads(body.decode("utf-8")).get("data") or [])
        for row in rows:
            attrs = row.get("attributes") or {}
            name = str(attrs.get("locationName") or "").strip()
            here = _where(attrs.get("locationCoordinates") or {})
            if not name or here is None:
                continue
            listed.append(CoveragePoint(
                id=name[:120], lon=here[0], lat=here[1],
                datum=str((attrs.get("verticalDatum") or {}).get("_id") or "")
                or None))
        if len(rows) < _LOCATIONS_PER_PAGE:
            break
    return listed


def _where(geometry: dict[str, Any]) -> tuple[float, float] | None:
    """The one lon/lat a RISE location stands at, or ``None`` where it states none.

    RISE publishes a reservoir as the polygon of its own pool rather than a
    point; the centre of that published outline is where the location is, which
    is a reading of what RISE states and not a coordinate invented beside it."""
    coords = geometry.get("coordinates")
    if geometry.get("type") == "Point" and isinstance(coords, list) \
            and len(coords) >= 2:
        return (float(coords[0]), float(coords[1]))
    if geometry.get("type") == "Polygon" and coords:
        ring = [pt for pt in coords[0] if isinstance(pt, list) and len(pt) >= 2]
        if ring:
            return (sum(float(p[0]) for p in ring) / len(ring),
                    sum(float(p[1]) for p in ring) / len(ring))
    return None


def _headers(spec: SourceSpec) -> dict[str, str]:
    return {"User-Agent": spec.auth.user_agent, "Accept": "application/vnd.api+json"}


def _get_json(spec: SourceSpec, url: str, what: str) -> dict[str, Any]:
    """One GET through the shared transport, decoded as JSON:API. Any transport or
    decode fault is the source's own upstream error, named by what was being read."""
    sc = spec.error_code_prefix
    try:
        body, _ct, _url = get_bytes(get_client(), url, headers=_headers(spec))
    except TransportError as exc:
        raise router_upstream_error(sc, f"RISE {what} failed: {exc}")
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"RISE {what} returned non-JSON: {exc}")


def _pick_location(spec: SourceSpec, station: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """The one RISE location this station means. A parenthetical code match
    (station's own ``(SCO)`` in its ``locationName``) wins over a bare name hit;
    more than one survivor refuses rather than choosing between reservoirs."""
    sc = spec.error_code_prefix
    # A station that NAMES its own code carries it in a trailing parenthetical,
    # which is the same spelling RISE's locationName carries.
    stated = re.search(r"\(([^()]+)\)\s*$", station.strip())
    code = (stated.group(1) if stated else station).strip().upper()
    coded = [c for c in candidates
             if f"({code})" in str((c.get("attributes") or {}).get("locationName") or "").upper()]
    if len(coded) == 1:
        return coded[0]
    if len(candidates) == 1:
        return candidates[0]
    names = [str((c.get("attributes") or {}).get("locationName") or "?") for c in candidates[:5]]
    raise router_input_error(
        sc,
        f"station={station!r} matches {len(candidates)} RISE locations "
        f"({'; '.join(names)}); use the station's own parenthetical code "
        "(e.g. \"SCO\") to disambiguate",
        spec.input_error_suffix,
    )


def _find_elevation_item(spec: SourceSpec, host: str, catalog_record_paths: list[str],
                          location_name: str) -> tuple[str, str, str]:
    """This location's Lake/Reservoir Elevation catalog item: its id, parameter
    name and unit, off the location's own catalog-record -> catalog-item walk."""
    sc = spec.error_code_prefix
    fetched_items = 0
    for record_path in catalog_record_paths[:_MAX_CATALOG_RECORDS]:
        record = _get_json(spec, f"{host}{record_path}", f"catalog-record {record_path}")
        item_paths = [row.get("id") for row in
                      ((record.get("data") or {}).get("relationships") or {})
                      .get("catalogItems", {}).get("data") or []
                      if row.get("id")]
        for item_path in item_paths:
            if fetched_items >= _MAX_CATALOG_ITEM_FETCHES:
                break
            fetched_items += 1
            item = _get_json(spec, f"{host}{item_path}", f"catalog-item {item_path}")
            attrs = (item.get("data") or {}).get("attributes") or {}
            if str(attrs.get("parameterName") or "").strip().lower() == _ELEVATION_PARAMETER:
                return (str(attrs.get("_id")), str(attrs.get("parameterName") or ""),
                        str(attrs.get("parameterUnit") or ""))
    raise router_empty_error(
        sc,
        f"RISE location {location_name!r} carries no Lake/Reservoir Elevation "
        "catalog item over its own catalog records",
        spec.empty_error_suffix,
    )


@register_hook("usbr_hydromet.resolve_build")
def resolve_build(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """GET the RISE location search for the station name or code."""
    station = str(params.get("station") or "").strip()
    if not station:
        raise router_input_error(spec.error_code_prefix, "station is required and empty",
                                  spec.input_error_suffix)
    return [RequestPlan(url=str(spec.endpoints["location"].url),
                        params={"search": station}, headers=_headers(spec))]


@register_hook("usbr_hydromet.resolve_parse")
def resolve_parse(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> dict[str, Any]:
    """Pick the location, walk its catalog to the elevation item, and merge the
    resolved item id and the station's own coordinates and datums into params."""
    sc = spec.error_code_prefix
    station = str(params.get("station") or "").strip()
    try:
        parsed = json.loads(bodies[0].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"RISE location search returned non-JSON: {exc}")
    candidates = [c for c in (parsed.get("data") or []) if isinstance(c, dict)]
    if not candidates:
        raise router_empty_error(
            sc, f"no USBR RISE location matches station={station!r}", spec.empty_error_suffix)
    location = _pick_location(spec, station, candidates)
    attrs = location.get("attributes") or {}
    name = str(attrs.get("locationName") or station)
    coords = ((attrs.get("locationCoordinates") or {}).get("coordinates")) or []
    if len(coords) < 2:
        raise router_input_error(sc, f"RISE location {name!r} states no coordinates",
                                  spec.input_error_suffix)
    vertical = str((attrs.get("verticalDatum") or {}).get("_id") or "").strip()
    horizontal = str((attrs.get("horizontalDatum") or {}).get("_id") or "").strip()
    if not vertical or not horizontal:
        raise router_input_error(
            sc,
            f"RISE location {name!r} states {vertical or 'no'} vertical datum and "
            f"{horizontal or 'no'} horizontal datum; nothing here may assume one",
            spec.input_error_suffix,
        )
    host = str(spec.endpoints["host"].url)
    record_paths = [str(row.get("id")) for row in
                    (((location.get("relationships") or {}).get("catalogRecords") or {})
                     .get("data") or []) if row.get("id")]
    if not record_paths:
        raise router_empty_error(sc, f"RISE location {name!r} carries no catalog records",
                                  spec.empty_error_suffix)
    item_id, parameter_name, parameter_unit = _find_elevation_item(spec, host, record_paths, name)
    return {
        "_item_id": item_id,
        "_station_id": str(attrs.get("_id") or ""),
        "_station_name": name,
        "_region": ", ".join(attrs.get("locationRegionNames") or []),
        "_vertical_datum": vertical,
        "_horizontal_datum": horizontal,
        "_timezone": str(attrs.get("timezone") or ""),
        "_lon": float(coords[0]),
        "_lat": float(coords[1]),
        "_parameter_name": parameter_name,
        "_parameter_unit": parameter_unit,
    }


@register_hook("usbr_hydromet.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """GET the resolved item's results over the requested window, one page wide
    enough that a window under the cap never truncates."""
    query = {
        "itemId": params["_item_id"],
        "dateTime[after]": str(params["start_date"]),
        # RISE reads a bare date as its midnight, which drops the end day's own
        # reading; the day's last second keeps end_date inclusive.
        "dateTime[before]": f"{params['end_date']}T23:59:59",
        "itemsPerPage": str(_MAX_RESULTS),
    }
    return [RequestPlan(url=str(spec.endpoints["result"].url), params=query, headers=_headers(spec))]


@register_hook("usbr_hydromet.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """One Point feature at the station, its window's readings inline as CSV."""
    sc = spec.error_code_prefix
    try:
        parsed = json.loads(bodies[0].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"RISE result query returned non-JSON: {exc}")
    rows = [r for r in (parsed.get("data") or []) if isinstance(r, dict)]
    total = (parsed.get("meta") or {}).get("totalItems")
    if isinstance(total, int) and total > len(rows):
        raise router_input_error(
            sc,
            f"{total} results over this window exceed the {len(rows)} this fetch "
            "retrieved in one page; narrow start_date/end_date",
            spec.input_error_suffix,
        )
    points: list[tuple[str, float]] = []
    for row in rows:
        a = row.get("attributes") or {}
        t, v = a.get("dateTime"), a.get("result")
        if t is None or v is None:
            continue
        try:
            points.append((str(t), float(v)))
        except (TypeError, ValueError):
            continue
    points.sort(key=lambda p: p[0])
    if not points:
        raise router_empty_error(
            sc,
            f"no USBR RISE results for station={params.get('station')!r} between "
            f"{params['start_date']} and {params['end_date']}",
            spec.empty_error_suffix,
        )
    values = [v for _t, v in points]
    csv_body = "\n".join(["iso,value_ft"] + [f"{t},{v}" for t, v in points])
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [params["_lon"], params["_lat"]]},
        "properties": {
            "station_id": params.get("_station_id", ""),
            "station_name": params.get("_station_name", ""),
            "region": params.get("_region", ""),
            "parameter_name": params.get("_parameter_name", ""),
            "vertical_datum": params.get("_vertical_datum", ""),
            "horizontal_datum": params.get("_horizontal_datum", ""),
            "timezone": params.get("_timezone", ""),
            "forebay_elevation_ft": round(points[-1][1], 3),
            "forebay_elevation_min_ft": round(min(values), 3),
            "forebay_elevation_max_ft": round(max(values), 3),
            "forebay_elevation_mean_ft": round(sum(values) / len(values), 3),
            "n_timesteps": len(points),
            "time_start": points[0][0],
            "time_end": points[-1][0],
            "time_series_csv": csv_body,
        },
    }
    return [feature]
