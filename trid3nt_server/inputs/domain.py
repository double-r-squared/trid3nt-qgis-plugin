"""THE DOMAIN: the closed polygon the equations are solved over.

One slot, geometry only. A polygon drawn on the canvas, the user's own layer, a
fetched waterbody and a reach section between two ends all enter through
``domain`` and read the same after, so nothing downstream branches on which way
it was filled. A body of water that exists in no fetcher is filled the same way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from .boundary import RUN_TYPES, WALL, BoundaryRun, boundary_runs
from .geometry import flatten_geometries, read_geometry_doc, source_uri
from .user_input import UserInputError, polygon_ring

__all__ = ["Domain", "domain", "domain_ring", "measured_footprint",
           "needs_beside", "place_kind"]

logger = logging.getLogger("trid3nt_server.inputs.domain")

_CODE = "DOMAIN_INVALID"
_LAYER_SCHEMES = ("s3://", "gs://", "file://", "/", "./")

#: The raster suffixes this slot reads a MEASURED FOOTPRINT off instead of a
#: polygon. Anything else handed in is read as a vector document.
_RASTER_SUFFIXES = (".tif", ".tiff", ".vrt", ".img", ".asc", ".jp2")

#: WHAT A PLACE IS, in the word the hydrography sources publish that feature
#: under, by the word the service that resolved the place used for it. Only
#: water: a city, a county and a building are places this says nothing about,
#: and nothing is the honest answer - the match then ranks every row it has.
#: A word absent here is absent on purpose; guessing which water a stadium is
#: would fill a domain with the wrong shape under the right name.
_PLACE_KINDS: Mapping[str, str] = MappingProxyType({
    "river": "flowline",
    "stream": "flowline",
    "creek": "flowline",
    "brook": "flowline",
    "canal": "flowline",
    "waterway": "flowline",
    "water": "waterbody",
    "lake": "waterbody",
    "pond": "waterbody",
    "reservoir": "waterbody",
    "lagoon": "waterbody",
    "coastline": "coastline",
    "beach": "coastline",
})


@dataclass(frozen=True, slots=True)
class Domain:
    """One closed polygon in EPSG:4326, the name it was given, and where it came
    from as an ADDRESS - never as a branch: a consumer reads the geometry.

    ``geometry`` is GeoJSON Polygon or MultiPolygon."""

    geometry: dict[str, Any]
    name: str | None = None
    uri: str | None = None
    #: The stretches of this domain's edge its PRODUCER measured, where one did.
    #: A reach fetcher returns the section and the two faces it was cut between
    #: as one artifact, and a run the user draws lands the same way.
    runs: tuple[BoundaryRun, ...] = ()
    #: The geometries a producer measured BESIDE the polygon, under its own names
    #: - a reach's centerline, a catchment's snapped outlet. Each is addressable
    #: as ``Ref("<row>.<name>")``, so a question that needs one asks for that one
    #: rather than for the whole artifact; a drawn outline carries none and a
    #: template that reads one on a domain without it is refused by name.
    companions: Mapping[str, dict[str, Any]] = field(default_factory=dict)

    def __getattr__(self, name: str) -> Any:
        """A named companion geometry, read as an attribute of the domain.

        Only what the producer measured: anything else is an AttributeError, so
        a ref to a companion this domain has not got refuses at binding."""
        try:
            return object.__getattribute__(self, "companions")[name]
        except KeyError:
            raise AttributeError(name) from None

    def __str__(self) -> str:
        """What a reader CALLS this domain: the name it came with, else the word.

        A layer title, a mesh session and a run's own name are all written from
        this, so a domain that stringified as its geometry would name every one
        of them after its coordinates."""
        return self.name or "domain"

    @property
    def centroid(self) -> tuple[float, float]:
        """A lon/lat point INSIDE this polygon - what a nearest-site query ranks
        against. A representative point, never the average of the vertices: a
        crescent bay's mean vertex sits on land."""
        from shapely.geometry import shape as _shape

        point = _shape(self.geometry).representative_point()
        return (float(point.x), float(point.y))

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """The lon/lat box this polygon spans."""
        xs, ys = [], []
        for ring in _rings(self.geometry):
            xs += [float(v[0]) for v in ring]
            ys += [float(v[1]) for v in ring]
        return (min(xs), min(ys), max(xs), max(ys))

    def as_feature_collection(self) -> dict[str, Any]:
        """This domain as the one collection every geometry reader opens.

        WHOLE: the polygon, the runs its producer measured and the companions it
        wrote beside them, each under its own ``part``. A reader that keeps only
        the polygon is what turns a recorded recipe into a domain with no edge
        conditions, so the round trip back through ``domain`` is lossless."""
        return {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"role": "domain", "part": "domain",
                                               "name": self.name},
             "geometry": dict(self.geometry)},
            *(run.feature for run in self.runs),
            *({"type": "Feature", "properties": {"part": name},
               "geometry": dict(geometry)}
              for name, geometry in self.companions.items())]}


