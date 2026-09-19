"""``derive_merge_rasters``: two overlapping surfaces -> one, the primary winning.

A measurement that covers part of the ground and a wider surface that covers the
rest are ONE surface only where a caller says which wins: the primary paints
every cell it measured and the fallback paints what is left. Both are read onto
one grid at the finer of the two cell sizes, and a sidecar records which input
painted each cell so a reader can see where the measurement stopped. Two
surfaces counting from DIFFERENT zeros meet only through an offset somebody
measured - named on the call, or published by the primary about itself - and a
primary counting DEPTHS down from its zero is read as elevations up from it
before either surface is read onto the common grid.
"""

from __future__ import annotations

import logging
import math
import tempfile
import uuid
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.inputs.geometry import source_uri
from trid3nt_server.inputs.vertical_datum import (
    Alignment, DatumError, align, datum_of, published_offset)
from trid3nt_server.tools import register_tool
from trid3nt_server.tools.derive import DeriveError
from trid3nt_server.tools.derive._hydrology_common import _stage_uri_local, write_cog

__all__ = ["MergeRastersError", "MergedRasterLayerURI", "derive_merge_rasters"]

logger = logging.getLogger(
    "trid3nt_server.tools.derive.derive_merge_rasters.derive_merge_rasters")


class MergeRastersError(DeriveError):
    """A typed refusal: ``MERGE_RASTERS_NO_SOURCE``, ``MERGE_RASTERS_UNREADABLE``,
    ``MERGE_RASTERS_DISJOINT`` (the two cover no common ground),
    ``MERGE_RASTERS_RESOLUTION_INVALID`` (a grid past the cell ceiling),
    ``MERGE_RASTERS_WRITE_FAILED``. ``MERGE_RASTERS_DATUM_UNSTATED``,
    ``MERGE_RASTERS_DATUMS_DIFFER`` and ``MERGE_RASTERS_DATUM_OFFSET_MISMATCH``
    come from the datum check itself.
    """


class MergedRasterLayerURI(LayerURI):
    """The merged surface, with WHAT PAINTED IT rather than what was offered."""

    #: The share of the merged cells each input actually painted.
    primary_fraction: float = 0.0
    fallback_fraction: float = 0.0
    #: The single-band raster carrying which input won at each cell - 0 primary,
    #: 1 fallback, nodata where neither measured. A sidecar rather than a second
    #: band, so the surface stays the one-band grid every sampler reads.
    provenance_uri: str | None = None
    resolution_m: float = 0.0
    #: The metres added to the primary to read it on the fallback's datum, which
    #: is zero wherever the two already counted from one zero.
    datum_shift_m: float = 0.0
    notes: list[str] = []


#: The surface is an elevation, so it draws as one.
_STYLE = {"kind": "continuous", "ramp": "terrain"}

#: The most cells one merge will build, and what a grid past it says. A ceiling
#: refuses by name rather than quietly coarsening the measurement it was called
#: to keep.
_MAX_CELLS = 60_000_000

#: What a surface of DEPTHS names its quantity as. A depth is counted DOWN from
#: the surface's own zero and an elevation UP from it, so a primary stating this
#: reaches the merge only through the flip below.
_DEPTH_QUANTITY = "depth_below_datum"

_NODATA = float("nan")
_PROVENANCE_NODATA = 255

