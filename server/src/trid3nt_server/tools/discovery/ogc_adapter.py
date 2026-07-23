"""Generic OGC Tier-2 adapter (job-0047, sprint-08 Stage B).

Single implementation that any §F.1.1 Tier 2 catalog entry (WMS / WMTS / WCS /
WFS) routes through. Mirrors the live-verified WCS 1.0.0 surgery landed by
job-0044 against MRLC NLCD (canonical class integers via `GetCoverage` rather
than palette indices via `GetMap`) — extracted here so:

- `fetch_landcover` (NLCD MRLC) shares the adapter (single source of truth);
- the new `fetch_from_catalog` Tier-2 dispatch routes any catalog entry through the
  same code path;
- future Tier-2 entries (FEMA NFHL ArcGIS REST MapServer, 3DEP Elevation
  ImageServer, USGS NHDPlus HR MapServer, etc.) avoid duplicating service-
  flavor request shapes.

Service flavors supported (per §F.1.1 Tier 2):

- ``WCS`` (Web Coverage Service): the raster-bytes surface for OGC catalogs —
  returns the source raster's actual byte values (canonical classes for NLCD,
  elevation in meters for 3DEP, etc.). Version 1.0.0 (most reliable on
  GeoServer); WCS 1.1.1 / 2.0.1 surface specific bugs (see job-0044 report).
- ``WMS`` (Web Map Service): the rendered-pixel surface — useful for
  visualization layers but NOT for raw model-input bytes (the palette-index
  trap job-0044 closed). Used by `fetch_from_catalog` for Tier-2 visualization-
  intent catalog entries (FEMA NFHL flood zones rendered as a map layer).
- ``WFS`` (Web Feature Service): vector feature retrieval; the catalog-entry
  path for ArcGIS REST FeatureServer-flavored services as well (via the
  shared HTTPS-GET shape).
- ``ARCGIS_REST`` (ArcGIS REST MapServer / FeatureServer / ImageServer): not a
  strict OGC service but the dominant Tier-2 surface for FEMA / USGS National
  Map / hazards.fema.gov endpoints. ESRI's REST query interface follows a
  consistent ``/MapServer/<layer>/query`` shape that we treat as a fourth OGC-
  adjacent dialect — the adapter dispatches by the entry's explicit
  ``service_type`` argument, not by URL sniffing.

Routes through ``read_through`` so identical params dedup at the cache.
External-API resilience (NFR-R-1) per the established pattern: per-call
timeout (default 120s; configurable), single re-raise on failure as
``UpstreamAPIError``, no sentinel on failure.

CRS hygiene: every request explicitly states a CRS (`EPSG:4326` by default;
caller passes whatever the source dataset emits). The returned bytes are
service-flavor-typed (GeoTIFF for WCS; PNG/JPEG/GeoTIFF for WMS; GeoJSON/
GeoPackage/Shapefile for WFS; JSON/GeoJSON for ArcGIS REST queries).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import requests

from trid3nt_server.tools.fetchers.imagery import _pc_stac

__all__ = [
    "OGCAdapterError",
    "OGCResponse",
    "ServiceType",
    "fetch_ogc_layer",
    "DEFAULT_USER_AGENT",
]

logger = logging.getLogger("trid3nt_server.tools.discovery.ogc_adapter")

#: Phase-2 resolution lever (job: fetch-side adjustable resolution). When a
#: raster Tier-2 service (WMS/WCS/ImageServer) is requested WITHOUT explicit
#: width_px/height_px, the adapter computes an extent-aware grid from the bbox
#: at this default cell size (metres). Callers opt into finer/coarser via
#: ``target_resolution_m`` (e.g. the catalog forwards an entry's
#: ``native_resolution_m``). The previous fixed-1024 default coarsened large
#: AOIs; this targets a real ground resolution instead.
_DEFAULT_OGC_CELL_M = 30.0
#: Hard cap on each computed raster axis so a large AOI never materializes an
#: enormous grid (bounds the response payload). ``bbox_pixel_dims`` clamps to
#: this on both axes.
_OGC_PX_MAX = 4096

#: Recognized Tier-2 service flavors. ``ARCGIS_REST`` is the ESRI MapServer /
#: FeatureServer / ImageServer dialect — strictly speaking not OGC, but the
#: dominant Tier-2 surface for US federal hazard catalogs (FEMA NFHL, USGS
#: National Map, etc.) so the adapter treats it as a fourth dialect.
ServiceType = Literal["WMS", "WMTS", "WCS", "WFS", "ARCGIS_REST"]

# Conservative default User-Agent; engine-callers (e.g. ``fetch_landcover``)
# pass their own descriptive one when policy requires (Nominatim, etc.). The
# default is fine for federal-public OGC endpoints.
DEFAULT_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent OGC adapter; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin)"
)


class OGCAdapterError(RuntimeError):
    """Adapter-level failure (HTTP error, OGC exception XML, empty body).

    Carries ``error_code="UPSTREAM_API_ERROR"`` and ``retryable=True`` to
    match the ``data_fetch.FetchError`` taxonomy — call sites that wrap the
    adapter behind a registered atomic tool re-raise as ``UpstreamAPIError``
    so the agent's FR-AS-11 surface sees a single typed failure mode.
    """

    error_code: str = "UPSTREAM_API_ERROR"
    retryable: bool = True


class OGCResponse:
    """Raw bytes + content-type + status from a single OGC adapter call.

    Attributes:
        content: response body bytes.
        content_type: HTTP ``Content-Type`` header (used by callers to pick
            an extension for cache writes — ``image/tiff`` → ``"tif"``, etc.).
        service_type: the dialect this response came from.
        url: the resolved request URL (useful for log/evidence capture).
        status_code: HTTP status code.
    """

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

    def __repr__(self) -> str:  # pragma: no cover — diagnostic
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
    """WMS ``GetMap`` query parameters.

    Per OGC WMS 1.1.1 / 1.3.0: ``service=WMS``, ``request=GetMap``,
    ``layers=...``, ``bbox=...``, ``srs/crs=...``, ``width=...``,
    ``height=...``, ``format=...``. The axis-order convention differs between
    1.1.1 (lon, lat) and 1.3.0 (varies by CRS); ``EPSG:4326`` is lon/lat in
    1.1.1 and lat/lon in 1.3.0 — caller is responsible for ordering bbox
    correctly for the requested version. Default version 1.1.1 (the common
    GeoServer flavor that uses lon/lat consistently).
    """
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
    """WCS ``GetCoverage`` query parameters.

    Version-specific shape: 1.0.0 uses ``Coverage`` + ``CRS`` + ``WIDTH/HEIGHT``;
    1.1.x / 2.0.1 use ``identifier`` / ``coverageId`` + ``boundingbox`` and have
    GeoServer-specific projection-mapping bugs (see job-0044 report — WCS
    1.0.0 is the only reliable surface on MRLC's GeoServer instance). The
    adapter defaults to 1.0.0; caller passes a different version explicitly
    when they have probe evidence it works.
    """
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
    # WCS 1.1.1 / 2.0.1: different parameter names. Surfaced as informational
    # only — the adapter prefers 1.0.0; 1.1.1 / 2.0.1 paths are reserved.
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
    """ArcGIS REST MapServer/FeatureServer ``/query`` parameters.

    Different layer paths on the same server use the same parameter shape:
    ``where=...&geometry=...&geometryType=esriGeometryEnvelope&inSR=...&outSR=...
    &outFields=...&f=geojson`` (or ``f=json``). The adapter picks geojson by
    default since downstream tools expect GeoJSON-like vectors.
    """
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
    """Single-call generic OGC Tier-2 fetch.

    Use this when: any §F.1.1 Tier 2 catalog entry needs to retrieve bytes
    (raster or vector) via WMS / WMTS / WCS / WFS / ArcGIS REST. This is the
    shared substrate for `fetch_landcover` (NLCD WCS), `fetch_from_catalog` Tier-2
    dispatch, and any future Tier-2 fetcher.

    Do NOT use this for: Tier 1 (STAC + COG, use `pystac-client`); Tier 3
    (direct HTTPS + Range, use `requests` / `rasterio /vsicurl/`); Tier 4
    (region download + clip, use the per-source fetcher pattern).

    Args:
        url: the OGC endpoint URL. The adapter appends query parameters; if
            the URL already carries a path segment like ``/wms`` or
            ``/MapServer/28/query`` it is preserved (caller is responsible for
            the correct trailing path).
        layer_name: WMS ``layers`` / WCS ``Coverage`` / WFS ``typeName`` /
            ArcGIS REST: irrelevant (the layer is embedded in the URL path —
            pass an empty string).
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326 (or the
            CRS named by ``crs``). Optional for WFS / ArcGIS REST (omit to
            query the whole layer).
        crs: CRS string in the WMS/WCS form ``"EPSG:4326"``. WFS uses
            ``"EPSG:4326"`` for ``srsName``; ArcGIS REST extracts the numeric
            code (``4326``).
        service_type: ``"WMS"`` / ``"WMTS"`` / ``"WCS"`` / ``"WFS"`` /
            ``"ARCGIS_REST"``.
        image_format: WMS ``format`` / WCS ``FORMAT``. Default ``image/geotiff``
            (raster); WMS-rendered surfaces typically want ``image/png``.
        version: OGC service version string. Defaults to ``"1.0.0"`` (WCS
            sweet spot per job-0044); WMS callers typically pass ``"1.1.1"``;
            WFS callers ``"2.0.0"`` or ``"1.1.0"``.
        width_px, height_px: pixel dimensions for raster responses (WMS / WCS /
            ImageServer). Ignored for WFS / ArcGIS REST (MapServer query).
            When BOTH are left ``None`` (the default), a raster request derives
            an extent-aware grid from ``bbox`` at ``target_resolution_m`` (or
            the ``_DEFAULT_OGC_CELL_M`` 30 m fallback), each axis clamped to
            ``_OGC_PX_MAX`` (4096). Passing explicit ints is byte-identical to
            the prior fixed behavior — the computed-grid path only runs when
            neither is given.
        target_resolution_m: optional ground cell size in metres for the
            auto-computed raster grid. Phase-2 resolution lever: callers (e.g.
            ``fetch_from_catalog`` forwarding an entry's ``native_resolution_m``)
            opt into finer/coarser output without hard-coding pixel counts.
            Ignored when explicit ``width_px``/``height_px`` are given, or for
            vector (WFS / MapServer query) service types.
        timeout_s: request timeout (NFR-R-1).
        user_agent: override the descriptive User-Agent header.
        extra_params: extra query parameters merged after the default set
            (rare — catalog-specific knobs like ``f=json`` overrides).
        max_features: WFS ``maxFeatures`` / ArcGIS ``resultRecordCount``.
        output_fields: ArcGIS REST ``outFields``.
        where_clause: ArcGIS REST ``where``. Default ``1=1`` (all features).

    Returns:
        ``OGCResponse(content=<bytes>, content_type=<str>, service_type, url,
        status_code)``.

    Raises:
        ``OGCAdapterError`` on HTTP failure, OGC exception XML response, or
        empty / sub-64-byte body. Callers translate to the engine's
        ``UpstreamAPIError`` taxonomy.
    """
    # Phase-2 resolution lever: when a raster request leaves BOTH width/height
    # unset, derive an extent-aware grid from the bbox at the target ground
    # resolution (or the 30 m fallback), clamped to ``_OGC_PX_MAX`` per axis.
    # Explicit ints pass through untouched (byte-identical to prior behavior).
    # Vector service types ignore width/height; resolve to a harmless int so
    # the builders that consume them always receive concrete pixel counts.
    if width_px is None and height_px is None:
        grid_bbox = bbox or (-180.0, -90.0, 180.0, 90.0)
        width_px, height_px = _pc_stac.bbox_pixel_dims(
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
        # ImageServer ``exportImage`` has a distinct param shape (bbox + size
        # + format=tiff) vs MapServer/FeatureServer ``query``. Detect via the
        # URL trailer.
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
        # WMTS tile addressing is per-zoom; for substrate scope, treat as a
        # GetTile request and surface a clear NotImplemented if the caller
        # didn't supply the needed extra_params. WMTS Tier-2 entries are
        # not common in the v0.1 30-entry seed catalog.
        raise OGCAdapterError(
            "WMTS GetTile addressing requires per-zoom tile coordinates; "
            "v0.1 substrate does not implement this dialect — surface as "
            "OQ-47-WMTS-DIALECT for a follow-up if a WMTS catalog entry lands."
        )
    else:  # pragma: no cover — Literal exhaustive
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
