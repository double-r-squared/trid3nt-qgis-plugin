"""``fetch_modis_lst`` atomic tool  --  MODIS land-surface temperature (deg C).

Fetches a MODIS 8-day composite land-surface-temperature (LST) grid for a bbox +
a date window via the Microsoft Planetary Computer (PC) STAC catalog and returns
a SINGLE-BAND float32 COG in degrees Celsius that paints as a thermal heat map
(``style_preset="land_surface_temp_c"``).

Why MODIS LST (vs Landsat thermal)?  MODIS gives an 8-day clear-sky COMPOSITE at
1 km that is GLOBAL and refreshed every 8 days back to 2000, so it is the keyless
path to a clean urban-heat / drought / thermal surface over a city-to-region AOI
without hunting for a single cloud-free Landsat overpass. Landsat thermal
(``fetch_landsat_imagery band_combo="thermal"``) is far finer (30 m, baked RGB)
but is a single-scene snapshot subject to clouds; MODIS LST is the smooth
composite scalar. This tool emits the PHYSICAL deg-C scalar (not a baked RGB) so
it can be zonal-summarized, differenced (day vs night, year vs year), or
thresholded for a heat-risk mask.

Data source
===========

PC collections (``product``):

    modis-11A2-061   MOD/MYD 11A2 v6.1  -- 8-day 1 km LST, asset ``LST_Day_1km``
                     (also ``LST_Night_1km``). Default.
    modis-21A2-061   MOD/MYD 21A2 v6.1  -- 8-day 1 km LST (day/night algorithm),
                     asset ``LST_Day_1KM`` (uppercase KM).

    catalog: https://planetarycomputer.microsoft.com/api/stac/v1
    select:  the MOST-RECENT MODIS tile that COVERS the bbox in the window
             (8-day composites are already clear-sky; there is no cloud field).

Scaling (MODIS LST DN -> physical)
==================================

    T(K)   = DN * 0.02            (raster:bands scale; uint16, fill DN == 0)
    T(degC) = T(K) - 273.15

Fill (DN == 0) -> NaN nodata. The emitted COG is single-band float32 deg C.

SAS signing
===========

MODIS COGs live in the Azure ``modiseuwest`` storage account. The per-collection
SAS token endpoint that ``_pc_stac.sas_sign_href`` uses returns a token that does
NOT authorize that account (verified 403 live), so this tool signs each href via
the per-HREF PC sign endpoint ``GET /api/sas/v1/sign?href=<blob-href>`` (which is
storage-account aware and returns a ready signed URL), falling back to the
per-collection token path only if the sign endpoint is unavailable.

Rendering
=========

The COG is single-band float32 deg C; ``publish_layer`` styles it through the
TiTiler style registry. ``style_preset="land_surface_temp_c"`` is intentionally
NOT the Kelvin ``*temperature*`` family (which rescales 250..320 K) -- LST here is
deg C, so the preset resolves to a deg-C rescale / red-blue ramp (registry entry)
or, until that lands centrally, a safe band-stats percentile auto-rescale.

Honesty (data-source fallback norm): if NO MODIS tile covers the bbox in the
window (or the window slices an all-fill region), a typed ``ModisLstNoDataError``
is raised  --  never a fabricated layer.

FR-CE-8 / FR-DC-3/4: routed through ``read_through`` so identical
``(bbox, window, product, daynight)`` calls reuse the cached COG in the
``static-30d`` / ``modis_lst`` cache prefix.

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

import requests

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from . import _pc_stac
from .cache import read_through

__all__ = [
    "fetch_modis_lst",
    "estimate_payload_mb",
    "ModisLstError",
    "ModisLstBboxError",
    "ModisLstParamError",
    "ModisLstNoDataError",
    "ModisLstUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.fetch_modis_lst")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class ModisLstError(RuntimeError):
    """Base class for fetch_modis_lst failures."""

    error_code = "MODIS_LST_ERROR"
    retryable = True


class ModisLstBboxError(ModisLstError):
    """Malformed / out-of-range / degenerate / too-large bbox."""

    error_code = "MODIS_LST_BBOX_INVALID"
    retryable = False


class ModisLstParamError(ModisLstError):
    """Unknown ``product`` or ``daynight`` value."""

    error_code = "MODIS_LST_PARAM_INVALID"
    retryable = False


class ModisLstNoDataError(ModisLstError):
    """No MODIS LST tile covers the bbox in the window (or all-fill window).

    Honest no-data signal (data-source fallback norm)  --  never fabricate.
    """

    error_code = "MODIS_LST_NO_DATA"
    retryable = False


class ModisLstUpstreamError(ModisLstError):
    """A PC STAC search / SAS-sign / asset read / COG write failed."""

    error_code = "MODIS_LST_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Per-href PC SAS sign endpoint (storage-account aware; the per-collection
#: /token endpoint does NOT authorize the MODIS ``modiseuwest`` account).
_PC_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"
_SIGN_TIMEOUT_S = 30.0

#: Supported MODIS LST products -> (collection id, {daynight -> asset key}).
#: 11A2 uses ``..._1km`` (lowercase), 21A2 uses ``..._1KM`` (uppercase KM).
_PRODUCTS: dict[str, tuple[str, dict[str, str]]] = {
    "11A2": (
        "modis-11A2-061",
        {"day": "LST_Day_1km", "night": "LST_Night_1km"},
    ),
    "21A2": (
        "modis-21A2-061",
        {"day": "LST_Day_1KM", "night": "LST_Night_1KM"},
    ),
}
_DEFAULT_PRODUCT = "11A2"
_DEFAULT_DAYNIGHT = "day"

#: MODIS LST native grid (1 km); used to size the bbox-windowed read.
_NATIVE_CELL_M = 1000.0

#: MODIS LST DN -> physical (raster:bands scale; uint16; fill DN == 0).
_LST_SCALE = 0.02
_KELVIN_C = 273.15

#: bbox area guardrail (deg^2). LST is AOI-scoped (city-to-region); a generous
#: 6 deg^2 (1 km native is coarse, so a wide AOI is still a modest grid) keeps a
#: large metro / county window in-bounds while blocking a continental request.
_MAX_BBOX_DEG2 = 6.0

#: 6-dp bbox quantization (~0.1 m) for cache-key stability.
_BBOX_DECIMALS = 6

#: Single-band deg-C style token. Intentionally avoids the substring
#: "temperature" so publish_layer does NOT apply the Kelvin (250..320 K) family
#: rescale; the deg-C registry entry / band-stats fallback styles it correctly.
_STYLE_PRESET = "land_surface_temp_c"


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_modis_lst",
    ttl_class="static-30d",
    source_class="modis_lst",
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
    """Estimate emitted single-band float32 COG size in MB.

    1 km float32 DEFLATE-COG is tiny (~0.5 MB / sq-deg for a smooth thermal
    surface). Scale linearly with bbox area, floored.
    """
    if bbox is None:
        return 0.25
    try:
        west, south, east, north = bbox
        sq_deg = max(0.0, (east - west)) * max(0.0, (north - south))
    except (TypeError, ValueError):
        return 0.25
    return max(0.1, sq_deg * 0.5)


# ---------------------------------------------------------------------------
# Validation helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise ModisLstBboxError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise ModisLstBboxError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise ModisLstBboxError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise ModisLstBboxError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise ModisLstBboxError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )
    area = (max_lon - min_lon) * (max_lat - min_lat)
    if area > _MAX_BBOX_DEG2:
        raise ModisLstBboxError(
            f"bbox area {area:.3f} deg^2 exceeds {_MAX_BBOX_DEG2} deg^2 guardrail "
            "for fetch_modis_lst (LST is AOI-scoped; narrow the bbox)."
        )


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_DECIMALS) for v in bbox)  # type: ignore[return-value]


def _normalize_product(product: str | None) -> str:
    if product is None:
        return _DEFAULT_PRODUCT
    key = str(product).strip().lower()
    aliases = {
        "mod11a2": "11A2",
        "myd11a2": "11A2",
        "11a2": "11A2",
        "modis-11a2-061": "11A2",
        "modis-11a2": "11A2",
        "mod21a2": "21A2",
        "myd21a2": "21A2",
        "21a2": "21A2",
        "modis-21a2-061": "21A2",
        "modis-21a2": "21A2",
    }
    norm = aliases.get(key, str(product).strip().upper())
    if norm not in _PRODUCTS:
        raise ModisLstParamError(
            f"unknown product {product!r}; expected one of {sorted(_PRODUCTS)} "
            "(11A2 = MOD/MYD11A2 8-day 1km LST; 21A2 = MOD/MYD21A2)."
        )
    return norm


def _normalize_daynight(daynight: str | None) -> str:
    if daynight is None:
        return _DEFAULT_DAYNIGHT
    key = str(daynight).strip().lower()
    aliases = {
        "day": "day",
        "daytime": "day",
        "lst_day": "day",
        "d": "day",
        "night": "night",
        "nighttime": "night",
        "lst_night": "night",
        "n": "night",
    }
    norm = aliases.get(key)
    if norm is None:
        raise ModisLstParamError(
            f"unknown daynight {daynight!r}; expected 'day' or 'night'."
        )
    return norm


def _default_window() -> tuple[str, str]:
    """Default datetime window: a trailing ~120-day window.

    Returns ``(start_iso, end_iso)`` as ``YYYY-MM-DD`` strings. MODIS LST 8-day
    composites land every 8 days; ~120 days gives several composites so a recent
    full-coverage tile is reliably found.
    """
    from datetime import datetime, timedelta, timezone

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=120)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------------------
# SAS signing (MODIS-account aware): per-href sign endpoint w/ token fallback.
# ---------------------------------------------------------------------------


def _sign_href(href: str, collection: str) -> str:
    """Sign ``href`` for GDAL ``/vsicurl/`` read.

    Primary: the per-HREF PC sign endpoint (storage-account aware; works for the
    MODIS ``modiseuwest`` account where the per-collection token 403s). Fallback:
    the per-collection token path in ``_pc_stac.sas_sign_href``.
    """
    try:
        resp = requests.get(
            _PC_SIGN_URL,
            params={"href": href},
            headers={"User-Agent": _pc_stac.USER_AGENT},
            timeout=_SIGN_TIMEOUT_S,
        )
        resp.raise_for_status()
        signed = resp.json().get("href")
        if signed and isinstance(signed, str):
            return signed
    except Exception as exc:  # noqa: BLE001  --  fall back to token path
        logger.warning(
            "fetch_modis_lst: per-href sign failed (%s); falling back to "
            "per-collection token path",
            exc,
        )
    return _pc_stac.sas_sign_href(href, collection)


# ---------------------------------------------------------------------------
# Scene selection: most-recent tile covering the bbox.
# ---------------------------------------------------------------------------


def _select_item(
    bbox: tuple[float, float, float, float],
    datetime_range: str,
    collection: str,
) -> Any:
    """Return the most-recent MODIS LST item intersecting ``bbox`` in the window.

    Raises ``ModisLstNoDataError`` on zero matches, ``ModisLstUpstreamError`` on
    a search failure.
    """
    try:
        from pystac_client import Client
    except ImportError as exc:  # pragma: no cover  --  hard dep
        raise ModisLstUpstreamError(
            f"pystac-client unavailable; cannot search PC STAC: {exc}"
        ) from exc

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
        raise ModisLstUpstreamError(
            f"MODIS LST STAC search failed (collection={collection!r}, "
            f"bbox={bbox}, window={datetime_range}): {exc}"
        ) from exc

    if not items:
        raise ModisLstNoDataError(
            f"no {collection!r} LST tile intersects bbox={bbox} within "
            f"{datetime_range}."
        )

    def _dt_key(it: Any) -> str:
        props = getattr(it, "properties", {}) or {}
        return (
            props.get("datetime")
            or props.get("end_datetime")
            or props.get("start_datetime")
            or ""
        )

    items.sort(key=_dt_key, reverse=True)
    return items[0]


# ---------------------------------------------------------------------------
# Core: search -> windowed read -> scale to deg C -> single-band float32 COG.
# ---------------------------------------------------------------------------


def _read_lst_window(
    signed_href: str,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
) -> Any:
    """Read ``signed_href`` warped to EPSG:4326 and windowed to ``bbox``.

    Returns a 2-D float32 numpy array of raw DN at ``(height_px, width_px)``;
    fill (DN == 0) reads back as 0.0. Raises ``ModisLstUpstreamError`` on any
    read failure.
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
                dst = np.zeros((height_px, width_px), dtype="float32")
                reproject(
                    source=rasterio.band(src, 1),
                    destination=dst,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=dst_transform,
                    dst_crs="EPSG:4326",
                    resampling=Resampling.bilinear,
                    src_nodata=0,
                    dst_nodata=0,
                )
        return dst
    except ModisLstError:
        raise
    except Exception as exc:  # noqa: BLE001  --  translate any rasterio/GDAL error
        raise ModisLstUpstreamError(
            f"MODIS LST band read failed (href={signed_href[:120]!r}): {exc}"
        ) from exc


