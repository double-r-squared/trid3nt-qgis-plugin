"""``merge_rasters`` - several surfaces laid into ONE, first wins, provenance kept.

Every input is read onto ONE vertical frame - the last input's datum, as each
layer states its own - through the shift it publishes about itself or an offset
handed in, before any of them paints a cell; a derive never fetches one. The second band names which input each cell
came from, and the share each reached is the feedback.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from trid3nt_contracts.execution import layer_seed
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.derive._raster_layers import (
    NODATA, UNMEASURED, RasterLayerError, RasterLayerURI, bbox_4326, coverage,
    case_layer, label_of, staged, stated, written)

__all__ = ["merge_rasters", "merged"]

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


def _bridge(layer: Any, zero: str, offsets: list[Any]) -> Any:
    """The offset among ``offsets`` whose two frames are this input's zero and
    ``zero``, either way round, or ``None``."""
    from trid3nt_server.inputs.vertical_datum import datum_of, names_frame, offset_row

    here = datum_of(layer)
    for value in offsets:
        row = offset_row(value)
        if row is None:
            continue
        pair = (row.from_frame, row.to_frame)
        if row.names_frames and any(names_frame(here, a) and names_frame(zero, b)
                                    for a, b in (pair, pair[::-1])):
            return row
    return None


def _aligned(layer: Any, label: str, zero: str, offsets: list[Any]) -> Any:
    """What it costs to read ONE input on ``zero``: nothing on that frame, the
    shift it publishes about itself, or the offset handed in for its pair - else
    a refusal naming the fetch that measures one."""
    from trid3nt_server.inputs.vertical_datum import (
        OFFSET_FETCH, DatumError, datum_of, one_datum, onto_frame)

    try:
        if not zero:
            one_datum(layer, code_prefix="MERGE_RASTERS_")
        return onto_frame(layer, zero, offset=_bridge(layer, zero, offsets),
                          code_prefix="MERGE_RASTERS_")
    except DatumError as exc:
        if not exc.error_code.endswith("DATUMS_DIFFER"):
            raise RasterLayerError(exc.error_code, str(exc)) from exc
        raise RasterLayerError(
            "MERGE_RASTERS_OFFSET_UNSTATED",
            f"{label} counts from {datum_of(layer)} and this merge lands on "
            f"{zero}, the last input's zero, and no offset between the two was "
            f"handed in. Run {OFFSET_FETCH} for that pair at this ground and pass "
            "what it returns in offsets.") from exc


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


def merged(layers: list[Any], name: str, offsets: list[Any] = (),
           *, _output_dir: str | None = None) -> RasterLayerURI:
    """The overlay itself, over inputs already resolved: what the tool runs."""
    import tempfile
    from contextlib import ExitStack

    import numpy as np
    import rasterio
    from rasterio.merge import merge

    from trid3nt_server.inputs.bed import on_the_frame
    from trid3nt_server.inputs.vertical_datum import datum_of, published_offset
    from trid3nt_server.workflows.runtime import journal_note

    zero = datum_of(layers[-1])
    published = published_offset(layers[-1])
    labels = [label_of(layer, rank) for rank, layer in enumerate(layers)]
    aligned = [_aligned(layer, labels[rank], zero, offsets)
               for rank, layer in enumerate(layers)]
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
        quantity="elevation",
        bbox=bbox_4326(crs, transform, width, height),
        vertical_datum=zero or None,
        # The surface lands on the last input's zero, so the shift that input
        # publishes about it still reads this one onto its frame.
        datum_offset_m=published.metres if published else None,
        datum_offset_frame=published.to_frame if published else None,
        sources=labels, coverage=reached,
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
                        offsets: list[Any] | None = None,
                        **_extra_ignored: Any) -> RasterLayerURI:
    """LAY several rasters into ONE, first wins, and say which painted each cell.

    ROUTING: "merge the survey over the DEM", "combine these bathymetry layers",
    "lay the chart soundings first and the terrain under them", "mosaic these
    surfaces with this one on top", "fill the gaps in this raster from that one".
    A new case layer; the inputs are not changed.

    `layers` is the ORDER of priority, each a case layer id: the first paints
    every cell it measured, each after it only the cells the ones before left.
    Every input is read onto ONE vertical frame - the LAST input's own datum -
    through the shift it publishes about itself, or an offset in `offsets`:
    the records fetch_vertical_datum_offset returns, matched by the two frames
    each names. An input on another datum with no offset refuses, naming that
    fetch. A layer stating DEPTHS below its datum is read as elevations. Cell
    and CRS are the finest input's, over the union of all of them. Two
    populations farther apart than the last input's relief refuse as a cliff.

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
    held = [case_layer(layer, f"input {rank + 1}")
            for rank, layer in enumerate(listed)]
    stated_offsets = list(offsets) if isinstance(offsets, (list, tuple)) else \
        ([offsets] if offsets else [])
    return await asyncio.to_thread(merged, held, str(name), stated_offsets)
