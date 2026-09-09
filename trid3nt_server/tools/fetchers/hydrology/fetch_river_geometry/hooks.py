"""river_geometry delegate: ``waterway`` ways CLIPPED to the exact bbox.

Fills the WHOLE bbox - a true per-bbox query, not a seed-connected sub-network -
and clips for the same reason the roads member does: a channel network is measured
inside an area, so a way crossing the boundary contributes only its in-AOI runs.

``ditch`` and ``drain`` are excluded from the default set: they explode feature
counts in drained-agriculture and urban areas.
"""

from __future__ import annotations

import re
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_input_error
from ..._router.hooks import register_hook
from ..._router.hooks.osm import clip_to_bbox, osm_id_of, overpass_features

__all__ = ["delegate", "validate"]

#: Default waterway-tag set: the channel-carrying network.
_WATERWAY_CLASSES: tuple[str, ...] = ("river", "stream", "canal")

#: Convenience aliases -> a class set.
_WATERWAY_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "default": ("river", "stream", "canal"),
    "rivers": ("river", "stream", "canal"),
    "channels": ("river", "stream", "canal"),
    "drainage": ("ditch", "drain"),
    "ditches": ("ditch", "drain"),
    "all": ("river", "stream", "canal", "ditch", "drain"),
}

#: Closed vocabulary of individual OSM ``waterway`` values a caller may name.
_WATERWAY_ALLOWED_VALUES: tuple[str, ...] = ("river", "stream", "canal", "ditch", "drain")

#: Accepted ``source`` labels, kept for signature back-compat: both resolve to the
#: OSM path, which is the only one this row has.
_RIVER_SOURCES: frozenset[str] = frozenset({"nhdplus_hr", "osm"})


def _resolve_waterway_classes(sc: str, sfx: str, waterway_type: Any) -> list[str]:
    """Resolve a caller ``waterway_type`` to a validated list of OSM classes.

    Accepts None (-> default), a convenience alias, a single value, a
    comma/plus/space-joined string, or a list of values. De-dupes preserving order;
    an unknown token is a typed non-retryable input error. Empty -> default.
    """
    if waterway_type is None:
        return list(_WATERWAY_CLASSES)
    raw_tokens: list[str] = []
    if isinstance(waterway_type, str):
        text = waterway_type.strip().lower()
        if not text:
            return list(_WATERWAY_CLASSES)
        if text in _WATERWAY_TYPE_ALIASES:
            return list(_WATERWAY_TYPE_ALIASES[text])
        raw_tokens = [c for c in re.split(r"[,+\s]+", text) if c]
    elif isinstance(waterway_type, (list, tuple)):
        for item in waterway_type:
            if not isinstance(item, str):
                raise router_input_error(
                    sc, f"waterway_type list entries must be strings; got "
                    f"{type(item).__name__}", sfx,
                )
            tok = item.strip().lower()
            if tok:
                raw_tokens.append(tok)
    else:
        raise router_input_error(
            sc, f"waterway_type must be a str or list of str; got "
            f"{type(waterway_type).__name__}", sfx,
        )
    resolved: list[str] = []
    for tok in raw_tokens:
        if tok not in _WATERWAY_ALLOWED_VALUES:
            raise router_input_error(
                sc, f"unsupported waterway_type token {tok!r}; allowed OSM waterway "
                f"values: {', '.join(_WATERWAY_ALLOWED_VALUES)} (or an alias: "
                f"{', '.join(sorted(_WATERWAY_TYPE_ALIASES))}).", sfx,
            )
        if tok not in resolved:
            resolved.append(tok)
    return resolved or list(_WATERWAY_CLASSES)


@register_hook("river_geometry.validate")
def validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Reject an unknown source label or waterway value before the cache key."""
    sc, sfx = spec.error_code_prefix, spec.input_error_suffix
    source = params.get("source")
    if source is not None and str(source) not in _RIVER_SOURCES:
        raise router_input_error(
            sc, f"unsupported source={source!r}; allowed: 'nhdplus_hr' or 'osm' "
            "(both resolve to the OSM waterway path, which is this row's only "
            "source).", sfx,
        )
    _resolve_waterway_classes(sc, sfx, params.get("waterway_type"))


@register_hook("river_geometry.delegate")
def delegate(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """Waterway ways in the bbox as strictly-in-AOI LineStrings.

    Empty is a legitimate answer - no rivers in the bbox - not an error.
    """
    classes = _resolve_waterway_classes(
        spec.error_code_prefix, spec.input_error_suffix, params.get("waterway_type"))
    gdf = overpass_features(spec, params, {"waterway": classes}, timeout_s=timeout_s)
    bbox = tuple(float(v) for v in params["bbox"])
    out: list[dict[str, Any]] = []
    for idx, row in gdf.iterrows():
        props = {
            "osm_id": osm_id_of(idx),
            "name": row.get("name"),
            "waterway": row.get("waterway"),
        }
        for seg in clip_to_bbox(row.geometry, bbox):
            out.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": seg},
                "properties": dict(props),
            })
    return out
