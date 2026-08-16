"""``show_nexrad_radar`` atomic tool -- live NEXRAD radar overlay (Iowa Mesonet WMS URL).

This is a DISPLAY tool, not a fetcher: it composes a live WMS GetMap service URL
for the Iowa State University Mesonet NEXRAD radar mosaic and returns it as a
``LayerURI`` the client renders directly. It downloads nothing, caches nothing,
and touches no data bytes -- the radar refreshes every ~5 minutes, so a static
pixel snapshot would misrepresent the live storm state. It lives under
``tools/display/`` (alongside other map-overlay tools) rather than
``tools/fetchers/`` precisely because it transfers a service URL, not a dataset.
"""

from __future__ import annotations

import logging
import math
from typing import Literal, Any
from urllib.parse import urlencode

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool

__all__ = ["show_nexrad_radar"]

logger = logging.getLogger("trid3nt_server.data.display.show_nexrad_radar.show_nexrad_radar")


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

# Iowa State University Mesonet NEXRAD WMS service base.
# Verified 2026-06-08: per-product endpoints at .../wms/nexrad/{product}.cgi.
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

# Iowa Mesonet docs note ``nexrad-n0r-wmst`` and product-specific layer names
# served on each cgi endpoint. We use the canonical WMS LAYERS= value per
# product so MapLibre / QGIS Server cascade can request the right product.
_PRODUCT_WMS_LAYER: dict[str, str] = {
    "n0r": "nexrad-n0r-wmst",
    "n0q": "nexrad-n0q-wmst",
    "vil": "nexrad-vil-wmst",
}


# ---------------------------------------------------------------------------
# AtomicToolMetadata -- registered once at import time.
#
# Composes only a service URL ("does NOT cache pixels"), so cacheable=False and
# ttl_class="live-no-cache".
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
# bbox helpers (identical-spirit to fetch_administrative_boundaries; copied to
# keep tools modular -- there is no shared bbox utility module yet).
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
    """Compose the Iowa Mesonet WMS service URL for ``product`` (+ optional bbox).

    Returns the full URL the client GetMap call will hit. The WMS service
    itself accepts standard parameters (``SERVICE=WMS``, ``REQUEST=GetMap``,
    ``LAYERS=``, ``BBOX=``, ``WIDTH=``, ``HEIGHT=``, ``CRS=``, ``FORMAT=``,
    ``TIME=``); we include the BBOX as a query-string hint when the caller
    scoped it geographically, so a downstream renderer that just appends
    standard GetMap params produces a correctly-scoped image.

    The output URL is the LayerURI.uri value; the QGIS plugin reads
    it as a base and tacks on per-tile params.
    """
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
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (composes an external public WMS endpoint URL),
    # destructiveHint=False, idempotentHint=True.
    open_world_hint=True,
)
def show_nexrad_radar(
    bbox: tuple[float, float, float, float] | None = None,
    product: Literal["n0r", "n0q", "vil"] = "n0r",
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Show live NEXRAD radar reflectivity on the map (composes a WMS URL; fetches nothing).

    **What it does:** Composes and returns a live WMS GetMap service URL for the
    Iowa State University Mesonet NEXRAD radar mosaic. This is a **display /
    URL-passthrough** tool: it emits a ``LayerURI`` the client renders against
    directly -- it does NOT download, sample, or cache any pixels. Radar
    reflectivity refreshes every ~5 minutes, so caching a static PNG would
    misrepresent the live storm state. Tier-1 free, no API key. CONUS coverage
    only (the NEXRAD network).

    **When to use:**

    - Storm-context display during a hurricane, squall-line, or convective-storm
      narrative -- "show me the current radar near Tampa", "put NEXRAD radar on
      the flood map for Harvey". Example: ``bbox=(-98.0, 27.0, -93.0, 31.0)`` for
      the Houston area, ``product="n0r"``.
    - Situational-awareness overlays alongside ``fetch_nws_alerts_conus`` or
      ``fetch_nifc_fire_perimeters`` for multi-hazard dashboards.
    - Vertically integrated liquid (``product="vil"``) for hail / heavy-precip
      risk assessment co-located with an active SFINCS pluvial run.

    **When NOT to use:**

    - Historical radar replay -- the Iowa Mesonet WMS serves the current mosaic
      only; archival radar retrieval is a separate path.
    - Quantitative precipitation estimation -- use ``fetch_mrms_qpe`` (gauge-
      corrected accumulation, mm); raw reflectivity is dBZ, not precipitation.
    - Downloading pixel arrays for analysis -- this tool emits a WMS URL, not a
      raster file.
    - Non-CONUS coverage -- NEXRAD is the US national radar network; for
      international radar overlays a different WMS source is needed.

    **Parameters:**

    - ``bbox``: optional ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326.
      When ``None``, returns the CONUS-wide WMS URL (``supports_global_query=True``).
      When supplied, the BBOX hint is encoded into the URL query string.
    - ``product``: ``"n0r"`` -- composite reflectivity, all-tilt max in dBZ
      (default; best for storm-context narratives); ``"n0q"`` -- base reflectivity,
      lowest 0.5° tilt in dBZ (shallow rotation, low-precip storms); ``"vil"`` -- vertically integrated liquid in kg/m² (hail / heavy-precip diagnostic).

    **Returns:**

    ``LayerURI`` with ``uri`` = Iowa Mesonet WMS endpoint for the product.
    ``layer_type="raster"``, ``role="context"`` (storm-state overlay, not a
    primary hazard product), ``units="dBZ"`` for n0r/n0q or ``"kg/m^2"`` for
    vil. ``bbox`` echoes the caller's bbox (or None for CONUS-wide). NOT routed
    through ``read_through`` -- ``cacheable=False``, ``ttl_class="live-no-cache"``.

    Raises: ``NexradProductError`` (unknown product), ``NexradBboxError``
    (malformed bbox: wrong arity, non-finite, out-of-range, or degenerate).

    **Cross-tool dependencies:**

    - Pair with: ``fetch_nws_alerts_conus`` (NWS watches/warnings) and
      ``fetch_goes_satellite`` (GOES-ABI satellite imagery) for live storm
      situational awareness.
    - Complement with: ``fetch_mrms_qpe`` when the user asks for precipitation
      accumulation rather than radar reflectivity.
    - Downstream: no tool consumes this LayerURI directly; the WMS URL is
      rendered by the QGIS plugin (added as a WMS layer) or a direct WMS
      GetMap request.
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
        style_preset=f"nexrad_{product}",
        role="context",
        units=("dBZ" if product in ("n0r", "n0q") else "kg/m^2"),
        bbox=bbox,
    )
