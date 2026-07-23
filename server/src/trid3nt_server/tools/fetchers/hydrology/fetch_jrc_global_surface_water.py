"""``fetch_jrc_global_surface_water`` atomic tool  --  JRC Global Surface Water.
"""

from __future__ import annotations

import logging
import math
import os
import tempfile
import time
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.fetchers.imagery import _pc_stac
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_jrc_global_surface_water",
    "estimate_payload_mb",
    "JrcSurfaceWaterError",
    "JrcSurfaceWaterBboxError",
    "JrcSurfaceWaterBandError",
    "JrcSurfaceWaterNoCoverageError",
    "JrcSurfaceWaterUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.hydrology.fetch_jrc_global_surface_water")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class JrcSurfaceWaterError(RuntimeError):
    """Base class for fetch_jrc_global_surface_water failures."""

    error_code = "JRC_GSW_ERROR"
    retryable = True


class JrcSurfaceWaterBboxError(JrcSurfaceWaterError):
    """Malformed / out-of-range / degenerate / too-large bbox."""

    error_code = "JRC_GSW_BBOX_INVALID"
    retryable = False


class JrcSurfaceWaterBandError(JrcSurfaceWaterError):
    """Requested band is not one of the supported GSW statistics."""

    error_code = "JRC_GSW_BAND_INVALID"
    retryable = False


class JrcSurfaceWaterNoCoverageError(JrcSurfaceWaterError):
    """No jrc-gsw item covers the bbox, or the mosaic is entirely no-data.

    Honest no-coverage signal (data-source fallback norm)  --  e.g. an ocean AOI
    or a fully-dry inland AOI with no surface water in the 38-year record.
    """

    error_code = "JRC_GSW_NO_COVERAGE"
    retryable = False


class JrcSurfaceWaterUpstreamError(JrcSurfaceWaterError):
    """A PC STAC search / SAS-sign / asset read / COG write failed at the net layer."""

    error_code = "JRC_GSW_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_COLLECTION = "jrc-gsw"

#: PC /sign REST endpoint  --  signs an arbitrary blob href (the canonical path
#: the official planetary-computer SDK uses). The per-collection /token path is
#: NOT accepted by the jrc-gsw blob container (AuthenticationFailed), so we sign
#: each full href here instead of appending a per-collection token.
_PC_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"
_SIGN_TIMEOUT_S = 30.0
_SIGN_RETRIES = 4

#: JRC GSW native grid (30 m); used to size the bbox-windowed read.
_NATIVE_CELL_M = 30.0

#: 6-dp bbox quantization (~0.1 m) for cache-key stability.
_BBOX_DECIMALS = 6

#: bbox area guardrail (deg^2). 30 m surface water over a huge AOI would span
#: many 10-degree tiles + materialize an enormous grid; the atomic-tool surface
#: is AOI-scoped. ~2 deg^2 ~ a large metro / sub-basin extent.
_MAX_BBOX_DEG2 = 2.0

#: Per-band spec: (output nodata value, human label, units, default style token).
#: ``nodata`` is the source value that means "no water / not measured" and is
#: rendered transparent. For occurrence/recurrence/seasonality that is 0 (never
#: water); for change it is 253 (the JRC "not water" sentinel).
_BANDS: dict[str, dict[str, Any]] = {
    "occurrence": {
        "nodata": 0,
        "label": "Water occurrence",
        "units": "percent_of_time_water_1984_2021",
        "style": "water_occurrence_pct",
    },
    "recurrence": {
        "nodata": 0,
        "label": "Water recurrence",
        "units": "percent_interannual_recurrence",
        "style": "water_recurrence_pct",
    },
    "seasonality": {
        "nodata": 0,
        "label": "Water seasonality",
        "units": "months_water_present_per_year",
        "style": "water_seasonality_months",
    },
    "change": {
        "nodata": 253,
        "label": "Water occurrence change",
        "units": "change_intensity_100_is_no_change",
        "style": "water_change_intensity",
    },
}
_DEFAULT_BAND = "occurrence"


# ---------------------------------------------------------------------------
# Per-band GDAL color tables (value -> (R, G, B, A)). Baked onto band 1 so
# publish_layer's embedded-palette passthrough colorizes the layer directly,
# independent of the single-band TiTiler style registry (which we cannot edit).
# ---------------------------------------------------------------------------


