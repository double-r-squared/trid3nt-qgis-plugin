"""THE BED: what every node of the domain carries for elevation.

One slot over every source a user can have: a fetched DEM, a fetched or supplied
bathymetry raster, a survey raster, a layer of soundings, or a stated depth below
the free surface. The slot says WHICH of those it was handed; what a mesher does
with each is the mesher's, and nothing above here branches on where it came from.
Two things happen on the way in, because only the slot knows the bed is an
ELEVATION: a survey's depths are turned into elevations on the frame that survey
publishes itself against, and a narrow measurement is composed over the wider
surface the row names beside it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from .user_input import UserInputError

__all__ = ["Bed", "DEPTH", "MERGE_DERIVE", "POINTS", "RASTER", "SURVEY_DERIVE",
           "bed"]

logger = logging.getLogger("trid3nt_server.inputs.bed")

_CODE = "BED_INVALID"

#: The three shapes an elevation source arrives in.
RASTER = "raster"
POINTS = "points"
DEPTH = "depth"

#: The derive that turns a layer of soundings into the surface a mesh samples.
#: A slot calls a DERIVE by name; interpolating scattered measurements onto a
#: surface is useful outside any slot, so it is a tool and not a coercion.
SURVEY_DERIVE = "derive_survey_surface"

#: The derive that lays a narrow measurement over the wider surface under it.
#: Called by name for the same reason: composing two surfaces is a question
#: anyone can ask, and the slot only decides that this bed is composed at all.
MERGE_DERIVE = "derive_merge_rasters"

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

    @property
    def is_depth(self) -> bool:
        return self.kind == DEPTH


def bed(value: Any, *, over: Any = None, label: str = "bed",
        code: str = _CODE) -> Bed | None:
    """THE ingestion: a raster, a sounding layer, or a depth in metres -> Bed.

    ``None`` only when nothing came and nothing lies under it. A number is a
    DEPTH below the free surface; a vector artifact is a point survey; everything
    else is a surface. ``over`` is the wider surface this one is composed over -
    the terrain a channel survey measures only part of - and it is what the bed
    is where the measurement stops, or whole where the measurement never came."""
    if isinstance(value, Bed):
        return value
    if value is None:
        return bed(over, label=label, code=code) if over is not None else None
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
        if over is not None:
            raise UserInputError(
                f"the {label} is a layer of SOUNDINGS and this row composes it "
                f"over a wider surface, which is done between two rasters. Grid "
                f"the soundings with {SURVEY_DERIVE!r} first, or drop the wider "
                "surface and let the mesh interpolate the points at its own "
                "element scale.", code=code)
        return Bed(kind=POINTS, source=value)
    surface = _elevation(value, label, code)
    return Bed(kind=RASTER,
               source=surface if over is None else _composed(surface, over))


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


def _elevation(layer: Any, label: str, code: str) -> Any:
    """A surface of DEPTHS below a survey's own zero, read as ELEVATIONS.

    The flip and the shift are one step and they live here: the interpolator
    grids what it was given, the merge only ever adds an offset somebody
    measured, and the bed is the one place that knows which way is up. A survey
    that publishes no shift onto a national frame refuses by name - inventing
    one would be indistinguishable from a measurement downstream."""
    if not str(getattr(layer, "quantity", "") or "").startswith(_DEPTH_QUANTITY):
        return layer
    from .vertical_datum import datum_of

    offset_m = getattr(layer, "datum_offset_m", None)
    frame = str(getattr(layer, "datum_offset_frame", "") or "")
    zero = datum_of(layer) or "its own datum"
    if offset_m is None or not frame:
        raise UserInputError(
            f"the {label} is a surface of DEPTHS below {zero}, and nothing "
            "states how far that zero sits above a national frame, so the "
            "depths cannot be read as the elevations a bed carries. Name a "
            "survey whose own metadata publishes the shift, or supply a bed "
            "already measured as elevations.", code=code)
    logger.info("%s: depths below %s read as elevations on %s - the survey's own "
                "rows put %s at %+.4f m on %s", label, zero, frame, zero,
                offset_m, frame)
    return _flipped(layer, float(offset_m), frame, label, code)


def _flipped(layer: Any, offset_m: float, frame: str, label: str,
             code: str) -> Any:
    """The same grid, each cell ``offset - depth``: one raster, written once."""
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
            depths = src.read(1, masked=True).filled(np.nan).astype("float32")
            crs, transform = src.crs, src.transform
        written = write_cog(np.float32(offset_m) - depths, crs=crs,
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


def _composed(primary: Any, over: Any) -> Any:
    """This measurement laid over the wider surface, through the merge derive.

    Called by NAME: composing two surfaces is a question anyone can ask outside
    a slot, and a tree without the derive refuses saying which one."""
    from trid3nt_server.tools import TOOL_REGISTRY

    if MERGE_DERIVE not in TOOL_REGISTRY:
        raise UserInputError(
            f"this bed is composed over a wider surface and {MERGE_DERIVE!r} is "
            "not registered, so there is nothing to lay one over the other.",
            code=_CODE)
    return TOOL_REGISTRY[MERGE_DERIVE].fn(primary=primary, fallback=over)
