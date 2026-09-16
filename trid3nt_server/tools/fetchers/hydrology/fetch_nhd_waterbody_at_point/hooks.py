"""nhd_waterbody_at_point hooks: one seed in, the ONE body it names out.

The envelope is the seed grown by the distance the caller allows; the pick is made
here because the service answers with every polygon that intersects the box and only
the seed says which of them was meant.
"""

# The two mirrors are the same dataset published twice and name their columns in
# different case, so every property is read case-insensitively off whichever answered.
#
# Distance is measured in the seed's own UTM zone rather than in degrees: a degree of
# longitude is 80 km at the Gulf and 60 km at the Canadian border, and the refusal
# distance is stated in kilometres.

from __future__ import annotations

import json
import logging
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import RequestPlan, register_hook

logger = logging.getLogger(__name__)

__all__ = ["build_request", "parse_response"]

#: Degrees of latitude per kilometre, for the envelope around the seed. The envelope
#: only has to CONTAIN the search: a body intersecting it comes back whole.
_DEG_PER_KM = 1.0 / 111.0

#: How far past the allowed distance the query looks. A refusal that cannot say what
#: WAS nearby is a refusal the caller cannot act on, so the box is wider than the
#: allowance and the allowance is applied to the answer instead of to the question.
_LOOK_FACTOR = 3.0

#: The smallest box asked for, in kilometres. A zero search distance still needs a box
#: with area, because the service takes an envelope and not a point.
_MIN_LOOK_KM = 1.0

#: The largest body this hands back as a DOMAIN, in square kilometres. A mesh over
#: one polygon is a node budget: at 100 m - already coarse for a lake question - an
#: equilateral triangulation spends about 115 nodes per square kilometre, so this
#: ceiling is already a quarter of a million nodes before the bed is painted. A
#: GREAT LAKE IS NOT A CANVAS DOMAIN - the smallest of them is nine times this, and
#: a seed anywhere along one of their shores lands in it.
_MAX_AREA_KM2 = 2000.0


def _seed(spec: SourceSpec, params: dict[str, Any]) -> tuple[float, float]:
    """The validated ``(lon, lat)`` the waterbody is named by."""
    raw = params.get("seed_point")
    try:
        lon, lat = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError):
        raise router_input_error(
            spec.error_code_prefix,
            f"seed_point must be (lon, lat); got {raw!r}",
            spec.input_error_suffix,
        )
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise router_input_error(
            spec.error_code_prefix,
            f"seed_point ({lon}, {lat}) is not a lon/lat pair on this planet.",
            spec.input_error_suffix,
        )
    return lon, lat


