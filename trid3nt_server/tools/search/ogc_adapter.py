"""The one OGC-dialect adapter: WMS, WMTS, WCS, WFS and the ESRI ArcGIS REST
shapes behind a single call. Dispatch is by the caller's explicit
``service_type``, NEVER by sniffing the URL, and every request states a CRS. A
failure re-raises as ``OGCAdapterError`` and writes no sentinel, so a caller never
caches a failure as if it were bytes."""

from __future__ import annotations

import logging
from typing import Any, Literal

import requests

from trid3nt_server.tools.fetchers._fetch_common import bbox_pixel_dims

__all__ = [
    "OGCAdapterError",
    "OGCResponse",
    "ServiceType",
    "fetch_ogc_layer",
    "DEFAULT_USER_AGENT",
]

logger = logging.getLogger("trid3nt_server.tools.search.ogc_adapter")

#: Cell size in metres for the grid a raster request derives from its bbox when it
#: gives no explicit width/height. Targeting a ground resolution rather than a fixed
#: pixel count is what keeps a large AOI from being silently coarsened; a caller
#: opts into finer or coarser through ``target_resolution_m``.
_DEFAULT_OGC_CELL_M = 30.0
#: Hard cap on each computed raster axis so a large AOI never materializes an
#: enormous grid (bounds the response payload). ``bbox_pixel_dims`` clamps to
#: this on both axes.
_OGC_PX_MAX = 4096

#: Recognized service flavors. ``ARCGIS_REST`` is the ESRI MapServer /
#: FeatureServer / ImageServer dialect: not strictly OGC, but the dominant surface
#: for the US federal catalogs, so the adapter treats it as a fourth dialect.
ServiceType = Literal["WMS", "WMTS", "WCS", "WFS", "ARCGIS_REST"]

# Conservative default User-Agent. A caller whose endpoint has a stated UA policy
# passes its own; the default is fine for public federal OGC endpoints.
DEFAULT_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent OGC adapter; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin)"
)


class OGCAdapterError(RuntimeError):
    """Adapter-level failure: an HTTP error, an OGC exception XML body, or an
    empty response. Retryable, and a call site wrapping the adapter behind a
    registered tool re-raises it as its own upstream error."""

    error_code: str = "UPSTREAM_API_ERROR"
    retryable: bool = True


class OGCResponse:
    """Raw bytes, content type and status from one adapter call. ``content_type``
    is the header verbatim - the caller picks the cache extension from it, since
    the dialect alone does not determine the payload format."""

    __slots__ = ("content", "content_type", "service_type", "url", "status_code")

    def __init__(
        self,
        content: bytes,
        content_type: str,
        service_type: ServiceType,
        url: str,
        status_code: int,
    ) -> None:
        self.content = content
        self.content_type = content_type
        self.service_type = service_type
        self.url = url
        self.status_code = status_code

    def __repr__(self) -> str:  # pragma: no cover - diagnostic
        return (
            f"OGCResponse(service={self.service_type}, bytes={len(self.content)}, "
            f"content_type={self.content_type!r}, status={self.status_code})"
        )


def _bbox_str(bbox: tuple[float, float, float, float]) -> str:
    return f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"


def _build_wms_params(
    layer_name: str,
    bbox: tuple[float, float, float, float],
    crs: str,
    image_format: str,
    width_px: int,
    height_px: int,
    version: str,
) -> dict[str, str]:
    """WMS ``GetMap`` query parameters. AXIS ORDER IS THE CALLER'S: EPSG:4326 is
    lon/lat under 1.1.1 and lat/lon under 1.3.0, and this builder passes the bbox
    through as given."""
    return {
        "service": "WMS",
        "version": version,
        "request": "GetMap",
        "layers": layer_name,
        "styles": "",
        "srs" if version.startswith("1.1") else "crs": crs,
        "bbox": _bbox_str(bbox),
        "width": str(width_px),
        "height": str(height_px),
        "format": image_format,
        "transparent": "true",
    }


