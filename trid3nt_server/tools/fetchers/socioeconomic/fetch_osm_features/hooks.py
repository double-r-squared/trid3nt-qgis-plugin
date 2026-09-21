"""osm_features delegate: one Overpass query surface, four feature classes.

The API answers one interpreter endpoint with a tag filter; ``feature`` names which
class this call asks for and each class owns its own tag resolution, clip-or-keep-
whole rule and property shape. ``roads`` is a network measured INSIDE the bbox and
clips; ``coastline`` and ``breakwaters`` are edges/objects that clipping would
falsify and are kept WHOLE; ``pois`` reduces any tagged element to its
representative point and refuses a zero-match query by name. Waterways
(``fetch_river_geometry``) is a separate source -- see the source.yaml comment."""

from __future__ import annotations

import json
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error
from ..._router.hooks import register_hook
from ..._router.hooks.osm import clip_to_bbox, osm_id_of, overpass_features

__all__ = ["validate", "delegate"]

_VALID_ROAD_CLASSES: frozenset[str] = frozenset({
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "service", "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link", "living_street", "pedestrian", "road",
})
_DEFAULT_ROAD_CLASSES: tuple[str, ...] = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "motorway_link", "trunk_link", "primary_link",
)

#: Bare-value -> OSM key alias map (the ``amenity="hospital"`` shortcut vocabulary).
_VALUE_KEY_ALIASES: dict[str, str] = {
    "hospital": "amenity", "clinic": "amenity", "doctors": "amenity",
    "pharmacy": "amenity", "school": "amenity", "college": "amenity",
    "university": "amenity", "kindergarten": "amenity", "fire_station": "amenity",
    "police": "amenity", "townhall": "amenity", "place_of_worship": "amenity",
    "shelter": "amenity", "community_centre": "amenity", "fuel": "amenity",
    "bank": "amenity", "restaurant": "amenity", "supermarket": "shop",
    "convenience": "shop",
}

#: The OSM ``man_made`` values that are wave BARRIERS. ``pier`` is excluded on
#: purpose: it is the berthing dock being sheltered, not a wave barrier.
_BARRIER_VALUES: tuple[str, ...] = ("breakwater", "groyne")

#: Columns the library adds that are not OSM tags, so the tag bag stays the tags.
_NON_TAG_COLUMNS: frozenset[str] = frozenset({"geometry", "nodes", "ways", "element"})


def _resolve_road_classes(sc: str, sfx: str, road_classes: Any) -> list[str]:
    """Validate the highway-tag set, sorted; an unknown value is a typed error. An
    EXPLICITLY empty list is the ambiguous case and refuses: pass None for the
    default set, or name at least one value."""
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


def _is_clean_token(s: str) -> bool:
    """True iff ``s`` is a plausible OSM key/value token (no metacharacters)."""
    if not s:
        return False
    return not any(ch.isspace() or ch in '"\\[](){};' for ch in s)


def _resolve_tag(sc: str, sfx: str, params: dict[str, Any]) -> tuple[str, str]:
    """Resolve the caller's tag inputs to ONE ``(key, value)`` pair, in priority
    order: ``amenity``, then ``tag``, then ``category``, then ``value``. A missing
    selector or an unmappable bare value is a typed non-retryable input error."""
    tag, amenity = params.get("tag"), params.get("amenity")
    category, value = params.get("category"), params.get("value")

    candidate: str | None = None
    forced_key: str | None = None
    if isinstance(amenity, str) and amenity.strip():
        forced_key, candidate = "amenity", amenity.strip()
    elif isinstance(tag, str) and tag.strip():
        candidate = tag.strip()
    elif isinstance(category, str) and category.strip():
        candidate = category.strip()
    elif isinstance(value, str) and value.strip():
        candidate = value.strip()

    if candidate is None:
        raise router_input_error(
            sc, "no POI tag supplied; pass one of: tag='key=value' (e.g. "
            "'amenity=hospital' or 'emergency=fire_hydrant'), amenity='hospital', "
            "or category='school'.", sfx,
        )
    if forced_key is not None:
        key, val = forced_key, candidate
    elif "=" in candidate:
        key, _, val = candidate.partition("=")
        key, val = key.strip(), val.strip()
    else:
        val = candidate
        key = _VALUE_KEY_ALIASES.get(val.lower(), "")
        if not key:
            raise router_input_error(
                sc, f"could not infer an OSM key for value={candidate!r}; pass an "
                f"explicit tag='key=value' (e.g. 'amenity={candidate}' or "
                f"'shop={candidate}'). Known bare values: "
                f"{sorted(_VALUE_KEY_ALIASES)}", sfx,
            )
    if not _is_clean_token(key) or not _is_clean_token(val):
        raise router_input_error(
            sc, f"tag key/value must be clean OSM tokens (no spaces / quotes / "
            f"brackets); got key={key!r} value={val!r}", sfx,
        )
    return key, val


