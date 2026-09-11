"""GOES ABI single-band imagery delegate hooks.

The delegate owns the listing and the netCDF socket and returns ``(array, transform,
crs)`` for the shared COG writer. A SINGLE-band float32 PHYSICAL-units COG with most-
recent-frame semantics and a 15-minute ``valid_time`` cache rounding."""

# The four hooks. ``validate`` refuses a missing bbox by name, normalizes the band and
# satellite, and pre-gates the sector, an honest fast-reject before the round trip.
# ``resolve`` rounds ``valid_time`` DOWN to the nearest 15 minutes and normalizes the
# satellite token BEFORE read_through, so both enter the cache key and a same-band,
# same-bbox fetch inside one slot reuses the cached COG. ``read`` lists the most-recent
# key, downloads the netCDF, applies the per-band CF scale and offset, reprojects over
# the bbox, and RECORDS the scan provenance, which is UNRECOVERABLE from the COG on a
# cache hit. ``envelope`` builds the layer_id and name and replays that provenance.

from __future__ import annotations

import logging
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_server.tools.cache import record_provenance

from ...imagery._goes_common import (
    _KEY_START_TIME_RE,
    _PRODUCT_PREFIX,
    _SATELLITE_BUCKETS,
    _doy_hour,
    _download_to_tempfile,
    _list_keys_for_prefix,
    _normalize_satellite,
    GOESBboxRequiredError,
    GOESEmptyError,
    GOESInputError,
    GOESUpstreamError,
)
from ..._router.hooks import register_hook

logger = logging.getLogger(__name__)

__all__ = [
    "estimate_payload_mb",
    "validate_goes_satellite",
    "resolve_goes_satellite",
    "read_goes_satellite",
    "envelope_goes_satellite",
]



# Band-name -> CMI variable mapping in the MCMIPC netCDF (one variable per ABI
# channel). Kept ASCII: a micron or degree symbol here would be commentary.
_BAND_TO_VARIABLE: dict[str, str] = {
    "visible": "CMI_C02",      # ABI band 2: 0.64 um "Red" -- reflectance
    "ir_window": "CMI_C13",    # ABI band 13: 10.35 um clean IR longwave -- BT
    "water_vapor": "CMI_C08",  # ABI band 8: 6.19 um upper-level WV -- BT
}

# Band-name -> physical-units string written into LayerURI.units.
_BAND_TO_UNITS: dict[str, str] = {
    "visible": "reflectance",   # 0..1.5 (clamped reflectance, dimensionless)
    "ir_window": "K",           # brightness temperature, kelvin
    "water_vapor": "K",         # brightness temperature, kelvin
}

# Human display label per band (LayerURI name).
_BAND_LABEL: dict[str, str] = {
    "visible": "Visible (Band 2)",
    "ir_window": "IR Window (Band 13)",
    "water_vapor": "Water Vapor (Band 8)",
}

# CONUS sector approximate bbox in EPSG:4326 -- the MCMIPC scan extent. Used for
# an early bbox-vs-sector fast-reject so a clearly off-sector AOI does not pay the
# S3 round-trip for a query that can never return pixels (honest GOES_EMPTY).
_CONUS_SECTOR_BBOX = (-153.0, 14.0, -52.0, 57.0)

# How many minutes to round ``valid_time`` to in the cache key (15 min = 3 frames
# per cache slot; fresh enough for animations, loose enough to reuse re-runs).
_VALID_TIME_ROUND_MINUTES = 15


# Payload estimator (kept importable for tests; the router synthesizes its own
# from source.yaml's payload_estimate block for the promoted tool).


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None, **_kw: Any
) -> float:
    """Estimate the emitted single-band float32 COG size (bbox-area scaled)."""
    if bbox is None:
        return 5.0
    try:
        w, s, e, n = bbox
        sq = max(0.0, e - w) * max(0.0, n - s)
    except (TypeError, ValueError):
        return 5.0
    return max(0.05, sq * 8.0)




def _band_to_variable(band: str) -> str:
    try:
        return _BAND_TO_VARIABLE[band]
    except KeyError as exc:
        raise GOESInputError(
            f"unknown band={band!r}; allowed: {sorted(_BAND_TO_VARIABLE)}"
        ) from exc


