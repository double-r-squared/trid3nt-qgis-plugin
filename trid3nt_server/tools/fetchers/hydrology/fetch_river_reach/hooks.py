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


def _cut_reach(spec: SourceSpec, banks: dict[str, Any],
               between: list[list[float]]) -> tuple[Any, dict[str, Any]]:
    """The section derive over the mapped water -> its result and the cut geometry.
    The derive's own refusals are carried by name."""
    import tempfile

    from trid3nt_server.tools.derive.section.section import SectionError, section

    with tempfile.TemporaryDirectory(prefix="trid3nt_reach_") as scratch:
        try:
            cut = section(polygon=banks, between=between, _output_dir=scratch)
        except SectionError as exc:
            raise router_empty_error(
                spec.error_code_prefix,
                f"the mapped water surface at this reach could not be cut into a "
                f"domain ({exc.error_code}): {exc}",
                spec.empty_error_suffix,
            )
        with open(cut.uri, "rb") as handle:
            return cut, json.load(handle)["features"][0]["geometry"]


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
    if not (banks.get("features") or []):
        raise router_empty_error(
            sc,
            "NHD maps no water SURFACE along this reach, so it has no banks to "
            "solve between - the channel here is a centreline only. Model a "
            "mapped river, or supply the domain polygon yourself.",
            spec.empty_error_suffix,
        )

    ends = [[float(center.coords[0][0]), float(center.coords[0][1])],
            [float(center.coords[-1][0]), float(center.coords[-1][1])]]
    cut, polygon = _cut_reach(spec, banks, ends)
    _water_coverage(center, polygon)

    common = {
        "comid": int(params["comid"]),
        "reach_km": round(reach_km, 4),
        "area_km2": round(float(cut.area_km2), 6),
        "mean_width_m": round(float(cut.area_km2) * 1.0e6 / max(reach_km * 1000.0, 1.0), 2),
    }
    rows = [("reach", polygon), ("centerline", mapping(center))]
    for part, face in (("inflow", cut.face_start), ("outflow", cut.face_end)):
        rows.append((part, {"type": "LineString",
                            "coordinates": [[float(p[0]), float(p[1])] for p in face]}))
    logger.info("river_reach: %.3f km reach, %.4f km^2, %d row(s)",
                reach_km, cut.area_km2, len(rows))
    return [{"type": "Feature", "geometry": geometry,
             "properties": {"part": part, **common}} for part, geometry in rows]
