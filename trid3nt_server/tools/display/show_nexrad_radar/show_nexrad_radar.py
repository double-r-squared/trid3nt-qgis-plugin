"""``show_nexrad_radar`` - a live NEXRAD radar overlay as an Iowa Mesonet WMS URL.
A DISPLAY tool, not a fetcher: it composes a GetMap service URL and returns it as a
``LayerURI`` the client renders directly, downloading and caching NOTHING - the
mosaic refreshes every few minutes, so a cached pixel snapshot would misrepresent
the live storm state."""

from __future__ import annotations

import logging
import math
from typing import Literal, Any
from urllib.parse import urlencode

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

__all__ = ["show_nexrad_radar"]

logger = logging.getLogger("trid3nt_server.tools.display.show_nexrad_radar.show_nexrad_radar")


# ---------------------------------------------------------------------------
# Error types (typed-error surface).
# ---------------------------------------------------------------------------


class NexradError(RuntimeError):
    """Base class for show_nexrad_radar failures."""

    error_code: str = "NEXRAD_ERROR"
    retryable: bool = False


class NexradProductError(NexradError):
    """Unknown product was requested."""

    error_code = "NEXRAD_PRODUCT_INVALID"
    retryable = False


class NexradBboxError(NexradError):
    """Bbox is malformed (non-finite, out-of-range, or degenerate)."""

    error_code = "NEXRAD_BBOX_INVALID"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

# Iowa State University Mesonet NEXRAD WMS service base; per-product endpoints
# hang off it at .../wms/nexrad/{product}.cgi.
_NEXRAD_WMS_BASE = "https://mesonet.agron.iastate.edu/cgi-bin/wms/nexrad"

_VALID_PRODUCTS = frozenset({"n0r", "n0q", "vil"})

_PRODUCT_DESCRIPTIONS: dict[str, str] = {
    "n0r": "composite reflectivity (all-tilt max, dBZ)",
    "n0q": "base reflectivity tilt 0.5° (dBZ)",
    "vil": "vertically integrated liquid (kg/m²)",
}

_PRODUCT_LAYER_NAME: dict[str, str] = {
    "n0r": "NEXRAD Composite Reflectivity",
    "n0q": "NEXRAD Base Reflectivity (0.5°)",
    "vil": "NEXRAD Vertically Integrated Liquid",
}

# The canonical WMS LAYERS= value per product, so a renderer requesting through
# this URL gets the product asked for rather than the endpoint's default.
_PRODUCT_WMS_LAYER: dict[str, str] = {
    "n0r": "nexrad-n0r-wmst",
    "n0q": "nexrad-n0q-wmst",
    "vil": "nexrad-vil-wmst",
}


# ---------------------------------------------------------------------------
# AtomicToolMetadata -- registered once at import time. Only a service URL is
# composed, so cacheable=False and ttl_class="live-no-cache".
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="show_nexrad_radar",
    ttl_class="live-no-cache",
    source_class=None,
    cacheable=False,
    # bbox=None returns the CONUS-wide WMS GetMap URL; this tool transfers only a
    # service URL (~0.1MB), never pixels, so a no-bbox global query is bounded + safe.
    supports_global_query=True,
)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``NexradBboxError`` if bbox is invalid."""
    if len(bbox) != 4:
        raise NexradBboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise NexradBboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise NexradBboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise NexradBboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise NexradBboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


# ---------------------------------------------------------------------------
# WMS URL builder.
# ---------------------------------------------------------------------------


def _build_wms_url(
    product: str,
    bbox: tuple[float, float, float, float] | None,
) -> str:
    """The Iowa Mesonet WMS base URL for ``product``, carrying ``bbox`` as a query
    hint when one is given. It is a BASE: a renderer appends its own GetMap params
    to it, so anything encoded here must survive that append."""
    if product not in _VALID_PRODUCTS:
        raise NexradProductError(
            f"unknown product={product!r}; allowed: {sorted(_VALID_PRODUCTS)}"
        )
    base = f"{_NEXRAD_WMS_BASE}/{product}.cgi"
    if bbox is None:
        # CONUS default; the LayerURI carries no bbox hint.
        return base

    # Encode BBOX as a service-default hint so URL inspection shows the scope.
    # WMS 1.3.0 axis order is lat,lon for some CRS; we use the WMS-1.1.1 lon,lat
    # order for the BBOX param (LonLat) since CRS:84 / EPSG:4326 long-axis-first
    # is the convention Iowa Mesonet documents for their NEXRAD WMS.
    bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
    qs = urlencode({"BBOX": bbox_str})
    return f"{base}?{qs}"


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Open-world: the composed URL points at an external public WMS endpoint.
    open_world_hint=True,
)
def show_nexrad_radar(
    bbox: tuple[float, float, float, float] | None = None,
    product: Literal["n0r", "n0q", "vil"] = "n0r",
    # Absorb model-invented kwargs; the normalizer already strips most of them.
    **_extra_ignored: Any,
) -> LayerURI:
    """Show live NEXRAD radar reflectivity on the map; composes a WMS URL and
    fetches nothing.

    ROUTING: storm context during a hurricane, squall line or convective storm -
    "show me the current radar", "put NEXRAD on the flood map" - and situational
    awareness beside weather alerts or satellite imagery. NOT for historical replay
    (the service carries the CURRENT mosaic only), NOT for quantitative
    precipitation (reflectivity is dBZ, not accumulation), NOT for pixel arrays, and
    NOT outside CONUS. Free, no API key.

    `bbox` omitted returns the CONUS-wide URL; supplied, it rides the URL as a BBOX
    hint. `product`: `n0r` composite reflectivity (all-tilt max, dBZ), `n0q` base
    reflectivity at the lowest tilt, `vil` vertically integrated liquid (kg/m^2).

    Returns a `LayerURI` the client renders directly, `role="context"`, units dBZ or
    kg/m^2. Nothing is cached - the mosaic refreshes every few minutes, so a static
    snapshot would misrepresent the live storm.
    """
    # Defensive validations on the registered surface (typed errors on unknown
    # product / bad bbox).
    if product not in _VALID_PRODUCTS:
        raise NexradProductError(
            f"unknown product={product!r}; allowed: {sorted(_VALID_PRODUCTS)}"
        )
    if bbox is not None:
        _validate_bbox(bbox)

    url = _build_wms_url(product, bbox)
    logger.info(
        "show_nexrad_radar: product=%s bbox=%s url=%s",
        product,
        bbox,
        url,
    )

    # layer_id encodes product + bbox-or-conus so multiple panels can carry
    # distinct LayerURI instances without colliding on the client.
    if bbox is None:
        layer_id = f"nexrad-{product}-conus"
    else:
        layer_id = (
            f"nexrad-{product}-{bbox[0]:.4f}-{bbox[1]:.4f}-"
            f"{bbox[2]:.4f}-{bbox[3]:.4f}"
        )

    name = f"{_PRODUCT_LAYER_NAME[product]} (Iowa State Mesonet)"

    return LayerURI(
        layer_id=layer_id,
        name=name,
        layer_type="raster",
        uri=url,
        style={"kind": "continuous"},
        role="context",
        units=("dBZ" if product in ("n0r", "n0q") else "kg/m^2"),
        bbox=bbox,
    )
