"""THE BED: what every node of the domain carries for elevation.

One slot over every source a user can have: a fetched DEM, a fetched or supplied
bathymetry raster, a survey raster, a layer of soundings, or a stated depth below
the free surface. The slot takes ONE source - a measurement over a wider surface
is composed by the merge derive in the DATA body, before anything reaches here -
says WHICH shape it was handed, and nothing above here branches on where it came
from. One thing happens on the way in, because only the slot knows the bed is an
ELEVATION: the surface is read on the RUN's own vertical frame, and a surface of
depths is turned into elevations counted up from the zero it states.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any, Mapping

from trid3nt_contracts.execution import LayerURI, layer_seed

from .user_input import UserInputError

logger = logging.getLogger(__name__)

__all__ = ["Bed", "DEPTH", "MERGE_DERIVE", "MergeRastersError",
           "MergedRasterLayerURI", "POINTS", "RASTER", "SURVEY_DERIVE",
           "SurveySurfaceError", "SurveySurfaceLayerURI", "bed", "elevations",
           "merged_surface", "survey_surface"]

_CODE = "BED_INVALID"

#: The three shapes an elevation source arrives in.
RASTER = "raster"
POINTS = "points"
DEPTH = "depth"

#: The runner the runtime calls the grid below by. The rule is the bed's own -
#: only this slot needs a surface between soundings - so it is reached at its
#: own address here rather than through a registered name a model could pick.
SURVEY_DERIVE = "trid3nt_server.inputs.bed.survey_surface"

#: The runner the runtime calls the merge below by, at its own address the same
#: way: composing one bed out of a measurement and a wider surface is what this
#: slot is for, and no other slot takes two rasters.
MERGE_DERIVE = "trid3nt_server.inputs.bed.merged_surface"

#: What a surface of DEPTHS names its quantity as. A depth is counted DOWN from
#: the survey's own zero, and a bed is an elevation counted UP from the run's,
#: so a source stating this reaches the mesh only through the flip below.
_DEPTH_QUANTITY = "depth_below_datum"

#: How deep a stated depth may be. A bed stated as a depth below the free
#: surface is a flat bottom, and a number outside this is not one.
_DEPTH_RANGE_M = (0.0, 12000.0)


@dataclass(frozen=True, slots=True)
class Bed:
    """One elevation source: which shape it is, and the thing itself.

    ``source`` is the raster or point layer; ``depth_m`` is set only on a stated
    depth, where there is no artifact at all."""

    kind: str
    source: Any = None
    depth_m: float | None = None
    #: The RUN's vertical frame, and the measured shift onto it - carried only on
    #: a POINTS bed, whose surface does not exist until the mesh scale is known.
    frame: Any = None
    offset: Any = None

    @property
    def is_depth(self) -> bool:
        return self.kind == DEPTH


def bed(value: Any, *, frame: Any = None, offset: Any = None,
        label: str = "bed", code: str = _CODE) -> Bed | None:
    """THE ingestion: a raster, a sounding layer, or a depth in metres -> Bed.

    ``None`` only when nothing came. A number is a DEPTH below the free surface;
    a vector artifact is a point survey; everything else is a surface. ``frame``
    is the RUN's vertical frame, which the runtime states once and every
    elevation here is read on, and ``offset`` is the measured shift onto it the
    runtime's own DATA row produced where the source states another frame."""
    if isinstance(value, Bed):
        return value
    if value is None:
        return None
    depth = _depth(value)
    if depth is not None:
        lo, hi = _DEPTH_RANGE_M
        if not (lo <= depth <= hi):
            raise UserInputError(
                f"the {label} was stated as {depth} m below the free surface, "
                f"which is outside {lo}-{hi} m. State the depth the water body "
                "actually holds, or supply a surveyed bed.", code=code)
        return Bed(kind=DEPTH, depth_m=depth)
    if isinstance(value, Mapping) and "depth_m" in value:
        return bed(value["depth_m"], label=label, code=code)
    from trid3nt_server.workflows.runtime.data import artifact_class

    # A source that arrives as GeoJSON is already READ - a slot's op converted it
    # on the way in - and a geometry document is a survey, never a surface.
    points = (isinstance(value, Mapping) and "type" in value) \
        or artifact_class(value) == "vector"
    if points:
        # A layer of soundings is not a surface yet, so the read onto the run's
        # frame waits for the derive that makes one: the frame and the shift
        # ride here until the mesh interpolates at its own scale.
        return Bed(kind=POINTS, source=value, frame=frame, offset=offset)
    return Bed(kind=RASTER,
               source=elevations(value, frame=frame, offset=offset, label=label,
                                 code=code))


def _depth(value: Any) -> float | None:
    """``value`` as a stated depth, or ``None`` when it is not a number.

    A bool is not a depth: it is an int in Python and nothing states a bed as
    one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def elevations(layer: Any, *, frame: Any, offset: Any = None,
               label: str = "bed", code: str = _CODE) -> Any:
    """One surface on the RUN's vertical frame, counted UP.

    The flip is the bed slot's, because only the slot knows a bed is an
    elevation; the SHIFT onto the run's frame is the vertical-frame coercion's,
    the one every elevation the run ingests goes through. A source whose zero
    reaches the run's frame through nothing refuses by name - inventing a shift
    would be indistinguishable from a measurement downstream."""
    from .vertical_datum import DatumError, datum_of, onto_frame

    depths = str(getattr(layer, "quantity", "") or "").startswith(_DEPTH_QUANTITY)
    zero = datum_of(layer)
    if depths and not zero:
        raise UserInputError(
            f"the {label} is a surface of DEPTHS and states no zero they are "
            "counted below, so nothing can read them as the elevations a bed "
            "carries. Name a survey whose own metadata publishes its datum, or "
            "supply a bed already measured as elevations.", code=code)
    if not zero:
        # A SURFACE SOMEBODY HANDED THIS RUN states no zero of its own and stands
        # on the run's frame: checking it against a datum nobody wrote down would
        # refuse every bed a user surveyed for this question.
        return layer
    try:
        aligned = onto_frame(layer, frame, offset=offset, code_prefix="BED_")
    except DatumError as exc:
        # The refusal already NAMES this surface, its zero and the run's; saying
        # it again here is one fact stated twice, and the two can disagree.
        raise UserInputError(str(exc), code=code) from exc
    if not (depths or aligned.shift_m):
        return layer
    _journal(label, layer, aligned, depths)
    return _flipped(layer, aligned.shift_m, aligned.datum or zero, label, code,
                    depths=depths)


def _journal(label: str, layer: Any, aligned: Any, depths: bool) -> None:
    """What the run SAYS about the bed it ingested: the flip, and the shift.

    On the run's journal and not in a log line - the shift a bed was moved by is
    part of the answer, and a reader of the packet has to see it."""
    from trid3nt_server.workflows.runtime import journal_note
    from .vertical_datum import datum_of

    zero = datum_of(layer) or "its own datum"
    counted = (f"the {label} is a surface of DEPTHS below {zero}, read as "
               f"elevations counted up from it" if depths else
               f"the {label} is a surface of elevations on {zero}")
    moved = (f"and it is {aligned.note}" if aligned.shift_m
             else f"and {zero} IS this run's frame")
    journal_note(f"{counted}, {moved}.")


def _on_the_frame(values: Any, offset_m: float, *, depths: bool) -> Any:
    """One grid read on another zero: ``offset - depth``, or ``value + offset``.

    A depth is counted DOWN from the zero it states and an elevation UP from the
    frame it lands on, so the two are one axis only through the flip. It happens
    on the source's OWN grid, before anything reads it onto another, so the cells
    that land carry the measurement's own values re-zeroed."""
    import numpy as np

    return (np.float32(offset_m) - values if depths
            else values + np.float32(offset_m))


