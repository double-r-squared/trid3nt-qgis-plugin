"""roads_osm delegate: ``highway`` ways CLIPPED to the exact bbox.

A road is a network you measure INSIDE an area, so unlike the coastal structures
this family clips: Overpass returns the whole way for any way with a node in the
bbox, and a way crossing the boundary several times yields several in-AOI segments
that share the way's attributes.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_input_error
from ..._router.hooks import register_hook
from ..._router.hooks.osm import clip_to_bbox, osm_id_of, overpass_features

__all__ = ["delegate", "validate"]

#: Full acceptable highway-tag vocabulary (carriageway sense; footway/cycleway/
#: track excluded by design). A value outside this set is a typed input error: an
#: invented tag is a wrong answer, not a wider search.
_VALID_ROAD_CLASSES: frozenset[str] = frozenset({
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "service", "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link", "living_street", "pedestrian", "road",
})

#: Default highway-tag set (major + arterial + link tier) when none is named.
_DEFAULT_ROAD_CLASSES: tuple[str, ...] = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "motorway_link", "trunk_link", "primary_link",
)


def _resolve_road_classes(sc: str, sfx: str, road_classes: Any) -> list[str]:
    """Validate the highway-tag set (sorted); an unknown value is a typed error.

    An explicitly-empty list is the ambiguous case: pass None for the default set
    or name at least one value.
    """
    if road_classes is None:
        return sorted(_DEFAULT_ROAD_CLASSES)
    if not isinstance(road_classes, (list, tuple)):
        raise router_input_error(
            sc, f"road_classes must be a list of highway tag values or None; "
            f"got {type(road_classes).__name__}", sfx,
        )
    if len(road_classes) == 0:
        raise router_input_error(
            sc, "road_classes is empty; pass None for the default set or supply at "
            "least one highway tag value", sfx,
        )
    for cls in road_classes:
        if cls not in _VALID_ROAD_CLASSES:
            raise router_input_error(
                sc, f"unknown highway tag value={cls!r}; allowed: "
                f"{sorted(_VALID_ROAD_CLASSES)}", sfx,
            )
    return sorted(set(road_classes))


@register_hook("roads_osm.validate")
def validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Reject an unknown highway value before the cache key is taken."""
    _resolve_road_classes(
        spec.error_code_prefix, spec.input_error_suffix, params.get("road_classes"))


@register_hook("roads_osm.delegate")
def delegate(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """Highway ways in the bbox as strictly-in-AOI LineStrings."""
    classes = _resolve_road_classes(
        spec.error_code_prefix, spec.input_error_suffix, params.get("road_classes"))
    gdf = overpass_features(spec, params, {"highway": classes}, timeout_s=timeout_s)
    bbox = tuple(float(v) for v in params["bbox"])
    out: list[dict[str, Any]] = []
    for idx, row in gdf.iterrows():
        props = {
            "osm_id": osm_id_of(idx),
            "name": row.get("name"),
            "highway": row.get("highway"),
            "lanes": row.get("lanes"),
            "maxspeed": row.get("maxspeed"),
        }
        for seg in clip_to_bbox(row.geometry, bbox):
            out.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": seg},
                "properties": dict(props),
            })
    return out
