"""``fetch_chirps_precipitation`` atomic tool — UCSB CHIRPS quasi-global rainfall COG.

Wraps the UC Santa Barbara Climate Hazards Center CHIRPS-2.0 (Climate Hazards
group InfraRed Precipitation with Station data) public archive and returns a
single-band float32 millimetre precipitation Cloud-Optimized GeoTIFF clipped to
the requested bbox (or the quasi-global CHIRPS extent when ``bbox`` is omitted,
per the ``supports_global_query=True`` opt-in).

CHIRPS is a quasi-global (50 S - 50 N) 0.05 deg (~5 km) gridded rainfall product
blending satellite IR cold-cloud-duration estimates with in-situ station data.
It is THE community-standard drought / agriculture / pluvial-flood precipitation
baseline for data-sparse regions (Africa, South Asia, Latin America) where gauge
networks and CONUS-only radar products (MRMS) do not reach. This complements the
existing CONUS-only ``fetch_mrms_qpe`` (radar QPE) and ``fetch_gridmet`` (CONUS
gridded met) by giving the agent a keyless, truly global rainfall layer.

Source (keyless, no auth, no API key, no Earthdata login):

    base:    https://data.chc.ucsb.edu/products/CHIRPS-2.0/
    monthly: global_monthly/tifs/chirps-v2.0.YYYY.MM.tif.gz
    daily:   global_daily/tifs/p05/YYYY/chirps-v2.0.YYYY.MM.DD.tif.gz

Each archive entry is a gzip-compressed GeoTIFF:
    - EPSG:4326, 0.05 deg grid, shape (2000, 7200) global
    - bounds (-180, -50, 180, 50)
    - dtype float32, values in mm (monthly: mm/month; daily: mm/day)
    - nodata sentinel = -9999.0 (ocean / no-data; NOT tagged in the header,
      so we detect and tag it explicitly). We collapse it to the GeoTIFF
      nodata so downstream consumers mask cleanly.

We HTTP-download the date's raster, gunzip it, window to the bbox with
rasterio (cheap — the source is already a geographic 0.05 deg grid so the
window is a pure array slice with a derived transform), tag nodata, and write
a deflate-compressed COG. Routed through ``read_through`` so identical
``(bbox, period, date)`` calls reuse the cached COG.

Honest typed errors:
    - ``CHIRPSInputError``   — bad bbox / unparseable or out-of-range date / bad period.
    - ``CHIRPSNotAvailableError`` — the archive has no raster for that date
      (HTTP 404: future date, pre-1981 date, or an unpublished recent month/day).
    - ``CHIRPSUpstreamError`` — network / gzip / decode failure (retryable).
    - ``CHIRPSEmptyError``   — bbox does not intersect the CHIRPS 50S-50N extent.

Tier-1 free (no auth). Single-band float32 mm COG; ``style_preset="precip_mm"``.
"""

from __future__ import annotations

import gzip
import logging
import math
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import date as _date, datetime, timezone
from typing import Any, Literal

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through

__all__ = [
    "fetch_chirps_precipitation",
    "estimate_payload_mb",
    "_resolve_chirps_url",  # exported for tests
    "_window_chirps_to_cog",  # exported for tests
]

logger = logging.getLogger("trid3nt_server.tools.fetch_chirps_precipitation")


# ---------------------------------------------------------------------------
# Typed-error surface (FR-AS-11).
# ---------------------------------------------------------------------------


class CHIRPSError(RuntimeError):
    """Base class for fetch_chirps_precipitation failures."""

    error_code: str = "CHIRPS_ERROR"
    retryable: bool = True


class CHIRPSInputError(CHIRPSError):
    """Bad inputs (malformed bbox, unparseable / out-of-range date, bad period)."""

    error_code = "CHIRPS_INPUT_ERROR"
    retryable = False


class CHIRPSUpstreamError(CHIRPSError):
    """CHC archive download / gzip / decode failed (retryable)."""

    error_code = "CHIRPS_UPSTREAM_ERROR"
    retryable = True


class CHIRPSNotAvailableError(CHIRPSError):
    """The archive has no raster for the requested date (HTTP 404)."""

    error_code = "CHIRPS_NOT_AVAILABLE"
    retryable = False


class CHIRPSEmptyError(CHIRPSError):
    """Bbox does not intersect the CHIRPS quasi-global extent (50S-50N)."""

    error_code = "CHIRPS_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: UCSB Climate Hazards Center public CHIRPS-2.0 archive (keyless, no auth).
