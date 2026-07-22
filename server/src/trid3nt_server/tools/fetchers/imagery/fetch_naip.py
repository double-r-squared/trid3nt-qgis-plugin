"""``fetch_naip`` atomic tool  --  NAIP high-res aerial imagery (conservation).

Fetches USDA NAIP (National Agriculture Imagery Program) leaf-on aerial
imagery for a bbox via the Microsoft Planetary Computer (PC) STAC catalog and
returns a 3-band RGB COG suitable for direct display as the aerial BASE layer in
the SC-DNR-style conservation-priority stack.

Data source
===========

PC collection ``naip``:

    catalog: https://planetarycomputer.microsoft.com/api/stac/v1
    asset:   ``image``  --  a 4-band (R, G, B, NIR) uint8 COG at ~0.6-1 m GSD
    extent:  CONUS + HI + PR + USVI (NAIP is US-only)

The ``image`` asset is an Azure-Blob COG behind a SAS token; this tool signs the
href (see ``_pc_stac.sas_sign_href``) and reads the first three bands (R, G, B)
warped to EPSG:4326 and windowed to the bbox through GDAL ``/vsicurl/``, then
re-emits a 3-band uint8 RGB COG. ``publish_layer`` renders multiband COGs
directly (the RGBA/multiband passthrough), so no per-band rescale/colormap is
applied  --  the baked aerial colors paint as-is.

Honesty (data-source fallback norm): NAIP is US-only. If NO NAIP item
intersects the bbox a typed ``NAIPNoCoverageError`` is raised (e.g. an offshore
or foreign AOI)  --  never a fabricated layer.

FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical ``(bbox,
year)`` calls reuse the cached RGB COG in the ``static-30d`` / ``naip`` cache
prefix.

Tier-1 free (no API key). Heavy emit-free sync raster work  --  registered in
``_ALWAYS_OFFLOAD_SYNC_TOOLS`` so it runs via ``asyncio.to_thread`` and never
stalls the WebSocket heartbeat.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.fetchers.imagery import _pc_stac
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_naip",
    "estimate_payload_mb",
    "NAIPError",
    "NAIPBboxError",
    "NAIPNoCoverageError",
    "NAIPUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.imagery.fetch_naip")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class NAIPError(RuntimeError):
    """Base class for fetch_naip failures."""

    error_code = "NAIP_ERROR"
    retryable = True


class NAIPBboxError(NAIPError):
    """Malformed / out-of-range / degenerate / too-large bbox."""

    error_code = "NAIP_BBOX_INVALID"
    retryable = False


class NAIPNoCoverageError(NAIPError):
    """No NAIP imagery intersects the bbox (NAIP is US-only).

    Honest no-coverage signal (data-source fallback norm)  --  never fabricate.
    """

    error_code = "NAIP_NO_COVERAGE"
    retryable = False


class NAIPUpstreamError(NAIPError):
    """A PC STAC search / asset read / COG write failed."""

    error_code = "NAIP_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_COLLECTION = "naip"
_IMAGE_ASSET = "image"

#: NAIP native GSD is ~0.6-1 m; we materialize the bbox window at ~1 m, clamped.
_NATIVE_CELL_M = 1.0

#: Larger pixel cap than the 10 m tools (NAIP is sub-meter, AOIs are small but
#: detail-rich); still bounded so a wide bbox does not blow up memory.
_PX_MAX = 8192

#: bbox area guardrail (deg^2). NAIP at ~1 m is detail-dense; keep the AOI
#: small (~a neighborhood / small preserve). ~0.05 deg^2.
_MAX_BBOX_DEG2 = 0.06

_BBOX_DECIMALS = 6


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_naip",
    ttl_class="static-30d",
    source_class="naip",
    cacheable=True,
    supports_global_query=False,
    payload_mb_estimator_name="estimate_payload_mb",
)


# ---------------------------------------------------------------------------
# Payload estimator (Wave 1.5 chat-warning gate).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    **_kw: Any,
) -> float:
    """Estimate emitted RGB COG size in MB.

    3-band uint8 at ~1 m runs large; ~250 MB / sq-deg LZW-compressed aerial.
    Scale linearly with bbox area, floored.
    """
    if bbox is None:
        return 20.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 20.0
    return max(1.0, sq_deg * 250.0)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise NAIPBboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise NAIPBboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise NAIPBboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise NAIPBboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise NAIPBboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    area = (max_lon - min_lon) * (max_lat - min_lat)
    if area > _MAX_BBOX_DEG2:
        raise NAIPBboxError(
            f"bbox area {area:.4f} deg^2 exceeds {_MAX_BBOX_DEG2} deg^2 guardrail "
            "for fetch_naip (NAIP is sub-meter; narrow the bbox to a "
            "neighborhood / small preserve)."
        )


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Core: search NAIP -> windowed RGB read -> 3-band uint8 COG bytes.
# ---------------------------------------------------------------------------


def _fetch_naip_cog_bytes(bbox: tuple[float, float, float, float]) -> bytes:
    """Search NAIP, read RGB clipped to ``bbox``, return a 3-band uint8 COG.

    Raises:
        ``NAIPNoCoverageError``: no NAIP item intersects the bbox (US-only).
        ``NAIPUpstreamError``: search / read / write failure.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import reproject, Resampling

    # 1. Pick the most-recent NAIP item intersecting the bbox.
    try:
        item = _pc_stac.search_least_cloudy_item(
            collection=_COLLECTION,
            bbox=bbox,
            datetime_range=None,
            max_cloud_cover=None,
            sort_by_cloud=False,
        )
    except _pc_stac.PCStacNoItemsError as exc:
        raise NAIPNoCoverageError(
            f"no NAIP imagery for bbox={bbox} (NAIP is US-only  --  CONUS + HI + "
            f"PR + USVI): {exc}"
        ) from exc
    except _pc_stac.PCStacError as exc:
        raise NAIPUpstreamError(f"NAIP STAC search failed: {exc}") from exc

    assets = getattr(item, "assets", {}) or {}
    if _IMAGE_ASSET not in assets:
        raise NAIPUpstreamError(
            f"NAIP item {getattr(item, 'id', '?')} missing '{_IMAGE_ASSET}' asset "
            f"(have {sorted(assets)})"
        )

    signed = _pc_stac.sas_sign_href(assets[_IMAGE_ASSET].href, _COLLECTION)
    vsicurl = "/vsicurl/" + signed

    width_px, height_px = _pc_stac.bbox_pixel_dims(
        bbox, _NATIVE_CELL_M, px_max=_PX_MAX
    )

    transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )

    try:
        with rasterio.Env(**_pc_stac.VSICURL_ENV_KW):
            with rasterio.open(vsicurl) as src:
                nbands = min(3, src.count)
                rgb = np.zeros((3, height_px, width_px), dtype="uint8")
                for bi in range(nbands):
                    reproject(
                        source=rasterio.band(src, bi + 1),
                        destination=rgb[bi],
                        src_transform=src.transform,
                        src_crs=src.crs,
                        dst_transform=transform,
                        dst_crs="EPSG:4326",
                        resampling=Resampling.bilinear,
                    )
    except Exception as exc:  # noqa: BLE001
        raise NAIPUpstreamError(
            f"NAIP RGB read failed for bbox={bbox} (item={getattr(item, 'id', '?')}): {exc}"
        ) from exc

    if not rgb.any():
        raise NAIPNoCoverageError(
            f"NAIP item {getattr(item, 'id', '?')} produced an all-black window "
            f"over bbox={bbox} (item does not actually cover the AOI)."
        )

    # 2. Re-emit as a 3-band uint8 RGB COG (publish_layer multiband passthrough).
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_naip_"
        ) as f:
            tmp_path = f.name
        profile = dict(
            driver="COG",
            dtype="uint8",
            count=3,
            height=height_px,
            width=width_px,
            crs="EPSG:4326",
            transform=transform,
            compress="DEFLATE",
            photometric="RGB",
        )
        with rasterio.open(tmp_path, "w", **profile) as dst:
            dst.write(rgb)
            dst.colorinterp = [
                rasterio.enums.ColorInterp.red,
                rasterio.enums.ColorInterp.green,
                rasterio.enums.ColorInterp.blue,
            ]
        with open(tmp_path, "rb") as fh:
            cog_bytes = fh.read()
    except Exception as exc:  # noqa: BLE001
        raise NAIPUpstreamError(f"NAIP COG write failed for bbox={bbox}: {exc}") from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    logger.info(
        "fetch_naip: item=%s bbox=%s -> %d-byte 3-band RGB COG (%dx%d)",
        getattr(item, "id", "?"),
        bbox,
        len(cog_bytes),
        width_px,
        height_px,
    )
    return cog_bytes


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (PC STAC public API),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_naip(
    bbox: tuple[float, float, float, float],
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch NAIP high-resolution aerial imagery (RGB) for a US bbox.

    **What it does:** Searches the Microsoft Planetary Computer for a NAIP
    aerial-imagery item intersecting ``bbox``, reads the R/G/B bands clipped to
    the bbox at ~1 m, and returns a 3-band uint8 RGB COG that renders directly
    as the aerial BASE layer (no rescale / colormap  --  the baked colors paint
    as-is via the multiband passthrough in ``publish_layer``).

    NAIP is leaf-on, ~0.6-1 m, refreshed on a multi-year state cycle  --  the
    canonical free US aerial basemap for site-level context.

    **When to use:**
    - User wants a high-res aerial / true-color basemap for a US area.
    - As the aerial BASE layer under the conservation-priority stack
      (``model_conservation_priority``), under species points + NDVI + MoBI.

    **When NOT to use:**
    - Outside the US (NAIP is CONUS + HI + PR + USVI only)  --  a no-coverage
      result is an honest typed error, not a fabricated layer.
    - Vegetation index (use ``compute_ndvi``) or land-cover classes (use
      ``fetch_landcover``).
    - Very large AOIs  --  NAIP is sub-meter; the tool caps the bbox to a small
      neighborhood / preserve (~0.06 deg^2).

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326.
      Required. US-only, AOI-scoped.

    **Returns:** A ``LayerURI`` (``layer_type="raster"``, ``role="context"``  -- 
    it is a basemap, not the analytical primary) pointing at a 3-band RGB COG in
    the ``static-30d``/``naip`` cache prefix. ``style_preset="naip_rgb"`` (a
    multiband passthrough token  --  no single-band rescale).

    **Data source:** USDA NAIP via the Microsoft Planetary Computer STAC
    (``naip`` collection; ``image`` asset).

    FR-CE-8: routed through ``read_through`` so identical bbox calls reuse the
    cached RGB COG.
    """
    _validate_bbox(bbox)
    q_bbox = _round_bbox(bbox)

    params = {"bbox": list(q_bbox), "collection": _COLLECTION}

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_naip_cog_bytes(q_bbox),
    )
    assert result.uri is not None, (
        "fetch_naip is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=(
            f"naip-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name="NAIP Aerial Imagery",
        layer_type="raster",
        uri=result.uri,
        # "naip_rgb" is a multiband-passthrough token: publish_layer's
        # RGBA/multiband probe renders the 3-band COG directly, and the token
        # is NOT in the single-band registry, so no rescale/colormap is applied.
        style_preset="naip_rgb",
        role="context",
        units=None,
    )
