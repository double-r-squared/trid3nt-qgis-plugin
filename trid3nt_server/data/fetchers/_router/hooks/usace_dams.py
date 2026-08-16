"""usace_dams hooks (tier-3 chained-resolution mode/0066): USACE National
Inventory of Dams points, offset-paged, KEYED (missing-key parity).

The wave-11 deferral was a credential-gated dual-endpoint with a non-maskable
auth error + list IN filters. Both fold onto the EXISTING hooks under the
keyed-source rule (never register a real key; the keyless path is the parity surface):
``build_request`` resolves the token (kwarg -> str secret_ref -> ``TRID3NT_USACE_NID_TOKEN``
env), normalizes the hazard_potential / state / min_height filters into an ``IN (...)`` /
``DAM_HEIGHT >=`` where clause (the bespoke controlled-vocab + USPS normalization is the
one irreducible pure step), and builds the page-1 query; ``next_page`` does offset paging;
``parse_response`` decodes the geojson into the NID point schema.

KEYLESS path (no token resolves) -> the PUBLIC ESRI Living Atlas mirror -> byte-parity
provable. The AUTHORITATIVE endpoint + the non-maskable auth-card path + the
authoritative->mirror non-auth fallback are only reachable WITH a token, which this wave
never registers: they are honestly BLOCKED-ON-KEY (divergence), not blocked-on-mode.
All I/O stays router-owned.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_input_error as _router_input_error, router_upstream_error

__all__ = ["build_request", "next_page", "parse_response", "VALID_HAZARD_POTENTIALS"]


def router_input_error(sc, msg, suffix="INPUT_INVALID"):
    """Stamp the twin's USACE_DAMS_INPUT_INVALID suffix (USACEDAMSInputError)."""
    return _router_input_error(sc, msg, suffix)

_NID_BASE = (
    "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/"
    "NID_v1/FeatureServer/0/query"
)
_NID_AUTHORITATIVE_BASE = (
    "https://geospatial.sec.usace.army.mil/server/rest/services/"
    "NID/NID/MapServer/0/query"
)
_NID_TOKEN_ENV = "TRID3NT_USACE_NID_TOKEN"
_PAGE_SIZE = 2000

VALID_HAZARD_POTENTIALS: dict[str, str] = {
    "high": "High", "significant": "Significant", "low": "Low", "undetermined": "Undetermined",
}
PRESERVED_PROPERTIES: tuple[str, ...] = (
    "OBJECTID", "NIDID", "FEDERAL_ID", "NAME", "OTHER_NAMES", "STATE", "COUNTYSTATE",
    "CITY", "LATITUDE", "LONGITUDE", "RIVER_OR_STREAM", "CONGDIST", "OWNER_TYPES",
    "PRIMARY_OWNER_TYPE", "STATE_REGULATED", "STATE_JURISDICTION", "STATE_REGULATORY_AGENCY",
    "PRIMARY_SOURCE_AGENCY", "PRIMARY_PURPOSE", "PURPOSES", "PRIMARY_DAM_TYPE", "DAM_TYPES",
    "DAM_HEIGHT", "HYDRAULIC_HEIGHT", "STRUCTURAL_HEIGHT", "NID_HEIGHT", "DAM_LENGTH",
    "DAM_VOLUME", "YEAR_COMPLETED", "NID_STORAGE", "MAX_STORAGE", "NORMAL_STORAGE",
    "SURFACE_AREA", "DRAINAGE_AREA", "MAX_DISCHARGE", "SPILLWAY_TYPE", "SPILLWAY_WIDTH",
    "HAZARD_POTENTIAL", "CONDITION_ASSESSMENT", "CONDITION_ASSESS_DATE", "EAP_PREPARED",
    "EAP_LAST_REV_DATE", "LAST_INSPECTION_DATE", "INSPECTION_FREQUENCY", "OPERATIONAL_STATUS",
    "OPERATIONAL_STATUS_DATE", "DATA_UPDATED",
)
_USPS_TO_NAME: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District Of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico", "GU": "Guam", "VI": "Virgin Islands", "AS": "American Samoa",
}


def _sql_escape(v: str) -> str:
    return v.replace("'", "''")


def _norm_hazard(sc: str, raw: Any) -> list[str]:
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else None
    if items is None:
        raise router_input_error(sc, f"hazard_potential must be a str or list of str; got {type(raw).__name__}")
    out: list[str] = []
    for it in items:
        if not isinstance(it, str):
            raise router_input_error(sc, f"hazard_potential entries must be str; got {it!r}")
        canon = VALID_HAZARD_POTENTIALS.get(it.strip().lower())
        if canon is None:
            raise router_input_error(sc, f"hazard_potential {it!r} is not a valid NID classification; "
                                         f"expected one of {sorted(set(VALID_HAZARD_POTENTIALS.values()))}")
        if canon not in out:
            out.append(canon)
    return out


