"""GOES ABI shared substrate: the satellite normalizer and the public-S3 primitives.

Low-level helpers with NO registered tool and no dependency on the router, the cache or
the stitch, so every GOES consumer imports them from this leaf."""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime
from typing import Any

import requests

from .._fetch_common import FetchError

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers.imagery._goes_common"
)

__all__ = [
    "GOESError",
    "GOESBboxRequiredError",
    "GOESInputError",
    "GOESUpstreamError",
    "GOESEmptyError",
    "_normalize_satellite",
    "_SATELLITE_BUCKETS",
    "_SATELLITE_FILENAME_CODE",
    "_GOES_EAST",
    "_GOES_WEST",
    "_PRODUCT_PREFIX",
    "_KEY_START_TIME_RE",
    "_USER_AGENT",
    "_doy_hour",
    "_list_keys_for_prefix",
    "_download_to_tempfile",
]




class GOESError(FetchError):
    """Base class for GOES satellite failures: ``error_code`` is the stable wire code
    and ``retryable`` guides the retry decision. The base is ``FetchError``, so a
    pinned code survives the delegate wrapper's passthrough."""

    error_code: str = "GOES_SATELLITE_ERROR"
    retryable: bool = True


class GOESBboxRequiredError(GOESError):
    """``bbox`` was None or otherwise missing. It is required because a full fixed-grid
    CONUS raster is around 50 MB uncompressed per band, so an unbounded request costs
    egress and a cache write for an area nobody asked about."""

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


# Satellite identifier normalization + maps.
#
# NOTE: the AWS Open Data bucket token GLUES the digits to "goes" with NO
# hyphen (noaa-goes18, NOT noaa-goes-18 -- the latter 404s). The dict keys are
# the canonical lowercase-hyphenated internal token ("goes-18"); _normalize_satellite
# maps every human/LLM spelling (GOES-18, goes18, G18, "GOES West", ...) onto
# these keys so a malformed identifier is normalized or rejected LOUD, never
# silently turned into a bad bucket path.
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
# hyphen -- the same glued-vs-hyphenated hazard as the bucket token.
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
    """Map any accepted satellite spelling to the canonical lowercase-hyphenated token
    that keys the bucket and filename tables. An unrecognized token fails LOUD, listing
    the accepted forms, rather than reaching a silent 404 or an empty fetch."""

    # The bucket spelling GLUES the digits while callers write a zoo of forms:
    # canonical goes-16 through goes-19, glued or spaced, the G18-style filename code, a
    # bare number, or a direction (east and west resolving to the current birds). All
    # normalize here, case- and separator-insensitive, BEFORE the allow-list check.
    if not isinstance(satellite, str):
        raise GOESInputError(
            f"satellite must be a string; got {type(satellite).__name__}; "
            f"accepted e.g. {sorted(_SATELLITE_BUCKETS)} or 'GOES-18'/'GOES East'"
        )

    raw = satellite.strip().lower()
    compact = re.sub(r"[\s_\-]+", "", raw)

    if compact in ("goeseast", "east"):
        return _GOES_EAST
    if compact in ("goeswest", "west"):
        return _GOES_WEST

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

# User-Agent per NOAA Big-Data Program courtesy convention.
_USER_AGENT = (
    "trid3nt/0.1 (Hazard Modeling Agent; "
    "https://github.com/double-r-squared/trid3nt-qgis-plugin; agent@trid3nt.dev)"
)

# Pre-compiled regex matching the ``s<14 digit timestamp>`` start-time substring
# in an MCMIPC key. Used as the "most recent" tie-breaker.
_KEY_START_TIME_RE = re.compile(r"_s(\d{14})_")


def _doy_hour(when: datetime) -> tuple[int, int, int]:
    """Return ``(year, doy, hour)`` in UTC for ``when``, the three levels the archive
    keys are partitioned by."""
    from datetime import timezone

    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)
    return when.year, when.timetuple().tm_yday, when.hour


def _list_keys_for_prefix(
    bucket: str,
    prefix: str,
    *,
    max_keys: int = 1000,
    session: requests.Session | None = None,
) -> list[str]:
    """List object keys under ``prefix`` through the unauthenticated list endpoint, since
    the buckets require no signed request. Returns ONE page, up to ``max_keys``; a
    per-hour prefix holds at most about a dozen frames."""
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
    return re.findall(r"<Key>([^<]+)</Key>", resp.text)


def _download_to_tempfile(url: str, *, session: requests.Session | None = None) -> str:
    """Stream-download ``url`` to a temp ``.nc`` file and return the path, which the
    caller is responsible for unlinking."""
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
