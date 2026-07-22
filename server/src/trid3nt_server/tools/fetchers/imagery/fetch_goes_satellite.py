"""``fetch_goes_satellite`` atomic tool — GOES-16/17/18/19 satellite imagery (job-0104).

Wraps the NOAA Big-Data Program GOES-R series public S3 buckets to fetch the
most-recent ABI L2 Cloud and Moisture Imagery (Multi-Channel) product
covering a requested bbox, then clips, reprojects to EPSG:4326, and writes a
COG to the FR-DC cache (``dynamic-1h``, ``source_class="goes_satellite"``).

Source S3 buckets (public, no auth, unauthenticated HTTPS listing):

    s3://noaa-goes19/  (GOES-19 / GOES-East -- operational at 75.2W since
                        2025-04-07; current default for fresh CONUS frames)
    s3://noaa-goes18/  (GOES-18 / GOES-West -- operational west of ~137W)
    s3://noaa-goes17/  (GOES-17 -- historical west; replaced by GOES-18)
    s3://noaa-goes16/  (GOES-16 -- historical GOES-East, decommissioned
                        2025-04-07; bucket no longer gains fresh frames)

The bucket token glues the digits to "goes" with NO hyphen (noaa-goes18, not
noaa-goes-18). All human/LLM satellite spellings ("GOES-18", "goes18", "G18",
"GOES East", a bare "18") are normalized to the canonical token by
``_normalize_satellite`` before any S3 path is built, so a malformed identifier
is canonicalized or rejected LOUD (typed error) -- never a silent 404.

Product: ``ABI-L2-MCMIPC`` (Multi-Channel Cloud and Moisture Imagery, CONUS
sector). One netCDF carries all 16 ABI channels (``CMI_C01`` … ``CMI_C16``)
on the CONUS fixed grid (~5,000 km x 3,000 km, 2 km nominal at sub-satellite
point). One frame is emitted every 5 minutes.

Bands supported:

    "visible"     — ABI band 2, 0.64 µm, reflectance (dimensionless 0..1)
    "ir_window"   — ABI band 13, 10.35 µm, brightness temperature (K)
    "water_vapor" — ABI band 8, 6.19 µm, brightness temperature (K)

Strategy:

1. ``_list_recent_keys(satellite)`` — list the latest hour of MCMIPC keys in
   the S3 bucket using the unauthenticated ``?list-type=2`` REST API.
2. ``_pick_most_recent_key(keys)`` — pick the key whose ``s<TIMESTAMP>``
   start-time substring is the largest (= most recent observation).
3. Download the netCDF to a temp file.
4. Open with rasterio's netCDF subdataset syntax ``NETCDF:"path":CMI_C##``
   and inherit the ABI fixed-grid geostationary CRS (``goes_imager_projection``).
5. Reproject (warp) the requested band to EPSG:4326 over the requested bbox.
6. Write as a COG (``rasterio`` driver ``COG``).

Cache key: SHA-256 of ``(bbox-rounded-to-6dp, band, satellite,
valid_time_rounded_15min)``. The TTL-bucket vintage adds the top-of-hour
boundary via ``ttl_bucket_vintage`` automatically; we still factor the
15-minute round of ``valid_time`` into the params so a fresh observation
inside the hour triggers a fresh fetch.

FR-TA-2 / FR-AS-3 docstring discipline applies.

Geographic-correctness check (job-0086 lesson, codified):
The live test asserts the output COG raster covers a sub-rectangle that lies
inside the requested bbox (CRS-tagged EPSG:4326), AND that the mean
reflectance / brightness-temperature falls inside a physically-plausible
range. A reprojection sign-flip or axis-swap would put pixels outside the
bbox or push reflectance outside [0, 1.5].

OQ-0104-CONTRACT-SUPPORTS-GLOBAL-QUERY: the kickoff asks for
``supports_global_query=False`` on the AtomicToolMetadata, but the
``contracts`` model has not yet been amended to carry that field.
The metadata constructed here uses the existing 4-field shape. Surfaced as
an OQ for the upcoming schema/Appendix D amendment that adds the field.
"""

from __future__ import annotations

import logging
import math
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Literal, Any

import requests

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through

