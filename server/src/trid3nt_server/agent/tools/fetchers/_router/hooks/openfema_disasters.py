"""openfema_disasters hooks (chained-resolution mode, ADR 0064): FEMA disaster
declarations aggregated per county, joined to Census TIGERweb county polygons.

Two ratchets retired at once here:
1. OFFSET paging ($skip/$top, stop-on-short-page) -- the ``next_page`` hook reuses
   the ADR 0063 offset-paging primitive (gbif's sibling): one combined OData query
   over the selector's states, paged to a short page / a row cap.
2. ATTRIBUTE-FEED <- BOUNDARY-SERVICE FIPS join -- the PHASE-E enrichment: the
   declarations are the attribute feed; ``enrich_plan`` emits one TIGERweb county
   FeatureServer GET per state-in-scope, and ``enrich_merge`` left-joins each
   aggregate onto its county polygon by the 5-digit GEOID (bbox-clipping the
   selector path). This is the enrich shape, NOT ``transforms/join.py`` (that
   transform is geometry-first single-value choropleth; openfema is
   attributes-first multi-field aggregate -- see ADR 0064 for why they diverge).

All I/O (both round-trip families, the paging loop, the deduped/bounded/best-effort
county-geometry loop, retry, cache, FGB serialize, LayerURI) stays router-owned;
these hooks only compute.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections import defaultdict
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_empty_error, router_input_error, router_upstream_error

__all__ = ["build_request", "next_page", "parse_response", "enrich_plan", "enrich_merge"]

_OPENFEMA_URL = "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
_TIGER_COUNTY_URL = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "State_County/MapServer/1/query"
)

#: OpenFEMA page size ($top); pages by $skip until a short page or the row cap.
_PAGE_SIZE = 1000
#: Safety cap on total declaration rows pulled over the combined query (the twin's
#: per-state cap; the combined-query cap is total, flagged in ADR 0064).
_MAX_ROWS = 12000

#: 2-letter USPS code -> 2-digit FIPS state code (50 states + DC + 5 territories).
STATE_FIPS: dict[str, str] = {
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

#: State FIPS -> approximate WGS84 envelope for the bbox -> intersecting-states
#: derivation (50 states + DC + PR + VI; generous ~10km border buffers).
_STATE_FIPS_BBOXES: dict[str, tuple[float, float, float, float]] = {
    "01": (-88.5, 30.1, -84.9, 35.0), "02": (-180.0, 51.2, -130.0, 71.5),
    "04": (-114.8, 31.3, -109.0, 37.0), "05": (-94.6, 33.0, -89.7, 36.5),
    "06": (-124.5, 32.5, -114.1, 42.0), "08": (-109.1, 36.9, -102.0, 41.0),
    "09": (-73.7, 40.9, -71.8, 42.1), "10": (-75.8, 38.4, -75.0, 39.8),
    "11": (-77.1, 38.8, -76.9, 39.0), "12": (-87.6, 24.4, -80.0, 31.0),
    "13": (-85.6, 30.3, -80.8, 35.0), "15": (-160.3, 18.9, -154.8, 22.2),
    "16": (-117.2, 42.0, -111.0, 49.0), "17": (-91.5, 36.9, -87.0, 42.5),
    "18": (-88.1, 37.8, -84.8, 41.8), "19": (-96.6, 40.4, -90.1, 43.5),
    "20": (-102.1, 36.9, -94.6, 40.0), "21": (-89.6, 36.5, -82.0, 39.1),
    "22": (-94.0, 28.9, -89.0, 33.0), "23": (-71.1, 43.0, -67.0, 47.5),
    "24": (-79.5, 37.9, -75.0, 39.7), "25": (-73.5, 41.2, -69.9, 42.9),
    "26": (-90.4, 41.7, -82.4, 48.3), "27": (-97.2, 43.5, -89.5, 49.4),
    "28": (-91.7, 30.1, -88.1, 35.0), "29": (-95.8, 35.9, -89.1, 40.6),
    "30": (-116.1, 44.4, -104.0, 49.0), "31": (-104.1, 40.0, -95.3, 43.0),
    "32": (-120.0, 35.0, -114.0, 42.0), "33": (-72.6, 42.7, -70.6, 45.3),
    "34": (-75.6, 38.9, -73.9, 41.4), "35": (-109.1, 31.3, -103.0, 37.0),
    "36": (-79.8, 40.5, -71.9, 45.0), "37": (-84.4, 33.8, -75.4, 36.6),
    "38": (-104.1, 45.9, -96.6, 49.0), "39": (-84.8, 38.4, -80.5, 42.3),
    "40": (-103.0, 33.6, -94.4, 37.0), "41": (-124.6, 41.9, -116.5, 46.3),
    "42": (-80.5, 39.7, -74.7, 42.3), "44": (-71.9, 41.1, -71.1, 42.0),
    "45": (-83.4, 32.0, -78.5, 35.2), "46": (-104.1, 42.5, -96.4, 45.9),
    "47": (-90.3, 35.0, -81.7, 36.7), "48": (-106.7, 25.8, -93.5, 36.5),
    "49": (-114.1, 37.0, -109.0, 42.0), "50": (-73.4, 42.7, -71.5, 45.0),
    "51": (-83.7, 36.5, -75.2, 39.5), "53": (-124.8, 45.5, -116.9, 49.0),
    "54": (-82.6, 37.2, -77.7, 40.6), "55": (-92.9, 42.5, -86.8, 47.1),
    "56": (-111.1, 40.9, -104.1, 45.0), "72": (-67.3, 17.9, -65.2, 18.6),
    "78": (-65.1, 17.6, -64.5, 18.5),
}

_FIPS_TO_STATE: dict[str, str] = {v: k for k, v in STATE_FIPS.items()}

#: OpenFEMA ``incidentType`` enumeration (the documented value set).
VALID_INCIDENT_TYPES: frozenset[str] = frozenset(
    {
        "Hurricane", "Flood", "Severe Storm", "Tornado", "Fire", "Snowstorm",
        "Severe Ice Storm", "Coastal Storm", "Tropical Storm", "Earthquake",
        "Drought", "Mud/Landslide", "Typhoon", "Dam/Levee Break", "Tsunami",
        "Volcanic Eruption", "Freezing", "Winter Storm", "Biological",
        "Chemical", "Fishing Losses", "Human Cause", "Other", "Toxic Substances",
        "Terrorist", "Straight-Line Winds", "Earthquake And Aftershocks",
    }
)


def _headers(spec: SourceSpec) -> dict[str, str]:
    return {"User-Agent": spec.auth.user_agent}


# --------------------------------------------------------------------------- #
# Selector resolution + input validation (pure; recomputed by each phase).
# --------------------------------------------------------------------------- #


def _validate_state_code(sc: str, state_code: Any) -> str:
    if not isinstance(state_code, str):
        raise router_input_error(
            sc, f"state_code must be a 2-letter string; got {type(state_code).__name__}"
        )
    code = state_code.strip().upper()
    if code not in STATE_FIPS:
        raise router_input_error(
            sc,
            f"state_code={state_code!r} is not a recognized 2-letter USPS code; "
            f"expected e.g. 'FL', 'TX', 'CA'",
        )
    return code


def _states_for_bbox(sc: str, bbox: tuple[float, float, float, float]) -> list[str]:
    west, south, east, north = bbox
    states: list[str] = []
    for fips, (s_w, s_s, s_e, s_n) in _STATE_FIPS_BBOXES.items():
        if west <= s_e and east >= s_w and south <= s_n and north >= s_s:
            usps = _FIPS_TO_STATE.get(fips)
            if usps is not None:
                states.append(usps)
    if not states:
        raise router_input_error(
            sc,
            f"bbox={bbox!r} does not intersect any US state envelope; "
            f"fetch_openfema_disasters covers US states + territories only "
            f"(supports_global_query=False).",
        )
    return sorted(states)


def _validate_incident_type(sc: str, incident_type: Any) -> str | None:
    if incident_type is None:
        return None
    if not isinstance(incident_type, str) or not incident_type.strip():
        return None
    want = incident_type.strip().lower()
    for canon in VALID_INCIDENT_TYPES:
        if canon.lower() == want:
            return canon
    raise router_input_error(
        sc,
        f"incident_type={incident_type!r} is not a recognized OpenFEMA incident "
        f"type. Examples: 'Hurricane', 'Flood', 'Severe Storm', 'Tornado', "
        f"'Fire', 'Tropical Storm', 'Earthquake', 'Drought'.",
    )


def _validate_start_year(sc: str, start_year: Any) -> int | None:
    if start_year is None:
        return None
    try:
        y = int(start_year)
    except (TypeError, ValueError):
        raise router_input_error(sc, f"start_year must be an integer year; got {start_year!r}")
    cur = _dt.date.today().year
    if not (1953 <= y <= cur + 1):
        raise router_input_error(sc, f"start_year={y} out of range; expected 1953..{cur + 1}")
    return y


def _resolve(spec: SourceSpec, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve the selector + optional filters (pure, idempotent, twin-identical).

    ``state_code`` wins over ``bbox``; a bbox derives the intersecting states and
    becomes the clip envelope. Neither given -> OPENFEMA_INPUT_ERROR.
    """
    sc = spec.error_code_prefix
    state_code = params.get("state_code")
    bbox = params.get("bbox")
    has_state = isinstance(state_code, str) and state_code.strip() != ""

    clip_bbox: tuple[float, float, float, float] | None = None
    if has_state:
        states = [_validate_state_code(sc, state_code)]
    elif bbox is not None:
        b = tuple(float(v) for v in bbox)
        clip_bbox = (b[0], b[1], b[2], b[3])
        states = _states_for_bbox(sc, clip_bbox)
    else:
        raise router_input_error(
            sc,
            "fetch_openfema_disasters requires a spatial selector: pass state_code "
            "(2-letter USPS, e.g. 'FL') or bbox=(west, south, east, north).",
        )
    return {
        "states": states,
        "clip_bbox": clip_bbox,
        "incident_type": _validate_incident_type(sc, params.get("incident_type")),
        "start_fy": _validate_start_year(sc, params.get("start_year")),
    }


