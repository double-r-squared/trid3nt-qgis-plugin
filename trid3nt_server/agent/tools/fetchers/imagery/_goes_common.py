"""GOES ABI shared SUBSTRATE: the satellite-identifier normalizer + the
public-S3 listing / download primitives the whole GOES family shares.

Relocated here from the deleted ``fetch_goes_satellite`` coded twin (which folded onto
the router as a ``library_delegate`` spec). These low-level helpers have NO registered
tool and NO dependency on the router / read_through / slider stitch, so they live in
this leaf substrate and every GOES consumer imports FROM here:
``hooks/goes_satellite`` (the fold), ``_goes_archive_core`` (the animation substrate),
and ``hooks/goes_archive`` / ``hooks/goes_animation`` / ``hooks/glm`` (the frame hooks).
This mirrors the move that relocated the archive-animation twin's body into
``_goes_archive_core``. ASCII only.
"""

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
    "trid3nt_server.agent.tools.fetchers.imagery._goes_common"
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


# ---------------------------------------------------------------------------
# Typed errors (typed-error surface). Shared across the GOES family.
# ---------------------------------------------------------------------------


class GOESError(FetchError):
    """Base class for GOES satellite failures.

    ``error_code`` maps to the WebSocket A.6 error frame emitted by the
    agent surface. ``retryable`` guides retry logic. Base is
    ``FetchError`` (still a ``RuntimeError``, so ``isinstance`` holds) so the
    pinned ``error_code`` survives ``library_delegate.invoke``'s passthrough
    when the ``fetch_goes_satellite`` delegate raises it.
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
# ---------------------------------------------------------------------------

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
    """Return ``(year, doy, hour)`` in UTC for ``when``.

    NOAA Big-Data Program GOES keys are partitioned
    ``ABI-L2-MCMIPC/<year>/<doy>/<hour>/...``.
    """
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
    """List S3 object keys under ``prefix`` in ``bucket`` via the public REST API.

    Uses the unauthenticated ``?list-type=2`` endpoint (no boto3; the NOAA
    buckets do not require signed requests). Returns up to ``max_keys`` keys
    (one page; the GOES per-hour prefixes contain at most ~12 frames).
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
    return re.findall(r"<Key>([^<]+)</Key>", resp.text)


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
