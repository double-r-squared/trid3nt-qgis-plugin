"""``fetch_copernicus_dem`` atomic tool  --  Copernicus GLO-30 global 30 m DEM.

Fetches the Copernicus DEM GLO-30 (Copernicus_DSM, 30 m global Digital Surface
Model) for a bbox via the Microsoft Planetary Computer (PC) STAC catalog and
returns a single-band float32 (meters) Cloud-Optimized GeoTIFF that paints with
the ``continuous_dem`` terrain ramp.

Why this tool (vs ``fetch_dem``)?  The canonical ``fetch_dem`` serves USGS 3DEP,
which is CONUS / US-territories only. Copernicus GLO-30 is the keyless GLOBAL
elevation complement: it covers every continent (the Alps, the Andes, the
Himalaya, the Sahara, ...). Use ``fetch_dem`` inside the US for the finer 10 m
3DEP product; use this tool for any non-US AOI (or a global before/after where a
single source is wanted).

Data source
===========

PC collection ``cop-dem-glo-30`` (Copernicus DEM GLO-30, 30 m, global). Each
1-degree tile is a single-band float32 COG in Azure Blob storage. The tool STAC-
searches the bbox, signs every intersecting tile, reprojects each to EPSG:4326
windowed to the bbox, and first-wins mosaics them into one DEM grid.

    catalog: https://planetarycomputer.microsoft.com/api/stac/v1
    asset:   ``data`` (single-band float32 elevation in meters, ellipsoidal /
             EGM2008-referenced per the Copernicus DEM product spec)

PC asset signing
================

Copernicus DEM blobs live in the ``elevationeuwest`` Azure container, which the
per-collection SAS token (``/api/sas/v1/token/<collection>``  --  the path the
shared ``_pc_stac.sas_sign_href`` uses) does NOT authorize (it 403s for this
container). The per-href sign endpoint ``/api/sas/v1/sign?href=<blob>`` DOES
mint a working token for it, so this tool signs each tile through that endpoint
(verified live 2026-06-27: signed read returns HTTP 206). The shared helper is
left untouched so the other PC tools that depend on it are unaffected.

Honesty (data-source fallback norm): if NO Copernicus tile intersects the bbox
(only possible for a degenerate / off-globe request, since GLO-30 is global) OR
every reprojected pixel is nodata, a typed ``CopernicusDemEmptyError`` is raised
 --  never a fabricated layer. A search / sign / read failure raises a typed
``CopernicusDemUpstreamError``.

FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical ``bbox`` calls
reuse the cached DEM COG in the ``static-30d`` / ``copernicus_dem`` cache prefix.

Tier-1 free (no API key, no Earthdata login). Heavy emit-free sync raster work
 --  intended for the ``_ALWAYS_OFFLOAD_SYNC_TOOLS`` set so it runs via
``asyncio.to_thread`` and never stalls the WebSocket heartbeat.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
from typing import Any

import requests

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from . import _pc_stac
from .cache import read_through

__all__ = [
    "fetch_copernicus_dem",
    "estimate_payload_mb",
    "CopernicusDemError",
    "CopernicusDemBboxError",
    "CopernicusDemEmptyError",
    "CopernicusDemUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.fetch_copernicus_dem")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class CopernicusDemError(RuntimeError):
    """Base class for fetch_copernicus_dem failures."""

    error_code = "COPERNICUS_DEM_ERROR"
    retryable = True


class CopernicusDemBboxError(CopernicusDemError):
    """Malformed / out-of-range / degenerate / too-large bbox."""

    error_code = "COPERNICUS_DEM_BBOX_INVALID"
    retryable = False


class CopernicusDemEmptyError(CopernicusDemError):
    """No Copernicus tile covers the bbox, or the window is all-nodata.

    Honest no-coverage signal (data-source fallback norm)  --  never fabricate.
    """

    error_code = "COPERNICUS_DEM_NO_COVERAGE"
    retryable = False


class CopernicusDemUpstreamError(CopernicusDemError):
    """A PC STAC search / SAS-sign / tile read / COG write failed."""

    error_code = "COPERNICUS_DEM_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_COLLECTION = "cop-dem-glo-30"

#: Single-band elevation asset key in the PC cop-dem-glo-30 items.
_DATA_ASSET = "data"

#: Copernicus GLO-30 native grid (~30 m); used to size the bbox-windowed read.
_NATIVE_CELL_M = 30.0

#: Per-href PC sign endpoint. Unlike the per-collection token endpoint that the
#: shared ``_pc_stac.sas_sign_href`` uses, this one authorizes the
#: ``elevationeuwest`` blob container that Copernicus DEM tiles live in.
_PC_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"

#: HTTP timeout for the per-href sign call (fast metadata round-trip).
_SIGN_TIMEOUT_S = 30.0

#: Nodata sentinel for the emitted single-band float32 DEM COG.
_NODATA = -9999.0

#: Terrain style token (single-band continuous ramp; same preset fetch_dem and
#: the 3DEP / terrain tools use). Matched by publish_layer's terrain passthrough.
_STYLE_PRESET = "continuous_dem"

#: 6-dp bbox quantization (~0.1 m) for cache-key stability.
_BBOX_DECIMALS = 6

#: bbox area guardrail (deg^2). A DEM over a huge AOI spans many 1-deg tiles and
#: materializes an enormous grid; the atomic-tool surface is AOI-scoped. ~4
#: deg^2 ~ a large region while staying tractable at 30 m (capped pixel grid).
_MAX_BBOX_DEG2 = 4.0


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_copernicus_dem",
    ttl_class="static-30d",
    source_class="copernicus_dem",
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
    """Estimate emitted single-band float32 DEM COG size in MB.

    A DEFLATE-compressed single-band float32 COG at 30 m runs ~8 MB / sq-deg for
    varied terrain (the Alps proto: 0.2 x 0.15 deg ~ 0.03 sq-deg -> ~1.2 MB,
    i.e. ~40 MB/sq-deg raw, ~8 MB/sq-deg compressed). Scale linearly with bbox
    area, floored. Intentionally conservative so the payload-warning gate fires.
    """
    if bbox is None:
        return 3.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 3.0
    return max(0.1, sq_deg * 8.0)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise CopernicusDemBboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise CopernicusDemBboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise CopernicusDemBboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise CopernicusDemBboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise CopernicusDemBboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    area = (max_lon - min_lon) * (max_lat - min_lat)
    if area > _MAX_BBOX_DEG2:
        raise CopernicusDemBboxError(
            f"bbox area {area:.3f} deg^2 exceeds {_MAX_BBOX_DEG2} deg^2 guardrail "
            "for fetch_copernicus_dem (DEM is AOI-scoped; narrow the bbox or use "
            "a tiled workflow for a very large domain)."
        )


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# PC per-href signing (container-scoped; the shared token endpoint 403s here).
# ---------------------------------------------------------------------------


def _sign_href(href: str) -> str:
    """Return ``href`` signed via the PC per-href sign endpoint.

    Raises ``CopernicusDemUpstreamError`` on any network / parse failure.
    """
    try:
        resp = requests.get(
            _PC_SIGN_URL,
            params={"href": href},
            headers={"User-Agent": _pc_stac.USER_AGENT},
            timeout=_SIGN_TIMEOUT_S,
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as exc:
        raise CopernicusDemUpstreamError(
            f"PC sign?href request failed for {href[:120]!r}: {exc}"
        ) from exc
    except ValueError as exc:  # JSON decode
        raise CopernicusDemUpstreamError(
            f"PC sign?href response was not JSON for {href[:120]!r}: {exc}"
        ) from exc
    signed = body.get("href")
    if not signed or not isinstance(signed, str):
        raise CopernicusDemUpstreamError(
            f"PC sign?href response had no signed href for {href[:120]!r}: "
            f"{str(body)[:200]!r}"
        )
    return signed


# ---------------------------------------------------------------------------
# Core: STAC search -> per-tile windowed read -> first-wins mosaic -> DEM COG.
# ---------------------------------------------------------------------------


def _search_tiles(bbox: tuple[float, float, float, float]) -> list[Any]:
    """Return the cop-dem-glo-30 items intersecting ``bbox``.

    Raises ``CopernicusDemEmptyError`` on zero matches (honest no-coverage),
    ``CopernicusDemUpstreamError`` on a search failure.
    """
    try:
        from pystac_client import Client
    except ImportError as exc:  # pragma: no cover  --  hard dep
        raise CopernicusDemUpstreamError(
            f"pystac-client unavailable; cannot search PC STAC: {exc}"
        ) from exc
    try:
        client = Client.open(_pc_stac.PC_STAC_ROOT)
        search = client.search(
            collections=[_COLLECTION], bbox=list(bbox), limit=100
        )
        items = list(search.items())
    except Exception as exc:  # noqa: BLE001  --  translate any pystac/http error
        raise CopernicusDemUpstreamError(
            f"Copernicus DEM STAC search failed (bbox={bbox}): {exc}"
        ) from exc
    if not items:
        raise CopernicusDemEmptyError(
            f"no Copernicus GLO-30 tiles intersect bbox={bbox} (GLO-30 is "
            "global; check the bbox is a valid on-globe extent)."
        )
    return items


def _read_tile_window(
    signed_href: str,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
) -> Any:
    """Read ``signed_href`` reprojected to EPSG:4326 windowed to ``bbox``.

    Returns a 2-D float32 numpy array at ``(height_px, width_px)`` with nodata as
    NaN. Raises ``CopernicusDemUpstreamError`` on any read failure.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import reproject, Resampling

    vsicurl = "/vsicurl/" + signed_href
    try:
        with rasterio.Env(**_pc_stac.VSICURL_ENV_KW):
            with rasterio.open(vsicurl) as src:
                dst_transform = rasterio.transform.from_bounds(
                    bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
                )
                dst = np.full((height_px, width_px), np.nan, dtype="float32")
                reproject(
                    source=rasterio.band(src, 1),
                    destination=dst,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=dst_transform,
                    dst_crs="EPSG:4326",
                    resampling=Resampling.bilinear,
                    src_nodata=src.nodata,
                    dst_nodata=np.nan,
                )
        return dst
    except Exception as exc:  # noqa: BLE001  --  translate any rasterio/GDAL error
        raise CopernicusDemUpstreamError(
            f"Copernicus DEM tile read failed (href={signed_href[:120]!r}): {exc}"
        ) from exc


