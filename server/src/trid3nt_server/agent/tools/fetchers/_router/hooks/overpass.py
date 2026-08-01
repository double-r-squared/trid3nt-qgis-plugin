"""Overpass-family hooks (ADR 0070): OSM tagged-feature fetch via Overpass QL.

The one irreducible per-source step is a PURE pair: build the Overpass QL (a
params -> query string function) and decode the Overpass JSON ``elements`` into
GeoJSON features the shared ``vector_fgb`` serializer writes. The 3-mirror
fallback is the router's ``ingest.http_source.endpoint_fallback`` chain (first
success wins; a non-429 4xx short-circuits); transport / retry / cache / FGB /
LayerURI stay router-owned.

Two members share this module: ``fetch_roads_osm`` (``highway`` ways -> clipped
LineStrings) and ``fetch_overpass_pois`` (any ``key=value`` node/way/relation ->
Points at the node coord or the way/relation centroid). Both build one POST plan
per mirror carrying the QL in the ``data`` form field, and both project to
strictly-in-bbox GeoJSON in the parse hook.
"""

from __future__ import annotations

import json
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from .. import hooks as _hooks
from ..errors import router_empty_error, router_input_error, router_upstream_error

__all__ = [
    "build_request_roads",
    "parse_response_roads",
    "build_request_pois",
    "parse_response_pois",
]

#: Overpass-side internal-query timeout (the ``[timeout:N]`` QL directive).
_OVERPASS_QL_TIMEOUT = 60


# --------------------------------------------------------------------------- #
# Shared request construction: one POST plan per mirror (the fallback chain).
# --------------------------------------------------------------------------- #


def _mirror_plans(spec: SourceSpec, ql: str) -> list["_hooks.RequestPlan"]:
    """One POST plan per declared endpoint (mirror), QL in the ``data`` form field.

    The router's ``endpoint_fallback`` chain tries them in order, first success
    wins -- the data-source fallback norm (primary -> fallback -> honest typed
    error). Ordered by ``spec.endpoints`` declaration order.
    """
    ua = spec.auth.user_agent
    return [
        _hooks.RequestPlan(
            url=ep.url or ep.url_template or "",
            method="POST",
            data={"data": ql},
            headers={"User-Agent": ua},
        )
        for ep in spec.endpoints.values()
    ]


def _elements(spec: SourceSpec, bodies: list[bytes]) -> list[dict[str, Any]]:
    """Decode the winning Overpass body to its ``elements`` list (typed on failure)."""
    sc = spec.error_code_prefix
    raw = bodies[0] if bodies else b""
    if not raw:
        return []
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"Overpass returned non-JSON: {exc}")
    elements = payload.get("elements") if isinstance(payload, dict) else None
    if elements is None:
        return []
    if not isinstance(elements, list):
        raise router_upstream_error(
            sc, f"Overpass 'elements' is not a list: {type(elements).__name__}"
        )
    return elements


# --------------------------------------------------------------------------- #
# fetch_roads_osm -- highway ways -> bbox-clipped LineStrings.
# --------------------------------------------------------------------------- #

#: Default highway-tag set (major + arterial + link tier) when road_classes=None.
_DEFAULT_ROAD_CLASSES: tuple[str, ...] = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "motorway_link", "trunk_link", "primary_link",
)

#: Full acceptable highway-tag vocabulary (carriageway sense; footway/cycleway/
#: track excluded by design). A value outside this set is a typed input error.
_VALID_ROAD_CLASSES: frozenset[str] = frozenset({
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified",
    "residential", "service", "motorway_link", "trunk_link", "primary_link",
    "secondary_link", "tertiary_link", "living_street", "pedestrian", "road",
})


def _resolve_road_classes(sc: str, sfx: str, road_classes: Any) -> tuple[str, ...]:
    """Validate the highway-tag set (sorted); an unknown value is a typed error.

    The router str_list already sorted + deduped the value into a list, and the
    spec default fills an absent param with the sorted default tier BEFORE this
    hook -- so ``None`` here is only the defensive path. An explicitly-empty list
    is the ambiguous case the twin rejected (require None or >= 1 value); an
    unknown tag value is a typed non-retryable input error.
    """
    if road_classes is None:
        return tuple(sorted(_DEFAULT_ROAD_CLASSES))
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
    return tuple(sorted(set(road_classes)))


