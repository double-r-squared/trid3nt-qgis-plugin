"""``fetch_goes_archive_animation`` -- GOES Fire Temperature animation from the RAW noaa-goes18 S3 ARCHIVE (PATH B).

The HISTORICAL-capable companion to ``fetch_goes_animation``. Where
``fetch_goes_animation`` pulls the ready-made CIRA/RAMMB SLIDER tiles (which only
serve ~100 RECENT frames -- NO historical archive), this tool reads the RAW
``ABI-L2-MCMIPC`` netCDFs from the public ``noaa-goes18`` S3 bucket (a FULL
historical archive, anonymous, no key) and composites the NOAA-NESDIS / CIRA
**Fire Temperature** RGB per-frame, for ANY date (including the distant past).

This unlocks BOTH:
  (b) HISTORICAL dates -- the S3 archive has every 5-minute CONUS scan going back
      to the GOES-18 operational start (vs SLIDER's ~100 recent frames), and
  (c) Fire Temperature -- composited here from the raw C07/C06/C05 CMI bands with
      full control (vs SLIDER's pre-rendered tiles, which had a zoom-coverage gap).

Fire Temperature RGB recipe (NOAA-NESDIS / CIRA Quick Guide, design spike S.12):
  R = ABI C07 (3.9um) BRIGHTNESS TEMPERATURE, stretch 0-60 C (273.15-333.15 K),
      gamma 1, NOT inverted.
  G = ABI C06 (2.2um) REFLECTANCE, stretch 0-100 % (0-1.0 factor), gamma 1.
  B = ABI C05 (1.6um) REFLECTANCE, stretch 0-75 % (0-0.75 factor), gamma 1.
  Per channel: linear stretch -> clip to [0, 1] -> gamma 1 -> scale to 0-255 uint8.
Hot fires read RED -> YELLOW -> WHITE (a hot 3.9um core saturates RED, then the
2.2um + 1.6um reflectance climb pushes G and B up so the very hottest pixels go
white). Water / cool land / cloud render dark / blue-grey.

UNITS WARNING (carried from the spike S.7 gotchas):
  - C07 is brightness TEMPERATURE in KELVIN (subtract 273.15 for the 0-60 C
    stretch). C06 / C05 are REFLECTANCE (0..1 factor, multiply by 100 for the %
    stretch). Mixing the units yields an all-dark or saturated image.
  - MCMIPC CMI bands are stored as scaled int16 with CF ``scale_factor`` /
    ``add_offset`` that rasterio's NETCDF driver does NOT auto-apply -- we apply
    them per band (the same lesson ``fetch_goes_satellite`` codifies).

Strategy:
  1. ``_list_archive_keys_in_window`` -- walk the ``ABI-L2-MCMIPC/<YYYY>/<DOY>/<HH>/``
     S3 partitions (JULIAN day-of-year) across the (start_utc, end_utc) window,
     collect every MCMIPC key whose ``_s<YYYYDOYHHMMSSf>`` start-time falls in the
     window, ordered ascending. Reuses ``fetch_goes_satellite``'s anonymous
     ``?list-type=2`` lister + ``_doy_hour`` + ``_KEY_START_TIME_RE``.
  2. ``_select_window_keys`` -- even-subsample to a frame cap (first + last kept;
     mirrors ``fetch_goes_animation._select_frame_indices`` /
     ``postprocess_flood._select_frame_time_indices``).
  3. Per frame: ONE ``read_through`` (independent cache key per timestamp) ->
     download the MCMIPC netCDF -> read C07/C06/C05 -> apply CF scaling -> reproject
     the geostationary fixed grid to EPSG:4326 over the AOI -> Fire-Temp composite
     -> 3-band uint8 RGB COG (``publish_layer``'s multiband passthrough renders it).
  4. Return an ordered ``list[LayerURI]`` in the SAME shape ``fetch_goes_animation``
     returns -- ``style_preset="goes_rgb_animation"``, the ``"GOES Fire Temperature
     (Archive) step <N> <ISO> (<SAT>)"`` name token, same bbox -- so Track A's
     composer and the web scrubber consume it UNCHANGED.

Honesty floor (the render-chokepoint norm): a window with no MCMIPC keys raises a
typed ``GOESArchiveEmptyError``; a frame whose AOI crop has no valid Fire-Temp
pixels is skipped, and a run that yields ZERO frames raises rather than emitting a
blank animation.

Cache key (per frame): SHA-256 over ``(bbox-6dp, product='fire_temperature',
satellite, ts_start, gamma)`` -- the per-frame start-time makes each key distinct.

ASCII only.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.tool_registry import AtomicToolMetadata

from . import register_tool
from .cache import read_through
from .fetch_goes_satellite import (
    _KEY_START_TIME_RE,
    _PRODUCT_PREFIX,
    _SATELLITE_BUCKETS,
    _doy_hour,
    _download_to_tempfile,
    _list_keys_for_prefix,
    _normalize_satellite,
)
from ._satellite_slider import rgb_array_to_cog_bytes

__all__ = [
    "fetch_goes_archive_animation",
    "GOESArchiveError",
    "GOESArchiveInputError",
    "GOESArchiveBboxRequiredError",
    "GOESArchiveUpstreamError",
    "GOESArchiveEmptyError",
    "GOES_ARCHIVE_SATELLITES",
    "MAX_ARCHIVE_FRAMES",
    "FIRE_TEMP_BANDS",
    "FIRE_TEMP_RED_KELVIN_RANGE",
    "FIRE_TEMP_GREEN_REFL_MAX",
    "FIRE_TEMP_BLUE_REFL_MAX",
    "TRUE_COLOR_BANDS",
    "TRUE_COLOR_GAMMA",
    "TRUE_COLOR_GREEN_COEFFS",
    "FIRE_DETECT_BANDS",
    "FIRE_BT_C07_MIN_K",
    "FIRE_BT_DIFF_MIN_K",
    "FIRE_HOTSPOT_RAMP_KELVIN_RANGE",
    "ARCHIVE_BANDS",
    "_parse_utc",
    "_key_start_datetime",
    "_select_window_keys",
    "_list_archive_keys_in_window",
    "_stretch_brightness_temp_red",
    "_stretch_reflectance",
    "_fire_temperature_rgb",
    "_true_color_rgb",
    "_band_valid_dn_range",
    "_detect_active_fire_mask",
    "_fire_hotspots_rgba",
    "_bake_fire_over_base",
]

logger = logging.getLogger("grace2_agent.tools.fetch_goes_archive_animation")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class GOESArchiveError(RuntimeError):
    """Base class for fetch_goes_archive_animation failures."""

    error_code: str = "GOES_ARCHIVE_ERROR"
    retryable: bool = True


class GOESArchiveInputError(GOESArchiveError):
    """Invalid input (unknown satellite, bad window, bad bbox)."""

    error_code = "GOES_ARCHIVE_INPUT_INVALID"
    retryable = False


class GOESArchiveBboxRequiredError(GOESArchiveError):
    """bbox is required (a sector-wide archive animation would be enormous)."""

    error_code = "BBOX_REQUIRED"
    retryable = False


class GOESArchiveUpstreamError(GOESArchiveError):
    """S3 listing or netCDF download/parse failed."""

    error_code = "GOES_ARCHIVE_UPSTREAM_ERROR"
    retryable = True


class GOESArchiveEmptyError(GOESArchiveError):
    """The window matched no MCMIPC keys, or every frame crop was empty."""

    error_code = "GOES_ARCHIVE_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Satellites with a full raw MCMIPC archive in the public S3 buckets. GOES-18 is
#: GOES-West (Utah / Nevada fire AOIs); GOES-19 is the GOES-East replacement;
#: GOES-16 is the historical East. All carry ABI-L2-MCMIPC.
GOES_ARCHIVE_SATELLITES = ("goes-16", "goes-18", "goes-19")

#: The three ABI CMI bands the Fire Temperature RGB composites (CONFIRMED from the
#: NOAA-NESDIS / CIRA Fire Temperature RGB Quick Guide). One MCMIPC netCDF carries
#: all 16 CMI bands so a single download yields all three.
FIRE_TEMP_BANDS = {
    "red": "CMI_C07",    # ABI band 7, 3.9um, brightness temperature (K)
    "green": "CMI_C06",  # ABI band 6, 2.2um, reflectance (0..1 factor)
    "blue": "CMI_C05",   # ABI band 5, 1.6um, reflectance (0..1 factor)
}

#: RED brightness-temperature stretch: 0-60 C == 273.15-333.15 K (gamma 1).
FIRE_TEMP_RED_KELVIN_RANGE = (273.15, 333.15)

#: GREEN reflectance stretch upper bound: 100 % == 1.0 reflectance factor.
FIRE_TEMP_GREEN_REFL_MAX = 1.0

#: BLUE reflectance stretch upper bound: 75 % == 0.75 reflectance factor.
FIRE_TEMP_BLUE_REFL_MAX = 0.75

#: The ABI CMI bands the daytime TRUE COLOR RGB composites. The ABI has NO native
#: green band, so green is synthesized (CIMSS ABI true-color approximation) from
#: the red (C02), blue (C01), and 0.86um veggie NIR (C03) reflectance bands. C02
#: is the 0.5 km native band -- finer than the 2 km thermal bands -- which is why
#: the true-color path runs at the finer ``_TRUE_COLOR_RES_DEG``.
TRUE_COLOR_BANDS = {
    "red": "CMI_C02",     # ABI band 2, 0.64um red, reflectance, 0.5 km native
    "blue": "CMI_C01",    # ABI band 1, 0.47um blue, reflectance, 1 km native
    "veggie": "CMI_C03",  # ABI band 3, 0.86um veggie NIR, reflectance, 1 km native
}

#: True-color reflectance gamma (~1/2.2). Visible reflectances are dim; a gamma
#: stretch < 1 brightens the midtones so land/smoke read naturally (the standard
#: true-color display gamma).
TRUE_COLOR_GAMMA = 1.0 / 2.2

#: CIMSS ABI synthetic-green coefficients: green = a*red + b*veggie + c*blue.
#: (0.45, 0.10, 0.45) is the widely used CIMSS "true green" coefficient set that
#: reproduces a natural-color scene from the ABI's red/blue/NIR bands.
TRUE_COLOR_GREEN_COEFFS = (0.45, 0.10, 0.45)

# ---------------------------------------------------------------------------
# Active-fire DETECTION bands + thresholds (the fire-only isolation product).
# ---------------------------------------------------------------------------
#
# The Fire Temperature RGB above renders ALL warm pixels red -- in a midday scene
# most of that red is just sun-heated desert, NOT fire. To isolate GENUINE active
# fire we use the STANDARD shortwave-vs-longwave brightness-temperature
# discriminator (Matson & Dozier 1981; the heritage behind the MODIS/Giglio
# contextual algorithm and the GOES-R ABI FDC / WFABBA fire products):
#
#   active_fire = (BT_C07 >= bt_c07_min_k)            # 3.9um is absolutely hot
#                 AND (BT_C07 - BT_longwave >= bt_diff_min_k)  # 3.9um >> 11um
#
# A sub-pixel flame is INTENSELY hot in the 3.9um shortwave window (Planck's law
# makes the 3.9um band hugely more sensitive to a small very-hot sub-pixel area)
# while the 10.3um/12.3um longwave window -- dominated by the cooler surrounding
# pixel area -- stays near the ambient land temperature. So a LARGE C07-minus-C13
# split uniquely separates a true fire from uniformly warm land/cloud (where C07
# and C13 track each other and the difference is small). This is exactly why a
# single-band C07 brightness threshold over-flags warm desert and the difference
# test does not.
#
# Thresholds (DEFENSIBLE DEFAULTS, both tunable params on the tool):
#   bt_c07_min_k  = 320.0 K  -- the absolute 3.9um floor. The MODIS Collection-5
#     fixed absolute test used T4 > 310 K (305 K night); GOES-R ABI band 7
#     saturates near 330 K. We pick 320 K: comfortably ABOVE typical midday land
#     3.9um BT (~300-315 K) yet below the ~330 K saturation ceiling, so warm land
#     is rejected on the absolute test alone while real fire cores (often
#     saturating) pass. Tunable DOWN (e.g. 315 K) to catch cooler/smaller fires.
#   bt_diff_min_k = 10.0 K   -- the C07-minus-longwave split floor. This is the
#     canonical MODIS Collection-5 delta-T* heritage value (Giglio et al.);
#     uniformly warm land has C07~C13 so its difference is only a few K, while a
#     fire pixel's 3.9um runs 10-60+ K hotter than its 11um. Tunable UP (e.g.
#     15-20 K) to demand a stronger, less-ambiguous fire signal.
#
# These two gates together are the self-contained raster detector the kickoff
# requires (FIRMS is a separate VECTOR cross-reference, not needed for this
# raster product). Overridable via env for box-side tuning.

#: The DETECTION bands: the shortwave window C07 (3.9um BT) plus a longwave window
#: band. Both are CMI brightness-temperature bands in the SAME MCMIPC netCDF the
#: Fire-Temp composite already downloads -- NO extra fetch. C13 (10.3um) is the
#: clean-window longwave default; C15 (12.3um) is the documented alternate.
FIRE_DETECT_BANDS = {
    "shortwave": "CMI_C07",   # ABI band 7, 3.9um, brightness temperature (K)
    "longwave": "CMI_C13",    # ABI band 13, 10.3um, brightness temperature (K)
    "longwave_alt": "CMI_C15",  # ABI band 15, 12.3um (alternate longwave window)
}

#: Default absolute 3.9um (C07) brightness-temperature floor for an active-fire
#: pixel (K). See the block comment above for the derivation. Tunable per call
#: via ``bt_c07_min_k``; env-overridable for box-side tuning.
FIRE_BT_C07_MIN_K: float = float(os.environ.get("GRACE2_FIRE_BT_C07_MIN_K", "320.0"))

#: Default C07-minus-longwave (3.9um - 10.3um) brightness-temperature DIFFERENCE
#: floor for an active-fire pixel (K). The MODIS Collection-5 delta-T* heritage
#: value. Tunable per call via ``bt_diff_min_k``; env-overridable.
FIRE_BT_DIFF_MIN_K: float = float(os.environ.get("GRACE2_FIRE_BT_DIFF_MIN_K", "10.0"))

#: The hot ramp for the fire-only RGBA layer maps C07 brightness temperature to
#: orange -> yellow -> white by intensity. The ramp spans from the detection floor
#: region up to the C07 saturation ceiling: 310 K (deep orange) -> 350 K (white).
#: Pixels colder than the low end still render orange (the floor color); the
#: hottest cores render white. Only DETECTED-fire pixels are colored at all; every
#: other pixel is fully transparent (alpha 0), so the span is a within-fire ramp.
FIRE_HOTSPOT_RAMP_KELVIN_RANGE = (310.0, 350.0)

#: The set of band/product modes the archive tool emits. ``fire_temperature`` is
#: the original full Fire-Temp RGB; ``fire_hotspots`` is the transparent RGBA
#: fire-only isolation layer; ``fire_baked`` alpha-composites the fire over the
#: Fire-Temp base into one opaque RGB. All three share the netCDF read + reproject
#: core (no duplicated I/O).
ARCHIVE_BANDS = ("fire_temperature", "true_color", "fire_hotspots", "fire_baked")

#: Upper bound on emitted frames (mirrors fetch_goes_animation.MAX_ANIM_FRAMES /
#: postprocess_flood.MAX_FLOOD_FRAMES=144). A wider window even-subsamples down
#: (first + last kept). Overridable via env.
MAX_ARCHIVE_FRAMES: int = int(os.environ.get("GRACE2_MAX_ARCHIVE_FRAMES", "144"))

#: Output resolution (degrees) for the EPSG:4326 reproject (~2 km, matching the
#: ABI nominal sub-satellite resolution -- same as fetch_goes_satellite). This is
#: the grid for the thermal/fire products (C07/C13 are 2 km native).
_OUT_RES_DEG = 0.02

#: Finer output resolution (degrees) for the daytime TRUE COLOR product (~0.5 km).
#: The true-color RED band C02 is 0.5 km native -- far finer than the 2 km thermal
#: bands -- so true_color reprojects onto this finer grid to keep the visible
#: detail. Overridable per call via ``true_color_res_deg``.
_TRUE_COLOR_RES_DEG = 0.005

#: Bbox quantization (6dp) for cache-key stability.
_BBOX_QUANTIZE_DP = 6

#: Shared style preset across every frame -- the SAME preset fetch_goes_animation
#: emits, so Track A + the web scrubber consume the archive frames UNCHANGED. A
#: 3-band RGB COG renders via publish_layer's multiband passthrough (no colormap).
_GOES_ARCHIVE_STYLE_PRESET = "goes_rgb_animation"

#: Product label for the LayerURI name. "(Archive)" distinguishes the raw-S3
#: historical path from the SLIDER recent path in the scrubber-group STEM, so the
#: two never collide into one group. Each band/product gets its OWN label so the
#: three never collide into one scrubber group on the web side.
_PRODUCT_LABEL = "Fire Temperature (Archive)"

#: Per-band product labels (the LayerURI name stem). Distinct stems keep each
#: product in its own scrubber group while sharing the "step <N> <ISO>" token.
_PRODUCT_LABELS: dict[str, str] = {
    "fire_temperature": "Fire Temperature (Archive)",
    "true_color": "True Color (Archive)",
    "fire_hotspots": "Active Fire Hotspots (Archive)",
    "fire_baked": "Fire Baked on Imagery (Archive)",
}

#: Per-band style preset. The Fire-Temp + baked products are 3-band RGB COGs and
#: the hotspots product is a 4-band RGBA COG -- BOTH render via publish_layer's
#: RGBA/multiband passthrough (band count >= 3 OR an alpha band -> empty
#: style_params, baked colors render directly with alpha respected). The preset
#: token is informational for the scrubber group; no new style_preset row is
#: needed in publish_layer because the passthrough handles RGB(A) directly.
_PRODUCT_STYLE_PRESETS: dict[str, str] = {
    "fire_temperature": "goes_rgb_animation",
    "true_color": "goes_rgb_animation",
    "fire_hotspots": "goes_fire_hotspots_rgba",
    "fire_baked": "goes_rgb_animation",
}

#: LayerURI id slug per band (keeps the products' ids distinct).
_PRODUCT_ID_SLUGS: dict[str, str] = {
    "fire_temperature": "firetemp",
    "true_color": "truecolor",
    "fire_hotspots": "firehot",
    "fire_baked": "firebaked",
}

#: Band-name aliases the LLM may invent for true_color -- normalized to the
#: canonical ``true_color`` so a natural request still routes. ("geocolor" proper
#: is a proprietary CIRA product; "geocolor_raw" maps here to the raw daytime
#: true-color approximation we composite from the visible bands.)
_BAND_ALIASES: dict[str, str] = {
    "natural_color": "true_color",
    "geocolor_raw": "true_color",
}


def _resolve_res_deg(band: str, true_color_res_deg: float | None) -> float:
    """Pick the output cell size (degrees) for ``band``.

    The thermal/fire products (``fire_temperature`` / ``fire_hotspots`` /
    ``fire_baked``) ALWAYS use ``_OUT_RES_DEG`` (0.02 deg ~2 km) -- the C07/C13
    bands are 2 km native, so a finer grid would only upsample. ``true_color``
    uses the caller override when given, else the finer ``_TRUE_COLOR_RES_DEG``
    (~0.5 km) so the 0.5 km native C02 detail survives. Keeping the thermal bands
    pinned to ``_OUT_RES_DEG`` is what makes the existing products byte-identical.
    """
    if band == "true_color":
        if true_color_res_deg is None:
            return _TRUE_COLOR_RES_DEG
        return float(true_color_res_deg)
    return _OUT_RES_DEG


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    common = dict(
        name="fetch_goes_archive_animation",
        ttl_class="dynamic-1h",
        source_class="goes_animation",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Time helpers.
# ---------------------------------------------------------------------------


def _parse_utc(value: Any) -> datetime:
    """Parse an ISO-8601 (or 'YYYY-MM-DD HH:MM') string / datetime -> aware UTC.

    Accepts a trailing 'Z', '+00:00', a space or 'T' separator, and a bare date.
    Raises ``GOESArchiveInputError`` for an unparseable value.
    """
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
    """Parse the ``_s<YYYYDOYHHMMSSf>`` start-time of an MCMIPC key -> aware UTC.

    The ABI naming convention is ``_s`` + 4-digit year + 3-digit day-of-year +
    2-digit hour + 2-digit minute + 2-digit second + 1-digit tenth-of-second
    (14 digits total). Returns ``None`` if the key has no recognizable start-time.
    """
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


# ---------------------------------------------------------------------------
# Frame-list assembly (pure).
# ---------------------------------------------------------------------------


def _select_window_keys(keys: list[str], cap: int = MAX_ARCHIVE_FRAMES) -> list[str]:
    """Even-subsample an ASCENDING list of keys down to ``cap``, endpoints kept.

    Mirrors ``fetch_goes_animation._select_frame_indices`` /
    ``postprocess_flood._select_frame_time_indices``: when ``len(keys) <= cap`` the
    list is returned unchanged; otherwise an even ``linspace`` (rounded + unique)
    keeps the first + last and subsamples the middle. Logs a subsample. Pure.
    """
    n = len(keys)
    if n <= 0:
        return []
    if n <= cap:
        return list(keys)
    import numpy as np

    idx = np.linspace(0, n - 1, cap).round().astype(int)
    kept_idx = [int(i) for i in np.unique(idx)]
    logger.info(
        "fetch_goes_archive_animation: %d in-window MCMIPC keys exceed cap=%d; "
        "subsampling evenly to %d (first+last kept).",
        n,
        cap,
        len(kept_idx),
    )
    return [keys[i] for i in kept_idx]


def _hours_in_window(start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """Return every top-of-hour datetime whose hour overlaps [start, end] (UTC).

    The MCMIPC S3 keys are partitioned by ``<YYYY>/<DOY>/<HH>/``; a frame at
    HH:MM lives under the HH partition, so we must list every hour partition the
    window touches (inclusive of the hour containing ``end_utc``).
    """
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
    """List MCMIPC ``(start_time, key)`` pairs in [start, end] from the S3 archive.

    Walks every ``ABI-L2-MCMIPC/<YYYY>/<DOY>/<HH>/`` partition the window touches
    (JULIAN day-of-year key layout), parses each key's ``_s<...>`` start-time, and
    keeps the ones with ``start_utc <= t <= end_utc``. Returns the pairs ORDERED
    ASCENDING by start time. Reuses ``fetch_goes_satellite``'s anonymous
    ``?list-type=2`` lister + ``_doy_hour``.

    Raises:
        ``GOESArchiveInputError``: unknown satellite.
        ``GOESArchiveUpstreamError``: every probed hour partition failed.
    """
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
                "fetch_goes_archive_animation: listing prefix=%s failed: %s",
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


# ---------------------------------------------------------------------------
# Fire Temperature band math (pure -- the testable core).
# ---------------------------------------------------------------------------


def _stretch_brightness_temp_red(bt_kelvin: Any) -> Any:
    """Stretch a C07 brightness-temperature (K) array to [0,1] RED per the recipe.

    Linear 273.15 K (0 C) -> 0.0, 333.15 K (60 C) -> 1.0, clipped to [0,1],
    gamma 1 (no exponent). NaN -> 0.0 (transparent / no-data reads dark).
    """
    import numpy as np

    lo, hi = FIRE_TEMP_RED_KELVIN_RANGE
    arr = np.asarray(bt_kelvin, dtype=np.float32)
    out = (arr - np.float32(lo)) / np.float32(hi - lo)
    out = np.clip(out, 0.0, 1.0)
    out = np.where(np.isfinite(out), out, 0.0)
    return out.astype(np.float32)


def _stretch_reflectance(refl: Any, refl_max: float) -> Any:
    """Stretch a reflectance (0..1 factor) array to [0,1] per ``refl_max``.

    Linear 0.0 -> 0.0, ``refl_max`` -> 1.0, clipped to [0,1], gamma 1. NaN -> 0.0.
    Used for GREEN (C06, refl_max=1.0 == 100 %) and BLUE (C05, refl_max=0.75 ==
    75 %).
    """
    import numpy as np

    arr = np.asarray(refl, dtype=np.float32)
    denom = max(1e-6, float(refl_max))
    out = arr / np.float32(denom)
    out = np.clip(out, 0.0, 1.0)
    out = np.where(np.isfinite(out), out, 0.0)
    return out.astype(np.float32)


def _fire_temperature_rgb(
    c07_bt_kelvin: Any,
    c06_reflectance: Any,
    c05_reflectance: Any,
) -> Any:
    """Composite the Fire Temperature RGB from the three CF-scaled CMI band arrays.

    R = C07 (3.9um) BT stretched 273.15-333.15 K.
    G = C06 (2.2um) reflectance stretched 0-100 %.
    B = C05 (1.6um) reflectance stretched 0-75 %.
    Each channel clipped to [0,1], gamma 1, scaled to 0-255 uint8. Returns a
    ``(3, H, W)`` uint8 array (band-first, the rasterio write order).

    Inputs must already carry physical units (CF scale_factor/add_offset applied)
    and be co-registered (same shape) -- the per-frame reproject upstream ensures
    that. Pure function (the testable Fire-Temp core).
    """
    import numpy as np

    red = _stretch_brightness_temp_red(c07_bt_kelvin)
    green = _stretch_reflectance(c06_reflectance, FIRE_TEMP_GREEN_REFL_MAX)
    blue = _stretch_reflectance(c05_reflectance, FIRE_TEMP_BLUE_REFL_MAX)
    if not (red.shape == green.shape == blue.shape):
        raise GOESArchiveUpstreamError(
            f"Fire Temperature band shapes differ: R={red.shape} G={green.shape} "
            f"B={blue.shape}; bands must be co-registered before compositing"
        )
    rgb = np.stack([red, green, blue], axis=0)  # (3, H, W) in [0,1]
    return np.clip(np.rint(rgb * 255.0), 0, 255).astype(np.uint8)


def _true_color_rgb(
    c02_red_refl: Any,
    c01_blue_refl: Any,
    c03_veggie_refl: Any,
    *,
    gamma: float = TRUE_COLOR_GAMMA,
) -> Any:
    """Composite the daytime TRUE COLOR RGB from the visible CMI reflectance bands.

    R = C02 (0.64um red) reflectance.
    B = C01 (0.47um blue) reflectance.
    synthetic GREEN = a*R + b*C03(0.86um veggie) + c*B (CIMSS ABI true-color
        approximation; ``TRUE_COLOR_GREEN_COEFFS`` == (0.45, 0.10, 0.45)). The ABI
        has no native green band, so green is synthesized from red/blue/NIR.
    Each channel clipped to [0,1], gamma-stretched (default ~1/2.2 to brighten the
    dim visible reflectances), scaled to 0-255 uint8. Returns a ``(3, H, W)`` uint8
    array (band-first, the rasterio write order). NaN -> 0 (no-data reads black).
    Pure function (the testable true-color core).

    Inputs must already carry physical reflectance units (CF scale_factor/
    add_offset applied) and be co-registered (same shape) -- the per-frame
    reproject upstream ensures that.
    """
    import numpy as np

    red = np.nan_to_num(np.asarray(c02_red_refl, dtype=np.float32), nan=0.0)
    blue = np.nan_to_num(np.asarray(c01_blue_refl, dtype=np.float32), nan=0.0)
    veg = np.nan_to_num(np.asarray(c03_veggie_refl, dtype=np.float32), nan=0.0)
    if not (red.shape == blue.shape == veg.shape):
        raise GOESArchiveUpstreamError(
            f"true-color band shapes differ: R={red.shape} B={blue.shape} "
            f"VEG={veg.shape}; bands must be co-registered before compositing"
        )
    red = np.clip(red, 0.0, 1.0)
    blue = np.clip(blue, 0.0, 1.0)
    veg = np.clip(veg, 0.0, 1.0)
    a, b, c = TRUE_COLOR_GREEN_COEFFS
    green = np.clip(a * red + b * veg + c * blue, 0.0, 1.0)

    g = max(1e-6, float(gamma))
    rgb01 = np.stack([red, green, blue], axis=0)
    rgb01 = np.power(rgb01, np.float32(g))  # gamma stretch (g<1 brightens)
    return np.clip(np.rint(rgb01 * 255.0), 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# Active-fire detection + isolation band math (pure -- the testable core).
# ---------------------------------------------------------------------------


def _detect_active_fire_mask(
    c07_bt_kelvin: Any,
    longwave_bt_kelvin: Any,
    bt_c07_min_k: float = FIRE_BT_C07_MIN_K,
    bt_diff_min_k: float = FIRE_BT_DIFF_MIN_K,
) -> Any:
    """Boolean active-fire mask from the C07 + longwave brightness-temp arrays.

    The STANDARD shortwave-vs-longwave fire discriminator (Matson & Dozier; the
    MODIS/Giglio + GOES-R ABI FDC / WFABBA heritage). A pixel is flagged active
    fire when BOTH gates pass:

      1. ABSOLUTE: ``BT_C07 >= bt_c07_min_k`` -- the 3.9um is intrinsically hot.
      2. DIFFERENCE: ``(BT_C07 - BT_longwave) >= bt_diff_min_k`` -- the 3.9um runs
         far hotter than the 10.3um (a sub-pixel flame dominates the shortwave
         while the longwave stays near ambient), which is what separates a true
         fire from uniformly warm land/cloud (there C07 ~ C13 so the difference is
         only a few K).

    NaN in either band (no-data / off-disk) yields False at that pixel (a NaN
    comparison is False, but we mask explicitly so the contract is unambiguous).
    Returns a boolean ndarray the shape of the inputs. Pure function.
    """
    import numpy as np

    c07 = np.asarray(c07_bt_kelvin, dtype=np.float32)
    lw = np.asarray(longwave_bt_kelvin, dtype=np.float32)
    if c07.shape != lw.shape:
        raise GOESArchiveUpstreamError(
            f"fire-detection band shapes differ: C07={c07.shape} "
            f"longwave={lw.shape}; bands must be co-registered before detection"
        )
    valid = np.isfinite(c07) & np.isfinite(lw)
    diff = np.where(valid, c07 - lw, np.float32(-1.0e9))
    hot = c07 >= np.float32(bt_c07_min_k)
    split = diff >= np.float32(bt_diff_min_k)
    return (valid & hot & split).astype(bool)


def _hotspot_intensity_ramp(c07_bt_kelvin: Any) -> Any:
    """Map a C07 brightness-temperature (K) array to a 0..1 fire-intensity ramp.

    Linear ``FIRE_HOTSPOT_RAMP_KELVIN_RANGE`` (310 K -> 0.0, 350 K -> 1.0),
    clipped to [0, 1]. Drives the orange -> yellow -> white hot ramp (hotter core
    = whiter). NaN -> 0.0. Pure helper for ``_fire_hotspots_rgba``.
    """
    import numpy as np

    lo, hi = FIRE_HOTSPOT_RAMP_KELVIN_RANGE
    arr = np.asarray(c07_bt_kelvin, dtype=np.float32)
    out = (arr - np.float32(lo)) / np.float32(hi - lo)
    out = np.clip(out, 0.0, 1.0)
    return np.where(np.isfinite(out), out, 0.0).astype(np.float32)


def _hot_ramp_rgb(intensity: Any) -> tuple[Any, Any, Any]:
    """Map a 0..1 fire-intensity array to (R, G, B) float arrays on a hot ramp.

    The ramp is orange (low) -> yellow (mid) -> white (high), a perceptually
    fire-like sequence:
      t in [0, 0.5]:  orange (255, 80, 0) -> yellow (255, 230, 0)
      t in [0.5, 1]:  yellow (255, 230, 0) -> white (255, 255, 255)
    R is pinned at 255 across the ramp (fire is always red-saturated); G climbs
    from 80 -> 230 -> 255; B stays 0 until the top quarter then climbs to 255 so
    only the very hottest cores whiten. Returns three float arrays in [0, 255]
    (the caller rounds to uint8). Pure.
    """
    import numpy as np

    t = np.clip(np.asarray(intensity, dtype=np.float32), 0.0, 1.0)
    r = np.full_like(t, 255.0, dtype=np.float32)
    # G: 80 at t=0, 230 at t=0.5, 255 at t=1.
    lower = t <= 0.5
    g = np.where(
        lower,
        80.0 + (230.0 - 80.0) * (t / 0.5),
        230.0 + (255.0 - 230.0) * ((t - 0.5) / 0.5),
    ).astype(np.float32)
    # B: 0 until t=0.75, then climbs to 255 at t=1 (only the hottest cores white).
    b = np.where(
        t <= 0.75,
        0.0,
        255.0 * ((t - 0.75) / 0.25),
    ).astype(np.float32)
    return r, g, b


def _fire_hotspots_rgba(
    c07_bt_kelvin: Any,
    longwave_bt_kelvin: Any,
    bt_c07_min_k: float = FIRE_BT_C07_MIN_K,
    bt_diff_min_k: float = FIRE_BT_DIFF_MIN_K,
) -> Any:
    """Build the TRANSPARENT RGBA fire-only isolation array.

    Detects active fire (``_detect_active_fire_mask``), colors ONLY the flagged
    pixels on the orange -> yellow -> white hot ramp (by C07 intensity), and sets
    EVERY non-fire pixel fully transparent (alpha 0). Flagged pixels get alpha
    255 (fully opaque fire color) so they bake cleanly over any base.

    Returns a ``(4, H, W)`` uint8 array (band-first R, G, B, A -- the rasterio
    write order). Pure function (the testable isolation core). The detection is
    self-contained from the two BT bands (no FIRMS needed).
    """
    import numpy as np

    mask = _detect_active_fire_mask(
        c07_bt_kelvin, longwave_bt_kelvin, bt_c07_min_k, bt_diff_min_k
    )
    intensity = _hotspot_intensity_ramp(c07_bt_kelvin)
    r, g, b = _hot_ramp_rgb(intensity)

    z = np.zeros(mask.shape, dtype=np.float32)
    red = np.where(mask, r, z)
    green = np.where(mask, g, z)
    blue = np.where(mask, b, z)
    alpha = np.where(mask, np.float32(255.0), np.float32(0.0))

    rgba = np.stack([red, green, blue, alpha], axis=0)
    return np.clip(np.rint(rgba), 0, 255).astype(np.uint8)


def _bake_fire_over_base(base_rgb: Any, fire_rgba: Any) -> Any:
    """Alpha-composite the fire-only RGBA OVER a 3-band base RGB -> one RGB array.

    ``out = base*(1-a) + fire_rgb*a`` per channel, with ``a = fire_alpha/255``.
    Where the fire alpha is 0 (every non-fire pixel) the base shows through
    UNCHANGED; where the fire alpha is 255 (a detected fire pixel) the fire color
    fully replaces the base. The result is an opaque 3-band RGB the user gets as
    "fire baked onto the satellite image" -- one layer, no new style preset
    (publish_layer's multiband passthrough renders the 3-band RGB directly).

    ``base_rgb`` is ``(3, H, W)`` uint8; ``fire_rgba`` is ``(4, H, W)`` uint8.
    Returns ``(3, H, W)`` uint8. Pure function.
    """
    import numpy as np

    base = np.asarray(base_rgb, dtype=np.float32)
    fire = np.asarray(fire_rgba, dtype=np.float32)
    if base.shape[0] < 3 or fire.shape[0] < 4:
        raise GOESArchiveUpstreamError(
            f"bake expects base (3,H,W) + fire (4,H,W); got base={base.shape} "
            f"fire={fire.shape}"
        )
    if base.shape[1:] != fire.shape[1:]:
        raise GOESArchiveUpstreamError(
            f"bake band shapes differ: base={base.shape[1:]} "
            f"fire={fire.shape[1:]}; the two must be co-registered"
        )
    a = (fire[3] / 255.0)[np.newaxis, :, :]  # (1, H, W) in [0, 1]
    fire_rgb = fire[:3]
    out = base[:3] * (1.0 - a) + fire_rgb * a
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# netCDF band read + CF scaling + reproject (the I/O core).
# ---------------------------------------------------------------------------


#: Fallback DN valid range if a CMI variable carries no usable ``valid_range``.
#: 14-bit covers BOTH the emissive C07 (true [0, 16383]) and the reflective
#: C05/C06 (true [0, 4095]) -- a too-wide fallback is safe (valid DN never
#: exceed their own 12-bit ceiling) where a too-narrow 4095 was the bug.
_DEFAULT_VALID_DN_RANGE = (0, 16383)


def _band_valid_dn_range(ncvar: Any) -> tuple[int, int]:
    """Return the ``(lo, hi)`` valid raw-DN range for a CMI band variable.

    Reads the CF ``valid_range`` attribute -- which DIFFERS BY BAND: the
    emissive/thermal C07 (3.9um) is a 14-bit product (``[0, 16383]``) while the
    reflective C05/C06 are 12-bit (``[0, 4095]``). The Fire Temperature RED
    channel is C07, so masking it with the 12-bit ``4095`` ceiling drops
    essentially every warm-land pixel (a 320 K BT is DN ~9368) and the RED
    channel reads 0 across the whole frame. Falls back to
    ``_DEFAULT_VALID_DN_RANGE`` (14-bit) only when ``valid_range`` is absent or
    malformed -- a wide fallback never masks real DN, where the narrow 4095 did.
    """
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
    """Build the output EPSG:4326 ``(transform, width, height)`` for ``bbox``.

    Shared by every product so the Fire-Temp, hotspots, and baked frames land on
    the IDENTICAL grid (the detection bands, the base, and the fire overlay are
    therefore always co-registered with no extra resample). Lifted out of
    ``_reproject_fire_temperature`` so the read core is not duplicated.

    ``res_deg`` is the output cell size in degrees; it defaults to ``_OUT_RES_DEG``
    (0.02 deg ~2 km) so every existing caller produces the SAME grid as before. A
    finer override (e.g. ``_TRUE_COLOR_RES_DEG`` 0.005 deg ~0.5 km) yields a
    proportionally larger grid for the visible true-color bands.
    """
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
    """Read one CMI band, CF-scale + reproject to the EPSG:4326 grid -> physical units.

    The SHARED single-band read/reproject core (extracted from the old inner
    ``_warp_band`` closure so the Fire-Temp composite AND the C13 fire-detection
    longwave band reuse the SAME netCDF read + warp + CF-unscale code -- no
    duplication).

      1. Read CF ``scale_factor`` / ``add_offset`` / ``_FillValue`` + the per-band
         ``valid_range`` (netCDF4).
      2. Read the raw int16 DN + inherit the geostationary CRS (rasterio NETCDF
         subdataset), warp to the EPSG:4326 grid with nearest-neighbor (clean
         int16 fill propagation).
      3. Apply ``scale_factor * DN + add_offset`` -> physical units (K for the
         emissive C07/C13/C15, reflectance for C05/C06), masking the warp
         sentinel + CF fill + out-of-valid-range DN to NaN.

    Returns a ``(H, W)`` float32 physical-unit array (NaN where invalid).

    Raises ``GOESArchiveUpstreamError`` on a missing variable / open / reproject
    failure.
    """
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


def _read_archive_bands(
    nc_path: str,
    bbox: tuple[float, float, float, float],
    variables: tuple[str, ...],
    res_deg: float = _OUT_RES_DEG,
) -> tuple[dict[str, Any], Any, int, int]:
    """Read + CF-scale + reproject a set of CMI bands onto ONE shared EPSG:4326 grid.

    Returns ``({variable: phys_array}, transform, width, height)``. Every band is
    on the identical grid (``_grid_for_bbox`` at ``res_deg``), so the Fire-Temp
    composite, the fire detection (C07 vs C13), and the bake overlay are all
    co-registered with no extra resample. ``res_deg`` defaults to ``_OUT_RES_DEG``
    (0.02 deg) so the thermal/fire products are unchanged; the true-color path
    forwards the finer ``_TRUE_COLOR_RES_DEG``.
    """
    out_transform, width, height = _grid_for_bbox(bbox, res_deg)
    arrays: dict[str, Any] = {}
    for var in variables:
        arrays[var] = _warp_band_to_physical(
            nc_path, var, out_transform, width, height
        )
    return arrays, out_transform, width, height


def _reproject_fire_temperature(
    nc_path: str,
    bbox: tuple[float, float, float, float],
    res_deg: float = _OUT_RES_DEG,
) -> Any:
    """Read C07/C06/C05 from an MCMIPC netCDF, CF-scale + reproject each to EPSG:4326 over ``bbox``, composite Fire Temperature.

    Returns a ``(3, H, W)`` uint8 RGB array plus the output ``(transform, W, H)``:
    ``(rgb, transform, width, height)``. Delegates the per-band netCDF read +
    warp + CF-unscale to the shared ``_read_archive_bands`` core (no duplicated
    I/O). ``res_deg`` defaults to ``_OUT_RES_DEG`` so the Fire-Temp grid is
    unchanged.

    Raises:
        ``GOESArchiveUpstreamError``: rasterio/netCDF open / reproject failure.
        ``GOESArchiveEmptyError``: bbox produces no valid pixels (off the disk).
    """
    import numpy as np

    arrays, out_transform, width, height = _read_archive_bands(
        nc_path,
        bbox,
        (FIRE_TEMP_BANDS["red"], FIRE_TEMP_BANDS["green"], FIRE_TEMP_BANDS["blue"]),
        res_deg,
    )
    c07 = arrays[FIRE_TEMP_BANDS["red"]]
    c06 = arrays[FIRE_TEMP_BANDS["green"]]
    c05 = arrays[FIRE_TEMP_BANDS["blue"]]

    # Honesty floor: refuse an all-NaN crop (bbox missed the disk / sector).
    if not (
        np.isfinite(c07).any() or np.isfinite(c06).any() or np.isfinite(c05).any()
    ):
        raise GOESArchiveEmptyError(
            f"bbox={bbox} produces no valid Fire Temperature pixels "
            "(likely outside the CONUS sector or behind the disk limb)"
        )

    rgb = _fire_temperature_rgb(c07, c06, c05)
    if not rgb.any():
        raise GOESArchiveEmptyError(
            f"bbox={bbox} Fire Temperature composite is all-black "
            "(no thermal / reflectance signal in the AOI crop)"
        )
    return rgb, out_transform, width, height


def _reproject_true_color(
    nc_path: str,
    bbox: tuple[float, float, float, float],
    res_deg: float = _TRUE_COLOR_RES_DEG,
) -> Any:
    """Read C02/C01/C03 from an MCMIPC netCDF, CF-scale + reproject each to EPSG:4326 over ``bbox``, composite daytime TRUE COLOR.

    Mirrors ``_reproject_fire_temperature`` but reads the ``TRUE_COLOR_BANDS``
    triple (red C02 / blue C01 / veggie C03) on the FINER ``res_deg`` grid
    (``_TRUE_COLOR_RES_DEG`` ~0.5 km) so the 0.5 km native C02 detail survives.
    Returns ``(rgb (3,H,W) uint8, transform, width, height)``. Delegates the
    per-band read + warp + CF-unscale to the shared ``_read_archive_bands`` core
    (no duplicated I/O).

    Raises:
        ``GOESArchiveUpstreamError``: rasterio/netCDF open / reproject failure.
        ``GOESArchiveEmptyError``: bbox produces no valid visible pixels (off the
            disk, or a fully nighttime AOI with no daytime reflectance).
    """
    import numpy as np

    arrays, out_transform, width, height = _read_archive_bands(
        nc_path,
        bbox,
        (TRUE_COLOR_BANDS["red"], TRUE_COLOR_BANDS["blue"], TRUE_COLOR_BANDS["veggie"]),
        res_deg,
    )
    c02 = arrays[TRUE_COLOR_BANDS["red"]]
    c01 = arrays[TRUE_COLOR_BANDS["blue"]]
    c03 = arrays[TRUE_COLOR_BANDS["veggie"]]

    # Honesty floor: refuse an all-NaN crop (bbox missed the disk / sector).
    if not (
        np.isfinite(c02).any() or np.isfinite(c01).any() or np.isfinite(c03).any()
    ):
        raise GOESArchiveEmptyError(
            f"bbox={bbox} produces no valid true-color pixels "
            "(likely outside the CONUS sector or behind the disk limb)"
        )

    rgb = _true_color_rgb(c02, c01, c03)
    if not rgb.any():
        raise GOESArchiveEmptyError(
            f"bbox={bbox} true-color composite is all-black "
            "(no visible reflectance in the AOI crop -- likely nighttime)"
        )
    return rgb, out_transform, width, height


def _reproject_fire_hotspots(
    nc_path: str,
    bbox: tuple[float, float, float, float],
    bt_c07_min_k: float = FIRE_BT_C07_MIN_K,
    bt_diff_min_k: float = FIRE_BT_DIFF_MIN_K,
    res_deg: float = _OUT_RES_DEG,
) -> Any:
    """Read C07 (3.9um) + C13 (10.3um) BT from an MCMIPC netCDF -> the fire-only RGBA array.

    Reuses the SHARED ``_read_archive_bands`` core (NO extra fetch -- C07 and C13
    are both CMI bands in the SAME netCDF the Fire-Temp composite downloads),
    runs the shortwave-vs-longwave active-fire discriminator, and isolates the
    flagged pixels on the transparent hot-ramp RGBA. Returns
    ``(rgba (4,H,W) uint8, transform, width, height)``.

    Honesty floor: an all-NaN crop (bbox off the disk) raises
    ``GOESArchiveEmptyError``. Unlike Fire-Temp, an all-transparent frame (NO fire
    detected in the AOI) is NOT an error -- a window with no active fire is a
    legitimate empty hotspot frame; the per-frame caller decides whether to keep
    it (it does, so the scrubber stays time-aligned with the Fire-Temp group).
    """
    import numpy as np

    arrays, out_transform, width, height = _read_archive_bands(
        nc_path,
        bbox,
        (FIRE_DETECT_BANDS["shortwave"], FIRE_DETECT_BANDS["longwave"]),
        res_deg,
    )
    c07 = arrays[FIRE_DETECT_BANDS["shortwave"]]
    c13 = arrays[FIRE_DETECT_BANDS["longwave"]]

    if not (np.isfinite(c07).any() and np.isfinite(c13).any()):
        raise GOESArchiveEmptyError(
            f"bbox={bbox} produces no valid C07/C13 brightness-temp pixels "
            "(likely outside the CONUS sector or behind the disk limb)"
        )

    rgba = _fire_hotspots_rgba(c07, c13, bt_c07_min_k, bt_diff_min_k)
    return rgba, out_transform, width, height


def _reproject_fire_baked(
    nc_path: str,
    bbox: tuple[float, float, float, float],
    bt_c07_min_k: float = FIRE_BT_C07_MIN_K,
    bt_diff_min_k: float = FIRE_BT_DIFF_MIN_K,
    res_deg: float = _OUT_RES_DEG,
) -> Any:
    """Read all bands once -> Fire-Temp base + fire-only RGBA -> bake fire over base.

    Reads C07/C06/C05 (the Fire-Temp base) AND C13 (the detection longwave) in ONE
    pass on the shared grid, composites the Fire-Temp RGB base, detects + isolates
    the fire RGBA, and alpha-composites the fire OVER the base. Returns the baked
    ``(rgb (3,H,W) uint8, transform, width, height)`` -- "fire baked onto the
    satellite image" as one opaque RGB layer. (The base is the Fire-Temp COG the
    tool already produces; a caller-supplied base is handled at the frame level.)
    """
    import numpy as np

    arrays, out_transform, width, height = _read_archive_bands(
        nc_path,
        bbox,
        (
            FIRE_TEMP_BANDS["red"],     # C07 (also the detection shortwave)
            FIRE_TEMP_BANDS["green"],   # C06
            FIRE_TEMP_BANDS["blue"],    # C05
            FIRE_DETECT_BANDS["longwave"],  # C13 (detection longwave)
        ),
        res_deg,
    )
    c07 = arrays[FIRE_TEMP_BANDS["red"]]
    c06 = arrays[FIRE_TEMP_BANDS["green"]]
    c05 = arrays[FIRE_TEMP_BANDS["blue"]]
    c13 = arrays[FIRE_DETECT_BANDS["longwave"]]

    if not (np.isfinite(c07).any() or np.isfinite(c06).any() or np.isfinite(c05).any()):
        raise GOESArchiveEmptyError(
            f"bbox={bbox} produces no valid Fire Temperature pixels "
            "(likely outside the CONUS sector or behind the disk limb)"
        )

    base_rgb = _fire_temperature_rgb(c07, c06, c05)
    fire_rgba = _fire_hotspots_rgba(c07, c13, bt_c07_min_k, bt_diff_min_k)
    baked = _bake_fire_over_base(base_rgb, fire_rgba)
    if not baked.any():
        raise GOESArchiveEmptyError(
            f"bbox={bbox} baked Fire frame is all-black "
            "(no thermal / reflectance signal in the AOI crop)"
        )
    return baked, out_transform, width, height


# ---------------------------------------------------------------------------
# Per-frame fetch (the read_through fetch_fn).
# ---------------------------------------------------------------------------


def _rgba_array_to_cog_bytes(
    rgba: Any,
    out_transform: Any,
    width: int,
    height: int,
) -> bytes:
    """Write a ``(4, H, W)`` uint8 EPSG:4326 RGBA array to COG bytes (alpha band).

    The transparent fire-only layer is a 4-band RGBA COG: band 4 is the ALPHA
    channel (0 = transparent off-fire, 255 = opaque fire), tagged with
    ColorInterp alpha so TiTiler + MapLibre honor transparency. publish_layer's
    ``_is_rgba_or_multiband`` returns True (count >= 3 / alpha band) -> empty
    style_params -> the baked colors + alpha render directly (no new style preset
    needed). Mirrors ``rgb_array_to_cog_bytes`` (COG driver -> GTiff fallback) but
    for 4 bands with an alpha mask.
    """
    import numpy as np
    import rasterio
    import tempfile
    from rasterio.enums import ColorInterp

    rgba = np.asarray(rgba, dtype=np.uint8)
    out_fd, out_path = tempfile.mkstemp(suffix=".tif", prefix="grace2_firehot_cog_")
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
                "fetch_goes_archive_animation: RGBA COG write failed (%s); "
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


def _fetch_archive_frame_cog_bytes(
    satellite: str,
    key: str,
    bbox: tuple[float, float, float, float],
    band: str = "fire_temperature",
    bt_c07_min_k: float = FIRE_BT_C07_MIN_K,
    bt_diff_min_k: float = FIRE_BT_DIFF_MIN_K,
    res_deg: float = _OUT_RES_DEG,
) -> bytes:
    """Download one MCMIPC netCDF -> the requested product COG bytes.

    Dispatches on ``band``:
      - ``fire_temperature`` -> 3-band Fire-Temp RGB COG (unchanged).
      - ``true_color``       -> 3-band daytime true-color RGB COG (finer res).
      - ``fire_hotspots``    -> 4-band transparent fire-only RGBA COG.
      - ``fire_baked``       -> 3-band fire-baked-over-Fire-Temp RGB COG.
    All share the one netCDF download + the shared reproject core. ``res_deg``
    defaults to ``_OUT_RES_DEG`` (the thermal/fire grid); the true-color path
    forwards the finer ``_TRUE_COLOR_RES_DEG`` so the visible detail survives.
    """
    bucket = _SATELLITE_BUCKETS[satellite]
    url = f"https://{bucket}.s3.amazonaws.com/{key}"
    nc_path = _download_to_tempfile(url)
    try:
        if band == "true_color":
            rgb, transform, width, height = _reproject_true_color(
                nc_path, bbox, res_deg
            )
            return rgb_array_to_cog_bytes(rgb, transform, width, height)
        if band == "fire_hotspots":
            rgba, transform, width, height = _reproject_fire_hotspots(
                nc_path, bbox, bt_c07_min_k, bt_diff_min_k, res_deg
            )
            return _rgba_array_to_cog_bytes(rgba, transform, width, height)
        if band == "fire_baked":
            rgb, transform, width, height = _reproject_fire_baked(
                nc_path, bbox, bt_c07_min_k, bt_diff_min_k, res_deg
            )
            return rgb_array_to_cog_bytes(rgb, transform, width, height)
        # Default: the original full Fire Temperature product (unchanged).
        rgb, transform, width, height = _reproject_fire_temperature(
            nc_path, bbox, res_deg
        )
        return rgb_array_to_cog_bytes(rgb, transform, width, height)
    finally:
        try:
            os.unlink(nc_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# bbox helpers.
# ---------------------------------------------------------------------------


def _validate_bbox(bbox: Any) -> tuple[float, float, float, float]:
    if bbox is None:
        raise GOESArchiveBboxRequiredError(
            "bbox is required for fetch_goes_archive_animation (a sector-wide raw "
            "MCMIPC animation is enormous); pass (min_lon, min_lat, max_lon, "
            "max_lat)."
        )
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise GOESArchiveInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    vals = tuple(float(v) for v in bbox)
    if not all(math.isfinite(v) for v in vals):
        raise GOESArchiveInputError(f"bbox contains non-finite values: {bbox!r}")
    min_lon, min_lat, max_lon, max_lat = vals
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise GOESArchiveInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise GOESArchiveInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise GOESArchiveInputError(
            f"bbox is degenerate (min<max on both axes): {bbox!r}"
        )
    return (min_lon, min_lat, max_lon, max_lat)


def _round_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_QUANTIZE_DP) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # readOnlyHint=True, openWorldHint=True (anonymous NOAA S3),
    # destructiveHint=False, idempotentHint=True (per-frame cache dedupes).
    open_world_hint=True,
)
def fetch_goes_archive_animation(
    bbox: tuple[float, float, float, float],
    satellite: str = "goes-18",
    start_utc: str | None = None,
    end_utc: str | None = None,
    step_minutes: int = 5,
    band: str = "fire_temperature",
    bt_c07_min_k: float = FIRE_BT_C07_MIN_K,
    bt_diff_min_k: float = FIRE_BT_DIFF_MIN_K,
    true_color_res_deg: float | None = None,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> list[LayerURI]:
    """Build a HISTORICAL GOES Fire Temperature animation from the RAW noaa-goes18 S3 archive (any past date).

    **What it does:** Reads the RAW ``ABI-L2-MCMIPC`` netCDFs from the public
    ``noaa-goes18`` S3 bucket (a FULL historical archive, anonymous / no key)
    across a UTC time window for ANY date -- including the distant past -- and
    composites the NOAA-NESDIS / CIRA **Fire Temperature** RGB per frame (R = ABI
    C07 3.9um brightness-temp 0-60 C, G = C06 2.2um reflectance 0-100 %, B = C05
    1.6um reflectance 0-75 %, gamma 1). Returns an ORDERED list of per-frame
    EPSG:4326 RGB COGs over the AOI -- one frame per CONUS 5-minute scan -- in the
    SAME shape ``fetch_goes_animation`` returns, so the workflow composer and the
    web scrubber animate them UNCHANGED.

    This is the HISTORICAL companion to ``fetch_goes_animation``: the SLIDER tiles
    that tool uses only serve ~100 RECENT frames (no archive), and their pre-
    rendered Fire Temperature had a zoom-coverage gap. This tool composites Fire
    Temperature from the raw bands and reaches any archived date.

    **When to use:**
    - "Animate the GOES Fire Temperature loop for a fire on a PAST date" (e.g.
      "recreate the Iron Fire GOES animation for 2026-06-22"); any historical
      intra-day GOES Fire Temperature timelapse.
    - When ``fetch_goes_animation`` returns no frames because the requested window
      is older than the SLIDER recent-frame horizon.

    **When NOT to use:**
    - A single most-recent frame (use ``fetch_goes_satellite``).
    - A GeoColor loop or a near-real-time recent loop (use
      ``fetch_goes_animation`` / ``fetch_goes_blend_animation`` -- GeoColor is a
      proprietary CIRA product not reconstructable from raw bands here).
    - A multi-day polar VIIRS timelapse (use ``fetch_viirs_day_fire``).

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326.
      Required. Example (Utah fire cluster): ``(-114.05, 37.0, -109.04, 42.0)``.
    - ``satellite`` (str, default ``"goes-18"``): ``"goes-18"`` (West / Utah-
      Nevada fires), ``"goes-19"`` (East), or ``"goes-16"`` (historical East).
    - ``start_utc`` / ``end_utc`` (str): ISO-8601 UTC window bounds (e.g.
      ``"2026-06-22T13:30:00Z"`` .. ``"2026-06-22T20:00:00Z"``). When omitted, the
      most-recent ~6.5h is used. Works for ANY past date in the archive.
    - ``step_minutes`` (int, default 5): informational; the CONUS archive is
      natively 5-minute. Frames are taken at the archived scan times in the
      window, then even-subsampled to the frame cap.
    - ``band`` (str, default ``"fire_temperature"``): which product to emit --
      one of:
        * ``"fire_temperature"`` -- the full Fire Temperature RGB (every warm
          pixel reds; the original product, unchanged).
        * ``"true_color"`` (aka ``"natural_color"`` / ``"geocolor_raw"``) -- the
          daytime TRUE COLOR RGB composited from the visible bands (R=C02 red,
          B=C01 blue, synthetic CIMSS green from C02/C03/C01) at the finer ~0.5 km
          native visible resolution. A natural-looking daytime base; goes black at
          night (no visible reflectance).
        * ``"fire_hotspots"`` -- the ISOLATED active-fire layer: a TRANSPARENT
          RGBA COG where ONLY pixels the active-fire discriminator flags are
          colored on an orange->yellow->white hot ramp (by C07 intensity) and
          every non-fire pixel is fully transparent (alpha 0). Overlays / "bakes"
          cleanly onto ANY base. Genuine active fire, NOT warm daytime land.
        * ``"fire_baked"`` -- the fire-only RGBA alpha-composited OVER the Fire
          Temperature base into ONE opaque RGB COG ("fire baked onto the
          satellite image").
    - ``bt_c07_min_k`` (float, default 320.0): the active-fire ABSOLUTE 3.9um
      (C07) brightness-temperature floor (K) used by the ``fire_hotspots`` /
      ``fire_baked`` discriminator. Tunable; lower to catch cooler/smaller fires.
    - ``bt_diff_min_k`` (float, default 10.0): the active-fire C07-minus-C13
      (3.9um - 10.3um) brightness-temperature DIFFERENCE floor (K). The
      shortwave-vs-longwave split that separates genuine fire from warm land.
      Tunable up (15-20 K) to demand a stronger fire signal.
    - ``true_color_res_deg`` (float | None, default None): output cell size in
      degrees for the ``true_color`` band ONLY. None -> the native ~0.5 km
      ``_TRUE_COLOR_RES_DEG`` (0.005 deg). Ignored for the thermal/fire bands,
      which always stay at the 2 km ``_OUT_RES_DEG`` (0.02 deg). A finer value
      gets its own cache namespace.

    **Active-fire detection (the ``fire_hotspots`` / ``fire_baked`` products):**
    A pixel is flagged active fire only when BOTH (C07 BT >= ``bt_c07_min_k``) AND
    (C07 - C13 BT >= ``bt_diff_min_k``). This is the standard GOES/MODIS-heritage
    shortwave-vs-longwave discriminator: a sub-pixel flame is intensely hot in the
    3.9um shortwave while the 10.3um longwave stays near ambient, so a large split
    uniquely separates real fire from uniformly warm land/cloud. C07 and C13 are
    both CMI bands in the SAME netCDF the Fire-Temp composite already downloads --
    NO extra fetch. The detection is self-contained (FIRMS is a separate VECTOR
    cross-reference, not needed for this raster product).

    **Returns:** an ORDERED ``list[LayerURI]`` (ascending UTC). For
    ``fire_temperature`` / ``fire_baked`` each is a 3-band uint8 RGB COG; for
    ``fire_hotspots`` each is a 4-band uint8 RGBA COG (alpha 0 off-fire).
    ``layer_type="raster"``, ``role="context"``, same ``bbox``; the RGB(A)
    passthrough in publish_layer renders the baked colors (and alpha) directly --
    no new style preset. The ``name`` is
    ``"GOES <product label> step <N> <ISO> (<SAT>)"`` -- the SAME scrubber-group
    contract ``fetch_goes_animation`` emits: the ``step <N>`` token is the
    monotonic frame value the web ``detectSequentialGroups`` parser keys on, the
    per-product label keeps each product in its own group, and the ISO valid-time
    is the per-frame display label.

    NOTE: an AOI / window with no archived frames raises a typed error (honesty
    floor) -- it never emits a blank animation.

    **Cross-tool dependencies:**
    - Upstream: ``fetch_wfigs_incident`` (the AOI bbox + the window floor).
    - Pairs with: ``fetch_firms_active_fire`` (historical-date hot-pixel overlay)
      + ``fetch_nifc_fire_perimeters`` (perimeter overlay).
    - Driven by: ``run_model_satellite_fire_animation`` (the historical GOES path).
    """
    q_bbox = _round_bbox(_validate_bbox(bbox))
    # Normalize any human/LLM spelling (GOES-18 / goes18 / G18 / "GOES West" / 18)
    # to the canonical "goes-NN" token BEFORE the allow-list check + before the
    # token is used to build any bucket/path/cache key. A truly-unknown bird raises
    # the shared loud typed error; a recognized-but-this-tool-doesn't-serve bird
    # still fails on the tool's OWN allow-list with the tool's OWN error type.
    satellite = _normalize_satellite(satellite)
    if satellite not in GOES_ARCHIVE_SATELLITES:
        raise GOESArchiveInputError(
            f"unknown satellite={satellite!r}; allowed: "
            f"{list(GOES_ARCHIVE_SATELLITES)}"
        )
    # Normalize LLM-invented aliases (natural_color / geocolor_raw -> true_color)
    # before the band check so a natural request still routes.
    if isinstance(band, str):
        band = _BAND_ALIASES.get(band, band)
    if band not in ARCHIVE_BANDS:
        raise GOESArchiveInputError(
            f"unknown band/product={band!r}; the raw-archive path supports "
            f"{list(ARCHIVE_BANDS)} (proprietary CIRA GeoColor -- use "
            "fetch_goes_animation for the recent GeoColor loop)"
        )
    if true_color_res_deg is not None:
        try:
            true_color_res_deg = float(true_color_res_deg)
        except (TypeError, ValueError):
            raise GOESArchiveInputError(
                f"true_color_res_deg must be numeric; got {true_color_res_deg!r}"
            )
        if not math.isfinite(true_color_res_deg) or true_color_res_deg <= 0.0:
            raise GOESArchiveInputError(
                "true_color_res_deg must be a positive finite degree value"
            )
    # Resolution is a USER lever: thermal/fire bands stay pinned to _OUT_RES_DEG
    # (byte-identical to today); true_color uses the override or _TRUE_COLOR_RES_DEG.
    res_deg = _resolve_res_deg(band, true_color_res_deg)
    try:
        bt_c07_min_k = float(bt_c07_min_k)
        bt_diff_min_k = float(bt_diff_min_k)
    except (TypeError, ValueError):
        raise GOESArchiveInputError(
            f"bt_c07_min_k / bt_diff_min_k must be numeric; got "
            f"{bt_c07_min_k!r} / {bt_diff_min_k!r}"
        )
    if not (math.isfinite(bt_c07_min_k) and math.isfinite(bt_diff_min_k)):
        raise GOESArchiveInputError(
            "bt_c07_min_k / bt_diff_min_k must be finite"
        )

    # Resolve the window. Default: most-recent ~6.5h ending now (UTC).
    now = datetime.now(timezone.utc)
    end_dt = _parse_utc(end_utc) if end_utc else now
    start_dt = _parse_utc(start_utc) if start_utc else (end_dt - timedelta(hours=6, minutes=30))
    if start_dt >= end_dt:
        raise GOESArchiveInputError(
            f"start_utc ({start_dt.isoformat()}) must be before end_utc "
            f"({end_dt.isoformat()})"
        )

    # 1. List the in-window MCMIPC keys + even-subsample to the frame cap.
    pairs = _list_archive_keys_in_window(satellite, start_dt, end_dt)
    if not pairs:
        raise GOESArchiveEmptyError(
            f"no MCMIPC frames in the noaa-{satellite.replace('-', '')} archive for "
            f"window {_iso_z(start_dt)}..{_iso_z(end_dt)} -- the date may pre-date "
            f"the {satellite} operational record or fall in an ingest gap"
        )
    keys_only = [k for _, k in pairs]
    kept_keys = set(_select_window_keys(keys_only, cap=MAX_ARCHIVE_FRAMES))
    frames = [(t, k) for (t, k) in pairs if k in kept_keys]

    sat_label = satellite.upper()
    product_label = _PRODUCT_LABELS[band]
    product_preset = _PRODUCT_STYLE_PRESETS[band]
    product_slug = _PRODUCT_ID_SLUGS[band]
    # The hotspots / baked products are threshold-dependent, so the detection
    # thresholds enter the cache key (a different threshold yields a different
    # COG). Fire-Temp ignores them but they stay constant there, so its key is
    # unaffected versus the pre-change params (the gamma=1 entry kept the old key
    # stable; band/thresholds are NEW additive entries -- a fresh cache namespace
    # for the new products, no collision with the old Fire-Temp objects because
    # 'product' now carries the band).

    # 2. Per-frame fetch (one read_through each -> independent cache key).
    layers: list[LayerURI] = []
    n_empty = 0
    last_err: Exception | None = None
    for frame_no, (t, key) in enumerate(frames, start=1):
        iso = _iso_z(t)
        ts_tag = t.strftime("%Y%m%d%H%M%S")
        params = {
            "bbox": list(q_bbox),
            "product": band,
            "satellite": satellite,
            "ts_start": ts_tag,
            "gamma": 1,
            # Resolution is part of the cache key so finer frames get a distinct
            # namespace. The thermal/fire bands resolve to _OUT_RES_DEG (0.02) so
            # their key is the SAME value those bands always carried implicitly --
            # the round(res_deg,6) entry is constant 0.02 for them, byte-stable.
            "res_deg": round(res_deg, 6),
        }
        if band in ("fire_hotspots", "fire_baked"):
            params["bt_c07_min_k"] = round(bt_c07_min_k, 3)
            params["bt_diff_min_k"] = round(bt_diff_min_k, 3)
        try:
            result = read_through(
                metadata=_METADATA,
                params=params,
                ext="tif",
                fetch_fn=lambda s=satellite, k=key: _fetch_archive_frame_cog_bytes(
                    s, k, q_bbox, band, bt_c07_min_k, bt_diff_min_k, res_deg
                ),
            )
        except GOESArchiveEmptyError as exc:
            n_empty += 1
            last_err = exc
            logger.warning(
                "fetch_goes_archive_animation: empty frame ts=%s skipped (%s)",
                iso,
                exc,
            )
            continue
        except GOESArchiveUpstreamError as exc:
            n_empty += 1
            last_err = exc
            logger.warning(
                "fetch_goes_archive_animation: frame ts=%s upstream-failed (%s)",
                iso,
                exc,
            )
            continue
        assert result.uri is not None
        # NAME token = "GOES <product label> step <N> <ISO> (<SAT>)".
        # The "step <N>" token is the MONOTONIC frame value the web
        # detectSequentialGroups parser keys on; the per-product "(Archive)"
        # label keeps each product in its OWN scrubber group; the ISO valid-time
        # is the per-frame display label.
        layers.append(
            LayerURI(
                layer_id=f"goes-arch-{product_slug}-{ts_tag}-{q_bbox[0]:.3f}-{q_bbox[1]:.3f}",
                name=f"GOES {product_label} step {frame_no} {iso} ({sat_label})",
                layer_type="raster",
                uri=result.uri,
                style_preset=product_preset,
                role="context",
                units=None,
                bbox=q_bbox,
            )
        )

    # Honesty floor: a run that produced NO frames is not success.
    if not layers:
        raise GOESArchiveEmptyError(
            f"every one of {len(frames)} archive {product_label} frames was "
            f"empty/failed for {satellite} over the AOI"
            + (f": {last_err}" if last_err else "")
        )
    logger.info(
        "fetch_goes_archive_animation: %d %s frames (%d empty skipped) for %s "
        "archive window %s..%s",
        len(layers),
        product_label,
        n_empty,
        satellite,
        _iso_z(start_dt),
        _iso_z(end_dt),
    )
    return layers
