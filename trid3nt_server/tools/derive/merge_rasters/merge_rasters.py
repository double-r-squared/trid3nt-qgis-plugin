"""``merge_rasters`` - several surfaces laid into ONE, first wins, provenance kept.

Every input is read onto ONE vertical frame - the last input's zero - through the
shift it publishes about itself or the one the offset service measures for it,
before any of them paints a cell. The second band names which input each cell
came from, and the share each reached is the feedback.
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
    NODATA, UNMEASURED, RasterLayerError, RasterLayerURI, bbox_4326, coverage,
    label_of, resolved, staged, stated, written)

__all__ = ["merge_rasters", "merged"]

logger = logging.getLogger(__name__)

#: What a source counting DOWN from its zero names its quantity as.
_DEPTH_QUANTITY = "depth_below_datum"

#: The most cells one merge will build: a ceiling refuses by name rather than
#: quietly coarsening the measurement the merge was called to keep.
_MAX_CELLS = 60_000_000

#: The merged surface is an elevation in metres on the frame it landed on.
_STYLE = {"kind": "continuous", "ramp": "terrain", "units": "m"}


def _metres_per_unit(crs: Any) -> float:
    """How many metres one unit of this CRS spans, for a degree grid or a metre
    one. A projected CRS in feet is not one any source here ships."""
    return 1.0 if crs is not None and crs.is_projected else 111_320.0


def _common_grid(sources: list[Any]) -> tuple[Any, int, int, Any, float]:
    """The one grid every input is read onto: the CRS and cell of the finest,
    over the union of what they cover."""
    from rasterio.transform import from_origin
    from rasterio.warp import transform_bounds

    finest = min(sources, key=lambda src: min(abs(v) for v in src.res)
                 * _metres_per_unit(src.crs))
    crs = finest.crs
    cell = min(abs(v) for v in finest.res)
    boxes = [transform_bounds(src.crs, crs, *src.bounds, densify_pts=21)
             for src in sources]
    west, south = min(b[0] for b in boxes), min(b[1] for b in boxes)
    east, north = max(b[2] for b in boxes), max(b[3] for b in boxes)
    width = max(1, int(math.ceil((east - west) / cell)))
    height = max(1, int(math.ceil((north - south) / cell)))
    if width * height > _MAX_CELLS:
        raise RasterLayerError(
            "MERGE_RASTERS_RESOLUTION_INVALID",
            f"merging these over their union at "
            f"{cell * _metres_per_unit(crs):.3g} m is {width} x {height} = "
            f"{width * height} cells, past the {_MAX_CELLS}-cell ceiling. Merge "
            "surfaces that cover less ground, or coarser ones.")
    return crs, width, height, from_origin(west, north, cell, cell), cell


def _warped(src: Any, crs: Any, width: int, height: int, transform: Any,
            values: Any) -> Any:
    """ONE input on the common grid, nodata where it measured nothing.

    ``values`` is that input's grid already re-zeroed, where the frame moved it."""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    out = np.full((height, width), NODATA, dtype="float32")
    reproject(source=(rasterio.band(src, 1) if values is None else values),
              destination=out,
              src_transform=src.transform, src_crs=src.crs,
              dst_transform=transform, dst_crs=crs,
              src_nodata=(src.nodata if values is None else NODATA),
              dst_nodata=NODATA, resampling=Resampling.bilinear)
    return out


def _counts_down(layer: Any) -> bool:
    """Does this surface state its values as DEPTHS below its own zero?"""
    quantity = (layer.get("quantity") if isinstance(layer, dict)
                else getattr(layer, "quantity", None))
    return str(quantity or "").startswith(_DEPTH_QUANTITY)


def _aligned(source: Any, frame: str, offset: Any) -> Any:
    """What it costs to read ONE input on the frame this merge lands on.

    An offset nothing measured is the one the source publishes about itself;
    a pair nothing measures refuses naming both, and a frame nothing named is
    the unstated zero refusing by name."""
    from trid3nt_server.inputs.vertical_datum import (
        DatumError, one_datum, onto_frame)

    try:
        if not frame:
            one_datum(source, code_prefix="MERGE_RASTERS_")
        return onto_frame(source, frame, offset=offset,
                          code_prefix="MERGE_RASTERS_")
    except DatumError as exc:
        raise RasterLayerError(exc.error_code, str(exc)) from exc