def place_kind(seed: Any) -> str:
    """WHICH FEATURE the seed is a place on, in the word a source publishes it
    under - "flowline", "waterbody", "coastline" - or "" where nothing said.

    The kind is a fact of the PLACE and not of the question asked there, so it
    is read off what the seed arrived with rather than stated by a template. A
    seed nothing said a type for is unclassified, never guessed."""
    kind = getattr(seed, "kind", None)
    if kind is None and isinstance(seed, Mapping):
        kind = seed.get("place_type")
    return _PLACE_KINDS.get(str(kind or "").strip().lower(), "")


#: WHAT A DOMAIN OF EACH KIND DECLARES BESIDE the row its own class matched, by
#: the word that kind is published under: the name it reads the row back under,
#: what that row asks its class for, and the shape it is read as. A flowline is a
#: line and a domain is a polygon, so a place on one needs the water surface the
#: line runs between before there is anything closed to solve over. Declaring a
#: need is not fetching: the slot states it and the match fills it.
_BESIDE: Mapping[str, tuple[tuple[str, str, str], ...]] = MappingProxyType({
    "flowline": (("banks", "water surface", "polygon"),)})


def needs_beside(kind: str) -> tuple[tuple[str, str, str], ...]:
    """The rows a domain of this KIND needs produced under it, or ``()``.

    Nothing for every kind that already arrives closed - a waterbody, a basin,
    an edge cut against a window."""
    return _BESIDE.get(str(kind or "").strip().lower(), ())


def domain(value: Any, *, label: str = "domain", code: str = _CODE,
           extent: Any = None, banks: Any = None, span_km: Any = None,
           near: Any = None) -> Domain | None:
    """THE ingestion: a drawing, a layer, a fetched polygon, a ring -> Domain.

    ``None`` only when nothing came; anything that is not a closed polygon
    refuses typed rather than being squared off into one. ``extent`` is the
    window the question was asked in, which a LAND-WATER EDGE is cut against:
    a coastline is a line and a domain is the polygon it leaves inside a box.
    ``banks`` is the WATER SURFACE this slot declared beside a line it stands
    on, and the cut between the two ends of ``span_km`` of that line from
    ``near`` is what closes it."""
    if value is None or isinstance(value, Domain):
        return value
    if banks is not None:
        return _between_the_ends(value, banks, near, span_km, label, code)
    uri = getattr(value, "uri", None) or (value if isinstance(value, str) else None)
    if isinstance(uri, str) and uri.strip().lower().endswith(_RASTER_SUFFIXES):
        # A SURVEY handed in as the domain: the ground it sounded is the ground
        # that can be solved over, and the file still spans a whole rectangle.
        return Domain(measured_footprint(uri.strip(), label=label, code=code),
                      _named(value), uri.strip())
    if isinstance(uri, str) and uri.strip().startswith(_LAYER_SCHEMES):
        doc = read_geometry_doc(uri.strip())
        logger.info("%s: read from %s", label, uri)
        return _carried_by(doc, _named(value), uri.strip(),
                           _runs_of(value, label, code), extent, label, code)
    if isinstance(value, Mapping):
        return _carried_by(value, _named(value), _uri_of(value),
                           _runs_of(value, label, code), extent, label, code)
    ring = polygon_ring(value, label=label, code=code)
    return Domain(_closed(ring or []))


