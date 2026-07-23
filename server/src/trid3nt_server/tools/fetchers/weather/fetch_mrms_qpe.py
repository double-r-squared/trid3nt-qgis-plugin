"""``fetch_mrms_qpe`` atomic tool — NOAA MRMS QPE precipitation fetcher (job-0103 + sprint-13 job-0226).
"""

from __future__ import annotations

import gzip
import io
import logging
import math
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Literal, Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_mrms_qpe",
    "estimate_payload_mb",
    "_normalize_accumulation",  # exported for tests
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.weather.fetch_mrms_qpe")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class MRMSQPEError(RuntimeError):
    """Base class for fetch_mrms_qpe failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the agent
    surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "MRMS_QPE_ERROR"
    retryable: bool = True


class MRMSQPEInputError(MRMSQPEError):
    """Bad inputs (unknown accumulation, malformed bbox, malformed valid_time)."""

    error_code = "MRMS_QPE_INPUT_ERROR"
    retryable = False


class MRMSQPEUpstreamError(MRMSQPEError):
    """NOAA MRMS S3 download or grib2 parsing failed."""

    error_code = "MRMS_QPE_UPSTREAM_ERROR"
    retryable = True


class MRMSQPENotAvailableError(MRMSQPEError):
    """Requested valid_time has no published QPE file (gap or future timestamp)."""

    error_code = "MRMS_QPE_NOT_AVAILABLE"
    retryable = False


class MRMSQPEEmptyError(MRMSQPEError):
    """Bbox clip produced an empty raster (no pixels intersected the requested area)."""

    error_code = "MRMS_QPE_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: NOAA MRMS public S3 bucket (open access, no auth).
_S3_BASE = "https://noaa-mrms-pds.s3.amazonaws.com"

#: Supported accumulation products. NOTE: 12H is included even though the
#: kickoff lists 01H/03H/06H/24H/48H/72H — the bucket exposes 12H too and it
#: is a common SFINCS reference window; surfaced as a docstring note rather
#: than expanding scope silently.
_VALID_ACCUMULATIONS: frozenset[str] = frozenset(
    {"01H", "03H", "06H", "12H", "24H", "48H", "72H"}
)

#: sprint-13 job-0226: lowercase alias map from the user-facing accumulation
#: values (``"1h"``, ``"6h"``, ``"24h"``, ``"72h"``) to the S3 canonical
#: product-key tokens. The LLM-facing docstring advertises the lowercase form
#: as the preferred short-hand; the normalizer accepts both.
_ACCUM_ALIAS_MAP: dict[str, str] = {
    "1h": "01H",
    "3h": "03H",
    "6h": "06H",
    "12h": "12H",
    "24h": "24H",
    "48h": "48H",
    "72h": "72H",
    # Also accept the full uppercase form unchanged (idempotent).
    "01H": "01H",
    "03H": "03H",
    "06H": "06H",
    "12H": "12H",
    "24H": "24H",
    "48H": "48H",
    "72H": "72H",
}


def _normalize_accumulation(accumulation: str) -> str:
    """Normalize a user-supplied accumulation string to the canonical S3 token.

    Accepts both the sprint-13 lowercase short-hand (``"1h"``, ``"6h"``,
    ``"24h"``, ``"72h"``) and the original uppercase form (``"01H"`` etc.).
    Raises ``MRMSQPEInputError`` for unknown values.
    """
    canonical = _ACCUM_ALIAS_MAP.get(accumulation)
    if canonical is None:
        raise MRMSQPEInputError(
            f"unknown accumulation={accumulation!r}; accepted values: "
            f"1h, 6h, 24h, 72h (and 3h, 12h, 48h); "
            f"uppercase aliases 01H, 06H, 24H, 72H are also accepted"
        )
    return canonical

#: We default to the Pass2 (gauge-corrected, delayed ~2 h) product because
#: the SFINCS Harvey reference (GMD 2025) uses gauge-corrected forcing. Pass1
#: is real-time radar-only. Surfaced as OQ-0103-MRMS-PASS-CHOICE.
_QPE_PASS = "Pass2"

#: CONUS bounding box (EPSG:4326) — the native MRMS QPE grid extent.
_CONUS_BBOX: tuple[float, float, float, float] = (-130.0, 20.0, -60.0, 55.0)

#: GeoTIFF nodata sentinel. MRMS publishes -3.0 (no-precip) and -1.0 (masked
#: / no-coverage); we collapse both to a single GeoTIFF nodata so consumers
#: can mask uniformly. -9999.0 is the GDAL convention for floating-point
#: nodata and is well outside any plausible precipitation value (mm).
_NODATA = -9999.0

#: User-Agent per AWS Open Data + NOAA usage guidelines.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

#: HTTP timeouts (seconds).
_LIST_TIMEOUT = 30.0
_DOWNLOAD_TIMEOUT = 180.0


# ---------------------------------------------------------------------------
# Metadata.
# ---------------------------------------------------------------------------

# Build AtomicToolMetadata DEFENSIVELY against the parallel
# job-0114-schema sibling that adds ``supports_global_query``. If the schema
# job lands first we want this tool to carry the field; if it doesn't, we
# fall back to a kwarg-free construction so registration still succeeds.
# This keeps Wave 1.5 cleanly parallel — see OQ-0103-METADATA-FIELD.

def _build_metadata() -> AtomicToolMetadata:
    common = dict(
        name="fetch_mrms_qpe",
        ttl_class="dynamic-1h",
        source_class="mrms_qpe",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=True)  # type: ignore[call-arg]
    except Exception:  # pydantic ValidationError when field absent (extra="forbid")
        logger.debug(
            "AtomicToolMetadata does not (yet) support supports_global_query; "
            "registering fetch_mrms_qpe without it"
        )
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Payload-MB estimator (Wave 1.5 chat-warning system, sprint-13 job-0226).
# ---------------------------------------------------------------------------


def estimate_payload_mb(
    bbox: tuple[float, float, float, float] | None = None,
    accumulation: str | None = None,
    **_kw: Any,
) -> float:
    """Estimate output GeoTIFF size in MB for a given call (Wave 1.5 surface).

    MRMS QPE at 0.01° (~1 km) CONUS resolution: the full CONUS grid is
    3500 × 7000 pixels ≈ 49M pixels × 4 bytes = ~196 MB uncompressed.
    With DEFLATE compression (predictor 3) on typical precip data the
    compression ratio is ~5–8×, so full CONUS is ~25–40 MB on disk.

    For a clipped bbox we scale linearly by the fractional area vs CONUS.
    CONUS spans 70° × 35° = 2450 sq-deg; each sq-deg → ~0.015 MB of
    compressed GeoTIFF. A 3° × 3° Florida-style bbox is ~9 sq-deg → ~0.13 MB.

    Used by the tool-payload-warning envelope. Wrong answers are cheap (a
    chat warning instead of a hard block); we err on the high side so the
    user sees the warning rather than a surprise large download.
    """
    _CONUS_SQ_DEG = 70.0 * 35.0  # ~2450 sq-deg
    _MB_PER_SQ_DEG = 196.0 / _CONUS_SQ_DEG / 6.0  # ~196 MB uncompressed / 6× ratio / sq-deg

    if bbox is None:
        sq_deg = _CONUS_SQ_DEG
    else:
        try:
            west, south, east, north = bbox
            sq_deg = max(0.001, (east - west)) * max(0.001, (north - south))
        except (TypeError, ValueError):
            sq_deg = 1.0

    return _MB_PER_SQ_DEG * sq_deg


# ---------------------------------------------------------------------------
# bbox + valid_time helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    """Raise ``MRMSQPEInputError`` if the bbox is degenerate or out of range."""
    if len(bbox) != 4:
        raise MRMSQPEInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    min_lon, min_lat, max_lon, max_lat = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise MRMSQPEInputError(f"bbox contains non-finite values: {bbox!r}")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise MRMSQPEInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise MRMSQPEInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise MRMSQPEInputError(
            f"bbox is degenerate (min must be < max on both axes): {bbox!r}"
        )


def _round_bbox_to_6dp(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    return tuple(round(v, 6) for v in bbox)  # type: ignore[return-value]


def _parse_valid_time(valid_time: str | None) -> datetime | None:
    """Parse the ``valid_time`` ISO-8601 UTC string. None means "latest available"."""
    if valid_time is None:
        return None
    if not isinstance(valid_time, str):
        raise MRMSQPEInputError(f"valid_time must be a string; got {type(valid_time).__name__}")
    s = valid_time.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise MRMSQPEInputError(
            f"valid_time={valid_time!r} is not a parseable ISO-8601 string"
        ) from exc
    if dt.tzinfo is None:
        # Caller passed a naive timestamp — assume UTC per the docstring
        # contract rather than guessing local time.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# S3 listing + file resolution.
# ---------------------------------------------------------------------------


def _http_get(url: str, timeout: float) -> bytes:
    """Plain HTTP GET against the public bucket. Raises ``MRMSQPEUpstreamError`` on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise MRMSQPEUpstreamError(
            f"MRMS S3 returned HTTP {exc.code} for {url}"
        ) from exc
    except urllib.error.URLError as exc:
        raise MRMSQPEUpstreamError(
            f"MRMS S3 network error for {url}: {exc.reason}"
        ) from exc
    except TimeoutError as exc:  # noqa: PERF203 — explicit timeout surface
        raise MRMSQPEUpstreamError(
            f"MRMS S3 timed out after {timeout}s for {url}"
        ) from exc


