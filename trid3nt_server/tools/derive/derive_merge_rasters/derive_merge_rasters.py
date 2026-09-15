"""``derive_merge_rasters``: two overlapping surfaces -> one, the primary winning.

A measurement that covers part of the ground and a wider surface that covers the
rest are ONE surface only where a caller says which wins: the primary paints
every cell it measured and the fallback paints what is left. Both are read onto
one grid at the finer of the two cell sizes, and a sidecar records which input
painted each cell so a reader can see where the measurement stopped.
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
from trid3nt_server.inputs.vertical_datum import DatumError, one_datum
from trid3nt_server.tools import register_tool
from trid3nt_server.tools.derive._hydrology_common import _stage_uri_local, write_cog

__all__ = ["MergeRastersError", "MergedRasterLayerURI", "derive_merge_rasters"]

logger = logging.getLogger(
    "trid3nt_server.tools.derive.derive_merge_rasters.derive_merge_rasters")


class MergeRastersError(RuntimeError):
    """A typed refusal: ``MERGE_RASTERS_NO_SOURCE``, ``MERGE_RASTERS_UNREADABLE``,
    ``MERGE_RASTERS_DISJOINT`` (the two cover no common ground),
    ``MERGE_RASTERS_RESOLUTION_INVALID`` (a grid past the cell ceiling),
    ``MERGE_RASTERS_WRITE_FAILED``. ``MERGE_RASTERS_DATUM_UNSTATED`` and
    ``MERGE_RASTERS_DATUMS_DIFFER`` come from the datum check itself.
    """

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


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
    notes: list[str] = []


#: The surface is an elevation, so it draws as one.
_STYLE = {"kind": "continuous", "ramp": "terrain"}

#: The most cells one merge will build, and what a grid past it says. A ceiling
#: refuses by name rather than quietly coarsening the measurement it was called
#: to keep.
_MAX_CELLS = 60_000_000

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


def _onto(src: Any, crs: Any, width: int, height: int, transform: Any) -> Any:
    """One source read onto the common grid, nodata where it measured nothing."""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    out = np.full((height, width), _NODATA, dtype="float32")
    reproject(source=rasterio.band(src, 1), destination=out,
              src_transform=src.transform, src_crs=src.crs,
              dst_transform=transform, dst_crs=crs,
              src_nodata=src.nodata, dst_nodata=_NODATA,
              resampling=Resampling.bilinear)
    return out


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

    The two must count their elevations from the SAME vertical datum. A survey
    stating a local project datum and a DEM stating NAVD88 are metres apart on
    the same ground, so the merge refuses by name rather than producing a
    surface with a step in it.

    Do NOT use for: mosaicking tiles of ONE dataset (a fetcher's own ladder does
    that), or resampling a single raster.

    Params:
        primary: the surface that WINS where it has data - the measurement.
            Absent or empty, the fallback passes through unchanged and the
            result says so.
        fallback: the surface that carries the rest.
        resolution_m: OPTIONAL cell size in metres for the merged grid. Default
            is the finer of the two inputs', which keeps the measurement.

    Returns the merged surface as a single-band raster, with the share each
    input painted, the sidecar naming which won at each cell, and the datum both
    count from.
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
    datum = _one_datum(primary, fallback)

    seed = uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix="merge-rasters-") as tmpdir:
        top = _staged(primary, "primary", tmpdir)
        under = _staged(fallback, "fallback", tmpdir)
        with rasterio.open(top) as a, rasterio.open(under) as b:
            crs, width, height, transform, cell = _grid([a, b], resolution_m)
            over = _onto(a, crs, width, height, transform)
            below = _onto(b, crs, width, height, transform)
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
    notes = [
        f"The primary painted {top_share * 100.0:.1f}% of the merged grid and "
        f"the fallback {under_share * 100.0:.1f}%; "
        f"{(1.0 - top_share - under_share) * 100.0:.1f}% is measured by neither "
        "and left as nodata.",
        f"Merged at {metres:.3g} m in {crs}, the finer of the two inputs unless "
        "a resolution was stated.",
        f"Both surfaces count from {datum}." if datum else
        "Neither surface states what it counts from.",
    ]
    logger.info("derive_merge_rasters: %dx%d at %.3g m, primary %.1f%% / "
                "fallback %.1f%%", width, height, metres, top_share * 100.0,
                under_share * 100.0)
    return MergedRasterLayerURI(
        layer_id=f"merged-bed-{seed}",
        name="merged bed surface",
        layer_type="raster",
        uri=uri,
        style=_STYLE,
        role="primary",
        units=getattr(primary, "units", None) or getattr(fallback, "units", None),
        quantity=getattr(primary, "quantity", None)
        or getattr(fallback, "quantity", None),
        bbox=_bbox_4326(crs, transform, width, height),
        vertical_datum=datum or None,
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


def _one_datum(primary: Any, fallback: Any) -> str:
    """The zero BOTH surfaces count from, or the refusal naming the two."""
    try:
        return one_datum(primary, fallback, code_prefix="MERGE_RASTERS_")
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
