"""usgs_groundwater_levels hooks: monitoring wells and their water levels, the latest or the dated ones of a window.

``build_request`` resolves the spatial selector -- state or bbox, exactly one, state winning -- and GETs
the latest-measurements collection, or asked a window the field measurements dated inside it; a primary
that misses raises a typed no-wells error. The enrich phase joins well metadata and NEVER drops a reading."""

from __future__ import annotations

import datetime as dt
import json
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import hooks as _hooks
from ..._router.errors import router_empty_error, router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response", "enrich_plan", "enrich_merge"]

_MEASUREMENTS_URL = (
    "https://api.waterdata.usgs.gov/ogcapi/v0/collections/latest-field-measurements/items"
)
_DATED_URL = (
    "https://api.waterdata.usgs.gov/ogcapi/v0/collections/field-measurements/items"
)
_LOCATIONS_URL = (
    "https://api.waterdata.usgs.gov/ogcapi/v0/collections/monitoring-locations/items"
)
_GW_PARAMETER_CODES = ("72019", "72150", "62610", "62611", "61055")
_PCODE_LABEL = {
    "72019": "depth to water (ft below land surface)",
    "72150": "groundwater level (ft, NAVD88)",
    "62610": "groundwater elevation (ft, NGVD29)",
    "62611": "groundwater elevation (ft, NAVD88)",
    "61055": "water level (ft below measuring point)",
}
# WHAT a reading's value IS, by its parameter code: a depth below the land surface
# (positive down), the water's elevation on the reading's own vertical_datum, or a
# depth below the well's measuring point, whose height above the land no reading states.
_PCODE_QUANTITY = {
    "72019": "water_table_depth",
    "72150": "groundwater_head",
    "62610": "groundwater_head",
    "62611": "groundwater_head",
    "61055": "depth_below_measuring_point",
}
_PAGE_LIMIT = 10000
_USPS_TO_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56", "PR": "72", "VI": "78", "GU": "66",
    "AS": "60", "MP": "69",
}
_OUT_COLUMNS = (
    "site_no", "site_name", "parameter_code", "parameter_label", "quantity",
    "water_level", "unit", "vertical_datum", "datetime", "approval_status",
    "aquifer_code", "well_depth_ft", "reading_count", "period_end",
)


def _resolve_selector(spec: SourceSpec, params: dict[str, Any]) -> tuple[str | None, list[float] | None]:
    """(state_fips, bbox) from the mutually-exclusive selector (state wins). Typed input error otherwise."""
    sc = spec.error_code_prefix
    state_code = params.get("state_code")
    if state_code is not None and str(state_code).strip() != "":
        s = str(state_code).strip().upper()
        if s not in _USPS_TO_FIPS:
            raise router_input_error(
                sc, f"state_code={state_code!r} is not a recognized 2-letter USPS code; "
                    "expected one of e.g. 'KS', 'CA', 'FL' (mapped to a FIPS code for the "
                    "USGS OGC API state_code filter)")
        return _USPS_TO_FIPS[s], None
    bbox = params.get("bbox")
    if bbox is not None:
        return None, list(bbox)
    raise router_input_error(
        sc, "fetch_usgs_groundwater_levels requires a spatial selector: pass state_code "
            "(2-letter USPS, e.g. 'KS') for a state-level query, or bbox=(west, south, east, "
            "north) for an area query.")


def _query(state_fips: str | None, bbox: list[float] | None, extra: list[tuple[str, str]]) -> list[tuple[str, str]]:
    q = list(extra) + [("f", "json"), ("limit", str(_PAGE_LIMIT))]
    if state_fips is not None:
        q.append(("state_code", state_fips))
    elif bbox is not None:
        q.append(("bbox", ",".join(repr(float(v)) for v in bbox)))
    return q


