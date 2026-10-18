"""A POINT: one location with an optional name, from every way a user names one."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from .user_input import PlaceNameError, UserInputError, lonlat_point

from .geometry import read_geometry_doc

__all__ = ["Point", "PointNotViableError", "as_utm", "containing_face", "point",
           "point_arg", "publish_point", "require_viable"]

logger = logging.getLogger("trid3nt_server.inputs.point")

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
    """One location in EPSG:4326, the name the user gave it, and what the place
    that named it IS."""

    lon: float
    lat: float
    name: str | None = None
    #: THE PLACE'S OWN WORD for what it is - "river", "reservoir", "city" - where the
    #: service that resolved the name published one, "" where nothing said.
    kind: str = ""


class PointNotViableError(UserInputError):
    """A point that misses what the run solves, said with the nearest place it would not
    miss. Retryable: a restated point fixes it, and nothing is ever moved for it."""

    retryable = True

    def __init__(self, point: Point, *, label: str, missed: str,
                 hint: tuple[float, float, float] | None = None) -> None:
        self.lon, self.lat = point.lon, point.lat
        self.hint = (None if hint is None else
                     {"lon": round(hint[0], 6), "lat": round(hint[1], 6),
                      "distance_m": round(hint[2], 1)})
        nearest = ("" if self.hint is None else
                   f" The nearest place it would not miss is ({self.hint['lon']:.5f}, "
                   f"{self.hint['lat']:.5f}), {self.hint['distance_m']:.0f} m away.")
        super().__init__(
            f"The {label} ({self.lon:.5f}, {self.lat:.5f}) is {missed}. It was not "
            f"moved: a point placed for the run answers a question nobody asked.{nearest} "
            f"State {label} where it lands on what the run solves, or pick it on the "
            "canvas.", code="POINT_NOT_VIABLE")
        self.suggestions = [
            f"Restate {label} where it lands on what the run solves"
            + ("" if self.hint is None else
               f", e.g. ({self.hint['lon']:.5f}, {self.hint['lat']:.5f})") + ".",
            f"Or ask the user to place {label} on the canvas.",
        ]


async def point(value: Any, *, label: str = "point",
                code: str = _CODE) -> Point | None:
    """THE ingestion: a pick, a pair, "lat,lon" or a point layer -> Point.

    ``None`` only when nothing came; a value of no readable shape refuses typed,
    and a place name refuses naming ``geocode_location``."""
    if value is None or isinstance(value, Point):
        return value
    if isinstance(value, Mapping):
        return _from_mapping(value, label, code)
    if isinstance(value, str):
        return await _from_text(value.strip(), label, code)
    lon, lat = lonlat_point(value, label=label, code=code)
    return Point(lon, lat)


def _kind_of(value: Mapping[str, Any]) -> str:
    """The word the answering service used for what this place is, or ""."""
    kind = value.get("place_type")
    if kind is None and isinstance(value.get("properties"), Mapping):
        kind = value["properties"].get("place_type")
    return str(kind).strip().lower() if kind else ""


def _named(value: Mapping[str, Any]) -> str | None:
    name = value.get("name")
    if name is None and isinstance(value.get("properties"), Mapping):
        name = value["properties"].get("name")
    text = str(name).strip() if name is not None else ""
    return text or None


def lonlat_of(value: Any) -> tuple[float, float] | None:
    """The place a value STATES, or ``None`` where it states none."""
    if value is None:
        return None
    lon, lat = getattr(value, "lon", None), getattr(value, "lat", None)
    if lon is None or lat is None:
        try:
            if isinstance(value, Mapping):
                found = _from_mapping(value, "point", _CODE)
                lon, lat = found.lon, found.lat
            else:
                lon, lat = lonlat_point(value, label="point", code=_CODE)
        except Exception:  # noqa: BLE001 - no place is an answer here
            return None
    return (float(lon), float(lat))


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
    return Point(lon, lat, _named(value), _kind_of(value))


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
    raise PlaceNameError(text, label=label, code=code,
                         wants="a (lon, lat) pair", retry_as="[lon, lat]")


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
        "point slot takes a point layer, a pick, or a (lon, lat) pair.",
        code=code)


def as_utm(pt: Point, utm_epsg: int) -> tuple[float, float]:
    """The point in a projected zone's metres."""
    from pyproj import Transformer

    x, y = Transformer.from_crs(4326, int(utm_epsg), always_xy=True).transform(
        pt.lon, pt.lat)
    return (float(x), float(y))