__all__ = [
    "fetch_goes_satellite",
    "GOESError",
    "GOESBboxRequiredError",
    "GOESInputError",
    "GOESUpstreamError",
    "GOESEmptyError",
    "_pick_most_recent_key",
    "_list_recent_keys",
    "_band_to_variable",
    "_band_to_units",
    # Shared satellite-identifier normalizer + maps. Sibling GOES/VIIRS fetchers
    # (fetch_glm_lightning, fetch_goes_archive_animation, fetch_goes_active_fire,
    # fetch_goes_animation) carry the same "goes18 vs goes-18" hazard and MAY
    # import _normalize_satellite to share this single canonicalization seam;
    # the orchestrator reconciles cross-file reuse.
    "_normalize_satellite",
    "_SATELLITE_BUCKETS",
    "_SATELLITE_FILENAME_CODE",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.imagery.fetch_goes_satellite")

# ---------------------------------------------------------------------------
# Error types (NFR-R-1 typed-error surface).
# ---------------------------------------------------------------------------


class GOESError(RuntimeError):
    """Base class for fetch_goes_satellite failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the
    agent surface. ``retryable`` guides FR-AS-11 retry logic.
    """

    error_code: str = "GOES_SATELLITE_ERROR"
    retryable: bool = True


class GOESBboxRequiredError(GOESError):
    """``bbox`` was None or otherwise missing.

    Required because the full ABI fixed-grid CONUS rasters are ~50MB
    uncompressed per band; allowing ``bbox=None`` would make the tool a
    foot-gun for both the agent (paying egress + cache-write cost) and
    the user (unintended global queries).
    """

    error_code = "BBOX_REQUIRED"
    retryable = False


class GOESInputError(GOESError):
    """Invalid input (unknown band, unknown satellite, malformed bbox)."""

    error_code = "GOES_INPUT_INVALID"
    retryable = False


class GOESUpstreamError(GOESError):
    """S3 listing or netCDF download/parse failed."""

    error_code = "GOES_UPSTREAM_ERROR"
    retryable = True


class GOESEmptyError(GOESError):
    """The bbox falls entirely outside the CONUS sector or yields zero pixels."""

    error_code = "GOES_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

# Supported satellites and their public S3 bucket names.
#
# NOTE: the AWS Open Data bucket token GLUES the digits to "goes" with NO
# hyphen (noaa-goes18, NOT noaa-goes-18 -- the latter 404s). The dict keys are
# the canonical lowercase-hyphenated internal token ("goes-18"); _normalize_satellite
# below maps every human/LLM spelling (GOES-18, goes18, G18, "GOES West", ...)
# onto these keys so a malformed identifier is normalized or rejected LOUD,
# never silently turned into a bad bucket path.
#
# East/West -> bird mapping (current as of the 2025-04-07 NOAA GOES-East swap,
# originally scheduled 2025-04-04): GOES-19 is operational GOES-East at 75.2W
# (Gulf/Atlantic), GOES-18 is operational GOES-West (Pacific). GOES-16 was the
# prior East and GOES-17 the prior West; both are historical/standby now and
# their buckets stop gaining fresh frames -- kept here ONLY for archival lookups.
_SATELLITE_BUCKETS: dict[str, str] = {
    "goes-16": "noaa-goes16",  # GOES-East (historical, pre-2025-04-07)
    "goes-17": "noaa-goes17",  # GOES-West (historical)
    "goes-18": "noaa-goes18",  # GOES-West (current operational)
    "goes-19": "noaa-goes19",  # GOES-East (current operational)
}

# Internal canonical token -> the satellite code embedded in MCMIPC FILENAMES
# (e.g. OR_ABI-L2-MCMIPC-M6_G18_s2025...nc). Glued "G" + 2-digit number, no
# hyphen -- the same glued-vs-hyphenated hazard as the bucket token. Exposed so
# callers that filter keys by bird never hand-build "G-18".
_SATELLITE_FILENAME_CODE: dict[str, str] = {
    "goes-16": "G16",
    "goes-17": "G17",
    "goes-18": "G18",
    "goes-19": "G19",
}

# Current GOES-East / GOES-West birds (see _SATELLITE_BUCKETS note). Used by the
# directional aliases in _normalize_satellite. Update both halves together if a
# future swap re-points East/West.
_GOES_EAST = "goes-19"  # operational East since 2025-04-07 (was goes-16)
_GOES_WEST = "goes-18"  # operational West (was goes-17)


def _normalize_satellite(satellite: str) -> str:
    """Map any accepted human/LLM satellite spelling to the canonical token.

    The canonical token is the lowercase-hyphenated form ("goes-19") that keys
    ``_SATELLITE_BUCKETS`` / ``_SATELLITE_FILENAME_CODE``. This is the fix for
    the "goes18 vs goes-18" identifier-format bug class: the AWS bucket spelling
    glues the digits ("noaa-goes18") while humans and LLM prompts write a zoo of
    forms -- "GOES-18", "GOES 18", "goes18", "G18", "GOES-East", "west", or a
    bare "18". All of those normalize here, case- and hyphen-insensitive, BEFORE
    the allow-list check, so a recognized bird is accepted and an unrecognized
    token fails LOUD (typed ``GOESInputError`` listing the accepted forms) --
    never a silent 404, empty fetch, or hallucinated success.

    Accepted forms (any case, hyphen/space/underscore-insensitive):
      - canonical: ``goes-16`` .. ``goes-19``
      - glued / spaced: ``goes18``, ``GOES 18``, ``GOES_18``
      - filename code: ``G18`` .. ``G19``
      - bare number: ``18``, ``19`` (assumed GOES-NN)
      - directional: ``goes-east``/``east`` -> current East (goes-19),
        ``goes-west``/``west`` -> current West (goes-18)

    Raises:
        ``GOESInputError``: if ``satellite`` is not a recognized form.
    """
    if not isinstance(satellite, str):
        raise GOESInputError(
            f"satellite must be a string; got {type(satellite).__name__}; "
            f"accepted e.g. {sorted(_SATELLITE_BUCKETS)} or 'GOES-18'/'GOES East'"
        )

    # Collapse case + strip hyphens/spaces/underscores to a bare alnum token so
    # "GOES-18", "goes 18", "G18", "goes18" all reduce to the same compare key.
    raw = satellite.strip().lower()
    compact = re.sub(r"[\s_\-]+", "", raw)

    # Directional aliases first (goeseast / east / goeswest / west).
    if compact in ("goeseast", "east"):
        return _GOES_EAST
    if compact in ("goeswest", "west"):
        return _GOES_WEST

    # "goesNN" (glued or originally hyphenated), "gNN", or bare "NN".
    m = re.fullmatch(r"(?:goes|g)?(\d{2})", compact)
    if m is not None:
        candidate = f"goes-{m.group(1)}"
        if candidate in _SATELLITE_BUCKETS:
            return candidate

    raise GOESInputError(
        f"unknown satellite={satellite!r}; accepted forms: "
        f"{sorted(_SATELLITE_BUCKETS)} (also 'GOES-18'/'goes18'/'G18'/'18', "
        f"or directional 'GOES-East'/'GOES-West' -> {_GOES_EAST}/{_GOES_WEST})"
    )

# Product prefix used in S3 keys (Multi-Channel CMIP, CONUS sector).
# Carries all 16 ABI channels in one netCDF file (~50 MB).
_PRODUCT_PREFIX = "ABI-L2-MCMIPC"

# Band-name → CMI variable mapping in the MCMIPC netCDF.
# CMI = "Cloud and Moisture Imagery" — one variable per ABI channel.
_BAND_TO_VARIABLE: dict[str, str] = {
    "visible": "CMI_C02",      # ABI band 2: 0.64 µm "Red" — reflectance
    "ir_window": "CMI_C13",    # ABI band 13: 10.35 µm "Clean IR longwave" — brightness temperature
    "water_vapor": "CMI_C08",  # ABI band 8: 6.19 µm "Upper-Level WV" — brightness temperature
}

# Band-name → physical-units string written into LayerURI.units.
_BAND_TO_UNITS: dict[str, str] = {
    "visible": "reflectance",   # 0..1.5 (clamped reflectance, dimensionless)
    "ir_window": "K",           # brightness temperature, kelvin
    "water_vapor": "K",         # brightness temperature, kelvin
}

# CONUS sector approximate bbox in EPSG:4326. Used for early bbox-vs-sector
# rejection so we don't pay the S3 round-trip for a query that can never
# return pixels. ABI CONUS scan is the ~5,000 km × 3,000 km fixed grid
# centered on Texas/Oklahoma (~95°W).
_CONUS_SECTOR_BBOX = (-153.0, 14.0, -52.0, 57.0)

# Bbox quantization step (6 decimal places, ~0.1 m equator) for cache-key
# stability. Matches sibling fetchers (fetch_administrative_boundaries).
_BBOX_QUANTIZE_DP = 6

# How many minutes to round ``valid_time`` to in the cache key.
# 15 minutes = 3 frames per cache slot; tight enough that animations stay
# fresh, loose enough that an agent re-running the same prompt 2 min later
# hits the cache.
_VALID_TIME_ROUND_MINUTES = 15

# User-Agent per NOAA Big-Data Program courtesy convention.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)


# ---------------------------------------------------------------------------
# AtomicToolMetadata — registered once at import time.
#
# NOTE: kickoff specifies ``supports_global_query=False`` but the contract
# model in contracts/tool_registry.py does not yet carry that
# field. The 4 existing fields (name, ttl_class, source_class, cacheable)
# are what AtomicToolMetadata accepts today. Surfaced as
# OQ-0104-CONTRACT-SUPPORTS-GLOBAL-QUERY for the schema specialist; the
# semantic guard is enforced at call time via BBOX_REQUIRED below.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="fetch_goes_satellite",
    ttl_class="dynamic-1h",
    source_class="goes_satellite",
    cacheable=True,
)


