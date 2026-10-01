"""``fill_nodata`` - the cells nothing measured inside a polygon, painted between
the measurements and the polygon's edge, the edge seeded at a STATED value.

The fill reaches inside the polygon alone and never reads a run: the seed is a
number somebody states. Every cell it painted is marked FILLED in the second
band, so a filled cell is never read as a measured one.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from trid3nt_contracts.execution import layer_seed
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.derive._raster_layers import (
    FILLED, NODATA, UNMEASURED, RasterLayerError, RasterLayerURI, bbox_4326,
    case_layer, coverage, label_of, percent, polygons, read, sources_of,
    staged, stated, written)

__all__ = ["fill_nodata", "filled"]

logger = logging.getLogger(__name__)

_STYLE = {"kind": "continuous", "ramp": "terrain", "units": "m"}


def _inside(within: Any, crs: Any, transform: Any, shape: tuple[int, int]) -> Any:
    """The cells of this grid INSIDE the polygon, or a refusal where it carries
    none. The polygon is read in lon/lat and put on the grid's own CRS here."""
    import numpy as np
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_geom

    shapes = polygons(within)
    if not shapes:
        raise RasterLayerError(
            "FILL_NODATA_NO_POLYGON",
            f"'within' ({within!r}) carries no polygon, so there is no inside to "
            "fill. Name the polygon layer the fill is bounded by.")
    inside = geometry_mask([transform_geom("EPSG:4326", crs, s) for s in shapes],
                           out_shape=shape, transform=transform, invert=True)
    if not int(np.count_nonzero(inside)):
        raise RasterLayerError(
            "FILL_NODATA_OUTSIDE",
            "the polygon 'within' reaches no cell of this raster, so there is "
            "nothing inside it to fill.")
    return inside


def _painted(band: Any, inside: Any, hole: Any, seed: float) -> Any:
    """The holes inside the polygon painted BETWEEN the measurements and its edge.

    The substrate's own inverse-distance fill on the polygon ALONE: ground outside
    it is never read in, and the polygon's own edge is seeded at ``seed``. There
    is no distance cap - a hole far from every measurement is painted and says so
    through the second band, which is what a reader weighs."""
    import numpy as np
    from rasterio.fill import fillnodata

    inner = inside.copy()
    inner[1:, :] &= inside[:-1, :]
    inner[:-1, :] &= inside[1:, :]
    inner[:, 1:] &= inside[:, :-1]
    inner[:, :-1] &= inside[:, 1:]
    work = np.where(inside, band, NODATA)
    work[inside & ~inner & hole] = float(seed)
    painted = fillnodata(work, mask=np.isfinite(work).astype("uint8"),
                         max_search_distance=float(sum(band.shape)))
    return np.where(hole, painted, band)


def _said(layer: Any, field: str) -> Any:
    """One thing the input layer states about itself, by id or whole."""
    return (layer.get(field) if isinstance(layer, dict)
            else getattr(layer, field, None))


def filled(layer: Any, within: Any, seed: float, *,
           _output_dir: str | None = None) -> RasterLayerURI:
    """The fill itself, over inputs already resolved: what the tool runs."""
    import tempfile

    import numpy as np

    from trid3nt_server.workflows.runtime import journal_note

    label = label_of(layer, 0)
    with tempfile.TemporaryDirectory(prefix="fill-nodata-") as scratch:
        values, won, crs, transform = read(staged(layer, label, scratch))
    if won is None:
        # A raster nothing composed came from ONE input: itself.
        won = np.where(np.isfinite(values), 0, UNMEASURED).astype("uint8")
        sources = [label]
    else:
        sources = sources_of(layer, won)
    inside = _inside(within, crs, transform, values.shape)
    hole = inside & ~np.isfinite(values)
    seed_note = (f"The polygon's edge is seeded at the stated {float(seed):g} m.")
    if bool(hole.any()):
        values = _painted(values, inside, hole, float(seed))
        won = won.copy()
        won[hole] = FILLED
    if "filled" not in sources:
        sources = [*sources, "filled"]
    seed_id = layer_seed()
    uri = written(values, won, crs=crs, transform=transform,
                  prefix="filled_nodata", seed=seed_id, output_dir=_output_dir)
    reached, blank = coverage(won, sources, inside)
    notes = [f"Inside the polygon {stated(reached, blank)}",
             f"{percent(reached[-1][1])} of the cells inside it were filled; "
             "nothing outside it was touched. " + seed_note]
    for line in notes:
        journal_note(line)
    return RasterLayerURI.published(
        "filled-nodata", seed=seed_id, name=f"{label} (filled)",
        layer_type="raster", uri=uri, style=_STYLE, role="primary",
        units=_said(layer, "units") or "m", quantity=_said(layer, "quantity"),
        bbox=bbox_4326(crs, transform, values.shape[1], values.shape[0]),
        vertical_datum=_said(layer, "vertical_datum"),
        sources=sources, coverage=reached, unmeasured_fraction=blank, notes=notes)


_METADATA = AtomicToolMetadata(
    name="fill_nodata", ttl_class="live-no-cache", source_class=None)


@register_tool(_METADATA, read_only_hint=False, open_world_hint=True,
               destructive_hint=False, idempotent_hint=True)
async def fill_nodata(layer: Any = None, within: Any = None,
                      seed: float | None = None,
                      **_extra_ignored: Any) -> RasterLayerURI:
    """FILL the holes in a raster inside a polygon, the polygon's edge seeded at
    a value you state.

    ROUTING: "fill the gaps in this bathymetry inside the lake", "interpolate
    the bed where nothing was sounded", "fill the nodata in the channel to the
    shoreline", "close the holes in this surface within the river polygon". A
    new case layer; the input is not changed.

    `layer` is a case layer id. `within` is the polygon the fill is bounded by -
    a case layer id or GeoJSON;
    nothing outside it is touched. `seed` is the value the polygon's EDGE takes,
    in the raster's own units and frame - for a bed, the elevation the water
    surface stands at - and it is required: nothing reads it off a run. The fill
    is inverse-distance between the measurements and that edge, with no cap.

    Returns a two-band raster: band 1 the values, band 2 the input each cell came
    from with 254 for a cell this fill painted, and the share filled inside the
    polygon.
    """
    if layer is None or within is None:
        raise RasterLayerError(
            "FILL_NODATA_ARGS_INVALID",
            "fill_nodata needs the layer to fill and the polygon 'within' it is "
            "bounded by.")
    if seed is None or isinstance(seed, bool) or not math.isfinite(float(seed)):
        raise RasterLayerError(
            "FILL_NODATA_SEED_UNSTATED",
            "fill_nodata seeds the polygon's edge at a value you STATE - for a "
            "bed, the elevation the water surface stands at - and none was "
            "stated. A number put there instead is a surface nobody measured.")
    held = case_layer(layer, "layer")
    polygon = case_layer(within, "within")
    return await asyncio.to_thread(filled, held, polygon, float(seed))
