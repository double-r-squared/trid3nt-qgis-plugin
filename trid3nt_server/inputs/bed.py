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

import logging
import math
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from trid3nt_contracts.execution import LayerURI

from .user_input import UserInputError

logger = logging.getLogger(__name__)

__all__ = ["Bed", "DEPTH", "POINTS", "RASTER", "SURVEY_DERIVE",
           "SurveySurfaceError", "SurveySurfaceLayerURI", "bed", "elevations",
           "survey_surface"]

_CODE = "BED_INVALID"

#: The three shapes an elevation source arrives in.
RASTER = "raster"
POINTS = "points"
DEPTH = "depth"

#: The registered name the runtime calls the grid below by. The rule is the
#: bed's own - only this slot needs a surface between soundings - and the tool is
#: a thin shim over it while declared plan steps still name it.
SURVEY_DERIVE = "derive_survey_surface"

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
        raise UserInputError(
            f"the {label} counts from {zero} and this run counts from "
            f"{frame}: {exc}", code=code) from exc
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


def _flipped(layer: Any, offset_m: float, frame: str, label: str,
             code: str, *, depths: bool) -> Any:
    """The same grid on the run's frame: ``offset - depth``, or ``value +
    offset``. One raster, written once."""
    import tempfile
    import uuid

    import numpy as np
    import rasterio
    from trid3nt_contracts.execution import LayerURI

    from trid3nt_server.inputs.geometry import source_uri
    from trid3nt_server.tools.derive._hydrology_common import (
        _stage_uri_local, write_cog)

    uri = str(source_uri(layer) or "").strip()
    if not uri:
        raise UserInputError(
            f"the {label} names no raster file to read ({layer!r}).", code=code)
    seed = uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="bed-elevation-") as scratch:
        with rasterio.open(_stage_uri_local(uri, scratch, "bed")) as src:
            read = src.read(1, masked=True).filled(np.nan).astype("float32")
            crs, transform = src.crs, src.transform
        values = (np.float32(offset_m) - read if depths
                  else read + np.float32(offset_m))
        written = write_cog(values, crs=crs,
                            transform=transform, prefix="bed_elevation",
                            seed=seed, output_dir=None,
                            code="BED_ELEVATION_WRITE_FAILED",
                            nodata=float("nan"))
    return LayerURI(
        layer_id=f"bed-elevation-{seed}",
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
        datum_offset_m=offset_m,
        datum_offset_frame=offset_frame,
        n_points=len(values),
        resolution_m=resolution_m,
        search_radius_m=round(radius, 3),
        filled_fraction=round(filled, 4),
        value_min=round(float(values.min()), 4),
        value_max=round(float(values.max()), 4),
        notes=notes)