def measured_footprint(raster: Any, *, label: str = "domain",
                       code: str = _CODE) -> dict[str, Any]:
    """Where a raster ACTUALLY measured anything, as one polygon in EPSG:4326.

    Read off the raster's own MASK, never off a threshold on values, so a real
    zero is inside the footprint and an untouched cell is not."""
    import geopandas as gpd
    import rasterio
    from rasterio.features import shapes as raster_shapes
    from shapely.geometry import mapping, shape as _shape
    from shapely.ops import unary_union

    uri = str(source_uri(raster) or "").strip()
    with rasterio.open(uri) as src:
        mask = src.dataset_mask()
        parts = [_shape(geom) for geom, value in
                 raster_shapes(mask, mask=mask.astype(bool), transform=src.transform)
                 if value]
        crs = src.crs
    if not parts:
        raise UserInputError(
            f"every cell of the {label} raster {uri!r} is nodata, so it measured "
            "nothing anywhere and there is no footprint to solve over.", code=code)
    merged = gpd.GeoSeries([unary_union(parts)], crs=crs).to_crs(4326).union_all()
    return dict(mapping(merged))


def domain_ring(dom: Domain) -> list[list[float]]:
    """The OUTER ring of the domain, open - what a mesher's extent reads."""
    rings = _rings(dom.geometry)
    return [[float(v[0]), float(v[1])] for v in rings[0][:-1]]


def _rings(geometry: Mapping[str, Any]) -> list[list[Any]]:
    kind = str(geometry.get("type") or "")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return [list(ring) for ring in coords]
    if kind == "MultiPolygon":
        return [list(ring) for polygon in coords for ring in polygon]
    return []


def _carried_by(doc: Any, name: str | None, uri: str | None,
                stated: tuple[BoundaryRun, ...], extent: Any, label: str,
                code: str) -> Domain:
    """The domain one geometry document carries.

    A document of LINES carries no polygon and no companions either: it is the
    land-water EDGE, and the water the cut leaves is the whole of what the slot
    then holds."""
    geometries = list(flatten_geometries(read_geometry_doc(doc)))
    polygons = [g for g in geometries
                if str(g.get("type")) in ("Polygon", "MultiPolygon")]
    if polygons:
        return Domain(_one_polygon(polygons), name, uri,
                      stated or _prescribed(doc, label, code), _companions(doc))
    if not any(str(g.get("type")) in ("LineString", "MultiLineString")
               for g in geometries):
        raise UserInputError(_nothing_closed(geometries, uri, label), code=code)
    return Domain(_water_left_by(doc, extent, label, code), name, uri)


def _nothing_closed(geometries: list[dict[str, Any]], uri: str | None,
                    label: str) -> str:
    """Why this document closes nothing, in the two cases a reader acts on
    differently: a producer that RAN and returned nothing, and a document that
    carries the wrong shape. An empty artifact and an absent one read alike
    otherwise, and the reader is told to draw a polygon that already exists."""
    closed = ("A domain is the CLOSED outline the equations are solved over: "
              "draw it, name a polygon layer, or let the template's own "
              "producer find one.")
    if not uri:
        return f"the {label} carries no polygon geometry. {closed}"
    if not geometries:
        return (f"the {label} was produced by {uri} and that artifact carries "
                "ZERO features: the producer RAN and found nothing, which is "
                "not the same as no producer running. Read that row's own "
                f"refusal for what the service answered. {closed}")
    kinds = sorted({str(g.get("type")) for g in geometries})
    return (f"the {label} came from {uri}, which carries {len(geometries)} "
            f"geometries of kind {', '.join(kinds)} - no polygon and no "
            f"land-water edge among them. {closed}")


def _one_polygon(polygons: list[dict[str, Any]]) -> dict[str, Any]:
    """The polygon a document carries; several are the one they cover together."""
    if len(polygons) == 1:
        return dict(polygons[0])
    # SEVERAL polygons are one domain with parts - a lake with islands cut out,
    # a two-basin harbour - and the mesher meshes the union. Picking the largest
    # would silently drop the rest.
    return {"type": "MultiPolygon",
            "coordinates": [g["coordinates"] if str(g["type"]) == "Polygon"
                            else part
                            for g in polygons
                            for part in ([g["coordinates"]]
                                         if str(g["type"]) == "Polygon"
                                         else g["coordinates"])]}