def _scaled_lst_celsius(dn: Any) -> Any:
    """DN -> land-surface temperature in deg C; fill (DN == 0) -> NaN."""
    import numpy as np

    lst_k = dn * _LST_SCALE
    lst_c = lst_k - _KELVIN_C
    lst_c = np.where(dn == 0, np.nan, lst_c)
    return lst_c.astype("float32")


def _write_lst_cog(
    lst_c: Any,
    bbox: tuple[float, float, float, float],
    width_px: int,
    height_px: int,
) -> bytes:
    """Write a single-band float32 deg-C COG (NaN nodata)."""
    import numpy as np
    import rasterio

    transform = rasterio.transform.from_bounds(
        bbox[0], bbox[1], bbox[2], bbox[3], width_px, height_px
    )
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tif", delete=False, prefix="trid3nt_modislst_"
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
            nodata=float("nan"),
        )
        with rasterio.open(tmp_path, "w", **profile) as dst:
            dst.write(np.asarray(lst_c, dtype="float32"), 1)
        with open(tmp_path, "rb") as fh:
            return fh.read()
    except Exception as exc:  # noqa: BLE001
        raise ModisLstUpstreamError(
            f"MODIS LST COG write failed for bbox={bbox}: {exc}"
        ) from exc
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _fetch_modis_lst_cog_bytes(
    bbox: tuple[float, float, float, float],
    datetime_range: str,
    collection: str,
    asset_key: str,
) -> bytes:
    """Search MODIS LST, scale to deg C, return a single-band float32 COG.

    Raises:
        ``ModisLstNoDataError``: no tile / all-fill window (honest no-data).
        ``ModisLstUpstreamError``: search / sign / read / write failure.
    """
    import numpy as np

    item = _select_item(bbox, datetime_range, collection)

    assets = getattr(item, "assets", {}) or {}
    if asset_key not in assets:
        raise ModisLstUpstreamError(
            f"MODIS LST item {getattr(item, 'id', '?')} missing asset "
            f"{asset_key!r} (have {sorted(assets)[:12]})"
        )

    width_px, height_px = _pc_stac.bbox_pixel_dims(bbox, _NATIVE_CELL_M)
    signed = _sign_href(assets[asset_key].href, collection)
    dn = _read_lst_window(signed, bbox, width_px, height_px)
    lst_c = _scaled_lst_celsius(dn)

    if not bool(np.isfinite(lst_c).any()):
        raise ModisLstNoDataError(
            f"MODIS LST tile {getattr(item, 'id', '?')} produced an all-fill "
            f"(no-valid-pixel) window over bbox={bbox}."
        )

    cog_bytes = _write_lst_cog(lst_c, bbox, width_px, height_px)

    logger.info(
        "fetch_modis_lst: item=%s coll=%s asset=%s bbox=%s -> %d-byte "
        "single-band float32 deg-C COG (%dx%d), valid_frac=%.2f",
        getattr(item, "id", "?"),
        collection,
        asset_key,
        bbox,
        len(cog_bytes),
        width_px,
        height_px,
        float(np.isfinite(lst_c).mean()),
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
def fetch_modis_lst(
    bbox: tuple[float, float, float, float],
    start_date: str | None = None,
    end_date: str | None = None,
    product: str = _DEFAULT_PRODUCT,
    daynight: str = _DEFAULT_DAYNIGHT,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch a MODIS 8-day land-surface-temperature (LST) grid in degrees C.

    **What it does:** Searches the Microsoft Planetary Computer for the
    most-recent MODIS 8-day LST tile covering ``bbox`` in the time window, reads
    the requested ``LST_Day``/``LST_Night`` 1 km band clipped to the bbox, scales
    the uint16 DN to degrees Celsius (``T = DN*0.02 - 273.15``; fill DN==0 ->
    NaN), and returns a SINGLE-BAND float32 COG that paints as a thermal heat map.
    Because it emits the physical deg-C scalar (not a baked RGB) the layer can be
    zonal-summarized, differenced (day vs night, year vs year), or thresholded
    for a heat-risk mask.

    MODIS LST is a GLOBAL 8-day clear-sky COMPOSITE at 1 km, so this is the
    keyless path to a clean urban-heat / drought / surface-temperature surface
    over a city-to-region AOI without hunting for a cloud-free overpass.

    **When to use:**
    - Urban heat-island / extreme-heat surface over a metro or county
      (``daynight="day"`` for peak surface heat; ``"night"`` for heat-retention
      / overnight minimums, the public-health-relevant signal).
    - Drought / thermal-stress context (high LST + low NDVI).
    - A coarse-but-clean LST you can zonal-stat by neighborhood / land cover, or
      difference between two windows (call twice).

    **When NOT to use:**
    - Fine-grained single-scene thermal detail  --  use
      ``fetch_landsat_imagery(band_combo="thermal")`` (30 m, baked RGB snapshot).
    - AIR temperature (2 m)  --  use ``fetch_era5_reanalysis`` / ``fetch_hrrr_forecast``
      / ``fetch_gridmet`` (LST is the SURFACE skin temperature, much hotter than
      air on a sunny day).
    - Sea-surface temperature  --  use ``fetch_noaa_sst``.

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326.
      Required. AOI-scoped (<= 6 deg^2; 1 km native).
    - ``start_date`` / ``end_date`` (str, optional): ``"YYYY-MM-DD"`` window
      bounds. Default: a trailing ~120-day window ending today. For a specific
      heat event set both to that month (e.g. a July heat wave).
    - ``product`` (str, default ``"11A2"``): ``"11A2"`` (MOD/MYD11A2, default) or
      ``"21A2"`` (MOD/MYD21A2 day/night algorithm).
    - ``daynight`` (str, default ``"day"``): ``"day"`` (LST_Day, peak surface
      heat) or ``"night"`` (LST_Night, overnight heat retention).

    **Returns:** A ``LayerURI`` (``layer_type="raster"``, ``role="primary"`` --
    LST is the analytical product) pointing at a single-band float32 deg-C COG in
    the ``static-30d``/``modis_lst`` cache prefix.
    ``style_preset="land_surface_temp_c"``, ``units="Land-surface temperature
    (deg C)"``.

    **Data source:** MODIS 8-day LST via the Microsoft Planetary Computer STAC
    (``modis-11A2-061`` / ``modis-21A2-061``; asset ``LST_Day_1km`` /
    ``LST_Night_1km``). MODIS COGs live in the Azure ``modiseuwest`` account, so
    each href is signed via the per-href PC sign endpoint (the per-collection
    token does not authorize that account).

    FR-CE-8: routed through ``read_through`` so identical ``(bbox, window,
    product, daynight)`` calls reuse the cached COG.
    """
    _validate_bbox(bbox)
    prod = _normalize_product(product)
    dn_key = _normalize_daynight(daynight)
    q_bbox = _round_bbox(bbox)

    collection, asset_by_dn = _PRODUCTS[prod]
    asset_key = asset_by_dn[dn_key]

    if start_date and end_date:
        dt_range = f"{start_date}/{end_date}"
    else:
        s, e = _default_window()
        dt_range = f"{s}/{e}"

    params = {
        "bbox": list(q_bbox),
        "datetime_range": dt_range,
        "product": prod,
        "daynight": dn_key,
        "collection": collection,
        "asset": asset_key,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_modis_lst_cog_bytes(
            q_bbox, dt_range, collection, asset_key
        ),
    )
    assert result.uri is not None, (
        "fetch_modis_lst is cacheable; uri must be set by read_through"
    )

    label = "Day" if dn_key == "day" else "Night"
    return LayerURI(
        layer_id=(
            f"modis-lst-{prod.lower()}-{dn_key}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-"
            f"{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        ),
        name=f"MODIS Land-Surface Temperature ({label})",
        layer_type="raster",
        uri=result.uri,
        style_preset=_STYLE_PRESET,
        role="primary",
        units="Land-surface temperature (deg C)",
    )
