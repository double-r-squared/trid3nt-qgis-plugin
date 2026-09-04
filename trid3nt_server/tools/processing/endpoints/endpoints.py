"""``endpoints``: a line layer -> the TWO points it begins and ends at.

The generic composition link between a fetched centerline and every tool that
takes a pair of points. A reach is named by its flowline, and the cut that turns
mapped banks into that reach is taken BETWEEN two points - so the two facts are
joined by measuring the line rather than by asking somebody to click twice at
coordinates the line already states.

Nothing is inferred: the points are vertices OF the supplied line, in its own
vertex order. A source whose parts do not join into one continuous line is
refused, because which two of several loose ends were meant is not measurable.
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

__all__ = ["EndpointsError", "EndpointsLayerURI", "endpoints"]

logger = logging.getLogger("trid3nt_server.tools.processing.endpoints.endpoints")


class EndpointsError(RuntimeError):
    """A typed endpoints refusal: an error code plus what to supply instead.

    Codes:
    - ``ENDPOINTS_NO_LINE`` -- the source carries no polyline geometry.
    - ``ENDPOINTS_NOT_CONTINUOUS`` -- the parts do not join into one line, so the
      two ends are not measurable.
    - ``ENDPOINTS_SOURCE_UNREADABLE`` -- the source could not be read.
    """

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class EndpointsLayerURI(LayerURI):
    """The two end points as a point layer, plus the pair a cut is taken between.

    Extra fields beyond ``LayerURI``: ``between`` (``[[lon, lat], [lon, lat]]`` -
    the pair, ready for ``section(between=...)``), ``start`` / ``end`` (the same
    two points, named in the line's own vertex order), ``length_m`` (the line
    measured in its local UTM zone), ``part_count`` (how many parts were joined),
    ``notes``.
    """

    between: list[list[float]] = []
    start: tuple[float, float] | None = None
    end: tuple[float, float] | None = None
    length_m: float = 0.0
    part_count: int = 0
    notes: list[str] = []


#: The label the two points travel under: ends the tool MEASURED off a line,
#: whose meaning is whatever the line meant.
_STYLE = {"kind": "reference", "geometry": "point"}

_ENDPOINTS_METADATA = AtomicToolMetadata(
    name="endpoints",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


def _lines(source: Any) -> list[Any]:
    """The polyline geometries the ends are measured off, in EPSG:4326."""
    from shapely.geometry import shape as _shape

    try:
        geoms = flatten_geometries(read_geometry_doc(source))
    except GeometryReadError as exc:
        raise EndpointsError("ENDPOINTS_SOURCE_UNREADABLE", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - every reader fault, named by source
        raise EndpointsError(
            "ENDPOINTS_SOURCE_UNREADABLE",
            f"the line source {source!r} could not be read: it is neither inline "
            f"GeoJSON nor a readable vector layer ({exc}).") from exc
    out: list[Any] = []
    for geometry in geoms:
        if str(geometry.get("type") or "") not in ("LineString", "MultiLineString"):
            continue
        shape = _shape(geometry)
        for part in getattr(shape, "geoms", [shape]):
            if not part.is_empty:
                out.append(part)
    return out


def _merged(parts: list[Any], notes: list[str]) -> Any:
    """The parts joined into ONE line, or a refusal naming how many they stayed."""
    from shapely.ops import linemerge

    if len(parts) == 1:
        return parts[0]
    merged = linemerge(parts)
    pieces = list(getattr(merged, "geoms", [merged]))
    if len(pieces) != 1:
        raise EndpointsError(
            "ENDPOINTS_NOT_CONTINUOUS",
            f"the {len(parts)} line part(s) supplied join into {len(pieces)} "
            "separate lines, so which two of their loose ends are THE two ends is "
            "not measurable. Supply one continuous line - a navigated flowline "
            "rather than a whole river network.")
    notes.append(f"{len(parts)} line parts joined into one continuous line.")
    return pieces[0]


def _length_m(line: Any) -> tuple[float, int]:
    """The line's length in metres, measured in its own UTM zone -> (length, epsg)."""
    from pyproj import Transformer
    from shapely.ops import transform as _transform

    from trid3nt_server.tools.processing._geometry_common import utm_epsg_for

    epsg = utm_epsg_for(float(line.centroid.x), float(line.centroid.y))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    return float(_transform(forward.transform, line).length), int(epsg)


