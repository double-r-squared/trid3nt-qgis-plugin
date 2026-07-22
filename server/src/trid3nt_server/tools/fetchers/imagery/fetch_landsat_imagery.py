"""``fetch_landsat_imagery`` atomic tool  --  Landsat Collection-2 Level-2 imagery.

Fetches a Landsat Collection-2 Level-2 (surface-reflectance + surface-temperature)
scene for a bbox + a date window via the Microsoft Planetary Computer (PC) STAC
catalog and returns a 3-band RGB COG that paints directly as a satellite image.

Three band combos are supported (``band_combo``):

    true_color       Red / Green / Blue surface reflectance (natural color)
    false_color_nir  NIR / Red / Green  (vegetation = bright red; CIR composite)
    thermal          land-surface temperature from the thermal band (lwir11),
                     converted to deg C and baked through an inferno heat ramp.

Why Landsat (vs Sentinel-2)?  Landsat's continuous archive reaches back to the
1980s (and 1972 for the MSS era), so it is the go-to source for LONG before/after
change detection -- decadal urban growth, reservoir drawdown, deforestation,
post-fire recovery -- and it carries a calibrated THERMAL band, so it is the
keyless path to land-surface temperature / urban-heat-island maps. Sentinel-2 is
finer (10 m) and more frequent (~5 d) but only reaches back to ~2015 and has no
thermal band; ``fetch_sentinel2_truecolor`` / ``compute_ndvi`` cover that lane.

Data source
===========

PC collection ``landsat-c2-l2`` (Landsat Collection-2 Level-2, 30 m; Landsat
4/5/7/8/9). This tool defaults to the Landsat 8/9 (OLI/TIRS) platforms so the
asset naming is stable (``lwir11`` thermal) and there are no Landsat-7 SLC-off
scan gaps; ``include_legacy_landsat=True`` widens to 4/5/7 for deep-history
windows.

    catalog: https://planetarycomputer.microsoft.com/api/stac/v1
    assets:  red / green / blue / nir08 (SR, 30 m), lwir11 (thermal, 30 m),
             qa_pixel (CFMask QA bitmask, 30 m)
    select:  among scenes intersecting the bbox in the window under the cloud
             cap, the one that best COVERS the bbox (coverage-aware), then the
             least-cloudy.

Assets are Azure-Blob COGs behind SAS tokens; this tool signs each asset href
via the PC SAS REST endpoint (see ``_pc_stac.sas_sign_href``) and reads a
bbox-windowed, EPSG:4326-warped array per band through GDAL ``/vsicurl/``.

Scaling (Landsat C2 L2 DN -> physical)
======================================

    surface reflectance:  ref  = DN * 2.75e-05 - 0.2          (bands red/grn/blu/nir)
    surface temperature:  T(K) = DN * 0.00341802 + 149.0      (band lwir11)

Cloud masking
=============

The ``qa_pixel`` band is the CFMask QA bitmask. We mask fill (bit 0), dilated
cloud (bit 1), cirrus (bit 2), cloud (bit 3) and cloud shadow (bit 4) so clouds
do not pollute the contrast stretch and are emitted as black no-data.

Rendering
=========

Reflectance bands are jointly percentile-stretched (2nd..98th over the CLEAR
pixels) to uint8; the thermal combo percentile-stretches the deg-C surface and
bakes an inferno colormap. All three combos emit a 3-band uint8 RGB COG, so
``publish_layer`` renders them via the RGBA/multiband passthrough (band count >=
3 -> TiTiler renders the baked colors directly, no rescale/colormap). The
``style_preset`` token ``"landsat_rgb"`` is intentionally NOT in the single-band
TiTiler style registry.

Honesty (data-source fallback norm): if NO Landsat scene intersects the bbox in
the window (or none under the cloud cap, or every pixel is clouded/nodata), a
typed ``LandsatNoImageryError`` is raised  --  never a fabricated layer.

FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical
``(bbox, window, band_combo, cloud, legacy)`` calls reuse the cached RGB COG in
the ``static-30d`` / ``landsat_imagery`` cache prefix.

Tier-1 free (no API key). Heavy emit-free sync raster work  --  intended for the
``_ALWAYS_OFFLOAD_SYNC_TOOLS`` set so it runs via ``asyncio.to_thread`` and never
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
    "fetch_landsat_imagery",
    "estimate_payload_mb",
    "LandsatImageryError",
    "LandsatBboxError",
    "LandsatBandComboError",
    "LandsatNoImageryError",
    "LandsatUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.imagery.fetch_landsat_imagery")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class LandsatImageryError(RuntimeError):
    """Base class for fetch_landsat_imagery failures."""

    error_code = "LANDSAT_IMAGERY_ERROR"
    retryable = True


class LandsatBboxError(LandsatImageryError):
    """Malformed / out-of-range / degenerate / too-large bbox."""

    error_code = "LANDSAT_BBOX_INVALID"
    retryable = False


class LandsatBandComboError(LandsatImageryError):
    """Unknown ``band_combo`` (not true_color / false_color_nir / thermal)."""

    error_code = "LANDSAT_BAND_COMBO_INVALID"
    retryable = False


class LandsatNoImageryError(LandsatImageryError):
    """No Landsat scene covers the bbox in the window under the cloud cap.

    Honest no-imagery signal (data-source fallback norm)  --  never fabricate.
    """

    error_code = "LANDSAT_NO_IMAGERY"
    retryable = False


class LandsatUpstreamError(LandsatImageryError):
    """A PC STAC search / asset read / COG write failed."""

    error_code = "LANDSAT_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

_COLLECTION = "landsat-c2-l2"

#: Surface-reflectance band assets (Landsat C2 L2 PC asset keys).
_RED_BAND = "red"
_GREEN_BAND = "green"
_BLUE_BAND = "blue"
_NIR_BAND = "nir08"
#: Thermal (land-surface-temperature) band -- OLI/TIRS (Landsat 8/9) naming.
_THERMAL_BAND = "lwir11"
#: CFMask QA bitmask band.
_QA_BAND = "qa_pixel"

#: Landsat C2 L2 native grid (30 m); used to size the bbox-windowed read.
_NATIVE_CELL_M = 30.0

#: Surface-reflectance DN -> reflectance (Collection-2 Level-2 scaling).
_SR_SCALE = 2.75e-05
_SR_OFFSET = -0.2

#: Surface-temperature DN -> Kelvin (Collection-2 Level-2 ST scaling, lwir11).
_ST_SCALE = 0.00341802
_ST_OFFSET = 149.0
_KELVIN_C = 273.15

#: ``qa_pixel`` CFMask bit indices to MASK as fill / cloud / shadow:
#:   bit 0 = fill (nodata)
#:   bit 1 = dilated cloud
#:   bit 2 = cirrus
#:   bit 3 = cloud
#:   bit 4 = cloud shadow
#: (bit 5 snow, bit 6 clear, bit 7 water are KEPT.)
_QA_FILL_BIT = 1 << 0
_QA_BAD_BITS = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4)

#: Default cloud-cover ceiling (percent) for scene selection. Generous so a
#: typical AOI finds a usable scene; the best-coverage / least-cloudy match is
#: then chosen.
_DEFAULT_MAX_CLOUD = 30.0

#: A scene must cover at least this fraction of the AOI to be preferred. Landsat
#: WRS-2 scenes are ~185 km wide; an AOI can clip a scene's nodata corner, so we
#: rank full-coverage scenes ahead of partial ones BEFORE cloud cover (a 0%-cloud
#: scene that only grazes a corner is useless).
_MIN_COVERAGE_FRAC = 0.99

#: Joint percentile stretch bounds (over clear pixels) for the float -> uint8
#: conversion (reflectance and thermal alike). 2nd/98th keeps deep shadows +
#: bright surfaces without blowing out.
_STRETCH_LO_PCT = 2.0
_STRETCH_HI_PCT = 98.0

#: bbox area guardrail (deg^2). Imagery over a huge AOI spans many WRS scenes +
#: materializes an enormous grid; the atomic-tool surface is AOI-scoped. ~0.5
#: deg^2 ~ a county-ish extent (matches fetch_sentinel2_truecolor / compute_ndvi).
_MAX_BBOX_DEG2 = 0.5

#: 6-dp bbox quantization (~0.1 m) for cache-key stability.
_BBOX_DECIMALS = 6

#: Multiband-passthrough style token. publish_layer's RGBA/multiband probe
#: (>=3 bands) renders the COG directly; this token is intentionally NOT in the
#: single-band TiTiler style registry, so no rescale / colormap is applied.
_STYLE_PRESET = "landsat_rgb"

#: Supported band combos -> (display name, role).
_BAND_COMBOS = {
    "true_color": ("Landsat True Color", "context"),
    "false_color_nir": ("Landsat False Color (NIR)", "context"),
    "thermal": ("Landsat Land-Surface Temperature", "primary"),
}
_DEFAULT_BAND_COMBO = "true_color"

#: Default platform set: OLI/TIRS (stable lwir11 asset, no SLC-off gaps).
_MODERN_PLATFORMS = ["landsat-8", "landsat-9"]
#: Legacy platforms (opt-in for deep-history windows).
_LEGACY_PLATFORMS = ["landsat-4", "landsat-5", "landsat-7"]


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_landsat_imagery",
    ttl_class="static-30d",
    source_class="landsat_imagery",
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

    A 3-band uint8 DEFLATE-COG at 30 m runs ~14 MB / sq-deg for varied surface
    (Landsat is 30 m, ~9x coarser than Sentinel-2's 10 m, so ~1/9 the pixels).
    Scale linearly with bbox area, floored.
    """
    if bbox is None:
        return 2.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 2.0
    return max(0.25, sq_deg * 14.0)


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise LandsatBboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise LandsatBboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise LandsatBboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise LandsatBboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise LandsatBboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    area = (max_lon - min_lon) * (max_lat - min_lat)
    if area > _MAX_BBOX_DEG2:
        raise LandsatBboxError(
            f"bbox area {area:.3f} deg^2 exceeds {_MAX_BBOX_DEG2} deg^2 guardrail "
            "for fetch_landsat_imagery (Landsat imagery is AOI-scoped; narrow the "
            "bbox)."
        )


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