def _water_left_by(doc: Any, extent: Any, label: str,
                   code: str) -> dict[str, Any]:
    """The water a LAND-WATER EDGE leaves inside the window this question was asked in.

    A coastline is a line, a domain is a polygon, and the step between the two
    is this slot's ingestion: the mesh seam's own cut, run where the line
    arrived. Without a window there is nothing to cut against, and a cut that
    does not close refuses in the cut's own words."""
    from trid3nt_server.workflows.mesh.meshers import MeshToolError
    from trid3nt_server.workflows.mesh.water import water_polygon

    if extent is None:
        raise UserInputError(
            f"the {label} carries lines and no polygon, and this run states no "
            "window to cut them against. A land-water edge divides a box into "
            "land and water: draw the box this question is asked inside, or "
            "supply the outline itself.", code=code)
    try:
        return dict(water_polygon(doc, extent))
    except MeshToolError as exc:
        raise UserInputError(str(exc), code=exc.error_code) from exc


def _closed(ring: list[list[float]]) -> dict[str, Any]:
    """A typed or drawn OPEN ring as a closed GeoJSON polygon.

    How few vertices a ring may have is the ingestion's own rule, stated where
    every drawn shape passes it."""
    return {"type": "Polygon", "coordinates": [[*ring, list(ring[0])]]}


def _runs_of(value: Any, label: str, code: str) -> tuple[BoundaryRun, ...]:
    """The boundary runs a producer stated beside its polygon, or none.

    A producer that measured the edge says so ON the artifact; a drawn outline
    says nothing, and nothing is an answer."""
    stated = (value.get("runs") if isinstance(value, Mapping)
              else getattr(value, "runs", None))
    return boundary_runs(stated, label=f"{label} runs", code=code)


def _prescribed(doc: Any, label: str, code: str) -> tuple[BoundaryRun, ...]:
    """The run rows a producer wrote INTO the artifact beside its polygon.

    A fetcher that cut a polygon between two faces returns those faces as rows
    of the same document, each row naming which stretch it is; a row naming a
    stretch that prescribes nothing is the wall the edge already is, and is not
    a run. The row's own word for which row it is - ``part`` on a multi-part
    artifact - is what names it, because a slot reads the producer's vocabulary
    rather than asking the producer to speak its own."""
    if not isinstance(doc, Mapping) or doc.get("type") != "FeatureCollection":
        return ()
    rows = []
    for feature in (doc.get("features") or ()):
        properties = dict((feature or {}).get("properties") or {})
        kind = str(properties.get("type") or properties.get("part") or "").strip()
        geometry = (feature or {}).get("geometry") or {}
        if kind in RUN_TYPES and kind != WALL \
                and str(geometry.get("type")) == "LineString":
            rows.append({**feature, "properties": {**properties, "type": kind}})
    return boundary_runs(rows, label=f"{label} runs", code=code) if rows else ()


def _companions(doc: Any) -> dict[str, dict[str, Any]]:
    """The geometries a producer wrote beside its polygon, by the name it gave.

    A producer names each row by what it IS - ``centerline``, ``outlet`` - and
    the slot keeps every row that is neither the polygon nor a run under that
    name. A row naming a run type is a boundary run and is read as one."""
    if not isinstance(doc, Mapping) or doc.get("type") != "FeatureCollection":
        return {}
    found: dict[str, dict[str, Any]] = {}
    for feature in (doc.get("features") or ()):
        properties = dict((feature or {}).get("properties") or {})
        name = str(properties.get("part") or properties.get("name") or "").strip()
        geometry = (feature or {}).get("geometry") or {}
        kind = str(geometry.get("type") or "")
        if not name or name in RUN_TYPES or not kind \
                or kind in ("Polygon", "MultiPolygon"):
            continue
        found.setdefault(name, dict(geometry))
    return found


def _named(value: Any) -> str | None:
    name = value.get("name") if isinstance(value, Mapping) else getattr(
        value, "name", None)
    text = str(name).strip() if name is not None else ""
    return text or None


def _uri_of(value: Mapping[str, Any]) -> str | None:
    uri = value.get("uri")
    return str(uri) if uri else None


#: How far apart the two ends of the span must stand before the cut planes have
#: a direction to be square to, in metres. Below it the two ends are one point.
_MIN_CHORD_M = 1.0

#: How far off a cut plane a vertex may stand and still BE on it, in metres. The
#: cut puts its vertices there exactly; what separates them from the surface's
#: own bank vertices is the clip's double-precision residue - nanometres on a
#: UTM coordinate - against metres of real geometry.
_ON_CUT_M = 1.0e-6


