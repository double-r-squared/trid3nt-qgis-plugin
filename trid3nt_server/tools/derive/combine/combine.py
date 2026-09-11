"""``combine``: several geometry layers -> ONE geometry document.

Nothing is computed: no union, no clip, no buffer, no reprojection beyond reading
each source into EPSG:4326. What comes back holds exactly what went in.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.inputs.geometry import (
    GeometryReadError,
    flatten_geometries,
    read_geometry_doc,
)
from trid3nt_server.tools.derive._hydrology_common import _write_geojson

__all__ = ["CombinedGeometryLayerURI", "CombineError", "combine"]

logger = logging.getLogger("trid3nt_server.tools.derive.combine.combine")


class CombineError(RuntimeError):
    """A typed combine refusal: ``COMBINE_NO_GEOMETRY`` (a named source carries no
    geometry) or ``COMBINE_SOURCE_UNREADABLE`` (not GeoJSON, not a readable layer).
    """

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class CombinedGeometryLayerURI(LayerURI):
    """The combined document's ``LayerURI``, plus counts by shape, the number of
    layers joined, and one note per source saying what it contributed.
    """

    polygon_count: int = 0
    line_count: int = 0
    point_count: int = 0
    source_count: int = 0
    notes: list[str] = []


#: The label the combined document travels under. It carries whatever the sources
#: meant, so it claims none of their semantics.
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
    """Join an extent polygon and the lines/points riding inside it into ONE layer.

    Use when a domain is a polygon PLUS the channel network a mesh should refine
    toward, or a polygon plus the points a tool must read alongside it - for the
    readers that want the two in ONE document. A mesher sizes toward a network by
    NAMING it in its own recipe instead. Do NOT use to merge polygons into a
    union (nothing is dissolved) or to clip one layer by another (``section``).

    Nothing is inferred: the document holds exactly the geometries the sources
    held, in EPSG:4326, and a source that maps nothing is refused by name.

    Params:
        polygon: the extent layer - a vector layer uri or inline GeoJSON.
            Required.
        lines: the polylines riding inside it - one uri/GeoJSON, or a list.
        points: points travelling with the domain - one uri/GeoJSON, or a list.

    Returns one FeatureCollection with counts by shape and honest notes; an
    unreadable or empty source raises CombineError.
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