def _normalize_band_combo(band_combo: str | None) -> str:
    if band_combo is None:
        return _DEFAULT_BAND_COMBO
    key = str(band_combo).strip().lower()
    # Friendly aliases the LLM may invent.
    aliases = {
        "rgb": "true_color",
        "natural": "true_color",
        "natural_color": "true_color",
        "truecolor": "true_color",
        "color_infrared": "false_color_nir",
        "cir": "false_color_nir",
        "false_color": "false_color_nir",
        "nir": "false_color_nir",
        "lst": "thermal",
        "temperature": "thermal",
        "thermal_lst": "thermal",
        "thermal-lst": "thermal",
        "surface_temperature": "thermal",
    }
    key = aliases.get(key, key)
    if key not in _BAND_COMBOS:
        raise LandsatBandComboError(
            f"unknown band_combo {band_combo!r}; expected one of "
            f"{sorted(_BAND_COMBOS)} (or an alias)."
        )
    return key


def _default_window() -> tuple[str, str]:
    """Default datetime window: a trailing ~14-month window.

    Returns ``(start_iso, end_iso)`` as ``YYYY-MM-DD`` strings. Landsat 8+9
    together revisit ~every 8 days; ~14 months gives many passes so a recent
    low-cloud, full-coverage scene is reliably found.
    """
    from datetime import datetime, timedelta, timezone

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=425)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# Scene selection: coverage-aware, then least-cloudy.
# ---------------------------------------------------------------------------


