"""watershed hooks: the DEM over a buffer window and the D8 trace, as one artifact.

The window is built in metres around the pour point and held to the cell clamp
before any network call; the elevation grid is the sibling DEM fetcher's, the
delineation runs on ingestion, and the basin and the outlet it drains through
come back as the two rows of one domain artifact carrying the DEM's own uri.
"""

# The DEM is fetched THROUGH fetch_dem rather than read from 3DEP again: that tool
# owns the source, the pixel budget and the user-gated Copernicus rung, its raster
# is cached under its own prefix, and the uri it returns is what the bed over this
# basin is read from - so the grid the trace ran in and the grid the bed is draped
# from are the same bytes.
#
# The outlet run's type is the boundary slot's own constant rather than a word spelled
# here: a producer states runs in the vocabulary the slot matches on, and a copy of that
# word would drop the run silently the day the slot renamed it.
#
# A basin that reaches the window edge is a LOWER BOUND on the true catchment, so it
# refuses by name: the derive states that truncation in its own notes, and matching
# that marker is what couples this refusal to it.

from __future__ import annotations

import json
import logging
import math
import tempfile
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from trid3nt_server.inputs.boundary import RATING

from ..._fetch_common import FetchError
from ..._router.errors import router_empty_error, router_input_error, router_upstream_error
from ..._router.hooks import register_hook

logger = logging.getLogger(__name__)

__all__ = ["validate", "read"]

#: Cells per axis the window is held to. 4000 x 4000 is the 16-million-cell D8 clamp
#: the delineation declares and the DEM fetcher's own pixel budget at once, so a
#: window under it is never silently coarsened on the way in.
_MAX_PX_PER_AXIS = 4000

#: Half the outlet run, in DEM cells: the cell the catchment drains through plus
#: one either side. A catchment leaves through its channel, which the grid resolves
#: as a cell, so the face the rating is imposed over is stated in cells.
_OUTLET_HALF_CELLS = 1.5

#: The delineation's own word for a catchment that ran out of DEM. It is the only
#: channel that fact travels on, and a basin clipped by the window is not a domain.
_TRUNCATION_MARKER = "touches the DEM edge"


def _pour_point(spec: SourceSpec, params: dict[str, Any]) -> tuple[float, float]:
    """The validated ``(lon, lat)`` the basin drains to."""
    raw = params.get("pour_point")
    try:
        lon, lat = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError):
        raise router_input_error(
            spec.error_code_prefix,
            f"pour_point must be (lon, lat); got {raw!r}",
            spec.input_error_suffix,
        )
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise router_input_error(
            spec.error_code_prefix,
            f"pour_point ({lon}, {lat}) is not a lon/lat pair on this planet.",
            spec.input_error_suffix,
        )
    return lon, lat


def _window(lon: float, lat: float, buffer_km: float) -> tuple[float, float, float, float]:
    """The square window of side ``2 * buffer_km``, measured on the ellipsoid.

    Measured rather than scaled by a degrees-per-kilometre constant so the window
    is the same width in metres on both axes at every latitude, which is what makes
    the cell budget below an honest count."""
    from pyproj import Geod

    geod = Geod(ellps="WGS84")
    metres = float(buffer_km) * 1000.0
    east = geod.fwd(lon, lat, 90.0, metres)[0]
    west = geod.fwd(lon, lat, 270.0, metres)[0]
    north = geod.fwd(lon, lat, 0.0, metres)[1]
    south = geod.fwd(lon, lat, 180.0, metres)[1]
    return (min(west, east), min(south, north), max(west, east), max(south, north))


@register_hook("watershed.validate")
def validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """The pour point and the cell budget, refused pre-cache and pre-network."""
    _pour_point(spec, params)
    buffer_km = float(params.get("buffer_km") or 10.0)
    resolution_m = int(params.get("resolution_m") or 30)
    px = math.ceil(2.0 * buffer_km * 1000.0 / resolution_m)
    if px > _MAX_PX_PER_AXIS:
        coarse = math.ceil(2.0 * buffer_km * 1000.0 / _MAX_PX_PER_AXIS)
        wide = _MAX_PX_PER_AXIS * resolution_m / 2000.0
        raise router_input_error(
            spec.error_code_prefix,
            f"a {2 * buffer_km:g} km window at {resolution_m} m is {px} cells per "
            f"axis, over the {_MAX_PX_PER_AXIS}-cell budget the D8 trace is clamped "
            f"to. Ask again at resolution_m={coarse} over this window, or keep "
            f"{resolution_m} m and cut buffer_km to {wide:g}.",
            spec.input_error_suffix,
        )


