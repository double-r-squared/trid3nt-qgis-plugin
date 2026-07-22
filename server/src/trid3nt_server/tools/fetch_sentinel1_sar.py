"""``fetch_sentinel1_sar`` atomic tool  --  Sentinel-1 SAR backscatter (dB) COG.

Fetches a Sentinel-1 C-band Synthetic Aperture Radar (SAR) backscatter image for
a bbox + a date window via the Microsoft Planetary Computer (PC) STAC catalog and
returns a single-band float32 COG of gamma0 backscatter in DECIBELS (dB) that
paints as a grayscale radar image.

Why SAR (vs optical Sentinel-2 / Landsat)?  Radar is its own illumination source
and C-band penetrates clouds, smoke and darkness, so Sentinel-1 images the ground
in ALL WEATHER, DAY OR NIGHT  --  the moment optical sensors go blind (a hurricane
landfall, a wildfire smoke pall, a polar night) SAR keeps seeing. That makes it
THE canonical keyless source for FLOOD mapping: calm open water is specular and
reflects the radar away from the sensor, so flooded ground reads as anomalously
LOW backscatter (dark) against the rougher, brighter un-flooded land. Urban /
rough surfaces double-bounce and read BRIGHT.

Data source
===========

PC collection ``sentinel-1-rtc`` by default (Sentinel-1 Radiometric Terrain
Corrected, 10 m, gamma0): the assets are analysis-ready terrain-flattened COGs of
LINEAR-power backscatter, so no border-noise / calibration / terrain-flattening
work is needed  --  just read the power and convert to dB. ``collection`` can be
set to ``sentinel-1-grd`` (Ground Range Detected) for the un-terrain-corrected
amplitude product where RTC has no coverage.

    catalog: https://planetarycomputer.microsoft.com/api/stac/v1
    assets:  vv / vh (single-band float32 gamma0 power COGs, 10 m)
    select:  among scenes intersecting the bbox in the window, the one that best
             COVERS the bbox (coverage-aware), then the most recent.

Assets are Azure-Blob COGs behind SAS tokens; this tool signs each asset href via
the PC SAS REST endpoint (see ``_pc_stac.sas_sign_href``) and reads a
bbox-windowed, EPSG:4326-warped array through GDAL ``/vsicurl/``.

Polarization
============

``polarization`` selects ``"vv"`` (default; co-pol, the all-purpose flood /
surface-roughness channel) or ``"vh"`` (cross-pol, more sensitive to volume
scattering  --  vegetation structure). IW-mode dual-pol scenes carry both.

dB scaling
==========

RTC / GRD assets are LINEAR power (gamma0). We convert to decibels:

    backscatter_dB = 10 * log10(power)        (power > 0)

dB is the conventional SAR display unit (water ~ -20..-15 dB, bare/agriculture
~ -12..-6 dB, urban ~ 0..+5 dB). nodata / non-positive power reads back as the
COG nodata sentinel (-9999.0).

Rendering
=========

A single-band float32 dB COG. ``style_preset="sar_backscatter_db"`` is a
single-band token; until a registry rescale is pinned for it, ``publish_layer``
auto-rescales band-1 by its 2nd..98th percentile (a perceptually-uniform ramp),
which renders the dark-water / bright-urban contrast correctly. (A canonical
grayscale dB ramp  --  e.g. ``rescale=-25,5 colormap_name=gray``  --  can later be
added to ``_TITILER_STYLE_REGISTRY`` for a fixed radar look; see corpus notes.)

Honesty (data-source fallback norm): if NO Sentinel-1 scene intersects the bbox
in the window (or the requested polarization is absent, or every pixel is
nodata), a typed ``Sentinel1NoImageryError`` is raised  --  never a fabricated
layer.

FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical
``(bbox, window, polarization, collection)`` calls reuse the cached dB COG in the
``static-30d`` / ``sentinel1_sar`` cache prefix.

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

from . import register_tool
from . import _pc_stac
from .cache import read_through

__all__ = [
    "fetch_sentinel1_sar",
    "estimate_payload_mb",
    "Sentinel1SarError",
    "Sentinel1BboxError",
    "Sentinel1PolarizationError",
    "Sentinel1CollectionError",
    "Sentinel1NoImageryError",
    "Sentinel1UpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.fetch_sentinel1_sar")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class Sentinel1SarError(RuntimeError):
    """Base class for fetch_sentinel1_sar failures."""

    error_code = "SENTINEL1_SAR_ERROR"
    retryable = True


class Sentinel1BboxError(Sentinel1SarError):
    """Malformed / out-of-range / degenerate / too-large bbox."""

    error_code = "SENTINEL1_BBOX_INVALID"
    retryable = False


class Sentinel1PolarizationError(Sentinel1SarError):
    """Unknown ``polarization`` (not vv / vh)."""

    error_code = "SENTINEL1_POLARIZATION_INVALID"
    retryable = False


class Sentinel1CollectionError(Sentinel1SarError):
    """Unknown ``collection`` (not sentinel-1-rtc / sentinel-1-grd)."""

    error_code = "SENTINEL1_COLLECTION_INVALID"
    retryable = False


class Sentinel1NoImageryError(Sentinel1SarError):
    """No Sentinel-1 scene covers the bbox in the window for the polarization.

    Honest no-imagery signal (data-source fallback norm)  --  never fabricate.
    """

    error_code = "SENTINEL1_NO_IMAGERY"
    retryable = False


class Sentinel1UpstreamError(Sentinel1SarError):
    """A PC STAC search / asset read / COG write failed."""

    error_code = "SENTINEL1_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Default collection: Radiometric Terrain Corrected (analysis-ready, terrain
#: flattened gamma0). GRD is the un-corrected fallback for gaps in RTC coverage.
_RTC_COLLECTION = "sentinel-1-rtc"
_GRD_COLLECTION = "sentinel-1-grd"
_VALID_COLLECTIONS = (_RTC_COLLECTION, _GRD_COLLECTION)
_DEFAULT_COLLECTION = _RTC_COLLECTION

#: Supported polarization asset keys (PC asset names are lowercase).
_VALID_POLARIZATIONS = ("vv", "vh")
_DEFAULT_POLARIZATION = "vv"

#: Sentinel-1 RTC/GRD native grid (~10 m); used to size the bbox-windowed read.
_NATIVE_CELL_M = 10.0

#: COG nodata sentinel for the dB float band (no-coverage / non-positive power).
_NODATA = -9999.0

#: bbox area guardrail (deg^2). SAR over a huge AOI spans multiple swaths +
#: materializes an enormous grid; the atomic-tool surface is AOI-scoped. ~0.5
#: deg^2 ~ a county-ish extent (matches fetch_sentinel2_truecolor / fetch_landsat).
_MAX_BBOX_DEG2 = 0.5

#: 6-dp bbox quantization (~0.1 m) for cache-key stability.
_BBOX_DECIMALS = 6

#: Single-band style token. Until a registry rescale is pinned, publish_layer
#: auto-rescales band-1 by its 2..98 percentile (dark-water / bright-urban
#: contrast renders correctly). A fixed grayscale dB ramp can be added later.
_STYLE_PRESET = "sar_backscatter_db"


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_sentinel1_sar",
    ttl_class="static-30d",
    source_class="sentinel1_sar",
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
    """Estimate emitted single-band float32 dB COG size in MB.

    A 1-band float32 DEFLATE-COG at 10 m runs ~40 MB / sq-deg for varied
    backscatter (SAR speckle compresses poorly vs an 8-bit RGB). Scale linearly
    with bbox area, floored.
    """
    if bbox is None:
        return 3.0
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 3.0
    return max(0.25, sq_deg * 40.0)


# ---------------------------------------------------------------------------
# bbox / param helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise Sentinel1BboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise Sentinel1BboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise Sentinel1BboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise Sentinel1BboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise Sentinel1BboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    area = (max_lon - min_lon) * (max_lat - min_lat)
    if area > _MAX_BBOX_DEG2:
        raise Sentinel1BboxError(
            f"bbox area {area:.3f} deg^2 exceeds {_MAX_BBOX_DEG2} deg^2 guardrail "
            "for fetch_sentinel1_sar (SAR backscatter is AOI-scoped; narrow the "
            "bbox)."
        )


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


def _normalize_polarization(polarization: str | None) -> str:
    if polarization is None:
        return _DEFAULT_POLARIZATION
    key = str(polarization).strip().lower()
    aliases = {
        "co-pol": "vv",
        "copol": "vv",
        "co_pol": "vv",
        "cross-pol": "vh",
        "crosspol": "vh",
        "cross_pol": "vh",
    }
    key = aliases.get(key, key)
    if key not in _VALID_POLARIZATIONS:
        raise Sentinel1PolarizationError(
            f"unknown polarization {polarization!r}; expected one of "
            f"{list(_VALID_POLARIZATIONS)} (or an alias)."
        )
    return key


def _normalize_collection(collection: str | None) -> str:
    if collection is None:
        return _DEFAULT_COLLECTION
    key = str(collection).strip().lower()
    aliases = {
        "rtc": _RTC_COLLECTION,
        "sentinel1-rtc": _RTC_COLLECTION,
        "s1-rtc": _RTC_COLLECTION,
        "grd": _GRD_COLLECTION,
        "sentinel1-grd": _GRD_COLLECTION,
        "s1-grd": _GRD_COLLECTION,
    }
    key = aliases.get(key, key)
    if key not in _VALID_COLLECTIONS:
        raise Sentinel1CollectionError(
            f"unknown collection {collection!r}; expected one of "
            f"{list(_VALID_COLLECTIONS)} (or an alias)."
        )
    return key


def _default_window() -> tuple[str, str]:
    """Default datetime window: a trailing ~3-month recent window.

    Returns ``(start_iso, end_iso)`` as ``YYYY-MM-DD`` strings. Sentinel-1
    revisits ~every 6-12 days; ~90 days gives several passes so a recent scene
    is reliably found.
    """
    from datetime import datetime, timedelta, timezone

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=90)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# Scene selection: coverage-aware, then most-recent.
# ---------------------------------------------------------------------------


def _select_scene(
    bbox: tuple[float, float, float, float],
    datetime_range: str,
    collection: str,
    polarization: str,
) -> Any:
    """Return the Sentinel-1 item best covering ``bbox`` (then most recent).

    Only scenes carrying the requested ``polarization`` asset are considered.
    Raises ``Sentinel1NoImageryError`` on zero matches, ``Sentinel1UpstreamError``
    on a search failure.
    """
    try:
        from pystac_client import Client
        from shapely.geometry import box, shape
    except ImportError as exc:  # pragma: no cover  --  hard deps
        raise Sentinel1UpstreamError(
            f"pystac-client / shapely unavailable; cannot search PC STAC: {exc}"
        ) from exc

    aoi = box(*bbox)
    try:
        client = Client.open(_pc_stac.PC_STAC_ROOT)
        search = client.search(
            collections=[collection],
            bbox=list(bbox),
            datetime=datetime_range,
            limit=100,
        )
        items = list(search.items())
    except Exception as exc:  # noqa: BLE001  --  translate any pystac/http error
        raise Sentinel1UpstreamError(
            f"Sentinel-1 STAC search failed (collection={collection!r}, "
            f"bbox={bbox}, window={datetime_range}): {exc}"
        ) from exc

    # Keep only scenes that actually carry the requested polarization asset.
    items = [it for it in items if polarization in (getattr(it, "assets", {}) or {})]
    if not items:
        raise Sentinel1NoImageryError(
            f"no {collection!r} scene with {polarization.upper()} polarization "
            f"intersects bbox={bbox} within {datetime_range}."
        )

    def _coverage(item: Any) -> float:
        try:
            inter = shape(item.geometry).intersection(aoi).area
            return inter / aoi.area if aoi.area > 0 else 0.0
        except Exception:  # noqa: BLE001  --  bad geometry: treat as no coverage
            return 0.0

    # Rank: best AOI coverage first, then most recent. A swath edge can clip a
    # corner, so coverage is the primary key; recency breaks ties.
    items.sort(
        key=lambda it: (
            -_coverage(it),
            it.properties.get("datetime", ""),
        ),
        reverse=False,
    )
    # The sort above ascends on datetime within equal coverage; flip so the most
    # recent wins among equally-covering scenes.
    items.sort(key=lambda it: (-_coverage(it), _neg_dt_key(it)))
    return items[0]


def _neg_dt_key(item: Any) -> str:
    """Sort key that makes a LATER datetime sort FIRST (lexicographic invert)."""
    dt = item.properties.get("datetime", "") or ""
    # Invert each char so a later ISO timestamp sorts before an earlier one.
    return "".join(chr(0x10FFFF - ord(c)) if ord(c) < 0x10FFFF else c for c in dt)


# ---------------------------------------------------------------------------
# Core: search -> windowed read -> dB convert -> single-band COG.
# ---------------------------------------------------------------------------


def _read_band_window(
    signed_href: str,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
) -> Any:
    """Read ``signed_href`` warped to EPSG:4326 and windowed to ``bbox``.

    Returns a 2-D float32 numpy array at ``(height_px, width_px)`` of LINEAR
    gamma0 power; nodata reads back as NaN. Raises ``Sentinel1UpstreamError`` on
    any read failure.
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
    except Sentinel1SarError:
        raise
    except Exception as exc:  # noqa: BLE001  --  translate any rasterio/GDAL error
        raise Sentinel1UpstreamError(
            f"Sentinel-1 band read failed (href={signed_href[:120]!r}): {exc}"
        ) from exc


