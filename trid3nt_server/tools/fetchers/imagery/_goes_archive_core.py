"""GOES raw-archive substrate: the per-frame netCDF core.

Holds the object-window listing over the public buckets, the CF scale and offset
band read with its reproject onto an EPSG:4326 grid, and the RGBA COG writer.
Pure helpers and typed errors only; nothing here composites and no registered tool
lives here - what is computed from the bands is a derive over the layer."""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any


from trid3nt_server.tools.fetchers.imagery._goes_common import (
    _KEY_START_TIME_RE,
    _PRODUCT_PREFIX,
    _SATELLITE_BUCKETS,
    _doy_hour,
    _download_to_tempfile,
    _list_keys_for_prefix,
    _normalize_satellite,
)

__all__ = [
    "GOESArchiveError",
    "GOESArchiveInputError",
    "GOESArchiveUpstreamError",
    "GOESArchiveEmptyError",
    "MAX_ARCHIVE_FRAMES",
    "_parse_utc",
    "_key_start_datetime",
    "_select_window_keys",
    "_list_archive_keys_in_window",
    "_band_valid_dn_range",
    "_grid_for_bbox",
    "_warp_band_to_physical",
]


logger = logging.getLogger("trid3nt_server.tools.fetchers.imagery._goes_archive_core")


class GOESArchiveError(RuntimeError):
    """Base class for raw-archive failures."""

    error_code: str = "GOES_ARCHIVE_ERROR"
    retryable: bool = True


class GOESArchiveInputError(GOESArchiveError):
    """Invalid input (unknown satellite, bad window, bad bbox)."""

    error_code = "GOES_ARCHIVE_INPUT_INVALID"
    retryable = False


class GOESArchiveUpstreamError(GOESArchiveError):
    """S3 listing or netCDF download/parse failed."""

    error_code = "GOES_ARCHIVE_UPSTREAM_ERROR"
    retryable = True


class GOESArchiveEmptyError(GOESArchiveError):
    """The window matched no MCMIPC keys, or every frame crop was empty."""

    error_code = "GOES_ARCHIVE_EMPTY"
    retryable = False


#: Upper bound on emitted frames. A wider window even-subsamples down
#: (first + last kept). Overridable via env.
MAX_ARCHIVE_FRAMES: int = int(os.environ.get("TRID3NT_MAX_ARCHIVE_FRAMES", "144"))


#: Output resolution (degrees) for the EPSG:4326 reproject (~2 km, matching the
#: ABI nominal sub-satellite resolution), which is the MCMIPC product's own common
#: grid for all sixteen channels.
_OUT_RES_DEG = 0.02


#: Bbox quantization (6dp) for cache-key stability.
_BBOX_QUANTIZE_DP = 6


def _parse_utc(value: Any) -> datetime:
    """Parse an ISO-8601 string or datetime to aware UTC, accepting a trailing 'Z' or
    '+00:00', a space or 'T' separator, and a bare date. An unparseable value raises
    ``GOESArchiveInputError``."""
    if isinstance(value, datetime):
        dt = value
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise GOESArchiveInputError(
            f"time must be an ISO-8601 string or datetime; got {value!r}"
        )
    s = value.strip().replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(value.strip().replace(" ", "T", 1), fmt)
                break
            except ValueError:
                continue
        else:
            raise GOESArchiveInputError(
                f"could not parse UTC time {value!r}; use ISO-8601 "
                "(e.g. '2026-06-22T13:30:00Z')"
            )
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _key_start_datetime(key: str) -> datetime | None:
    """Parse the ``_s<YYYYDOYHHMMSSf>`` start time of an MCMIPC key to aware UTC: the
    ABI convention is 4-digit year, 3-digit day-of-year, hour, minute, second and a
    tenth. A key with no recognizable start time returns ``None``."""
    m = _KEY_START_TIME_RE.search(key)
    if not m:
        return None
    s = m.group(1)  # 14 digits: YYYYDDDHHMMSSf
    try:
        year = int(s[0:4])
        doy = int(s[4:7])
        hour = int(s[7:9])
        minute = int(s[9:11])
        second = int(s[11:13])
    except (ValueError, IndexError):
        return None
    try:
        base = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1)
        return base.replace(hour=hour, minute=minute, second=second)
    except (ValueError, OverflowError):
        return None