# --------------------------------------------------------------------------- #
# MAIN FETCH -- one combined OData query, offset-paged.
# --------------------------------------------------------------------------- #


def _odata_filter(states: list[str], incident: str | None, start_fy: int | None) -> str:
    state_clause = " or ".join(f"state eq '{s}'" for s in states)
    clauses = [f"({state_clause})" if len(states) > 1 else state_clause]
    if incident is not None:
        clauses.append(f"incidentType eq '{incident.replace(chr(39), chr(39) * 2)}'")
    if start_fy is not None:
        clauses.append(f"fyDeclared ge {int(start_fy)}")
    return " and ".join(clauses)


def _page_plan(spec: SourceSpec, resolved: dict[str, Any], skip: int) -> "_hooks.RequestPlan":
    q = {
        "$filter": _odata_filter(resolved["states"], resolved["incident_type"], resolved["start_fy"]),
        "$orderby": "declarationDate desc",
        "$top": str(_PAGE_SIZE),
        "$skip": str(skip),
        "$format": "json",
    }
    return _hooks.RequestPlan(url=_OPENFEMA_URL, params=q, headers=_headers(spec))


@_hooks.register_hook("openfema_disasters.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Resolve the selector + validate filters; build the page-1 OData query ($skip=0)."""
    resolved = _resolve(spec, params)
    return [_page_plan(spec, resolved, 0)]


def _page_records(sc: str, body: bytes) -> list[dict[str, Any]]:
    if not body:
        return []
    try:
        obj = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"OpenFEMA response is not valid JSON: {exc}")
    recs = obj.get("DisasterDeclarationsSummaries")
    if recs is None:
        raise router_upstream_error(
            sc,
            f"OpenFEMA body missing 'DisasterDeclarationsSummaries' key; "
            f"got keys {list(obj.keys())[:8]}",
        )
    return list(recs)