def _window(spec: SourceSpec, params: dict[str, Any]) -> tuple[str, str] | None:
    """The asked window as the API's datetime interval bounds, or None where none is asked; one end alone refuses.

    A moment reaches back the observation window, so every reading the observation ingestion would let speak for
    the run is fetched; a period average is asked over the span alone."""
    start, end = (str(params.get(k) or "").strip()[:10] for k in ("start_date", "end_date"))
    if not start and not end:
        return None
    try:
        opens, closes = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    except ValueError:
        raise router_input_error(
            spec.error_code_prefix, f"start_date={params.get('start_date')!r} and end_date={params.get('end_date')!r} "
            "state no window: a window is both ends as ISO dates, start_date not after end_date")
    if opens > closes:
        raise router_input_error(spec.error_code_prefix, f"start_date {start} is after end_date {end}")
    if not params.get("period_average"):
        from trid3nt_server.settings import setting

        opens -= dt.timedelta(days=float(setting("OBSERVATION_WINDOW_DAYS")))
    return f"{opens.isoformat()}T00:00:00Z", f"{closes.isoformat()}T23:59:59Z"


@_hooks.register_hook("usgs_groundwater_levels.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """GET the latest-field-measurements collection for the resolved scope, or asked a window the field
    measurements dated inside it."""
    state_fips, bbox = _resolve_selector(spec, params)
    window = _window(spec, params)
    extra = [("parameter_code", ",".join(_GW_PARAMETER_CODES))]
    if window is not None:
        extra.append(("datetime", "/".join(window)))
    q = _query(state_fips, bbox, extra)
    ua = {"User-Agent": spec.auth.user_agent}
    return [_hooks.RequestPlan(url=_MEASUREMENTS_URL if window is None else _DATED_URL, params=dict(q),
                               headers=ua)]


def _feature_coords(feat: dict[str, Any]) -> tuple[float, float] | None:
    geom = feat.get("geometry") or {}
    coords = geom.get("coordinates") or []
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    try:
        lon = float(coords[0]); lat = float(coords[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    return lon, lat


@_hooks.register_hook("usgs_groundwater_levels.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Decode the measurements GeoJSON -> reading Point features; NO_WELLS if none."""
    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    if not raw:
        obj: Any = {"type": "FeatureCollection", "features": []}
    else:
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise router_upstream_error(sc, f"USGS OGC measurements response is not valid JSON: {exc}")
    if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
        raise router_upstream_error(
            sc, "USGS OGC measurements response is not a GeoJSON FeatureCollection: "
                f"type={obj.get('type') if isinstance(obj, dict) else type(obj).__name__!r}")

    if any(isinstance(link, dict) and link.get("rel") == "next" for link in obj.get("links") or []):
        raise router_upstream_error(
            sc, f"the USGS OGC API holds more than {_PAGE_LIMIT} readings for this scope and window, so one page "
                "is not all of them: ask a smaller box or a shorter window")
    feats: list[dict[str, Any]] = []
    for feat in obj.get("features") or []:
        if not isinstance(feat, dict):
            continue
        lonlat = _feature_coords(feat)
        if lonlat is None:
            continue
        lon, lat = lonlat
        props = feat.get("properties") or {}
        mlid = str(props.get("monitoring_location_id") or "").strip()
        site_no = mlid.split("-", 1)[1] if "-" in mlid else mlid
        raw_val = props.get("value")
        water_level: float | None = None
        if raw_val not in (None, ""):
            try:
                fv = float(raw_val)
                if math.isfinite(fv):
                    water_level = fv
            except (TypeError, ValueError):
                water_level = None
        pcode = str(props.get("parameter_code") or "").strip() or None
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "monitoring_location_id": mlid, "site_no": site_no,
                "parameter_code": pcode,
                "parameter_label": _PCODE_LABEL.get(pcode or "", pcode or ""),
                "quantity": _PCODE_QUANTITY.get(pcode or ""),
                "water_level": water_level,
                "unit": str(props.get("unit_of_measure") or "").strip() or None,
                "vertical_datum": str(props.get("vertical_datum") or "").strip() or None,
                "datetime": str(props.get("time") or "").strip() or None,
                "approval_status": str(props.get("approval_status") or "").strip() or None,
            },
        })
    if not feats:
        scope = (f"state_code={params.get('state_code')!r}" if params.get("state_code")
                 else f"bbox={params.get('bbox')!r}")
        raise router_empty_error(
            sc,
            f"No USGS groundwater monitoring wells reporting a water-level reading "
            f"(pcodes {','.join(_GW_PARAMETER_CODES)}) found for {scope}. The USGS Water Data "
            f"OGC API (latest-field-measurements) returned zero readings. Either the area has no "
            f"instrumented groundwater wells or none have a level on record; try a different area "
            f"or a state-level query (e.g. a High Plains aquifer state like state_code='KS').",
            spec.empty_error_suffix,
        )
    return _averaged(feats, params) if params.get("period_average") and _window(spec, params) else feats


