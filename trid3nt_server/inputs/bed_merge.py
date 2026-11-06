"""The bed's MERGE and FILL: an ordered list of surfaces, each already on the run's frame, laid into one grid.

The earlier-handed surface wins every cell it measured and a later one paints only what the earlier left;
band 2 names each cell's surface by its place, FILLED or UNMEASURED. What is left inside the region is filled
by the FILL_ALGORITHM setting up to FILL_MAX_DISTANCE pixels from what was measured, and no further.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from trid3nt_server.inputs.geometry import METRES_PER_DEGREE
from trid3nt_server.settings import setting
from trid3nt_server.tools.derive._raster_layers import (
    FILLED, NODATA, UNMEASURED, polygons, staged)

from .user_input import UserInputError

__all__ = ["Laid", "laid"]


@dataclass(frozen=True)
class Laid:
    """The surfaces on one grid: band 1 ``values``, band 2 ``won``, and what the merge and the fill did."""

    values: Any
    won: Any
    crs: Any
    transform: Any
    cell_m: float
    labels: list[str]
    #: Cells inside the region nothing measured that the fill painted, and those it could not reach.
    filled: Any
    unreached: Any
    #: Did the grid differ from the one surface's own: a second surface, a seam blend, or a fill.
    changed: bool


def _metres_per_unit(crs: Any) -> float:
    """How many metres one unit of this CRS spans; a projected CRS in feet is one no source here ships."""
    return 1.0 if crs is not None and crs.is_projected else METRES_PER_DEGREE


def _grid(sources: list[Any], region: list[Any]) -> tuple[Any, int, int, Any, float, list[Any]]:
    """The one grid every surface is read onto: the CRS and cell of the finest, over the union of what they
    cover and of the region, so water past every surface's edge is a hole the fill can reach.

    The region's lon/lat polygons come back on that CRS."""
    from rasterio.transform import from_origin
    from rasterio.warp import transform_bounds, transform_geom
    from shapely.geometry import shape

    finest = min(sources, key=lambda src: min(abs(v) for v in src.res)
                 * _metres_per_unit(src.crs))
    crs = finest.crs
    cell = min(abs(v) for v in finest.res)
    region = [transform_geom("EPSG:4326", crs, g) for g in region]
    boxes = [transform_bounds(src.crs, crs, *src.bounds, densify_pts=21) for src in sources]
    boxes += [shape(g).bounds for g in region]
    west, south = min(b[0] for b in boxes), min(b[1] for b in boxes)
    east, north = max(b[2] for b in boxes), max(b[3] for b in boxes)
    # Rounded before the ceiling: a span of exactly N cells reads N plus a float's error.
    width = max(1, int(math.ceil(round((east - west) / cell, 6))))
    height = max(1, int(math.ceil(round((north - south) / cell, 6))))
    ceiling = setting("MERGE_MAX_CELLS")
    if width * height > ceiling:
        raise UserInputError(
            f"laying the bed's surfaces at {cell * _metres_per_unit(crs):.3g} m over what they and the domain "
            f"cover is {width} x {height} = {width * height} cells, past the {ceiling}-cell ceiling (setting "
            "MERGE_MAX_CELLS). Hand surfaces that cover less ground, or coarser ones.", code="BED_MERGE_CELLS")
    return crs, width, height, from_origin(west, north, cell, cell), cell, region


def _warped(src: Any, crs: Any, width: int, height: int, transform: Any) -> Any:
    """ONE surface on the common grid, NaN where it measured nothing."""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    out = np.full((height, width), NODATA, dtype="float32")
    reproject(source=rasterio.band(src, 1), destination=out,
              src_transform=src.transform, src_crs=src.crs,
              dst_transform=transform, dst_crs=crs, src_nodata=src.nodata,
              dst_nodata=NODATA, resampling=Resampling[setting("MERGE_RESAMPLING")])
    return out


def _band(spans: list[tuple[float, float, str]]) -> str:
    """One population of painted values: what it spans, and which surfaces painted it."""
    return (f"{min(low for low, _high, _label in spans):.2f} to "
            f"{max(high for _low, high, _label in spans):.2f} m from "
            f"{', '.join(label for _low, _high, label in spans)}")


def _no_cliff(grids: list[Any], won: Any, labels: list[str]) -> None:
    """REFUSE a bed whose painted values split into two populations farther apart than the relief the last
    surface measures over the whole grid times MERGE_CLIFF_FACTOR.

    Such a step is two zeros that never met, and every cell is painted, so nothing downstream catches it."""
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
    allowed = relief * setting("MERGE_CLIFF_FACTOR")
    for split in range(1, len(spans)):
        gap = spans[split][0] - max(high for _low, high, _label in spans[:split])
        if gap <= allowed:
            continue
        raise UserInputError(
            f"the bed splits into two populations {gap:.1f} m apart, more than the {relief:.1f} m of relief "
            f"{labels[-1]} measures here (setting MERGE_CLIFF_FACTOR): {_band(spans[:split])}, and "
            f"{_band(spans[split:])}. No ground holds that step - the surfaces were read onto zeros that never "
            "met. Hand surfaces on one frame, or ones whose zero the offset service knows.", code="BED_CLIFF")