# ---------------------------------------------------------------------------
# Public helpers (also importable for tests).
# ---------------------------------------------------------------------------


def _band_to_variable(band: str) -> str:
    """Return the netCDF variable name for ``band``.

    Raises:
        ``GOESInputError``: if ``band`` is unknown.
    """
    try:
        return _BAND_TO_VARIABLE[band]
    except KeyError as exc:
        raise GOESInputError(
            f"unknown band={band!r}; allowed: {sorted(_BAND_TO_VARIABLE)}"
        ) from exc


def _band_to_units(band: str) -> str:
    """Return the units string for ``band``."""
    try:
        return _BAND_TO_UNITS[band]
    except KeyError as exc:
        raise GOESInputError(
            f"unknown band={band!r}; allowed: {sorted(_BAND_TO_UNITS)}"
        ) from exc


def _validate_bbox(bbox: tuple[float, float, float, float] | None) -> None:
    """Raise ``GOESBboxRequiredError`` / ``GOESInputError`` if bbox is invalid."""
    if bbox is None:
        raise GOESBboxRequiredError(
            "bbox is required for fetch_goes_satellite — full disk / sector "
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


def _round_bbox(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Round bbox to ``_BBOX_QUANTIZE_DP`` decimals for cache-key stability."""
    return tuple(round(v, _BBOX_QUANTIZE_DP) for v in bbox)  # type: ignore[return-value]


def _round_valid_time(now: datetime) -> str:
    """Round ``now`` (UTC) down to the nearest 15-minute boundary; return ISO-Z.

    Used in the cache-key params so a same-band same-bbox fetch within the
    same 15-min slot reuses the cached file.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    floored_minutes = (now.minute // _VALID_TIME_ROUND_MINUTES) * _VALID_TIME_ROUND_MINUTES
    rounded = now.replace(minute=floored_minutes, second=0, microsecond=0)
    return rounded.strftime("%Y-%m-%dT%H:%M:%SZ")


def _doy_hour(when: datetime) -> tuple[int, int, int]:
    """Return ``(year, doy, hour)`` in UTC for ``when``.

    NOAA Big-Data Program GOES keys are partitioned ``ABI-L2-MCMIPC/<year>/<doy>/<hour>/...``.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)
    return when.year, when.timetuple().tm_yday, when.hour


# Pre-compiled regex matching the ``s<14 digit timestamp>`` start-time
# substring in an MCMIPC key. We use that as the "most recent" tie-breaker.
# Example: ``OR_ABI-L2-MCMIPC-M6_G16_s20241801201176_e..._c....nc``
_KEY_START_TIME_RE = re.compile(r"_s(\d{14})_")


def _key_start_time(key: str) -> str:
    """Return the ``s<...>`` start-time substring from a key, or ``""`` if absent."""
    m = _KEY_START_TIME_RE.search(key)
    return m.group(1) if m else ""


def _pick_most_recent_key(keys: list[str]) -> str:
    """Pick the most-recent MCMIPC key from a list.

    Selection is by the ``_s<YYYYJJJHHMMSSF>`` start-time substring (a
    14-digit lexicographically-sortable timestamp); the largest is the most
    recent. Returns ``""`` if the list is empty or no key has the expected
    start-time pattern.
    """
    candidates = [(_key_start_time(k), k) for k in keys]
    candidates = [(t, k) for t, k in candidates if t]
    if not candidates:
        return ""
    candidates.sort()
    return candidates[-1][1]


def _list_keys_for_prefix(
    bucket: str,
    prefix: str,
    *,
    max_keys: int = 1000,
    session: requests.Session | None = None,
) -> list[str]:
    """List S3 object keys under ``prefix`` in ``bucket`` via the public REST API.

    Uses the unauthenticated ``?list-type=2`` endpoint. We deliberately do
    NOT use ``boto3`` to keep the agent dependency surface small (boto3 is
    not in the venv) and because the NOAA buckets do not require signed
    requests.

    Returns up to ``max_keys`` keys (one page; the GOES per-hour prefixes
    contain at most ~12 frames so paging is unnecessary).
    """
    url = (
        f"https://{bucket}.s3.amazonaws.com/"
        f"?list-type=2&prefix={prefix}&max-keys={max_keys}"
    )
    sess = session or requests
    try:
        resp = sess.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=30.0,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise GOESUpstreamError(
            f"GOES S3 listing failed (bucket={bucket}, prefix={prefix}): {exc}"
        ) from exc
    # The S3 ListObjects v2 response is XML; we don't need a real XML parser
    # for our narrow use (extract ``<Key>...</Key>`` strings). The full set
    # of edge cases (XML entity expansion, CDATA) is not present in S3 list
    # responses, and re.findall is robust enough for the fixed S3 schema.
    return re.findall(r"<Key>([^<]+)</Key>", resp.text)


def _list_recent_keys(
    satellite: str,
    *,
    now: datetime | None = None,
    session: requests.Session | None = None,
    lookback_hours: int = 3,
) -> list[str]:
    """Return MCMIPC keys from the last ``lookback_hours`` hours, newest-first sorted.

    Walks ``<year>/<doy>/<hour>/`` partitions backwards from ``now`` until a
    non-empty result is found, or ``lookback_hours`` are exhausted. A 3-hour
    window is the safety margin against quirks of the NOAA ingestion lag
    (we have observed up to ~30 minutes occasionally).

    Raises:
        ``GOESInputError``: if ``satellite`` is unknown.
        ``GOESUpstreamError``: if every probed hour partition fails.
        ``GOESEmptyError``: if the lookback window yields no keys.
    """
    # Normalize-then-validate (belt-and-suspenders: callers reach this helper
    # directly in tests/animation siblings). Maps GOES-18/goes18/G18/West/18 ->
    # canonical token; unknown -> loud GOESInputError instead of a 404 bucket.
    satellite = _normalize_satellite(satellite)
    bucket = _SATELLITE_BUCKETS[satellite]

    when = now or datetime.now(timezone.utc)
    last_upstream_error: GOESUpstreamError | None = None

    for hours_back in range(lookback_hours + 1):
        probe_when = when - timedelta(hours=hours_back)
        year, doy, hour = _doy_hour(probe_when)
        prefix = f"{_PRODUCT_PREFIX}/{year}/{doy:03d}/{hour:02d}/"
        try:
            keys = _list_keys_for_prefix(bucket, prefix, session=session)
        except GOESUpstreamError as exc:
            last_upstream_error = exc
            logger.warning(
                "fetch_goes_satellite: listing prefix=%s failed: %s", prefix, exc
            )
            continue
        if keys:
            logger.info(
                "fetch_goes_satellite: %d MCMIPC keys in %s (hours_back=%d)",
                len(keys),
                prefix,
                hours_back,
            )
            return keys

    if last_upstream_error is not None:
        raise last_upstream_error
    raise GOESEmptyError(
        f"no MCMIPC keys in last {lookback_hours}h for satellite={satellite!r}"
    )


# ---------------------------------------------------------------------------
# Download + reproject.
# ---------------------------------------------------------------------------


def _download_to_tempfile(url: str, *, session: requests.Session | None = None) -> str:
    """Stream-download ``url`` to a temp ``.nc`` file; return the path.

    Caller is responsible for ``os.unlink``-ing the returned path.
    """
    sess = session or requests
    try:
        resp = sess.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=300.0,
            stream=True,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise GOESUpstreamError(
            f"GOES netCDF download failed url={url}: {exc}"
        ) from exc

    fd, path = tempfile.mkstemp(suffix=".nc", prefix="trid3nt_goes_")
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):  # 1 MiB
                if chunk:
                    f.write(chunk)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    logger.info(
        "fetch_goes_satellite: downloaded %d bytes to %s", os.path.getsize(path), path
    )
    return path


def _reproject_and_clip(
    nc_path: str,
    variable: str,
    bbox: tuple[float, float, float, float],
    target_res_deg: float = 0.02,
) -> bytes:
    """Reproject the requested ``variable`` to EPSG:4326 over ``bbox``; return COG bytes.

    Uses rasterio's netCDF subdataset syntax (``NETCDF:<path>:<var>``) which
    inherits the ABI fixed-grid geostationary CRS from the file metadata.
    The reprojection (``calculate_default_transform`` + ``reproject``) lands
    the pixels on a regular EPSG:4326 grid covering ``bbox`` only.

    CMI variables in the L2 MCMIPC product are stored as scaled int16 with
    ``scale_factor`` and ``add_offset`` CF attributes. We read the raw int16
    pixels via rasterio (which inherits the geostationary CRS), then read the
    scale/offset from the netCDF metadata via ``netCDF4`` and apply them after
    the warp so the output float32 array carries physical units
    (reflectance / kelvin).

    Raises:
        ``GOESUpstreamError``: rasterio open / reproject / write failure.
        ``GOESEmptyError``: bbox produces 0 output pixels.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import (
        Resampling,
        reproject,
    )
    from rasterio.transform import from_bounds

    min_lon, min_lat, max_lon, max_lat = bbox

    sub_uri = f'NETCDF:"{nc_path}":{variable}'

    # Read CF scale/offset from the netCDF metadata so we can convert raw
    # int16 DN into physical units after the warp. This is the same
    # transformation netCDF4 would apply automatically (its ``auto_mask=True``
    # / ``auto_scale=True`` defaults) but rasterio's NETCDF driver does not.
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

        # Compute output grid: roughly preserve native resolution but resampled
        # into the EPSG:4326 bbox. Default ~0.02° (~2 km at the equator, matching
        # the ABI nominal sub-satellite-point resolution); a caller MAY pass a
        # finer ``target_res_deg`` (e.g. 0.005° ~0.5 km) to keep the native
        # visible-band detail. The default is unchanged, so every current call is
        # byte-identical.
        out_res_deg = target_res_deg
        width = max(1, int(math.ceil((max_lon - min_lon) / out_res_deg)))
        height = max(1, int(math.ceil((max_lat - min_lat) / out_res_deg)))
        out_transform = from_bounds(min_lon, min_lat, max_lon, max_lat, width, height)

        # Warp the raw int16 DN to the output grid using a sentinel for nodata;
        # the scale/offset is applied AFTER the warp on float values. Using
        # nearest-neighbor resampling on int16 keeps the int16-fill-value
        # propagation clean (a bilinear blend of fill+real would corrupt
        # nodata accounting).
        warp_sentinel = np.iinfo(np.int16).min  # -32768 — outside [0, 4095] valid_range
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

        # Convert raw DN → float32 physical units: scale_factor * DN + add_offset.
        # Mask out the warp sentinel AND the CF _FillValue if any.
        out_arr = warped.astype(np.float32) * np.float32(scale_factor) + np.float32(add_offset)
        mask = warped == warp_sentinel
        if fill_value is not None:
            mask |= warped == int(fill_value)
        # Also mask values outside the CF valid_range [0, 4095] (negative
        # sentinels and overflow).
        mask |= (warped < 0) | (warped > 4095)
        out_arr[mask] = np.nan

        # Sanity: refuse to emit an all-NaN output (bbox missed the disk).
        if not np.isfinite(out_arr).any():
            raise GOESEmptyError(
                f"bbox={bbox} produces no valid {variable} pixels "
                "(likely outside CONUS sector or behind the disk limb)"
            )

        # Write COG.
        out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="trid3nt_goes_cog_")
        os.close(out_fd)
        try:
            profile = {
                "driver": "COG",
                "dtype": "float32",
                "count": 1,
                "height": height,
                "width": width,
                "crs": "EPSG:4326",
                "transform": out_transform,
                "nodata": float("nan"),
                "compress": "DEFLATE",
            }
            try:
                with rasterio.open(out_path, "w", **profile) as dst:
                    dst.write(out_arr, 1)
            except Exception as exc:  # noqa: BLE001 — COG driver may not be available
                # Fall back to GTiff if COG isn't available in this rasterio.
                logger.warning(
                    "fetch_goes_satellite: COG write failed (%s); falling back to GTiff",
                    exc,
                )
                profile["driver"] = "GTiff"
                profile["tiled"] = True
                with rasterio.open(out_path, "w", **profile) as dst:
                    dst.write(out_arr, 1)

            with open(out_path, "rb") as f:
                return f.read()
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass
    finally:
        src.close()


# ---------------------------------------------------------------------------
# Core fetch — composes listing + download + reprojection.
# ---------------------------------------------------------------------------


def _fetch_goes_bytes(
    bbox: tuple[float, float, float, float],
    band: str,
    satellite: str,
    target_res_deg: float = 0.02,
) -> bytes:
    """End-to-end fetch: list most-recent MCMIPC → download → reproject → COG bytes."""
    variable = _band_to_variable(band)
    bucket = _SATELLITE_BUCKETS[satellite]

    keys = _list_recent_keys(satellite)
    chosen = _pick_most_recent_key(keys)
    if not chosen:
        raise GOESEmptyError(
            f"no usable MCMIPC keys found among {len(keys)} candidates for satellite={satellite}"
        )
    url = f"https://{bucket}.s3.amazonaws.com/{chosen}"
    logger.info("fetch_goes_satellite: chosen key %s", chosen)

    nc_path = _download_to_tempfile(url)
    try:
        return _reproject_and_clip(nc_path, variable, bbox, target_res_deg)
    finally:
        try:
            os.unlink(nc_path)
        except OSError:
            pass


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
def fetch_goes_satellite(
    bbox: tuple[float, float, float, float],
    band: str = "visible",
    satellite: str = "goes-19",
    target_res_deg: float | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI:
    """GOES-16/17/18/19 ABI satellite imagery via NOAA Big-Data Program S3.

    **What it does:** Fetches the most-recent ABI L2 Multi-Channel Cloud and
    Moisture Imagery (MCMIPC) product from the public NOAA Big-Data Program S3
    buckets, reprojects the requested band to EPSG:4326 over the requested bbox,
    and writes a COG to the 1-hour dynamic cache. Three bands: visible
    reflectance, IR window brightness temperature, and water-vapor brightness
    temperature. The CONUS sector refreshes every 5 minutes.

    **When to use:**
    - User asks to "show satellite imagery", "show cloud cover", or "show the
      storm on satellite" for any CONUS or near-CONUS location.
    - Current-conditions weather context for a hurricane or severe-weather
      narration — visible band shows cloud structure; IR window shows storm
      tops; water vapor shows upper-level moisture.
    - Near-real-time storm monitoring alongside NEXRAD radar overlays.
    - Any map view that benefits from a live geostationary background to
      orient the user relative to current cloud/storm structure.

    **When NOT to use:**
    - DO NOT use for precipitation data — GOES CMI bands are radiometric
      measurements (reflectance / brightness temperature), not rainfall;
      use ``fetch_mrms_qpe`` for gauge-corrected rain accumulation.
    - DO NOT use when the bbox is outside the CONUS sector (~-153 W to -52 W,
      14 N to 57 N) — the MCMIPC product is CONUS-only; full-disk imagery is
      not served by this tool.
    - DO NOT use for historical replay at a specific valid time — this tool
      always fetches the most-recent observation; a future ``valid_time``
      param is in scope for v0.2.
    - DO NOT use for precipitation-radar products — use
      ``fetch_nexrad_reflectivity`` for that.

    **Parameters:**
    - ``bbox`` (tuple[float, float, float, float]): ``(min_lon, min_lat,
      max_lon, max_lat)`` in EPSG:4326. Required (full-disk download is
      ~50 MB per band; bbox is mandatory). Example for Tampa Bay:
      ``(-83.0, 27.0, -81.5, 28.5)``.
    - ``band`` (str, default ``"visible"``): ``"visible"`` (ABI band 2,
      0.64 µm, reflectance 0-1.5), ``"ir_window"`` (ABI band 13, 10.35 µm,
      brightness temperature in K), ``"water_vapor"`` (ABI band 8, 6.19 µm,
      brightness temperature in K).
    - ``satellite`` (str, default ``"goes-19"``): which GOES-R bird to read.
      ``"goes-19"`` (current operational GOES-East, 75.2W, since 2025-04-07 --
      eastern CONUS / Gulf / Atlantic), ``"goes-18"`` (current operational
      GOES-West, Pacific / western CONUS), ``"goes-16"`` (historical GOES-East,
      decommissioned 2025-04-07 -- data distribution ended, kept for archival
      lookups), ``"goes-17"`` (historical GOES-West). Accepts forgiving spellings
      -- ``"GOES-18"``, ``"goes18"``, ``"G18"``, a bare ``"18"``, and directional
      ``"GOES-East"`` / ``"GOES-West"`` (mapped to goes-19 / goes-18) -- all
      normalized internally; an unrecognized token raises a typed input error.

    **Returns:** A ``LayerURI`` pointing at a float32 COG in the cache bucket
    (``s3://trid3nt-cache/cache/dynamic-1h/goes_satellite/<key>.tif``).
    ``layer_type="raster"``, ``role="context"``. ``units="reflectance"`` for
    visible, ``"K"`` for IR/WV. EPSG:4326, ~0.02 degree (~2 km) resolution.

    **Cross-tool dependencies:**
    - Pairs with ``fetch_nexrad_reflectivity`` for combined radar + satellite
      overlays during storm narrations.
    - Typically requested alongside ``fetch_nws_alerts_conus`` for real-time
      storm context.
    - Pairs with ``fetch_mrms_qpe`` when the user wants both cloud context
      and precipitation accumulation.

    FR-CE-8: ``read_through`` with ``ttl_class="dynamic-1h"``; cache key is
    SHA-256 over ``(bbox-6dp, band, satellite, valid_time_rounded_15min)`` so
    a fresh observation within the same hour can trigger a fresh fetch while
    repeated queries hit the cache.
    """
    _validate_bbox(bbox)
    if band not in _BAND_TO_VARIABLE:
        raise GOESInputError(
            f"unknown band={band!r}; allowed: {sorted(_BAND_TO_VARIABLE)}"
        )
    # Normalize-then-validate: accept GOES-18 / goes18 / G18 / "GOES West" / 18
    # etc. and canonicalize to the bucket-key token ("goes-18") before any S3
    # path is built; an unrecognized token raises GOESInputError (loud, typed).
    satellite = _normalize_satellite(satellite)

    q_bbox = _round_bbox(bbox)
    valid_time = _round_valid_time(datetime.now(timezone.utc))

    # Resolution is a USER lever: default 0.02 deg (~2 km) when unset so every
    # current call is byte-identical; an explicit ``target_res_deg`` (e.g. 0.005
    # deg ~0.5 km for the visible band) drops into the reproject AND the cache key
    # so a finer frame gets its own cache namespace and never collides with the
    # default-res object.
    res_deg = 0.02 if target_res_deg is None else float(target_res_deg)
    if not math.isfinite(res_deg) or res_deg <= 0.0:
        raise GOESInputError(
            f"target_res_deg must be a positive finite degree value; got "
            f"{target_res_deg!r}"
        )

    params = {
        "bbox": list(q_bbox),
        "band": band,
        "satellite": satellite,
        "valid_time": valid_time,
    }
    # Additive cache-key entry ONLY when overridden, so the default-res cache key
    # stays byte-identical to the pre-change key.
    if target_res_deg is not None:
        params["res_deg"] = round(res_deg, 6)

    result = read_through(
        metadata=_METADATA,
        params=params,
        ext="tif",
        fetch_fn=lambda: _fetch_goes_bytes(q_bbox, band, satellite, res_deg),
    )
    assert result.uri is not None, (
        "fetch_goes_satellite is cacheable; uri must be set by read_through"
    )

    units = _band_to_units(band)
    layer_label = {
        "visible": "Visible (Band 2)",
        "ir_window": "IR Window (Band 13)",
        "water_vapor": "Water Vapor (Band 8)",
    }[band]

    return LayerURI(
        layer_id=f"goes-{satellite}-{band}-{q_bbox[0]:.4f}-{q_bbox[1]:.4f}",
        name=f"GOES Satellite — {layer_label} ({satellite.upper()})",
        layer_type="raster",
        uri=result.uri,
        style_preset="goes_satellite",
        role="context",
        units=units,
    )