_CHC_BASE = "https://data.chc.ucsb.edu/products/CHIRPS-2.0"

#: CHIRPS quasi-global extent (EPSG:4326). The source raster spans 50S-50N.
_CHIRPS_BBOX: tuple[float, float, float, float] = (-180.0, -50.0, 180.0, 50.0)

#: Native CHIRPS resolution in degrees (~5 km).
_CHIRPS_RES_DEG = 0.05

#: CHIRPS no-data sentinel embedded in the pixel values (ocean / masked).
#: The published GeoTIFF header does NOT tag nodata, so we detect any value
#: <= this threshold and collapse to the GeoTIFF nodata convention.
_CHIRPS_NODATA_SENTINEL = -9000.0
_NODATA = -9999.0

#: CHIRPS-2.0 record starts in 1981.
_CHIRPS_FIRST_YEAR = 1981

#: HTTP settings.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)
_DOWNLOAD_TIMEOUT = 180.0


# ---------------------------------------------------------------------------
# Metadata.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    # CHIRPS monthly/daily values are fixed once published, so static-30d is
    # the right TTL (historic rasters never change; the bucket lifecycle
    # evicts stale entries). A recent month/day that 404s simply re-fetches
    # on the next call after publication because the miss is never cached.
    common = dict(
        name="fetch_chirps_precipitation",
        ttl_class="static-30d",
        source_class="chirps_precipitation",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(
            **common,
            supports_global_query=True,
            payload_mb_estimator_name="estimate_payload_mb",
        )  # type: ignore[call-arg]
    except Exception:  # pydantic ValidationError when fields absent
        logger.debug(
            "AtomicToolMetadata missing supports_global_query / "
            "payload_mb_estimator_name; registering fetch_chirps_precipitation "
            "without them"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    period: str | None = None,
    **_kw: Any,
) -> float:
    """Estimate the output COG size in MB for a given call.

    CHIRPS is 0.05 deg (~5 km): the full quasi-global grid is 7200 x 2000 =
    14.4M pixels x 4 bytes = ~57 MB uncompressed. DEFLATE (float predictor) on
    precipitation data compresses ~4x, so the full grid is ~14 MB on disk.
    We scale linearly by fractional area vs the quasi-global extent.
    """
    _GLOBAL_SQ_DEG = 360.0 * 100.0  # 36000 sq-deg over 50S-50N
    _MB_PER_SQ_DEG = 57.0 / _GLOBAL_SQ_DEG / 4.0  # ~57 MB uncompressed / 4x ratio

    if bbox is None:
        sq_deg = _GLOBAL_SQ_DEG
    else:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.001, (east - west)) * max(0.001, (north - south))
        except (TypeError, ValueError):
            sq_deg = 1.0
    # Floor so even a tiny bbox reports a non-zero estimate.
    return max(0.02, _MB_PER_SQ_DEG * sq_deg)


# ---------------------------------------------------------------------------
# bbox + date helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    if len(bbox) != 4:
        raise CHIRPSInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise CHIRPSInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise CHIRPSInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise CHIRPSInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise CHIRPSInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _parse_date(date_str: str, period: str) -> _date:
    """Parse the requested date string for the given period.

    For ``period="monthly"`` accept ``"YYYY-MM"`` or ``"YYYY-MM-DD"`` (day
    ignored). For ``period="daily"`` require a full ``"YYYY-MM-DD"``.
    Raises ``CHIRPSInputError`` on a malformed or out-of-range date.
    """
    if not isinstance(date_str, str) or not date_str.strip():
        raise CHIRPSInputError(
            f"date must be a non-empty string; got {date_str!r}"
        )
    s = date_str.strip()
    try:
        if period == "monthly":
            m = re.fullmatch(r"(\d{4})-(\d{2})(?:-\d{2})?", s)
            if not m:
                raise ValueError("expected YYYY-MM or YYYY-MM-DD")
            year, month = int(m.group(1)), int(m.group(2))
            if not (1 <= month <= 12):
                raise ValueError(f"month {month} out of 1..12")
            parsed = _date(year, month, 1)
        else:  # daily
            m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", s)
            if not m:
                raise ValueError("expected YYYY-MM-DD")
            parsed = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError as exc:
        raise CHIRPSInputError(
            f"date={date_str!r} is not a valid {period} date: {exc}"
        ) from exc

    if parsed.year < _CHIRPS_FIRST_YEAR:
        raise CHIRPSInputError(
            f"CHIRPS-2.0 starts in {_CHIRPS_FIRST_YEAR}; date={date_str!r} predates it"
        )
    today = datetime.now(timezone.utc).date()
    if parsed > today:
        raise CHIRPSInputError(
            f"date={date_str!r} is in the future; CHIRPS only publishes past data"
        )
    return parsed


