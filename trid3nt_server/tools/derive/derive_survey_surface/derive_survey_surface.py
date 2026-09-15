"""``derive_survey_surface``: scattered measurements -> the surface between them.

Inverse-distance weighting over the nearest measurements, computed in the points'
own UTM zone so the resolution asked for is the resolution on the ground. A cell
with no measurement within the search radius is left as nodata rather than filled
from far away: an unsurveyed bank is unsurveyed, and the raster says so.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.inputs.geometry import GeometryReadError, read_geometry_doc, utm_epsg_for
from trid3nt_server.tools import register_tool
from trid3nt_server.tools.derive._hydrology_common import write_cog

__all__ = ["SurveySurfaceError", "SurveySurfaceLayerURI", "derive_survey_surface"]

logger = logging.getLogger(
    "trid3nt_server.tools.derive.derive_survey_surface.derive_survey_surface")


class SurveySurfaceError(RuntimeError):
    """A typed refusal: ``SURVEY_SURFACE_NO_POINTS``,
    ``SURVEY_SURFACE_NO_VALUE_FIELD`` (none, or several and no choice made),
    ``SURVEY_SURFACE_RESOLUTION_INVALID`` (a cell size that is not positive, or a
    grid past the cell ceiling), ``SURVEY_SURFACE_UNREADABLE``,
    ``SURVEY_SURFACE_DATUMS_DIFFER``, ``SURVEY_SURFACE_WRITE_FAILED``.
    """

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class SurveySurfaceLayerURI(LayerURI):
    """The interpolated surface, with what it was computed from and how far the
    interpolation was allowed to reach."""

    value_field: str = ""
    #: What the measurements were counted from, VERBATIM off their own rows. A
    #: survey states its own zero - a local project datum on many rivers - so a
    #: consumer reads it here and never from the dataset's spec row.
    vertical_datum: str = ""
    method: str = "idw"
    n_points: int = 0
    resolution_m: float = 0.0
    search_radius_m: float = 0.0
    filled_fraction: float = 0.0
    value_min: float | None = None
    value_max: float | None = None
    notes: list[str] = []


#: The surface is a measurement between measurements, so it draws as one.
_STYLE = {"kind": "continuous", "ramp": "ylgnbu"}

#: IDW over the K nearest measurements with distance weighted ``1/d^POWER``. Two is
#: the classical exponent for a bed: it holds the measured value at the point and
#: falls off fast enough that a distant sounding does not flatten a channel.
_NEAREST = 12
_POWER = 2.0

#: How far the interpolation reaches when the caller states no radius: a multiple of
#: the survey's own median nearest-neighbour spacing. Beyond that a cell is between
#: no measurements at all and is left nodata.
_RADIUS_SPACINGS = 3.0

#: The most cells one call will build. A ceiling refuses by name rather than
#: quietly coarsening a resolution the caller chose.
_MAX_CELLS = 40_000_000

_NODATA = float("nan")

_METADATA = AtomicToolMetadata(
    name="derive_survey_surface",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


def _features(source: Any) -> list[dict[str, Any]]:
    """The point features of a vector document, properties kept."""
    try:
        doc = read_geometry_doc(source)
    except GeometryReadError as exc:
        raise SurveySurfaceError("SURVEY_SURFACE_UNREADABLE", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - every reader fault, named by source
        raise SurveySurfaceError(
            "SURVEY_SURFACE_UNREADABLE",
            f"the sounding layer {source!r} could not be read: it is neither inline "
            f"GeoJSON nor a readable vector layer ({exc}).") from exc
    out = []
    for feature in (doc.get("features") or []):
        geometry = (feature or {}).get("geometry") or {}
        if geometry.get("type") != "Point":
            continue
        coords = geometry.get("coordinates") or []
        if len(coords) >= 2:
            out.append({"xy": (float(coords[0]), float(coords[1])),
                        "properties": feature.get("properties") or {}})
    return out


def _number(value: Any) -> float | None:
    """``value`` as a finite float, or None for anything that is not one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _value_field(features: list[dict[str, Any]], stated: str | None) -> str:
    """The property the surface is built from: the stated one, or the only numeric one."""
    if stated:
        if not any(_number((f["properties"]).get(stated)) is not None for f in features):
            raise SurveySurfaceError(
                "SURVEY_SURFACE_NO_VALUE_FIELD",
                f"no point carries a finite number under {stated!r}. The layer's "
                f"fields are {sorted({k for f in features for k in f['properties']})}.")
        return stated
    numeric = sorted({key for f in features for key, value in f["properties"].items()
                      if _number(value) is not None})
    if len(numeric) == 1:
        return numeric[0]
    raise SurveySurfaceError(
        "SURVEY_SURFACE_NO_VALUE_FIELD",
        f"the layer carries {len(numeric)} numeric fields ({numeric}), so which one "
        "is the measurement is not decidable here. Name it with value_field.")


