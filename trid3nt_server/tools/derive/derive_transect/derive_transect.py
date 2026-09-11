"""``derive_transect``: a shape -> the ONE line through its centroid along a bearing.

The line is laid in the shape's own UTM zone, so its length is metres on the
ground and its bearing is a direction there; nothing about what the shape IS is
read, only where it sits.
"""
from __future__ import annotations

import logging
import math
import uuid
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.derive._hydrology_common import _write_geojson
from trid3nt_server.workflows.inputs.geometry import flatten_geometries, utm_epsg_for
from trid3nt_server.workflows.inputs.shape import shape as _ingest
from trid3nt_server.workflows.runtime.user_input import UserInputError

__all__ = ["TransectError", "TransectLayerURI", "derive_transect"]

logger = logging.getLogger("trid3nt_server.tools.derive.derive_transect.derive_transect")


class TransectError(RuntimeError):
    """A typed refusal: ``TRANSECT_INPUT_INVALID`` (the length, the bearing or
    the convention), ``TRANSECT_NO_SHAPE`` (nothing to centre on),
    ``TRANSECT_SOURCE_UNREADABLE``."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class TransectLayerURI(LayerURI):
    """The transect as a line layer, with the centre it passes through, the
    bearing it runs on in the convention it was stated in, its length and the
    UTM zone it was laid in."""

    centre: tuple[float, float] | None = None
    bearing_deg: float = 0.0
    convention: str = "compass"
    length_m: float = 0.0
    utm_epsg: int = 0


#: The two ways a direction is stated: a compass bearing clockwise from north,
#: or a trigonometric angle counter-clockwise from east.
_CONVENTIONS = ("compass", "trig")

#: The label the line travels under: a line the tool LAID, whose meaning is
#: whatever the shape it was centred on meant.
_STYLE = {"kind": "reference", "geometry": "line"}

_TRANSECT_METADATA = AtomicToolMetadata(
    name="derive_transect",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


def _geometries(source: Any) -> list[Any]:
    """Every non-empty geometry the shape carries, in EPSG:4326, through the
    one Shape ingestion: a layer, inline GeoJSON, a drawn or typed line set."""
    from shapely.geometry import shape as _shape

    try:
        shp = _ingest(source, label="shape", code="TRANSECT_SOURCE_UNREADABLE")
    except UserInputError as exc:
        raise TransectError("TRANSECT_SOURCE_UNREADABLE", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - every reader fault, named by source
        raise TransectError(
            "TRANSECT_SOURCE_UNREADABLE",
            f"the shape {source!r} could not be read: it is neither inline "
            f"GeoJSON, a readable vector layer nor a line set ({exc}).") from exc
    if shp is None:
        return []
    geoms = [_shape(g) for g in flatten_geometries(shp.features)]
    return [g for g in geoms if g is not None and not g.is_empty]


def _direction(bearing_deg: float, convention: str) -> tuple[float, float]:
    """The unit vector a stated direction points along, in metres east and north."""
    if convention not in _CONVENTIONS:
        raise TransectError(
            "TRANSECT_INPUT_INVALID",
            f"convention must be one of {list(_CONVENTIONS)}; got {convention!r}.")
    angle = math.radians(float(bearing_deg))
    if convention == "compass":
        return math.sin(angle), math.cos(angle)
    return math.cos(angle), math.sin(angle)


@register_tool(
    _TRANSECT_METADATA,
    read_only_hint=False,
    # Reads only the shape it is handed and writes its own artifact.
    open_world_hint=False,
)
def derive_transect(
    shape: Any,
    bearing_deg: float,
    length_m: float,
    convention: str = "compass",
    *,
    _output_dir: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> TransectLayerURI:
    """Lay ONE straight line through the centroid of a shape, along a bearing -> a line layer.

    Use this when a profile has to be read ACROSS a feature rather than along
    the domain's own axis: the agitation behind a breakwater along the incident
    wave, a cross-section through a bar along the flow. The line is centred on
    the shape's centroid and runs ``length_m`` in total, half each way, so it
    crosses the feature and reaches both sides of it. Hand the result to a
    profile read (``profile(name, along=<this uri>)``) or draw it. Do NOT use
    for the ends of a line (``endpoints``) or for cutting a polygon (``section``).

    Params:
        shape: the shape to centre on - a vector layer uri (GeoJSON, FlatGeobuf,
            shapefile), inline GeoJSON, or a drawn line; every geometry it
            carries counts toward the centroid.
        bearing_deg: the direction the line runs along, from its first vertex
            to its last.
        length_m: the line's whole length in metres, on the ground.
        convention: how ``bearing_deg`` is stated - ``compass`` (clockwise from
            north, the default) or ``trig`` (counter-clockwise from east, the
            convention a wave-propagation direction is stated in).

    Returns the line as a GeoJSON layer with its centre, its bearing, its length
    and the UTM zone it was laid in.
    """
    from pyproj import Transformer
    from shapely.ops import transform as _transform, unary_union

    if not (float(length_m) > 0.0):
        raise TransectError(
            "TRANSECT_INPUT_INVALID",
            f"length_m must be a positive distance in metres; got {length_m!r}.")
    geoms = _geometries(shape)
    if not geoms:
        raise TransectError(
            "TRANSECT_NO_SHAPE",
            f"the shape {shape!r} carries no geometry, so there is nothing to "
            "centre a transect on. Supply a layer or a drawn line.")
    ex, ny = _direction(bearing_deg, convention)
    centre_ll = unary_union(geoms).centroid
    epsg = utm_epsg_for(float(centre_ll.x), float(centre_ll.y))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    cx, cy = forward.transform(float(centre_ll.x), float(centre_ll.y))
    half = float(length_m) / 2.0
    start = back.transform(cx - half * ex, cy - half * ny)
    end = back.transform(cx + half * ex, cy + half * ny)
    line = [[float(start[0]), float(start[1])], [float(end[0]), float(end[1])]]

    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": {"type": "LineString", "coordinates": line},
        "properties": {"bearing_deg": float(bearing_deg), "convention": convention,
                       "length_m": round(float(length_m), 2), "utm_epsg": int(epsg)}}]}
    seed = uuid.uuid4().hex[:8]
    uri = _write_geojson(fc, "transect", seed, _output_dir)
    lons, lats = (line[0][0], line[1][0]), (line[0][1], line[1][1])
    logger.info("transect: %.0f m through (%.5f, %.5f) along %g deg %s in EPSG:%d",
                float(length_m), centre_ll.x, centre_ll.y, float(bearing_deg),
                convention, epsg)
    return TransectLayerURI(
        layer_id=f"transect-{seed}",
        name=f"Transect of {float(length_m) / 1000.0:.2f} km along {float(bearing_deg):g} deg",
        layer_type="vector",
        uri=uri,
        style=_STYLE,
        role="primary",
        units="m",
        crs_authid="EPSG:4326",
        bbox=(min(lons), min(lats), max(lons), max(lats)),
        centre=(float(centre_ll.x), float(centre_ll.y)),
        bearing_deg=float(bearing_deg),
        convention=convention,
        length_m=round(float(length_m), 2),
        utm_epsg=int(epsg))
