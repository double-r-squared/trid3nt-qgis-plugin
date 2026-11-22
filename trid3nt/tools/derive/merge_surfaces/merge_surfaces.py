"""``merge_surfaces`` - an ordered list of raster surfaces laid into one, the earlier handed winning every cell it measured.

A later surface paints only what the earlier left; band 2 names each cell's surface by its place, FILLED or
UNMEASURED. The grid is the finest surface's CRS and cell over the union of the surfaces and the region; the seam is
blended over MERGE_BUFFER_CELLS. It reads rasters and fetches nothing: surfaces on two zeros refuse by name.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt.inputs.geometry import METRES_PER_DEGREE
from trid3nt.inputs.user_input import UserInputError
from trid3nt.settings import setting
from trid3nt.tools import register_tool
from trid3nt.tools.derive import DeriveError
from trid3nt.tools.derive._raster_layers import (
    FILLED, NODATA, UNMEASURED, polygons, staged)
from trid3nt.tools.derive.interpolate_surface.interpolate_surface import inside, interpolated

__all__ = ["Laid", "laid", "merge_surfaces", "published"]


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
    #: The cells inside the region, every cell where it carries none.
    within: Any = None


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


def laid(surfaces: list[Any], labels: list[str], *, region: Any = None,
         fill: Any = None) -> Laid:
    """The surfaces, in the order handed and named by ``labels``, laid into one grid -> :class:`Laid`.

    ``region`` is the polygon the fill reaches inside (every cell where it carries none); ``fill`` is the
    interpolation the holes inside it are filled by, ``None`` laying without filling.
    A grid nothing measured a cell of refuses BED_DISJOINT; a cliff refuses BED_CLIFF."""
    import tempfile
    from contextlib import ExitStack

    import numpy as np
    import rasterio

    from trid3nt.inputs.coverage import interpolation

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
        # The seam is the merge's own blend, interpolated by the settings in force.
        blend = interpolation(None)
        values = interpolated(values, seam, np.isfinite(values), algorithm=blend.algorithm,
                              max_distance=blend.max_distance)
        won[seam] = np.where(np.isfinite(values[seam]), FILLED, UNMEASURED)
    within = inside(shapes, crs, transform, values.shape)
    holes = within & ~np.isfinite(values)
    filled = np.zeros(values.shape, dtype=bool)
    if fill is not None and holes.any():
        values = interpolated(values, holes, within & np.isfinite(values), algorithm=fill.algorithm,
                              max_distance=fill.max_distance)
        filled = holes & np.isfinite(values)
        won[filled] = FILLED
    return Laid(values=values, won=won, crs=crs, transform=transform,
                cell_m=cell * _metres_per_unit(crs), labels=labels, filled=filled,
                unreached=holes & ~filled, within=within,
                changed=len(surfaces) > 1 or bool(seam.any()) or bool(filled.any()))


#: A laid surface is an elevation in metres.
_STYLE = {"kind": "continuous", "ramp": "terrain", "units": "m"}


def published(grid: Laid, *, frame: str | None, name: str, notes: list[str], kind: str = "merged",
              prefix: str = "merged") -> Any:
    """The laid grid written once as two bands -> the layer, with the share each source reached and nothing did."""
    from trid3nt_contracts.execution import layer_seed

    from trid3nt.tools.derive._raster_layers import RasterLayerURI, bbox_4326, coverage, written

    seed = layer_seed()
    sources = [*grid.labels, "filled"]
    uri = written(grid.values, grid.won, crs=grid.crs, transform=grid.transform,
                  prefix=prefix, seed=seed, output_dir=None)
    reached, blank = coverage(grid.won, sources)
    height, width = grid.values.shape
    return RasterLayerURI.published(
        kind, seed=seed, name=name,
        layer_type="raster", uri=uri, style=_STYLE, role="primary", units="m",
        quantity="elevation", bbox=bbox_4326(grid.crs, grid.transform, width, height),
        vertical_datum=frame, sources=sources, coverage=reached,
        unmeasured_fraction=blank, notes=notes)


def merged_layer(layers: list[Any], *, region: Any = None, name: str | None = None) -> Any:
    """The tool's body: each surface read as elevations on its own zero, the zeros held to one, laid -> the layer."""
    from trid3nt.inputs.bed import elevations
    from trid3nt.inputs.vertical_datum import DatumError, datum_of, one_datum
    from trid3nt.render.uri_registry import lookup_layer_for_handle
    from trid3nt.tools.derive._raster_layers import label_of

    if not isinstance(layers, (list, tuple)) or not layers:
        raise DeriveError("MERGE_ARGUMENT", "merge_surfaces takes an ordered list of raster layers, the first winning.")
    held = [lookup_layer_for_handle(v) or v if isinstance(v, str) else v for v in layers]
    surfaces = [elevations(layer, frame=None, label=f"surface {rank + 1}", code="MERGE_SURFACE")
                for rank, layer in enumerate(held)]
    stating = [surface for surface in surfaces if datum_of(surface)]
    try:
        frame = one_datum(*stating, code_prefix="MERGE_") if len(stating) > 1 else (
            datum_of(stating[0]) if stating else None)
    except DatumError as exc:
        raise DeriveError(exc.error_code, str(exc)) from exc
    labels = [label_of(layer, rank) for rank, layer in enumerate(held)]
    grid = laid(surfaces, labels, region=_region(region))
    return published(grid, frame=frame or None, name=name or f"merged ({', '.join(labels)})", notes=[])


def _region(region: Any) -> Any:
    from trid3nt.render.uri_registry import lookup_layer_for_handle

    return (lookup_layer_for_handle(region) or region) if isinstance(region, str) else region


_METADATA = AtomicToolMetadata(name="merge_surfaces", ttl_class="live-no-cache", source_class=None)


@register_tool(_METADATA, read_only_hint=False, open_world_hint=False, destructive_hint=False, idempotent_hint=True)
def merge_surfaces(layers: list[Any] | None = None, region: Any = None, name: str | None = None,
                   **_extra_ignored: Any) -> Any:
    """DERIVE: lay an ordered list of raster surfaces (beds, DEMs, bathymetry) into one, the first winning where it measured.

    ROUTING: "merge the survey with the DEM", "combine these two bathymetry rasters, the survey first", "lay the eHydro
    grid over CUDEM", "mosaic these elevation layers in this order". A new case layer; the inputs are not changed.

    `layers` is an ordered list of case layer ids (or uris) of rasters: the first handed wins every cell it measured, a
    later one paints only the cells left empty. Each is read as elevations on the zero it states (a surface of depths
    counted up from its own); two surfaces stating different zeros refuse naming both - bring one onto the other's
    first. `region` is an optional polygon layer id or GeoJSON the grid reaches over. Nothing is interpolated: holes
    stay nodata (interpolate_surface fills them).

    Returns a two-band raster on the finest surface's grid: band 1 the elevations, band 2 which surface valued each
    cell, with the share each surface reached and the share nothing reached. Two populations farther apart than the
    relief refuse (BED_CLIFF), and surfaces with no ground in common refuse (BED_DISJOINT).
    """
    return merged_layer(list(layers or []), region=region, name=name)