def _datum(features: list[dict[str, Any]]) -> str:
    """The zero the measurements state on their OWN rows, or "" where none do.

    Several zeros in one layer refuse: a surface interpolated across them would
    be measured from two different places and be an elevation nowhere."""
    stated = sorted({str((f["properties"]).get("vertical_datum") or "").strip()
                     for f in features} - {""})
    if len(stated) > 1:
        raise SurveySurfaceError(
            "SURVEY_SURFACE_DATUMS_DIFFER",
            f"these points state {stated} for their vertical datum, and a single "
            "surface cannot be interpolated across two zeros. Interpolate each "
            "survey on its own datum, or shift them onto one first.")
    return stated[0] if stated else ""


def _grid(bounds: tuple[float, float, float, float], resolution_m: float) -> tuple[int, int, Any]:
    """The raster grid over the measured bounds, half a cell proud on every side."""
    from rasterio.transform import from_origin

    minx, miny, maxx, maxy = bounds
    half = resolution_m / 2.0
    minx, miny, maxx, maxy = minx - half, miny - half, maxx + half, maxy + half
    width = max(1, int(math.ceil((maxx - minx) / resolution_m)))
    height = max(1, int(math.ceil((maxy - miny) / resolution_m)))
    if width * height > _MAX_CELLS:
        raise SurveySurfaceError(
            "SURVEY_SURFACE_RESOLUTION_INVALID",
            f"a {resolution_m} m cell over these soundings is {width} x {height} = "
            f"{width * height} cells, past the {_MAX_CELLS}-cell ceiling. Ask for a "
            "coarser cell, or interpolate a smaller extent.")
    return width, height, from_origin(minx, miny + height * resolution_m, resolution_m,
                                      resolution_m)


def _idw(xy: Any, values: Any, width: int, height: int, transform: Any,
         radius_m: float) -> Any:
    """The IDW surface over the grid, nodata beyond ``radius_m`` of any measurement."""
    import numpy as np
    from scipy.spatial import cKDTree

    columns, rows = np.meshgrid(np.arange(width), np.arange(height))
    xs, ys = transform * (columns + 0.5, rows + 0.5)
    tree = cKDTree(xy)
    k = min(_NEAREST, len(values))
    distance, index = tree.query(np.column_stack([xs.ravel(), ys.ravel()]), k=k)
    if k == 1:
        distance, index = distance[:, None], index[:, None]
    # A cell standing ON a measurement takes it: the weight there is otherwise
    # infinite, and the measured value is the honest answer at its own position.
    exact = distance[:, 0] <= 0.0
    weight = 1.0 / np.maximum(distance, 1.0e-9) ** _POWER
    surface = (weight * values[index]).sum(axis=1) / weight.sum(axis=1)
    surface[exact] = values[index[exact, 0]]
    surface[distance[:, 0] > radius_m] = _NODATA
    return surface.reshape(height, width).astype("float32")