def _blue_ramp_colormap(nodata: int, vmax: int) -> dict[int, tuple[int, int, int, int]]:
    """White(low)->deep-blue(high) ramp over [1..vmax]; ``nodata`` transparent.

    Used for occurrence / recurrence (0..100 %). Every value in [0, 255] gets an
    entry so GDAL writes a complete palette; ``nodata`` is fully transparent.
    """
    cmap: dict[int, tuple[int, int, int, int]] = {}
    for v in range(256):
        if v == nodata or v > vmax:
            cmap[v] = (0, 0, 0, 0)
            continue
        t = max(0.0, min(1.0, v / float(vmax)))
        # white (247,251,255) -> deep blue (8,48,107)  (rio-tiler "blues" feel)
        r = int(round(247 - t * (247 - 8)))
        g = int(round(251 - t * (251 - 48)))
        b = int(round(255 - t * (255 - 107)))
        cmap[v] = (r, g, b, 255)
    return cmap


def _seasonality_colormap() -> dict[int, tuple[int, int, int, int]]:
    """12-step blue ramp over months 1..12; 0 (no water) transparent."""
    cmap: dict[int, tuple[int, int, int, int]] = {}
    for v in range(256):
        if v == 0 or v > 12:
            cmap[v] = (0, 0, 0, 0)
            continue
        t = (v - 1) / 11.0
        r = int(round(229 - t * (229 - 8)))
        g = int(round(245 - t * (245 - 48)))
        b = int(round(249 - t * (249 - 107)))
        cmap[v] = (r, g, b, 255)
    return cmap


def _change_colormap() -> dict[int, tuple[int, int, int, int]]:
    """Diverging red(loss)->white(no change=100)->blue(gain) over [0..200].

    JRC change intensity: 0 = full loss, 100 = no change, 200 = full gain.
    253 (not water) transparent; values 201..252 and 254..255 transparent.
    """
    cmap: dict[int, tuple[int, int, int, int]] = {}
    for v in range(256):
        if v > 200:
            cmap[v] = (0, 0, 0, 0)
            continue
        if v <= 100:
            # red (178,24,43) -> white (247,247,247)
            t = v / 100.0
            r = int(round(178 + t * (247 - 178)))
            g = int(round(24 + t * (247 - 24)))
            b = int(round(43 + t * (247 - 43)))
        else:
            # white (247,247,247) -> blue (33,102,172)
            t = (v - 100) / 100.0
            r = int(round(247 - t * (247 - 33)))
            g = int(round(247 - t * (247 - 102)))
            b = int(round(247 - t * (247 - 172)))
        cmap[v] = (r, g, b, 255)
    return cmap


def _band_colormap(band: str) -> dict[int, tuple[int, int, int, int]]:
    if band in ("occurrence", "recurrence"):
        return _blue_ramp_colormap(nodata=0, vmax=100)
    if band == "seasonality":
        return _seasonality_colormap()
    if band == "change":
        return _change_colormap()
    raise JrcSurfaceWaterBandError(f"no colormap for band {band!r}")


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_jrc_global_surface_water",
    ttl_class="static-30d",
    source_class="jrc_global_surface_water",
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
    """Estimate emitted single-band paletted COG size in MB.

    A 1-band uint8 DEFLATE-COG of surface water at 30 m is mostly transparent
    nodata (land) with sparse water runs, so it compresses hard; empirically
    ~3 MB / sq-deg. Scale linearly with bbox area, floored.
    """
    if bbox is None:
        return 1.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 1.0
    return max(0.2, sq_deg * 3.0)


# ---------------------------------------------------------------------------
# bbox / band helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise JrcSurfaceWaterBboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise JrcSurfaceWaterBboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise JrcSurfaceWaterBboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise JrcSurfaceWaterBboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise JrcSurfaceWaterBboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    area = (max_lon - min_lon) * (max_lat - min_lat)
    if area > _MAX_BBOX_DEG2:
        raise JrcSurfaceWaterBboxError(
            f"bbox area {area:.3f} deg^2 exceeds {_MAX_BBOX_DEG2} deg^2 guardrail "
            "for fetch_jrc_global_surface_water (30 m global surface water is "
            "AOI-scoped; narrow the bbox)."
        )


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