# ---------------------------------------------------------------------------
# URL resolution.
# ---------------------------------------------------------------------------


def _resolve_chirps_url(d: _date, period: str) -> str:
    """Build the CHC archive .tif.gz URL for the given date + period."""
    if period == "monthly":
        return (
            f"{_CHC_BASE}/global_monthly/tifs/"
            f"chirps-v2.0.{d.year:04d}.{d.month:02d}.tif.gz"
        )
    # daily
    return (
        f"{_CHC_BASE}/global_daily/tifs/p05/{d.year:04d}/"
        f"chirps-v2.0.{d.year:04d}.{d.month:02d}.{d.day:02d}.tif.gz"
    )


# ---------------------------------------------------------------------------
# Download + window + COG.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float) -> bytes:
    """HTTP GET against the public archive. Maps 404 -> CHIRPSNotAvailableError."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise CHIRPSNotAvailableError(
                f"CHIRPS archive has no raster at {url} (HTTP 404) — the date may be "
                f"too recent to be published yet, or outside the CHIRPS record"
            ) from exc
        raise CHIRPSUpstreamError(
            f"CHIRPS archive returned HTTP {exc.code} for {url}"
        ) from exc
    except urllib.error.URLError as exc:
        raise CHIRPSUpstreamError(
            f"CHIRPS archive network error for {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise CHIRPSUpstreamError(
            f"CHIRPS archive timed out after {timeout}s for {url}"
        ) from exc


def _window_chirps_to_cog(
    tif_bytes: bytes,
    bbox: tuple[float, float, float, float] | None,
) -> bytes:
    """Window a decompressed CHIRPS GeoTIFF to bbox; return single-band mm COG bytes.

    The source is already an EPSG:4326 0.05 deg grid, so windowing is a pure
    array slice with a derived transform (no reprojection). CHIRPS sentinel
    no-data (ocean / masked, <= -9000) is collapsed to the GeoTIFF nodata.
    """
    try:
        import numpy as np
        import rasterio
        from rasterio.io import MemoryFile
        from rasterio.windows import from_bounds
    except ImportError as exc:
        raise CHIRPSUpstreamError(f"rasterio / numpy not available: {exc}") from exc

    tmp_src: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as sf:
            sf.write(tif_bytes)
            tmp_src = sf.name

        with rasterio.open(tmp_src) as src:
            src_transform = src.transform
            src_height, src_width = src.shape
            src_crs = src.crs or rasterio.crs.CRS.from_epsg(4326)

            if bbox is not None:
                window = from_bounds(
                    bbox[0], bbox[1], bbox[2], bbox[3], transform=src_transform
                )
                row_off = max(0, int(math.floor(window.row_off)))
                col_off = max(0, int(math.floor(window.col_off)))
                row_end = min(src_height, int(math.ceil(window.row_off + window.height)))
                col_end = min(src_width, int(math.ceil(window.col_off + window.width)))
                if row_end <= row_off or col_end <= col_off:
                    raise CHIRPSEmptyError(
                        f"bbox={bbox} does not intersect the CHIRPS extent "
                        f"({_CHIRPS_BBOX})"
                    )
                read_window = rasterio.windows.Window(
                    col_off, row_off, col_end - col_off, row_end - row_off
                )
                arr = src.read(1, window=read_window).astype("float32")
                out_transform = src.window_transform(read_window)
            else:
                arr = src.read(1).astype("float32")
                out_transform = src_transform

        out_height, out_width = arr.shape

        # Collapse CHIRPS ocean / no-data sentinel to GeoTIFF nodata.
        arr = np.where(arr <= _CHIRPS_NODATA_SENTINEL, _NODATA, arr).astype("float32")

        # Guard against an all-nodata window (e.g. an open-ocean bbox).
        if not np.any(arr > _NODATA):
            raise CHIRPSEmptyError(
                f"bbox={bbox} clipped CHIRPS to all-nodata (ocean / outside land "
                f"coverage); no precipitation pixels in the requested area"
            )

        profile = {
            "driver": "COG",
            "height": out_height,
            "width": out_width,
            "count": 1,
            "dtype": "float32",
            "crs": src_crs,
            "transform": out_transform,
            "nodata": _NODATA,
            "compress": "deflate",
            "predictor": 3,  # float predictor
        }
        try:
            with MemoryFile() as memf:
                with memf.open(**profile) as dst:
                    dst.write(arr, 1)
                    dst.set_band_description(1, "precipitation_mm")
                    dst.update_tags(
                        units="mm",
                        source="UCSB Climate Hazards Center CHIRPS-2.0",
                        nodata_meaning="-9999 = ocean / no-data (CHIRPS sentinel collapsed)",
                    )
                    dst.update_tags(1, units="mm", long_name="precipitation")
                return memf.read()
        except Exception:  # noqa: BLE001 — fall back to plain GTiff if COG driver fails
            gtiff_profile = dict(profile)
            gtiff_profile.update(
                driver="GTiff", tiled=True, blockxsize=256, blockysize=256
            )
            with MemoryFile() as memf:
                with memf.open(**gtiff_profile) as dst:
                    dst.write(arr, 1)
                    dst.set_band_description(1, "precipitation_mm")
                    dst.update_tags(units="mm", source="CHIRPS-2.0")
                return memf.read()
    finally:
        if tmp_src:
            try:
                os.unlink(tmp_src)
            except OSError:
                pass


def _fetch_chirps_bytes(
    d: _date,
    period: str,
    bbox: tuple[float, float, float, float] | None,
) -> bytes:
    """Download a CHIRPS .tif.gz, gunzip, window to bbox, return COG bytes."""
    url = _resolve_chirps_url(d, period)
    logger.info("fetch_chirps_precipitation: downloading %s", url)
    gz_bytes = _http_get(url, timeout=_DOWNLOAD_TIMEOUT)
    if not gz_bytes:
        raise CHIRPSUpstreamError(f"empty response from {url}")
    try:
        tif_bytes = gzip.decompress(gz_bytes)
    except (OSError, gzip.BadGzipFile) as exc:
        raise CHIRPSUpstreamError(
            f"gzip decompression failed for {url}: {exc}"
        ) from exc
    logger.info(
        "fetch_chirps_precipitation: gz=%d bytes, tif=%d bytes",
        len(gz_bytes),
        len(tif_bytes),
    )
    return _window_chirps_to_cog(tif_bytes, bbox)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    open_world_hint=True,
)
def fetch_chirps_precipitation(
    bbox: tuple[float, float, float, float] | None = None,
    date: str | None = None,
    period: Literal["monthly", "daily"] = "monthly",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch UCSB CHIRPS quasi-global rainfall as a single-band mm COG.

    **What it does:** Downloads a CHIRPS-2.0 monthly or daily precipitation
    GeoTIFF from the keyless UC Santa Barbara Climate Hazards Center public
    archive (``data.chc.ucsb.edu``), gunzips it, windows to ``bbox``, collapses
    the CHIRPS ocean / no-data sentinel to GeoTIFF nodata, and writes a
    deflate-compressed single-band float32 Cloud-Optimized GeoTIFF in mm.
    CHIRPS is 0.05 deg (~5 km) and quasi-global (50 S - 50 N). Tier-1 free, no
    API key, no login.

    **When to use:**

    - Quasi-global rainfall baseline for drought, agriculture, and pluvial-flood
      context OUTSIDE CONUS / outside radar coverage — Africa, South Asia, Latin
      America, the tropics. CHIRPS is the community standard for data-sparse
      regions.
    - Monsoon / wet-season precipitation totals: ``period="monthly"`` returns
      mm/month; e.g. peak-monsoon Western Ghats:
      ``fetch_chirps_precipitation(bbox=(72,15,78,21), date="2023-07")``.
    - Event-scale daily rainfall: ``period="daily"`` returns mm/day for a single
      date, e.g. ``date="2022-08-25"``.
    - Drought analysis (anomaly vs climatology), seasonal ag water-balance, and
      flood antecedent-rainfall context where MRMS / gridMET do not reach.

    **When NOT to use:**

    - CONUS gauge-corrected radar precipitation at ~1 km / sub-daily — use
      ``fetch_mrms_qpe`` (NOAA MRMS QPE, CONUS only).
    - CONUS gridded daily meteorology (precip + temp + wind + humidity) at
      ~4 km — use ``fetch_gridmet``.
    - Hourly / forecast precipitation — use ``fetch_hrrr_forecast`` (CONUS) or
      ``fetch_era5_reanalysis`` (global hourly, needs a Copernicus CDS key).
    - Areas poleward of 50 deg latitude — CHIRPS coverage stops at 50 S / 50 N;
      a bbox entirely outside that band raises ``CHIRPSEmptyError``.

    **Parameters:**

    - ``bbox``: ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326. When
      ``None``, returns the full quasi-global CHIRPS grid (~7200 x 2000 px;
      ``supports_global_query=True``). Must intersect ``(-180, -50, 180, 50)``
      or ``CHIRPSEmptyError`` is raised.
    - ``date``: for ``period="monthly"`` a ``"YYYY-MM"`` (or ``"YYYY-MM-DD"``,
      day ignored) string; for ``period="daily"`` a full ``"YYYY-MM-DD"``.
      Required. Must be within the CHIRPS record (1981-present, past dates only).
    - ``period``: ``"monthly"`` (default, mm/month) or ``"daily"`` (mm/day).

    **Returns:**

    ``LayerURI`` pointing at the cached COG
    (``s3://<cache-bucket>/cache/static-30d/chirps_precipitation/<key>.tif``).
    COG: float32, EPSG:4326, deflate-compressed, nodata=-9999, band 1
    ``"precipitation_mm"``. ``layer_type="raster"``, ``role="primary"``,
    ``units="mm"``, ``style_preset="precip_mm"``.

    Raises: ``CHIRPSInputError`` (bad bbox / date / period),
    ``CHIRPSNotAvailableError`` (no published raster for that date, HTTP 404),
    ``CHIRPSUpstreamError`` (network / gzip / decode failure, retryable),
    ``CHIRPSEmptyError`` (bbox outside the CHIRPS 50S-50N extent or all-ocean).

    **Cross-tool dependencies:**

    - Pair with ``fetch_us_drought_monitor`` (CONUS drought) for a global vs
      CONUS drought view, or ``compute_zonal_statistics`` for per-admin-unit or
      per-field rainfall totals.
    - Alternative for CONUS radar precip: ``fetch_mrms_qpe``. Alternative for
      global hourly reanalysis precip: ``fetch_era5_reanalysis`` (CDS key).

    Cache: ``ttl_class="static-30d"`` (published rasters are immutable); key =
    SHA-256 of ``(bbox-6dp, period, date)``. Payload estimate:
    ``estimate_payload_mb(bbox, period)``.
    """
    if date is None:
        raise CHIRPSInputError(
            "date is required, e.g. date='2023-07' (monthly) or "
            "date='2023-07-15' (daily)"
        )
    if period not in ("monthly", "daily"):
        raise CHIRPSInputError(
            f"period must be 'monthly' or 'daily'; got {period!r}"
        )

    d = _parse_date(date, period)

    # Validate bbox (None means quasi-global per supports_global_query=True).
    q_bbox: tuple[float, float, float, float] | None
    if bbox is None:
        q_bbox = None
    else:
        if not isinstance(bbox, tuple):
            try:
                bbox = tuple(bbox)  # type: ignore[arg-type]
            except TypeError as exc:
                raise CHIRPSInputError(
                    f"bbox must be a 4-tuple or list; got {type(bbox).__name__}"
                ) from exc
        _validate_bbox(bbox)  # type: ignore[arg-type]
        q_bbox = _round_bbox_to_6dp(bbox)  # type: ignore[arg-type]

    # Canonical date token for the cache key (monthly keys on YYYY-MM only).
    if period == "monthly":
        date_token = f"{d.year:04d}-{d.month:02d}"
    else:
        date_token = f"{d.year:04d}-{d.month:02d}-{d.day:02d}"

    params = {
        "period": period,
        "date": date_token,
        "bbox": list(q_bbox) if q_bbox is not None else "GLOBAL",
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_chirps_bytes(d, period, q_bbox),
    )
    assert result.uri is not None, (
        "fetch_chirps_precipitation is cacheable; uri must be set by read_through"
    )

    if q_bbox is None:
        bbox_tag = "GLOBAL"
        layer_bbox: tuple[float, float, float, float] | None = _CHIRPS_BBOX
    else:
        bbox_tag = f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        layer_bbox = q_bbox

    unit_label = "mm/month" if period == "monthly" else "mm/day"

    return LayerURI(
        layer_id=f"chirps-{period}-{date_token}-{bbox_tag}",
        name=f"CHIRPS {period} precipitation ({unit_label}; {date_token})",
        layer_type="raster",
        uri=result.uri,
        style_preset="precip_mm",
        role="primary",
        units="mm",
        bbox=layer_bbox,
    )