def _list_keys_under_prefix(prefix: str, max_keys: int = 1000) -> list[str]:
    """List S3 keys under ``prefix`` (one bucket-list call, up to ``max_keys``)."""
    url = (
        f"{_S3_BASE}/?list-type=2"
        f"&prefix={urllib.parse.quote(prefix)}"
        f"&max-keys={max_keys}"
    )
    body = _http_get(url, timeout=_LIST_TIMEOUT).decode("utf-8", errors="replace")
    return re.findall(r"<Key>([^<]+)</Key>", body)


def _list_date_prefixes(accumulation: str) -> list[str]:
    """List the YYYYMMDD/ subdirectories under the QPE accumulation prefix.

    Each call returns up to 1000 prefixes. We paginate until the bucket says
    ``IsTruncated=false``. Returned list is sorted ascending (S3 returns keys
    in lexicographic order, which for YYYYMMDD is chronological).
    """
    base_prefix = f"CONUS/MultiSensor_QPE_{accumulation}_{_QPE_PASS}_00.00/"
    all_dates: list[str] = []
    token: str | None = None
    while True:
        url = (
            f"{_S3_BASE}/?list-type=2"
            f"&prefix={urllib.parse.quote(base_prefix)}"
            f"&delimiter=/&max-keys=1000"
        )
        if token:
            url += f"&continuation-token={urllib.parse.quote(token)}"
        body = _http_get(url, timeout=_LIST_TIMEOUT).decode("utf-8", errors="replace")
        prefixes = re.findall(
            rf"<Prefix>{re.escape(base_prefix)}([^<]+)</Prefix>",
            body,
        )
        all_dates.extend(prefixes)
        truncated = "<IsTruncated>true</IsTruncated>" in body
        if not truncated:
            break
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", body)
        if not m:
            break
        token = m.group(1)
    # Strip trailing slashes for caller convenience.
    return [p.rstrip("/") for p in all_dates]


