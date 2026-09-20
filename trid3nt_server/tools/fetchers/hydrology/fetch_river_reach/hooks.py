"""river_reach hooks: two services and two derives composed into one reach artifact.

The seed resolves to a COMID pre-cache-key; the navigate and the bank query are
built together; the parse clips the centerline to the asked distance, cuts the
mapped water surface square to it, and emits the four rows the domain slot reads.
"""

# The reach runs FROM the seed. NLDI returns WHOLE flowlines rather than a clipped
# walk - the seed's own flowline included, with the seed somewhere in its middle -
# so the distance is measured here, from the seed's projection onto the merged line:
# forward along it going downstream, back along it going upstream. The line is put
# in FLOW order first, read off the flowlines' own vertex order (NHD digitizes
# downstream), so inflow and outflow are named for what the water does at each end
# and not for which way the walk went.

from __future__ import annotations

import json
import logging
import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import RequestPlan, register_hook

logger = logging.getLogger(__name__)

__all__ = ["resolve_build", "resolve_parse", "build_request", "parse_response"]

#: NHDPlus v2.1 / NHDPlus HR footprint. A seed outside it has no network to walk.
_CONUS = (-130.0, 20.0, -60.0, 55.0)

#: Degrees of latitude per kilometre, for the bank-query envelope around the seed.
#: The envelope only has to CONTAIN the walk; NHDArea returns an intersecting
#: polygon whole, so a generous box costs one query and no accuracy.
_DEG_PER_KM = 1.0 / 111.0
_ENVELOPE_PAD_KM = 1.0


def _seed(spec: SourceSpec, params: dict[str, Any]) -> tuple[float, float]:
    """The validated ``(lon, lat)`` seed, refused when it is off the network's ground."""
    raw = params.get("seed_point")
    try:
        lon, lat = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError):
        raise router_input_error(
            spec.error_code_prefix,
            f"seed_point must be (lon, lat); got {raw!r}",
            spec.input_error_suffix,
        )
    if not (_CONUS[0] <= lon <= _CONUS[2] and _CONUS[1] <= lat <= _CONUS[3]):
        raise router_input_error(
            spec.error_code_prefix,
            f"seed_point ({lon}, {lat}) is outside the NHDPlus footprint "
            f"{_CONUS}; this source maps CONUS rivers only.",
            spec.input_error_suffix,
        )
    return lon, lat