def _select_scene(
    bbox: tuple[float, float, float, float],
    datetime_range: str,
    max_cloud_cover: float,
    platforms: list[str],
) -> Any:
    """Return the Landsat item best covering ``bbox`` (then least-cloudy).

    Raises ``LandsatNoImageryError`` on zero matches, ``LandsatUpstreamError``
    on a search failure.
    """
    try:
        from pystac_client import Client
        from shapely.geometry import box, shape
    except ImportError as exc:  # pragma: no cover  --  hard deps
        raise LandsatUpstreamError(
            f"pystac-client / shapely unavailable; cannot search PC STAC: {exc}"
        ) from exc

    aoi = box(*bbox)
    query: dict[str, Any] = {
        "eo:cloud_cover": {"lt": float(max_cloud_cover)},
        "platform": {"in": platforms},
    }
    try:
        client = Client.open(_pc_stac.PC_STAC_ROOT)
        search = client.search(
            collections=[_COLLECTION],
            bbox=list(bbox),
            datetime=datetime_range,
            query=query,
            limit=100,
        )
        items = list(search.items())
    except Exception as exc:  # noqa: BLE001  --  translate any pystac/http error
        raise LandsatUpstreamError(
            f"Landsat STAC search failed (bbox={bbox}, window={datetime_range}): "
            f"{exc}"
        ) from exc

    if not items:
        raise LandsatNoImageryError(
            f"no Landsat ({'/'.join(platforms)}) imagery intersects bbox={bbox} "
            f"within {datetime_range} under {max_cloud_cover}% cloud cover."
        )

    def _coverage(item: Any) -> float:
        try:
            inter = shape(item.geometry).intersection(aoi).area
            return inter / aoi.area if aoi.area > 0 else 0.0
        except Exception:  # noqa: BLE001  --  bad geometry: treat as no coverage
            return 0.0

    # Rank: full-coverage scenes first, then least cloudy. A 0%-cloud scene that
    # only grazes a corner is useless, so coverage is the primary key.
    items.sort(
        key=lambda it: (
            -(1 if _coverage(it) >= _MIN_COVERAGE_FRAC else 0),
            it.properties.get("eo:cloud_cover", 100.0),
            -_coverage(it),
        )
    )
    return items[0]


