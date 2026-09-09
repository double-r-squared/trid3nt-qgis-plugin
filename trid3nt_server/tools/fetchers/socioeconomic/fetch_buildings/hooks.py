"""buildings: OSM footprints, slim inline, with the full tag bag beside them.

The inline FGB stays SLIM (osm_id / osm_type / fid) so the frontend GeoJSON is
tiny; the full OSM tag bag per footprint goes to the ``.tags.json`` sidecar the
executor writes next to it, read back cross-module by the building-detail route.

EVERY footprint whose geometry intersects the bbox is kept WHOLE, never clipped: a
building straddling an AOI edge is one building, and half of one is not a smaller
building.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.hooks import register_hook
from ..._router.hooks.osm import osm_id_of, overpass_features

__all__ = ["features"]

#: Columns the library adds that are not OSM tags, so the tag bag stays the tags.
_NON_TAG_COLUMNS: frozenset[str] = frozenset({"geometry", "nodes", "ways", "element"})


def _building_fid(el_type: Any, osm_id: Any) -> str:
    """Stable composite id ``"<first-letter-of-osm_type><osm_id>"`` (w123456 / r222)."""
    return f"{str(el_type or '')[:1]}{osm_id}"


@register_hook("buildings.features")
def features(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """``(geojson_features, tags_by_fid)`` for every building footprint in the bbox.

    The library assembles the multipolygon relations - a courtyard block is one
    footprint with its hole - and hands back the tag bag as frame columns, so both
    halves come off one read.
    """
    import pandas as pd
    from shapely.geometry import mapping

    gdf = overpass_features(spec, params, {"building": True}, timeout_s=timeout_s)
    out: list[dict[str, Any]] = []
    tags_by_fid: dict[str, dict[str, Any]] = {}
    for idx, row in gdf.iterrows():
        geom = row.geometry
        if geom is None or geom.is_empty or geom.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        el_type = str(idx[0]) if isinstance(idx, tuple) else None
        fid = _building_fid(el_type, osm_id_of(idx))
        out.append({
            "type": "Feature",
            "geometry": mapping(geom),
            "properties": {"osm_id": osm_id_of(idx), "osm_type": el_type, "fid": fid},
        })
        tags = {
            str(k): str(v)
            for k, v in row.items()
            if k not in _NON_TAG_COLUMNS and not pd.isna(v)
        }
        if tags:
            tags_by_fid[fid] = tags
    return out, tags_by_fid