def _resolve_qpe_key(
    accumulation: str, valid_time: datetime | None
) -> tuple[str, datetime]:
    """Resolve the S3 key + actual timestamp for the requested ``valid_time``.

    Strategy:
    - If ``valid_time`` is None, return the most-recent published file.
    - Otherwise, round to the nearest top-of-hour, then probe for the
      ``...{accumulation}_Pass2_00.00_YYYYMMDD-HHMMSS.grib2.gz`` key with HH
      from the requested hour. If that exact hour is not published, walk
      backwards up to 24 hours to find the nearest earlier file (Pass2 is
      delayed ~2 h so the most-recent hour for a "now" query may not yet exist).
    """
    if valid_time is None:
        # Latest published: find the latest YYYYMMDD/, then the latest file.
        dates = _list_date_prefixes(accumulation)
        if not dates:
            raise MRMSQPEUpstreamError(
                f"MRMS QPE {accumulation} {_QPE_PASS} bucket has no date prefixes"
            )
        latest_date = dates[-1]
        # List files for that date
        date_prefix = (
            f"CONUS/MultiSensor_QPE_{accumulation}_{_QPE_PASS}_00.00/{latest_date}/"
        )
        keys = _list_keys_under_prefix(date_prefix)
        # Filter to gz files (defensive against any sidecar files)
        keys = [k for k in keys if k.endswith(".grib2.gz")]
        if not keys:
            raise MRMSQPEUpstreamError(
                f"no MRMS QPE files under {date_prefix}"
            )
        latest_key = sorted(keys)[-1]
        resolved_dt = _parse_key_timestamp(latest_key)
        return latest_key, resolved_dt

    # Targeted time: probe for exact hour, walk backwards up to 24h.
    # Round down to top of hour first.
    target = valid_time.replace(minute=0, second=0, microsecond=0)
    for hours_back in range(0, 25):
        candidate = target.replace(hour=(target.hour - hours_back) % 24)
        # Adjust the date if we wrapped past midnight
        if hours_back > target.hour:
            from datetime import timedelta
            candidate = target - timedelta(hours=hours_back)
        yyyymmdd = candidate.strftime("%Y%m%d")
        hhmmss = candidate.strftime("%H0000")
        key = (
            f"CONUS/MultiSensor_QPE_{accumulation}_{_QPE_PASS}_00.00/"
            f"{yyyymmdd}/MRMS_MultiSensor_QPE_{accumulation}_{_QPE_PASS}_00.00_"
            f"{yyyymmdd}-{hhmmss}.grib2.gz"
        )
        # HEAD check via single-object list
        url = f"{_S3_BASE}/?list-type=2&prefix={urllib.parse.quote(key)}&max-keys=1"
        body = _http_get(url, timeout=_LIST_TIMEOUT).decode("utf-8", errors="replace")
        if f"<Key>{key}</Key>" in body:
            return key, candidate
    raise MRMSQPENotAvailableError(
        f"no MRMS QPE {accumulation} {_QPE_PASS} file found within 24h before "
        f"valid_time={valid_time.isoformat()}; the bucket may have a gap or the "
        f"timestamp may be too recent (Pass2 is delayed ~2 h)"
    )


