"""A POINT: one location with an optional name, from every way a user names one.

A canvas pick, a (lon, lat) pair, a "lat,lon" string, a selected point layer and a
geocoded place all enter through ``point``. What reads a Point after that reads
``.lon``, ``.lat`` and ``.name`` and never parses again; where a point is allowed
to be - inside a domain, on a river, in water - is answered here for any slot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from trid3nt_server.workflows.runtime.user_input import UserInputError, lonlat_point

from .geometry import flatten_geometries, read_geometry_doc, utm_epsg_for

__all__ = ["Point", "PointOutsideDomainError", "as_utm", "contain", "point",
           "point_arg", "publish_point", "snap_to_wet"]

logger = logging.getLogger("trid3nt_server.workflows.inputs.point")

_CODE = "POINT_INVALID"

#: Half-width of a published point's declared bbox, in degrees. A zero-extent
#: bbox reads as "no extent" to the camera, so the point gets a small honest box.
_BBOX_PAD_DEG = 0.002

#: A published point's style token. Vector presets are free descriptive strings
#: (the QML style registry governs rasters), so it names what the point IS.
POINT_STYLE = {"kind": "reference", "geometry": "point"}

_LAYER_SCHEMES = ("s3://", "gs://", "file://", "/", "./")
_LAYER_SUFFIXES = (".geojson", ".json", ".fgb", ".gpkg", ".shp", ".parquet")
_TWO_NUMBERS = re.compile(
    r"^\s*[\[(]?\s*(-?\d+(?:\.\d+)?)\s*[, ]\s*(-?\d+(?:\.\d+)?)\s*[\])]?\s*$")


@dataclass(frozen=True, slots=True)
class Point:
    """One location in EPSG:4326, and the name the user gave it, if any."""

    lon: float
    lat: float
    name: str | None = None


class PointOutsideDomainError(UserInputError):
    """A point the domain polygon does not hold. Retryable: the fix rides the
    tool-retry loop rather than a relocation nobody asked for."""

    retryable = True

    def __init__(self, point: Point, *, label: str,
                 distance_m: float | None = None) -> None:
        self.lon, self.lat = point.lon, point.lat
        self.distance_m = float(distance_m) if distance_m is not None else None
        outside = (f", {self.distance_m:.0f} m outside its nearest edge"
                   if self.distance_m is not None else "")
        super().__init__(
            f"The {label} ({self.lon:.5f}, {self.lat:.5f}) is not inside the "
            f"domain polygon this run solves over{outside}. Nothing was relocated: "
            "moving it would answer a different question. Retry with a point "
            "INSIDE the modeled water body, widen the domain so the point falls in "
            f"it, or omit {label} and the template places it.",
            code="POINT_OUTSIDE_DOMAIN")
        self.suggestions = [
            f"Retry with {label} INSIDE the modeled water polygon (not on the "
            "bank, and not at a nearby gage).",
            "Or widen the domain - a longer reach, or a section cut that covers "
            "the point - so it falls inside.",
            f"Or omit {label} and the template places it.",
        ]


async def point(value: Any, *, label: str = "point",
                code: str = _CODE) -> Point | None:
    """THE ingestion: a pick, a pair, "lat,lon", a point layer or a place -> Point.

    ``None`` only when nothing came; a value of no readable shape refuses typed."""
    if value is None or isinstance(value, Point):
        return value
    if isinstance(value, Mapping):
        return _from_mapping(value, label, code)
    if isinstance(value, str):
        return await _from_text(value.strip(), label, code)
    lon, lat = lonlat_point(value, label=label, code=code)
    return Point(lon, lat)


def _named(value: Mapping[str, Any]) -> str | None:
    name = value.get("name")
    if name is None and isinstance(value.get("properties"), Mapping):
        name = value["properties"].get("name")
    text = str(name).strip() if name is not None else ""
    return text or None


def _from_mapping(value: Mapping[str, Any], label: str, code: str) -> Point:
    geometry = value.get("geometry") if value.get("type") == "Feature" else value
    coords: Any = None
    if isinstance(geometry, Mapping):
        if geometry.get("type") == "Point":
            coords = geometry.get("coordinates")
        elif "coordinates" in geometry and "type" not in geometry:
            coords = geometry["coordinates"]
        elif "lon" in geometry and "lat" in geometry:
            coords = (geometry["lon"], geometry["lat"])
        elif "longitude" in geometry and "latitude" in geometry:
            coords = (geometry["longitude"], geometry["latitude"])
    if coords is None:
        raise UserInputError(
            f"{label} {dict(value)!r} carries no point: give it as "
            "{\"coordinates\": [lon, lat], \"name\": ...} the way a pick returns "
            "it, as a (lon, lat) pair, or as a GeoJSON Point.", code=code)
    lon, lat = lonlat_point(list(coords)[:2], label=label, code=code)
    return Point(lon, lat, _named(value))


async def _from_text(text: str, label: str, code: str) -> Point:
    if not text:
        raise UserInputError(f"{label} is an empty string.", code=code)
    if text.startswith("{"):
        return _from_mapping(json.loads(text), label, code)
    if text.startswith(_LAYER_SCHEMES) or text.lower().endswith(_LAYER_SUFFIXES):
        return await asyncio.to_thread(_from_layer, text, label, code)
    two = _TWO_NUMBERS.match(text)
    if two:
        # A typed pair reads as it is spoken - latitude first - so "42.58,-114.31"
        # is the point near Twin Falls rather than one off the map.
        lon, lat = lonlat_point((two.group(2), two.group(1)), label=label,
                                code=code)
        return Point(lon, lat)
    return await _from_place(text, label, code)


def _from_layer(uri: str, label: str, code: str) -> Point:
    """The first point feature of a stored layer; its ``name`` property rides."""
    doc = read_geometry_doc(uri)
    features = doc.get("features") if isinstance(doc, Mapping) else None
    for feature in (features or [doc]):
        geometry = (feature.get("geometry") if feature.get("type") == "Feature"
                    else feature)
        if isinstance(geometry, Mapping) and geometry.get("type") == "Point":
            lon, lat = lonlat_point(list(geometry["coordinates"])[:2],
                                    label=label, code=code)
            return Point(lon, lat, _named(feature))
    raise UserInputError(
        f"the layer supplied as {label} ({uri}) carries no point feature; a "
        "point slot takes a point layer, a pick, a pair, or a place name.",
        code=code)


async def _from_place(name: str, label: str, code: str) -> Point:
    from .aoi import geocode_place

    found = await geocode_place(name)
    if found is None:
        raise UserInputError(
            f"{label} {name!r} could not be geocoded to a point. Give a place the "
            "geocoder knows, a (lon, lat) pair, or pick it on the canvas.",
            code=code)
    return Point(found[0], found[1], name)


def as_utm(pt: Point, utm_epsg: int) -> tuple[float, float]:
    """The point in a projected zone's metres."""
    from pyproj import Transformer

    x, y = Transformer.from_crs(4326, int(utm_epsg), always_xy=True).transform(
        pt.lon, pt.lat)
    return (float(x), float(y))