def _norm_state(sc: str, raw: Any) -> list[str]:
    if raw is None:
        return []
    items = [raw] if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else None
    if items is None:
        raise router_input_error(sc, f"state must be a str or list of str; got {type(raw).__name__}")
    out: list[str] = []
    for it in items:
        if not isinstance(it, str):
            raise router_input_error(sc, f"state entries must be str; got {it!r}")
        s = it.strip()
        if not s:
            raise router_input_error(sc, "state entries must be non-empty")
        canon = _USPS_TO_NAME[s.upper()] if len(s) == 2 and s.upper() in _USPS_TO_NAME else " ".join(w.capitalize() for w in s.split())
        if canon not in out:
            out.append(canon)
    return out


def _norm_min_height(sc: str, raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise router_input_error(sc, f"min_height_ft must be a number (feet); got {raw!r}")
    if isinstance(raw, str):
        try:
            raw = float(raw.strip())
        except ValueError:
            raise router_input_error(sc, f"min_height_ft must be a number (feet); got {raw!r}") from None
    if not isinstance(raw, (int, float)):
        raise router_input_error(sc, f"min_height_ft must be a number (feet); got {type(raw).__name__}")
    v = float(raw)
    if math.isnan(v) or math.isinf(v):
        raise router_input_error(sc, f"min_height_ft must be finite; got {raw!r}")
    if v < 0:
        raise router_input_error(sc, f"min_height_ft must be >= 0; got {raw!r}")
    return v


def _where(hazard: list[str], states: list[str], min_h: float | None) -> str:
    clauses: list[str] = []
    if hazard:
        clauses.append("HAZARD_POTENTIAL IN (" + ",".join(f"'{_sql_escape(h)}'" for h in hazard) + ")")
    if states:
        clauses.append("STATE IN (" + ",".join(f"'{_sql_escape(s)}'" for s in states) + ")")
    if min_h is not None:
        clauses.append(f"DAM_HEIGHT >= {min_h:g}")
    return " AND ".join(clauses) if clauses else "1=1"


def _resolve_token(params: dict[str, Any]) -> str | None:
    token = params.get("token")
    if token:
        return str(token)
    sr = params.get("secret_ref")
    if isinstance(sr, str) and sr:
        return sr
    env = os.environ.get(_NID_TOKEN_ENV)
    return env or None


def _page_plan(spec: SourceSpec, params: dict[str, Any], offset: int) -> "_hooks.RequestPlan":
    sc = spec.error_code_prefix
    where = _where(
        _norm_hazard(sc, params.get("hazard_potential")),
        _norm_state(sc, params.get("state")),
        _norm_min_height(sc, params.get("min_height_ft")),
    )
    token = _resolve_token(params)
    base = _NID_AUTHORITATIVE_BASE if token else _NID_BASE
    q = {
        "where": where,
        "outFields": ",".join(PRESERVED_PROPERTIES),
        "outSR": "4326",
        "f": "geojson",
        "resultOffset": str(offset),
        "resultRecordCount": str(_PAGE_SIZE),
        "orderByFields": "OBJECTID ASC",
    }
    bbox = params.get("bbox")
    if bbox is not None:
        q["geometry"] = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
        q["geometryType"] = "esriGeometryEnvelope"
        q["spatialRel"] = "esriSpatialRelIntersects"
        q["inSR"] = "4326"
    if token:
        q["token"] = token
    return _hooks.RequestPlan(url=base, params=q, headers={"User-Agent": spec.auth.user_agent})


@_hooks.register_hook("usace_dams.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Validate filters + resolve token, build page 1 (mirror keyless / authoritative keyed)."""
    return [_page_plan(spec, params, 0)]


def _page_features(sc: str, body: bytes) -> list[dict[str, Any]]:
    if not body:
        return []
    try:
        obj = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"USACE NID returned non-JSON: {exc}")
    if isinstance(obj, dict) and "error" in obj:
        raise router_upstream_error(sc, f"USACE NID query returned error envelope: {obj['error']}")
    if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
        raise router_upstream_error(sc, f"USACE NID response is not a GeoJSON FeatureCollection")
    return obj.get("features", []) or []


@_hooks.register_hook("usace_dams.next_page")
def next_page(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> "_hooks.RequestPlan | None":
    """Offset paging: next offset by page size; stop on a short page."""
    sc = spec.error_code_prefix
    page = _page_features(sc, bodies[-1])
    if len(page) < _PAGE_SIZE:
        return None
    return _page_plan(spec, params, len(bodies) * _PAGE_SIZE)


@_hooks.register_hook("usace_dams.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Decode all pages; project to the NID PRESERVED_PROPERTIES schema."""
    sc = spec.error_code_prefix
    out: list[dict[str, Any]] = []
    for body in bodies:
        for feat in _page_features(sc, body):
            if not isinstance(feat, dict) or feat.get("geometry") is None:
                continue
            props = feat.get("properties") or {}
            row: dict[str, Any] = {}
            for key in PRESERVED_PROPERTIES:
                v = props.get(key)
                if isinstance(v, (dict, list)):
                    v = json.dumps(v)
                row[key] = v
            out.append({"type": "Feature", "geometry": feat.get("geometry"), "properties": row})
    return out