@register_tool(
    _METADATA,
    read_only_hint=False,
    # Reads only the layer it is handed and writes its own artifact.
    open_world_hint=False,
)
def derive_survey_surface(
    points: Any,
    resolution_m: float,
    value_field: str | None = None,
    max_distance_m: float | None = None,
    *,
    _output_dir: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> SurveySurfaceLayerURI:
    """Interpolate a POINT layer of measurements onto a raster surface -> a continuous grid.

    ROUTING: "grid these soundings", "turn this point survey into a surface I can
    drape a mesh on", "make a bathymetry raster from these depth points", "IDW
    these measurements at 5 m". Use it wherever scattered measurements - a
    channel survey, a set of well readings, any point layer carrying one number -
    have to become the continuous field something else samples.

    The surface is inverse-distance weighted over the twelve nearest
    measurements with weights ``1/d^2``, computed in the points' own UTM zone so
    the cell size asked for is metres on the ground. A cell with no measurement
    within the search radius is left as NODATA: the gap between two survey lines
    is interpolated, the bank a survey never crossed is not.

    Do NOT use for: resampling a raster (that is a warp), or contouring.

    Params:
        points: the measurements - a point vector layer uri or inline GeoJSON.
            Non-point rows are ignored, so a survey artifact carrying its
            footprint beside its soundings enters as it is.
        resolution_m: the cell size in metres.
        value_field: the property to interpolate. Optional only when the layer
            carries exactly one numeric field; with several, naming it is
            required rather than guessed.
        max_distance_m: how far a cell may be from the nearest measurement and
            still be filled. Default is three times the survey's own median
            point spacing, which is stated on the result.

    Returns the surface as a single-band float32 raster in the local UTM zone,
    with the field it interpolated, the vertical datum the measurements state on
    their own rows, the method, the point count, the search radius, the share of
    cells filled and the value range.
    """
    import numpy as np
    from pyproj import Transformer

    if not isinstance(resolution_m, (int, float)) or not math.isfinite(float(resolution_m)) \
            or float(resolution_m) <= 0.0:
        raise SurveySurfaceError(
            "SURVEY_SURFACE_RESOLUTION_INVALID",
            f"resolution_m must be a positive number of metres; got {resolution_m!r}.")
    resolution_m = float(resolution_m)

    features = _features(points)
    if not features:
        raise SurveySurfaceError(
            "SURVEY_SURFACE_NO_POINTS",
            f"the layer {points!r} carries no point geometry, so there are no "
            "measurements to interpolate between. Supply a point survey.")
    field = _value_field(features, value_field)
    datum = _datum(features)
    measured = [(f["xy"], _number(f["properties"].get(field))) for f in features]
    measured = [(xy, value) for xy, value in measured if value is not None]
    if len(measured) < 2:
        raise SurveySurfaceError(
            "SURVEY_SURFACE_NO_POINTS",
            f"only {len(measured)} point(s) carry a finite {field!r}, and a surface "
            "cannot be interpolated between fewer than two measurements.")

    lons = [xy[0] for xy, _ in measured]
    lats = [xy[1] for xy, _ in measured]
    epsg = utm_epsg_for(sum(lons) / len(lons), sum(lats) / len(lats))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    xs, ys = forward.transform(lons, lats)
    xy = np.column_stack([np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)])
    values = np.asarray([value for _, value in measured], dtype=float)

    from scipy.spatial import cKDTree

    spacing = float(np.median(cKDTree(xy).query(xy, k=2)[0][:, 1]))
    radius = float(max_distance_m) if max_distance_m else max(
        spacing * _RADIUS_SPACINGS, resolution_m)
    width, height, transform = _grid(
        (xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max()), resolution_m)
    surface = _idw(xy, values, width, height, transform, radius)
    filled = float(np.isfinite(surface).mean())

    seed = uuid.uuid4().hex[:8]
    uri = write_cog(surface, crs=f"EPSG:{epsg}", transform=transform,
                    prefix="survey_surface", seed=seed, output_dir=_output_dir,
                    code="SURVEY_SURFACE_WRITE_FAILED", nodata=_NODATA)
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    west, south = back.transform(transform.c, transform.f - height * resolution_m)
    east, north = back.transform(transform.c + width * resolution_m, transform.f)
    notes = [
        f"Inverse-distance weighted over the {min(_NEAREST, len(values))} nearest of "
        f"{len(values)} measurements, weights 1/d^{_POWER:g}, in EPSG:{epsg}.",
        f"Median point spacing {spacing:.2f} m; cells farther than {radius:.2f} m "
        f"from any measurement are nodata ({filled * 100.0:.1f}% of cells filled).",
        (f"The measurements are counted from {datum}." if datum
         else "The measurements state no vertical datum, so what this surface is "
              "counted from is unknown and it cannot be merged with another."),
    ]
    logger.info("derive_survey_surface: %d point(s) -> %dx%d at %.2f m, %.1f%% filled",
                len(values), width, height, resolution_m, filled * 100.0)
    return SurveySurfaceLayerURI(
        layer_id=f"soundings-{seed}",
        name=f"{field} interpolated at {resolution_m:g} m",
        layer_type="raster",
        uri=uri,
        style=_STYLE,
        role="primary",
        units=getattr(points, "units", None),
        quantity=field,
        crs_authid=f"EPSG:{epsg}",
        bbox=(float(west), float(south), float(east), float(north)),
        value_field=field,
        vertical_datum=datum,
        n_points=len(values),
        resolution_m=resolution_m,
        search_radius_m=round(radius, 3),
        filled_fraction=round(filled, 4),
        value_min=round(float(values.min()), 4),
        value_max=round(float(values.max()), 4),
        notes=notes)