def _resolve_band(band: str | None) -> str:
    """Normalize/validate the requested band; default to occurrence."""
    if band is None:
        return _DEFAULT_BAND
    key = str(band).strip().lower()
    if key not in _BANDS:
        raise JrcSurfaceWaterBandError(
            f"band must be one of {sorted(_BANDS)}; got {band!r}"
        )
    return key


def _bbox_intersects(
    item_bbox: Any, bbox: tuple[float, float, float, float]
) -> bool:
    """True iff ``item_bbox`` (min_lon, min_lat, max_lon, max_lat) overlaps bbox."""
    try:
        ib0, ib1, ib2, ib3 = (
            float(item_bbox[0]),
            float(item_bbox[1]),
            float(item_bbox[2]),
            float(item_bbox[3]),
        )
    except (TypeError, ValueError, IndexError):
        return False
    return not (ib2 < bbox[0] or ib0 > bbox[2] or ib3 < bbox[1] or ib1 > bbox[3])


# ---------------------------------------------------------------------------
# PC /sign href signing (jrc-gsw per-collection token is NOT accepted by the
# blob; the /sign endpoint signs an arbitrary href and is the canonical path).
# ---------------------------------------------------------------------------


def _sign_href(href: str) -> str:
    """Sign a blob ``href`` through the PC /sign REST endpoint (with retries).

    Raises ``JrcSurfaceWaterUpstreamError`` on persistent failure.
    """
    import requests

    last_exc: Exception | None = None
    for attempt in range(_SIGN_RETRIES):
        try:
            resp = requests.get(
                _PC_SIGN_URL,
                params={"href": href},
                headers={"User-Agent": _pc_stac.USER_AGENT},
                timeout=_SIGN_TIMEOUT_S,
            )
            if resp.status_code == 200:
                signed = resp.json().get("href")
                if signed:
                    return signed
                raise JrcSurfaceWaterUpstreamError(
                    f"PC /sign response had no signed href for {href[:120]!r}"
                )
            # 429 / transient 5xx -> back off and retry.
            last_exc = JrcSurfaceWaterUpstreamError(
                f"PC /sign returned {resp.status_code} for {href[:120]!r}"
            )
        except requests.RequestException as exc:
            last_exc = JrcSurfaceWaterUpstreamError(
                f"PC /sign request failed for {href[:120]!r}: {exc}"
            )
        time.sleep(1.5 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Core: search -> per-tile warp/window read -> mosaic -> palette COG.
# ---------------------------------------------------------------------------


def _select_items(bbox: tuple[float, float, float, float]) -> list[Any]:
    """Return jrc-gsw items whose footprint intersects ``bbox``.

    Raises ``JrcSurfaceWaterNoCoverageError`` when zero items match (honest
    no-coverage signal) and ``JrcSurfaceWaterUpstreamError`` on a search failure.
    """
    try:
        from pystac_client import Client
    except ImportError as exc:  # pragma: no cover  --  pystac_client is a hard dep
        raise JrcSurfaceWaterUpstreamError(
            f"pystac-client unavailable; cannot search PC STAC: {exc}"
        ) from exc

    try:
        client = Client.open(_pc_stac.PC_STAC_ROOT)
        search = client.search(
            collections=[_COLLECTION],
            bbox=list(bbox),
            limit=100,
        )
        all_items = list(search.items())
    except Exception as exc:  # noqa: BLE001  --  translate any pystac/http error
        raise JrcSurfaceWaterUpstreamError(
            f"PC STAC search failed (collection={_COLLECTION!r}, bbox={bbox}): {exc}"
        ) from exc

    items = [
        it
        for it in all_items
        if _bbox_intersects(getattr(it, "bbox", None), bbox)
    ]
    if not items:
        raise JrcSurfaceWaterNoCoverageError(
            f"no {_COLLECTION!r} surface-water item covers bbox={bbox}."
        )
    return items


def _read_tile_window(
    signed_href: str,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
    nodata: int,
) -> Any:
    """Warp+window-read a tile's band-1 to EPSG:4326 at ``bbox``.

    Returns a uint8 array (H, W) with ``nodata`` filling pixels with no data.
    Bilinear resampling (continuous percent/month statistics). Raises
    ``JrcSurfaceWaterUpstreamError`` on any read failure.
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
                dst = np.full((height_px, width_px), nodata, dtype="uint8")
                src_nodata = src.nodata if src.nodata is not None else nodata
                reproject(
                    source=rasterio.band(src, 1),
                    destination=dst,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=dst_transform,
                    dst_crs="EPSG:4326",
                    resampling=Resampling.bilinear,
                    src_nodata=src_nodata,
                    dst_nodata=nodata,
                )
        return dst
    except JrcSurfaceWaterError:
        raise
    except Exception as exc:  # noqa: BLE001  --  translate any rasterio/GDAL error
        raise JrcSurfaceWaterUpstreamError(
            f"JRC GSW tile read failed (href={signed_href[:120]!r}): {exc}"
        ) from exc


def _fetch_surface_water_cog_bytes(
    bbox: tuple[float, float, float, float],
    band: str,
) -> bytes:
    """Search + mosaic jrc-gsw tiles for ``bbox``/``band`` -> palette COG bytes.

    Raises:
        ``JrcSurfaceWaterNoCoverageError``: no item / all-nodata mosaic (honest).
        ``JrcSurfaceWaterUpstreamError``: search / sign / read / write failure.
    """
    import numpy as np
    import rasterio

    nodata = int(_BANDS[band]["nodata"])
    items = _select_items(bbox)

    width_px, height_px = _pc_stac.bbox_pixel_dims(bbox, _NATIVE_CELL_M)
    dst_transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )

    # Mosaic: first valid (non-nodata) pixel wins.
    mosaic = np.full((height_px, width_px), nodata, dtype="uint8")
    filled = np.zeros((height_px, width_px), dtype=bool)
    tiles_used = 0
    for item in items:
        assets = getattr(item, "assets", {}) or {}
        if band not in assets:
            continue
        href = _sign_href(assets[band].href)
        tile = _read_tile_window(href, bbox, width_px, height_px, nodata)
        valid = (tile != nodata) & (~filled)
        if valid.any():
            mosaic[valid] = tile[valid]
            filled |= valid
            tiles_used += 1

    if not bool(filled.any()):
        # Items existed but no real surface-water coverage over this AOI (ocean,
        # or a fully-dry inland AOI in the 38-year record).
        raise JrcSurfaceWaterNoCoverageError(
            f"{_COLLECTION!r} items intersected bbox={bbox}, but the {band} mosaic "
            "is entirely no-data over the AOI (no surface water in the record)."
        )

    colormap = _band_colormap(band)

    # Write a single-band uint8 palette COG (publish_layer embedded-palette path).
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_jrc_gsw_"
        ) as f:
            tmp_path = f.name
        profile = dict(
            driver="COG",
            dtype="uint8",
            count=1,
            height=height_px,
            width=width_px,
            crs="EPSG:4326",
            transform=dst_transform,
            compress="DEFLATE",
            nodata=nodata,
        )
        with rasterio.open(tmp_path, "w", **profile) as dst:
            dst.write(mosaic, 1)
            dst.write_colormap(1, colormap)
            try:
                from rasterio.enums import ColorInterp

                interp = list(dst.colorinterp)
                interp[0] = ColorInterp.palette
                dst.colorinterp = tuple(interp)
            except Exception:  # noqa: BLE001  --  colorinterp set is best-effort
                pass
        with open(tmp_path, "rb") as fh:
            cog_bytes = fh.read()
    except Exception as exc:  # noqa: BLE001
        raise JrcSurfaceWaterUpstreamError(
            f"JRC GSW COG write failed for bbox={bbox} band={band}: {exc}"
        ) from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    valid_px = int(filled.sum())
    water = mosaic[filled]
    logger.info(
        "fetch_jrc_global_surface_water: band=%s bbox=%s tiles=%d -> %d-byte "
        "1-band palette COG (%dx%d) valid_px=%d (%.1f%% of AOI) value_range=[%d,%d]",
        band,
        bbox,
        tiles_used,
        len(cog_bytes),
        width_px,
        height_px,
        valid_px,
        100.0 * valid_px / float(width_px * height_px),
        int(water.min()),
        int(water.max()),
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
def fetch_jrc_global_surface_water(
    bbox: tuple[float, float, float, float],
    band: str | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch JRC Global Surface Water long-term statistics for a bbox.

    **What it does:** Searches the Microsoft Planetary Computer for the JRC
    Global Surface Water ``jrc-gsw`` 30 m items covering ``bbox``, warps+window-
    reads the requested ``band`` of each intersecting 10-degree tile to EPSG:4326
    at 30 m, mosaics them, and returns a single-band uint8 COG with a
    band-appropriate color ramp baked in. ``publish_layer`` colorizes it directly
    from the embedded palette (no rescale/colormap override).

    GSW is the European Commission JRC's 38-year (1984-2021) Landsat-derived
    surface-water record reduced to per-pixel statistics  --  the long-term
    BASELINE that answers "is this water permanent or seasonal?", "where has
    water EVER been (the flood envelope)?", and "is this lake growing or
    shrinking?".

    **Bands** (``band`` parameter):
    - ``occurrence`` (default): 0..100 % of the time water was present over the
      whole record. The flood-frequency / where-water-ever-was layer.
    - ``recurrence``: 0..100 % inter-annual recurrence (permanent water ~ 100 %,
      seasonal lakes lower).
    - ``seasonality``: 0..12 months of water per year (12 = permanent).
    - ``change``: 0..200 intensity of change in occurrence between the
      1984-1999 and 2000-2021 epochs (100 = no change, < 100 = loss, > 100 =
      gain).

    **When to use:**
    - "Show permanent vs seasonal water" / "where is water all year?" -> default
      ``occurrence`` (or ``seasonality`` for the month count).
    - "Where has the river EVER flooded?" (the long-term flood envelope) ->
      ``occurrence``.
    - "Is this reservoir / lake shrinking?" -> ``change``.
    - A long-term water BASELINE to compare against a current NDWI snapshot from
      ``digitize_water_body``.

    **When NOT to use:**
    - Outlining water in ONE recent scene -> ``digitize_water_body`` (NDWI) or
      ``fetch_sentinel2_truecolor``.
    - A modeled flood DEPTH surface -> the SFINCS / SWMM engines.
    - FEMA regulatory flood ZONES -> ``fetch_fema_nfhl_zones``.

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326.
      Required. AOI-scoped (<= 2.0 deg^2).
    - ``band`` (str, optional): one of ``occurrence`` / ``recurrence`` /
      ``seasonality`` / ``change``. Default ``occurrence``.

    **Returns:** A ``LayerURI`` (``layer_type="raster"``, ``role="input"``)
    pointing at a single-band paletted COG in the ``static-30d`` /
    ``jrc_global_surface_water`` cache prefix. The embedded palette renders the
    band's ramp directly (occurrence/recurrence white->blue, seasonality 12-step
    blue, change red->white->blue diverging).

    **Data source:** JRC Global Surface Water (European Commission Joint Research
    Centre) via the Microsoft Planetary Computer STAC (``jrc-gsw``).

    Honesty: no covering item, or an all-nodata mosaic (ocean / fully-dry AOI),
    raises a typed ``JrcSurfaceWaterNoCoverageError``  --  never a fabricated
    layer.

    FR-CE-8: routed through ``read_through`` so identical ``(bbox, band)`` calls
    reuse the cached COG.
    """
    _validate_bbox(bbox)
    q_bbox = _round_bbox(bbox)
    q_band = _resolve_band(band)
    spec = _BANDS[q_band]

    params = {
        "bbox": list(q_bbox),
        "band": q_band,
        "collection": _COLLECTION,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_surface_water_cog_bytes(q_bbox, q_band),
    )
    assert result.uri is not None, (
        "fetch_jrc_global_surface_water is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=(
            f"jrc-gsw-{q_band}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=f"JRC Global Surface Water ({spec['label']})",
        layer_type="raster",
        uri=result.uri,
        # Embedded band-1 palette wins in publish_layer step 1, so this token is
        # descriptive only and never reaches the single-band rescale registry.
        style_preset=str(spec["style"]),
        role="input",
        units=str(spec["units"]),
        bbox=q_bbox,
    )