def _flipped(layer: Any, offset_m: float, frame: str, label: str,
             code: str, *, depths: bool) -> Any:
    """The same grid on the run's frame: ``offset - depth``, or ``value +
    offset``. One raster, written once."""
    import tempfile

    import numpy as np
    import rasterio
    from trid3nt_server.inputs.geometry import source_uri
    from trid3nt_server.tools.derive._hydrology_common import (
        _stage_uri_local, write_cog)

    uri = str(source_uri(layer) or "").strip()
    if not uri:
        raise UserInputError(
            f"the {label} names no raster file to read ({layer!r}).", code=code)
    seed = layer_seed()
    with tempfile.TemporaryDirectory(prefix="bed-elevation-") as scratch:
        with rasterio.open(_stage_uri_local(uri, scratch, "bed")) as src:
            read = src.read(1, masked=True).filled(np.nan).astype("float32")
            crs, transform = src.crs, src.transform
        values = _on_the_frame(read, offset_m, depths=depths)
        written = write_cog(values, crs=crs,
                            transform=transform, prefix="bed_elevation",
                            seed=seed, output_dir=None,
                            code="BED_ELEVATION_WRITE_FAILED",
                            nodata=float("nan"))
    return LayerURI.published(
        "bed-elevation", seed=seed,
        name=f"{getattr(layer, 'name', None) or 'survey'} as elevations",
        layer_type="raster", uri=written,
        style=getattr(layer, "style", None), role="primary", units="m",
        quantity="bed_elevation_m", bbox=getattr(layer, "bbox", None),
        crs_authid=getattr(layer, "crs_authid", None), vertical_datum=frame)


# THE SURVEY GRID: scattered soundings -> the surface a mesh samples.


class SurveySurfaceError(UserInputError):
    """The survey grid's typed refusal: ``SURVEY_SURFACE_NO_POINTS``,
    ``SURVEY_SURFACE_NO_VALUE_FIELD`` (none, or several and no choice made),
    ``SURVEY_SURFACE_RESOLUTION_INVALID`` (a cell size that is not positive, or a
    grid past the cell ceiling), ``SURVEY_SURFACE_FOOTPRINT_EMPTY`` (a cell so
    coarse that no cell centre falls inside the footprint),
    ``SURVEY_SURFACE_UNREADABLE``,
    ``SURVEY_SURFACE_DATUMS_DIFFER``, ``SURVEY_SURFACE_WRITE_FAILED``.
    """


class SurveySurfaceLayerURI(LayerURI):
    """The interpolated surface, with what it was computed from and how far the
    interpolation was allowed to reach."""

    value_field: str = ""
    #: What the measurements were counted from, VERBATIM off their own rows. A
    #: survey states its own zero - a local project datum on many rivers - so a
    #: consumer reads it here and never from the dataset's spec row.
    vertical_datum: str = ""
    #: The shift the measurements' own rows publish between that zero and a
    #: national frame, carried through unapplied: this surface is still counted
    #: from the survey's datum, and whoever puts it on another frame says so.
    datum_offset_m: float | None = None
    datum_offset_frame: str = ""
    method: str = "idw"
    n_points: int = 0
    resolution_m: float = 0.0
    search_radius_m: float = 0.0
    #: The share of the grid inside the FOOTPRINT: what this survey measured,
    #: and what the merge below it may call measured. The rest is nodata.
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

#: THE FOOTPRINT RULE, as a multiple of the survey's own median nearest-neighbour
#: spacing: the surface is valid inside the union of discs of that reach around the
#: soundings and nodata outside. The reach belongs to the MEASUREMENTS - a coarser
#: output grid never widens it, because how far a sounding speaks for the ground
#: around it is not a property of the raster it is drawn on.
_RADIUS_SPACINGS = 3.0

#: The most cells one call will build. A ceiling refuses by name rather than
#: quietly coarsening a resolution the caller chose.
_MAX_CELLS = 40_000_000

_NODATA = float("nan")



def _features(source: Any) -> list[dict[str, Any]]:
    """The point features of a vector document, properties kept."""
    from .geometry import GeometryReadError, read_geometry_doc

    try:
        doc = read_geometry_doc(source)
    except GeometryReadError as exc:
        raise SurveySurfaceError(
            str(exc),
            code="SURVEY_SURFACE_UNREADABLE") from exc
    except Exception as exc:  # noqa: BLE001 - every reader fault, named by source
        raise SurveySurfaceError(
            f"the sounding layer {source!r} could not be read: it is neither inline "
            f"GeoJSON nor a readable vector layer ({exc}).",
            code="SURVEY_SURFACE_UNREADABLE") from exc
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


#: Numeric properties this derive reads as METADATA about the zero rather than as
#: measurements. A survey that publishes its own shift carries it on every row,
#: and offering it as a candidate measurement would make an unnamed call
#: undecidable on exactly the layers the shift exists for.
_ABOUT_THE_DATUM = frozenset({"datum_offset_m"})


