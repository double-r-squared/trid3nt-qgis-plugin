"""``derive_raster_mean`` - the mean of a raster's real pixels over an area.

A gridded field states a value per cell; a forcing, a rate or a budget wants one
number for the ground a run is solved over. Nodata and non-finite cells are not
counted and never read as zero: a grid with no real pixel over the area refuses
rather than returning a mean of nothing.
"""
from __future__ import annotations

import logging
import tempfile
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.inputs.geometry import source_uri
from trid3nt_server.tools import register_tool
from trid3nt_server.tools.derive._hydrology_common import _stage_uri_local

__all__ = ["RasterMeanError", "derive_raster_mean"]

logger = logging.getLogger(
    "trid3nt_server.tools.derive.derive_raster_mean.derive_raster_mean")

_METADATA = AtomicToolMetadata(
    name="derive_raster_mean",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


class RasterMeanError(RuntimeError):
    """A typed refusal: ``DERIVE_RASTER_MEAN_EMPTY`` (no real pixel over the
    area), ``_NO_OVERLAP`` or ``_SOURCE_UNREADABLE``."""

    error_code: str
    retryable: bool = False

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _clip_geometries(within: Any) -> list[dict[str, Any]]:
    """The polygons the mean is taken over, in EPSG:4326."""
    from trid3nt_server.inputs.geometry import flatten_geometries, read_geometry_doc

    return [g for g in flatten_geometries(read_geometry_doc(within))
            if str(g.get("type") or "") in ("Polygon", "MultiPolygon")]


def _values(path: str, band: int, within: Any) -> Any:
    """The finite, non-nodata band values, clipped to ``within`` when given."""
    import numpy as np
    import rasterio
    from rasterio.mask import mask as rio_mask

    with rasterio.open(path) as src:
        if within is None:
            arr = src.read(band, masked=True)
        else:
            shapes = _clip_geometries(within)
            if not shapes:
                raise RasterMeanError(
                    "DERIVE_RASTER_MEAN_NO_OVERLAP",
                    f"the area {within!r} carries no polygon, so there is no "
                    "ground to take the mean over.")
            try:
                arr = rio_mask(src, shapes, crop=True, filled=False)[0][band - 1]
            except ValueError as exc:
                raise RasterMeanError(
                    "DERIVE_RASTER_MEAN_NO_OVERLAP",
                    f"the area does not overlap the raster at all ({exc}).") from exc
    vals = np.asarray(arr.compressed() if hasattr(arr, "compressed") else arr,
                      dtype="float64")
    return vals[np.isfinite(vals)]


@register_tool(
    _METADATA,
    read_only_hint=True,
    # Reads only the raster it is handed.
    open_world_hint=False,
)
def derive_raster_mean(
    layer: Any,
    within: Any = None,
    band: int = 1,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """AVERAGE a raster over its area -> one number, with the pixels it counted.

    ROUTING: "what is the mean rainfall over this catchment", "average this
    grid over the domain", "one number for this field over the area I am
    modelling". Use it wherever a gridded field has to become the single value
    a forcing, a rate or a budget is stated as.

    Do NOT use for: a value at ONE point (``probe_point``), or a time series at
    a point (``extract_timeseries_at_point``).

    Params:
        layer: the raster to average - a fetched raster layer or its uri.
        within: OPTIONAL polygons to take the mean over (a vector layer or
            inline GeoJSON). Omitted, the whole raster is averaged.
        band: the band to read, 1-based.

    Returns the ``mean``, the ``count`` of real pixels it came from, the
    ``min`` and ``max`` they span, and a note. Nodata and non-finite cells are
    not counted; a raster with no real pixel over the area refuses rather than
    reporting zero.
    """
    from trid3nt_server.workflows.runtime import journal_note

    uri = str(source_uri(layer) or "").strip()
    if not uri:
        raise RasterMeanError(
            "DERIVE_RASTER_MEAN_SOURCE_UNREADABLE",
            f"the raster {layer!r} names no file to read.")
    with tempfile.TemporaryDirectory(prefix="raster-mean-") as tmpdir:
        try:
            path = _stage_uri_local(uri, tmpdir, "raster")
        except Exception as exc:  # noqa: BLE001 - every reader fault, named by source
            raise RasterMeanError(
                "DERIVE_RASTER_MEAN_SOURCE_UNREADABLE",
                f"the raster {uri!r} could not be read ({exc}).") from exc
        vals = _values(path, int(band), within)
    if vals.size == 0:
        raise RasterMeanError(
            "DERIVE_RASTER_MEAN_EMPTY",
            f"the raster {uri!r} carries no real pixel over this area - every "
            "cell is nodata or non-finite - so its mean is not measured. A "
            "mean of nothing is not zero.")
    mean = float(vals.mean())
    note = (f"raster mean {mean:.6g} over {int(vals.size)} real pixels "
            f"(min {float(vals.min()):.6g}, max {float(vals.max()):.6g})"
            + (" inside the area given." if within is not None else "."))
    journal_note(note)
    logger.info("derive_raster_mean: %.6g over %d pixels", mean, int(vals.size))
    return {"mean": mean, "count": int(vals.size), "min": float(vals.min()),
            "max": float(vals.max()), "note": note}