def _averaged(feats: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    """Each site's readings of one parameter over the asked span as ONE reading: their mean, stamped at the
    span's opening, with how many readings it averages and when the span closes. An unparsed value averages nothing."""
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for feat in feats:
        props = feat["properties"]
        if props["water_level"] is None:
            continue
        key = (props["monitoring_location_id"], props["parameter_code"], props["unit"], props["vertical_datum"])
        groups.setdefault(key, []).append(feat)
    out = []
    for rows in groups.values():
        props = dict(rows[0]["properties"])
        props.update(water_level=sum(r["properties"]["water_level"] for r in rows) / len(rows),
                     datetime=str(params["start_date"])[:10], reading_count=len(rows),
                     period_end=str(params["end_date"])[:10], approval_status=None
                     if len({r["properties"]["approval_status"] for r in rows}) > 1 else props["approval_status"])
        out.append({"type": "Feature", "geometry": rows[0]["geometry"], "properties": props})
    return out


@_hooks.register_hook("usgs_groundwater_levels.enrich_plan")
def enrich_plan(spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]]) -> list[tuple[str, "_hooks.RequestPlan"]]:
    """One best-effort monitoring-locations ref for the same scope (well name/aquifer/depth)."""
    state_fips, bbox = _resolve_selector(spec, params)
    q = _query(state_fips, bbox, [
        ("site_type_code", "GW"),
        ("properties",
         "monitoring_location_number,monitoring_location_name,national_aquifer_code,"
         "well_constructed_depth"),
    ])
    ua = {"User-Agent": spec.auth.user_agent}
    return [("locations", _hooks.RequestPlan(url=_LOCATIONS_URL, params=dict(q), headers=ua))]


def _parse_locations(body: bytes | None) -> dict[str, dict[str, Any]]:
    if not body:
        return {}
    try:
        obj = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
        return {}
    lookup: dict[str, dict[str, Any]] = {}
    for feat in obj.get("features") or []:
        if not isinstance(feat, dict):
            continue
        mlid = str(feat.get("id") or "").strip()
        if not mlid:
            continue
        props = feat.get("properties") or {}
        depth = props.get("well_constructed_depth")
        try:
            depth_f: float | None = float(depth) if depth not in (None, "") else None
            if depth_f is not None and not math.isfinite(depth_f):
                depth_f = None
        except (TypeError, ValueError):
            depth_f = None
        lookup[mlid] = {
            "site_name": str(props.get("monitoring_location_name") or "").strip(),
            "aquifer_code": str(props.get("national_aquifer_code") or "").strip() or None,
            "well_depth_ft": depth_f,
        }
    return lookup


@_hooks.register_hook("usgs_groundwater_levels.enrich_merge")
def enrich_merge(spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]], results: dict[str, Any]) -> list[dict[str, Any]]:
    """Best-effort join of well name/aquifer/depth onto every reading (never drops a reading)."""
    res = results.get("locations")
    body = getattr(res, "body", None) if res is not None else None
    locations = _parse_locations(body)
    out: list[dict[str, Any]] = []
    for feat in features:
        props = dict(feat.get("properties") or {})
        meta = locations.get(props.get("monitoring_location_id", "")) or {}
        props["site_name"] = meta.get("site_name") or ""
        props["aquifer_code"] = meta.get("aquifer_code")
        props["well_depth_ft"] = meta.get("well_depth_ft")
        out.append({
            "type": "Feature", "geometry": feat.get("geometry"),
            "properties": {c: props.get(c) for c in _OUT_COLUMNS},
        })
    return out
