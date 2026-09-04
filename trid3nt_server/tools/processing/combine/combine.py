"""``combine``: several geometry layers -> ONE geometry document.

The generic composition link. A domain is often more than one shape: an extent
polygon says where the model stops, and the polylines riding inside it say what
the interior is sized toward. Both facts come out of separate tools, and a
consumer that took them as two arguments could be handed a sizing network for an
extent it does not describe - so they are joined into one document HERE, by an
explicit call somebody wrote, and travel as one thing afterwards.

Nothing is computed: no union, no clip, no buffer, no reprojection beyond reading
each source into EPSG:4326. What comes back holds exactly the geometries that
went in, which is what makes this composable rather than an opinion.
"""

from __future__ import annotations

import logging
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

__all__ = ["CombinedGeometryLayerURI", "CombineError", "combine"]

logger = logging.getLogger("trid3nt_server.tools.processing.combine.combine")


class CombineError(RuntimeError):
    """A typed combine refusal: an error code plus what to supply instead.

    Codes:
    - ``COMBINE_NO_GEOMETRY`` -- a named source carries no geometry at all.
    - ``COMBINE_SOURCE_UNREADABLE`` -- a source is neither inline GeoJSON nor a
      readable vector layer.
    """

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class CombinedGeometryLayerURI(LayerURI):
    """The combined document's ``LayerURI`` plus what went into it.

    Extra fields beyond ``LayerURI``: ``polygon_count`` / ``line_count`` /
    ``point_count`` (what the document holds, by shape), ``source_count`` (how
    many layers were joined), ``notes`` (what each source contributed).
    """

    polygon_count: int = 0
    line_count: int = 0
    point_count: int = 0
    source_count: int = 0
    notes: list[str] = []


#: The label the combined document travels under. It carries whatever the sources
#: meant, so it claims none of their semantics - the same reason ``section``
#: names its own preset rather than borrowing the polygon's.
_STYLE = {"kind": "reference"}

_COMBINE_METADATA = AtomicToolMetadata(
    name="combine",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


def _sources(value: Any) -> list[Any]:
    """One source or several -> the list. A bare layer is one source, not a sequence."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for v in value if v is not None]
    return [value]


def _read(source: Any, label: str, notes: list[str]) -> list[dict[str, Any]]:
    """One source -> its geometries, or a refusal naming which source failed."""
    try:
        geoms = flatten_geometries(read_geometry_doc(source))
    except GeometryReadError as exc:
        raise CombineError("COMBINE_SOURCE_UNREADABLE", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - every reader fault, named by source
        raise CombineError(
            "COMBINE_SOURCE_UNREADABLE",
            f"the {label} {source!r} could not be read: it is neither inline "
            f"GeoJSON nor a readable vector layer ({exc}).") from exc
    if not geoms:
        raise CombineError(
            "COMBINE_NO_GEOMETRY",
            f"the {label} {source!r} carries no geometry, so there is nothing of "
            "it to combine. Supply a layer that maps something, or leave the slot "
            "empty.")
    notes.append(f"{len(geoms)} geometry/geometries taken from the {label}.")
    return geoms


def _bounds(geoms: list[dict[str, Any]]) -> tuple[float, float, float, float]:
    from shapely.geometry import shape as _shape
    from shapely.ops import unary_union

    minx, miny, maxx, maxy = unary_union([_shape(g) for g in geoms]).bounds
    return (float(minx), float(miny), float(maxx), float(maxy))


@register_tool(
    _COMBINE_METADATA,
    read_only_hint=False,
    # Reads only the layers it is handed and writes its own artifact.
    open_world_hint=False,
)
def combine(
    polygon: str,
    lines: Any = None,
    points: Any = None,
    *,
    _output_dir: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> CombinedGeometryLayerURI:
    """Join an extent polygon and the lines/points riding inside it into ONE geometry layer.

    Use this when: a domain is a polygon PLUS the channel network the mesh should
    refine toward, or a polygon plus the points a tool has to read alongside it.
    A mesh sizes itself toward a channel network by NAMING it in its recipe
    (``mesh_op('distance_sizing_from_line_function', line_file=<the lines>)``),
    so use this for the readers that want the two in ONE document rather than to
    hand a mesher a domain and its sizing source folded together. Do NOT use
    for: merging two polygons into their union (nothing is dissolved here), or
    clipping one layer by another (``section``).

    Nothing is inferred: the document holds exactly the geometries the sources
    held, in EPSG:4326, and a source that maps nothing is refused by name rather
    than dropped.

    Params:
        polygon: the extent layer - a vector layer uri (GeoJSON, FlatGeobuf,
            shapefile) or inline GeoJSON. Required: a combined domain with no
            polygon has no interior anything downstream can use.
        lines: the polylines riding inside it - one layer uri/GeoJSON, or a list
            of them. Optional.
        points: points that travel with the domain - one layer uri/GeoJSON, or a
            list of them. Optional.

    Returns:
        ``CombinedGeometryLayerURI`` -- one GeoJSON FeatureCollection (EPSG:4326) with ``polygon_count``,
        ``line_count``, ``point_count``, ``source_count`` and honest ``notes``.

    Raises:
        CombineError: ``COMBINE_NO_GEOMETRY`` (a named source maps nothing),
            ``COMBINE_SOURCE_UNREADABLE`` (a source could not be read).
    """
    notes: list[str] = []
    geoms = _read(polygon, "polygon", notes)
    sources = 1
    for source in _sources(lines):
        geoms.extend(_read(source, "lines", notes))
        sources += 1
    for source in _sources(points):
        geoms.extend(_read(source, "points", notes))
        sources += 1

    kinds = [str(g.get("type") or "") for g in geoms]
    polygons = sum(k in ("Polygon", "MultiPolygon") for k in kinds)
    linestrings = sum(k in ("LineString", "MultiLineString") for k in kinds)
    pts = sum(k in ("Point", "MultiPoint") for k in kinds)
    fc = {"type": "FeatureCollection",
          "features": [{"type": "Feature", "geometry": g, "properties": {}}
                       for g in geoms]}
    seed = uuid.uuid4().hex[:8]
    uri = _write_geojson(fc, "combined", seed, _output_dir)
    minx, miny, maxx, maxy = _bounds(geoms)
    logger.info("combine: %d source(s) -> %d polygon(s), %d line(s), %d point(s)",
                sources, polygons, linestrings, pts)
    return CombinedGeometryLayerURI(
        layer_id=f"combined-{seed}",
        name=f"Combined geometry - {polygons} polygon(s), {linestrings} line(s)",
        layer_type="vector",
        uri=uri,
        style=_STYLE,
        role="primary",
        crs_authid="EPSG:4326",
        bbox=(minx, miny, maxx, maxy),
        polygon_count=polygons,
        line_count=linestrings,
        point_count=pts,
        source_count=sources,
        notes=notes)