def _parse_key_timestamp(key: str) -> datetime:
    """Extract the UTC datetime from a QPE key like
    ``CONUS/.../20260608/MRMS_MultiSensor_QPE_24H_Pass2_00.00_20260608-110000.grib2.gz``.
    """
    m = re.search(r"_(\d{8})-(\d{6})\.grib2\.gz$", key)
    if not m:
        raise MRMSQPEUpstreamError(f"could not parse timestamp from key {key!r}")
    yyyymmdd, hhmmss = m.group(1), m.group(2)
    return datetime(
        int(yyyymmdd[0:4]),
        int(yyyymmdd[4:6]),
        int(yyyymmdd[6:8]),
        int(hhmmss[0:2]),
        int(hhmmss[2:4]),
        int(hhmmss[4:6]),
        tzinfo=timezone.utc,
    )


# ---------------------------------------------------------------------------
# Grib2 → GeoTIFF conversion (the core data pipeline).
# ---------------------------------------------------------------------------


def _grib2_to_geotiff(
    grib_bytes: bytes,
    bbox: tuple[float, float, float, float] | None,
    valid_time: datetime,
) -> bytes:
    """Decode a grib2 byte blob, optionally clip to bbox, return GeoTIFF bytes.

    - Reprojects/aligns CRS to EPSG:4326 (MRMS native is a sphere-based geographic
      CRS very close to but not bit-exact with EPSG:4326).
    - Collapses MRMS sentinel values (-3 no-precip, -1 missing) to a single
      GeoTIFF nodata (-9999) so consumers can mask uniformly. Positive values
      are precipitation in mm.
    - Stores the valid_time in the GeoTIFF's ``TIFFTAG_DATETIME`` tag for
      provenance, plus a ``units=mm`` band description.
    """
    # Lazy imports so a stub-network test environment doesn't pay the cost.
    try:
        import numpy as np
        import rasterio
        from rasterio.io import MemoryFile
        from rasterio.windows import from_bounds
        from rasterio.warp import calculate_default_transform, reproject, Resampling
        from rasterio.crs import CRS
    except ImportError as exc:
        raise MRMSQPEUpstreamError(
            f"rasterio / numpy not available: {exc}"
        ) from exc

    # Write grib bytes to a temp file (rasterio's MemoryFile cannot host GRIB
    # since the GRIB driver requires a real path for tabular indexing).
    tmp_grib: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".grib2", delete=False) as gf:
            gf.write(grib_bytes)
            tmp_grib = gf.name

        with rasterio.open(tmp_grib) as src:
            src_crs = src.crs
            src_transform = src.transform
            src_height, src_width = src.shape
            arr = src.read(1).astype("float32")

        # MRMS sentinels → nodata
        # -3.0 = no precip (cell observed, value confirmed zero/below detection)
        # -1.0 = missing / no radar coverage (cell unobserved)
        # We deliberately collapse both into nodata so a downstream consumer
        # masking nodata gets only valid precipitation values.
        sentinel_mask = (arr == -3.0) | (arr == -1.0) | (arr < -0.5)
        arr = np.where(sentinel_mask, _NODATA, arr).astype("float32")

        # Build a target raster in EPSG:4326. The MRMS native sphere CRS
        # differs from WGS84 by <1e-3° at most; we still reproject explicitly
        # for CRS hygiene per the engine.md "CRS hygiene end-to-end" rule.
        dst_crs = CRS.from_epsg(4326)

        # Clip to bbox BEFORE reprojection if requested — works on the source
        # grid (cheaper) and preserves data integrity since the source CRS is
        # also geographic.
        if bbox is not None:
            try:
                window = from_bounds(
                    bbox[0], bbox[1], bbox[2], bbox[3], transform=src_transform
                )
                # Round window to ints + clip to raster extent
                row_off = max(0, int(math.floor(window.row_off)))
                col_off = max(0, int(math.floor(window.col_off)))
                row_end = min(src_height, int(math.ceil(window.row_off + window.height)))
                col_end = min(src_width, int(math.ceil(window.col_off + window.width)))
                if row_end <= row_off or col_end <= col_off:
                    raise MRMSQPEEmptyError(
                        f"bbox={bbox} does not intersect the MRMS CONUS grid "
                        f"({_CONUS_BBOX})"
                    )
                arr = arr[row_off:row_end, col_off:col_end]
                # Derive the clipped transform from the source transform + offsets
                src_transform = rasterio.transform.Affine(
                    src_transform.a,
                    src_transform.b,
                    src_transform.c + col_off * src_transform.a,
                    src_transform.d,
                    src_transform.e,
                    src_transform.f + row_off * src_transform.e,
                )
                src_height, src_width = arr.shape
            except MRMSQPEEmptyError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise MRMSQPEUpstreamError(
                    f"bbox clip failed: {exc}"
                ) from exc

        # Reproject source → EPSG:4326 (in-place: same resolution).
        # We use rasterio.warp.reproject to a NEW array sized to the same
        # bbox in 4326. For MRMS the source IS already a geographic 0.01° grid
        # so the reprojection is effectively a CRS-tag flip — but we go through
        # the warp machinery so any future source-CRS change is handled.
        if src_crs is not None and src_crs != dst_crs:
            dst_transform, dst_width, dst_height = calculate_default_transform(
                src_crs,
                dst_crs,
                src_width,
                src_height,
                left=src_transform.c,
                bottom=src_transform.f + src_height * src_transform.e,
                right=src_transform.c + src_width * src_transform.a,
                top=src_transform.f,
            )
            dst_arr = np.full(
                (dst_height, dst_width), _NODATA, dtype="float32"
            )
            reproject(
                source=arr,
                destination=dst_arr,
                src_transform=src_transform,
                src_crs=src_crs,
                dst_transform=dst_transform,
                dst_crs=dst_crs,
                resampling=Resampling.nearest,
                src_nodata=_NODATA,
                dst_nodata=_NODATA,
            )
            arr = dst_arr
            out_transform = dst_transform
            out_height, out_width = dst_height, dst_width
        else:
            # Tag the CRS as 4326 since the source IS a geographic grid.
            out_transform = src_transform
            out_height, out_width = src_height, src_width

        # Write GeoTIFF (COG-style profile, but plain TIF is fine for the cache
        # — publish_layer or another tool can convert to COG later if needed).
        profile = {
            "driver": "GTiff",
            "height": out_height,
            "width": out_width,
            "count": 1,
            "dtype": "float32",
            "crs": dst_crs,
            "transform": out_transform,
            "nodata": _NODATA,
            "compress": "deflate",
            "tiled": True,
            "blockxsize": 256,
            "blockysize": 256,
            "predictor": 3,  # float predictor
        }
        with MemoryFile() as memf:
            with memf.open(**profile) as dst:
                dst.write(arr, 1)
                dst.set_band_description(1, "precipitation_mm")
                dst.update_tags(
                    units="mm",
                    valid_time=valid_time.isoformat(),
                    source="NOAA MRMS MultiSensor QPE Pass2",
                    nodata_meaning="-3 (no-precip) and -1 (missing) collapsed to nodata",
                )
                dst.update_tags(1, units="mm", long_name="accumulated precipitation")
            return memf.read()

    finally:
        if tmp_grib:
            try:
                os.unlink(tmp_grib)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Fetch function — bytes callable for read_through.