def _dem(spec: SourceSpec, bbox: tuple[float, float, float, float],
         resolution_m: int, source: str) -> Any:
    """The elevation grid under the window, from the DEM fetcher that owns it.

    Its typed errors are re-raised naming THIS tool's parameter for the source, so
    the retry the DEM names is one the caller can actually make; the underlying
    code and message ride along verbatim."""
    from trid3nt_server.tools import TOOL_REGISTRY

    entry = TOOL_REGISTRY.get("fetch_dem")
    if entry is None:
        raise router_upstream_error(
            spec.error_code_prefix, "fetch_dem is not registered, so no elevation "
            "grid can be reached for this pour point")
    try:
        return entry.fn(bbox=list(bbox), resolution_m=int(resolution_m), source=str(source))
    except FetchError as exc:
        code = getattr(exc, "error_code", "UPSTREAM_ERROR")
        message = (
            f"the elevation grid under this pour point could not be fetched. "
            f"fetch_dem says ({code}): {exc} -- on this tool that source is the "
            "dem_source parameter."
        )
        if getattr(exc, "retryable", True):
            raise router_upstream_error(spec.error_code_prefix, message)
        raise router_input_error(
            spec.error_code_prefix, message, spec.input_error_suffix)


def _basin(spec: SourceSpec, pour_point: tuple[float, float], dem_uri: str,
           scratch: str) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """The D8 catchment over that grid: its geometry, its measures and its notes."""
    from trid3nt_server.tools.derive._hydrology_common import (
        HydrologyInputError,
        HydrologyPrimitivesError,
    )
    from trid3nt_server.tools.derive.delineate_watershed.delineate_watershed import (
        EmptyWatershedError,
        delineate_watershed,
    )

    sc = spec.error_code_prefix
    try:
        result = delineate_watershed(
            pour_point=pour_point, dem_uri=dem_uri, _output_dir=scratch)
    except EmptyWatershedError as exc:
        raise router_empty_error(sc, str(exc), spec.empty_error_suffix)
    except HydrologyInputError as exc:
        raise router_input_error(sc, str(exc), spec.input_error_suffix)
    except HydrologyPrimitivesError as exc:
        raise router_upstream_error(sc, f"the delineation failed: {exc}")
    notes = list(result.notes or ())
    if any(_TRUNCATION_MARKER in note for note in notes):
        raise router_input_error(
            sc,
            "the basin above this pour point runs off the edge of the window, so "
            f"its area ({result.area_km2:.3f} km^2) is a LOWER BOUND and its "
            "outline is a cut, not a divide. Raise buffer_km until the whole "
            "catchment fits and ask again.",
            "TRUNCATED",
        )
    with open(result.uri, "rb") as handle:
        feature = json.load(handle)["features"][0]
    return feature["geometry"], dict(feature["properties"]), notes


def _outlet_run(geometry: dict[str, Any], snapped: tuple[float, float],
                resolution_m: int) -> dict[str, Any]:
    """The stretch of the basin edge the water leaves through, as its two ends.

    Centred on the snapped outlet and cut to the outlet cell plus one either side:
    the traced outline merges collinear cell edges, so the segment the outlet lies
    on can run a kilometre of divide, and a rating imposed over that is a rating
    imposed on hillside. The cut is measured in metres, in the local UTM zone."""
    from pyproj import Transformer
    from shapely.geometry import Point, shape
    from shapely.ops import substring, transform as _transform

    from trid3nt_server.inputs.geometry import utm_epsg_for

    ring = shape(geometry).exterior
    epsg = utm_epsg_for(float(snapped[0]), float(snapped[1]))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    metric = _transform(forward.transform, ring)
    at = metric.project(_transform(forward.transform, Point(*snapped)))
    half = _OUTLET_HALF_CELLS * float(resolution_m)
    cut = substring(metric, max(0.0, at - half), min(metric.length, at + half))
    start, end = _transform(back.transform, cut).boundary.geoms
    return {"type": "LineString",
            "coordinates": [[float(start.x), float(start.y)],
                            [float(end.x), float(end.y)]]}


@register_hook("watershed.read")
def read(spec: SourceSpec, params: dict[str, Any], *,
         timeout_s: float) -> list[dict[str, Any]]:
    """The basin and the outlet run it drains through, over the DEM they were traced in."""
    lon, lat = _pour_point(spec, params)
    buffer_km = float(params.get("buffer_km") or 10.0)
    resolution_m = int(params.get("resolution_m") or 30)
    dem_source = str(params.get("dem_source") or "auto")
    dem = _dem(spec, _window(lon, lat, buffer_km), resolution_m, dem_source)

    with tempfile.TemporaryDirectory(prefix="trid3nt_watershed_") as scratch:
        geometry, measures, notes = _basin(spec, (lon, lat), str(dem.uri), scratch)
    snapped = (float(measures["snapped_lon"]), float(measures["snapped_lat"]))

    common = {
        "area_km2": float(measures["area_km2"]),
        "cell_count": int(measures["cell_count"]),
        "pour_point_lon": lon,
        "pour_point_lat": lat,
        "snapped_lon": snapped[0],
        "snapped_lat": snapped[1],
        "dem_uri": str(dem.uri),
        "dem_source": dem_source,
        "dem_resolution_m": resolution_m,
        "notes": " | ".join(notes),
    }
    logger.info("watershed: %.4f km^2 over a %g km window at %d m",
                common["area_km2"], 2.0 * buffer_km, resolution_m)
    return [
        {"type": "Feature", "geometry": geometry,
         "properties": {"part": "basin", "type": None, **common}},
        {"type": "Feature", "geometry": _outlet_run(geometry, snapped, resolution_m),
         "properties": {"part": "outlet", "type": RATING, **common}},
    ]
