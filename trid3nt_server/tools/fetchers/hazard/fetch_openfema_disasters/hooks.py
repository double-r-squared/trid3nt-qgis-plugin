"""openfema_disasters hooks: the two steps the field map cannot state.

``build_request`` resolves the spatial selector - a state code, or the states a bbox
touches - into one OData filter over the declared page window; ``parse_response``
reduces many declaration rows to ONE aggregate per county, which is not a row walk."""

from __future__ import annotations

import datetime as _dt
import json
from collections import defaultdict
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ...us_states import STATE_CODE_TO_FIPS, states_intersecting_bbox
from ..._router import hooks as _hooks
from ..._router.errors import router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response"]

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


def _validate_state_code(sc: str, state_code: Any) -> str:
    if not isinstance(state_code, str):
        raise router_input_error(
            sc, f"state_code must be a 2-letter string; got {type(state_code).__name__}"
        )
    code = state_code.strip().upper()
    if code not in STATE_CODE_TO_FIPS:
        raise router_input_error(
            sc,
            f"state_code={state_code!r} is not a recognized 2-letter USPS code; "
            f"expected e.g. 'FL', 'TX', 'CA'",
        )
    return code


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


def _resolve_states(spec: SourceSpec, params: dict[str, Any]) -> list[str]:
    """The states to query: ``state_code`` wins, else the states the bbox touches.
    Neither given, or a bbox outside the US, raises the source's input error - this
    source serves US states and territories only."""
    sc = spec.error_code_prefix
    state_code = params.get("state_code")
    bbox = params.get("bbox")
    if isinstance(state_code, str) and state_code.strip():
        return [_validate_state_code(sc, state_code)]
    if bbox is not None:
        states = states_intersecting_bbox(tuple(float(v) for v in bbox))
        if states:
            return states
        raise router_input_error(
            sc,
            f"bbox={bbox!r} does not intersect any US state envelope; "
            f"fetch_openfema_disasters covers US states + territories only "
            f"(supports_global_query=False).",
        )
    raise router_input_error(
        sc,
        "fetch_openfema_disasters requires a spatial selector: pass state_code "
        "(2-letter USPS, e.g. 'FL') or bbox=(west, south, east, north).",
    )


def _odata_filter(states: list[str], incident: str | None, start_fy: int | None) -> str:
    state_clause = " or ".join(f"state eq '{s}'" for s in states)
    clauses = [f"({state_clause})" if len(states) > 1 else state_clause]
    if incident is not None:
        clauses.append(f"incidentType eq '{incident.replace(chr(39), chr(39) * 2)}'")
    if start_fy is not None:
        clauses.append(f"fyDeclared ge {int(start_fy)}")
    return " and ".join(clauses)


@_hooks.register_hook("openfema_disasters.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Build one page's OData query. The router's declared pagination supplies ``offset``
    and ``page_size``, so this hook states only the filter it alone can assemble."""
    sc = spec.error_code_prefix
    endpoint = spec.endpoints["data"]
    query = {
        "$filter": _odata_filter(
            _resolve_states(spec, params),
            _validate_incident_type(sc, params.get("incident_type")),
            _validate_start_year(sc, params.get("start_year")),
        ),
        "$orderby": "declarationDate desc",
        "$top": str(int(params.get("page_size", 1000))),
        "$skip": str(int(params.get("offset", 0))),
        "$format": "json",
    }
    return [_hooks.RequestPlan(
        url=endpoint.url or "", params=query,
        headers={"User-Agent": spec.auth.user_agent},
    )]


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


@_hooks.register_hook("openfema_disasters.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict[str, Any]]:
    """Reduce every declaration row to ONE geometry-less aggregate per 5-digit county
    FIPS. A statewide row (county 000) names no county and is excluded; the declared
    enrich then joins each aggregate to its polygon."""
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
            "ia_program": False,
            "pa_program": False,
        }
    )
    for rec in records:
        state = str(rec.get("fipsStateCode") or "").strip()
        county = str(rec.get("fipsCountyCode") or "").strip()
        if not state or not county or county.zfill(3) == "000":
            continue
        agg = by_fips[f"{state.zfill(2)}{county.zfill(3)}"]
        agg["n_declarations"] += 1
        dn = rec.get("disasterNumber")
        if dn is not None:
            agg["disaster_numbers"].add(str(dn))
        it = rec.get("incidentType")
        if it:
            agg["incident_types"].add(str(it))
        dt = rec.get("declarationType")
        if dt:
            agg["declaration_types"].add(str(dt))
        dd = rec.get("declarationDate")
        if dd and (agg["latest_declaration"] is None or dd > agg["latest_declaration"]):
            agg["latest_declaration"] = dd
        if rec.get("iaProgramDeclared"):
            agg["ia_program"] = True
        if rec.get("paProgramDeclared"):
            agg["pa_program"] = True

    return [
        {
            "type": "Feature",
            "geometry": None,
            "properties": {
                "county_fips": fips,
                "county_name": None,
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
            },
        }
        for fips, agg in by_fips.items()
    ]