def _value_field(features: list[dict[str, Any]], stated: str | None) -> str:
    """The property the surface is built from: the stated one, or the only numeric one."""
    if stated:
        if not any(_number((f["properties"]).get(stated)) is not None for f in features):
            raise SurveySurfaceError(
                f"no point carries a finite number under {stated!r}. The layer's "
                f"fields are {sorted({k for f in features for k in f['properties']})}.",
                code="SURVEY_SURFACE_NO_VALUE_FIELD")
        return stated
    numeric = sorted({key for f in features for key, value in f["properties"].items()
                      if _number(value) is not None} - _ABOUT_THE_DATUM)
    if len(numeric) == 1:
        return numeric[0]
    raise SurveySurfaceError(
        f"the layer carries {len(numeric)} numeric fields ({numeric}), so which one "
        "is the measurement is not decidable here. Name it with value_field.",
        code="SURVEY_SURFACE_NO_VALUE_FIELD")


def _datum(features: list[dict[str, Any]]) -> str:
    """The zero the measurements state on their OWN rows, or "" where none do.

    Several zeros in one layer refuse: a surface interpolated across them would
    be measured from two different places and be an elevation nowhere."""
    stated = sorted({str((f["properties"]).get("vertical_datum") or "").strip()
                     for f in features} - {""})
    if len(stated) > 1:
        raise SurveySurfaceError(
            f"these points state {stated} for their vertical datum, and a single "
            "surface cannot be interpolated across two zeros. Interpolate each "
            "survey on its own datum, or shift them onto one first.",
            code="SURVEY_SURFACE_DATUMS_DIFFER")
    return stated[0] if stated else ""


def _published_shift(features: list[dict[str, Any]]
                     ) -> tuple[float | None, str, str]:
    """The offset the measurements' own rows publish, the frame it reaches, and
    what a reader has to know about it.

    A project datum SLOPES: two surveys of one river publish the shift onto a
    national frame at their own river miles, and a surface laid across both is
    read through their mean with the spread between them stated. Two different
    FRAMES is another matter and refuses - one surface reaches one frame."""
    published = {(_number((f["properties"]).get("datum_offset_m")),
                  str((f["properties"]).get("datum_offset_frame") or "").strip())
                 for f in features}
    stated = sorted(row for row in published if row[0] is not None and row[1])
    frames = sorted({frame for _metres, frame in stated})
    if len(frames) > 1:
        raise SurveySurfaceError(
            f"these points publish shifts onto {frames}, and one surface reaches "
            "one frame. Interpolate each survey on its own.",
            code="SURVEY_SURFACE_DATUMS_DIFFER")
    if not stated:
        return None, "", ""
    metres = [value for value, _frame in stated]
    mean = round(sum(metres) / len(metres), 4)
    if len(metres) == 1:
        return mean, frames[0], ""
    return mean, frames[0], (
        f"{len(metres)} published shifts onto {frames[0]} - "
        f"{', '.join(f'{v:+.4f}' for v in metres)} m, {max(metres) - min(metres):.4f} "
        f"m apart, which is a project datum sloping along the water - so this "
        f"surface is read through their mean {mean:+.4f} m.")


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
            f"a {resolution_m} m cell over these soundings is {width} x {height} = "
            f"{width * height} cells, past the {_MAX_CELLS}-cell ceiling. Ask for a "
            "coarser cell, or interpolate a smaller extent.",
            code="SURVEY_SURFACE_RESOLUTION_INVALID")
    return width, height, from_origin(minx, miny + height * resolution_m, resolution_m,
                                      resolution_m)


def _idw(xy: Any, values: Any, width: int, height: int, transform: Any,
         radius_m: float) -> Any:
    """The IDW surface over the grid, nodata outside the soundings' footprint."""
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
    # THE FOOTPRINT: a cell whose centre is farther than the reach from every
    # sounding is outside what this survey measured, and nothing is invented there.
    surface[distance[:, 0] > radius_m] = _NODATA
    return surface.reshape(height, width).astype("float32")