#: The metres a point may stand off a face and still be read as on it - a lon/lat
#: round-trip through a projected zone lands a whole micron away, never exactly on.
_ON_FACE_M = 1e-6


def containing_face(xy: tuple[float, float], *, x: Any, y: Any, faces: Any
                    ) -> tuple[int | None, float]:
    """The index of the face holding ``xy`` in the zone's own metres and 0.0 away,
    else ``None`` and the metres from ``xy`` to the nearest face.

    Pure over the arrays a result already carries: the node coordinates and the
    faces' vertex indices - no viability, no movement, no refusal, no run."""
    import shapely

    px, py = float(xy[0]), float(xy[1])
    polygons = [shapely.Polygon([(float(x[i]), float(y[i])) for i in face])
                for face in faces]
    tree = shapely.STRtree(polygons)
    here = shapely.Point(px, py)
    hits = tree.query(here, predicate="covered_by")
    if len(hits):
        return int(sorted(hits)[0]), 0.0
    near = int(tree.nearest(here))
    away = float(shapely.distance(here, polygons[near]))
    if away <= _ON_FACE_M:
        return near, 0.0
    return None, away


def require_viable(pt: Point, *, label: str, utm_epsg: int, sites_xy: Any,
                   viable: Any, resolve: Callable[[tuple[float, float]], int | None],
                   missed: str, region_m: Any = None) -> int:
    """The site the engine reads ``pt`` at, or the refusal naming what it missed and the nearest viable site.

    ``resolve`` is the engine's own reading of a point (its nearest node, its containing cell) in the zone's
    metres; ``viable`` marks the sites a point may land on; ``region_m`` the domain it must lie inside."""
    import numpy as np
    from pyproj import Transformer
    from shapely.geometry import Point as _P

    here = as_utm(pt, utm_epsg)
    sites = np.asarray(sites_xy, dtype=float)
    held = np.asarray(viable, dtype=bool)
    site = None
    if region_m is not None and not region_m.covers(_P(*here)):
        reason = "outside the domain this run solves over"
    else:
        site = resolve(here)
        if site is not None and held[site]:
            return int(site)
        reason = ("outside the mesh the run solves on" if site is None
                  else f"{missed} (site {site})")
    hint = None
    if held.any():
        candidates = np.flatnonzero(held)
        reach = np.hypot(sites[candidates, 0] - here[0], sites[candidates, 1] - here[1])
        nearest = int(candidates[int(np.argmin(reach))])
        lon, lat = Transformer.from_crs(int(utm_epsg), 4326, always_xy=True).transform(
            float(sites[nearest, 0]), float(sites[nearest, 1]))
        hint = (float(lon), float(lat), float(reach.min()))
    raise PointNotViableError(pt, label=label, missed=reason, hint=hint)


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

        from trid3nt_server.render.layer_uri_emit import publish_input_layer

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

    from trid3nt_server.store import objects as storage

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
    bucket = storage.runs_bucket()
    key = f"inputs/{new_ulid()}/point.geojson"
    storage.client().put_object(Bucket=bucket, Key=key, Body=body,
                                ContentType="application/geo+json")
    return f"s3://{bucket}/{key}"


def point_arg(param: str, *, tool: str, prompt: str | None, code: str = _CODE
              ) -> Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]:
    """A coercion reading one wire field into a Point, or asking the canvas for it.

    The ask happens only in a live ``user_gated`` session, only when nothing came on
    the wire and only with a ``prompt``: a point the caller may leave unstated is
    never asked for. A decline leaves the slot empty for the template to place."""
    async def _coerce(args: Mapping[str, Any]) -> dict[str, Any]:
        from trid3nt_server.inputs.slots import draw_on_canvas

        value = args.get(param)
        if value is None and prompt is not None:
            value = await draw_on_canvas("point", tool=tool, param=param,
                                         prompt=prompt,
                                         input_mode=args.get("input_mode"))
        return {param: await point(value, label=param, code=code)}

    _coerce.__name__ = f"point:{param}"
    return _coerce
