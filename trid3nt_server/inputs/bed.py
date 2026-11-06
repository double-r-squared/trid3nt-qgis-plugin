"""THE BED: what every node of the domain carries for elevation.

A layer of soundings is REFUSED by name (gridding it is a step the person runs first). A surface is read on
the RUN's vertical frame, and a surface of depths becomes elevations counted up from the zero it states. An
ordered list of surfaces is laid into one, the first handed winning, and what none measured inside the cut
is filled by the bed settings or REFUSED by name.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from trid3nt_contracts.execution import LayerURI, layer_seed

from trid3nt_server.settings import setting

from .user_input import UserInputError

logger = logging.getLogger(__name__)

__all__ = ["Bed", "DEPTH", "RASTER", "bed", "elevations", "on_the_frame"]

_CODE = "BED_INVALID"

#: The two shapes an elevation source arrives in.
RASTER = "raster"
DEPTH = "depth"

#: What a surface of DEPTHS names its quantity as; a depth counts DOWN from the
#: survey's zero and a bed counts UP, so it reaches the mesh only through the flip.
_DEPTH_QUANTITY = "depth_below_datum"

#: How deep a stated depth may be. A bed stated as a depth below the free
#: surface is a flat bottom, and a number outside this is not one.
_DEPTH_RANGE_M = (0.0, 12000.0)

#: The laid bed is an elevation in metres on the run's frame.
_STYLE = {"kind": "continuous", "ramp": "terrain", "units": "m"}


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
        water: Any = None, merge: bool = True, label: str = "bed",
        code: str = _CODE) -> Bed | None:
    """THE ingestion: a raster, a case layer id, a depth in metres, or an ORDERED LIST of surfaces -> Bed.

    A number is a DEPTH below the free surface; a vector artifact refuses. ``frame`` and ``offset`` (one per
    surface of a list) are the run's vertical frame and shift; ``water`` is the cut polygon, inside which
    coverage is stated and holes filled; ``merge`` False is an engine laying its own ladder of surfaces."""
    if isinstance(value, Bed):
        return value
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return _listed(list(value), frame=frame, offset=offset, water=water,
                       merge=merge, label=label, code=code)
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
    held, surface = _surface(value, frame=frame, offset=offset, label=label, code=code)
    if water is None:
        return Bed(kind=RASTER, source=surface)
    return Bed(kind=RASTER, source=_composed(
        [held], [surface], frame=frame, water=water, fill=merge, label=label))


def _listed(values: list[Any], *, frame: Any, offset: Any, water: Any,
            merge: bool, label: str, code: str) -> Bed:
    """An ORDERED LIST of surfaces as one bed: each read on the run's frame through its own offset, the
    first handed winning every cell it measured."""
    if not values:
        raise UserInputError(
            f"the {label} was handed an empty list: hand it a surface, or several in the order they win.",
            code=code)
    offsets = list(offset) if isinstance(offset, (list, tuple)) else [offset] * len(values)
    if len(values) == 1:
        return bed(values[0], frame=frame, offset=offsets[0], water=water,
                   merge=merge, label=label, code=code)
    if not merge:
        raise UserInputError(
            f"the {label} was handed {len(values)} surfaces, and this engine lays its own ladder of surfaces "
            "and takes the finest at each point: hand the bed one surface and state the others in the "
            "engine's own topography list.", code="BED_LADDER")
    held, surfaces = [], []
    for rank, value in enumerate(values):
        named = f"{label} surface {rank + 1}"
        if _depth(value) is not None or (isinstance(value, Mapping) and "depth_m" in value):
            raise UserInputError(
                f"the {named} is a stated depth, which is a whole flat bed and not one surface of several: "
                "hand the bed that depth alone, or hand it surfaces.", code=code)
        original, surface = _surface(value, frame=frame, offset=offsets[rank], label=named, code=code)
        held.append(original)
        surfaces.append(surface)
    if not str(frame or "").strip():
        from .vertical_datum import DatumError, datum_of, one_datum

        stating = [surface for surface in surfaces if datum_of(surface)]
        try:
            if len(stating) > 1:
                one_datum(*stating, code_prefix="BED_")
        except DatumError as exc:
            raise UserInputError(str(exc), code=code) from exc
    return Bed(kind=RASTER, source=_composed(
        held, surfaces, frame=frame, water=water, fill=water is not None, label=label))


def _surface(value: Any, *, frame: Any, offset: Any, label: str, code: str) -> tuple[Any, Any]:
    """One surface by id or whole, refused as soundings -> ``(as handed, read on the run's frame)``."""
    from .geometry import artifact_class

    if isinstance(value, Mapping) and "layer" in value:
        value = value["layer"]
    value = _by_id(value)
    # A source that arrives as GeoJSON is already READ, and a geometry document
    # is a survey, never a surface.
    if (isinstance(value, Mapping) and "type" in value) \
            or artifact_class(value) == "vector":
        raise UserInputError(
            f"the {label} was handed a layer of points, and a bed is a raster: "
            f"grid the soundings first with run_qgis_algorithm {setting('SOUNDING_GRID_ALGORITHM')} "
            "(QGIS's IDW) and hand this slot the raster it produces.",
            code="BED_POINTS")
    return value, elevations(value, frame=frame, offset=offset, label=label, code=code)


def _by_id(value: Any) -> Any:
    """A case layer named by its id, with the datum and quantity its producer stated."""
    if not isinstance(value, str):
        return value
    from trid3nt_server.render.uri_registry import lookup_layer_for_handle

    return lookup_layer_for_handle(value) or value


def _composed(held: list[Any], surfaces: list[Any], *, frame: Any, water: Any,
              fill: bool, label: str) -> Any:
    """The surfaces laid into one, its coverage STATED and a wet hole past the fill REFUSED -> the layer.

    ``held`` is each surface as handed, which names it and dates it. One surface nothing was painted over
    is handed on as it came; anything else is written as one raster of two bands."""
    from trid3nt_server.tools.derive._raster_layers import label_of

    from .bed_merge import laid
    from .vertical_datum import datum_of

    grid = laid(surfaces, [label_of(surface, rank) for rank, surface in enumerate(held)],
                region=water or None, fill=fill)
    said = _covered(grid, water, label, fill=fill)
    if len(surfaces) > 1:
        said.append(_aged(held, grid.labels))
    if not grid.changed:
        return surfaces[0]
    return _written(grid, str(frame or "").strip() or datum_of(surfaces[0]) or None, label, said)


def _covered(grid: Any, water: Any, label: str, *, fill: bool) -> list[str]:
    """State what the bed covers over water and land and what the fill painted -> the lines; REFUSE a WET hole.

    Coverage is read on a grid over the whole cut; a dry hole is left to the mesh."""
    from trid3nt_server.tools.derive._raster_layers import (
        FILLED, coverage, over_the_cut, percent, stated)
    from .run_notes import cut_coverage

    sources = [*grid.labels, *(["filled"] if (grid.won == FILLED).any() else [])]
    won, wet = (over_the_cut(grid.won, grid.crs, grid.transform, water)
                if water else (grid.won, None))
    said, blank = [], 0.0
    if wet is not None:
        reached, blank = coverage(won, sources, wet)
        said.append(f"Over the WATER - the polygon the domain was cut with - "
                    f"{stated(reached, blank)}")
    reached_land, ashore = coverage(won, sources, None if wet is None else ~wet)
    said.append(f"Over the {'LAND' if wet is not None else 'grid'} "
                f"{stated(reached_land, ashore)}")
    if fill and grid.filled.any():
        said.append(
            f"The fill painted {int(grid.filled.sum())} cells nothing measured inside the "
            f"{'cut' if water else 'grid'}, by {setting('FILL_ALGORITHM')} interpolation reaching at most "
            f"{setting('FILL_MAX_DISTANCE')} px ({setting('FILL_MAX_DISTANCE') * grid.cell_m:.4g} m) from a "
            "measured cell; band 2 marks each one filled.")
    for line in said:
        cut_coverage(line)
    if not blank:
        return said
    remedy = (f"Hand the {label} a further surface after {'these' if len(grid.labels) > 1 else 'this one'} "
              "- an ordered list, the first handed winning - that measures it, or raise the setting "
              f"FILL_MAX_DISTANCE (now {setting('FILL_MAX_DISTANCE')} px) so the fill reaches it."
              if fill else
              f"Hand the {label} a surface that measures it.")
    raise UserInputError(
        f"{percent(blank)} of the water in this domain is measured by nothing in the {label}"
        f"{_where(grid)}, a WET hole: a node standing there has no bed. {remedy}",
        code="BED_WET_HOLE")


def _where(grid: Any) -> str:
    """Where the cells the fill could not reach stand, in lon/lat, or "" where it reached every one."""
    import numpy as np
    from rasterio.transform import xy
    from rasterio.warp import transform as warp

    rows, cols = np.nonzero(grid.unreached)
    if not rows.size:
        return ""
    xs, ys = xy(grid.transform, [rows.min(), rows.max()], [cols.min(), cols.max()])
    lon, lat = warp(grid.crs, "EPSG:4326", list(xs), list(ys))
    return (f" (the cells past the fill's reach stand between ({min(lon):.5f}, {min(lat):.5f}) and "
            f"({max(lon):.5f}, {max(lat):.5f}))")


def _aged(surfaces: list[Any], labels: list[str]) -> str:
    """State how far apart in time the surfaces were measured, and which state no instant -> the line."""
    from trid3nt_server.tools.search.match import instant

    from .run_notes import cut_coverage

    dated, undated = [], []
    for surface, name in zip(surfaces, labels):
        stamp = next((instant(_said(surface, field)) for field in ("reference_time", "valid_from")
                      if instant(_said(surface, field)) is not None), None)
        (dated.append((stamp, name)) if stamp is not None else undated.append(name))
    parts = []
    if len(dated) > 1:
        dated.sort()
        days = (dated[-1][0] - dated[0][0]).days
        parts.append(f"the surfaces were measured {days} days apart ("
                     + "; ".join(f"{name} {stamp.date().isoformat()}" for stamp, name in dated) + ")")
    elif dated:
        parts.append(f"{dated[0][1]} was measured {dated[0][0].date().isoformat()}")
    if undated:
        parts.append(f"{', '.join(undated)} state{'s' if len(undated) == 1 else ''} no instant it was measured")
    line = "Age of the bed: " + ", and ".join(parts) + "."
    cut_coverage(line)
    return line


def _said(layer: Any, field: str) -> Any:
    """One thing a layer states about itself, whole or as a mapping."""
    return layer.get(field) if isinstance(layer, Mapping) else getattr(layer, field, None)


def _written(grid: Any, frame: str | None, label: str, said: list[str]) -> Any:
    """The laid bed written once as two bands -> the layer the slot hands on."""
    from trid3nt_server.tools.derive._raster_layers import (
        RasterLayerURI, bbox_4326, coverage, written)

    seed = layer_seed()
    sources = [*grid.labels, "filled"]
    uri = written(grid.values, grid.won, crs=grid.crs, transform=grid.transform,
                  prefix="bed_laid", seed=seed, output_dir=None)
    reached, blank = coverage(grid.won, sources)
    height, width = grid.values.shape
    return RasterLayerURI.published(
        "bed-laid", seed=seed, name=f"{label} ({', '.join(grid.labels)})",
        layer_type="raster", uri=uri, style=_STYLE, role="primary", units="m",
        quantity="elevation", bbox=bbox_4326(grid.crs, grid.transform, width, height),
        vertical_datum=frame, sources=sources, coverage=reached,
        unmeasured_fraction=blank, notes=said)


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
    from .run_notes import journal_note
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