def contain(pt: Point, *, domain: Any, flowline: Any,
            label: str = "point") -> tuple[Point, float]:
    """Refuse a point outside ``domain``; move one inside it onto ``flowline``.

    Returns the moved point and how far it moved, in the domain's own UTM metres."""
    from shapely.geometry import Point as _P, shape as _shape
    from shapely.ops import nearest_points, transform as _transform, unary_union
    from pyproj import Transformer

    polygons = _of_kinds(domain, ("Polygon", "MultiPolygon"))
    if not polygons:
        raise UserInputError(
            f"the domain {domain!r} carries no polygon, so there is no shape a "
            f"{label} could be inside of.", code="DOMAIN_NO_POLYGON")
    lines = _of_kinds(flowline, ("LineString", "MultiLineString"))
    if not lines:
        raise UserInputError(
            f"the flowline {flowline!r} carries no line, so there is no river to "
            f"put the {label} on.", code="FLOWLINE_NO_LINE")

    domain_ll = unary_union([_shape(g) for g in polygons]).buffer(0)
    river_ll = unary_union([_shape(g) for g in lines])
    epsg = utm_epsg_for(float(domain_ll.centroid.x), float(domain_ll.centroid.y))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    domain_m = _transform(forward.transform, domain_ll)
    here = _P(*forward.transform(pt.lon, pt.lat))
    if not domain_m.covers(here):
        raise PointOutsideDomainError(pt, label=label,
                                      distance_m=float(here.distance(domain_m)))
    # The flowline is clipped to the domain FIRST: a river that runs on past the
    # modeled stretch would otherwise offer its own out-of-domain length as the
    # nearest point, and the run would solve a source it just refused to accept.
    reach_m = _transform(forward.transform, river_ll).intersection(domain_m)
    if reach_m.is_empty:
        raise UserInputError(
            f"the flowline {flowline!r} does not run through the domain "
            f"{domain!r}, so the two describe different reaches and there is no "
            f"river inside the domain to place the {label} on.",
            code="FLOWLINE_OUTSIDE_DOMAIN")
    moved_m, _ = nearest_points(reach_m, here)
    distance = float(here.distance(moved_m))
    lon, lat = back.transform(moved_m.x, moved_m.y)
    logger.info("%s (%.5f, %.5f) contained; moved %.1f m to (%.5f, %.5f)",
                label, pt.lon, pt.lat, distance, lon, lat)
    return Point(float(lon), float(lat), pt.name), distance


