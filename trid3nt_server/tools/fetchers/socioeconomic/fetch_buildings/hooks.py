"""buildings: OSM footprints, slim inline.

EVERY footprint intersecting the bbox is kept WHOLE: half a building is not a
smaller building. A bbox with no mapped footprint is a typed empty: there is no
second source to fall back to."""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error
from ..._router.hooks import register_hook
from ..._router.hooks.osm import osm_id_of, overpass_features

__all__ = ["features"]


@register_hook("buildings.features")
def features(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """GeoJSON features for every footprint in the bbox. The library assembles the
    multipolygon relations, so a courtyard block is one footprint with its hole."""
    from shapely.geometry import mapping

    gdf = overpass_features(spec, params, {"building": True}, timeout_s=timeout_s)
    out: list[dict[str, Any]] = []
    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        el_type = str(idx[0]) if isinstance(idx, tuple) else None
        osm_id = osm_id_of(idx)
        out.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {"osm_id": osm_id, "osm_type": el_type,
                           "fid": f"{(el_type or '')[:1]}{osm_id}"},
        })
    if not out:
        raise router_empty_error(
            spec.error_code_prefix,
            f"No OpenStreetMap building footprints intersect bbox={params.get('bbox')!r} "
            f"(the area may be unmapped in OSM).",
            spec.empty_error_suffix,
        )
    return out
