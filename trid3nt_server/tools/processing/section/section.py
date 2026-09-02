"""``section``: cut a polygon layer down to the part a chain actually wants.

One generic geometry primitive with two cuts. BETWEEN two points: the polygon is
sliced by the two lines perpendicular to the chord joining them, at each end, and
what survives between them is kept - which is how a mapped water polygon becomes
the REACH between an upstream and a downstream point, with the two cuts standing
as the transects an inflow and an outflow are prescribed on. WITHIN an extent:
the polygon meets a box, the ordinary clip.

The cut is measured in the local UTM zone, because a line perpendicular to a
chord in degrees is not perpendicular on the ground, and a section that is not
square to the reach is not the reach.

Nothing is inferred here: no buffering, no snapping, no widening. A polygon this
tool is handed is a polygon somebody mapped or drew; what comes back is a subset
of it. When the input carries no polygon there is nothing to section and it
refuses, because a shape invented to stand in for the missing one would be a
domain no measurement backs.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.processing._geometry_common import (
    GeometryReadError,
    flatten_geometries,
    read_geometry_doc,
)
from trid3nt_server.tools.processing._hydrology_common import _write_geojson

__all__ = ["SectionLayerURI", "SectionError", "section"]

logger = logging.getLogger("trid3nt_server.tools.processing.section.section")


class SectionError(RuntimeError):
    """A typed section refusal: an error code plus what to supply instead.

    Codes:
    - ``SECTION_INPUT_INVALID`` -- neither ``between`` nor ``within`` given (or
      both), or a point/extent that is not two/four finite numbers.
    - ``SECTION_NO_POLYGON`` -- the source layer carries no polygon geometry.
    - ``SECTION_CUT_EMPTY`` -- the cut and the polygon do not meet.
    - ``SECTION_END_FACE_UNMEASURED`` -- one end cut left no transect on the
      polygon, so the section ends along its own bank there.
    - ``SECTION_SOURCE_UNREADABLE`` -- the source is neither inline GeoJSON nor a
      readable vector layer.
    """

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str, *,
                 retryable: bool = False) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


class SectionLayerURI(LayerURI):
    """Section polygon ``LayerURI`` plus what the cut measured.

    Extra fields beyond ``LayerURI``: ``area_km2`` (what survived the cut),
    ``source_area_km2`` (what went in), ``length_m`` (the chord, 0 for an extent
    cut), ``utm_epsg`` (the zone the cut was measured in, 0 for an extent cut),
    ``parts_kept`` / ``parts_dropped`` (disconnected pieces the cut left),
    ``face_start`` / ``face_end`` (the two transects the ``between`` cut left, as
    ``[[lon, lat], [lon, lat]]`` - what a boundary role is prescribed across;
    empty on an extent cut, which leaves no transect), ``notes`` (every choice the
    cut made).
    """

    area_km2: float = 0.0
    source_area_km2: float = 0.0
    length_m: float = 0.0
    utm_epsg: int = 0
    parts_kept: int = 0
    parts_dropped: int = 0
    face_start: list[list[float]] = []
    face_end: list[list[float]] = []
    notes: list[str] = []


#: Below this the two points are one point and the chord has no direction to be
#: perpendicular to.
_MIN_CHORD_M: float = 1.0

#: How far off the cut plane a vertex may stand and still BE on it, in metres. The
#: cut puts its vertices there exactly; what separates them from the section's own
#: bank vertices is the clip's double-precision residue (nanometres on a UTM
#: coordinate) against metres of real geometry.
_ON_CUT_M: float = 1.0e-6

#: The label the section travels under: an outline the tool CUT, whose meaning is
#: whatever the polygon it came from meant. A producer names its own preset, and
#: this one cannot claim the source's semantics because it is handed any polygon.
_STYLE_PRESET = "section_polygon"

_SECTION_METADATA = AtomicToolMetadata(
    name="section",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


def _polygons(source: Any) -> list[Any]:
    """The polygon geometries the section is cut from, in EPSG:4326.

    Read through the SHARED geometry reader, so a chain that binds the producing
    tool's layer value and a person who types its uri reach the same file.
    """
    from shapely.geometry import shape as _shape

    try:
        geoms = [_shape(g) for g in flatten_geometries(read_geometry_doc(source))]
    except GeometryReadError as exc:
        raise SectionError("SECTION_SOURCE_UNREADABLE", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - every reader fault, named by source
        raise SectionError(
            "SECTION_SOURCE_UNREADABLE",
            f"the polygon source {source!r} could not be read: it is neither "
            f"inline GeoJSON nor a readable vector layer ({exc}).") from exc
    out: list[Any] = []
    for geom in geoms:
        if geom is None or geom.is_empty:
            continue
        for part in getattr(geom, "geoms", [geom]):
            if part.geom_type == "Polygon":
                out.append(part if part.is_valid else part.buffer(0))
    return [p for p in out if not p.is_empty]


def _point(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise SectionError(
            "SECTION_INPUT_INVALID",
            f"{label} must be (lon, lat); got {value!r}")
    try:
        lon, lat = float(value[0]), float(value[1])
    except (TypeError, ValueError) as exc:
        raise SectionError(
            "SECTION_INPUT_INVALID",
            f"{label} holds non-numeric values: {value!r}") from exc
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise SectionError(
            "SECTION_INPUT_INVALID", f"{label} is non-finite: {value!r}")
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise SectionError(
            "SECTION_INPUT_INVALID",
            f"{label} is outside the lon/lat range: {value!r}")
    return (lon, lat)


def _extent(value: Any) -> tuple[float, float, float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 4:
        raise SectionError(
            "SECTION_INPUT_INVALID",
            f"within must be (min_lon, min_lat, max_lon, max_lat); got {value!r}")
    try:
        box = tuple(float(v) for v in value)
    except (TypeError, ValueError) as exc:
        raise SectionError(
            "SECTION_INPUT_INVALID",
            f"within holds non-numeric values: {value!r}") from exc
    if not all(math.isfinite(v) for v in box):
        raise SectionError(
            "SECTION_INPUT_INVALID", f"within is non-finite: {value!r}")
    if not (box[0] < box[2] and box[1] < box[3]):
        raise SectionError(
            "SECTION_INPUT_INVALID",
            f"within has no area: {value!r} (need min_lon < max_lon and "
            "min_lat < max_lat).")
    return box  # type: ignore[return-value]


def _band(a: Any, b: Any, reach: float) -> Any:
    """The strip between the two perpendicular cuts at ``a`` and ``b``.

    Wide enough to span anything the polygon can reach sideways, so the only
    edges of the strip that ever touch the polygon are the two cuts themselves.
    """
    import numpy as np
    from shapely.geometry import Polygon

    span = np.asarray(b, dtype=float) - np.asarray(a, dtype=float)
    length = float(np.hypot(*span))
    unit = span / length
    normal = np.array([-unit[1], unit[0]])
    wide = normal * reach
    return Polygon([tuple(a + wide), tuple(b + wide),
                    tuple(b - wide), tuple(a - wide)])


def _end_face(section_m: Any, point: Any, unit: Any, normal: Any, label: str,
              back: Any) -> list[list[float]]:
    """The TRANSECT the cut left at one end -> its two lon/lat ends.

    The end cut is a real edge of the section, so the face is measured off the
    geometry rather than restated: the cut put VERTICES on the plane through the
    point, and the two furthest apart across the reach are the face's ends. They
    are found by projecting the section's own boundary vertices, never by
    intersecting a probe line with the polygon - the cut edge is exactly collinear
    with such a line, and a collinear intersection over a domain-sized probe
    returns an edge at one end and nothing at the other for no reason a reader can
    see. A solver prescribes its inflow across this whole face, so the face - not
    the single point the chain named it by - is what a boundary role is matched
    against.
    """
    import numpy as np

    origin = np.asarray(point, dtype=float)
    rings = [np.asarray(ring.coords, dtype=float)
             for part in getattr(section_m, "geoms", [section_m])
             for ring in (part.exterior, *part.interiors)]
    vertices = np.vstack(rings) - origin
    on_plane = np.abs(vertices @ np.asarray(unit, dtype=float)) <= _ON_CUT_M
    across = vertices[on_plane] @ np.asarray(normal, dtype=float)
    if across.size < 2 or float(across.max() - across.min()) <= 0.0:
        raise SectionError(
            "SECTION_END_FACE_UNMEASURED",
            f"the {label} cut left no transect on the polygon: {int(on_plane.sum())} "
            f"of the section's {vertices.shape[0]} boundary vertices stand on the "
            f"cut plane, and the nearest one is "
            f"{float(np.abs(vertices @ np.asarray(unit, dtype=float)).min()):.3f} m "
            "off it. The section reaches that end along its own bank rather than "
            "along the cut, so there is no transect there to prescribe across - "
            "move that point onto the stretch the polygon maps.")
    ends = vertices[on_plane][[int(across.argmin()), int(across.argmax())]] + origin
    return [[float(v) for v in back.transform(*e)] for e in ends]


def _cut_between(polys: list[Any], start: tuple[float, float],
                 end: tuple[float, float],
                 notes: list[str]) -> tuple[Any, int, int, float, int,
                                            list[list[float]],
                                            list[list[float]]]:
    """Section the polygons between two points -> geometry, parts, chord, zone, faces."""
    import numpy as np
    from pyproj import Transformer
    from shapely.geometry import LineString
    from shapely.ops import transform as _transform, unary_union

    from trid3nt_server.tools.processing._geometry_common import utm_epsg_for

    union_ll = unary_union(polys)
    lon_c, lat_c = union_ll.centroid.x, union_ll.centroid.y
    epsg = utm_epsg_for(float(lon_c), float(lat_c))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    union_m = _transform(forward.transform, union_ll)

    a = np.asarray(forward.transform(*start), dtype=float)
    b = np.asarray(forward.transform(*end), dtype=float)
    chord = float(np.hypot(*(b - a)))
    if chord < _MIN_CHORD_M:
        raise SectionError(
            "SECTION_INPUT_INVALID",
            f"the two points are {chord:.3f} m apart, which is one point: there "
            "is no direction for the end cuts to be square to. Supply two "
            "points at opposite ends of the stretch you want.")

    minx, miny, maxx, maxy = union_m.bounds
    reach = float(np.hypot(maxx - minx, maxy - miny)) + chord
    cut = union_m.intersection(_band(a, b, reach))
    if cut.is_empty:
        raise SectionError(
            "SECTION_CUT_EMPTY",
            f"nothing of the polygon lies between {start} and {end}: the two "
            "end cuts fall entirely off it. Supply two points on the polygon.")

    axis = LineString([tuple(a), tuple(b)])
    parts = [p for p in getattr(cut, "geoms", [cut]) if p.geom_type == "Polygon"]
    kept = [p for p in parts if p.intersects(axis)]
    if not kept:
        raise SectionError(
            "SECTION_CUT_EMPTY",
            f"the line between {start} and {end} touches no part of the polygon, "
            f"so which of the {len(parts)} piece(s) between the end cuts is the "
            "one you meant is not measurable. Supply two points on the polygon.")
    dropped = len(parts) - len(kept)
    if dropped:
        notes.append(
            f"{dropped} piece(s) between the two end cuts do not touch the line "
            "joining the points and were left out of the section.")
    notes.append(
        f"Cut square to the {chord:.1f} m line between the two points, measured "
        f"in EPSG:{epsg}.")
    section_m = unary_union(kept)
    unit = (b - a) / chord
    normal = np.array([-unit[1], unit[0]])
    return (_transform(back.transform, section_m), len(kept), dropped, chord,
            int(epsg), _end_face(section_m, a, unit, normal, "upstream", back),
            _end_face(section_m, b, unit, normal, "downstream", back))


def _cut_within(polys: list[Any], box: tuple[float, float, float, float],
                notes: list[str]) -> tuple[Any, int, int]:
    """Section the polygons down to a lon/lat box -> geometry and parts."""
    from shapely.geometry import box as _box
    from shapely.ops import unary_union

    cut = unary_union(polys).intersection(_box(*box))
    if cut.is_empty:
        raise SectionError(
            "SECTION_CUT_EMPTY",
            f"the polygon does not reach into {box}: the clip is empty. Widen "
            "the extent, or section a polygon that covers it.")
    parts = [p for p in getattr(cut, "geoms", [cut]) if p.geom_type == "Polygon"]
    notes.append(f"Clipped to the extent {tuple(round(v, 6) for v in box)}.")
    return unary_union(parts), len(parts), 0


def _area_km2(geom: Any) -> float:
    """Area in square kilometres, measured in the geometry's own UTM zone."""
    from pyproj import Transformer
    from shapely.ops import transform as _transform

    from trid3nt_server.tools.processing._geometry_common import utm_epsg_for

    epsg = utm_epsg_for(float(geom.centroid.x), float(geom.centroid.y))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    return float(_transform(forward.transform, geom).area) / 1.0e6