def _validate_bbox(bbox: tuple[float, float, float, float] | None) -> None:
    if bbox is None:
        raise GOESBboxRequiredError(
            "bbox is required for fetch_goes_satellite -- full disk / sector "
            "downloads are ~50MB+; pass a (min_lon, min_lat, max_lon, max_lat)."
        )
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise GOESInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise GOESInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise GOESInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise GOESInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise GOESInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_valid_time(now: datetime) -> str:
    """Round ``now`` (UTC) down to the nearest 15-minute boundary; return ISO-Z."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    floored = (now.minute // _VALID_TIME_ROUND_MINUTES) * _VALID_TIME_ROUND_MINUTES
    rounded = now.replace(minute=floored, second=0, microsecond=0)
    return rounded.strftime("%Y-%m-%dT%H:%M:%SZ")


def _key_start_time(key: str) -> str:
    m = _KEY_START_TIME_RE.search(key)
    return m.group(1) if m else ""


def _pick_most_recent_key(keys: list[str]) -> str:
    """Pick the most-recent MCMIPC key (largest ``_s<YYYYJJJHHMMSSF>`` token)."""
    candidates = [(_key_start_time(k), k) for k in keys]
    candidates = [(t, k) for t, k in candidates if t]
    if not candidates:
        return ""
    candidates.sort()
    return candidates[-1][1]


def _scan_time_iso(start_token: str) -> str | None:
    """Parse the 14-digit ``s<YYYYJJJHHMMSSF>`` scan-start token to an ISO-Z string."""
    if not start_token or len(start_token) < 13:
        return None
    try:
        year = int(start_token[0:4])
        doy = int(start_token[4:7])
        hour = int(start_token[7:9])
        minute = int(start_token[9:11])
        sec = int(start_token[11:13])
        dt = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(
            days=doy - 1, hours=hour, minutes=minute, seconds=sec
        )
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, IndexError):
        return None


def _list_recent_keys(
    satellite: str,
    *,
    now: datetime | None = None,
    lookback_hours: int = 3,
) -> list[str]:
    """MCMIPC keys from the last ``lookback_hours`` hours (walks partitions back)."""
    satellite = _normalize_satellite(satellite)
    bucket = _SATELLITE_BUCKETS[satellite]

    when = now or datetime.now(timezone.utc)
    last_upstream_error: GOESUpstreamError | None = None

    for hours_back in range(lookback_hours + 1):
        probe_when = when - timedelta(hours=hours_back)
        year, doy, hour = _doy_hour(probe_when)
        prefix = f"{_PRODUCT_PREFIX}/{year}/{doy:03d}/{hour:02d}/"
        try:
            keys = _list_keys_for_prefix(bucket, prefix)
        except GOESUpstreamError as exc:
            last_upstream_error = exc
            logger.warning(
                "fetch_goes_satellite: listing prefix=%s failed: %s", prefix, exc
            )
            continue
        if keys:
            logger.info(
                "fetch_goes_satellite: %d MCMIPC keys in %s (hours_back=%d)",
                len(keys), prefix, hours_back,
            )
            return keys

    if last_upstream_error is not None:
        raise last_upstream_error
    raise GOESEmptyError(
        f"no MCMIPC keys in last {lookback_hours}h for satellite={satellite!r}"
    )


def _reproject_and_clip(
    nc_path: str,
    variable: str,
    bbox: tuple[float, float, float, float],
    target_res_deg: float = 0.02,
) -> tuple[Any, Any, str]:
    """Reproject ``variable`` to EPSG:4326 over ``bbox``, returning the ARRAY for the
    shared COG writer. A CMI variable is scaled int16, so the RAW int16 is warped with
    a sentinel and the CF scale and offset applied after, to float32 physical units."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds
    from rasterio.warp import Resampling, reproject

    min_lon, min_lat, max_lon, max_lat = bbox
    sub_uri = f'NETCDF:"{nc_path}":{variable}'

    try:
        import netCDF4  # type: ignore[import-not-found]

        with netCDF4.Dataset(nc_path) as ncds:
            ncvar = ncds.variables[variable]
            scale_factor = float(getattr(ncvar, "scale_factor", 1.0))
            add_offset = float(getattr(ncvar, "add_offset", 0.0))
            fill_value = getattr(ncvar, "_FillValue", None)
            fill_value = float(fill_value) if fill_value is not None else None
    except Exception as exc:  # noqa: BLE001
        raise GOESUpstreamError(
            f"netCDF metadata read failed for {variable} in {nc_path}: {exc}"
        ) from exc

    try:
        src = rasterio.open(sub_uri)
    except Exception as exc:  # noqa: BLE001
        raise GOESUpstreamError(
            f"rasterio could not open netCDF subdataset {sub_uri}: {exc}"
        ) from exc

    try:
        if src.crs is None:
            raise GOESUpstreamError(
                f"netCDF subdataset {variable} has no CRS metadata; cannot reproject"
            )

        out_res_deg = target_res_deg
        width = max(1, int(math.ceil((max_lon - min_lon) / out_res_deg)))
        height = max(1, int(math.ceil((max_lat - min_lat) / out_res_deg)))
        out_transform = from_bounds(min_lon, min_lat, max_lon, max_lat, width, height)

        warp_sentinel = np.iinfo(np.int16).min  # -32768, outside [0, 4095] valid_range
        warped = np.full((height, width), warp_sentinel, dtype=np.int16)
        src_nodata = src.nodata if src.nodata is not None else fill_value
        try:
            reproject(
                source=rasterio.band(src, 1),
                destination=warped,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=out_transform,
                dst_crs="EPSG:4326",
                resampling=Resampling.nearest,
                src_nodata=src_nodata,
                dst_nodata=warp_sentinel,
            )
        except Exception as exc:  # noqa: BLE001
            raise GOESUpstreamError(
                f"rasterio reproject failed for {variable}: {exc}"
            ) from exc

        out_arr = warped.astype(np.float32) * np.float32(scale_factor) + np.float32(add_offset)
        mask = warped == warp_sentinel
        if fill_value is not None:
            mask |= warped == int(fill_value)
        mask |= (warped < 0) | (warped > 4095)
        out_arr[mask] = np.nan

        if not np.isfinite(out_arr).any():
            raise GOESEmptyError(
                f"bbox={bbox} produces no valid {variable} pixels "
                "(likely outside CONUS sector or behind the disk limb)"
            )
        return out_arr, out_transform, "EPSG:4326"
    finally:
        src.close()