def _between_the_ends(line_doc: Any, banks: Any, near: Any, span_km: Any,
                      label: str, code: str) -> Domain:
    """The WATER SURFACE cut square to a line, between the two ends of the span.

    A line is not a domain and a surface is not one either: the polygon is what
    the two leave together, and the two end transects the cut left are the runs
    the water crosses. The centerline rides along as a companion, because the
    producer of neither row measured it as one."""
    from shapely.geometry import mapping

    centre = _clipped(_one_line(line_doc, label, code), near, span_km, label,
                      code)
    polygon, faces = _cut_square(_surfaces(banks, label, code), centre, label,
                                 code)
    runs = boundary_runs(
        [{"type": "Feature", "properties": {"type": kind},
          "geometry": {"type": "LineString", "coordinates": face}}
         for kind, face in zip(("inflow", "outflow"), faces)],
        label=f"{label} runs", code=code)
    return Domain(polygon, _named(line_doc), source_uri(line_doc) or None, runs,
                  {"centerline": dict(mapping(centre))})


def _one_line(doc: Any, label: str, code: str) -> Any:
    """Every line the row carries, joined into ONE running the way the water does.

    NHD digitizes downstream, so the merged line is put in the vertex order of
    its longest part: the first vertex is then the upstream end whichever way
    the walk that produced the rows ran."""
    from shapely.geometry import LineString, Point, shape
    from shapely.ops import linemerge

    parts = [shape(g) for g in flatten_geometries(read_geometry_doc(doc))
             if str(g.get("type")) in ("LineString", "MultiLineString")]
    if not parts:
        raise UserInputError(
            f"the {label} row carries no line at all, so there is nothing to "
            "cut the water surface square to. Supply the outline itself.",
            code=code)
    merged = parts[0] if len(parts) == 1 else linemerge(parts)
    pieces = sorted(getattr(merged, "geoms", [merged]), key=lambda g: -g.length)
    line = pieces[0]
    longest = max(parts, key=lambda part: part.length)
    if line.project(Point(longest.coords[0])) \
            > line.project(Point(longest.coords[-1])):
        line = LineString(list(line.coords)[::-1])
    return line


def _clipped(line: Any, near: Any, span_km: Any, label: str, code: str) -> Any:
    """``span_km`` of the line DOWNSTREAM of the seed, in the line's flow order.

    Measured on the ground rather than in degrees, so a span is the same length
    at the Gulf and at the Canadian border. A run that states no seed takes the
    line as it came - the row was already asked over the span."""
    from pyproj import Transformer
    from shapely.geometry import Point
    from shapely.ops import substring, transform as _transform

    from .geometry import utm_epsg_for
    from .point import lonlat_of

    seed = lonlat_of(near)
    if seed is None or not span_km:
        return line
    epsg = utm_epsg_for(float(line.centroid.x), float(line.centroid.y))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    metric = _transform(forward.transform, line)
    at_seed = metric.project(_transform(forward.transform, Point(*seed)))
    cut = substring(metric, at_seed,
                    min(at_seed + float(span_km) * 1000.0, metric.length))
    if cut.is_empty or cut.length <= 0.0:
        raise UserInputError(
            f"the {label} seed stands at the far end of the line it was given, "
            "so no stretch of it runs on from there. Seed the upstream end of "
            "the stretch this question is about.", code=code)
    return _transform(back.transform, cut)


def _surfaces(banks: Any, label: str, code: str) -> list[Any]:
    """The water-surface polygons the declared row carries, valid."""
    from shapely.geometry import shape

    found = []
    for geometry in flatten_geometries(read_geometry_doc(banks)):
        geom = shape(geometry)
        for part in getattr(geom, "geoms", [geom]):
            if part.geom_type == "Polygon" and not part.is_empty:
                found.append(part if part.is_valid else part.buffer(0))
    if not found:
        raise UserInputError(
            f"nothing maps a water SURFACE along this {label}, so it has no "
            "banks to solve between - the channel here is a centreline only. "
            "Model a mapped channel, or supply the domain polygon yourself.",
            code=code)
    return found


