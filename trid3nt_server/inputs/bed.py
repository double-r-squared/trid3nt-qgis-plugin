"""THE BED: what every node of the domain carries for elevation.

One slot over every source a user can have: a fetched DEM, a fetched or supplied
bathymetry raster, a survey raster, a layer of soundings, or a stated depth below
the free surface. The slot says WHICH of those it was handed; what a mesher does
with each is the mesher's, and nothing above here branches on where it came from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from .user_input import UserInputError

__all__ = ["Bed", "DEPTH", "POINTS", "RASTER", "SURVEY_DERIVE", "bed"]

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


def bed(value: Any, *, label: str = "bed", code: str = _CODE) -> Bed | None:
    """THE ingestion: a raster, a sounding layer, or a depth in metres -> Bed.

    ``None`` only when nothing came. A number is a DEPTH below the free surface;
    a vector artifact is a point survey; everything else is a surface."""
    if value is None or isinstance(value, Bed):
        return value
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
    if isinstance(value, Mapping) and "type" in value:
        return Bed(kind=POINTS, source=value)
    if artifact_class(value) == "vector":
        return Bed(kind=POINTS, source=value)
    return Bed(kind=RASTER, source=value)


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