def survey_surface(
    points: Any,
    resolution_m: float,
    value_field: str | None = None,
    max_distance_m: float | None = None,
    *,
    _output_dir: str | None = None,
) -> "SurveySurfaceLayerURI | None":
    """Interpolate a POINT layer of measurements onto a raster surface -> a continuous grid.

    ROUTING: "grid these soundings", "turn this point survey into a surface I can
    drape a mesh on", "make a bathymetry raster from these depth points", "IDW
    these measurements at 5 m". Use it wherever scattered measurements - a
    channel survey, a set of well readings, any point layer carrying one number -
    have to become the continuous field something else samples.

    The surface is inverse-distance weighted over the twelve nearest
    measurements with weights ``1/d^2``, computed in the points' own UTM zone so
    the cell size asked for is metres on the ground. It is valid only over the
    FOOTPRINT the soundings measured - what lies within the survey's own reach of
    one of them - and NODATA outside: the gap between two survey lines is
    interpolated, the bank a survey never crossed is not. That mask is what a
    merge below reads to say which cells a measurement painted.

    Do NOT use for: resampling a raster (that is a warp), or contouring.

    Params:
        points: the measurements - a point vector layer uri or inline GeoJSON.
            Non-point rows are ignored, so a survey artifact carrying its
            footprint beside its soundings enters as it is. ABSENT - a survey
            row whose source held nothing - there is no surface and the answer
            is nothing, which is what a caller composing over it reads.
        resolution_m: the cell size in metres.
        value_field: the property to interpolate. Optional only when the layer
            carries exactly one numeric field; with several, naming it is
            required rather than guessed.
        max_distance_m: THE REACH - how far a cell may be from the nearest
            measurement and still be inside the footprint. Default is three
            times the survey's own median point spacing, which is stated on the
            result; a coarse ``resolution_m`` never widens it, and a cell so
            coarse that no cell centre falls inside the footprint refuses.

    Returns the surface as a single-band float32 raster in the local UTM zone,
    with the field it interpolated, the vertical datum the measurements state on
    their own rows, any shift those rows publish onto a national frame (carried
    through UNAPPLIED - this surface is still counted from the survey's own
    zero), the method, the point count, the search radius, the share of cells
    filled and the value range. ``None`` where no soundings were handed over.
    """
    import numpy as np
    from pyproj import Transformer

    from .geometry import utm_epsg_for
    from trid3nt_server.tools.derive._hydrology_common import write_cog

    if points is None:
        logger.info("survey surface: no soundings were handed over")
        return None
    if not isinstance(resolution_m, (int, float)) or not math.isfinite(float(resolution_m)) \
            or float(resolution_m) <= 0.0:
        raise SurveySurfaceError(
            f"resolution_m must be a positive number of metres; got {resolution_m!r}.",
            code="SURVEY_SURFACE_RESOLUTION_INVALID")
    resolution_m = float(resolution_m)

    features = _features(points)
    if not features:
        raise SurveySurfaceError(
            f"the layer {points!r} carries no point geometry, so there are no "
            "measurements to interpolate between. Supply a point survey.",
            code="SURVEY_SURFACE_NO_POINTS")
    field = _value_field(features, value_field)
    datum = _datum(features)
    offset_m, offset_frame, offset_note = _published_shift(features)
    measured = [(f["xy"], _number(f["properties"].get(field))) for f in features]
    measured = [(xy, value) for xy, value in measured if value is not None]
    if len(measured) < 2:
        raise SurveySurfaceError(
            f"only {len(measured)} point(s) carry a finite {field!r}, and a surface "
            "cannot be interpolated between fewer than two measurements.",
            code="SURVEY_SURFACE_NO_POINTS")

    lons = [xy[0] for xy, _ in measured]
    lats = [xy[1] for xy, _ in measured]
    epsg = utm_epsg_for(sum(lons) / len(lons), sum(lats) / len(lats))
    forward = Transformer.from_crs(4326, epsg, always_xy=True)
    xs, ys = forward.transform(lons, lats)
    xy = np.column_stack([np.asarray(xs, dtype=float), np.asarray(ys, dtype=float)])
    values = np.asarray([value for _, value in measured], dtype=float)

    from scipy.spatial import cKDTree

    spacing = float(np.median(cKDTree(xy).query(xy, k=2)[0][:, 1]))
    radius = float(max_distance_m) if max_distance_m else spacing * _RADIUS_SPACINGS
    width, height, transform = _grid(
        (xy[:, 0].min(), xy[:, 1].min(), xy[:, 0].max(), xy[:, 1].max()), resolution_m)
    surface = _idw(xy, values, width, height, transform, radius)
    inside = int(np.isfinite(surface).sum())
    if not inside:
        raise SurveySurfaceError(
            f"no cell centre of a {resolution_m:g} m grid falls within {radius:.2f} m "
            f"of a sounding, so this survey measures none of it and a surface here "
            "would be interpolation over ground nobody sounded. Ask for a cell at or "
            "below the survey's own reach, or state max_distance_m if these "
            "measurements speak for the ground farther than their spacing suggests.",
            code="SURVEY_SURFACE_FOOTPRINT_EMPTY")
    filled = float(inside) / float(width * height)
    footprint_km2 = inside * resolution_m * resolution_m / 1.0e6

    seed = layer_seed()
    uri = write_cog(surface, crs=f"EPSG:{epsg}", transform=transform,
                    prefix="survey_surface", seed=seed, output_dir=_output_dir,
                    code="SURVEY_SURFACE_WRITE_FAILED", nodata=_NODATA)
    back = Transformer.from_crs(epsg, 4326, always_xy=True)
    west, south = back.transform(transform.c, transform.f - height * resolution_m)
    east, north = back.transform(transform.c + width * resolution_m, transform.f)
    notes = [
        f"Inverse-distance weighted over the {min(_NEAREST, len(values))} nearest of "
        f"{len(values)} measurements, weights 1/d^{_POWER:g}, in EPSG:{epsg}.",
        f"Median point spacing {spacing:.2f} m. The FOOTPRINT is what lies within "
        f"{radius:.2f} m of a sounding - {footprint_km2:.4f} km2, "
        f"{filled * 100.0:.1f}% of the grid this surface spans - and every cell "
        f"outside it is nodata, measured by nothing.",
        (f"The measurements are counted from {datum}." if datum
         else "The measurements state no vertical datum, so what this surface is "
              "counted from is unknown and it cannot be merged with another."),
        *([offset_note or f"Their own rows put that zero {offset_m:+.4f} m on "
           f"{offset_frame}."] if offset_m is not None else []),
        *(["The shift is carried through UNAPPLIED: these are still depths below "
           "the survey's own zero."] if offset_m is not None else []),
    ]
    logger.info("survey surface: %d point(s) -> %dx%d at %.2f m, %.1f%% filled",
                len(values), width, height, resolution_m, filled * 100.0)
    return SurveySurfaceLayerURI.published(
        "soundings", seed=seed,
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
        datum_offset_m=offset_m,
        datum_offset_frame=offset_frame,
        n_points=len(values),
        resolution_m=resolution_m,
        search_radius_m=round(radius, 3),
        filled_fraction=round(filled, 4),
        value_min=round(float(values.min()), 4),
        value_max=round(float(values.max()), 4),
        notes=notes)


# THE MERGE: a measurement over part of the ground, a wider surface under the rest.