def _build_roads_ql(bbox: tuple[float, float, float, float], road_classes: tuple[str, ...]) -> str:
    """Overpass QL for highway ways in ``bbox`` (corners as south,west,north,east).

    Regex alternation pinned with ``^...$`` so a partial match (``motorway`` vs
    ``motorway_junction``) does not leak in. ``out geom`` returns full way node
    geometry inline.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    s, w, n, e = min_lat, min_lon, max_lat, max_lon
    classes_pipe = "|".join(road_classes)
    return (
        f"[out:json][timeout:{_OVERPASS_QL_TIMEOUT}];"
        f"(way[\"highway\"~\"^({classes_pipe})$\"]({s},{w},{n},{e}););"
        f"out geom;"
    )


@_hooks.register_hook("overpass_roads.build_request")
def build_request_roads(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Validate the highway-tag set, build the QL, and plan one POST per mirror."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    classes = _resolve_road_classes(sc, sfx, params.get("road_classes"))
    bbox = tuple(float(v) for v in params["bbox"])
    return _mirror_plans(spec, _build_roads_ql(bbox, classes))


def _way_coords(geom: Any) -> list[tuple[float, float]]:
    """Extract a finite ``(lon, lat)`` coordinate list from an Overpass ``geometry``."""
    coords: list[tuple[float, float]] = []
    if not isinstance(geom, list):
        return coords
    for pt in geom:
        if not isinstance(pt, dict):
            continue
        lat_v, lon_v = pt.get("lat"), pt.get("lon")
        if lat_v is None or lon_v is None:
            continue
        try:
            lat, lon = float(lat_v), float(lon_v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(lat) and math.isfinite(lon):
            coords.append((lon, lat))
    return coords


def _clip_linestring_parts(geom: Any) -> list[list[tuple[float, float]]]:
    """Flatten a shapely clip result into LineString coord lists (>= 2 vertices)."""
    parts: list[list[tuple[float, float]]] = []
    if geom is None or getattr(geom, "is_empty", True):
        return parts
    gt = geom.geom_type
    if gt == "LineString":
        candidates = [geom]
    elif gt in ("MultiLineString", "GeometryCollection"):
        candidates = list(geom.geoms)
    else:
        candidates = []
    for part in candidates:
        if getattr(part, "is_empty", True):
            continue
        if part.geom_type == "LineString":
            cc = [(float(x), float(y)) for x, y in part.coords]
            if len(cc) >= 2:
                parts.append(cc)
        elif part.geom_type in ("MultiLineString", "GeometryCollection"):
            parts.extend(_clip_linestring_parts(part))
    return parts


@_hooks.register_hook("overpass_roads.parse_response")
def parse_response_roads(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Project highway ways to LineStrings, CLIPPED to the exact bbox (no spill).

    Overpass ``out geom`` returns the full way for any way with a node in the
    bbox, so each LineString is clipped to the requested bbox; a way crossing the
    boundary several times yields several in-AOI segments that share the way's
    attributes. Empty -> ``[]`` (the executor writes an honest header-only FGB).
    """
    from shapely import clip_by_rect
    from shapely.geometry import LineString

    bbox = tuple(float(v) for v in params["bbox"])
    min_lon, min_lat, max_lon, max_lat = bbox
    out: list[dict[str, Any]] = []
    for el in _elements(spec, bodies):
        if not isinstance(el, dict) or el.get("type") != "way":
            continue
        coords = _way_coords(el.get("geometry"))
        if len(coords) < 2:
            continue
        tags = el.get("tags") if isinstance(el.get("tags"), dict) else {}
        try:
            clipped = clip_by_rect(LineString(coords), min_lon, min_lat, max_lon, max_lat)
        except Exception:  # noqa: BLE001 -- degenerate geometry drops the way
            continue
        props = {
            "osm_id": el.get("id"),
            "name": tags.get("name"),
            "highway": tags.get("highway"),
            "lanes": tags.get("lanes"),
            "maxspeed": tags.get("maxspeed"),
        }
        for seg in _clip_linestring_parts(clipped):
            out.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": seg},
                "properties": dict(props),
            })
    return out


# --------------------------------------------------------------------------- #
# fetch_overpass_pois -- any key=value node/way/relation -> Points.
# --------------------------------------------------------------------------- #

_ELEMENT_TYPES: tuple[str, ...] = ("node", "way", "relation")