def _interpolated(values: Any, targets: Any, known: Any) -> Any:
    """``values`` with the ``targets`` cells interpolated from the ``known`` cells alone, by the FILL_ALGORITHM
    setting, NaN where a target lies farther than FILL_MAX_DISTANCE pixels from every known cell."""
    import numpy as np

    reach = float(setting("FILL_MAX_DISTANCE"))
    work = np.where(known & ~targets, values, NODATA).astype("float32")
    if setting("FILL_ALGORITHM") == "linear":
        painted = _linear(work, targets, reach)
    else:
        from rasterio.fill import fillnodata

        painted = fillnodata(work, mask=np.isfinite(work).astype("uint8"),
                             max_search_distance=reach)
    out = values.copy()
    out[targets] = painted[targets]
    return out


def _linear(work: Any, targets: Any, reach: float) -> Any:
    """Linear interpolation over the triangulation of the measured cells bordering the targets; a target
    outside their hull or past ``reach`` pixels from every measured cell stays NaN."""
    import numpy as np
    from scipy.interpolate import griddata
    from scipy.ndimage import binary_dilation, distance_transform_edt

    known = np.isfinite(work)
    rim = known & binary_dilation(targets, structure=np.ones((3, 3), dtype=bool))
    out = work.copy()
    if int(rim.sum()) < 3:
        return out
    rows, cols = np.nonzero(rim)
    want_rows, want_cols = np.nonzero(targets)
    out[want_rows, want_cols] = griddata((rows, cols), work[rows, cols],
                                         (want_rows, want_cols), method="linear")
    out[targets & (distance_transform_edt(~known) > reach)] = NODATA
    return out


def _seam(won: Any, ranks: int) -> Any:
    """The cells of each later surface within MERGE_BUFFER_CELLS of what the earlier ones measured.

    Blanked and interpolated, so the bed passes from one surface to the next across them."""
    import numpy as np
    from scipy.ndimage import binary_dilation

    width = int(setting("MERGE_BUFFER_CELLS"))
    seam = np.zeros(won.shape, dtype=bool)
    if width <= 0:
        return seam
    for rank in range(1, ranks):
        earlier = won < rank
        seam |= (won == rank) & binary_dilation(earlier, iterations=width) & ~earlier
    return seam


def _inside(region: list[Any], crs: Any, transform: Any, shape: tuple[int, int]) -> Any:
    """The cells inside the region's polygons (lon/lat, put on the grid's CRS here); every cell without one."""
    import numpy as np
    from rasterio.features import geometry_mask

    if not region:
        return np.ones(shape, dtype=bool)
    return geometry_mask(region, out_shape=shape, transform=transform, invert=True)


def laid(surfaces: list[Any], labels: list[str], *, region: Any = None,
         fill: bool = True) -> Laid:
    """The surfaces, in the order handed and named by ``labels``, laid into one grid -> :class:`Laid`.

    ``region`` is the polygon the fill reaches inside (every cell where it carries none); ``fill`` False lays
    without filling. A grid nothing measured a cell of refuses BED_DISJOINT; a cliff refuses BED_CLIFF."""
    import tempfile
    from contextlib import ExitStack

    import numpy as np
    import rasterio

    with tempfile.TemporaryDirectory(prefix="bed-merge-") as scratch:
        paths = [staged(surface, labels[rank], scratch) for rank, surface in enumerate(surfaces)]
        with ExitStack() as opened:
            sources = [opened.enter_context(rasterio.open(path)) for path in paths]
            crs, width, height, transform, cell, shapes = _grid(
                sources, polygons(region) if region else [])
            grids = [_warped(src, crs, width, height, transform) for src in sources]
    won = np.full((height, width), UNMEASURED, dtype="uint8")
    values = np.full((height, width), NODATA, dtype="float32")
    for rank, grid in enumerate(grids):
        take = (won == UNMEASURED) & np.isfinite(grid)
        values[take], won[take] = grid[take], rank
    if not int((won != UNMEASURED).sum()):
        raise UserInputError(
            f"none of the {len(surfaces)} surfaces handed the bed ({', '.join(labels)}) measured a single cell "
            "of the grid they span with the domain. Hand surfaces over the domain's ground.", code="BED_DISJOINT")
    _no_cliff(grids, won, labels)
    seam = _seam(won, len(grids))
    if seam.any():
        values = _interpolated(values, seam, np.isfinite(values))
        won[seam] = np.where(np.isfinite(values[seam]), FILLED, UNMEASURED)
    inside = _inside(shapes, crs, transform, values.shape)
    holes = inside & ~np.isfinite(values)
    filled = np.zeros(values.shape, dtype=bool)
    if fill and holes.any():
        values = _interpolated(values, holes, inside & np.isfinite(values))
        filled = holes & np.isfinite(values)
        won[filled] = FILLED
    return Laid(values=values, won=won, crs=crs, transform=transform,
                cell_m=cell * _metres_per_unit(crs), labels=labels, filled=filled,
                unreached=holes & ~filled,
                changed=len(surfaces) > 1 or bool(seam.any()) or bool(filled.any()))