@register_tool(
    _SECTION_METADATA,
    read_only_hint=False,
    # Reads only the polygon it is handed and writes its own artifact.
    open_world_hint=False,
)
def section(
    polygon: str,
    between: Any = None,
    within: Any = None,
    *,
    _output_dir: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> SectionLayerURI:
    """Cut a POLYGON LAYER down to the part between two points, or inside an extent -> a polygon layer.

    Use this when: "just the reach of the river between these two points", "the
    stretch of this water body from here to here", "clip this polygon to my
    area". The polygon it returns is a DOMAIN: hand it to
    ``build_mesh(mesher='om2d', extent=<this uri>)`` to triangulate its interior,
    or to ``clip_raster_to_polygon``. The classic chain for a river reach is
    ``fetch_nhd_area_water`` (the mapped water) -> ``section(..., between=[
    upstream, downstream])``. Do NOT use for: a drainage basin
    (``delineate_watershed``); widening a line into a polygon - a line has no
    banks, so a reach nothing maps water for has no domain here.

    A ``between`` cut is square to the line joining the two points, so the two
    end faces are the transects an inflow and an outflow are prescribed on.

    Params:
        polygon: the polygon to cut - a vector layer uri (GeoJSON, FlatGeobuf,
            shapefile) or inline GeoJSON. Non-polygon geometries are ignored.
        between: ``[(lon, lat), (lon, lat)]`` - keep what lies between the two
            perpendicular cuts at these points. Supply this OR ``within``.
        within: ``(min_lon, min_lat, max_lon, max_lat)`` - keep what lies inside
            this extent. Supply this OR ``between``.

    Returns:
        ``SectionLayerURI`` -- the sectioned polygon (GeoJSON, EPSG:4326,
        ``style_preset="section_polygon"``) with ``area_km2``,
        ``source_area_km2``, ``length_m``, ``utm_epsg``, ``parts_kept``,
        ``parts_dropped``, ``face_start`` / ``face_end`` (the two end transects a
        ``between`` cut left, which a mesh prescribes its boundary roles across),
        honest ``notes``.

    Raises:
        SectionError: ``SECTION_INPUT_INVALID`` (no cut, or both, or a malformed
            point/extent), ``SECTION_NO_POLYGON`` (the source maps no polygon),
            ``SECTION_CUT_EMPTY`` (the cut and the polygon do not meet),
            ``SECTION_SOURCE_UNREADABLE`` (the source could not be read).
    """
    from shapely.geometry import mapping
    from shapely.ops import unary_union

    if (between is None) == (within is None):
        raise SectionError(
            "SECTION_INPUT_INVALID",
            "section takes exactly one cut: 'between' two points, or 'within' "
            f"an extent; got between={between!r} within={within!r}.")

    notes: list[str] = []
    polys = _polygons(polygon)
    if not polys:
        raise SectionError(
            "SECTION_NO_POLYGON",
            f"the layer {polygon!r} carries no polygon, so there is nothing to "
            "section. Nothing here can invent one: draw the polygon, name a "
            "case layer that holds it, or section a source that maps it.")

    source_area = _area_km2(unary_union(polys))
    if between is not None:
        if not isinstance(between, (tuple, list)) or len(between) != 2:
            raise SectionError(
                "SECTION_INPUT_INVALID",
                "between must be two (lon, lat) points, the two ends of the "
                f"stretch to keep; got {between!r}.")
        start = _point(between[0], "between[0]")
        end = _point(between[1], "between[1]")
        geom, kept, dropped, chord, epsg, face_start, face_end = _cut_between(
            polys, start, end, notes)
        name = f"Section of a polygon along a {chord / 1000.0:.2f} km line"
    else:
        box = _extent(within)
        geom, kept, dropped = _cut_within(polys, box, notes)
        chord, epsg = 0.0, 0
        face_start, face_end = [], []
        name = "Section of a polygon clipped to an extent"

    area = _area_km2(geom)
    notes.append(
        f"{area:.4f} km^2 kept of the {source_area:.4f} km^2 supplied.")
    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": mapping(geom),
        "properties": {"area_km2": round(area, 6),
                       "source_area_km2": round(source_area, 6),
                       "length_m": round(chord, 2),
                       "utm_epsg": int(epsg),
                       "parts_kept": kept, "parts_dropped": dropped}}]}
    seed = uuid.uuid4().hex[:8]
    uri = _write_geojson(fc, "section", seed, _output_dir)
    minx, miny, maxx, maxy = geom.bounds
    logger.info("section: %.4f km^2 of %.4f km^2, %d part(s) kept, %d dropped",
                area, source_area, kept, dropped)
    return SectionLayerURI(
        layer_id=f"section-{seed}",
        name=name,
        layer_type="vector",
        uri=uri,
        style_preset=_STYLE_PRESET,
        role="primary",
        units="km^2",
        crs_authid="EPSG:4326",
        bbox=(float(minx), float(miny), float(maxx), float(maxy)),
        area_km2=round(area, 6),
        source_area_km2=round(source_area, 6),
        length_m=round(chord, 2),
        utm_epsg=int(epsg),
        parts_kept=kept,
        parts_dropped=dropped,
        face_start=face_start,
        face_end=face_end,
        notes=notes)