@register_tool(
    _ENDPOINTS_METADATA,
    read_only_hint=False,
    # Reads only the line it is handed and writes its own artifact.
    open_world_hint=False,
)
def endpoints(
    line: str,
    *,
    _output_dir: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> EndpointsLayerURI:
    """Take the TWO END POINTS of a line layer -> a point layer plus the pair itself.

    Use this when a downstream tool wants two points and a LINE already states
    them: the classic chain is ``fetch_nhdplus_nldi_navigate`` (the navigated
    flowline) -> ``endpoints`` -> ``section(polygon=<mapped banks>,
    between=<these two points>)``, which cuts the banks square to the reach at its
    upstream and downstream ends. Do NOT use for: every vertex of a line
    (``compute_cross_section`` and the playground read geometry directly), or the
    ends of a whole river NETWORK - a network has more than two.

    Params:
        line: the line to measure - a vector layer uri (GeoJSON, FlatGeobuf,
            shapefile) or inline GeoJSON. Several parts are joined first; parts
            that do not join into one continuous line are refused.

    Returns:
        ``EndpointsLayerURI`` -- the two points as a GeoJSON point layer
        (EPSG:4326) with ``between`` (the pair,
        as ``section`` takes it), ``start``, ``end``, ``length_m``,
        ``part_count`` and honest ``notes``.

    Raises:
        EndpointsError: ``ENDPOINTS_NO_LINE`` (the source maps no polyline),
            ``ENDPOINTS_NOT_CONTINUOUS`` (the parts stay separate),
            ``ENDPOINTS_SOURCE_UNREADABLE`` (the source could not be read).
    """
    notes: list[str] = []
    parts = _lines(line)
    if not parts:
        raise EndpointsError(
            "ENDPOINTS_NO_LINE",
            f"the layer {line!r} carries no polyline, so there is no line to take "
            "the ends of. Supply a flowline, a centerline, or a drawn line.")
    merged = _merged(parts, notes)
    coords = list(merged.coords)
    start = (float(coords[0][0]), float(coords[0][1]))
    end = (float(coords[-1][0]), float(coords[-1][1]))
    length, epsg = _length_m(merged)
    notes.append(
        f"Ends taken in the line's own vertex order over {length:.1f} m, measured "
        f"in EPSG:{epsg}.")

    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [point[0], point[1]]},
         "properties": {"position": label, "length_m": round(length, 2)}}
        for label, point in (("start", start), ("end", end))]}
    seed = uuid.uuid4().hex[:8]
    uri = _write_geojson(fc, "endpoints", seed, _output_dir)
    lons, lats = (start[0], end[0]), (start[1], end[1])
    logger.info("endpoints: %d part(s) -> (%.5f,%.5f) .. (%.5f,%.5f) over %.1f m",
                len(parts), start[0], start[1], end[0], end[1], length)
    return EndpointsLayerURI(
        layer_id=f"endpoints-{seed}",
        name=f"End points of a {length / 1000.0:.2f} km line",
        layer_type="vector",
        uri=uri,
        style=_STYLE,
        role="primary",
        units="m",
        crs_authid="EPSG:4326",
        bbox=(min(lons), min(lats), max(lons), max(lats)),
        between=[[start[0], start[1]], [end[0], end[1]]],
        start=start,
        end=end,
        length_m=round(length, 2),
        part_count=len(parts),
        notes=notes)