# ---------------------------------------------------------------------------
# Core: search -> per-band windowed read -> mask + stretch -> RGB COG.
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
    back as 0.0. Use ``nearest=True`` for the categorical QA band.
    Raises ``LandsatUpstreamError`` on any read failure.
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
    except LandsatImageryError:
        raise
    except Exception as exc:  # noqa: BLE001  --  translate any rasterio/GDAL error
        raise LandsatUpstreamError(
            f"Landsat band read failed (href={signed_href[:120]!r}): {exc}"
        ) from exc


def _bad_mask_from_qa(qa: Any) -> Any:
    """Boolean ``bad`` mask (fill | cloud | shadow) from the qa_pixel band."""
    import numpy as np

    qa_i = qa.astype("uint16")
    fill = (qa_i & _QA_FILL_BIT) != 0
    cloudy = (qa_i & _QA_BAD_BITS) != 0
    return np.asarray(fill | cloudy)


def _stretch_rgb(bands: list[Any], bad: Any) -> Any:
    """Joint 2..98 percentile-stretch a list of 3 float bands to a uint8 RGB.

    Pixels that are nodata (NaN) in any band OR flagged ``bad`` are zeroed
    (black no-data). Raises ``LandsatNoImageryError`` when no clear pixel
    remains. Returns ``(3, H, W)`` uint8.
    """
    import numpy as np

    stack = np.stack(bands)  # (3, H, W) float32, NaN = nodata
    finite = np.all(np.isfinite(stack), axis=0)
    clear = finite & (~bad)
    clear_vals = stack[:, clear]
    if clear_vals.size == 0:
        raise LandsatNoImageryError(
            "Landsat scene produced an all-cloud / all-nodata window over the "
            "AOI (no clear pixels to render)."
        )
    lo = float(np.nanpercentile(clear_vals, _STRETCH_LO_PCT))
    hi = float(np.nanpercentile(clear_vals, _STRETCH_HI_PCT))
    span = max(1e-6, hi - lo)

    scaled = np.clip((stack - lo) / span * 255.0, 0.0, 255.0)
    rgb = np.nan_to_num(scaled, nan=0.0).astype("uint8")
    nod = (~finite) | bad
    for bi in range(3):
        rgb[bi][nod] = 0
    return rgb


