"""fema_nfhl_zones hooks (tier-3 chained-resolution mode, ADR 0063/0066): FEMA NFHL
regulatory flood-zone polygons, OBJECTID-cursor paged.

The wave-11 deferral (ADR 0059) was OBJECTID-cursor pagination: the NFHL endpoint
500s on ``resultOffset>0`` against a bbox-filtered selection, so the twin walks an
``OBJECTID>watermark`` cursor instead. That cursor IS a ``next_page`` variant -- the
pure offset/endOfRecords loop control the declarative pager cannot express (ADR 0063)
-- so it folds onto the EXISTING chained-resolution ``next_page`` hook with ZERO new
machinery: ``build_request`` builds page 1 (``OBJECTID>0`` + the sfha/zone server
filter), ``next_page`` advances the cursor to the page's max OBJECTID (stop on a short
page), ``parse_response`` projects to the regulatory-zone semantic columns.

sfha_only (``SFHA_TF='T'``) and zone_filter (``FLD_ZONE IN (...)``) are applied
SERVER-side in the where clause (the twin filtered zone_filter client-side; the feature
SET is value-identical -- ADR 0066 divergence). All I/O stays router-owned.
"""

from __future__ import annotations

import json
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_input_error, router_upstream_error

__all__ = ["build_request", "next_page", "parse_response", "VALID_FLOOD_ZONES"]

_NFHL_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)

#: NFHL FeatureServer page size. maxRecordCount is 2000, but a documented FEMA
#: quirk 500s the cursor-paged request at 2000; 1000 reliably round-trips.
_PAGE_SIZE = 1000

#: Properties preserved from each NFHL feature (the regulatory-flood semantic core).
_PRESERVED_PROPERTIES: tuple[str, ...] = (
    "FLD_ZONE", "ZONE_SUBTY", "SFHA_TF", "STATIC_BFE", "V_DATUM", "DEPTH",
    "LEN_UNIT", "VELOCITY", "VEL_UNIT", "DFIRM_ID", "FLD_AR_ID", "STUDY_TYP",
    "SOURCE_CIT", "GFID",
)

#: outFields includes OBJECTID so the cursor can read the watermark; OBJECTID is
#: NOT part of the regulatory core and is stripped from the FGB output.
_OUT_FIELDS = "OBJECTID," + ",".join(_PRESERVED_PROPERTIES)

#: Canonical FEMA flood-zone designations accepted by zone_filter (D_FLD_ZONE domain).
VALID_FLOOD_ZONES: frozenset[str] = frozenset({
    "A", "AE", "AH", "AO", "AR", "A99", "V", "VE", "X", "D", "B", "C",
    "AREA NOT INCLUDED", "OPEN WATER",
})


def _validate_zone_filter(sc: str, raw: Any) -> list[str] | None:
    """Uppercase + validate zone_filter against VALID_FLOOD_ZONES; raise on unknown."""
    if not raw:
        return None
    if not isinstance(raw, (list, tuple)):
        raise router_input_error(
            sc, f"zone_filter must be a list[str] or None; got {type(raw).__name__}",
            "INPUT_INVALID",
        )
    out: list[str] = []
    for z in raw:
        if not isinstance(z, str):
            raise router_input_error(
                sc, f"zone_filter entries must be str; got {type(z).__name__}",
                "INPUT_INVALID",
            )
        zu = z.upper()
        if zu not in VALID_FLOOD_ZONES:
            raise router_input_error(
                sc, f"zone_filter entry {z!r} not in known NFHL zone codes "
                    f"{sorted(VALID_FLOOD_ZONES)}",
                "INPUT_INVALID",
            )
        if zu not in out:
            out.append(zu)
    return sorted(out) or None