def _of_kinds(source: Any, kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    return [g for g in flatten_geometries(read_geometry_doc(source))
            if g.get("type") in kinds]


def snap_to_wet(xy: tuple[float, float], *, node_xy: Any, wet: Any,
                state: str, label: str = "point"
                ) -> tuple[tuple[float, float], float, int]:
    """Put a point where the run holds WATER at t0 -> where it went, how far, which node.

    The engine solves a source at the mesh node nearest it, so the node decides
    whether the substance enters water or bed; a dry landing moves to the nearest
    wet node and a state with no wet node anywhere refuses. Mesh metres throughout."""
    import numpy as np

    nodes = np.asarray(node_xy, dtype=float)
    mask = np.asarray(wet, dtype=bool)
    here = np.asarray(xy, dtype=float)
    reach = np.hypot(nodes[:, 0] - here[0], nodes[:, 1] - here[1])
    nearest = int(np.argmin(reach))
    if mask[nearest]:
        return (float(here[0]), float(here[1])), 0.0, nearest
    if not mask.any():
        raise UserInputError(
            f"the {label} lands at mesh node {nearest}, {reach[nearest]:.0f} m "
            f"away and DRY, and there is no wet water to move it to: {state}. A "
            f"{label} needs water at t0 - continue from a state that holds some, "
            "or run the scenario that fills the domain first.",
            code="POINT_NOWHERE_WET")
    wet_nodes = np.flatnonzero(mask)
    node = int(wet_nodes[np.argmin(reach[wet_nodes])])
    moved = float(np.hypot(nodes[node, 0] - here[0], nodes[node, 1] - here[1]))
    logger.info("%s node %d is dry at t0; moved %.1f m to wet node %d",
                label, nearest, moved, node)
    return (float(nodes[node, 0]), float(nodes[node, 1])), moved, node


async def publish_point(emitter: Any, pt: Point, *, label: str, basis: str,
                        context: str) -> bool:
    """Put the point on the canvas as a context layer. Best-effort: never fails a run.

    ``basis`` says who placed it - a user point and a derived one must not read
    the same on the map - and ``context`` names what it belongs to."""
    if emitter is None:
        return False
    try:
        uri = await asyncio.to_thread(_upload, pt, basis, context)
        from trid3nt_contracts import new_ulid
        from trid3nt_contracts.execution import LayerURI

        from trid3nt_server.emission.layer_uri_emit import publish_input_layer

        title = f"{label} {pt.name!r}" if pt.name else label
        layer = LayerURI(
            layer_id=f"point-{new_ulid()}", name=f"{title} ({basis}) - {context}",
            layer_type="vector", uri=uri, style=POINT_STYLE, role="context",
            bbox=(pt.lon - _BBOX_PAD_DEG, pt.lat - _BBOX_PAD_DEG,
                  pt.lon + _BBOX_PAD_DEG, pt.lat + _BBOX_PAD_DEG))
        emitted = await publish_input_layer(emitter, layer, role="context")
        logger.info("point layer emitted=%s basis=%s at (%.5f, %.5f)",
                    emitted, basis, pt.lon, pt.lat)
        return bool(emitted)
    except Exception as exc:  # noqa: BLE001 - a context layer never voids a solve
        logger.warning("point layer skipped: %s", exc)
        return False


def _upload(pt: Point, basis: str, context: str) -> str:
    from trid3nt_contracts import new_ulid

    from trid3nt_server.workflows.solver.solver import _get_runs_bucket, _get_s3_client

    body = json.dumps({
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {"role": "point", "basis": basis, "name": pt.name,
                           "context": context},
            "geometry": {"type": "Point",
                         "coordinates": [round(pt.lon, 6), round(pt.lat, 6)]},
        }],
    }).encode("utf-8")
    bucket = _get_runs_bucket()
    key = f"inputs/{new_ulid()}/point.geojson"
    _get_s3_client().put_object(Bucket=bucket, Key=key, Body=body,
                                ContentType="application/geo+json")
    return f"s3://{bucket}/{key}"


def point_arg(param: str, *, tool: str, prompt: str, code: str = _CODE
              ) -> Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]:
    """A coercion reading one wire field into a Point, or asking the canvas for it.

    The ask happens only in a live ``user_gated`` session and only when nothing
    came on the wire; a decline leaves the slot empty for the template to place."""
    async def _coerce(args: Mapping[str, Any]) -> dict[str, Any]:
        value = args.get(param)
        if value is None and _gated(args.get("input_mode")):
            value = await _pick(tool, param, prompt)
        return {param: await point(value, label=param, code=code)}

    _coerce.__name__ = f"point:{param}"
    return _coerce


def _gated(input_mode: Any) -> bool:
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.gates.input_review import resolve_input_gate_mode

    return (resolve_input_gate_mode(input_mode) == "user_gated"
            and current_emitter() is not None)


async def _pick(tool: str, param: str, prompt: str) -> Point | None:
    from trid3nt_server.gates.draw_input import gate_draw_input

    outcome = await gate_draw_input(tool_name=tool, param=param, geometry="point",
                                    prompt=prompt)
    if not outcome.drawn:
        logger.info("%s: %s not picked (%s)", tool, param, outcome.reason)
        return None
    return outcome.value
