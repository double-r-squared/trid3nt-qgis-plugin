"""osm_breakwaters delegate: ``man_made`` wave barriers as whole structures.

Deliberately NOT bbox-clipped. A breakwater is meshed as a SOLID BARRIER: clipping
one at the AOI edge opens a gap in the middle of a structure that has none, and
waves would pour through a hole the survey does not contain. A road is a network
you measure inside an area; this is an object you either have or do not.

``man_made=pier`` is excluded on purpose: a pier is the berthing dock being
sheltered, not a wave barrier, and meshing one solid answers a different question.
``groyne`` and ``breakwater`` are both barriers and both ride.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_input_error
from ..._router.hooks import register_hook
from ..._router.hooks.osm import osm_id_of, overpass_features

__all__ = ["delegate", "validate"]

#: The OSM ``man_made`` values that are wave BARRIERS, and the closed vocabulary a
#: caller may name: an invented value is a wrong answer, not a wider search.
_BARRIER_VALUES: tuple[str, ...] = ("breakwater", "groyne")


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


@register_hook("osm_breakwaters.validate")
def validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Reject an unknown barrier value before the cache key is taken."""
    _resolve_barrier_values(
        spec.error_code_prefix, spec.input_error_suffix, params.get("structure_type"))


def _line_coords(geom: Any) -> list[list[float]] | None:
    """The structure's vertices; a barrier that closes on itself keeps its ring."""
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "LineString":
        coords = list(geom.coords)
    elif geom.geom_type == "Polygon":
        coords = list(geom.exterior.coords)
    else:
        return None
    return [[float(x), float(y)] for x, y in coords] if len(coords) >= 2 else None


@register_hook("osm_breakwaters.delegate")
def delegate(
    spec: SourceSpec, params: dict[str, Any], *, timeout_s: float
) -> list[dict[str, Any]]:
    """Barrier ways touching the bbox as WHOLE LineStrings.

    Empty is a legitimate answer, not an error: an open-water AOI with no structure
    is exactly the case a harbour-agitation run has to be able to solve and label.
    """
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
