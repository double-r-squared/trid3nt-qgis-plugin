"""epa_frs_facilities hooks (tier-3 http_json mode/0066): EPA regulated-facility
points by program, multi-layer UNION.

The wave-11 deferral was a 5-layer fan-out UNION plus a Superfund geometry
synthesized from LAT/LON attribute columns. Both fold onto the EXISTING multi-plan
build_request/parse_response path with ZERO new machinery: ``build_request`` expands the
facility_program enum into the ordered layer set (the "frs" union = 5 point layers, a
single program = 1, superfund = 1 esri-json layer) and emits one RequestPlan per layer;
``parse_response`` decodes the bodies IN THAT ORDER (point layers -> common point schema;
superfund -> point-from-LAT/LON synthesis), stamps program/label, and unions.

Single-page per layer (no next_page) caps each layer at the server maxRecordCount (2000)
where the twin paged to 20000/layer -- a realistic small-AOI query is value-identical; a
dense state-scale bbox truncates earlier (divergence, advisory payload gate warns
first). All I/O stays router-owned.
"""

from __future__ import annotations

import json
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_input_error as _router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response", "FACILITY_PROGRAMS", "PROGRAM_ALIASES"]


def router_input_error(sc, msg, suffix="INPUT_INVALID"):
    """Stamp the twin's EPA_FRS_INPUT_INVALID suffix (EpaFrsInputError)."""
    return _router_input_error(sc, msg, suffix)

_EPA_BASE = (
    "https://geopub.epa.gov/arcgis/rest/services/"
    "NEPAssist/NEPAVELayersPublic_fgdb/MapServer"
)
_PAGE_SIZE = 2000

#: program -> (layer_id, label, is_polygon). Polygon (Superfund) carries the point
#: in LATITUDE/LONGITUDE columns.
FACILITY_PROGRAMS: dict[str, tuple[int, str, bool]] = {
    "tri": (15, "Toxic Release (TRI)", False),
    "water": (16, "Water Discharger (NPDES)", False),
    "hazwaste": (17, "Hazardous Waste (RCRA)", False),
    "air": (18, "Air Emissions", False),
    "brownfields": (13, "Brownfield", False),
    "superfund": (14, "Superfund (NPL)", True),
}
FRS_UNION_PROGRAMS: list[str] = ["tri", "water", "hazwaste", "air", "brownfields"]
PROGRAM_ALIASES: dict[str, str] = {
    "frs": "frs", "all": "frs", "facilities": "frs", "regulated": "frs",
    "regulated_facilities": "frs", "epa": "frs", "toxic_release": "tri",
    "toxic_releases": "tri", "tris": "tri", "toxics": "tri", "npl": "superfund",
    "sems": "superfund", "cercla": "superfund", "superfund_npl": "superfund",
    "npdes": "water", "water_discharger": "water", "water_dischargers": "water",
    "discharger": "water", "wastewater": "water", "rcra": "hazwaste",
    "rcrainfo": "hazwaste", "hazardous_waste": "hazwaste", "hazwaste_facilities": "hazwaste",
    "air_emissions": "air", "air_emission": "air", "afs": "air",
    "brownfield": "brownfields", "acres": "brownfields",
}
_OUTPUT_COLUMNS: list[str] = [
    "registry_id", "program", "program_label", "program_acronym", "program_id",
    "facility_name", "address", "city", "county", "state", "postal_code",
    "epa_region", "facility_url", "npl_status",
]


def _resolve_program(sc: str, facility_program: Any) -> str:
    if facility_program is None:
        return "frs"
    if not isinstance(facility_program, str) or not facility_program.strip():
        raise router_input_error(sc, f"facility_program must be a non-empty string; got "
                                     f"{type(facility_program).__name__}: {facility_program!r}. "
                                     f"Valid values: {['frs'] + sorted(FACILITY_PROGRAMS)}")
    key = facility_program.strip().lower().replace(" ", "_").replace("-", "_")
    key = PROGRAM_ALIASES.get(key, key)
    if key != "frs" and key not in FACILITY_PROGRAMS:
        raise router_input_error(sc, f"facility_program={facility_program!r} is not supported; "
                                     f"valid values: {['frs'] + sorted(FACILITY_PROGRAMS)} "
                                     f"(aliases also accepted, e.g. 'all', 'toxic_release', 'npl', "
                                     f"'npdes', 'rcra')")
    return key


def _programs(sc: str, params: dict[str, Any]) -> list[str]:
    key = _resolve_program(sc, params.get("facility_program"))
    return list(FRS_UNION_PROGRAMS) if key == "frs" else [key]