def _thermal_rgb(lst_c: Any, bad: Any) -> Any:
    """Percentile-stretch a deg-C land-surface-temperature band into a baked RGB.

    Applies an inferno heat ramp (cool = dark purple, hot = bright yellow) and
    zeroes nodata / clouded pixels. Raises ``LandsatNoImageryError`` when no
    valid pixel remains. Returns ``(3, H, W)`` uint8.
    """
    import numpy as np

    valid = np.isfinite(lst_c) & (~bad)
    vals = lst_c[valid]
    if vals.size == 0:
        raise LandsatNoImageryError(
            "Landsat thermal band produced an all-cloud / all-nodata window over "
            "the AOI (no valid surface-temperature pixels to render)."
        )
    lo = float(np.nanpercentile(vals, _STRETCH_LO_PCT))
    hi = float(np.nanpercentile(vals, _STRETCH_HI_PCT))
    span = max(1e-6, hi - lo)
    norm = np.clip((lst_c - lo) / span, 0.0, 1.0)
    norm = np.nan_to_num(norm, nan=0.0)

    try:
        import matplotlib

        cmap = matplotlib.colormaps["inferno"]
        rgba = (cmap(norm) * 255.0).astype("uint8")  # (H, W, 4)
        rgb = np.transpose(rgba[..., :3], (2, 0, 1))  # (3, H, W)
    except Exception:  # noqa: BLE001  --  no matplotlib: fall back to a manual ramp
        # Simple black->red->yellow ramp without matplotlib.
        r = np.clip(norm * 2.0, 0.0, 1.0)
        g = np.clip(norm * 2.0 - 1.0, 0.0, 1.0)
        b = np.zeros_like(norm)
        rgb = (np.stack([r, g, b]) * 255.0).astype("uint8")

    nod = (~valid)
    for bi in range(3):
        rgb[bi][nod] = 0
    return rgb


def _scaled_reflectance(dn: Any) -> Any:
    """DN -> surface reflectance; nodata (DN==0) -> NaN."""
    import numpy as np

    ref = dn * _SR_SCALE + _SR_OFFSET
    ref = np.where(dn == 0, np.nan, ref)
    return ref.astype("float32")


def _scaled_lst_celsius(dn: Any) -> Any:
    """DN -> land-surface temperature in deg C; nodata (DN==0) -> NaN."""
    import numpy as np

    lst_k = dn * _ST_SCALE + _ST_OFFSET
    lst_c = lst_k - _KELVIN_C
    lst_c = np.where(dn == 0, np.nan, lst_c)
    return lst_c.astype("float32")