def _write_dem_cog(
    dem: Any,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
) -> bytes:
    """Write a single-band float32 DEM COG (publish_layer continuous_dem ramp)."""
    import numpy as np
    import rasterio

    transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )
    out = np.where(np.isfinite(dem), dem, _NODATA).astype("float32")
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_copdem_"
        ) as f:
            tmp_path = f.name
        profile = dict(
            driver="COG",
            dtype="float32",
            count=1,
            height=height_px,
            width=width_px,
            crs="EPSG:4326",
            transform=transform,
            compress="DEFLATE",
            nodata=_NODATA,
        )
        with rasterio.open(tmp_path, "w", **profile) as dst:
            dst.write(out, 1)
        with open(tmp_path, "rb") as fh:
            return fh.read()
    except Exception as exc:  # noqa: BLE001
        raise CopernicusDemUpstreamError(
            f"Copernicus DEM COG write failed for bbox={bbox}: {exc}"
        ) from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _fetch_copernicus_dem_bytes(
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Search Copernicus GLO-30, mosaic the bbox, return a float32 DEM COG.

    Raises:
        ``CopernicusDemEmptyError``: no tile, or an all-nodata window (honest).
        ``CopernicusDemUpstreamError``: search / sign / read / write failure.
    """
    import numpy as np

    items = _search_tiles(bbox)
    width_px, height_px = _pc_stac.bbox_pixel_dims(bbox, _NATIVE_CELL_M)

    mosaic = np.full((height_px, width_px), np.nan, dtype="float32")
    used = 0
    for item in items:
        assets = getattr(item, "assets", {}) or {}
        asset = assets.get(_DATA_ASSET)
        if asset is None:
            continue
        signed = _sign_href(asset.href)
        tile = _read_tile_window(signed, bbox, width_px, height_px)
        # First-wins fill: only write where the mosaic is still nodata. GLO-30
        # tiles abut on a clean 1-deg grid so overlap is edge-thin; first-wins is
        # deterministic + avoids double-counting.
        fillmask = np.isnan(mosaic) & np.isfinite(tile)
        mosaic[fillmask] = tile[fillmask]
        used += 1

    valid = np.isfinite(mosaic)
    if used == 0 or not valid.any():
        raise CopernicusDemEmptyError(
            f"Copernicus GLO-30 produced an all-nodata window over bbox={bbox} "
            f"(tiles_read={used}); no valid elevation pixels to render."
        )

    cog_bytes = _write_dem_cog(mosaic, bbox, width_px, height_px)

    logger.info(
        "fetch_copernicus_dem: bbox=%s tiles=%d -> %d-byte float32 DEM COG "
        "(%dx%d) elev %.1f..%.1f m valid=%d/%d",
        bbox,
        used,
        len(cog_bytes),
        width_px,
        height_px,
        float(np.nanmin(mosaic)),
        float(np.nanmax(mosaic)),
        int(valid.sum()),
        mosaic.size,
    )
    return cog_bytes


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


def _copernicus_dem_impl(
    bbox: tuple[float, float, float, float],
) -> LayerURI:
    """Shared Copernicus GLO-30 implementation.

    Reached via ``fetch_dem(bbox, source="copernicus")`` (canonical surface) and
    via the DEPRECATED ``fetch_copernicus_dem`` delegate (backward compat).

    **What it does:** STAC-searches the Microsoft Planetary Computer for the
    Copernicus DEM GLO-30 (Copernicus_DSM, 30 m global) tiles covering ``bbox``,
    reads each tile clipped to the bbox, first-wins mosaics them, and returns a
    single-band float32 (meters) Cloud-Optimized GeoTIFF rendered with the
    ``continuous_dem`` terrain ramp.

    **When to use:**
    - Terrain elevation for ANY non-US area  --  the Alps, the Andes, the
      Himalaya, an African watershed, etc. ``fetch_dem`` (USGS 3DEP) is US-only;
      this is the keyless global complement.
    - A global before/after or multi-region study where a single consistent DEM
      source is wanted regardless of country.
    - Any terrain step (slope / hillshade / aspect / colored relief / zonal
      stats / a hydrology or flood model) over a non-US AOI that needs a DEM.

    **When NOT to use:**
    - A US AOI where the finer 10 m 3DEP product is preferred  --  use
      ``fetch_dem`` (10 m vs GLO-30's 30 m), or ``fetch_3dep_extra`` for the
      other 3DEP resolutions.
    - Bathymetry (below-water depth)  --  GLO-30 is a surface model (ocean reads
      ~0 m); use a future ``fetch_bathymetry`` for sea-floor depth.
    - Bboxes larger than 4 deg^2  --  the tool raises ``CopernicusDemBboxError``
      at that threshold; tile a very large domain.

    **Parameters:**
    - ``bbox`` (tuple[float,float,float,float]): ``(min_lon, min_lat, max_lon,
      max_lat)`` in EPSG:4326. Required. AOI-scoped (<= 4 deg^2).

    **Returns:** A ``LayerURI`` (``layer_type="raster"``, ``role="input"``,
    ``units="meters"``) pointing at a single-band float32 DEM COG in the
    ``static-30d`` / ``copernicus_dem`` cache prefix.
    ``style_preset="continuous_dem"`` (the terrain ramp 3DEP / terrain tools use).
    CRS EPSG:4326; elevations are meters (Copernicus DEM is EGM2008-referenced).

    **Data source:** Copernicus DEM GLO-30 via the Microsoft Planetary Computer
    STAC (``cop-dem-glo-30``; single-band ``data`` asset). Keyless (no API key,
    no Earthdata login). Honest typed ``CopernicusDemEmptyError`` on no coverage
    (only possible for an off-globe request)  --  never a fabricated layer.

    **Cross-tool dependencies:**
    - Downstream: ``compute_slope``, ``compute_hillshade``, ``compute_aspect``,
      ``compute_colored_relief``, ``compute_zonal_statistics``, and any
      DEM-consuming hydrology / flood setup over a non-US AOI.
    - Typically called after: ``geocode_location`` supplies the bbox.
    - Sibling: ``fetch_dem`` (USGS 3DEP, US-only 10 m). Prefer ``fetch_dem``
      inside the US; use this tool everywhere else.

    FR-CE-8: routed through ``read_through`` so identical ``bbox`` calls reuse
    the cached DEM COG.
    """
    _validate_bbox(bbox)
    q_bbox = _round_bbox(bbox)

    params = {"bbox": list(q_bbox), "collection": _COLLECTION}

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_copernicus_dem_bytes(q_bbox),
    )
    assert result.uri is not None, (
        "fetch_copernicus_dem is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=(
            f"copdem-glo30-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name="Copernicus GLO-30 DEM (30m)",
        layer_type="raster",
        uri=result.uri,
        style_preset=_STYLE_PRESET,
        role="input",
        units="meters",
        bbox=q_bbox,
    )


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (PC STAC public API),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_copernicus_dem(
    bbox: tuple[float, float, float, float],
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """DEPRECATED alias of ``fetch_dem`` with ``source="copernicus"``.

    Retained as a thin registered delegate for backward compatibility (existing
    cases + the routing bench). New callers should use ``fetch_dem`` with
    ``source="copernicus"`` -- the GLOBAL Copernicus GLO-30 30 m elevation model
    is now a source mode of the one DEM tool, not a separate sibling.

    Fetches a GLOBAL 30 m elevation model (Copernicus GLO-30) for ``bbox`` via the
    Microsoft Planetary Computer STAC and returns a single-band float32 (meters)
    Cloud-Optimized GeoTIFF (``style_preset="continuous_dem"``). Use it (or
    ``fetch_dem(source="copernicus")``) for terrain OUTSIDE the US, where the
    default 3DEP source has no coverage.
    """
    return _copernicus_dem_impl(bbox)