#: Bare-value -> OSM key alias map (amenity="hospital" shortcut vocabulary).
_VALUE_KEY_ALIASES: dict[str, str] = {
    "hospital": "amenity", "clinic": "amenity", "doctors": "amenity",
    "pharmacy": "amenity", "school": "amenity", "college": "amenity",
    "university": "amenity", "kindergarten": "amenity", "fire_station": "amenity",
    "police": "amenity", "townhall": "amenity", "place_of_worship": "amenity",
    "shelter": "amenity", "community_centre": "amenity", "fuel": "amenity",
    "bank": "amenity", "restaurant": "amenity", "supermarket": "shop",
    "convenience": "shop",
}


def _is_clean_token(s: str) -> bool:
    """True iff ``s`` is a plausible OSM key/value token (no QL metachars)."""
    if not s:
        return False
    for ch in s:
        if ch.isspace() or ch in '"\\[](){};':
            return False
    return True


def _resolve_tag(sc: str, sfx: str, params: dict[str, Any]) -> tuple[str, str]:
    """Resolve the caller's tag inputs to one ``(key, value)`` pair (twin priority).

    Priority: ``amenity`` (value-only), then ``tag`` (key=value or a bare aliased
    value), then ``category``, then ``value``. A missing selector or an
    unmappable bare value is a typed non-retryable input error.
    """
    tag = params.get("tag")
    amenity = params.get("amenity")
    category = params.get("category")
    value = params.get("value")

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


def _build_pois_ql(bbox: tuple[float, float, float, float], key: str, value: str) -> str:
    """Overpass QL for node/way/relation carrying ``key=value`` (``out center``)."""
    min_lon, min_lat, max_lon, max_lat = bbox
    s, w, n, e = min_lat, min_lon, max_lat, max_lon
    parts = "".join(
        f'{et}["{key}"="{value}"]({s},{w},{n},{e});' for et in _ELEMENT_TYPES
    )
    return f"[out:json][timeout:{_OVERPASS_QL_TIMEOUT}];({parts});out center;"


@_hooks.register_hook("overpass_pois.build_request")
def build_request_pois(spec: SourceSpec, params: dict[str, Any]) -> list["_hooks.RequestPlan"]:
    """Resolve the tag to (key, value), build the QL, and plan one POST per mirror."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    key, value = _resolve_tag(sc, sfx, params)
    bbox = tuple(float(v) for v in params["bbox"])
    return _mirror_plans(spec, _build_pois_ql(bbox, key, value))


@_hooks.register_hook("overpass_pois.parse_response")
def parse_response_pois(
    spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]
) -> list[dict[str, Any]]:
    """Project elements to Points strictly inside the bbox; zero -> NO_FEATURES.

    A ``node`` uses its own coord; a ``way`` / ``relation`` uses the ``out center``
    centroid. A centroid outside the requested bbox is dropped. Zero features is a
    typed non-retryable ``*_NO_FEATURES`` (never a fabricated empty-success layer).
    """
    sc = spec.error_code_prefix
    key, value = _resolve_tag(sc, spec.input_error_suffix, params)
    bbox = tuple(float(v) for v in params["bbox"])
    min_lon, min_lat, max_lon, max_lat = bbox
    out: list[dict[str, Any]] = []
    for el in _elements(spec, bodies):
        if not isinstance(el, dict):
            continue
        etype = el.get("type")
        if etype == "node":
            lat_v, lon_v = el.get("lat"), el.get("lon")
        else:
            center = el.get("center") or {}
            lat_v, lon_v = center.get("lat"), center.get("lon")
        if lat_v is None or lon_v is None:
            continue
        try:
            lat, lon = float(lat_v), float(lon_v)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(lat) and math.isfinite(lon)):
            continue
        if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
            continue
        tags = el.get("tags") if isinstance(el.get("tags"), dict) else {}
        out.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "osm_id": el.get("id"),
                "osm_type": etype,
                "name": tags.get("name"),
                "key": key,
                "value": value,
                "tags_json": json.dumps(tags, separators=(",", ":"), sort_keys=True),
            },
        })
    if not out:
        raise router_empty_error(
            sc, f"No OpenStreetMap features carrying {key}={value!r} were found in "
            f"bbox={bbox!r}. The area genuinely has no such tagged features in OSM, "
            f"or the tag is misspelled. Widen the area or try a different tag.",
            spec.empty_error_suffix,
        )
    return out