def _server_where(sc: str, params: dict[str, Any], last_oid: int) -> str:
    """Build the cursor where clause: OBJECTID>watermark [AND SFHA_TF='T'] [AND FLD_ZONE IN (...)]."""
    parts = [f"OBJECTID>{int(last_oid)}"]
    if bool(params.get("sfha_only")):
        parts.append("SFHA_TF='T'")
    zones = _validate_zone_filter(sc, params.get("zone_filter"))
    if zones:
        ins = ",".join(f"'{z}'" for z in zones)
        parts.append(f"FLD_ZONE IN ({ins})")
    return " AND ".join(parts)


def _page_plan(spec: SourceSpec, params: dict[str, Any], last_oid: int) -> "_hooks.RequestPlan":
    b = params["bbox"]
    q = {
        "where": _server_where(spec.error_code_prefix, params, last_oid),
        "geometry": f"{b[0]},{b[1]},{b[2]},{b[3]}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326",
        "outFields": _OUT_FIELDS,
        "outSR": "4326",
        "f": "geojson",
        "resultRecordCount": str(_PAGE_SIZE),
        "orderByFields": "OBJECTID",
    }
    return _hooks.RequestPlan(url=_NFHL_URL, params=q, headers={"User-Agent": spec.auth.user_agent})


@_hooks.register_hook("fema_nfhl_zones.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Validate sfha_only/zone_filter, build page 1 (OBJECTID>0 cursor start)."""
    sc = spec.error_code_prefix
    if not isinstance(params.get("sfha_only", False), bool):
        raise router_input_error(sc, "sfha_only must be bool", "INPUT_INVALID")
    _validate_zone_filter(sc, params.get("zone_filter"))  # raise early on bad zone
    return [_page_plan(spec, params, 0)]


def _page_features(sc: str, body: bytes) -> list[dict[str, Any]]:
    if not body:
        return []
    try:
        obj = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"FEMA NFHL returned non-JSON: {exc}")
    if isinstance(obj, dict) and "error" in obj:
        raise router_upstream_error(sc, f"FEMA NFHL query returned error envelope: {obj['error']}")
    if not isinstance(obj, dict) or obj.get("type") != "FeatureCollection":
        raise router_upstream_error(
            sc, f"FEMA NFHL response is not a GeoJSON FeatureCollection: "
                f"type={obj.get('type') if isinstance(obj, dict) else type(obj).__name__!r}"
        )
    return obj.get("features", []) or []


@_hooks.register_hook("fema_nfhl_zones.next_page")
def next_page(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> "_hooks.RequestPlan | None":
    """Advance the OBJECTID cursor to the page's max OBJECTID; stop on a short page."""
    sc = spec.error_code_prefix
    page = _page_features(sc, bodies[-1])
    if len(page) < _PAGE_SIZE:
        return None
    oids = [
        int((f.get("properties") or {}).get("OBJECTID", 0))
        for f in page
        if (f.get("properties") or {}).get("OBJECTID") is not None
    ]
    if not oids:
        return None
    new_last = max(oids)
    # Compute the cumulative max seen so far as the cursor floor (guards a
    # non-advancing server).
    prev_max = 0
    for b in bodies:
        for f in _page_features(sc, b):
            oid = (f.get("properties") or {}).get("OBJECTID")
            if oid is not None:
                prev_max = max(prev_max, int(oid))
    if new_last <= 0 or new_last < prev_max:
        return None
    return _page_plan(spec, params, new_last)


@_hooks.register_hook("fema_nfhl_zones.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Decode all pages; project to the 14 regulatory columns (OBJECTID stripped)."""
    sc = spec.error_code_prefix
    out: list[dict[str, Any]] = []
    for body in bodies:
        for feat in _page_features(sc, body):
            if not isinstance(feat, dict):
                continue
            geom = feat.get("geometry")
            if geom is None:
                continue
            props = feat.get("properties") or {}
            row: dict[str, Any] = {}
            for key in _PRESERVED_PROPERTIES:
                v = props.get(key)
                if isinstance(v, (dict, list)):
                    v = json.dumps(v)
                row[key] = v
            out.append({"type": "Feature", "geometry": geom, "properties": row})
    return out
