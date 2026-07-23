"""``fetch_sentinel2_truecolor`` atomic tool  --  Sentinel-2 L2A true-color RGB.
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
    "fetch_sentinel2_truecolor",
    "estimate_payload_mb",
    "S2TrueColorError",
    "S2TrueColorBboxError",
    "S2TrueColorNoImageryError",
    "S2TrueColorUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.imagery.fetch_sentinel2_truecolor")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class S2TrueColorError(RuntimeError):
    """Base class for fetch_sentinel2_truecolor failures."""

    error_code = "S2_TRUECOLOR_ERROR"
    retryable = True


class S2TrueColorBboxError(S2TrueColorError):
    """Malformed / out-of-range / degenerate / too-large bbox."""

    error_code = "S2_TRUECOLOR_BBOX_INVALID"
    retryable = False


class S2TrueColorNoImageryError(S2TrueColorError):
    """No Sentinel-2 scene covers the bbox in the window under the cloud cap.

    Honest no-imagery signal (data-source fallback norm)  --  never fabricate.
    """

    error_code = "S2_TRUECOLOR_NO_IMAGERY"
    retryable = False


class S2TrueColorUpstreamError(S2TrueColorError):
    """A PC STAC search / asset read / COG write failed."""

    error_code = "S2_TRUECOLOR_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_COLLECTION = "sentinel-2-l2a"
_RED_BAND = "B04"
_GREEN_BAND = "B03"
_BLUE_BAND = "B02"
_SCL_BAND = "SCL"

#: Sentinel-2 native visible-band grid; used to size the bbox-windowed read.
_NATIVE_CELL_M = 10.0

#: SCL (Scene Classification Layer) class values to MASK as cloud / bad:
#:   0  = no-data
#:   1  = saturated or defective
#:   3  = cloud shadow
#:   8  = cloud, medium probability
#:   9  = cloud, high probability
#:   10 = thin cirrus
#: (2 = dark area, 4 = vegetation, 5 = bare soil, 6 = water, 7 = unclassified,
#:  11 = snow/ice are KEPT.)
_SCL_MASK_CLASSES = (0, 1, 3, 8, 9, 10)

#: Default cloud-cover ceiling (percent) for scene selection. Generous so a
#: typical AOI finds a usable scene; the least-cloudy match is then chosen.
_DEFAULT_MAX_CLOUD = 30.0

#: Joint percentile stretch bounds (over clear pixels) for the uint16 -> uint8
#: true-color conversion. 2nd/98th keeps deep shadows + bright surfaces.
_STRETCH_LO_PCT = 2.0
_STRETCH_HI_PCT = 98.0

#: bbox area guardrail (deg^2). True-color over a huge AOI would span many MGRS
#: tiles + materialize an enormous grid; the atomic-tool surface is AOI-scoped.
#: ~0.5 deg^2 ~ a county-ish extent (matches compute_ndvi).
_MAX_BBOX_DEG2 = 0.5

#: 6-dp bbox quantization (~0.1 m) for cache-key stability.
_BBOX_DECIMALS = 6

#: Multiband-passthrough style token. publish_layer's RGBA/multiband probe
#: (>=3 bands) renders the COG directly; this token is intentionally NOT in the
#: single-band TiTiler style registry, so no rescale / colormap is applied.
_STYLE_PRESET = "s2_truecolor"


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_sentinel2_truecolor",
    ttl_class="static-30d",
    source_class="s2_truecolor",
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

    A 3-band uint8 DEFLATE-COG at 10 m runs ~120 MB / sq-deg for varied
    surface; true-color natural scenes compress moderately. Scale linearly with
    bbox area, floored.
    """
    if bbox is None:
        return 8.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 8.0
    return max(0.5, sq_deg * 120.0)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise S2TrueColorBboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise S2TrueColorBboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise S2TrueColorBboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise S2TrueColorBboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise S2TrueColorBboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    area = (max_lon - min_lon) * (max_lat - min_lat)
    if area > _MAX_BBOX_DEG2:
        raise S2TrueColorBboxError(
            f"bbox area {area:.3f} deg^2 exceeds {_MAX_BBOX_DEG2} deg^2 guardrail "
            "for fetch_sentinel2_truecolor (Sentinel-2 true-color is AOI-scoped; "
            "narrow the bbox)."
        )


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


def _default_window() -> tuple[str, str]:
    """Default datetime window: a trailing ~3-month recent window.

    Returns ``(start_iso, end_iso)`` as ``YYYY-MM-DD`` strings. ~90 days gives
    the 5-day-revisit Sentinel-2 several passes so a recent low-cloud scene is
    reliably found while staying "recent".
    """
    from datetime import datetime, timedelta, timezone

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=90)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# Core: search -> per-band windowed read -> cloud-mask + stretch -> RGB COG.
# ---------------------------------------------------------------------------


