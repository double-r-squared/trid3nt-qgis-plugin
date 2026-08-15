"""usace_nsi hooks (tier-3 hook wave): USACE National Structure Inventory points.

The one irreducible step: the NSI ``structures`` request is a POST whose query
is a JSON body -- a FeatureCollection wrapping the bbox as a polygon (NSI has no
query-string bbox) -- and the GeoJSON FeatureCollection decode (project each
structure to the preserved NSI property set, JSON-coerce nested props, and derive
the two Pelicun-consumer columns ``component_type`` <- ``occtype`` and
``replacement_value`` <- ``val_struct``). The per-axis 1-degree span cap the NSI
server enforces (oversized envelopes 500) is the one bespoke pre-fetch gate the
declarative bbox validation does not carry. Transport (the shared POST path) /
retry / cache / FGB serialize / LayerURI stay router-owned.

The POST body shape is expressed via ``RequestPlan(method="POST", json_body=...)``
(the tier-3 transport extension); the hook stays pure (it DESCRIBES the
request, the router owns the socket). Empty result -> a header-only FGB (a bbox
over open water is legitimate), never an honest-empty typed error.
"""

from __future__ import annotations

import json
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_input_error, router_upstream_error

__all__ = ["build_request", "parse_response"]

NSI_STRUCTURES_URL = "https://nsi.sec.usace.army.mil/nsiapi/structures"

#: NSI rejects envelopes wider than ~1 degree per axis with a 500.
NSI_BBOX_MAX_SPAN_DEG = 1.0

#: Properties preserved from each NSI feature (the twin's exact subset).
_PRESERVED_PROPERTIES = (
    "fd_id", "occtype", "st_damcat", "bldgtype", "found_type", "found_ht",
    "num_story", "sqft", "med_yr_blt", "val_struct", "val_cont", "val_vehic",
    "firmzone", "cbfips", "ground_elv", "ground_elv_m", "pop2amu65", "pop2amo65",
    "pop2pmu65", "pop2pmo65", "students", "source",
)

#: Pelicun-consumer convenience columns (occtype IS the HAZUS occupancy class;
#: val_struct is the canonical per-structure USD value).
_COMPONENT_TYPE_COL = "component_type"
_REPLACEMENT_VALUE_COL = "replacement_value"

#: The output column order (preserved props + the two derived Pelicun columns),
#: mirrored by the spec's ``ingest.properties`` for the honest-empty header.
OUTPUT_COLUMNS = list(_PRESERVED_PROPERTIES) + [_COMPONENT_TYPE_COL, _REPLACEMENT_VALUE_COL]


def _bbox_polygon_body(bbox: list[float]) -> dict[str, Any]:
    """Build the NSI POST body -- a FeatureCollection with the bbox as one polygon."""
    min_lon, min_lat, max_lon, max_lat = bbox
    ring = [
        [min_lon, min_lat],
        [max_lon, min_lat],
        [max_lon, max_lat],
        [min_lon, max_lat],
        [min_lon, min_lat],
    ]
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": {}}
        ],
    }


@_hooks.register_hook("usace_nsi.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Apply the per-axis span cap and build the NSI structures POST plan."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    bbox = list(params["bbox"])
    lon_span = bbox[2] - bbox[0]
    lat_span = bbox[3] - bbox[1]
    if lon_span > NSI_BBOX_MAX_SPAN_DEG or lat_span > NSI_BBOX_MAX_SPAN_DEG:
        raise router_input_error(
            sc,
            f"bbox span exceeds {NSI_BBOX_MAX_SPAN_DEG} degrees per axis "
            f"(lon_span={lon_span:.4f}, lat_span={lat_span:.4f}); NSI rejects "
            "oversized queries -- split into tiles and call once per tile.",
            sfx,
        )
    return [
        _hooks.RequestPlan(
            url=NSI_STRUCTURES_URL,
            params={"fmt": "fc"},
            headers={
                "User-Agent": spec.auth.user_agent,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
            json_body=_bbox_polygon_body(bbox),
        )
    ]


@_hooks.register_hook("usace_nsi.parse_response")
def parse_response(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Decode the NSI FeatureCollection, project props, derive the Pelicun columns."""
    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    if not raw:
        return []
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"NSI response is not valid JSON: {exc}")
    if not isinstance(obj, dict):
        raise router_upstream_error(sc, f"NSI response is not a JSON object: type={type(obj).__name__}")
    # NSI may surface errors as {"message": "..."} in a 200/4xx body.
    if "message" in obj and obj.get("type") != "FeatureCollection":
        raise router_upstream_error(sc, f"NSI returned error message: {obj.get('message')!r}")
    if obj.get("type") != "FeatureCollection":
        raise router_upstream_error(sc, f"NSI response is not a GeoJSON FeatureCollection: type={obj.get('type')!r}")

    out: list[dict[str, Any]] = []
    for feat in obj.get("features") or []:
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
        occtype = props.get("occtype")
        row[_COMPONENT_TYPE_COL] = occtype if (isinstance(occtype, str) and occtype) else None
        val_struct = props.get("val_struct")
        row[_REPLACEMENT_VALUE_COL] = (
            float(val_struct) if (isinstance(val_struct, (int, float)) and not isinstance(val_struct, bool) and math.isfinite(val_struct)) else None
        )
        out.append({"type": "Feature", "geometry": geom, "properties": row})
    return out
