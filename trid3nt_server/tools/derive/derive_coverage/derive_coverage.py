"""``derive_coverage`` - how much of a LINE lies inside an area, as a fraction.

The area is either polygons (what a water-body source maps) or the cells of a
built mesh (what a triangulation actually holds), read off the file it arrived
in. Lengths are measured in metres, in the line's own UTM zone or the mesh's
own projected one, never in degree space. Zero is a refusal: a line and an area
that do not meet at all is a fact about the two inputs, not a measurement.
"""
from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.inputs.geometry import (
    GeometryReadError,
    flatten_geometries,
    read_geometry_doc,
    source_uri,
    utm_epsg_for,
)
from trid3nt_server.tools import register_tool

__all__ = ["CoverageError", "derive_coverage"]

logger = logging.getLogger("trid3nt_server.tools.derive.derive_coverage.derive_coverage")

#: The TIN suffixes a built mesh's display face arrives under. Anything else is
#: read as a vector document of polygons.
_MESH_SUFFIXES = (".2dm",)

_METADATA = AtomicToolMetadata(
    name="derive_coverage",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


class CoverageError(RuntimeError):
    """A typed refusal: ``DERIVE_COVERAGE_NO_LINE``, ``_NO_AREA``, ``_DISJOINT``,
    ``_UNPROJECTED`` (a mesh face with no zone named) or ``_SOURCE_UNREADABLE``."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _read(source: Any, label: str) -> dict[str, Any]:
    try:
        return read_geometry_doc(source)
    except GeometryReadError as exc:
        raise CoverageError("DERIVE_COVERAGE_SOURCE_UNREADABLE", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - every reader fault, named by source
        raise CoverageError(
            "DERIVE_COVERAGE_SOURCE_UNREADABLE",
            f"the {label} {source!r} could not be read: it is neither inline "
            f"GeoJSON nor a readable vector layer ({exc}).") from exc


def _line(source: Any) -> Any:
    """The one line the coverage is measured along, in EPSG:4326."""
    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union

    parts = [_shape(g) for g in flatten_geometries(_read(source, "line"))
             if str(g.get("type") or "") in ("LineString", "MultiLineString")]
    if not parts:
        raise CoverageError(
            "DERIVE_COVERAGE_NO_LINE",
            f"the layer {source!r} carries no polyline, so there is no line to "
            "measure coverage along. Supply a flowline, a centerline or a drawn "
            "line.")
    return unary_union(parts)


def _polygon_coverage(line_4326: Any, source: Any) -> tuple[float, float, int]:
    """(covered_m, length_m, epsg) of the line inside the polygons it is handed."""
    from pyproj import Transformer
    from shapely.geometry import shape as _shape
    from shapely.ops import transform as _transform, unary_union

    polys = [_shape(g).buffer(0) for g in flatten_geometries(_read(source, "area"))
             if str(g.get("type") or "") in ("Polygon", "MultiPolygon")]
    if not polys:
        raise CoverageError(
            "DERIVE_COVERAGE_NO_AREA",
            f"the layer {source!r} carries no polygon, so there is no area the "
            "line could lie inside. Supply mapped water, a drawn domain, or a "
            "mesh face.")
    epsg = utm_epsg_for(float(line_4326.centroid.x), float(line_4326.centroid.y))
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True).transform
    line_m = _transform(to_utm, line_4326)
    area_m = _transform(to_utm, unary_union(polys))
    if line_m.length <= 0.0:
        return (0.0, 0.0, epsg)
    return (float(line_m.intersection(area_m).length), float(line_m.length), epsg)


def _mesh_coverage(line: Any, uri: str,
                   epsg: int | None) -> tuple[float, float, int]:
    """(covered_m, length_m, epsg) of the line inside a mesh's own cells.

    Summed over the cells the line touches: a triangulation tiles its domain
    without overlap, so the per-cell lengths already add to the length inside it
    and no union of tens of thousands of triangles has to be built."""
    import numpy as np
    import shapely
    from shapely.geometry import LineString

    from trid3nt_server.workflows.mesh.shared.nodes import (
        read_accepted_mesh_nodes, read_centerline_utm,
    )

    if not epsg:
        raise CoverageError(
            "DERIVE_COVERAGE_UNPROJECTED",
            f"the mesh face {uri!r} carries its nodes in metres but names no "
            "projected zone, so the line cannot be placed on it. Pass "
            "within_epsg, which the accepted mesh states as its utm_epsg.")
    points_utm, cells, _bed, _lonlat = read_accepted_mesh_nodes(uri, utm_epsg=None)
    line_m = LineString(read_centerline_utm(line, int(epsg)))
    if line_m.length <= 0.0:
        return (0.0, 0.0, int(epsg))
    rings = np.asarray(points_utm, dtype=float)[np.asarray(cells, dtype=np.int64)]
    cell_polygons = shapely.polygons(np.concatenate([rings, rings[:, :1]], axis=1))
    touched = shapely.STRtree(cell_polygons).query(line_m, predicate="intersects")
    covered = sum(float(line_m.intersection(cell_polygons[i]).length) for i in touched)
    return (min(covered, float(line_m.length)), float(line_m.length), int(epsg))


@register_tool(
    _METADATA,
    read_only_hint=True,
    # Reads only the two sources it is handed.
    open_world_hint=False,
)
def derive_coverage(
    line: Any,
    within: Any,
    within_epsg: int | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """MEASURE how much of a line lies inside an area -> the covered fraction.

    ROUTING: "how much of this reach do the mapped water polygons cover", "how
    much of the centerline did the mesh actually hold", "what fraction of this
    line is inside that polygon". Use it wherever a line states the stretch a
    question is about and something else states where data, water or a
    triangulation reaches: the answer is about the covered stretch, and this is
    the number that says how much that is.

    Do NOT use for: the area of a polygon inside another (that is a clip), or a
    point-in-polygon test.

    Params:
        line: the line the stretch is measured along - a vector layer uri or
            inline GeoJSON.
        within: the area it is measured against - polygons (a vector layer or
            inline GeoJSON), or a built mesh's ``.2dm`` face, whose cells are
            the area.
        within_epsg: REQUIRED for a mesh face, which carries metres and no CRS:
            the projected zone its nodes are in (an accepted mesh states it as
            ``utm_epsg``). Ignored for polygons.

    Returns the ``fraction`` covered, ``covered_m``, ``length_m``, the ``of``
    kind measured against, the ``epsg`` measured in, and a note. A line and an
    area that do not meet at all refuse rather than return zero.
    """
    from trid3nt_server.workflows.runtime import journal_note

    line_4326 = _line(line)
    uri = str(source_uri(within) or "")
    if uri.lower().endswith(_MESH_SUFFIXES):
        covered, length, epsg = _mesh_coverage(line, uri, within_epsg)
        kind = "mesh cells"
    else:
        covered, length, epsg = _polygon_coverage(line_4326, within)
        kind = "polygons"
    fraction = (covered / length) if length > 0.0 else 0.0
    if fraction <= 0.0:
        raise CoverageError(
            "DERIVE_COVERAGE_DISJOINT",
            f"none of the {length:.0f} m line lies inside the {kind} it was "
            "measured against, so the two do not describe the same ground. "
            "Check that both cover the same place before reading anything off "
            "the overlap.")
    remedy = (" A finer resolution, a declared sizing function or a supplied "
              "mesh is how more of it gets held." if kind == "mesh cells" else "")
    note = (f"coverage: {fraction:.1%} of the {length / 1000.0:.2f} km line lies "
            f"inside the {kind} (measured in EPSG:{epsg}). Anything outside them "
            "carries no data from this source and is not what the answer is "
            f"about.{remedy}")
    journal_note(note)
    logger.info("derive_coverage: %.1f m of %.1f m inside %s (EPSG:%d)",
                covered, length, kind, epsg)
    return {"fraction": round(fraction, 6), "covered_m": round(covered, 2),
            "length_m": round(length, 2), "of": kind, "epsg": epsg, "note": note}
