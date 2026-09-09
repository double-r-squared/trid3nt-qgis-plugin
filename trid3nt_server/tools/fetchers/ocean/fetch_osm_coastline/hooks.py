"""osm_coastline delegate: ``natural=coastline`` ways as whole land/water edges.

UNCLIPPED, with the vertex ORDER carried through untouched: the LAND IS ON THE LEFT of a
coastline way's direction, and that is the only thing making an open line an edge. A way
cut at the AOI edge loses the side it was carrying at the cut."""

# A lake is deliberately absent: inland water is mapped as polygons with no coastline
# way, so an inland box comes back with nothing rather than with a shore that is not
# one.

from __future__ import annotations

from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.hooks import register_hook
from ..._router.hooks.osm import osm_id_of, overpass_features

__all__ = ["delegate"]


def _line_coords(geom: Any) -> list[list[float]] | None:
    """The way's vertices in the ORDER they were drawn, whatever shape it closed into. A
    closed way reaches the library as a Polygon, whose exterior ring is the same node
    sequence, so the edge keeps its side."""
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "LineString":
        coords = list(geom.coords)
    elif geom.geom_type == "Polygon":
        coords = list(geom.exterior.coords)
    else:
        return None
    return [[float(x), float(y)] for x, y in coords] if len(coords) >= 2 else None


@register_hook("osm_coastline.delegate")
def delegate(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """Coastline ways in the bbox as whole LineStrings. Empty is a legitimate answer --
    an inland box has no coastline -- and the consumer decides what absence means."""
    gdf = overpass_features(spec, params, {"natural": "coastline"}, timeout_s=timeout_s)
    out: list[dict[str, Any]] = []
    for idx, row in gdf.iterrows():
        coords = _line_coords(row.geometry)
        if coords is None:
            continue
        out.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "osm_id": osm_id_of(idx),
                "name": row.get("name"),
                "natural": row.get("natural"),
            },
        })
    return out