def _write_rgb_cog(
    rgb: Any,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
) -> bytes:
    """Write a 3-band uint8 RGB COG (publish_layer multiband passthrough)."""
    import rasterio

    transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_landsat_"
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
            return fh.read()
    except Exception as exc:  # noqa: BLE001
        raise LandsatUpstreamError(
            f"Landsat RGB COG write failed for bbox={bbox}: {exc}"
        ) from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _fetch_landsat_cog_bytes(
    bbox: tuple[float, float, float, float],
    datetime_range: str,
    band_combo: str,
    max_cloud_cover: float,
    platforms: list[str],
) -> bytes:
    """Search Landsat, build the requested combo, return a 3-band uint8 RGB COG.

    Raises:
        ``LandsatNoImageryError``: no scene in the window (honest no-imagery).
        ``LandsatUpstreamError``: search / read / write failure.
    """
    item = _select_scene(bbox, datetime_range, max_cloud_cover, platforms)

    assets = getattr(item, "assets", {}) or {}
    if band_combo == "thermal":
        needed = (_THERMAL_BAND, _QA_BAND)
    elif band_combo == "false_color_nir":
        needed = (_NIR_BAND, _RED_BAND, _GREEN_BAND, _QA_BAND)
    else:  # true_color
        needed = (_RED_BAND, _GREEN_BAND, _BLUE_BAND, _QA_BAND)
    missing = [b for b in needed if b not in assets]
    if missing:
        raise LandsatUpstreamError(
            f"Landsat item {getattr(item, 'id', '?')} missing assets {missing} "
            f"for band_combo={band_combo!r} (have {sorted(assets)[:12]})"
        )

    width_px, height_px = _pc_stac.bbox_pixel_dims(bbox, _NATIVE_CELL_M)

    def _read(asset_key: str, *, nearest: bool = False) -> Any:
        href = _pc_stac.sas_sign_href(assets[asset_key].href, _COLLECTION)
        return _read_band_window(
            href, bbox, width_px, height_px, nearest=nearest
        )

    qa = _read(_QA_BAND, nearest=True)
    bad = _bad_mask_from_qa(qa)

    if band_combo == "thermal":
        lst_c = _scaled_lst_celsius(_read(_THERMAL_BAND))
        rgb = _thermal_rgb(lst_c, bad)
    elif band_combo == "false_color_nir":
        nir = _scaled_reflectance(_read(_NIR_BAND))
        red = _scaled_reflectance(_read(_RED_BAND))
        green = _scaled_reflectance(_read(_GREEN_BAND))
        rgb = _stretch_rgb([nir, red, green], bad)
    else:  # true_color
        red = _scaled_reflectance(_read(_RED_BAND))
        green = _scaled_reflectance(_read(_GREEN_BAND))
        blue = _scaled_reflectance(_read(_BLUE_BAND))
        rgb = _stretch_rgb([red, green, blue], bad)

    cog_bytes = _write_rgb_cog(rgb, bbox, width_px, height_px)

    logger.info(
        "fetch_landsat_imagery: scene=%s platform=%s cc=%.2f combo=%s bbox=%s -> "
        "%d-byte 3-band RGB COG (%dx%d)",
        getattr(item, "id", "?"),
        getattr(item, "properties", {}).get("platform", "?"),
        float(getattr(item, "properties", {}).get("eo:cloud_cover", -1.0)),
        band_combo,
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
def fetch_landsat_imagery(
    bbox: tuple[float, float, float, float],
    start_date: str | None = None,
    end_date: str | None = None,
    band_combo: str = _DEFAULT_BAND_COMBO,
    max_cloud_cover: float = _DEFAULT_MAX_CLOUD,
    include_legacy_landsat: bool = False,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch a Landsat Collection-2 Level-2 image (true / false-color / thermal).

    **What it does:** Searches the Microsoft Planetary Computer for the Landsat
    C2 L2 scene that best COVERS ``bbox`` inside the time window (then the
    least-cloudy), reads the requested 30 m bands clipped to the bbox, masks
    cloud / shadow / fill via the ``qa_pixel`` CFMask bitmask, and returns a
    3-band RGB COG that paints directly as a satellite image (multiband
    passthrough in ``publish_layer``  --  no rescale / colormap; the baked colors
    render as-is).

    Three ``band_combo`` outputs:

    - ``true_color`` (default): Red/Green/Blue surface reflectance  --  natural
      color, "what does this area look like from space".
    - ``false_color_nir``: NIR/Red/Green  --  the color-infrared composite;
      healthy vegetation glows bright red, water is near-black. Good for
      vegetation extent / burn scars / water bodies.
    - ``thermal``: land-surface temperature from the thermal band (``lwir11``),
      converted to deg C and baked through an inferno heat ramp  --  the keyless
      path to urban-heat-island / surface-temperature maps.

    Landsat's archive reaches back to the 1980s, so this is the source for LONG
    before/after change detection (decadal urban growth, reservoir drawdown,
    deforestation, post-fire recovery) and the only keyless THERMAL imagery
    here. For finer / more-frequent recent natural color use
    ``fetch_sentinel2_truecolor`` (10 m, ~5 d, but no thermal and only ~2015+).

    **When to use:**
    - A historical "what did this area look like in year YYYY" picture (set the
      window to that year), or a decadal before/after pair (two calls).
    - Land-surface temperature / urban-heat-island maps (``band_combo="thermal"``).
    - A vegetation / water / burn-scar composite (``band_combo="false_color_nir"``).

    **When NOT to use:**
    - US sub-meter aerial detail  --  use ``fetch_naip`` (~1 m vs 30 m).
    - Recent fine-grained natural color  --  ``fetch_sentinel2_truecolor`` (10 m).
    - A continuous vegetation INDEX  --  ``compute_ndvi`` (Sentinel-2 NDVI).
    - Areas fully clouded in the window (raise the window / ``max_cloud_cover``);
      a no-imagery result is an honest typed error, never a fabricated layer.

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326.
      Required. AOI-scoped (<= 0.5 deg^2).
    - ``start_date`` / ``end_date`` (str, optional): ``"YYYY-MM-DD"`` window
      bounds. Default: a trailing ~14-month window ending today. For a historical
      look, set both to the target year (e.g. ``"1995-06-01"`` / ``"1995-09-30"``
      with ``include_legacy_landsat=True``).
    - ``band_combo`` (str, default ``"true_color"``): ``true_color`` /
      ``false_color_nir`` / ``thermal``.
    - ``max_cloud_cover`` (float, default 30.0): only scenes below this
      ``eo:cloud_cover`` percent are considered.
    - ``include_legacy_landsat`` (bool, default False): when True, widen the
      platform set to Landsat 4/5/7 for deep-history windows (note: Landsat 7
      post-2003 has SLC-off scan-line gaps, and Landsat 4/5/7 thermal uses a
      different band  --  thermal is most reliable on the default 8/9).

    **Returns:** A ``LayerURI`` (``layer_type="raster"``) pointing at a 3-band
    RGB COG in the ``static-30d``/``landsat_imagery`` cache prefix.
    ``role="primary"`` for thermal LST (the analytical product) else ``"context"``
    (a basemap). ``style_preset="landsat_rgb"`` (a multiband-passthrough token  --
    no single-band rescale).

    **Data source:** Landsat Collection-2 Level-2 via the Microsoft Planetary
    Computer STAC (``landsat-c2-l2``; assets red/green/blue/nir08/lwir11 +
    qa_pixel).

    FR-CE-8: routed through ``read_through`` so identical ``(bbox, window,
    band_combo, cloud, legacy)`` calls reuse the cached RGB COG.
    """
    _validate_bbox(bbox)
    combo = _normalize_band_combo(band_combo)
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

    platforms = (
        _MODERN_PLATFORMS + _LEGACY_PLATFORMS
        if include_legacy_landsat
        else list(_MODERN_PLATFORMS)
    )

    params = {
        "bbox": list(q_bbox),
        "datetime_range": dt_range,
        "band_combo": combo,
        "max_cloud_cover": max_cc,
        "platforms": platforms,
        "collection": _COLLECTION,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_landsat_cog_bytes(
            q_bbox, dt_range, combo, max_cc, platforms
        ),
    )
    assert result.uri is not None, (
        "fetch_landsat_imagery is cacheable; uri must be set by read_through"
    )

    name, role = _BAND_COMBOS[combo]
    units = "Land-surface temperature (deg C)" if combo == "thermal" else None

    return LayerURI(
        layer_id=(
            f"landsat-{combo}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=name,
        layer_type="raster",
        uri=result.uri,
        # "landsat_rgb" is a multiband-passthrough token: publish_layer's
        # RGBA/multiband probe renders the 3-band COG directly, and the token is
        # NOT in the single-band registry, so no rescale/colormap is applied.
        style_preset=_STYLE_PRESET,
        role=role,
        units=units,
    )
