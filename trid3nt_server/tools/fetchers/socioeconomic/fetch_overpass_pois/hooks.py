"""overpass_pois delegate: any ``key=value`` OSM element as a Point.

The tool's surface is five ways of naming ONE tag - ``tag='key=value'``,
``amenity``, ``category``, ``value`` - because that is how a caller asks for a
point of interest; the resolution between them is this row's vocabulary.

A node is its own coordinate. A way or a relation is reported at the CENTRE OF ITS
BOUNDING BOX, not its centroid: it is the representative point Overpass itself
publishes for an element, it is stable under a re-mapped interior, and a courtyard
building would otherwise be pinned outside its own walls.
"""

from __future__ import annotations

import json
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error
from ..._router.hooks import register_hook
from ..._router.hooks.osm import osm_id_of, overpass_features

__all__ = ["delegate", "validate"]

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

#: Columns the library adds that are not OSM tags, so the tag bag stays the tags.
_NON_TAG_COLUMNS: frozenset[str] = frozenset({"geometry", "nodes", "ways", "element"})


def _is_clean_token(s: str) -> bool:
    """True iff ``s`` is a plausible OSM key/value token (no metacharacters)."""
    if not s:
        return False
    return not any(ch.isspace() or ch in '"\\[](){};' for ch in s)


def _resolve_tag(sc: str, sfx: str, params: dict[str, Any]) -> tuple[str, str]:
    """Resolve the caller's tag inputs to one ``(key, value)`` pair.

    Priority: ``amenity`` (value-only), then ``tag`` (key=value or a bare aliased
    value), then ``category``, then ``value``. A missing selector or an unmappable
    bare value is a typed non-retryable input error.
    """
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


@register_hook("overpass_pois.validate")
def validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Resolve the tag before the cache key is taken, so a bad ask fails early."""
    _resolve_tag(spec.error_code_prefix, spec.input_error_suffix, params)


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


@register_hook("overpass_pois.delegate")
def delegate(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """Elements carrying the resolved tag, as Points strictly inside the bbox.

    Zero features is a typed non-retryable ``*_NO_FEATURES``, never a fabricated
    empty-success layer: an ask for hospitals that finds none is an answer the
    caller has to see as one.
    """
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