def _cut_square(surfaces: list[Any], centre: Any, label: str,
                code: str) -> tuple[dict[str, Any], list[list[list[float]]]]:
    """The surfaces cut square to the line's two ends -> the polygon and the
    two transects the cut left.

    Square is measured in the local UTM zone, because square in degrees is not
    square on the ground."""
    import numpy as np
    from pyproj import Transformer
    from shapely.geometry import LineString, mapping
    from shapely.ops import transform as _transform, unary_union

    from .geometry import utm_epsg_for

    union_ll = unary_union(surfaces)
    epsg = utm_epsg_for(float(union_ll.centroid.x), float(union_ll.centroid.y))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    union_m = _transform(forward.transform, union_ll)
    a = np.asarray(forward.transform(*centre.coords[0]), dtype=float)
    b = np.asarray(forward.transform(*centre.coords[-1]), dtype=float)
    chord = float(np.hypot(*(b - a)))
    if chord < _MIN_CHORD_M:
        raise UserInputError(
            f"the {label} line is {chord:.3f} m long, which is one point: there "
            "is no direction for the end cuts to be square to. Ask for a longer "
            "span, or seed a channel with line either side of it.", code=code)
    minx, miny, maxx, maxy = union_m.bounds
    across = float(np.hypot(maxx - minx, maxy - miny)) + chord
    parts = [p for p in getattr(union_m.intersection(_strip(a, b, across)),
                                "geoms", [union_m.intersection(
                                    _strip(a, b, across))])
             if p.geom_type == "Polygon"]
    kept = [p for p in parts if p.intersects(LineString([tuple(a), tuple(b)]))]
    if not kept:
        raise UserInputError(
            f"the {label} line touches no part of the water surface between its "
            "two ends, so which piece is the domain is not measurable. Seed a "
            "channel whose banks are mapped.", code=code)
    if len(parts) > len(kept):
        logger.info("%s: %d piece(s) between the end cuts do not touch the line "
                    "and were left out", label, len(parts) - len(kept))
    cut_m = unary_union(kept)
    unit = (b - a) / chord
    normal = np.array([-unit[1], unit[0]])
    faces = [_end_face(cut_m, point, unit, normal, name, back, label, code)
             for point, name in ((a, "inflow"), (b, "outflow"))]
    return dict(mapping(_transform(back.transform, cut_m))), faces


def _strip(a: Any, b: Any, across: float) -> Any:
    """The band between the two perpendicular cuts at ``a`` and ``b``, wide
    enough that only those two cuts ever touch the surface."""
    import numpy as np
    from shapely.geometry import Polygon

    span = np.asarray(b, dtype=float) - np.asarray(a, dtype=float)
    unit = span / float(np.hypot(*span))
    wide = np.array([-unit[1], unit[0]]) * across
    return Polygon([tuple(a + wide), tuple(b + wide),
                    tuple(b - wide), tuple(a - wide)])


def _end_face(cut_m: Any, point: Any, unit: Any, normal: Any, name: str,
              back: Any, label: str, code: str) -> list[list[float]]:
    """The TRANSECT one cut left, as its two lon/lat ends.

    The cut put VERTICES on the plane through the point, and the two furthest
    apart across the water are the face's ends. They are found by projecting the
    polygon's OWN boundary vertices, never by intersecting a probe line: the cut
    edge is exactly collinear with such a line, and a collinear intersection
    over a domain-sized probe returns an edge at one end and nothing at the
    other, for no reason a reader can see."""
    import numpy as np

    origin = np.asarray(point, dtype=float)
    rings = [np.asarray(ring.coords, dtype=float)
             for part in getattr(cut_m, "geoms", [cut_m])
             for ring in (part.exterior, *part.interiors)]
    vertices = np.vstack(rings) - origin
    on_plane = np.abs(vertices @ np.asarray(unit, dtype=float)) <= _ON_CUT_M
    across = vertices[on_plane] @ np.asarray(normal, dtype=float)
    if across.size < 2 or float(across.max() - across.min()) <= 0.0:
        raise UserInputError(
            f"the {name} end of this {label} left no transect on the water "
            "surface: the water reaches that end along its own bank rather "
            "than along the cut, so there is no transect there to prescribe a "
            "boundary across. Seed a stretch the surface maps end to end.",
            code=code)
    ends = vertices[on_plane][[int(across.argmin()), int(across.argmax())]] \
        + origin
    return [[float(v) for v in back.transform(*end)] for end in ends]