_METADATA = AtomicToolMetadata(
    name="derive_merge_rasters",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


def _staged(layer: Any, role: str, tmpdir: str) -> str:
    """One input raster, local and readable, or a refusal naming it."""
    uri = str(source_uri(layer) or "").strip()
    if not uri:
        raise MergeRastersError(
            "MERGE_RASTERS_NO_SOURCE",
            f"the {role} {layer!r} names no raster file to read.")
    try:
        return _stage_uri_local(uri, tmpdir, role)
    except Exception as exc:  # noqa: BLE001 - every reader fault, named by source
        raise MergeRastersError(
            "MERGE_RASTERS_UNREADABLE",
            f"the {role} raster {uri!r} could not be read ({exc}).") from exc


def _metres_per_unit(crs: Any) -> float:
    """How many metres one unit of this CRS spans, for a degree grid or a metre
    one. A projected CRS in feet is not one either of these sources ships."""
    return 1.0 if crs is not None and crs.is_projected else 111_320.0


def _grid(sources: list[Any], resolution_m: float | None
          ) -> tuple[Any, int, int, Any, float]:
    """The common grid both inputs are read onto: the CRS and cell of the finest
    source, over the union of what they cover."""
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.transform import from_origin

    finest = min(sources, key=lambda src: min(abs(v) for v in src.res)
                 * _metres_per_unit(src.crs))
    crs = finest.crs
    cell = (float(resolution_m) / _metres_per_unit(crs) if resolution_m
            else min(abs(v) for v in finest.res))
    if not math.isfinite(cell) or cell <= 0.0:
        raise MergeRastersError(
            "MERGE_RASTERS_RESOLUTION_INVALID",
            f"resolution_m must be a positive number of metres; got {resolution_m!r}.")
    boxes = [transform_bounds(src.crs, crs, *src.bounds, densify_pts=21)
             for src in sources]
    west = min(b[0] for b in boxes)
    south = min(b[1] for b in boxes)
    east = max(b[2] for b in boxes)
    north = max(b[3] for b in boxes)
    width = max(1, int(math.ceil((east - west) / cell)))
    height = max(1, int(math.ceil((north - south) / cell)))
    if width * height > _MAX_CELLS:
        raise MergeRastersError(
            "MERGE_RASTERS_RESOLUTION_INVALID",
            f"merging these two over their union at {cell * _metres_per_unit(crs):.3g} m "
            f"is {width} x {height} = {width * height} cells, past the "
            f"{_MAX_CELLS}-cell ceiling. State a coarser resolution_m, or merge "
            "surfaces that cover less ground.")
    return crs, width, height, from_origin(west, north, cell, cell), cell


def _onto(src: Any, crs: Any, width: int, height: int, transform: Any,
          values: Any = None) -> Any:
    """One source read onto the common grid, nodata where it measured nothing.

    ``values`` is that source's own grid re-zeroed before the read, which is
    what the caller passes where the two surfaces did not count the same way."""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    out = np.full((height, width), _NODATA, dtype="float32")
    reproject(source=(rasterio.band(src, 1) if values is None else values),
              destination=out,
              src_transform=src.transform, src_crs=src.crs,
              dst_transform=transform, dst_crs=crs,
              src_nodata=(src.nodata if values is None else _NODATA),
              dst_nodata=_NODATA,
              resampling=Resampling.bilinear)
    return out


def _counts_down(layer: Any) -> bool:
    """Does this surface state its values as DEPTHS below its own zero?"""
    return str(getattr(layer, "quantity", "") or "").startswith(_DEPTH_QUANTITY)


def _as_elevations(src: Any, offset_m: float) -> Any:
    """A surface of DEPTHS, read as ELEVATIONS on the datum the merge lands on.

    A depth counted down from its own zero and an elevation counted up from
    another's are one axis only through ``offset - depth``. It happens on the
    source's OWN grid, before either surface is read onto the common one, so the
    cells this merge lays down carry the measurement's own values re-zeroed."""
    import numpy as np

    read = src.read(1, masked=True).filled(_NODATA).astype("float32")
    return np.float32(offset_m) - read


def _journal(primary: Any, aligned: Alignment, *, depths: bool) -> None:
    """What the run SAYS about the surface this merge moved onto the other's zero.

    On the run journal and not in a log line: the shift a bed was moved by is
    part of the answer, and a reader of the packet has to see it."""
    from trid3nt_server.workflows.runtime import journal_note

    zero = datum_of(primary) or "its own datum"
    counted = (f"the measurement is a surface of DEPTHS below {zero}, read as "
               f"elevations counted up from it" if depths else
               f"the measurement is a surface of elevations on {zero}")
    journal_note(f"{counted}, and it is {aligned.note}.")


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
    import numpy as np
    import rasterio

    if fallback is None and primary is None:
        raise MergeRastersError(
            "MERGE_RASTERS_NO_SOURCE",
            "derive_merge_rasters was given neither surface, so there is "
            "nothing to merge.")
    if primary is None or fallback is None:
        return _passed_through(primary if fallback is None else fallback,
                               absent="primary" if primary is None else "fallback")
    aligned = _aligned(primary, fallback, offset)
    depths = _counts_down(primary)

    seed = uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="merge-rasters-") as tmpdir:
        top = _staged(primary, "primary", tmpdir)
        under = _staged(fallback, "fallback", tmpdir)
        with rasterio.open(top) as a, rasterio.open(under) as b:
            crs, width, height, transform, cell = _grid([a, b], resolution_m)
            over = _onto(a, crs, width, height, transform,
                         _as_elevations(a, aligned.shift_m) if depths else None)
            below = _onto(b, crs, width, height, transform)
        if aligned.shift_m and not depths:
            over = over + np.float32(aligned.shift_m)
        won = np.where(np.isfinite(over), 0,
                       np.where(np.isfinite(below), 1, _PROVENANCE_NODATA))
        merged = np.where(np.isfinite(over), over, below).astype("float32")
        painted = int((won != _PROVENANCE_NODATA).sum())
        if not painted:
            raise MergeRastersError(
                "MERGE_RASTERS_DISJOINT",
                "neither surface measured a single cell of the grid they span "
                "together, so there is nothing to merge. Name two surfaces over "
                "the same ground.")
        metres = float(cell * _metres_per_unit(crs))
        uri = write_cog(merged, crs=crs, transform=transform, prefix="merged_bed",
                        seed=seed, output_dir=_output_dir,
                        code="MERGE_RASTERS_WRITE_FAILED", nodata=_NODATA)
        provenance = write_cog(won.astype("uint8"), crs=crs, transform=transform,
                               prefix="merged_bed_source", seed=seed,
                               output_dir=_output_dir,
                               code="MERGE_RASTERS_WRITE_FAILED",
                               nodata=float(_PROVENANCE_NODATA))

    top_share = float((won == 0).sum()) / float(won.size)
    under_share = float((won == 1).sum()) / float(won.size)
    read_as = (f"The primary counted DEPTHS below {datum_of(primary)} and was "
               f"{aligned.note}." if depths else f"The primary was {aligned.note}.")
    notes = [
        f"The primary painted {top_share * 100.0:.1f}% of the merged grid and "
        f"the fallback {under_share * 100.0:.1f}%; "
        f"{(1.0 - top_share - under_share) * 100.0:.1f}% is measured by neither "
        "and left as nodata.",
        f"Merged at {metres:.3g} m in {crs}, the finer of the two inputs unless "
        "a resolution was stated.",
        read_as if (depths or aligned.shift_m)
        else f"Both surfaces count from {aligned.datum}.",
    ]
    logger.info("derive_merge_rasters: %dx%d at %.3g m, primary %.1f%% / "
                "fallback %.1f%%", width, height, metres, top_share * 100.0,
                under_share * 100.0)
    if depths or aligned.shift_m:
        _journal(primary, aligned, depths=depths)
    return MergedRasterLayerURI(
        layer_id=f"merged-bed-{seed}",
        name="merged bed surface",
        layer_type="raster",
        uri=uri,
        style=_STYLE,
        role="primary",
        units=getattr(primary, "units", None) or getattr(fallback, "units", None),
        # A primary read the other way round no longer carries the quantity it
        # named, so the merged surface states the one it was read onto.
        quantity=(getattr(fallback, "quantity", None) if depths
                  else getattr(primary, "quantity", None)
                  or getattr(fallback, "quantity", None)),
        bbox=_bbox_4326(crs, transform, width, height),
        vertical_datum=aligned.datum or None,
        datum_shift_m=round(float(aligned.shift_m), 4),
        primary_fraction=round(top_share, 4),
        fallback_fraction=round(under_share, 4),
        provenance_uri=provenance,
        resolution_m=round(metres, 4),
        notes=notes)