# ---------------------------------------------------------------------------


def _fetch_mrms_qpe_bytes(
    accumulation: str,
    bbox: tuple[float, float, float, float] | None,
    valid_time_dt: datetime | None,
) -> bytes:
    """Download a MRMS QPE grib2.gz, decompress, convert to GeoTIFF bytes."""
    key, resolved_dt = _resolve_qpe_key(accumulation, valid_time_dt)
    url = f"{_S3_BASE}/{key}"
    logger.info(
        "fetch_mrms_qpe: downloading %s (resolved valid_time=%s)",
        url,
        resolved_dt.isoformat(),
    )
    gz_bytes = _http_get(url, timeout=_DOWNLOAD_TIMEOUT)
    if not gz_bytes:
        raise MRMSQPEUpstreamError(f"empty response from {url}")
    try:
        grib_bytes = gzip.decompress(gz_bytes)
    except (OSError, gzip.BadGzipFile) as exc:
        raise MRMSQPEUpstreamError(
            f"gzip decompression failed for {url}: {exc}"
        ) from exc
    logger.info(
        "fetch_mrms_qpe: gz=%d bytes, grib=%d bytes", len(gz_bytes), len(grib_bytes)
    )
    return _grib2_to_geotiff(grib_bytes, bbox, resolved_dt)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: readOnlyHint=True (read-only; no state mutation),
    # openWorldHint=True (calls external public API endpoint),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def fetch_mrms_qpe(
    bbox: tuple[float, float, float, float] | None = None,
    accumulation: str = "24h",
    valid_time: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """Fetch NOAA MRMS accumulated QPE (gauge-corrected precipitation) as a COG.

    **What it does:** Downloads a NOAA MRMS MultiSensor QPE Pass2 grib2.gz from
    the public ``noaa-mrms-pds`` S3 bucket (anonymous access), decompresses and
    decodes it with rasterio, collapses MRMS sentinels (−3 no-precip, −1 missing)
    to GeoTIFF nodata (−9999), optionally clips to bbox, reprojects to EPSG:4326,
    and writes a deflate-compressed Cloud-Optimized GeoTIFF. Pass2 is
    gauge-corrected (~2 h delayed) at CONUS 0.01° (~1 km) resolution. Tier-1
    free, no API key required. Picks the most-recent available timestamp at call
    time when ``valid_time`` is omitted; records the resolved timestamp in the
    returned ``LayerURI`` metadata.

    **When to use:**

    - SFINCS pluvial-flood forcing precipitation for CONUS events — MRMS QPE
      Pass2 is the Harvey/Houston SFINCS reference forcing (GMD 2025). Typical
      call for Case 3 (Idaho NWS flood warning):
      ``fetch_mrms_qpe(bbox=warning_polygon_bbox, accumulation="24h")``.
    - Rainfall-runoff analysis and storm characterization ("how much rain fell
      in 24 hours over the watershed?").
    - Near-real-time precipitation context: omit ``valid_time`` to fetch the
      most recently published file (~2 h behind current).
    - Feeding ``model_flood_scenario(forcing_raster_uri=mrms_uri)`` as the real-
      precip forcing branch for Case 3 (sprint-13 job-0225/0229 composers).

    **When NOT to use:**

    - Live radar reflectivity — use ``fetch_nexrad_reflectivity`` (Iowa Mesonet
      WMS; dBZ products n0r/n0q/vil).
    - Historical return-period precipitation (design storms) — use
      ``lookup_precip_return_period`` (NOAA Atlas 14 PFDS).
    - Global precipitation outside CONUS — MRMS is CONUS-only; for global
      use ``fetch_era5_reanalysis`` (27 km, daily/hourly ERA5 precip).
    - Sub-hourly accumulations — MRMS publishes ``1h`` (01H) as the finest window.

    **Parameters:**

    - ``bbox``: ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326. Required for
      Case 3 usage; when ``None``, returns full CONUS grid (~3500×7000 px).
      Must intersect CONUS ``(-130, 20, -60, 55)`` or ``MRMSQPEEmptyError``
      is raised. ``supports_global_query=True`` is retained for the CONUS-wide
      path (``bbox=None``).
    - ``accumulation``: preferred values ``"1h"``, ``"6h"``, ``"24h"``
      (default), ``"72h"`` — the standard sprint-13 Case 3 window. Also accepts
      ``"3h"``, ``"12h"``, ``"48h"`` and the original uppercase S3 tokens
      (``"01H"``, ``"06H"``, ``"24H"``, ``"72H"``, etc.) for backward
      compatibility. Unknown values raise ``MRMSQPEInputError``.
    - ``valid_time``: ISO-8601 UTC string, e.g. ``"2017-08-27T12:00:00Z"``.
      When ``None``, fetches the most recent published file. When provided,
      resolves to the nearest-earlier published hour within a 24 h walkback
      (Pass2 is delayed ~2 h). Raises ``MRMSQPENotAvailableError`` if no
      file exists in that window.

    **Returns:**

    ``LayerURI`` pointing at
    ``s3://trid3nt-cache/cache/dynamic-1h/mrms_qpe/<key>.tif``.
    GeoTIFF: float32, EPSG:4326, deflate-compressed, tiled 256×256,
    nodata=−9999. Band 1 description ``"precipitation_mm"``; GeoTIFF tags
    ``units="mm"``, ``valid_time``, ``source="NOAA MRMS MultiSensor QPE Pass2"``.
    ``layer_type="raster"``, ``role="primary"``, ``units="mm"``.
    The ``name`` field embeds ``valid_time=<ISO-timestamp>`` (sprint-13
    provenance requirement) since LayerURI has no freeform metadata dict
    (schema extra="forbid").

    Raises: ``MRMSQPEInputError`` (bad params), ``MRMSQPEUpstreamError``
    (S3 / grib2 failure, retryable), ``MRMSQPENotAvailableError`` (no published
    file in walkback window), ``MRMSQPEEmptyError`` (bbox outside CONUS).

    **Cross-tool dependencies:**

    - Pair with: ``fetch_nexrad_reflectivity`` (live radar reflectivity overlay
      for the same storm event) and ``fetch_nws_alerts_conus`` (NWS watches/
      warnings) for a complete storm-situation display.
    - Consumed by: ``model_flood_scenario(forcing_raster_uri=...)`` as pluvial
      precipitation forcing (Case 3 composer); ``compute_zonal_statistics``
      for per-watershed accumulation queries.
    - Alternative for non-CONUS: ``fetch_era5_reanalysis`` (global, 27 km,
      needs Copernicus CDS key).

    Cache: ``ttl_class="dynamic-1h"``; key = SHA-256 of
    ``(bbox-6dp, accumulation-canonical, valid_time-or-LATEST, pass)``.
    Payload estimate: ``estimate_payload_mb(bbox, accumulation)`` (Wave 1.5).
    """
    # Normalize accumulation (accepts "24h", "24H", "01H", etc.) — sprint-13
    canonical_accumulation = _normalize_accumulation(accumulation)

    # Validate bbox (None means CONUS-wide per supports_global_query=True)
    q_bbox: tuple[float, float, float, float] | None
    if bbox is None:
        q_bbox = None
    else:
        # Accept lists too (LLM tool callers often produce lists).
        if not isinstance(bbox, tuple):
            try:
                bbox = tuple(bbox)  # type: ignore[arg-type]
            except TypeError as exc:
                raise MRMSQPEInputError(
                    f"bbox must be a 4-tuple or list; got {type(bbox).__name__}"
                ) from exc
        _validate_bbox(bbox)  # type: ignore[arg-type]
        q_bbox = _round_bbox_to_6dp(bbox)  # type: ignore[arg-type]

    # Parse valid_time
    valid_time_dt = _parse_valid_time(valid_time)

    # Build cache key params — use canonical (uppercase) accumulation in the key
    # so "24h" and "24H" map to the same cached entry.
    params = {
        "accumulation": canonical_accumulation,
        "bbox": list(q_bbox) if q_bbox is not None else "CONUS",
        # Key on the literal valid_time string (or "LATEST") so two callers
        # asking for the same hour get the same key. We deliberately do NOT
        # quantize the inbound timestamp before keying — the dynamic-1h
        # vintage already gives the cache its "this hour" semantic; pinned
        # timestamps deserve their own key.
        "valid_time": valid_time if valid_time is not None else "LATEST",
        "pass": _QPE_PASS,
    }

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_mrms_qpe_bytes(
            canonical_accumulation, q_bbox, valid_time_dt
        ),
    )
    assert result.uri is not None, (
        "fetch_mrms_qpe is cacheable; uri must be set by read_through"
    )

    # Build a stable layer_id including accumulation + bbox digest
    if q_bbox is None:
        bbox_tag = "CONUS"
        layer_bbox: tuple[float, float, float, float] | None = _CONUS_BBOX
    else:
        bbox_tag = f"{q_bbox[0]:.4f}-{q_bbox[1]:.4f}-{q_bbox[2]:.4f}-{q_bbox[3]:.4f}"
        layer_bbox = q_bbox
    vt_tag = valid_time if valid_time is not None else "latest"

    # sprint-13 job-0226: record the resolved provenance valid_time so
    # downstream composers (Case 3 NWS→MRMS→SFINCS) can narrate which
    # QPE timestamp was used in the forcing. LayerURI has no freeform
    # metadata dict (schema extra="forbid"), so we embed the timestamp
    # in the human-readable name field and in the layer_id suffix.
    provenance_vt = valid_time if valid_time is not None else "latest-available"

    return LayerURI(
        layer_id=f"mrms-qpe-{canonical_accumulation}-{bbox_tag}-{vt_tag}",
        name=(
            f"MRMS QPE {canonical_accumulation} (Pass2 gauge-corrected, mm; "
            f"valid_time={provenance_vt})"
        ),
        layer_type="raster",
        uri=result.uri,
        style_preset="precipitation_mm",
        role="primary",
        units="mm",
        bbox=layer_bbox,
    )