def _power_to_db(power: Any) -> Any:
    """Convert a linear gamma0 power array to dB; nodata / <=0 -> NODATA sentinel.

    Raises ``Sentinel1NoImageryError`` when no valid (positive) pixel remains
    (e.g. the scene only grazes a nodata corner of the AOI).
    """
    import numpy as np

    valid = np.isfinite(power) & (power > 0.0)
    if not bool(valid.any()):
        raise Sentinel1NoImageryError(
            "Sentinel-1 scene produced an all-nodata window over the AOI (no "
            "valid backscatter pixels to render)."
        )
    db = np.full(power.shape, _NODATA, dtype="float32")
    db[valid] = (10.0 * np.log10(power[valid])).astype("float32")
    return db


def _write_db_cog(
    db: Any,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
) -> bytes:
    """Write a single-band float32 dB COG with the NODATA sentinel."""
    import rasterio

    transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_s1sar_"
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
            dst.write(db, 1)
        with open(tmp_path, "rb") as fh:
            return fh.read()
    except Exception as exc:  # noqa: BLE001
        raise Sentinel1UpstreamError(
            f"Sentinel-1 dB COG write failed for bbox={bbox}: {exc}"
        ) from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _fetch_sar_cog_bytes(
    bbox: tuple[float, float, float, float],
    datetime_range: str,
    polarization: str,
    collection: str,
) -> bytes:
    """Search Sentinel-1, read backscatter, return a single-band float32 dB COG.

    Raises:
        ``Sentinel1NoImageryError``: no scene in the window (honest no-imagery).
        ``Sentinel1UpstreamError``: search / read / write failure.
    """
    item = _select_scene(bbox, datetime_range, collection, polarization)

    assets = getattr(item, "assets", {}) or {}
    if polarization not in assets:
        raise Sentinel1UpstreamError(
            f"Sentinel-1 item {getattr(item, 'id', '?')} missing {polarization!r} "
            f"asset (have {sorted(assets)[:12]})"
        )

    signed = _pc_stac.sas_sign_href(assets[polarization].href, collection)
    width_px, height_px = _pc_stac.bbox_pixel_dims(bbox, _NATIVE_CELL_M)

    power = _read_band_window(signed, bbox, width_px, height_px)
    db = _power_to_db(power)
    cog_bytes = _write_db_cog(db, bbox, width_px, height_px)

    logger.info(
        "fetch_sentinel1_sar: scene=%s coll=%s pol=%s orbit=%s bbox=%s -> "
        "%d-byte single-band dB COG (%dx%d)",
        getattr(item, "id", "?"),
        collection,
        polarization,
        getattr(item, "properties", {}).get("sat:orbit_state", "?"),
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
def fetch_sentinel1_sar(
    bbox: tuple[float, float, float, float],
    start_date: str | None = None,
    end_date: str | None = None,
    polarization: str = _DEFAULT_POLARIZATION,
    collection: str = _DEFAULT_COLLECTION,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch a Sentinel-1 C-band SAR backscatter (dB) image for a bbox.

    **What it does:** Searches the Microsoft Planetary Computer for the
    Sentinel-1 scene that best COVERS ``bbox`` inside the time window (then the
    most recent), reads the requested polarization band (``vv`` or ``vh``)
    clipped to the bbox at ~10 m, converts the linear gamma0 power to decibels
    (``10*log10(power)``), and returns a single-band float32 dB COG that paints
    as a grayscale radar image.

    Sentinel-1 SAR is its own illumination source and C-band sees THROUGH
    clouds, smoke and darkness, so it images the ground in ALL WEATHER, DAY OR
    NIGHT  --  exactly when optical Sentinel-2 / Landsat go blind (hurricane
    landfall, wildfire smoke, polar night). It is the canonical keyless FLOOD
    layer: calm open water is specular and reflects radar away from the sensor,
    so flooded ground reads as anomalously LOW backscatter (dark) against the
    rougher, brighter dry land; urban / rough surfaces read BRIGHT.

    **When to use:**
    - Flood-extent mapping when the sky is clouded (the SAR flood-water signature
      is low/dark backscatter); compare a pre-event and an event-window scene.
    - Any "see the ground despite cloud / smoke / night" need (storm, fire).
    - Surface-roughness / structure context (urban vs water vs bare; ``vh`` adds
      vegetation-volume sensitivity).

    **When NOT to use:**
    - A natural-color "what does it look like" picture  --  use
      ``fetch_sentinel2_truecolor`` (10 m optical) or ``fetch_landsat_imagery``.
    - A vegetation index  --  use ``compute_ndvi``.
    - Areas / windows with no Sentinel-1 pass (widen the window); a no-imagery
      result is an honest typed error, never a fabricated layer.

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326.
      Required. AOI-scoped (<= 0.5 deg^2).
    - ``start_date`` / ``end_date`` (str, optional): ``"YYYY-MM-DD"`` window
      bounds. Default: a trailing ~90-day recent window ending today. For a
      flood pair, call once with the pre-event window and once with the event
      window.
    - ``polarization`` (str, default ``"vv"``): ``"vv"`` (co-pol; the
      all-purpose flood / roughness channel) or ``"vh"`` (cross-pol; vegetation
      structure).
    - ``collection`` (str, default ``"sentinel-1-rtc"``): ``"sentinel-1-rtc"``
      (Radiometric Terrain Corrected, analysis-ready, terrain-flattened) or
      ``"sentinel-1-grd"`` (Ground Range Detected; un-terrain-corrected fallback
      where RTC has no coverage).

    **Returns:** A ``LayerURI`` (``layer_type="raster"``, ``role="primary"``  --
    the backscatter IS the analytical product) pointing at a single-band float32
    dB COG in the ``static-30d``/``sentinel1_sar`` cache prefix.
    ``style_preset="sar_backscatter_db"`` (single-band; auto-rescaled by band
    percentile so the dark-water / bright-urban contrast renders). ``units`` is
    ``"VV/VH gamma0 backscatter (dB)"``.

    **Data source:** Sentinel-1 RTC / GRD via the Microsoft Planetary Computer
    STAC (``sentinel-1-rtc`` / ``sentinel-1-grd``; vv / vh gamma0 assets).

    FR-CE-8: routed through ``read_through`` so identical ``(bbox, window,
    polarization, collection)`` calls reuse the cached dB COG.
    """
    _validate_bbox(bbox)
    pol = _normalize_polarization(polarization)
    coll = _normalize_collection(collection)
    q_bbox = _round_bbox(bbox)

    if start_date and end_date:
        dt_range = f"{start_date}/{end_date}"
    else:
        s, e = _default_window()
        dt_range = f"{s}/{e}"

    params = {
        "bbox": list(q_bbox),
        "datetime_range": dt_range,
        "polarization": pol,
        "collection": coll,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_sar_cog_bytes(q_bbox, dt_range, pol, coll),
    )
    assert result.uri is not None, (
        "fetch_sentinel1_sar is cacheable; uri must be set by read_through"
    )

    return LayerURI(
        layer_id=(
            f"s1-sar-{pol}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=f"Sentinel-1 SAR Backscatter ({pol.upper()}, dB)",
        layer_type="raster",
        uri=result.uri,
        style_preset=_STYLE_PRESET,
        role="primary",
        units=f"{pol.upper()} gamma0 backscatter (dB)",
    )