def _read_band_window(
    signed_href: str,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
    *,
    nearest: bool = False,
) -> Any:
    """Read ``signed_href`` warped to EPSG:4326 and windowed to ``bbox``.

    Returns a 2-D float32 numpy array at ``(height_px, width_px)``; nodata reads
    back as 0.0. Use ``nearest=True`` for the categorical SCL band.
    Raises ``S2TrueColorUpstreamError`` on any read failure.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import reproject, Resampling

    vsicurl = "/vsicurl/" + signed_href
    resampling = Resampling.nearest if nearest else Resampling.bilinear
    try:
        with rasterio.Env(**_pc_stac.VSICURL_ENV_KW):
            with rasterio.open(vsicurl) as src:
                dst_transform = rasterio.transform.from_bounds(
                    bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
                )
                dst = np.zeros((height_px, width_px), dtype="float32")
                reproject(
                    source=rasterio.band(src, 1),
                    destination=dst,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=dst_transform,
                    dst_crs="EPSG:4326",
                    resampling=resampling,
                    src_nodata=src.nodata if src.nodata is not None else 0,
                    dst_nodata=0,
                )
        return dst
    except S2TrueColorError:
        raise
    except Exception as exc:  # noqa: BLE001  --  translate any rasterio/GDAL error
        raise S2TrueColorUpstreamError(
            f"Sentinel-2 band read failed (href={signed_href[:120]!r}): {exc}"
        ) from exc


def _truecolor_from_bands(red: Any, green: Any, blue: Any, scl: Any) -> Any:
    """Cloud-mask + percentile-stretch 3 uint16 bands into a 3-band uint8 array.

    Returns ``rgb`` of shape ``(3, H, W)`` uint8 with cloud / nodata pixels
    zeroed (black no-data). Raises ``S2TrueColorNoImageryError`` when every
    pixel is masked (scene does not actually cover the AOI / fully clouded).
    """
    import numpy as np

    cloud_mask = np.isin(scl.astype("int16"), np.asarray(_SCL_MASK_CLASSES))
    nodata_mask = (red == 0) & (green == 0) & (blue == 0)
    bad = cloud_mask | nodata_mask
    clear = ~bad

    stack = np.stack([red, green, blue])
    clear_vals = stack[:, clear]
    if clear_vals.size == 0:
        raise S2TrueColorNoImageryError(
            "Sentinel-2 scene produced an all-cloud / all-nodata window over the "
            "AOI (no clear pixels to render)."
        )

    lo = float(np.percentile(clear_vals, _STRETCH_LO_PCT))
    hi = float(np.percentile(clear_vals, _STRETCH_HI_PCT))
    span = max(1.0, hi - lo)

    def _stretch(band: Any) -> Any:
        out = (band - lo) / span * 255.0
        return np.clip(out, 0.0, 255.0).astype("uint8")

    rgb = np.stack([_stretch(red), _stretch(green), _stretch(blue)])
    for bi in range(3):
        rgb[bi][bad] = 0
    return rgb


def _fetch_truecolor_cog_bytes(
    bbox: tuple[float, float, float, float],
    datetime_range: str,
    max_cloud_cover: float,
) -> bytes:
    """Search S2, build true-color for ``bbox``, return a 3-band uint8 RGB COG.

    Raises:
        ``S2TrueColorNoImageryError``: no scene in the window (honest no-imagery).
        ``S2TrueColorUpstreamError``: search / read / write failure.
    """
    import rasterio

    # 1. Pick the least-cloudy intersecting scene in the window.
    try:
        item = _pc_stac.search_least_cloudy_item(
            collection=_COLLECTION,
            bbox=bbox,
            datetime_range=datetime_range,
            max_cloud_cover=max_cloud_cover,
            sort_by_cloud=True,
        )
    except _pc_stac.PCStacNoItemsError as exc:
        raise S2TrueColorNoImageryError(
            f"no Sentinel-2 imagery for bbox={bbox} in {datetime_range} "
            f"under {max_cloud_cover}% cloud cover: {exc}"
        ) from exc
    except _pc_stac.PCStacError as exc:
        raise S2TrueColorUpstreamError(
            f"Sentinel-2 STAC search failed: {exc}"
        ) from exc

    assets = getattr(item, "assets", {}) or {}
    needed = (_RED_BAND, _GREEN_BAND, _BLUE_BAND, _SCL_BAND)
    missing = [b for b in needed if b not in assets]
    if missing:
        raise S2TrueColorUpstreamError(
            f"Sentinel-2 item {getattr(item, 'id', '?')} missing assets "
            f"{missing} (have {sorted(assets)[:10]})"
        )

    red_href = _pc_stac.sas_sign_href(assets[_RED_BAND].href, _COLLECTION)
    grn_href = _pc_stac.sas_sign_href(assets[_GREEN_BAND].href, _COLLECTION)
    blu_href = _pc_stac.sas_sign_href(assets[_BLUE_BAND].href, _COLLECTION)
    scl_href = _pc_stac.sas_sign_href(assets[_SCL_BAND].href, _COLLECTION)

    width_px, height_px = _pc_stac.bbox_pixel_dims(bbox, _NATIVE_CELL_M)

    # 2. Read each band warped+windowed to the bbox.
    red = _read_band_window(red_href, bbox, width_px, height_px)
    green = _read_band_window(grn_href, bbox, width_px, height_px)
    blue = _read_band_window(blu_href, bbox, width_px, height_px)
    scl = _read_band_window(scl_href, bbox, width_px, height_px, nearest=True)

    # 3. Cloud-mask + percentile-stretch into a baked uint8 true-color array.
    rgb = _truecolor_from_bands(red, green, blue, scl)

    # 4. Write a 3-band uint8 RGB COG (publish_layer multiband passthrough).
    transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_s2tc_"
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
        raise S2TrueColorUpstreamError(
            f"Sentinel-2 true-color COG write failed for bbox={bbox}: {exc}"
        ) from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    logger.info(
        "fetch_sentinel2_truecolor: scene=%s cc=%.2f bbox=%s -> %d-byte "
        "3-band RGB COG (%dx%d)",
        getattr(item, "id", "?"),
        float(getattr(item, "properties", {}).get("eo:cloud_cover", -1.0)),
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
def fetch_sentinel2_truecolor(
    bbox: tuple[float, float, float, float],
    start_date: str | None = None,
    end_date: str | None = None,
    max_cloud_cover: float = _DEFAULT_MAX_CLOUD,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch a recent Sentinel-2 true-color (natural-color) RGB image for a bbox.

    **What it does:** Searches the Microsoft Planetary Computer for the
    least-cloudy Sentinel-2 L2A scene intersecting ``bbox`` inside the time
    window, reads the visible bands (B04 Red / B03 Green / B02 Blue) plus the
    SCL scene-classification band clipped to the bbox at 10 m, masks cloud /
    shadow / nodata pixels via SCL, joint-percentile-stretches the uint16
    reflectance to uint8, and returns a 3-band RGB COG that paints directly as a
    recent satellite basemap (no rescale / colormap  --  the baked natural colors
    render as-is via the multiband passthrough in ``publish_layer``).

    Sentinel-2 is GLOBAL and refreshes every ~5 days, so this is the go-to
    "what does this area look like right now from space" layer anywhere on
    Earth  --  the worldwide complement to the US-only ``fetch_naip``.

    **When to use:**
    - User wants a recent true-color / natural-color satellite image of an area
      ("show me what X looks like from space", a post-event before/after look,
      a current basemap for context).
    - Anywhere outside the US, or where NAIP is stale, or where a specific
      recent date matters (call twice with different windows to compare).

    **When NOT to use:**
    - US sub-meter aerial detail  --  use ``fetch_naip`` (~1 m vs 10 m).
    - Vegetation vigor index  --  use ``compute_ndvi`` (continuous greenness, not
      a picture).
    - Land-cover CLASSES (forest / crop / urban)  --  use ``fetch_landcover`` /
      ``extract_landcover_class``.
    - Areas with persistent cloud cover in the window (raise the window or the
      ``max_cloud_cover`` ceiling); a no-imagery result is an honest typed error.

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326.
      Required. AOI-scoped (<= 0.5 deg^2).
    - ``start_date`` / ``end_date`` (str, optional): ``"YYYY-MM-DD"`` window
      bounds. Default: a trailing ~90-day recent window ending today.
    - ``max_cloud_cover`` (float, default 30.0): only scenes below this
      ``eo:cloud_cover`` percent are considered; the least-cloudy is chosen.

    **Returns:** A ``LayerURI`` (``layer_type="raster"``, ``role="context"``  --
    it is a basemap, not the analytical primary) pointing at a 3-band RGB COG in
    the ``static-30d``/``s2_truecolor`` cache prefix.
    ``style_preset="s2_truecolor"`` (a multiband-passthrough token  --  no
    single-band rescale).

    **Data source:** Sentinel-2 Level-2A via the Microsoft Planetary Computer
    STAC (``sentinel-2-l2a`` collection; bands B04/B03/B02 + SCL).

    FR-CE-8: routed through ``read_through`` so identical ``(bbox, window,
    cloud)`` calls reuse the cached RGB COG.
    """
    _validate_bbox(bbox)
    q_bbox = _round_bbox(bbox)

    if start_date and end_date:
        dt_range = f"{start_date}/{end_date}"
    else:
        s, e = _default_window()
        dt_range = f"{s}/{e}"

    try:
        max_cc = float(max_cloud_cover)
    except (TypeError, ValueError):
        max_cc = _DEFAULT_MAX_CLOUD

    params = {
        "bbox": list(q_bbox),
        "datetime_range": dt_range,
        "max_cloud_cover": max_cc,
        "collection": _COLLECTION,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_truecolor_cog_bytes(q_bbox, dt_range, max_cc),
    )
    assert result.uri is not None, (
        "fetch_sentinel2_truecolor is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=(
            f"s2-truecolor-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name="Sentinel-2 True Color",
        layer_type="raster",
        uri=result.uri,
        # "s2_truecolor" is a multiband-passthrough token: publish_layer's
        # RGBA/multiband probe renders the 3-band COG directly, and the token
        # is NOT in the single-band registry, so no rescale/colormap is applied.
        style_preset=_STYLE_PRESET,
        role="context",
        units=None,
    )