@register_hook("nhd_waterbody_at_point.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """The same envelope query against the two mirrors, high resolution first."""
    import math

    lon, lat = _seed(spec, params)
    look_km = max(_MIN_LOOK_KM,
                  _LOOK_FACTOR * float(params.get("search_distance_km") or 0.0))
    pad = look_km * _DEG_PER_KM
    pad_lon = pad / max(0.2, math.cos(math.radians(lat)))
    envelope = f"{lon - pad_lon},{lat - pad},{lon + pad_lon},{lat + pad}"
    plans = []
    for key in ("data", *spec.endpoint_fallback):
        endpoint = spec.endpoints[key]
        plans.append(RequestPlan(url=str(endpoint.url),
                                 params={**endpoint.query, "geometry": envelope}))
    return plans


def _property(properties: dict[str, Any], name: str) -> Any:
    """One NHD column, whichever case the mirror that answered spells it in."""
    lowered = {str(k).lower(): v for k, v in (properties or {}).items()}
    return lowered.get(name)


def _picked(shapes: list[Any], point: Any) -> tuple[int, float]:
    """``(index, distance in metres)`` of the body the seed names.

    A seed INSIDE a body takes that body at zero distance, and the smallest of
    several nested outlines, which is the one a person pointing there means."""
    inside = [i for i, geometry in enumerate(shapes) if geometry.contains(point)]
    if inside:
        return min(inside, key=lambda i: shapes[i].area), 0.0
    index = min(range(len(shapes)), key=lambda i: shapes[i].distance(point))
    return index, float(shapes[index].distance(point))


def _area_km2(geometry: Any) -> float:
    """The body's surface area, measured on the ellipsoid.

    A lake wide enough to argue about spans several UTM zones, where a projected
    area is a different number at each end of it."""
    from pyproj import Geod
    from shapely.geometry import shape

    area, _perimeter = Geod(ellps="WGS84").geometry_area_perimeter(shape(geometry))
    return abs(float(area)) / 1.0e6


@register_hook("nhd_waterbody_at_point.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any],
                   bodies: list[bytes]) -> list[dict]:
    """The one body the seed names, or a refusal saying which was nearest."""
    from pyproj import Transformer
    from shapely.geometry import Point, shape
    from shapely.ops import transform as _transform

    from trid3nt_server.inputs.geometry import utm_epsg_for

    sc = spec.error_code_prefix
    lon, lat = _seed(spec, params)
    allowed_km = float(params.get("search_distance_km") or 0.0)
    try:
        parsed = json.loads((bodies[0] if bodies else b"").decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, IndexError) as exc:
        raise router_upstream_error(sc, f"the NHD waterbody service returned no "
                                        f"usable response: {exc}")
    features = [f for f in (parsed.get("features") or []) if (f or {}).get("geometry")]
    if not features:
        look_km = max(_MIN_LOOK_KM, _LOOK_FACTOR * allowed_km)
        raise router_empty_error(
            sc,
            f"NHD maps no waterbody at all within {look_km:g} km of ({lon}, {lat}). "
            "Seed a mapped lake, pond or reservoir, or supply the outline yourself.",
            spec.empty_error_suffix,
        )

    forward = Transformer.from_crs(4326, utm_epsg_for(lon, lat), always_xy=True)
    shapes = [_transform(forward.transform, shape(f["geometry"])) for f in features]
    index, metres = _picked(shapes, _transform(forward.transform, Point(lon, lat)))
    picked = features[index]
    name = _property(picked.get("properties"), "gnis_name")
    if metres > allowed_km * 1000.0:
        raise router_empty_error(
            sc,
            f"the nearest waterbody to ({lon}, {lat}) is "
            f"{name or 'an unnamed body'} at {metres / 1000.0:.2f} km, beyond the "
            f"{allowed_km:g} km you allowed. Move the seed onto the water, or "
            "raise search_distance_km if that really is the body you mean.",
            spec.empty_error_suffix,
        )

    area_km2 = _area_km2(picked["geometry"])
    if area_km2 > _MAX_AREA_KM2:
        raise router_input_error(
            sc,
            f"the seed at ({lon}, {lat}) lands in {name or 'an unnamed waterbody'}, "
            f"{area_km2:,.0f} km2 of open water, past the {_MAX_AREA_KM2:,.0f} km2 "
            "this returns as a model domain - a mesh over it spends a quarter of a "
            "million nodes at a 100 m edge before the bed is painted, and a Great "
            "Lake is not a canvas domain. Draw the bay, the harbour or the shore "
            "section the question is about and hand that outline over as the "
            "domain.",
            spec.input_error_suffix,
        )

    properties = picked.get("properties") or {}
    logger.info("nhd_waterbody_at_point: %s, %.0f km2, %.3f km from the seed",
                name or "unnamed", area_km2, metres / 1000.0)
    return [{"type": "Feature", "geometry": picked["geometry"], "properties": {
        "part": "waterbody",
        "gnis_name": name,
        "gnis_id": _property(properties, "gnis_id"),
        "permanent_identifier": _property(properties, "permanent_identifier"),
        "ftype": _property(properties, "ftype"),
        "fcode": _property(properties, "fcode"),
        "reachcode": _property(properties, "reachcode"),
        "areasqkm": _property(properties, "areasqkm"),
        "seed_distance_km": round(metres / 1000.0, 4),
    }}]