def _resolve_barrier_values(sc: str, sfx: str, structure_type: Any) -> list[str]:
    """Validate the requested barrier vocabulary; unknown -> typed input error."""
    if structure_type is None:
        return list(_BARRIER_VALUES)
    if isinstance(structure_type, str):
        tokens = [t for t in structure_type.replace(",", " ").split() if t]
    elif isinstance(structure_type, (list, tuple)):
        tokens = [str(t).strip() for t in structure_type if str(t).strip()]
    else:
        raise router_input_error(
            sc, f"structure_type must be a str or list of str; got "
            f"{type(structure_type).__name__}", sfx)
    out: list[str] = []
    for tok in tokens:
        low = tok.lower()
        if low not in _BARRIER_VALUES:
            raise router_input_error(
                sc, f"unsupported structure_type {tok!r}; allowed OSM man_made "
                f"barrier values: {', '.join(_BARRIER_VALUES)}. A pier is "
                "deliberately excluded - it is the dock being sheltered, not a "
                "wave barrier.", sfx)
        if low not in out:
            out.append(low)
    return out or list(_BARRIER_VALUES)


@register_hook("osm_features.validate")
def validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Reject a class-specific bad ask before the cache key is taken; ``feature``
    itself is an enum param the router's own validation already closes."""
    sc, sfx = spec.error_code_prefix, spec.input_error_suffix
    feature = params.get("feature")
    if feature == "roads":
        _resolve_road_classes(sc, sfx, params.get("road_classes"))
    elif feature == "pois":
        _resolve_tag(sc, sfx, params)
    elif feature == "breakwaters":
        _resolve_barrier_values(sc, sfx, params.get("structure_type"))


def _line_coords(geom: Any) -> list[list[float]] | None:
    """A way's vertices in the ORDER they were drawn, whatever shape it closed
    into. A closed way reaches the library as a Polygon, whose exterior ring is
    the same node sequence, so a coastline edge keeps its side."""
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "LineString":
        coords = list(geom.coords)
    elif geom.geom_type == "Polygon":
        coords = list(geom.exterior.coords)
    else:
        return None
    return [[float(x), float(y)] for x, y in coords] if len(coords) >= 2 else None


def _representative_point(geom: Any) -> tuple[float, float] | None:
    """A node's own coordinate, or the centre of an element's bounding box."""
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "Point":
        return float(geom.x), float(geom.y)
    minx, miny, maxx, maxy = geom.bounds
    return (minx + maxx) / 2.0, (miny + maxy) / 2.0


def _tag_bag(row: Any) -> dict[str, Any]:
    """The element's own tags, in the shape OSM published them."""
    import pandas as pd

    return {
        str(k): str(v)
        for k, v in row.items()
        if k not in _NON_TAG_COLUMNS and not pd.isna(v)
    }


def _roads_features(
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


def _pois_features(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """Elements carrying the resolved tag, as Points strictly inside the bbox.
    Zero features is a typed non-retryable refusal, never an empty-success layer:
    an ask that finds none is an answer the caller has to see as one."""
    sc, sfx = spec.error_code_prefix, spec.input_error_suffix
    key, value = _resolve_tag(sc, sfx, params)
    gdf = overpass_features(spec, params, {key: value}, timeout_s=timeout_s)
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in params["bbox"])
    out: list[dict[str, Any]] = []
    for idx, row in gdf.iterrows():
        pt = _representative_point(row.geometry)
        if pt is None:
            continue
        lon, lat = pt
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            continue
        out.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "osm_id": osm_id_of(idx),
                "osm_type": str(idx[0]) if isinstance(idx, tuple) else None,
                "name": row.get("name"),
                "key": key,
                "value": value,
                "tags_json": json.dumps(
                    _tag_bag(row), separators=(",", ":"), sort_keys=True),
            },
        })
    if not out:
        raise router_empty_error(
            sc, f"No OpenStreetMap features carrying {key}={value!r} were found in "
            f"bbox={tuple(params['bbox'])!r}. The area genuinely has no such tagged "
            f"features in OSM, or the tag is misspelled. Widen the area or try a "
            f"different tag.",
            spec.empty_error_suffix,
        )
    return out


def _coastline_features(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """Coastline ways in the bbox as whole LineStrings. Empty is a legitimate
    answer -- an inland box has no coastline -- and the consumer decides what
    absence means."""
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


def _breakwaters_features(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """Barrier ways touching the bbox as WHOLE LineStrings. Empty is a legitimate
    answer, not an error: an open-water AOI with no structure is exactly the case
    an agitation run has to be able to solve and label."""
    values = _resolve_barrier_values(
        spec.error_code_prefix, spec.input_error_suffix, params.get("structure_type"))
    gdf = overpass_features(spec, params, {"man_made": values}, timeout_s=timeout_s)
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
                "man_made": row.get("man_made"),
            },
        })
    return out


_BY_FEATURE = {
    "roads": _roads_features,
    "pois": _pois_features,
    "coastline": _coastline_features,
    "breakwaters": _breakwaters_features,
}


@register_hook("osm_features.delegate")
def delegate(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """Dispatch to the named class's own tag resolution, clip rule and property
    shape; ``feature`` selects the ONE request this call makes of the API."""
    return _BY_FEATURE[params["feature"]](spec, params, timeout_s=timeout_s)