class MergeRastersError(UserInputError):
    """The merge's typed refusal: ``MERGE_RASTERS_NO_SOURCE``,
    ``MERGE_RASTERS_UNREADABLE``, ``MERGE_RASTERS_DISJOINT`` (the two cover no
    common ground), ``MERGE_BED_CLIFF`` (the painted values split into two
    populations farther apart than the domain's own relief),
    ``MERGE_RASTERS_RESOLUTION_INVALID`` (a grid past the cell
    ceiling), ``MERGE_RASTERS_WRITE_FAILED``. ``MERGE_RASTERS_DATUM_UNSTATED``,
    ``MERGE_RASTERS_DATUMS_DIFFER`` and ``MERGE_RASTERS_DATUM_OFFSET_MISMATCH``
    come from the datum check itself.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message, code=error_code)


class MergedRasterLayerURI(LayerURI):
    """The merged surface, with WHAT PAINTED IT rather than what was offered."""

    #: The share of the merged cells the MEASUREMENTS painted between them, and
    #: the share the wider surface under all of them painted.
    primary_fraction: float = 0.0
    fallback_fraction: float = 0.0
    #: The share of the WATER - the cells inside the polygon the domain was cut
    #: with - that no rung of this ladder measured. ``None`` where the merge was
    #: handed no polygon, because a bed with no water to be inside claims
    #: nothing about it. It is the number a reader weighs before asking for a
    #: surface between the measurements, and the shares above are over the whole
    #: grid rather than over the water, so neither of them answers it.
    unmeasured_water_fraction: float | None = None
    #: What each RUNG of the ladder painted, in rank order: the name the rung
    #: carries and its share of the merged cells. The provenance sidecar below
    #: writes a cell's rung as its index into this list.
    rungs: list[tuple[str, float]] = []
    #: The single-band raster carrying which rung won at each cell, by its rank -
    #: 0 the top rung, then each rung that painted what the ones above it left -
    #: and nodata where none of them measured. A sidecar rather than a second
    #: band, so the surface stays the one-band grid every sampler reads, and the
    #: mesh reads it to say which source painted each node.
    provenance_uri: str | None = None
    resolution_m: float = 0.0
    #: The metres added to the top rung and to the wider surface to read each on
    #: the frame the merge landed on, zero wherever it already counted from it.
    #: Every rung's own shift is on the notes, said in the words a reader needs.
    datum_shift_m: float = 0.0
    fallback_shift_m: float = 0.0
    notes: list[str] = []

    def input_row(self) -> dict[str, Any]:
        """What the raster publishing seam takes to put this surface on the map.

        Stated by the surface itself, because the ledger carries the row onto
        the merge's record: a REPLAYED merge publishes the bed a fresh one
        published, rather than leaving a resumed run with nothing to see."""
        painted = ", ".join(f"{label} {share * 100.0:.1f}%"
                            for label, share in self.rungs)
        said = [f"merged: {painted}"] if painted else []
        if self.unmeasured_water_fraction is not None:
            said.append(f"{self.unmeasured_water_fraction * 100.0:.1f}% of the "
                        "water measured by nothing")
        if self.vertical_datum:
            said.append(f"datum {self.vertical_datum}")
        return {"cog_uri": self.uri, "layer_id": f"input-{self.layer_id}",
                "name": f"Input: bed ({', '.join(said)})" if said
                        else "Input: bed",
                "style": self.style}


#: The merged surface is an elevation in metres, so it draws as one: the merge
#: lands each rung on the run's own frame, whatever unit the rung arrived in.
_BED_STYLE = {"kind": "continuous", "ramp": "terrain", "units": "m"}

#: The surfacing tasks in flight, held because a bare ``create_task`` reference
#: is the loop's only claim on the coroutine and a dropped one is collectable.
_publishing: set[Any] = set()

#: The most cells one merge will build. A ceiling refuses by name rather than
#: quietly coarsening the measurement the merge was called to keep.
_MERGE_MAX_CELLS = 60_000_000

_PROVENANCE_NODATA = 255


def _staged(layer: Any, role: str, scratch: str) -> str:
    """One rung's raster, local and readable, or a refusal naming it."""
    from trid3nt_server.tools.derive._hydrology_common import _stage_uri_local

    from .geometry import source_uri

    uri = str(source_uri(layer) or "").strip()
    if not uri:
        raise MergeRastersError(
            "MERGE_RASTERS_NO_SOURCE",
            f"the {role} {layer!r} names no raster file to read.")
    try:
        # The role is a rung's own name and reaches a FILENAME here, so what is
        # not a word in it is not carried into one.
        return _stage_uri_local(
            uri, scratch, "".join(c if c.isalnum() else "_" for c in role))
    except Exception as exc:  # noqa: BLE001 - every reader fault, named by source
        raise MergeRastersError(
            "MERGE_RASTERS_UNREADABLE",
            f"the {role} raster {uri!r} could not be read ({exc}).") from exc


def _listed(value: Any) -> list[Any]:
    """One argument as the list of things it names, in the order it names them."""
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _rungs(surfaces: Any, offsets: Any) -> list[tuple[Any, Any]]:
    """The surfaces one argument names, in rank order, each with its own offset.

    A caller states one surface and the one offset row declared for it, or a
    ranked list and the rows declared for each; the two are paired by rank, and
    a rung the caller named no row for owes none."""
    shifts = _listed(offsets)
    return [(layer, shifts[rank] if rank < len(shifts) else None)
            for rank, layer in enumerate(_listed(surfaces))]


def _rung_label(layer: Any, rank: int) -> str:
    """What the merge CALLS one rung: the name it carries, else its rank."""
    return str(getattr(layer, "name", "") or "").strip() or f"rung {rank + 1}"


def _metres_per_unit(crs: Any) -> float:
    """How many metres one unit of this CRS spans, for a degree grid or a metre
    one. A projected CRS in feet is not one either of these sources ships."""
    return 1.0 if crs is not None and crs.is_projected else 111_320.0


def _common_grid(sources: list[Any], resolution_m: float | None
                 ) -> tuple[Any, int, int, Any, float]:
    """The one grid both inputs are read onto: the CRS and cell of the finest
    source, over the union of what they cover."""
    from rasterio.transform import from_origin
    from rasterio.warp import transform_bounds

    finest = min(sources, key=lambda src: min(abs(v) for v in src.res)
                 * _metres_per_unit(src.crs))
    crs = finest.crs
    cell = (float(resolution_m) / _metres_per_unit(crs) if resolution_m
            else min(abs(v) for v in finest.res))
    if not math.isfinite(cell) or cell <= 0.0:
        raise MergeRastersError(
            "MERGE_RASTERS_RESOLUTION_INVALID",
            f"resolution_m must be a positive number of metres; got {resolution_m!r}.")
    boxes = [transform_bounds(src.crs, crs, *src.bounds, densify_pts=21)
             for src in sources]
    west = min(b[0] for b in boxes)
    south = min(b[1] for b in boxes)
    east = max(b[2] for b in boxes)
    north = max(b[3] for b in boxes)
    width = max(1, int(math.ceil((east - west) / cell)))
    height = max(1, int(math.ceil((north - south) / cell)))
    if width * height > _MERGE_MAX_CELLS:
        raise MergeRastersError(
            "MERGE_RASTERS_RESOLUTION_INVALID",
            f"merging these two over their union at {cell * _metres_per_unit(crs):.3g} m "
            f"is {width} x {height} = {width * height} cells, past the "
            f"{_MERGE_MAX_CELLS}-cell ceiling. State a coarser resolution_m, or merge "
            "surfaces that cover less ground.")
    return crs, width, height, from_origin(west, north, cell, cell), cell


