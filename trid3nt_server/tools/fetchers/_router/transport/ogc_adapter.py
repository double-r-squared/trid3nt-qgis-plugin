"""The WCS GetCoverage transport: one coverage over one bbox, as bytes.

Every request states a CRS and an explicit pixel grid, because WCS 1.0.0 requires
one. A failure re-raises as ``OGCAdapterError`` and writes no sentinel, so a
caller never caches a failure as if it were bytes."""

from __future__ import annotations

import logging
from typing import Any

import requests

from trid3nt_server.tools.fetchers._fetch_common import bbox_pixel_dims

__all__ = [
    "OGCAdapterError",
    "OGCResponse",
    "fetch_ogc_layer",
    "DEFAULT_USER_AGENT",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers._router.transport.ogc_adapter")

#: Cell size in metres for the grid a request derives from its bbox when it gives
#: no explicit width/height. Targeting a ground resolution rather than a fixed
#: pixel count is what keeps a large AOI from being silently coarsened; a caller
#: opts into finer or coarser through ``target_resolution_m``.
_DEFAULT_OGC_CELL_M = 30.0
#: Hard cap on each computed axis so a large AOI never materializes an enormous
#: grid (bounds the response payload). ``bbox_pixel_dims`` clamps to this on both
#: axes.
_OGC_PX_MAX = 4096

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
    """Raw bytes and content type from one adapter call. ``content_type`` is the
    header verbatim - the caller picks the cache extension from it, since the
    dialect alone does not determine the payload format."""

    __slots__ = ("content", "content_type")

    def __init__(self, content: bytes, content_type: str) -> None:
        self.content = content
        self.content_type = content_type

    def __repr__(self) -> str:  # pragma: no cover - diagnostic
        return (f"OGCResponse(bytes={len(self.content)}, "
                f"content_type={self.content_type!r})")


def fetch_ogc_layer(
    url: str,
    layer_name: str,
    bbox: tuple[float, float, float, float] | None,
    *,
    crs: str = "EPSG:4326",
    image_format: str = "image/geotiff",
    version: str = "1.0.0",
    width_px: int | None = None,
    height_px: int | None = None,
    target_resolution_m: float | None = None,
    timeout_s: float = 120.0,
    user_agent: str | None = None,
    extra_params: dict[str, Any] | None = None,
) -> OGCResponse:
    """One WCS GetCoverage. ``url`` keeps whatever trailing path it carries, the
    adapter only APPENDS params. Raises rather than returning a partial body."""
    # 1.0.0 is the only dialect probed against the endpoints that declare this
    # access: the later versions rename every parameter and carry GeoServer
    # projection-mapping bugs. A row naming another version refuses rather than
    # sending a request shape nobody has seen answered.
    if not version.startswith("1.0"):
        raise OGCAdapterError(
            f"WCS GetCoverage is built for 1.0.0; got version={version!r}")
    # When a request leaves BOTH width and height unset, derive an extent-aware
    # grid from the bbox at the target ground resolution, clamped per axis.
    # Explicit ints pass through untouched.
    if width_px is None and height_px is None:
        width_px, height_px = bbox_pixel_dims(
            bbox or (-180.0, -90.0, 180.0, 90.0),
            target_resolution_m if target_resolution_m is not None else _DEFAULT_OGC_CELL_M,
            px_max=_OGC_PX_MAX,
        )
    elif width_px is None:
        width_px = height_px
    elif height_px is None:
        height_px = width_px

    west, south, east, north = bbox or (-180.0, -90.0, 180.0, 90.0)
    params: dict[str, str] = {
        "service": "WCS",
        "version": version,
        "request": "GetCoverage",
        "Coverage": layer_name,
        "CRS": crs,
        "BBOX": f"{west},{south},{east},{north}",
        "WIDTH": str(width_px),
        "HEIGHT": str(height_px),
        "FORMAT": image_format,
    }
    if extra_params:
        params.update({k: str(v) for k, v in extra_params.items()})

    headers = {"User-Agent": user_agent or DEFAULT_USER_AGENT}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=timeout_s)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise OGCAdapterError(
            f"WCS GetCoverage failed for url={url} coverage={layer_name!r}: {exc}"
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
            f"WCS returned exception body for url={url} "
            f"coverage={layer_name!r}: {content[:400]!r}"
        )
    if not content or len(content) < 64:
        raise OGCAdapterError(
            f"WCS returned empty/short body ({len(content)} bytes) for "
            f"url={url} coverage={layer_name!r}"
        )

    logger.info("ogc_adapter WCS url=%s coverage=%s bytes=%d content_type=%s",
                url, layer_name, len(content), content_type)
    return OGCResponse(content=content, content_type=content_type)