def _placed(layers: list[Any], offsets: list[Any], labels: list[str], zero: str
            ) -> list[tuple[int, Any]]:
    """Every input that can be READ on the landing frame, in the order laid,
    with what it costs to read there.

    One nothing places on that frame drops off and the journal says which and
    why; the FIRST is the exception, because a merge whose best input cannot be
    placed is not that merge."""
    from trid3nt_server.inputs.vertical_datum import datum_of
    from trid3nt_server.workflows.runtime import journal_note

    standing: list[tuple[int, Any]] = []
    for rank, layer in enumerate(layers):
        try:
            standing.append((rank, _aligned(layer, zero, offsets[rank])))
        except RasterLayerError:
            if rank == 0:
                raise
            line = (f"{labels[rank]} counts from "
                    f"{datum_of(layer) or 'no stated zero'} and nothing measures "
                    f"that against {zero}, so it drops off this merge and the "
                    "inputs after it paint what it would have.")
            logger.info("merge_rasters: %s", line)
            journal_note(line)
    return standing


def _read_as(layer: Any, label: str, aligned: Any, *, depths: bool) -> str:
    """The sentence the merge SAYS about an input it moved onto its frame, or
    "" where that input already stood on it."""
    from trid3nt_server.inputs.vertical_datum import datum_of

    if not (depths or aligned.shift_m):
        return ""
    zero = datum_of(layer) or "its own datum"
    counted = (f"{label} is a surface of DEPTHS below {zero}, read as "
               f"elevations counted up from it" if depths else
               f"{label} is a surface of elevations on {zero}")
    return f"{counted}, and it is {aligned.note}."


def _band(spans: list[tuple[float, float, str]]) -> str:
    """One population of painted values: what it spans, and which painted it."""
    return (f"{min(low for low, _high, _label in spans):.2f} to "
            f"{max(high for _low, high, _label in spans):.2f} m from "
            f"{', '.join(label for _low, _high, label in spans)}")


def _no_cliff(grids: list[Any], won: Any, labels: list[str]) -> None:
    """REFUSE a merge whose painted values split into two populations farther
    apart than the relief the last input measures over the whole grid.

    A step wider than that is two zeros that never met, and every cell is
    painted, so nothing downstream catches it."""
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
        raise RasterLayerError(
            "MERGE_RASTERS_CLIFF",
            f"the merged surface splits into two populations {gap:.1f} m apart, "
            f"more than the {relief:.1f} m of relief {labels[-1]} measures here: "
            f"{_band(spans[:split])}, and {_band(spans[split:])}. No ground holds "
            "that step - the inputs were read onto zeros that never met. Name "
            "inputs on one frame, or ones whose zero the offset service knows.")