@_hooks.register_hook("openfema_disasters.next_page")
def next_page(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> "_hooks.RequestPlan | None":
    """Offset paging: next $skip until a short page or the row cap (the reused primitive)."""
    sc = spec.error_code_prefix
    last = _page_records(sc, bodies[-1])
    total_so_far = sum(len(_page_records(sc, b)) for b in bodies)
    if len(last) < _PAGE_SIZE or total_so_far >= _MAX_ROWS:
        return None
    return _page_plan(spec, _resolve(spec, params), len(bodies) * _PAGE_SIZE)


@_hooks.register_hook("openfema_disasters.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Concatenate pages, aggregate declarations per 5-digit county FIPS (geometry-less)."""
    sc = spec.error_code_prefix
    records: list[dict[str, Any]] = []
    for body in bodies:
        records.extend(_page_records(sc, body))

    by_fips: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "n_declarations": 0,
            "disaster_numbers": set(),
            "incident_types": set(),
            "declaration_types": set(),
            "latest_declaration": None,
            "area_name": None,
            "ia_program": False,
            "pa_program": False,
        }
    )
    for rec in records:
        state = str(rec.get("fipsStateCode") or "").strip()
        county = str(rec.get("fipsCountyCode") or "").strip()
        if not state or not county or county.zfill(3) == "000":
            continue
        fips = f"{state.zfill(2)}{county.zfill(3)}"
        b = by_fips[fips]
        b["n_declarations"] += 1
        dn = rec.get("disasterNumber")
        if dn is not None:
            b["disaster_numbers"].add(str(dn))
        it = rec.get("incidentType")
        if it:
            b["incident_types"].add(str(it))
        dt = rec.get("declarationType")
        if dt:
            b["declaration_types"].add(str(dt))
        dd = rec.get("declarationDate")
        if dd and (b["latest_declaration"] is None or dd > b["latest_declaration"]):
            b["latest_declaration"] = dd
            b["area_name"] = rec.get("designatedArea")
        if rec.get("iaProgramDeclared"):
            b["ia_program"] = True
        if rec.get("paProgramDeclared"):
            b["pa_program"] = True

    out: list[dict[str, Any]] = []
    for fips, agg in by_fips.items():
        out.append({
            "type": "Feature",
            "geometry": None,
            "properties": {
                "county_fips": fips,
                "state_fips": fips[:2],
                "n_declarations": int(agg["n_declarations"]),
                "disaster_numbers": ",".join(
                    sorted(agg["disaster_numbers"], key=lambda s: int(s) if s.isdigit() else 0)
                ),
                "incident_types": ",".join(sorted(agg["incident_types"])),
                "declaration_types": ",".join(sorted(agg["declaration_types"])),
                "latest_declaration": str(agg["latest_declaration"] or ""),
                "ia_program": bool(agg["ia_program"]),
                "pa_program": bool(agg["pa_program"]),
                "_area_name": agg["area_name"],
            },
        })
    return out


