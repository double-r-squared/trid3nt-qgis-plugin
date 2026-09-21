"""usace_nsi hooks: the POST the National Structure Inventory is queried by.

The one irreducible step is the request: the service has no query-string bbox, so the
query is a POST whose JSON body wraps the bbox as one polygon, and it rejects an
envelope wider than a degree per axis with a 500 the ask refuses before the call."""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router import hooks as _hooks
from ..._router.errors import router_input_error

__all__ = ["build_request"]

NSI_STRUCTURES_URL = "https://nsi.sec.usace.army.mil/nsiapi/structures"

#: NSI rejects envelopes wider than ~1 degree per axis with a 500.
NSI_BBOX_MAX_SPAN_DEG = 1.0


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