def _build_wcs_params(
    coverage_id: str,
    bbox: tuple[float, float, float, float],
    crs: str,
    image_format: str,
    width_px: int,
    height_px: int,
    version: str,
) -> dict[str, str]:
    """WCS ``GetCoverage`` query parameters. 1.0.0 is the default because the
    later versions carry GeoServer projection-mapping bugs; pass another version
    only with probe evidence that the endpoint honours it."""
    if version.startswith("1.0"):
        return {
            "service": "WCS",
            "version": version,
            "request": "GetCoverage",
            "Coverage": coverage_id,
            "CRS": crs,
            "BBOX": _bbox_str(bbox),
            "WIDTH": str(width_px),
            "HEIGHT": str(height_px),
            "FORMAT": image_format,
        }
    # WCS 1.1.1 / 2.0.1 rename the parameters. Reserved: the adapter prefers 1.0.0.
    return {
        "service": "WCS",
        "version": version,
        "request": "GetCoverage",
        "identifier" if version.startswith("1.1") else "coverageId": coverage_id,
        "boundingBox": _bbox_str(bbox) + f",urn:ogc:def:crs:{crs}",
        "format": image_format,
    }


def _build_wfs_params(
    type_name: str,
    bbox: tuple[float, float, float, float] | None,
    crs: str,
    output_format: str,
    version: str,
    max_features: int,
) -> dict[str, str]:
    """WFS ``GetFeature`` query parameters."""
    params: dict[str, str] = {
        "service": "WFS",
        "version": version,
        "request": "GetFeature",
        "typeName": type_name,
        "outputFormat": output_format,
        "srsName": crs,
        "maxFeatures": str(max_features),
    }
    if bbox is not None:
        # WFS 1.1.0 / 2.0.0 bbox: x_min,y_min,x_max,y_max,EPSG:CODE.
        params["bbox"] = _bbox_str(bbox) + "," + crs
    return params


def _build_arcgis_query_params(
    bbox: tuple[float, float, float, float] | None,
    crs_code: int,
    output_fields: str,
    output_format: str,
    max_records: int,
    where: str,
) -> dict[str, str]:
    """ArcGIS REST MapServer/FeatureServer ``/query`` parameters. Every layer path
    on a server takes the same shape, so the layer lives in the URL and not in
    these params."""
    params: dict[str, str] = {
        "where": where,
        "outFields": output_fields,
        "outSR": str(crs_code),
        "inSR": str(crs_code),
        "f": output_format,
        "returnGeometry": "true",
        "resultRecordCount": str(max_records),
    }
    if bbox is not None:
        params["geometry"] = _bbox_str(bbox)
        params["geometryType"] = "esriGeometryEnvelope"
        params["spatialRel"] = "esriSpatialRelIntersects"
    return params