def _layer_plan(spec: SourceSpec, params: dict[str, Any], program: str) -> "_hooks.RequestPlan":
    layer_id, _label, is_polygon = FACILITY_PROGRAMS[program]
    b = params["bbox"]
    q: dict[str, str] = {
        "where": "1=1",
        "geometry": f"{b[0]},{b[1]},{b[2]},{b[3]}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "outSR": "4326",
        "resultOffset": "0",
        "resultRecordCount": str(_PAGE_SIZE),
        "orderByFields": "OBJECTID ASC",
    }
    if is_polygon:
        q["f"] = "json"
        q["returnGeometry"] = "false"
    else:
        q["f"] = "geojson"
    return _hooks.RequestPlan(url=f"{_EPA_BASE}/{layer_id}/query", params=q,
                              headers={"User-Agent": spec.auth.user_agent})


@_hooks.register_hook("epa_frs_facilities.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Expand facility_program into the ordered layer set; one plan per layer."""
    sc = spec.error_code_prefix
    return [_layer_plan(spec, params, prog) for prog in _programs(sc, params)]


def _normalize_point(feat: dict[str, Any], program: str, label: str) -> dict[str, Any] | None:
    if not isinstance(feat, dict):
        return None
    geom = feat.get("geometry")
    if geom is None or geom.get("type") != "Point":
        return None
    coords = geom.get("coordinates")
    if not isinstance(coords, (list, tuple)) or len(coords) < 2:
        return None
    if not (math.isfinite(coords[0]) and math.isfinite(coords[1])):
        return None
    p = feat.get("properties") or {}
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [coords[0], coords[1]]},
            "properties": {
                "registry_id": p.get("registry_id"), "program": program, "program_label": label,
                "program_acronym": p.get("pgm_sys_acrnm"), "program_id": p.get("pgm_sys_id"),
                "facility_name": p.get("primary_name"), "address": p.get("location_address"),
                "city": p.get("city_name"), "county": p.get("county_name"), "state": p.get("state_code"),
                "postal_code": p.get("postal_code"), "epa_region": p.get("epa_region"),
                "facility_url": p.get("facility_url"), "npl_status": None}}


def _normalize_superfund(feat: dict[str, Any], label: str) -> dict[str, Any] | None:
    if not isinstance(feat, dict):
        return None
    attrs = feat.get("attributes") if "attributes" in feat else feat
    if not isinstance(attrs, dict):
        return None
    try:
        lat_f = float(attrs.get("LATITUDE")); lon_f = float(attrs.get("LONGITUDE"))
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
        return None
    if not (-180.0 <= lon_f <= 180.0 and -90.0 <= lat_f <= 90.0):
        return None
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon_f, lat_f]},
            "properties": {
                "registry_id": attrs.get("EPA_ID"), "program": "superfund", "program_label": label,
                "program_acronym": "SEMS/NPL", "program_id": attrs.get("EPA_ID"),
                "facility_name": attrs.get("Site_Name"), "address": attrs.get("Address"),
                "city": attrs.get("City"), "county": attrs.get("County"), "state": attrs.get("State"),
                "postal_code": attrs.get("Zip_Code"), "epa_region": attrs.get("Region"),
                "facility_url": attrs.get("FACILITY_URL"), "npl_status": attrs.get("NPL_Status")}}


def _decode(sc: str, body: bytes, program: str, is_polygon: bool) -> list[dict[str, Any]]:
    if not body:
        return []
    try:
        obj = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"EPA FRS returned non-JSON program={program}: {exc}")
    if not isinstance(obj, dict):
        raise router_upstream_error(sc, f"EPA FRS response is not a JSON object program={program}")
    if "error" in obj:
        raise router_upstream_error(sc, f"EPA FRS query returned error envelope program={program}: {obj['error']}")
    if is_polygon:
        feats = obj.get("features")
        if feats is None:
            raise router_upstream_error(sc, f"EPA FRS ESRI-JSON response missing 'features' program={program}")
        return feats or []
    if obj.get("type") != "FeatureCollection":
        raise router_upstream_error(sc, f"EPA FRS response is not a GeoJSON FeatureCollection program={program}: type={obj.get('type')!r}")
    return obj.get("features", []) or []


@_hooks.register_hook("epa_frs_facilities.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Decode each body by its program (build order), normalize + stamp, union."""
    sc = spec.error_code_prefix
    programs = _programs(sc, params)
    out: list[dict[str, Any]] = []
    for program, body in zip(programs, bodies):
        _layer_id, label, is_polygon = FACILITY_PROGRAMS[program]
        for feat in _decode(sc, body, program, is_polygon):
            norm = _normalize_superfund(feat, label) if is_polygon else _normalize_point(feat, program, label)
            if norm is not None:
                out.append(norm)
    return out
