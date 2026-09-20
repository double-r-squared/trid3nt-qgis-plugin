"""``derive_merge_rasters`` - the registered name of the bed's merge.

The rule itself is the bed slot's own: only that slot composes one bed out of a
measurement and the wider surface under it, and it lives in
``inputs.bed.merged_surface``. This shim is here while declared plan steps still
name the tool.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.inputs.bed import (
    MergedRasterLayerURI,
    MergeRastersError,
    merged_surface,
)
from trid3nt_server.tools import register_tool

__all__ = ["MergeRastersError", "MergedRasterLayerURI", "derive_merge_rasters"]

_METADATA = AtomicToolMetadata(
    name="derive_merge_rasters",
    # The bed ingestion runs this producer by registry name: resolvable, never
    # model-facing, so it states no coverage row and carries no corpus.
    tier="internal",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    # Reads only the two layers it is handed and writes its own artifacts.
    open_world_hint=False,
)
def derive_merge_rasters(
    primary: Any = None,
    fallback: Any = None,
    resolution_m: float | None = None,
    offset: Any = None,
    *,
    _output_dir: str | None = None,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> MergedRasterLayerURI:
    """MERGE two overlapping surfaces into one, the PRIMARY winning where it measured.

    ROUTING: "merge the channel survey over the DEM", "lay this bathymetry on
    the terrain", "fill the holes in this survey from that wider surface", "one
    bed raster out of these two grids". Use it wherever a detailed measurement
    covers part of the ground and a wider surface has to carry the rest.

    Both inputs are read onto one grid at the finer of their two cell sizes,
    over the union of what they cover. The primary paints every cell it
    measured; the fallback paints what is left; a cell neither measured stays
    nodata. Which input won at each cell is written as a sidecar raster.

    The two must count their elevations from the SAME vertical datum, or an
    OFFSET between them must be stated - on the call, or by the primary about
    itself, which is where a survey on a district's project datum publishes it.
    A survey on a local project datum and a DEM on NAVD88 are metres apart on
    the same ground, so without that offset the merge refuses by name rather
    than producing a surface with a step in it; with it, the primary is read on
    the fallback's datum and the result says by how much and on whose authority.

    A primary whose quantity states DEPTHS below its zero is read as ELEVATIONS
    counted up from it on the way, because a depth counted down and an elevation
    counted up are one surface only through that flip.

    Do NOT use for: mosaicking tiles of ONE dataset (a fetcher's own ladder does
    that), or resampling a single raster.

    Params:
        primary: the surface that WINS where it has data - the measurement.
            Absent or empty, the fallback passes through unchanged and the
            result says so.
        fallback: the surface that carries the rest.
        resolution_m: OPTIONAL cell size in metres for the merged grid. Default
            is the finer of the two inputs', which keeps the measurement.
        offset: the measured offset between the two vertical datums, needed only
            when they differ and the primary publishes none of its own. The
            record ``fetch_vertical_datum_offset`` returns, or a shift as
            metres. Positive means the primary's zero sits ABOVE the
            fallback's.

    Returns the merged surface as a single-band raster, with the share each
    input painted, the sidecar naming which won at each cell, the datum both end
    up on and the shift that got them there.
    """
    return merged_surface(primary, fallback, resolution_m=resolution_m,
                          offset=offset, _output_dir=_output_dir)