def fetch_ogc_layer(
    url: str,
    layer_name: str,
    bbox: tuple[float, float, float, float] | None,
    *,
    crs: str = "EPSG:4326",
    service_type: ServiceType = "WMS",
    image_format: str = "image/geotiff",
    version: str = "1.0.0",
    width_px: int | None = None,
    height_px: int | None = None,
    target_resolution_m: float | None = None,
    timeout_s: float = 120.0,
    user_agent: str | None = None,
    extra_params: dict[str, Any] | None = None,
    max_features: int = 1000,
    output_fields: str = "*",
    where_clause: str = "1=1",
) -> OGCResponse:
    """One generic OGC fetch. ``url`` keeps whatever trailing path it carries, the
    adapter only APPENDS params, and ``layer_name`` is unused for ARCGIS_REST, whose
    layer lives in the URL. Raises rather than returning a partial body."""
    # When a raster request leaves BOTH width and height unset, derive an
    # extent-aware grid from the bbox at the target ground resolution, clamped per
    # axis. Explicit ints pass through untouched. A vector service type ignores
    # width/height, but they still resolve to concrete ints so every builder that
    # consumes them receives numbers.
    if width_px is None and height_px is None:
        grid_bbox = bbox or (-180.0, -90.0, 180.0, 90.0)
        width_px, height_px = bbox_pixel_dims(
            grid_bbox,
            target_resolution_m if target_resolution_m is not None else _DEFAULT_OGC_CELL_M,
            px_max=_OGC_PX_MAX,
        )
    else:
        # One axis given but not the other: mirror it so both are concrete.
        if width_px is None:
            width_px = height_px
        if height_px is None:
            height_px = width_px

    if service_type == "WMS":
        params: dict[str, str] = _build_wms_params(
            layer_name=layer_name,
            bbox=bbox or (-180.0, -90.0, 180.0, 90.0),
            crs=crs,
            image_format=image_format,
            width_px=width_px,
            height_px=height_px,
            version=version,
        )
    elif service_type == "WCS":
        params = _build_wcs_params(
            coverage_id=layer_name,
            bbox=bbox or (-180.0, -90.0, 180.0, 90.0),
            crs=crs,
            image_format=image_format,
            width_px=width_px,
            height_px=height_px,
            version=version,
        )
    elif service_type == "WFS":
        params = _build_wfs_params(
            type_name=layer_name,
            bbox=bbox,
            crs=crs,
            output_format=image_format if image_format != "image/geotiff" else "application/json",
            version=version,
            max_features=max_features,
        )
    elif service_type == "ARCGIS_REST":
        # Pull the EPSG numeric code from the CRS string.
        try:
            crs_code = int(crs.upper().replace("EPSG:", ""))
        except ValueError as exc:  # noqa: BLE001
            raise OGCAdapterError(
                f"ARCGIS_REST requires EPSG:<code> CRS form; got {crs!r}"
            ) from exc
        # ImageServer ``exportImage`` takes a different param shape from the
        # MapServer/FeatureServer ``query``; the URL trailer is what distinguishes
        # them, since both arrive as ARCGIS_REST.
        if url.rstrip("/").endswith("/exportImage"):
            params = {
                "bbox": _bbox_str(bbox) if bbox else "",
                "bboxSR": str(crs_code),
                "imageSR": str(crs_code),
                "size": f"{width_px},{height_px}",
                "format": image_format if image_format != "image/geotiff" else "tiff",
                "pixelType": "F32",
                "f": "image",
            }
        else:
            params = _build_arcgis_query_params(
                bbox=bbox,
                crs_code=crs_code,
                output_fields=output_fields,
                output_format="geojson",
                max_records=max_features,
                where=where_clause,
            )
    elif service_type == "WMTS":
        # WMTS tile addressing is per-zoom, which this single-GET shape cannot
        # express; refuse rather than issue a request that cannot be correct.
        raise OGCAdapterError(
            "WMTS GetTile addressing requires per-zoom tile coordinates; "
            "v0.1 substrate does not implement this dialect — surface as "
            "OQ-47-WMTS-DIALECT for a follow-up if a WMTS catalog entry lands."
        )
    else:  # pragma: no cover - Literal exhaustive
        raise OGCAdapterError(f"unknown service_type={service_type!r}")

    if extra_params:
        params.update({k: str(v) for k, v in extra_params.items()})

    headers = {"User-Agent": user_agent or DEFAULT_USER_AGENT}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout_s)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise OGCAdapterError(
            f"OGC {service_type} GET failed for url={url} layer={layer_name!r}: {exc}"
        ) from exc

    content = resp.content
    content_type = resp.headers.get("content-type", "")

    # OGC servers return a 200 + XML exception body on logical errors (bad
    # coverage name, projection-mapping bug, sub-pixel request). Surface that
    # rather than caching the XML as if it were the raster.
    if "xml" in content_type.lower() and (
        b"ExceptionReport" in content or b"ServiceException" in content
    ):
        raise OGCAdapterError(
            f"OGC {service_type} returned exception body for url={url} "
            f"layer={layer_name!r}: {content[:400]!r}"
        )

    # ArcGIS REST returns a JSON error object on bad query; spot-check.
    if service_type == "ARCGIS_REST" and (
        b'"error":{' in content[:200] or b'"error" :' in content[:200]
    ):
        raise OGCAdapterError(
            f"ArcGIS REST returned error JSON for url={url}: {content[:400]!r}"
        )

    if not content or len(content) < 64:
        raise OGCAdapterError(
            f"OGC {service_type} returned empty/short body "
            f"({len(content)} bytes) for url={url} layer={layer_name!r}"
        )

    logger.info(
        "ogc_adapter %s url=%s layer=%s bytes=%d content_type=%s",
        service_type,
        url,
        layer_name,
        len(content),
        content_type,
    )
    return OGCResponse(
        content=content,
        content_type=content_type,
        service_type=service_type,
        url=getattr(resp, "url", url),  # final URL after redirects (defensive for test stubs)
        status_code=resp.status_code,
    )