def _bbox_4326(crs: Any, transform: Any, width: int, height: int
               ) -> tuple[float, float, float, float]:
    """The lon/lat box the merged grid spans, so the camera can fly to it."""
    from rasterio.warp import transform_bounds

    west, north = transform.c, transform.f
    east = west + width * transform.a
    south = north + height * transform.e
    return tuple(float(v) for v in transform_bounds(
        crs, "EPSG:4326", west, south, east, north, densify_pts=21))


def _aligned(primary: Any, fallback: Any, offset: Any) -> Alignment:
    """The zero both surfaces end up counting from, and what it cost to get there.

    The FALLBACK's datum is the one the merge lands on: it is the wider surface,
    so every cell the primary does not paint is already on it. An offset the
    call does not state is the one the PRIMARY publishes about itself - a survey
    measured on a district's project datum states in its own metadata how far
    that zero sits above a national frame, and no service serves that datum."""
    try:
        return align(primary, fallback,
                     offset=offset if offset is not None else published_offset(primary),
                     code_prefix="MERGE_RASTERS_")
    except DatumError as exc:
        raise MergeRastersError(exc.error_code, str(exc)) from exc


def _passed_through(only: Any, absent: str) -> MergedRasterLayerURI:
    """The one surface there is, carried through under the merge's own shape.

    An absent row is an answer - a reach with no published survey has a terrain
    bed and nothing else - and the result says which side was missing rather
    than presenting one input as a merge of two."""
    uri = str(source_uri(only) or "").strip()
    if not uri:
        raise MergeRastersError(
            "MERGE_RASTERS_NO_SOURCE",
            f"only the {'fallback' if absent == 'primary' else 'primary'} "
            f"surface was given and it names no raster file to read ({only!r}).")
    seed = uuid.uuid4().hex[:8]
    note = (f"The {absent} surface was absent, so the other one passed through "
            "unchanged: this is that surface, not a merge of two.")
    logger.info("derive_merge_rasters: %s absent, %s passed through", absent, uri)
    return MergedRasterLayerURI(
        layer_id=f"merged-bed-{seed}",
        name=getattr(only, "name", None) or "merged bed surface",
        layer_type="raster",
        uri=uri,
        style=getattr(only, "style", None) or _STYLE,
        role="primary",
        units=getattr(only, "units", None),
        quantity=getattr(only, "quantity", None),
        bbox=getattr(only, "bbox", None),
        vertical_datum=getattr(only, "vertical_datum", None),
        primary_fraction=0.0 if absent == "primary" else 1.0,
        fallback_fraction=1.0 if absent == "primary" else 0.0,
        resolution_m=0.0,
        notes=[note])