def _warped(src: Any, crs: Any, width: int, height: int, transform: Any,
            values: Any, scratch: str, role: str, never: Any = None
            ) -> tuple[Any, str]:
    """ONE source on the common grid, nodata where it measured nothing.

    GDAL's own warp, through the binding the daemon links, and the result is
    both read back for the provenance and left on disk for the overlay below.
    ``values`` is that source's grid already re-zeroed, which is what the caller
    passes where the two surfaces did not count from one zero. ``never`` is the
    cells this rung may not paint whatever it holds there - a terrain surface
    measures the water TOP, so inside the water it is not a bed and the rungs
    that measured the bottom are the only ones that speak for it."""
    import os

    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    out = np.full((height, width), _NODATA, dtype="float32")
    reproject(source=(rasterio.band(src, 1) if values is None else values),
              destination=out,
              src_transform=src.transform, src_crs=src.crs,
              dst_transform=transform, dst_crs=crs,
              src_nodata=(src.nodata if values is None else _NODATA),
              dst_nodata=_NODATA,
              resampling=Resampling.bilinear)
    if never is not None:
        out[never] = _NODATA
    path = os.path.join(scratch, f"{role}_on_the_grid.tif")
    with rasterio.open(path, "w", driver="GTiff", height=height, width=width,
                       count=1, dtype="float32", crs=crs, transform=transform,
                       nodata=_NODATA, tiled=True) as destination:
        destination.write(out, 1)
    return out, path


def _counts_down(layer: Any) -> bool:
    """Does this surface state its values as DEPTHS below its own zero?"""
    return str(getattr(layer, "quantity", "") or "").startswith(_DEPTH_QUANTITY)


def _merge_frame(under: Any, frame: Any) -> str:
    """The zero this merge lands on: the RUN's, else the LAST rung's own.

    A run states its frame and every elevation it ingests is read onto it. A
    merge called outside one lands on the bottom rung, which paints every cell
    the rungs above it left, so nothing it already holds moves."""
    from .vertical_datum import datum_of

    return str(frame or "").strip() or datum_of(under)


def _placed(ladder: list[tuple[Any, Any]], labels: list[str], zero: str
            ) -> list[tuple[int, Any]]:
    """Every rung that can be READ on the frame this merge lands on, by rank,
    each with what it costs to read there.

    A rung whose zero nothing measures against that frame is not a rung of this
    bed: it drops off the way an empty one does, the journal says which and why,
    and the rungs under it paint the cells it would have. The TOP rung is the
    exception - a bed whose best source cannot be placed is not that bed - and
    what is left still has to paint the grid, which the overlay below refuses on."""
    from trid3nt_server.workflows.runtime import journal_note

    from .vertical_datum import datum_of

    standing: list[tuple[int, Any]] = []
    for rank, (layer, row) in enumerate(ladder):
        try:
            standing.append((rank, _merge_aligned(layer, zero, row)))
        except MergeRastersError:
            if rank == 0:
                raise
            line = (f"{labels[rank]} counts from "
                    f"{datum_of(layer) or 'no stated zero'} and nothing measures "
                    f"that against {zero}, so it drops off the ladder and the "
                    "rungs under it paint what it would have.")
            logger.info("bed merge: %s", line)
            journal_note(line)
    return standing


def _merge_aligned(source: Any, frame: str, offset: Any) -> Any:
    """What it costs to read ONE rung of the ladder on the frame it lands on.

    Every rung is read onto that zero before any of them paints a cell, so the
    overlay is over one axis. An offset the call does not state is the one the
    source
    publishes about itself - a survey measured on a district's project datum
    states in its own metadata how far that zero sits above a national frame,
    and no service serves that datum - and a pair nothing measures refuses
    naming both. A frame nothing named leaves nothing to land on, which is the
    unstated zero refusing by name."""
    from .vertical_datum import DatumError, one_datum, onto_frame

    try:
        if not frame:
            one_datum(source, code_prefix="MERGE_RASTERS_")
        return onto_frame(source, frame, offset=offset,
                          code_prefix="MERGE_RASTERS_")
    except DatumError as exc:
        raise MergeRastersError(exc.error_code, str(exc)) from exc


def _read_as(layer: Any, role: str, aligned: Any, *, depths: bool) -> str:
    """The sentence a run SAYS about a rung this merge moved onto its frame, or
    "" where that rung already stood on it.

    The packet's note and the journal line are one statement, said once."""
    from .vertical_datum import datum_of

    if not (depths or aligned.shift_m):
        return ""
    zero = datum_of(layer) or "its own datum"
    counted = (f"The {role} is a surface of DEPTHS below {zero}, read as "
               f"elevations counted up from it" if depths else
               f"The {role} is a surface of elevations on {zero}")
    return f"{counted}, and it is {aligned.note}."


def _rezeroed(source: Any, aligned: Any, *, depths: bool) -> Any:
    """One rung's own grid read on the frame the merge lands on, or ``None``
    where nothing moved it.

    On its OWN grid, before anything reads it onto the common one, so the cells
    that land carry the measurement's values already re-zeroed."""
    if not (depths or aligned.shift_m):
        return None
    return _on_the_frame(
        source.read(1, masked=True).filled(_NODATA).astype("float32"),
        aligned.shift_m, depths=depths)


def _water_cells(water: Any, crs: Any, transform: Any, width: int, height: int
                 ) -> Any:
    """The cells of the common grid INSIDE the polygon the domain was cut with,
    or ``None`` where that polygon reaches none of them.

    The cut is the run's statement of where the water is, and a bed is only
    asked to measure under it. The polygon is read in the frame the domain
    publishes it in - lon/lat - and put on this grid's own CRS here, because the
    grid is the finest rung's and no caller knows which rung that was."""
    import numpy as np
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_geom

    from .geometry import flatten_geometries, read_geometry_doc

    shapes = [g for g in flatten_geometries(read_geometry_doc(water))
              if str(g.get("type")) in ("Polygon", "MultiPolygon")]
    if not shapes:
        raise MergeRastersError(
            "MERGE_RASTERS_NO_SOURCE",
            f"the water this bed is merged under ({water!r}) carries no polygon, "
            "so there is no inside for the terrain to stay out of. Hand the cut "
            "the domain was made with, or merge without one.")
    inside = geometry_mask(
        [transform_geom("EPSG:4326", crs, shape) for shape in shapes],
        out_shape=(height, width), transform=transform, invert=True)
    return inside if int(np.count_nonzero(inside)) else None


def _bbox_4326(crs: Any, transform: Any, width: int, height: int
               ) -> tuple[float, float, float, float]:
    """The lon/lat box the merged grid spans, so the camera can fly to it."""
    from rasterio.warp import transform_bounds

    west, north = transform.c, transform.f
    east = west + width * transform.a
    south = north + height * transform.e
    return tuple(float(v) for v in transform_bounds(
        crs, "EPSG:4326", west, south, east, north, densify_pts=21))


