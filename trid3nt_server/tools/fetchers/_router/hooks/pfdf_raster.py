"""pfdf raster-delegate hooks: USGS readers whose library owns the socket.

pfdf ships maintained readers for the USGS TNM 3DEP DEM and the STATSGO soils COG
collection, each owning discovery and the socket. A hook returns ``(array_float32,
transform, crs)``; the coverage envelope is each row's own gate."""

from __future__ import annotations

import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_empty_error, router_input_error, router_upstream_error
from . import register_hook

__all__ = ["read_statsgo", "read_3dep"]

def _raster_to_array(spec: SourceSpec, raster: Any) -> tuple[Any, Any, Any]:
    """pfdf ``Raster`` -> ``(float32 array, affine, crs)`` with nodata masked to NaN."""
    import numpy as np

    arr = np.asarray(raster.values, dtype="float32")
    if arr.ndim == 3 and arr.shape[0] == 1:
        arr = arr[0]
    nod = getattr(raster, "nodata", None)
    if nod is not None and not (isinstance(nod, float) and math.isnan(nod)):
        try:
            arr = np.where(arr == float(nod), np.nan, arr)
        except (TypeError, ValueError):
            pass
    return arr, raster.affine, raster.crs




@register_hook("pfdf_statsgo.read")
def read_statsgo(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> tuple[Any, Any, Any]:
    """Read a STATSGO field COG via pfdf to ``(array, affine, crs)``. An all-NaN window
    inside CONUS -- open water, a Great Lakes pocket -- is a typed EMPTY; a library
    failure reaches the invoke wrapper's verbatim upstream backstop."""
    sc = spec.error_code_prefix
    field = params["field"]
    bbox = [float(v) for v in params["bbox"]]
    try:
        from pfdf.data.usgs import statsgo
        from pfdf.projection import BoundingBox
        import rioxarray  # noqa: F401 -- registers the .rio accessor for downstream reuse
    except Exception as exc:  # noqa: BLE001
        raise router_upstream_error(sc, f"pfdf / rioxarray unavailable: {exc}")

    raster = statsgo.read(field, BoundingBox(bbox[0], bbox[1], bbox[2], bbox[3], crs=4326), timeout=timeout_s)
    arr, affine, crs = _raster_to_array(spec, raster)

    import numpy as np

    if arr.size == 0 or bool(np.all(np.isnan(arr))):
        raise router_empty_error(
            sc,
            f"STATSGO field={field} bbox={tuple(bbox)} returned no pixels "
            f"(likely open water or outside STATSGO coverage)",
            spec.empty_error_suffix,
        )
    return arr, affine, crs




@register_hook("pfdf_3dep.read")
def read_3dep(spec: SourceSpec, params: dict[str, Any], *, timeout_s: float) -> tuple[Any, Any, Any]:
    """Read a 3DEP DEM tile mosaic via pfdf TNM to ``(array, affine, crs)``. A
    zero-coverage resolution is EMPTY (a coarser one may cover), a tile-count overrun
    is INPUT, and anything else reaches the invoke wrapper's upstream backstop."""

    # There is no all-NaN empty gate here: the library's own no-products error is the
    # only empty signal 3DEP gives.
    sc = spec.error_code_prefix
    resolution = params["resolution"]
    max_tiles = int(params["max_tiles"])
    bbox = [float(v) for v in params["bbox"]]
    try:
        from pfdf.data.usgs.tnm import dem
        from pfdf.projection import BoundingBox
        import rioxarray  # noqa: F401 -- registers the .rio accessor for downstream reuse
    except Exception as exc:  # noqa: BLE001
        raise router_upstream_error(sc, f"pfdf / rioxarray unavailable: {exc}")

    try:
        raster = dem.read(
            BoundingBox(bbox[0], bbox[1], bbox[2], bbox[3], crs=4326),
            resolution=resolution,
            max_tiles=max_tiles,
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 -- lowercased-message dispatch
        msg = str(exc).lower()
        if "noproducts" in msg.replace(" ", "") or "no tnm products" in msg:
            raise router_empty_error(
                sc,
                f"3DEP {resolution} has no TNM products covering bbox={tuple(bbox)}; "
                "try a different resolution or expand the bbox",
                spec.empty_error_suffix,
            )
        if "too many" in msg or ("tile" in msg and "limit" in msg):
            raise router_input_error(
                sc,
                f"3DEP {resolution} request would exceed max_tiles={max_tiles} "
                f"for bbox={tuple(bbox)}; raise max_tiles or shrink the bbox: {exc}",
                spec.input_error_suffix,
            )
        raise router_upstream_error(
            sc,
            f"pfdf.data.usgs.tnm.dem.read failed for resolution={resolution} "
            f"bbox={tuple(bbox)}: {exc}",
        )
    return _raster_to_array(spec, raster)
