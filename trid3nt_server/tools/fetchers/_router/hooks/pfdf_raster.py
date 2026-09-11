"""pfdf raster-delegate hooks: USGS readers whose library owns the socket.

pfdf ships maintained readers for the USGS TNM 3DEP DEM and the STATSGO soils COG
collection, each owning discovery and the socket. A hook returns ``(array_float32,
transform, crs)``, and its companion validate hook is the pre-cache coverage gate."""

from __future__ import annotations

import math
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..errors import router_empty_error, router_input_error, router_upstream_error
from . import register_hook

__all__ = [
    "validate_statsgo",
    "read_statsgo",
    "validate_3dep",
    "read_3dep",
]

#: STATSGO CONUS envelope: STATSGO does not cover Alaska, Hawaii or the territories.
#: Kept here rather than on the router's ``conus_only`` gate so the envelope is this
#: source's own.
_STATSGO_CONUS: tuple[float, float, float, float] = (-125.0, 24.0, -66.5, 49.5)

#: 3DEP US envelope: CONUS plus AK, HI and the territories. The live TNM query is the
#: authoritative coverage check; this is the loose gate before it.
_3DEP_US: tuple[float, float, float, float] = (-180.0, 13.0, -65.0, 72.0)


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




@register_hook("pfdf_statsgo.validate")
def validate_statsgo(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Pre-cache CONUS gate; the field itself is router-enum-validated."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    bbox = params.get("bbox")
    if not bbox or len(bbox) != 4:
        raise router_input_error(sc, f"bbox must be (min_lon,min_lat,max_lon,max_lat); got {bbox!r}", sfx)
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    if max_lon < _STATSGO_CONUS[0] or min_lon > _STATSGO_CONUS[2] or \
       max_lat < _STATSGO_CONUS[1] or min_lat > _STATSGO_CONUS[3]:
        raise router_input_error(
            sc,
            f"bbox {tuple(bbox)} does not intersect STATSGO CONUS envelope "
            f"{_STATSGO_CONUS}; STATSGO does not cover Alaska / Hawaii / territories",
            sfx,
        )


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




@register_hook("pfdf_3dep.validate")
def validate_3dep(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Pre-cache US-envelope gate: 3DEP is US-only. The router's shared validator
    already ran finite, range and degenerate checks, so this adds only the envelope
    intersection, kept here rather than approximated by a shared gate."""
    sc = spec.error_code_prefix
    sfx = spec.input_error_suffix
    bbox = params.get("bbox")
    if not bbox or len(bbox) != 4:
        raise router_input_error(sc, f"bbox must be (min_lon,min_lat,max_lon,max_lat); got {bbox!r}", sfx)
    min_lon, min_lat, max_lon, max_lat = (float(v) for v in bbox)
    if max_lon < _3DEP_US[0] or min_lon > _3DEP_US[2] or \
       max_lat < _3DEP_US[1] or min_lat > _3DEP_US[3]:
        raise router_input_error(
            sc,
            f"bbox {tuple(bbox)} does not intersect US envelope {_3DEP_US}; 3DEP is US-only",
            sfx,
        )


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