def _passed_through(only: Any, absent: str) -> MergedRasterLayerURI:
    """The one surface there is, carried through under the merge's own shape.

    An absent row is an answer - a reach with no published survey has a terrain
    bed and nothing else - and the result says which side was missing rather
    than presenting one input as a merge of two."""
    from .geometry import source_uri

    uri = str(source_uri(only) or "").strip()
    if not uri:
        raise MergeRastersError(
            "MERGE_RASTERS_NO_SOURCE",
            f"only the {'fallback' if absent == 'primary' else 'primary'} "
            f"surface was given and it names no raster file to read ({only!r}).")
    seed = layer_seed()
    note = (f"The {absent} surface was absent, so the other one passed through "
            "unchanged: this is that surface, not a merge of two.")
    logger.info("bed merge: %s absent, %s passed through", absent, uri)
    return MergedRasterLayerURI.published(
        "merged-bed", seed=seed,
        name=getattr(only, "name", None) or "merged bed surface",
        layer_type="raster",
        uri=uri,
        style=getattr(only, "style", None) or _BED_STYLE,
        role="primary",
        units=getattr(only, "units", None),
        quantity=getattr(only, "quantity", None),
        bbox=getattr(only, "bbox", None),
        vertical_datum=getattr(only, "vertical_datum", None),
        primary_fraction=0.0 if absent == "primary" else 1.0,
        fallback_fraction=1.0 if absent == "primary" else 0.0,
        resolution_m=0.0,
        notes=[note])


def _surfaced(merged: MergedRasterLayerURI) -> None:
    """Put the merged bed on the map as an input row, so the surface the mesh is
    painted from is SEEN rather than inferred from a survey's outline.

    Best-effort on the emitter the run is bracketed by: a bed nobody can see is a
    poorer run, never a failed one."""
    try:
        from trid3nt_server.render.layer_uri_emit import publish_raster_input_cog
        from trid3nt_server.render.pipeline_emitter import current_emitter

        emitter = current_emitter()
        if emitter is None:
            return
        coro = publish_raster_input_cog(emitter, **merged.input_row())
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coro)
            return
        task = loop.create_task(coro)
        _publishing.add(task)
        task.add_done_callback(_publishing.discard)
    except Exception as exc:  # noqa: BLE001 - surfacing never voids a merge
        logger.warning("merged bed not surfaced as an input layer: %s", exc)


def _band(spans: list[tuple[float, float, str]]) -> str:
    """One population of painted values: what it spans, and which rungs painted it."""
    return (f"{min(low for low, _high, _label in spans):.2f} to "
            f"{max(high for _low, high, _label in spans):.2f} m from "
            f"{', '.join(label for _low, _high, label in spans)}")


def _no_cliff(grids: list[Any], won: Any, labels: list[str]) -> None:
    """REFUSE a merged bed whose painted values split into two populations.

    The rungs paint ONE landscape, so what they paint sits within that
    landscape's own relief - the range of the surface under all of them. A step
    wider than the whole of it, a hundred metres on a reach that falls ten, is
    two zeros that never met rather than a bed, and every node is painted, so
    nothing below catches it: a mesh takes the cliff and the run opens a river in
    a column of air."""
    import numpy as np

    under = grids[-1][np.isfinite(grids[-1])]
    relief = float(under.max() - under.min()) if under.size else 0.0
    spans = []
    for rank, grid in enumerate(grids):
        values = grid[won == rank]
        values = values[np.isfinite(values)]
        if values.size:
            spans.append((float(values.min()), float(values.max()), labels[rank]))
    if relief <= 0.0 or len(spans) < 2:
        return
    spans.sort()
    for split in range(1, len(spans)):
        gap = spans[split][0] - max(high for _low, high, _label in spans[:split])
        if gap <= relief:
            continue
        raise MergeRastersError(
            "MERGE_BED_CLIFF",
            f"the merged bed splits into two populations {gap:.1f} m apart, more "
            f"than the {relief:.1f} m of relief {labels[-1]} measures over this "
            f"domain: {_band(spans[:split])}, and {_band(spans[split:])}. No "
            "ground holds that step - the rungs were read onto zeros that never "
            "met. Name the offset row each rung is read through, or name a rung "
            "measured on the frame this bed lands on.")