# --------------------------------------------------------------------------- #
# PHASE E -- boundary-service FIPS join (TIGERweb county polygons).
# --------------------------------------------------------------------------- #


def _tiger_plan(spec: SourceSpec, state_fips: str) -> "_hooks.RequestPlan":
    q = {
        "where": f"STATE='{state_fips}'",
        "outFields": "GEOID,NAME,STATE,COUNTY",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }
    return _hooks.RequestPlan(url=_TIGER_COUNTY_URL, params=q, headers=_headers(spec))


@_hooks.register_hook("openfema_disasters.enrich_plan")
def enrich_plan(
    spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]]
) -> list[tuple[str, "_hooks.RequestPlan"]]:
    """One TIGERweb county GET per distinct state-in-scope (mode dedups by state FIPS)."""
    seen: list[str] = []
    for feat in features:
        sf = (feat.get("properties") or {}).get("state_fips")
        if isinstance(sf, str) and sf and sf not in seen:
            seen.append(sf)
    return [(sf, _tiger_plan(spec, sf)) for sf in seen]


def _geom_by_geoid(sc: str, results: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for res in results.values():
        body = getattr(res, "body", None) if res is not None else None
        if not body:
            continue
        try:
            obj = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise router_upstream_error(sc, f"TIGERweb response is not valid JSON: {exc}")
        if isinstance(obj, dict) and obj.get("error"):
            raise router_upstream_error(sc, f"TIGERweb error: {obj['error']}")
        for feat in (obj.get("features") if isinstance(obj, dict) else None) or []:
            props = feat.get("properties") or {}
            geoid = str(props.get("GEOID") or "").strip()
            geom = feat.get("geometry")
            if geoid and geom:
                out[geoid] = {"geometry": geom, "name": props.get("NAME")}
    return out


@_hooks.register_hook("openfema_disasters.enrich_merge")
def enrich_merge(
    spec: SourceSpec, params: dict[str, Any], features: list[dict[str, Any]], results: dict[str, Any]
) -> list[dict[str, Any]]:
    """Left-join county polygons by GEOID, bbox-clip the selector path, drop unmatched.

    Raises OPENFEMA_NO_DECLARATIONS when nothing joins (twin-identical honesty), never
    an empty success-shaped layer.
    """
    sc = spec.error_code_prefix
    resolved = _resolve(spec, params)
    clip_bbox = resolved["clip_bbox"]
    geom_by_geoid = _geom_by_geoid(sc, results)

    clip_geom = None
    if clip_bbox is not None:
        try:
            from shapely.geometry import box, shape  # type: ignore[import-not-found]
        except ImportError as exc:
            raise router_upstream_error(sc, f"shapely not available for bbox clip: {exc}")
        clip_geom = box(*clip_bbox)
    else:
        shape = None  # type: ignore[assignment]

    out: list[dict[str, Any]] = []
    for feat in features:
        props = feat.get("properties") or {}
        fips = props.get("county_fips")
        match = geom_by_geoid.get(fips)
        if match is None:
            continue
        geom = match["geometry"]
        if clip_geom is not None:
            try:
                if not shape(geom).intersects(clip_geom):
                    continue
            except (KeyError, TypeError, ValueError):
                continue
        county_name = str(match.get("name") or props.get("_area_name") or "")
        out.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "county_fips": fips,
                "county_name": county_name,
                "state_fips": props.get("state_fips"),
                "n_declarations": props.get("n_declarations"),
                "disaster_numbers": props.get("disaster_numbers"),
                "incident_types": props.get("incident_types"),
                "declaration_types": props.get("declaration_types"),
                "latest_declaration": props.get("latest_declaration"),
                "ia_program": props.get("ia_program"),
                "pa_program": props.get("pa_program"),
            },
        })

    if not out:
        raise router_empty_error(
            sc,
            "No county-level FEMA disaster declarations could be joined to a county "
            "polygon for the requested scope (no declarations join to a county, or "
            "none of the affected counties fall inside the bbox). Try dropping the "
            "incident_type filter, extending the year window, or widening the area.",
            "NO_DECLARATIONS",
        )
    return out