@register_hook("goes_satellite.validate")
def validate_goes_satellite(spec: Any, params: dict[str, Any]) -> None:
    """Pre-cache input gate: bbox-required, band + satellite, CONUS fast-reject."""
    bbox = params.get("bbox")
    _validate_bbox(tuple(float(v) for v in bbox) if bbox is not None else None)
    band = params.get("band", "visible")
    if band not in _BAND_TO_VARIABLE:
        raise GOESInputError(
            f"unknown band={band!r}; allowed: {sorted(_BAND_TO_VARIABLE)}"
        )
    # Normalize-then-validate the satellite (raises loud GOESInputError on unknown).
    _normalize_satellite(str(params.get("satellite", "goes-19")))
    res = params.get("target_res_deg")
    if res is not None and (not math.isfinite(float(res)) or float(res) <= 0.0):
        raise GOESInputError(
            f"target_res_deg must be a positive finite degree value; got {res!r}"
        )
    # CONUS-sector fast-reject: an AOI entirely off the MCMIPC scan can never return
    # pixels; raise the honest GOES_EMPTY before paying the S3 round-trip.
    w, s, e, n = (float(v) for v in bbox)
    cw, cs, ce, cn = _CONUS_SECTOR_BBOX
    if e < cw or w > ce or n < cs or s > cn:
        raise GOESEmptyError(
            f"bbox={tuple(bbox)} falls entirely outside the GOES CONUS sector "
            f"{_CONUS_SECTOR_BBOX}; the MCMIPC product is CONUS-only."
        )




@register_hook("goes_satellite.resolve")
def resolve_goes_satellite(spec: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Merge the canonical satellite token + the 15-min-rounded valid_time (cache key)."""
    out: dict[str, Any] = {
        "satellite": _normalize_satellite(str(params.get("satellite", "goes-19"))),
        "valid_time": _round_valid_time(datetime.now(timezone.utc)),
    }
    return out




@register_hook("goes_satellite.read")
def read_goes_satellite(
    spec: Any, params: dict[str, Any], *, timeout_s: float
) -> tuple[Any, Any, Any]:
    """List the most-recent MCMIPC scan, download + reproject; RECORD scan provenance."""
    bbox = tuple(float(v) for v in params["bbox"])
    band = str(params.get("band", "visible"))
    satellite = _normalize_satellite(str(params.get("satellite", "goes-19")))
    res = params.get("target_res_deg")
    target_res_deg = 0.02 if res is None else float(res)

    variable = _band_to_variable(band)
    bucket = _SATELLITE_BUCKETS[satellite]

    keys = _list_recent_keys(satellite)
    chosen = _pick_most_recent_key(keys)
    if not chosen:
        raise GOESEmptyError(
            f"no usable MCMIPC keys found among {len(keys)} candidates for "
            f"satellite={satellite}"
        )
    url = f"https://{bucket}.s3.amazonaws.com/{chosen}"
    logger.info("fetch_goes_satellite: chosen key %s", chosen)

    nc_path = _download_to_tempfile(url)
    try:
        array, transform, crs = _reproject_and_clip(
            nc_path, variable, bbox, target_res_deg
        )
    finally:
        try:
            os.unlink(nc_path)
        except OSError:
            pass

    record_provenance(
        {
            "satellite": satellite,
            "band": band,
            "scan_time": _scan_time_iso(_key_start_time(chosen)),
        }
    )
    return array, transform, crs




@register_hook("goes_satellite.envelope")
def envelope_goes_satellite(
    spec: Any,
    params: dict[str, Any],
    layer: Any,
    data: bytes | None,
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the layer_id and display name, and replay the scan provenance. The display
    name carries a typographic dash, written as an escape so this file stays ASCII."""
    band = str(params.get("band", "visible"))
    satellite = _normalize_satellite(str(params.get("satellite", "goes-19")))
    q_bbox = tuple(float(v) for v in params["bbox"])
    layer_label = _BAND_LABEL.get(band, band)
    # The display string carries a typographic dash, written as an escape.
    name = f"GOES Satellite \u2014 {layer_label} ({satellite.upper()})"
    prov = provenance or {}
    return {
        "layer_id": f"goes-{satellite}-{band}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        "name": name,
        "satellite": str(prov.get("satellite", satellite)),
        "band": str(prov.get("band", band)),
        "scan_time": prov.get("scan_time"),
    }