def merged_surface(
    primary: Any = None,
    fallback: Any = None,
    resolution_m: float | None = None,
    frame: Any = None,
    primary_offset: Any = None,
    fallback_offset: Any = None,
    water: Any = None,
    *,
    _output_dir: str | None = None,
) -> MergedRasterLayerURI:
    """MERGE a ranked LADDER of surfaces into one bed, each rung winning where it measured.

    ``primary`` is the measurement, or the ranked list of measurements highest
    rung first; ``fallback`` is the wider surface under all of them, and each
    offset is the row declared for the rung of the same rank. A rung paints only
    the cells the rungs above it left, because a domain reaching past the top
    pick is what the next rung is for. Every rung is re-zeroed onto the run's
    vertical frame by the flip above - through its own offset row, else the shift
    it publishes about itself - before any of them is read onto the common grid,
    so the overlay is over one axis; one whose zero nothing measures against that
    frame drops off the ladder as an empty rung does, and only the TOP rung being
    unplaceable refuses. They then land on one grid at the finest of
    their cell sizes over the union of what they cover, and the OVERLAY is the
    substrate's own merge over those single-source warps with priority by order -
    nothing here re-implements it. Which rung won at each cell is rebuilt from the
    same warps and written as a sidecar, because the mesh records which source
    painted each node.

    ``water`` is the polygon the domain was CUT with. Inside it the fallback
    measures the water top rather than the bottom, so it paints only OUTSIDE it
    and water no measured rung reached is left unpainted; the share of the water
    measured by nothing is stated on the result and on the journal, which is the
    number a reader weighs before asking for a surface between the measurements.
    """
    import tempfile
    from contextlib import ExitStack

    import numpy as np
    import rasterio
    from rasterio.merge import merge

    from trid3nt_server.tools.derive._hydrology_common import write_cog
    from trid3nt_server.workflows.runtime import journal_note

    offered = _rungs(primary, primary_offset)
    ladder = offered + _rungs(fallback, fallback_offset)
    if not ladder:
        raise MergeRastersError(
            "MERGE_RASTERS_NO_SOURCE",
            "a bed merge was given neither surface, so there is nothing to merge.")
    if len(ladder) == 1:
        return _passed_through(ladder[0][0],
                               absent="fallback" if offered else "primary")
    zero = _merge_frame(ladder[-1][0], frame)
    labels = [_rung_label(layer, rank) for rank, (layer, _row) in enumerate(ladder)]
    standing = _placed(ladder, labels, zero)
    measured = sum(1 for rank, _cost in standing if rank < len(offered))
    aligned = [cost for _rank, cost in standing]
    labels = [labels[rank] for rank, _cost in standing]
    ladder = [ladder[rank] for rank, _cost in standing]
    if len(ladder) == 1:
        return _passed_through(ladder[0][0],
                               absent="fallback" if measured else "primary")
    counts_down = [_counts_down(layer) for layer, _row in ladder]

    seed = layer_seed()
    with tempfile.TemporaryDirectory(prefix="bed-merge-") as scratch:
        staged = [_staged(layer, labels[rank], scratch)
                  for rank, (layer, _row) in enumerate(ladder)]
        with ExitStack() as opened:
            surfaces = [opened.enter_context(rasterio.open(path))
                        for path in staged]
            crs, width, height, transform, cell = _common_grid(surfaces, resolution_m)
            wet = (_water_cells(water, crs, transform, width, height)
                   if water is not None else None)
            grids, paths = [], []
            for rank, src in enumerate(surfaces):
                values, path = _warped(
                    src, crs, width, height, transform,
                    _rezeroed(src, aligned[rank], depths=counts_down[rank]),
                    scratch, f"rung{rank}",
                    None if rank < measured else wet)
                grids.append(values)
                paths.append(path)
        # BOTTOM UP, so the rung with the best claim to a cell is the last to
        # write it: the ladder's order IS the priority the overlay below applies.
        won = np.full(grids[0].shape, _PROVENANCE_NODATA, dtype="uint8")
        for rank in range(len(grids) - 1, -1, -1):
            won[np.isfinite(grids[rank])] = rank
        if not int((won != _PROVENANCE_NODATA).sum()):
            raise MergeRastersError(
                "MERGE_RASTERS_DISJOINT",
                f"none of the {len(ladder)} surfaces tried ({', '.join(labels)}) "
                "measured a single cell of the grid they span together, so there "
                "is nothing to merge. Name surfaces over the same ground.")
        _no_cliff(grids, won, labels)
        west, north = transform.c, transform.f
        stack, _ = merge(paths, method="first",
                         bounds=(west, north + height * transform.e,
                                 west + width * transform.a, north),
                         res=(cell, cell))
        metres = float(cell * _metres_per_unit(crs))
        uri = write_cog(stack[0], crs=crs, transform=transform, prefix="merged_bed",
                        seed=seed, output_dir=_output_dir,
                        code="MERGE_RASTERS_WRITE_FAILED", nodata=_NODATA)
        provenance = write_cog(won, crs=crs, transform=transform,
                               prefix="merged_bed_source", seed=seed,
                               output_dir=_output_dir,
                               code="MERGE_RASTERS_WRITE_FAILED",
                               nodata=float(_PROVENANCE_NODATA))

    shares = [float((won == rank).sum()) / float(won.size)
              for rank in range(len(ladder))]
    moved = [line for line in
             (_read_as(layer, labels[rank], aligned[rank], depths=counts_down[rank])
              for rank, (layer, _row) in enumerate(ladder)) if line]
    painted = "; ".join(f"{label} {share * 100.0:.1f}%"
                        for label, share in zip(labels, shares))
    # ONE NUMBER PER RUNG, in rank order: a reader asking what measured this bed
    # is asking about each rung, and a pair of fractions over a ladder of three
    # is an answer to a question nobody asked.
    covered = (f"The ladder painted the merged grid in rank order - {painted} - "
               f"and {(1.0 - sum(shares)) * 100.0:.1f}% is measured by none of "
               "them and left as nodata.")
    unmeasured = (round(float((won[wet] == _PROVENANCE_NODATA).sum())
                        / float(wet.sum()), 4) if wet is not None else None)
    # THE ONE NUMBER A READER WEIGHS before asking for a surface between the
    # measurements, so it is said in the same breath as the rule that produced
    # it rather than left to be read off two shares over a different denominator.
    wet_said = (["Only the measured rungs paint inside the polygon the domain "
                 "was cut with - a wider surface measures the water TOP, not "
                 f"the bed - and {unmeasured * 100.0:.1f}% of the water inside "
                 "it is measured by nothing."]
                if unmeasured is not None else [])
    notes = [
        covered,
        *wet_said,
        f"Merged at {metres:.3g} m in {crs}, the finest of the rungs unless a "
        "resolution was stated.",
        *(moved or [f"Every surface counts from {zero}."]),
    ]
    logger.info("bed merge: %dx%d at %.3g m over %d rungs - %s",
                width, height, metres, len(ladder), painted)
    for line in (covered, *wet_said, *moved):
        journal_note(line)
    merged = MergedRasterLayerURI.published(
        "merged-bed", seed=seed,
        name="merged bed surface",
        layer_type="raster",
        uri=uri,
        style=_BED_STYLE,
        role="primary",
        units=next((getattr(layer, "units", None) for layer, _row in ladder
                    if getattr(layer, "units", None)), None),
        # A rung read the other way round no longer carries the quantity it
        # named, so the merged surface states the one it was read onto.
        quantity=next((getattr(layer, "quantity", None)
                       for rank, (layer, _row) in enumerate(ladder)
                       if not counts_down[rank] and getattr(layer, "quantity", None)),
                      None),
        bbox=_bbox_4326(crs, transform, width, height),
        vertical_datum=zero or None,
        datum_shift_m=round(float(aligned[0].shift_m), 4),
        fallback_shift_m=(round(float(aligned[-1].shift_m), 4)
                          if len(ladder) > measured else 0.0),
        primary_fraction=round(sum(shares[:measured]), 4),
        fallback_fraction=round(sum(shares[measured:]), 4),
        unmeasured_water_fraction=unmeasured,
        rungs=[(label, round(share, 4)) for label, share in zip(labels, shares)],
        provenance_uri=provenance,
        resolution_m=round(metres, 4),
        notes=notes)
    _surfaced(merged)
    return merged