def _iso_z(dt: datetime) -> str:
    """Render an aware UTC datetime as an ISO-8601 'Z' string (second precision)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _select_window_keys(keys: list[str], cap: int = MAX_ARCHIVE_FRAMES) -> list[str]:
    """Even-subsample an ASCENDING list of keys down to ``cap``, keeping both endpoints.
    At or under the cap the list is returned unchanged; over it, a rounded linspace
    keeps the first and last and subsamples the middle."""
    n = len(keys)
    if n <= 0:
        return []
    if n <= cap:
        return list(keys)
    import numpy as np

    idx = np.linspace(0, n - 1, cap).round().astype(int)
    kept_idx = [int(i) for i in np.unique(idx)]
    logger.info(
        "goes archive: %d in-window MCMIPC keys exceed cap=%d; "
        "subsampling evenly to %d (first+last kept).",
        n,
        cap,
        len(kept_idx),
    )
    return [keys[i] for i in kept_idx]


def _hours_in_window(start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """Every top-of-hour datetime whose hour overlaps [start, end] in UTC. The archive
    keys are partitioned by hour and a frame at HH:MM lives under the HH partition, so
    every partition the window touches must be listed, the end hour included."""
    start_h = start_utc.replace(minute=0, second=0, microsecond=0)
    out: list[datetime] = []
    cur = start_h
    # Cap the walk defensively so a malformed huge window cannot list forever.
    max_hours = 24 * 31  # one month of hour-partitions
    while cur <= end_utc and len(out) < max_hours:
        out.append(cur)
        cur = cur + timedelta(hours=1)
    return out


def _list_archive_keys_in_window(
    satellite: str,
    start_utc: datetime,
    end_utc: datetime,
    *,
    session: Any = None,
) -> list[tuple[datetime, str]]:
    """List ``(start_time, key)`` pairs inside [start, end], ORDERED ASCENDING, by
    walking every hour partition the window touches. An unknown satellite is an input
    error; every probed partition failing is an upstream error."""
    # Normalize any human/LLM spelling (GOES-18 / goes18 / G18 / "GOES West" / 18)
    # to the canonical "goes-NN" token BEFORE the bucket lookup; a truly-unknown
    # bird raises the shared loud typed error. (Belt-and-suspenders: this helper is
    # importable/callable directly in tests + siblings, so it normalizes its own
    # entry rather than trusting the caller.)
    satellite = _normalize_satellite(satellite)
    bucket = _SATELLITE_BUCKETS.get(satellite)
    if bucket is None:
        raise GOESArchiveInputError(
            f"unknown satellite={satellite!r}; allowed: {sorted(_SATELLITE_BUCKETS)}"
        )

    pairs: list[tuple[datetime, str]] = []
    hours = _hours_in_window(start_utc, end_utc)
    n_fail = 0
    last_exc: Exception | None = None
    for probe in hours:
        year, doy, hour = _doy_hour(probe)
        prefix = f"{_PRODUCT_PREFIX}/{year}/{doy:03d}/{hour:02d}/"
        try:
            keys = _list_keys_for_prefix(bucket, prefix, session=session)
        except Exception as exc:  # noqa: BLE001 -- per-hour failure tolerated
            n_fail += 1
            last_exc = exc
            logger.warning(
                "goes archive: listing prefix=%s failed: %s",
                prefix,
                exc,
            )
            continue
        for k in keys:
            # MCMIPC product only (the prefix already scopes it, but guard).
            if "MCMIPC" not in k:
                continue
            t = _key_start_datetime(k)
            if t is None:
                continue
            if start_utc <= t <= end_utc:
                pairs.append((t, k))

    # All probed hours failed (and there were hours to probe) -> upstream error.
    if not pairs and hours and n_fail == len(hours):
        raise GOESArchiveUpstreamError(
            f"every one of {n_fail} S3 hour-partition listings failed for "
            f"{satellite} in window {_iso_z(start_utc)}..{_iso_z(end_utc)}"
            + (f": {last_exc}" if last_exc else "")
        )

    # Sort ascending by start time; dedupe on start time (a scan can have a
    # mode-change duplicate key) keeping the first.
    pairs.sort(key=lambda p: (p[0], p[1]))
    deduped: list[tuple[datetime, str]] = []
    seen_ts: set[str] = set()
    for t, k in pairs:
        tag = _iso_z(t)
        if tag in seen_ts:
            continue
        seen_ts.add(tag)
        deduped.append((t, k))
    return deduped


#: Fallback DN valid range if a CMI variable carries no usable ``valid_range``.
#: 14-bit covers BOTH the emissive C07 (true [0, 16383]) and the reflective
#: C05/C06 (true [0, 4095]) -- a too-wide fallback is safe (valid DN never
#: exceed their own 12-bit ceiling) where a too-narrow 4095 was the bug.
_DEFAULT_VALID_DN_RANGE = (0, 16383)


def _band_valid_dn_range(ncvar: Any) -> tuple[int, int]:
    """The ``(lo, hi)`` valid raw-DN range for a CMI band variable, read off its own CF
    ``valid_range`` and falling back to the 14-bit default only when that attribute is
    absent or malformed."""

    # The range DIFFERS BY BAND: the emissive C07 is a 14-bit product while the
    # reflective C05 and C06 are 12-bit. Masking C07 with the 12-bit ceiling drops
    # essentially every warm-land pixel -- a 320 K brightness temperature is DN ~9368 --
    # and the RED channel then reads 0 across the whole frame. A wide fallback never
    # masks real DN, where the narrow one did.
    raw = getattr(ncvar, "valid_range", None)
    try:
        if raw is not None and len(raw) >= 2:
            lo = int(raw[0])
            hi = int(raw[1])
            if hi > lo:
                return lo, hi
    except (TypeError, ValueError):
        pass
    return _DEFAULT_VALID_DN_RANGE


def _grid_for_bbox(
    bbox: tuple[float, float, float, float],
    res_deg: float = _OUT_RES_DEG,
) -> tuple[Any, int, int]:
    """Build the output EPSG:4326 ``(transform, width, height)`` for ``bbox`` at
    ``res_deg``. Shared by every product, so the base, the detection bands and the fire
    overlay always land co-registered with no extra resample."""
    from rasterio.transform import from_bounds

    min_lon, min_lat, max_lon, max_lat = bbox
    width = max(1, int(math.ceil((max_lon - min_lon) / res_deg)))
    height = max(1, int(math.ceil((max_lat - min_lat) / res_deg)))
    out_transform = from_bounds(min_lon, min_lat, max_lon, max_lat, width, height)
    return out_transform, width, height


def _warp_band_to_physical(
    nc_path: str,
    variable: str,
    out_transform: Any,
    width: int,
    height: int,
) -> Any:
    """Read one CMI band, CF-scale it and reproject it onto the EPSG:4326 grid, to a
    ``(H, W)`` float32 array in PHYSICAL units with NaN where invalid. A missing
    variable, or an open or reproject failure, raises the upstream error."""

    # The order matters: read the CF scale, offset, fill and per-band valid_range; read
    # the raw DN and inherit the geostationary CRS, warping nearest so the int16 fill
    # propagates cleanly; then apply scale and offset, masking the warp sentinel, the CF
    # fill and any out-of-valid-range DN to NaN.
    import numpy as np
    import netCDF4  # type: ignore[import-not-found]
    import rasterio
    from rasterio.warp import Resampling, reproject

    warp_sentinel = int(np.iinfo(np.int16).min)  # -32768, outside the valid range

    # CF attrs.
    try:
        with netCDF4.Dataset(nc_path) as ncds:
            if variable not in ncds.variables:
                raise GOESArchiveUpstreamError(
                    f"MCMIPC netCDF {nc_path} has no variable {variable!r}; "
                    f"available CMI vars: "
                    f"{[v for v in ncds.variables if v.startswith('CMI_')]}"
                )
            ncvar = ncds.variables[variable]
            scale_factor = float(getattr(ncvar, "scale_factor", 1.0))
            add_offset = float(getattr(ncvar, "add_offset", 0.0))
            fill_raw = getattr(ncvar, "_FillValue", None)
            fill_value = float(fill_raw) if fill_raw is not None else None
            # Per-band valid DN range. CRITICAL: this differs by band -- the
            # thermal/emissive C07 (3.9um) and the longwave C13/C15 are 14-bit
            # products (valid_range [0, 16383]), while the reflective C05/C06 are
            # 12-bit (valid_range [0, 4095]). Hardcoding 4095 masks ~all warm-land
            # C07 DN (a 320 K pixel is DN ~9368, far above 4095) -> RED collapses
            # to 0 over the whole frame while G/B look fine. Read the actual range
            # so each band masks correctly.
            valid_lo, valid_hi = _band_valid_dn_range(ncvar)
    except GOESArchiveError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GOESArchiveUpstreamError(
            f"netCDF metadata read failed for {variable} in {nc_path}: {exc}"
        ) from exc

    sub_uri = f'NETCDF:"{nc_path}":{variable}'
    try:
        src = rasterio.open(sub_uri)
    except Exception as exc:  # noqa: BLE001
        raise GOESArchiveUpstreamError(
            f"rasterio could not open netCDF subdataset {sub_uri}: {exc}"
        ) from exc
    try:
        if src.crs is None:
            raise GOESArchiveUpstreamError(
                f"netCDF subdataset {variable} has no CRS metadata; cannot "
                "reproject (expected the ABI geostationary projection)"
            )
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
            raise GOESArchiveUpstreamError(
                f"rasterio reproject failed for {variable}: {exc}"
            ) from exc
    finally:
        src.close()

    # CF unscale -> physical units; mask sentinel + CF fill + out-of-range DN.
    phys = warped.astype(np.float32) * np.float32(scale_factor) + np.float32(add_offset)
    mask = warped == warp_sentinel
    if fill_value is not None:
        mask |= warped == int(fill_value)
    # Mask out-of-valid-range DN using THIS band's range (14-bit for the
    # emissive C07/C13/C15, 12-bit for C05/C06) -- never a hardcoded 4095.
    mask |= (warped < valid_lo) | (warped > valid_hi)
    phys[mask] = np.nan
    return phys


def _rgba_array_to_cog_bytes(
    rgba: Any,
    out_transform: Any,
    width: int,
    height: int,
) -> bytes:
    """Write a ``(4, H, W)`` uint8 RGBA array to COG bytes, band 4 being the ALPHA
    channel and tagged ColorInterp alpha so the tile server and the map honour
    transparency. The baked colours then render with no style row."""
    import numpy as np
    import rasterio
    import tempfile
    from rasterio.enums import ColorInterp

    rgba = np.asarray(rgba, dtype=np.uint8)
    out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_firehot_cog_")
    os.close(out_fd)
    try:
        profile = {
            "driver": "COG",
            "dtype": "uint8",
            "count": 4,
            "height": height,
            "width": width,
            "crs": "EPSG:4326",
            "transform": out_transform,
            "compress": "DEFLATE",
        }
        try:
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(rgba)
                dst.colorinterp = (
                    ColorInterp.red,
                    ColorInterp.green,
                    ColorInterp.blue,
                    ColorInterp.alpha,
                )
        except Exception as exc:  # noqa: BLE001 -- COG driver may be unavailable
            logger.warning(
                "goes archive: RGBA COG write failed (%s); "
                "falling back to GTiff",
                exc,
            )
            profile["driver"] = "GTiff"
            profile["tiled"] = True
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(rgba)
                dst.colorinterp = (
                    ColorInterp.red,
                    ColorInterp.green,
                    ColorInterp.blue,
                    ColorInterp.alpha,
                )
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _round_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_QUANTIZE_DP) for v in bbox)  # type: ignore[return-value]