@register_hook("river_reach.resolve_build")
def resolve_build(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """Snap the seed to its NHDPlus flowline: the one round trip before the cache key."""
    lon, lat = _seed(spec, params)
    endpoint = spec.endpoints["position"]
    return [RequestPlan(
        url=str(endpoint.url),
        params={**endpoint.query, "coords": f"POINT({lon} {lat})"},
    )]


@register_hook("river_reach.resolve_parse")
def resolve_parse(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> dict[str, Any]:
    """The snapped COMID, merged into params so the navigate has a reach to walk from."""
    sc = spec.error_code_prefix
    try:
        parsed = json.loads((bodies[0] if bodies else b"").decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, IndexError) as exc:
        raise router_upstream_error(sc, f"NLDI returned no usable snap response: {exc}")
    features = (parsed or {}).get("features") or []
    identifier = None
    if features:
        identifier = ((features[0] or {}).get("properties") or {}).get("identifier")
    if identifier is None:
        raise router_input_error(
            sc,
            f"seed_point {tuple(params.get('seed_point') or ())} snaps to no NHDPlus "
            "flowline. Move the seed onto the channel - a point far from any mapped "
            "stream has no reach to build.",
            spec.input_error_suffix,
        )
    return {"comid": int(identifier)}


@register_hook("river_reach.build_request")
def build_request(spec: SourceSpec, params: dict[str, Any]) -> list[RequestPlan]:
    """The network walk and the bank query, in that order: the parse joins them."""
    lon, lat = _seed(spec, params)
    distance_km = float(params.get("distance_km") or 3.0)
    navigate = spec.endpoints["navigate"]
    banks = spec.endpoints["banks"]
    walk = str(navigate.url_template).format(
        comid=int(params["comid"]), direction=str(params.get("direction") or "DM"))
    pad = (distance_km + _ENVELOPE_PAD_KM) * _DEG_PER_KM
    pad_lon = pad / max(0.2, math.cos(math.radians(lat)))
    envelope = f"{lon - pad_lon},{lat - pad},{lon + pad_lon},{lat + pad}"
    return [
        RequestPlan(url=walk, params={**navigate.query, "distance": f"{distance_km:g}"}),
        RequestPlan(url=str(banks.url), params={**banks.query, "geometry": envelope}),
    ]


def _merged_centerline(spec: SourceSpec, body: bytes, seed: Any) -> Any:
    """The navigated flowlines joined into ONE line running the way the water does."""
    from shapely.geometry import LineString, Point, shape
    from shapely.ops import linemerge

    sc = spec.error_code_prefix
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"NLDI navigate returned non-JSON: {exc}")
    parts = [shape(f["geometry"]) for f in (parsed.get("features") or [])
             if (f or {}).get("geometry")]
    if not parts:
        raise router_empty_error(
            sc,
            "the seed's reach has no flowline downstream or upstream of it - a "
            "network terminus or a stub reach. Seed a mapped channel.",
            spec.empty_error_suffix,
        )
    merged = parts[0] if len(parts) == 1 else linemerge(parts)
    pieces = list(getattr(merged, "geoms", [merged]))
    if len(pieces) > 1:
        # A braided or diverging walk leaves several lines; the reach is the one
        # the seed stands on, and the others are stated as left out.
        pieces.sort(key=lambda g: seed.distance(g))
        logger.info("river_reach: walk left %d separate lines; kept the seeded one",
                    len(pieces))
    line = pieces[0]
    longest = max(parts, key=lambda part: part.length)
    if line.project(Point(longest.coords[0])) > line.project(Point(longest.coords[-1])):
        line = LineString(list(line.coords)[::-1])
    return line


def _clipped(line: Any, seed: Any, distance_km: float,
             direction: str) -> tuple[Any, float, int]:
    """``distance_km`` of the line either side of the seed -> geometry, km, UTM zone.
    The cut keeps the line's flow order, so its first vertex is always the upstream
    end whichever way the walk ran."""
    from pyproj import Transformer
    from shapely.ops import substring, transform as _transform

    from trid3nt_server.inputs.geometry import utm_epsg_for

    epsg = utm_epsg_for(float(line.centroid.x), float(line.centroid.y))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    metric = _transform(forward.transform, line)
    at_seed = metric.project(_transform(forward.transform, seed))
    span = distance_km * 1000.0
    if direction == "UM":
        cut = substring(metric, max(0.0, at_seed - span), at_seed)
    else:
        cut = substring(metric, at_seed, min(at_seed + span, metric.length))
    return _transform(back.transform, cut), float(cut.length) / 1000.0, int(epsg)


#: Below this the two ends of the walk are one point and the chord has no
#: direction for the end cuts to be square to.
_MIN_CHORD_M = 1.0

#: How far off the cut plane a vertex may stand and still BE on it, in metres.
#: The cut puts its vertices there exactly; what separates them from the reach's
#: own bank vertices is the clip's double-precision residue (nanometres on a UTM
#: coordinate) against metres of real geometry.
_ON_CUT_M = 1.0e-6


def _bank_polygons(spec: SourceSpec, banks: dict[str, Any]) -> list[Any]:
    """The polygons NHDArea mapped, in EPSG:4326."""
    from shapely.geometry import shape as _shape

    from trid3nt_server.inputs.geometry import (
        GeometryReadError, flatten_geometries, read_geometry_doc)

    try:
        geoms = [_shape(g) for g in flatten_geometries(read_geometry_doc(banks))]
    except (GeometryReadError, Exception) as exc:  # noqa: BLE001 - reader fault
        raise router_upstream_error(
            spec.error_code_prefix,
            f"the mapped water surface at this reach could not be read: {exc}")
    out: list[Any] = []
    for geom in geoms:
        if geom is None or geom.is_empty:
            continue
        for part in getattr(geom, "geoms", [geom]):
            if part.geom_type == "Polygon":
                out.append(part if part.is_valid else part.buffer(0))
    return [p for p in out if not p.is_empty]


def _band(a: Any, b: Any, reach: float) -> Any:
    """The strip between the two perpendicular cuts at ``a`` and ``b``, wide enough
    that only those two cuts ever touch the polygon."""
    import numpy as np
    from shapely.geometry import Polygon

    span = np.asarray(b, dtype=float) - np.asarray(a, dtype=float)
    unit = span / float(np.hypot(*span))
    wide = np.array([-unit[1], unit[0]]) * reach
    return Polygon([tuple(a + wide), tuple(b + wide),
                    tuple(b - wide), tuple(a - wide)])


def _end_face(spec: SourceSpec, cut_m: Any, point: Any, unit: Any, normal: Any,
              label: str, back: Any) -> list[list[float]]:
    """The TRANSECT the cut left at one end, as its two lon/lat ends; a boundary
    role is prescribed across this whole face, not across the naming point."""
    # The cut put VERTICES on the plane through the point, and the two furthest
    # apart across the reach are the face's ends. They are found by projecting
    # the reach's OWN boundary vertices, never by intersecting a probe line: the
    # cut edge is exactly collinear with such a line, and a collinear
    # intersection over a domain-sized probe returns an edge at one end and
    # nothing at the other, for no reason a reader can see.
    import numpy as np

    origin = np.asarray(point, dtype=float)
    rings = [np.asarray(ring.coords, dtype=float)
             for part in getattr(cut_m, "geoms", [cut_m])
             for ring in (part.exterior, *part.interiors)]
    vertices = np.vstack(rings) - origin
    on_plane = np.abs(vertices @ np.asarray(unit, dtype=float)) <= _ON_CUT_M
    across = vertices[on_plane] @ np.asarray(normal, dtype=float)
    if across.size < 2 or float(across.max() - across.min()) <= 0.0:
        raise router_empty_error(
            spec.error_code_prefix,
            f"the {label} end cut left no transect on the mapped water: the reach "
            "reaches that end along its own bank rather than along the cut, so "
            "there is no transect there to prescribe a boundary across. Seed a "
            "stretch the water surface maps end to end.",
            spec.empty_error_suffix,
        )
    ends = vertices[on_plane][[int(across.argmin()), int(across.argmax())]] + origin
    return [[float(v) for v in back.transform(*e)] for e in ends]


def _area_km2(geom: Any) -> float:
    """Area in square kilometres, measured in the geometry's own UTM zone."""
    from pyproj import Transformer
    from shapely.ops import transform as _transform

    from trid3nt_server.inputs.geometry import utm_epsg_for

    epsg = utm_epsg_for(float(geom.centroid.x), float(geom.centroid.y))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    return float(_transform(forward.transform, geom).area) / 1.0e6


def _cut_reach(spec: SourceSpec, banks: dict[str, Any],
               between: list[list[float]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The mapped water surface cut square to the walk -> the reach geometry and
    its area and two end transects.

    Perpendicular is measured in the local UTM zone, because perpendicular in
    degrees is not perpendicular on the ground."""
    import numpy as np
    from pyproj import Transformer
    from shapely.geometry import LineString, mapping
    from shapely.ops import transform as _transform, unary_union

    from trid3nt_server.inputs.geometry import utm_epsg_for

    polys = _bank_polygons(spec, banks)
    if not polys:
        raise router_empty_error(
            spec.error_code_prefix,
            "NHD maps no water SURFACE along this reach, so it has no banks to "
            "solve between - the channel here is a centreline only. Model a "
            "mapped river, or supply the domain polygon yourself.",
            spec.empty_error_suffix,
        )

    union_ll = unary_union(polys)
    source_area = _area_km2(union_ll)
    epsg = utm_epsg_for(float(union_ll.centroid.x), float(union_ll.centroid.y))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    union_m = _transform(forward.transform, union_ll)

    a = np.asarray(forward.transform(*between[0]), dtype=float)
    b = np.asarray(forward.transform(*between[1]), dtype=float)
    chord = float(np.hypot(*(b - a)))
    if chord < _MIN_CHORD_M:
        raise router_empty_error(
            spec.error_code_prefix,
            f"the walked reach is {chord:.3f} m long, which is one point: there "
            "is no direction for the end cuts to be square to. Ask for a longer "
            "distance, or seed a channel with flowline either side of it.",
            spec.empty_error_suffix,
        )

    minx, miny, maxx, maxy = union_m.bounds
    reach = float(np.hypot(maxx - minx, maxy - miny)) + chord
    cut = union_m.intersection(_band(a, b, reach))
    parts = [p for p in getattr(cut, "geoms", [cut]) if p.geom_type == "Polygon"]
    axis = LineString([tuple(a), tuple(b)])
    kept = [p for p in parts if p.intersects(axis)]
    if not kept:
        raise router_empty_error(
            spec.error_code_prefix,
            "the walked centerline touches no part of the mapped water surface "
            "between its two ends, so which piece is the reach is not measurable. "
            "Seed a channel whose banks NHD maps.",
            spec.empty_error_suffix,
        )
    if len(parts) > len(kept):
        logger.info("river_reach: %d piece(s) between the end cuts do not touch "
                    "the centerline and were left out",
                    len(parts) - len(kept))

    cut_m = unary_union(kept)
    unit = (b - a) / chord
    normal = np.array([-unit[1], unit[0]])
    geom = _transform(back.transform, cut_m)
    props = {
        "area_km2": _area_km2(geom),
        "source_area_km2": source_area,
        "utm_epsg": int(epsg),
        "face_start": _end_face(spec, cut_m, a, unit, normal, "inflow", back),
        "face_end": _end_face(spec, cut_m, b, unit, normal, "outflow", back),
    }
    return mapping(geom), props


def _water_coverage(center: Any, polygon: dict) -> None:
    """Say how much of the walked centerline the mapped water actually covers.

    A partly mapped reach proceeds: what is outside the banks carries nothing
    this source measured, and the run is about the covered stretch."""
    from shapely.geometry import mapping

    from trid3nt_server.inputs.geometry import covered_fraction
    from trid3nt_server.workflows.runtime import journal_note

    try:
        measured = covered_fraction(mapping(center), polygon)
    except Exception as exc:  # noqa: BLE001 - an unmeasurable overlap is not a fetch fault
        logger.info("river_reach: water coverage not measured (%s)", exc)
        return
    journal_note(measured["note"])


@register_hook("river_reach.parse_response")
def parse_response(spec: SourceSpec, params: dict[str, Any], bodies: list[bytes]) -> list[dict]:
    """The reach polygon, its two boundary runs and the centerline, as four rows."""
    from shapely.geometry import Point, mapping

    sc = spec.error_code_prefix
    seed = Point(*_seed(spec, params))
    line = _merged_centerline(spec, bodies[0], seed)
    center, reach_km, _epsg = _clipped(
        line, seed, float(params.get("distance_km") or 3.0),
        str(params.get("direction") or "DM"))
    try:
        banks = json.loads(bodies[1].decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise router_upstream_error(sc, f"NHDArea returned non-JSON: {exc}")
    ends = [[float(center.coords[0][0]), float(center.coords[0][1])],
            [float(center.coords[-1][0]), float(center.coords[-1][1])]]
    polygon, cut = _cut_reach(spec, banks, ends)
    _water_coverage(center, polygon)

    common = {
        "comid": int(params["comid"]),
        "reach_km": round(reach_km, 4),
        "area_km2": round(float(cut["area_km2"]), 6),
        "mean_width_m": round(
            float(cut["area_km2"]) * 1.0e6 / max(reach_km * 1000.0, 1.0), 2),
    }
    rows = [("reach", polygon), ("centerline", mapping(center))]
    for part, face in (("inflow", cut["face_start"]), ("outflow", cut["face_end"])):
        rows.append((part, {"type": "LineString",
                            "coordinates": [[float(p[0]), float(p[1])] for p in face]}))
    logger.info("river_reach: %.3f km reach, %.4f km^2, %d row(s)",
                reach_km, cut["area_km2"], len(rows))
    return [{"type": "Feature", "geometry": geometry,
             "properties": {"part": part, **common}} for part, geometry in rows]
