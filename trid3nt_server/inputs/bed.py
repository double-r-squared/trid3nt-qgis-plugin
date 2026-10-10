"""THE BED: what every node of the domain carries for elevation.

A layer of soundings is REFUSED by name (gridding it is a step the person runs first),
and so is water the surface did not measure. A surface is read on the RUN's vertical
frame, and a surface of depths becomes elevations counted up from the zero it states.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from trid3nt_contracts.execution import LayerURI, layer_seed

from .user_input import UserInputError

logger = logging.getLogger(__name__)

__all__ = ["Bed", "DEPTH", "RASTER", "bed", "elevations", "on_the_frame"]

_CODE = "BED_INVALID"

#: The two steps that compose a bed covering water nothing measured.
_REMEDIES = ("merge_rasters", "fill_nodata")

#: The two shapes an elevation source arrives in.
RASTER = "raster"
DEPTH = "depth"

#: The QGIS step a layer of soundings is gridded by before it is a bed.
_GRID_STEP = "gdal:gridinversedistancenearestneighbor"

#: What a surface of DEPTHS names its quantity as; a depth counts DOWN from the
#: survey's zero and a bed counts UP, so it reaches the mesh only through the flip.
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


def bed(value: Any, *, frame: Any = None, offset: Any = None,
        water: Any = None, label: str = "bed", code: str = _CODE) -> Bed | None:
    """THE ingestion: a raster, a case layer id, or a depth in metres -> Bed.

    A number is a DEPTH below the free surface; a vector artifact refuses. ``frame``
    and ``offset`` are the run's vertical frame and shift; ``water`` is the cut polygon."""
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

    value = _by_id(value)
    # A source that arrives as GeoJSON is already READ, and a geometry document
    # is a survey, never a surface.
    if (isinstance(value, Mapping) and "type" in value) \
            or artifact_class(value) == "vector":
        raise UserInputError(
            f"the {label} was handed a layer of points, and a bed is a raster: "
            f"grid the soundings first with run_qgis_algorithm {_GRID_STEP} "
            "(QGIS's IDW) and hand this slot the raster it produces.",
            code="BED_POINTS")
    if water is not None:
        _covered(value, water, label)
    return Bed(kind=RASTER,
               source=elevations(value, frame=frame, offset=offset, label=label,
                                 code=code))


def _by_id(value: Any) -> Any:
    """A case layer named by its id, with the datum and quantity its producer stated."""
    if not isinstance(value, str):
        return value
    from trid3nt_server.render.uri_registry import lookup_layer_for_handle

    return lookup_layer_for_handle(value) or value


def _covered(surface: Any, water: Any, label: str) -> None:
    """State what the surface covers over water and land, and REFUSE a WET hole by name.

    Coverage is read on a grid over the whole cut; a dry hole is left to the mesh."""
    import tempfile

    import numpy as np

    from trid3nt_server.tools.derive._raster_layers import (
        UNMEASURED, coverage, label_of, over_the_cut, percent, read,
        sources_of, staged, stated)
    from trid3nt_server.workflows.runtime import cut_coverage

    with tempfile.TemporaryDirectory(prefix="bed-coverage-") as scratch:
        values, won, crs, transform = read(staged(surface, label, scratch))
    if won is None:
        won, sources = np.zeros(values.shape, dtype="uint8"), [label_of(surface, 0)]
    else:
        sources = sources_of(surface, won)
    won = np.where(np.isfinite(values), won, UNMEASURED).astype("uint8")
    won, wet = over_the_cut(won, crs, transform, water) if water else (won, None)
    said, blank = [], 0.0
    if wet is not None:
        reached, blank = coverage(won, sources, wet)
        said.append(f"Over the WATER - the polygon the domain was cut with - "
                    f"{stated(reached, blank)}")
    reached_land, ashore = coverage(won, sources, None if wet is None else ~wet)
    said.append(f"Over the {'LAND' if wet is not None else 'grid'} "
                f"{stated(reached_land, ashore)}")
    for line in said:
        cut_coverage(line)
    if blank:
        raise UserInputError(
            f"{percent(blank)} of the water in this domain is measured by "
            f"nothing in the {label}, a WET hole: a node standing there has no "
            f"bed. Run {_REMEDIES[0]} to lay a surface that measures it under "
            f"this one, or {_REMEDIES[1]} within the domain seeded at the water "
            "surface, and hand the layer it produces to this slot.",
            code="BED_WET_HOLE")


def _depth(value: Any) -> float | None:
    """``value`` as a stated depth, or ``None``; a bool is not a depth."""
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

    A source whose zero reaches the run's frame through nothing refuses by name:
    an invented shift would read downstream as a measurement."""
    from .vertical_datum import DatumError, datum_of, onto_frame

    quantity = (layer.get("quantity") if isinstance(layer, Mapping)
                else getattr(layer, "quantity", None))
    depths = str(quantity or "").startswith(_DEPTH_QUANTITY)
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
    """Say the flip and the shift on the run's journal, where a packet reader sees them."""
    from trid3nt_server.workflows.runtime import journal_note
    from .vertical_datum import datum_of

    zero = datum_of(layer) or "its own datum"
    counted = (f"the {label} is a surface of DEPTHS below {zero}, read as "
               f"elevations counted up from it" if depths else
               f"the {label} is a surface of elevations on {zero}")
    moved = (f"and it is {aligned.note}" if aligned.shift_m
             else f"and {zero} IS this run's frame")
    journal_note(f"{counted}, {moved}.")


def on_the_frame(values: Any, offset_m: float, *, depths: bool) -> Any:
    """One grid read on another zero: ``offset - depth``, or ``value + offset``.

    Runs on the source's OWN grid, before anything resamples it onto another."""
    import numpy as np

    return (np.float32(offset_m) - values if depths
            else values + np.float32(offset_m))


def _flipped(layer: Any, offset_m: float, frame: str, label: str,
             code: str, *, depths: bool) -> Any:
    """The same grid on the run's frame, written once as one raster."""
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
        values = on_the_frame(read, offset_m, depths=depths)
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