async def _offsets(layers: list[Any], zero: str) -> list[Any]:
    """The measured shift onto ``zero`` for each input that owes one, asked at
    its own footprint, or ``None`` where it owes none."""
    from trid3nt_server.inputs.vertical_datum import OFFSET_FETCH, offset_ask
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime.fill import call

    held: list[Any] = []
    for layer in layers:
        box = (layer.get("bbox") if isinstance(layer, dict)
               else getattr(layer, "bbox", None))
        at = (((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
              if box else None)
        ask = await asyncio.to_thread(offset_ask, layer, zero, at=at)
        held.append(None if ask is None else
                    await call(TOOL_REGISTRY[OFFSET_FETCH].fn, ask, OFFSET_FETCH))
    return held


def merged(layers: list[Any], name: str, offsets: list[Any],
           *, _output_dir: str | None = None) -> RasterLayerURI:
    """The overlay itself, over inputs already resolved: what the tool runs."""
    import tempfile
    from contextlib import ExitStack

    import numpy as np
    import rasterio
    from rasterio.merge import merge

    from trid3nt_server.inputs.bed import on_the_frame
    from trid3nt_server.inputs.vertical_datum import datum_of
    from trid3nt_server.workflows.runtime import journal_note

    zero = datum_of(layers[-1])
    labels = [label_of(layer, rank) for rank, layer in enumerate(layers)]
    standing = _placed(layers, offsets, labels, zero)
    aligned = [cost for _rank, cost in standing]
    labels = [labels[rank] for rank, _cost in standing]
    layers = [layers[rank] for rank, _cost in standing]
    depths = [_counts_down(layer) for layer in layers]
    seed = layer_seed()
    with tempfile.TemporaryDirectory(prefix="merge-rasters-") as scratch:
        paths = [staged(layer, labels[rank], scratch)
                 for rank, layer in enumerate(layers)]
        with ExitStack() as opened:
            sources = [opened.enter_context(rasterio.open(p)) for p in paths]
            crs, width, height, transform, cell = _common_grid(sources)
            grids, laid = [], []
            for rank, src in enumerate(sources):
                moved = (on_the_frame(
                    src.read(1, masked=True).filled(NODATA).astype("float32"),
                    aligned[rank].shift_m, depths=depths[rank])
                    if depths[rank] or aligned[rank].shift_m else None)
                grid = _warped(src, crs, width, height, transform, moved)
                grids.append(grid)
                laid.append(_on_disk(grid, crs, transform, scratch, rank))
        # BOTTOM UP, so the input with the best claim to a cell writes it last.
        won = np.full(grids[0].shape, UNMEASURED, dtype="uint8")
        for rank in range(len(grids) - 1, -1, -1):
            won[np.isfinite(grids[rank])] = rank
        if not int((won != UNMEASURED).sum()):
            raise RasterLayerError(
                "MERGE_RASTERS_DISJOINT",
                f"none of the {len(layers)} surfaces ({', '.join(labels)}) "
                "measured a single cell of the grid they span together, so there "
                "is nothing to merge. Name surfaces over the same ground.")
        _no_cliff(grids, won, labels)
        west, north = transform.c, transform.f
        stack, _ = merge(laid, method="first",
                         bounds=(west, north + height * transform.e,
                                 west + width * transform.a, north),
                         res=(cell, cell))
        uri = written(stack[0], won, crs=crs, transform=transform,
                      prefix="merged_rasters", seed=seed, output_dir=_output_dir)
    reached, blank = coverage(won, labels)
    moves = [line for line in (_read_as(layer, labels[rank], aligned[rank],
                                        depths=depths[rank])
                               for rank, layer in enumerate(layers)) if line]
    notes = [f"Laid first wins: {stated(reached, blank)}",
             f"Merged at {cell * _metres_per_unit(crs):.3g} m in {crs}, the "
             "finest of the inputs.",
             *(moves or [f"Every input counts from {zero}."])]
    for line in notes:
        journal_note(line)
    return RasterLayerURI.published(
        "merged-rasters", seed=seed, name=f"{name} (on {zero})",
        layer_type="raster", uri=uri, style=_STYLE, role="primary", units="m",
        quantity=next((getattr(layer, "quantity", None)
                       for rank, layer in enumerate(layers)
                       if not depths[rank] and getattr(layer, "quantity", None)),
                      None),
        bbox=bbox_4326(crs, transform, width, height),
        vertical_datum=zero or None, sources=labels, coverage=reached,
        unmeasured_fraction=blank, notes=notes)


def _on_disk(grid: Any, crs: Any, transform: Any, scratch: str, rank: int) -> str:
    """One warped input left on disk for the substrate's own first-wins merge."""
    import os

    import rasterio

    path = os.path.join(scratch, f"input{rank}_on_the_grid.tif")
    with rasterio.open(path, "w", driver="GTiff", height=grid.shape[0],
                       width=grid.shape[1], count=1, dtype="float32", crs=crs,
                       transform=transform, nodata=NODATA, tiled=True) as out:
        out.write(grid, 1)
    return path


_METADATA = AtomicToolMetadata(
    name="merge_rasters", ttl_class="live-no-cache", source_class=None)


@register_tool(_METADATA, read_only_hint=False, open_world_hint=True,
               destructive_hint=False, idempotent_hint=True)
async def merge_rasters(layers: list[Any] | None = None,
                        name: str = "merged rasters",
                        **_extra_ignored: Any) -> RasterLayerURI:
    """LAY several rasters into ONE, first wins, and say which painted each cell.

    ROUTING: "merge the survey over the DEM", "combine these bathymetry layers",
    "lay the chart soundings first and the terrain under them", "mosaic these
    surfaces with this one on top", "fill the gaps in this raster from that one".
    A new case layer; the inputs are not changed.

    `layers` is the ORDER of priority: the first paints every cell it measured,
    each after it only the cells the ones before left. Each is a case layer id,
    a source name fetched over the run's domain, or {"source": name, "bbox":
    [w, s, e, n], ...} stating the place and any other argument that source
    takes. Every input is read onto ONE vertical frame - the LAST input's own -
    through the shift it publishes or the offset the datum service measures at
    its footprint; one nothing places drops off, said on the journal, and the
    first being unplaceable refuses. Cell and CRS are the finest input's, over
    the union of all of them. Two populations farther apart than the last
    input's relief refuse as a cliff.

    Returns a two-band raster: band 1 the values, band 2 the place in `layers`
    of the input each cell came from (255 where none reached it), with the
    share each input reached, the frame it landed on, and every shift applied.
    """
    listed = list(layers) if isinstance(layers, (list, tuple)) else \
        ([layers] if layers else [])
    if not listed:
        raise RasterLayerError(
            "MERGE_RASTERS_NO_SOURCE",
            "merge_rasters was given no layers, so there is nothing to lay.")
    held = [await resolved(layer, f"input {rank + 1}")
            for rank, layer in enumerate(listed)]
    from trid3nt_server.inputs.vertical_datum import datum_of

    offsets = await _offsets(held, datum_of(held[-1]))
    return await asyncio.to_thread(merged, held, str(name), offsets)
