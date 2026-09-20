"""CHS NONNA-10 delegate hooks: the published WCS coverage onto the AOI lattice.

The mosaic's native frame is EPSG:3857 and GeoServer refuses a GetCoverage whose
BBOX is stated in another one, so the AOI is reprojected into 3857 and
RESPONSE_CRS asks for the answer back on 4326. NONNA is a survey mosaic rather
than a filled grid: a box CHS surveyed nothing in answers HTTP 200 with an
all-nodata raster, so emptiness is read off the array and never off the status."""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ..._fetch_common import bbox_pixel_dims, enforce_pixel_budget
from ..._router import hooks as _hooks
from ..._router.errors import (
    router_empty_error, router_input_error, router_upstream_error)

__all__ = ["validate", "read"]

#: The coverage's own no-value sentinel, as DescribeCoverage states it. The
#: service resamples onto the asked lattice, so a cell straddling the edge of a
#: survey can come back near it rather than on it: at or above is the fill.
NODATA = 3.4028234663852886e38


def _bbox(spec: SourceSpec, params: dict[str, Any]) -> tuple[float, ...]:
    """The AOI as four ordered finite numbers, or this source's typed input error."""
    raw = params.get("bbox")
    try:
        bbox = tuple(float(v) for v in raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise router_input_error(
            spec.error_code_prefix, f"bbox must be four numbers; got {raw!r}",
            spec.input_error_suffix)
    if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise router_input_error(
            spec.error_code_prefix,
            f"bbox must be (min_lon, min_lat, max_lon, max_lat) with each min "
            f"strictly below its max; got {raw!r}", spec.input_error_suffix)
    return bbox


@_hooks.register_hook("chs_nonna.validate")
def validate(spec: SourceSpec, params: dict[str, Any]) -> None:
    """Bbox shape and the service's per-axis pixel budget, before the cache and
    before the network. The asked spacing is the spacing fetched, so a bbox that
    does not fit refuses naming the spacing that would."""
    bbox = _bbox(spec, params)
    wcs = (spec.ingest or {}).get("wcs", {})
    enforce_pixel_budget(
        bbox, float(params.get("resolution_m") or 10.0),
        budget_px=int(wcs.get("max_px", 4000)), source=spec.name)


@_hooks.register_hook("chs_nonna.read")
def read(spec: SourceSpec, params: dict[str, Any], *,
         timeout_s: float) -> tuple[Any, Any, Any]:
    """One WCS GetCoverage over the AOI, as ``(elevation array, transform, crs)``."""
    import numpy as np
    import rasterio
    from pyproj import Transformer

    from ..._router.transport.ogc_adapter import OGCAdapterError, fetch_ogc_layer

    wcs = (spec.ingest or {}).get("wcs", {})
    bbox = _bbox(spec, params)
    native = str(wcs.get("request_crs", "EPSG:3857"))
    width_px, height_px = bbox_pixel_dims(
        bbox, float(params.get("resolution_m") or 10.0),
        px_max=int(wcs.get("max_px", 4000)))
    into_native = Transformer.from_crs(
        spec.normalize.crs, native, always_xy=True).transform
    west, south = into_native(bbox[0], bbox[1])
    east, north = into_native(bbox[2], bbox[3])

    endpoint = spec.endpoints["data"]
    try:
        resp = fetch_ogc_layer(
            url=str(endpoint.url), layer_name=str(wcs["coverage"]),
            bbox=(west, south, east, north), crs=native, service_type="WCS",
            image_format=str(wcs.get("image_format", "GeoTIFF")),
            version=str(wcs.get("version", "1.0.0")),
            width_px=width_px, height_px=height_px, timeout_s=timeout_s,
            user_agent=spec.auth.user_agent,
            extra_params={"RESPONSE_CRS": spec.normalize.crs})
    except OGCAdapterError as exc:
        raise router_upstream_error(
            spec.error_code_prefix,
            f"CHS NONNA WCS GetCoverage failed for bbox={bbox}: {exc}")
    if "tiff" not in (resp.content_type or "").lower():
        raise router_upstream_error(
            spec.error_code_prefix,
            f"CHS NONNA WCS answered content-type={resp.content_type!r} for "
            f"bbox={bbox}; body preview: {resp.content[:200]!r}")

    with rasterio.io.MemoryFile(resp.content) as mem, mem.open() as src:
        arr = src.read(1).astype("float32")
        transform = src.transform
        crs = src.crs
        fill = float(src.nodata) if src.nodata is not None else NODATA
    arr[~np.isfinite(arr) | (arr >= fill)] = np.nan
    if not np.isfinite(arr).any():
        raise router_empty_error(
            spec.error_code_prefix,
            f"bbox={bbox} carries no NONNA sounding - CHS has surveyed nothing "
            "here, and the service answers such a box with an all-nodata "
            "raster rather than an error", spec.empty_error_suffix)
    return arr, transform, crs
