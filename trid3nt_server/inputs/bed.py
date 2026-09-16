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

from dataclasses import dataclass
from typing import Any, Mapping

from .user_input import UserInputError

__all__ = ["Bed", "DEPTH", "POINTS", "RASTER", "SURVEY_DERIVE", "bed",
           "elevations"]

_CODE = "BED_INVALID"

#: The three shapes an elevation source arrives in.
RASTER = "raster"
POINTS = "points"
DEPTH = "depth"

#: The derive that turns a layer of soundings into the surface a mesh samples.
#: A slot calls a DERIVE by name; interpolating scattered measurements onto a
#: surface is useful outside any slot, so it is a tool and not a coercion.
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

