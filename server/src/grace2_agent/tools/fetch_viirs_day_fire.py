"""``fetch_viirs_day_fire`` atomic tool -- JPSS / VIIRS Day Fire polar animation frames (fire demo J3, the core net-new).

PATH A (ready-made CIRA Polar SLIDER, sat=jpss). Builds an ORDERED list of
per-overpass EPSG:4326 RGB COGs of the VIIRS Day Fire product over a multi-day
window for an AOI -- the exact product + irregular polar cadence the CIRA
cira_csu Day Fire animations are made from. This is the POLAR analogue of the
geostationary ``fetch_goes_animation``: instead of a smooth 5-minute cadence it
enumerates the IRREGULAR polar overpass timestamps from the SLIDER jpss time
index (each timestamp directory = one overpass), keeps DAY-only passes (Day Fire
is a daytime product -- the green/blue channels are reflectance and go black at
night), and emits one frame per pass labelled with its REAL irregular UTC pass
time.

CONFIRMED SLIDER facts (define-products.js + live probes 2026-06-22):
- sat slug = ``jpss``; the jpss time index is ALREADY the merged multi-satellite
  polar pass list (SUOMI-NPP + NOAA-20 + NOAA-21), each ``timestamps_int``
  directory = one overpass. The exact bird per pass is NOT exposed in the SLIDER
  tile path, so ``satellite`` selects the conceptual subset and the per-frame
  label records the requested satellite filter (or 'jpss' for the merged set);
  it is NOT a per-pass bird tag (LIVE-VERIFY the per-bird attribution if exact
  attribution is required -- the FIRMS overlay carries the true per-detection
  satellite field for cross-check).
- Day Fire RGB product slug = ``cira_natural_fire_color`` (title "Day Fire
  (CIRA)") -- CONFIRMED LIVE. (The 375 m native fire product is
  ``cira_hires_fire_temperature``; GeoColor is ``cira_geocolor``.)

Day Fire RGB recipe (for reference -- the SLIDER product is pre-rendered, we do
NOT composite it): R = VIIRS 3.7um BT (0-60 C, gamma 0.4); G = 0.86um NIR refl
(0-100%); B = 0.64um visible refl (0-100%). Thermal-red fire over a near-true-
color land/veg/smoke base; near-black sea (so the island + fire pop). PATH B (raw
VIIRS L1b swath resample) is the optional 375 m full-control fallback noted in
the spike J4 -- left as a commented seam, NOT built here.

Georeferencing is the APPROXIMATE SLIDER sector-extent mapping documented in
``_satellite_slider`` (SLIDER ships no projection; the JPSS polar remap is not a
published projection, so it is approximate-only -- LIVE-VERIFY). The imagery +
irregular cadence are the real CIRA product. The honesty floor holds: a run that
produced no frames does NOT report success.

Cache key (per frame): SHA-256 over ``(bbox-6dp, product, sector, ts_int,
zoom)``.

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
from ._satellite_slider import (
    SliderEmptyError,
    SliderError,
    SliderUpstreamError,
    fetch_slider_timestamps,
    mosaic_to_cog_bytes,
    pick_zoom_for_aoi,
    stitch_slider_mosaic,
    ts_int_to_datetime,
    ts_int_to_iso,
)

__all__ = [
    "fetch_viirs_day_fire",
    "VIIRSDayFireError",
    "VIIRSDayFireInputError",
    "VIIRSDayFireBboxRequiredError",
    "VIIRSDayFireUpstreamError",
    "VIIRSDayFireEmptyError",
    "VIIRS_SATELLITES",
    "VIIRS_PRODUCTS",
    "DAY_FIRE_PRODUCT_SLUG",
    "MAX_VIIRS_FRAMES",
    "_parse_utc",
    "_is_daytime_pass",
    "_build_pass_list",
]

logger = logging.getLogger("grace2_agent.tools.fetch_viirs_day_fire")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class VIIRSDayFireError(RuntimeError):
    """Base class for fetch_viirs_day_fire failures."""

    error_code: str = "VIIRS_DAY_FIRE_ERROR"
    retryable: bool = True


class VIIRSDayFireInputError(VIIRSDayFireError):
    """Invalid input (unknown satellite/product, bad window)."""

    error_code = "VIIRS_DAY_FIRE_INPUT_INVALID"
    retryable = False


class VIIRSDayFireBboxRequiredError(VIIRSDayFireError):
    """bbox is required."""

    error_code = "BBOX_REQUIRED"
    retryable = False


class VIIRSDayFireUpstreamError(VIIRSDayFireError):
    """SLIDER time-index or tile fetch failed."""

    error_code = "VIIRS_DAY_FIRE_UPSTREAM_ERROR"
    retryable = True


class VIIRSDayFireEmptyError(VIIRSDayFireError):
    """No daytime passes in the window, or every pass crop was empty."""

    error_code = "VIIRS_DAY_FIRE_EMPTY"
    retryable = False


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------

#: Conceptual JPSS satellite subsets. 'all' = the merged SLIDER jpss pass list.
VIIRS_SATELLITES = ("suomi-npp", "noaa-20", "noaa-21", "all")

#: VIIRS Day Fire product slug on the CIRA Polar SLIDER (CONFIRMED LIVE).
DAY_FIRE_PRODUCT_SLUG = "cira_natural_fire_color"

#: product name -> SLIDER jpss product slug.
_PRODUCT_TO_SLUG: dict[str, str] = {
    "day_fire": DAY_FIRE_PRODUCT_SLUG,
}

VIIRS_PRODUCTS = tuple(_PRODUCT_TO_SLUG.keys())

#: Shared style preset (RGB COG -> publish_layer multiband passthrough).
_VIIRS_ANIM_STYLE_PRESET = "viirs_day_fire_animation"

#: Local-solar-time window (hours) treated as a DAY pass. JPSS daytime overpasses
#: cross the equator ~13:30 local solar time; a generous 06:00-19:00 LST window
#: keeps the daytime ascending passes and drops the ~01:30 LST night passes.
_DAY_LST_START_H = 6.0
_DAY_LST_END_H = 19.0

#: Upper bound on emitted frames (mirrors postprocess_flood.MAX_FLOOD_FRAMES).
#: A 4-day window of ~9-12 daytime passes/day ~= 36-48 frames sits well under
#: this cap. Overridable via env.
MAX_VIIRS_FRAMES: int = int(os.environ.get("GRACE2_MAX_VIIRS_FRAMES", "144"))

_BBOX_QUANTIZE_DP = 6


# ---------------------------------------------------------------------------
# AtomicToolMetadata.
# ---------------------------------------------------------------------------


def _build_metadata() -> AtomicToolMetadata:
    common = dict(
        name="fetch_viirs_day_fire",
        ttl_class="dynamic-1h",
        source_class="viirs_satellite",
        cacheable=True,
    )
    try:
        return AtomicToolMetadata(**common, supports_global_query=False)  # type: ignore[call-arg]
    except Exception:
        return AtomicToolMetadata(**common)


_METADATA = _build_metadata()


# ---------------------------------------------------------------------------
# Pure helpers (also importable for tests).
# ---------------------------------------------------------------------------


def _parse_utc(value: Any) -> datetime:
    """Parse an ISO-8601 string / datetime -> aware UTC. Raises VIIRSDayFireInputError."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        raise VIIRSDayFireInputError(f"time must be an ISO-8601 string or datetime; got {value!r}")
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
            raise VIIRSDayFireInputError(
                f"could not parse UTC time {value!r}; use ISO-8601 "
                "(e.g. '2026-05-15T20:47:00Z')"
            )
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local_solar_hour(dt_utc: datetime, lon: float) -> float:
    """Approximate local-solar-time hour-of-day at longitude ``lon`` for a UTC time.

    Local solar time = UTC + lon/15 hours (lon east positive). Returns a value in
    [0, 24). Used to keep DAY-only VIIRS passes (Day Fire is a daytime product).
    """
    utc_hours = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0
    lst = (utc_hours + lon / 15.0) % 24.0
    return lst


def _is_daytime_pass(ts_int: int, aoi_center_lon: float) -> bool:
    """True iff the overpass is during local DAYTIME at the AOI longitude.

    Day Fire's green/blue channels are reflectance -> black at night, so night
    passes carry no usable imagery and are dropped (matches the CIRA day-only
    caption). The day window is the local-solar-time band [06:00, 19:00).
    """
    lst = _local_solar_hour(ts_int_to_datetime(ts_int), aoi_center_lon)
    return _DAY_LST_START_H <= lst < _DAY_LST_END_H


def _select_frame_indices(n: int, cap: int = MAX_VIIRS_FRAMES) -> list[int]:
    """Pick up to ``cap`` evenly-spaced indices over ``n``, endpoints kept.

    Mirrors ``postprocess_flood._select_frame_time_indices`` -- a safety cap only
    (polar pass counts are small, so this rarely fires).
    """
    if n <= 0:
        return []
    if n <= cap:
        return list(range(n))
    import numpy as np

    idx = np.linspace(0, n - 1, cap).round().astype(int)
    kept = [int(i) for i in np.unique(idx)]
    logger.info(
        "fetch_viirs_day_fire: %d daytime passes exceed cap=%d; subsampling to %d.",
        n,
        cap,
        len(kept),
    )
    return kept


def _build_pass_list(
    timestamps_int: list[int],
    start_utc: datetime,
    end_utc: datetime,
    aoi_center_lon: float,
    *,
    day_only: bool = True,
    cap: int = MAX_VIIRS_FRAMES,
) -> list[int]:
    """Window + day-filter + merge/sort the SLIDER jpss pass timestamps.

    ``timestamps_int`` is the ascending SLIDER jpss ``timestamps_int`` list (the
    already-merged multi-satellite overpass set). Returns the ORDERED (ascending)
    list of selected overpass ints inside [start, end], keeping DAY-only passes
    when ``day_only`` (the Day Fire default). Pure function.
    """
    in_window = [
        ts for ts in timestamps_int if start_utc <= ts_int_to_datetime(ts) <= end_utc
    ]
    if day_only:
        in_window = [ts for ts in in_window if _is_daytime_pass(ts, aoi_center_lon)]
    in_window.sort()
    keep = _select_frame_indices(len(in_window), cap=cap)
    return [in_window[i] for i in keep]


def _validate_bbox(bbox: Any) -> tuple[float, float, float, float]:
    if bbox is None:
        raise VIIRSDayFireBboxRequiredError(
            "bbox is required for fetch_viirs_day_fire; pass "
            "(min_lon, min_lat, max_lon, max_lat)."
        )
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        raise VIIRSDayFireInputError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat); got {bbox!r}"
        )
    vals = tuple(float(v) for v in bbox)
    if not all(math.isfinite(v) for v in vals):
        raise VIIRSDayFireInputError(f"bbox contains non-finite values: {bbox!r}")
    min_lon, min_lat, max_lon, max_lat = vals
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise VIIRSDayFireInputError(f"bbox lon out of [-180,180]: {bbox!r}")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise VIIRSDayFireInputError(f"bbox lat out of [-90,90]: {bbox!r}")
    if min_lon >= max_lon or min_lat >= max_lat:
        raise VIIRSDayFireInputError(f"bbox is degenerate (min<max on both axes): {bbox!r}")
    return (min_lon, min_lat, max_lon, max_lat)


def _round_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return tuple(round(v, _BBOX_QUANTIZE_DP) for v in bbox)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Per-frame fetch (the read_through fetch_fn).
# ---------------------------------------------------------------------------


def _fetch_frame_cog_bytes(
    sector: str,
    product_slug: str,
    ts_int: int,
    zoom: int,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Stitch + reproject one VIIRS overpass -> 3-band EPSG:4326 RGB COG bytes."""
    rgb, mosaic_extent = stitch_slider_mosaic("jpss", sector, product_slug, ts_int, zoom, bbox)
    return mosaic_to_cog_bytes(rgb, mosaic_extent, bbox)


# ---------------------------------------------------------------------------
# Registered atomic tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # readOnlyHint=True, openWorldHint=True (external SLIDER tiles),
    # destructiveHint=False, idempotentHint=True (per-frame cache dedupes).
    open_world_hint=True,
)
def fetch_viirs_day_fire(
    bbox: tuple[float, float, float, float],
    satellite: str = "all",
    product: str = "day_fire",
    sector: str = "conus",
    start_utc: str | None = None,
    end_utc: str | None = None,
    day_only: bool = True,
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> list[LayerURI]:
    """Build a VIIRS Day Fire JPSS polar animation (ordered per-overpass RGB COGs) over a multi-day window.

    **What it does:** Pulls the ready-made CIRA Polar SLIDER VIIRS Day Fire RGB
    imagery (sat=jpss) for an AOI over a multi-day UTC window and returns an
    ORDERED list of per-OVERPASS EPSG:4326 RGB COGs -- one frame per polar pass,
    DAY-only by default, each labelled with its REAL irregular UTC pass time.
    This is how you RECREATE a CIRA-style multi-day VIIRS Day Fire animation (the
    polar analogue of ``fetch_goes_animation``): the imagery + irregular polar
    cadence are the exact product the CIRA loops are made from. The Day Fire RGB
    shows thermal-red fire over a near-true-color land/veg/smoke base, so the
    fire pops against a near-black sea (ideal for an offshore AOI like the
    Channel Islands).

    **When to use:**
    - "Recreate the JPSS / VIIRS Day Fire animation over this 4-day window",
      "animate the fire day-by-day from the polar satellites", "VIIRS Day Fire
      passes over the Channel Islands".
    - Any MULTI-DAY polar (LEO) fire timelapse, especially offshore AOIs where
      VIIRS 375 m beats the edge-of-good-geometry GOES sector.

    **When NOT to use:**
    - An intra-day GEOSTATIONARY 5-minute loop (use ``fetch_goes_animation``).
    - Active-fire pixel detections (``fetch_firms_active_fire``) -- though that is
      the same VIIRS instrument and co-registers as a hot-pixel overlay.
    - Fire perimeters (``fetch_nifc_fire_perimeters``).

    **Parameters:**
    - ``bbox`` (tuple): ``(min_lon, min_lat, max_lon, max_lat)`` EPSG:4326.
      Required.
    - ``satellite`` (str, default ``"all"``): ``"all"`` (the merged SLIDER jpss
      pass set), or ``"suomi-npp"`` / ``"noaa-20"`` / ``"noaa-21"`` (the SLIDER
      jpss index does not tag the per-pass bird, so a specific value records the
      requested filter in the frame label; use FIRMS for true per-detection
      satellite attribution).
    - ``product`` (str, default ``"day_fire"``): the VIIRS Day Fire RGB.
    - ``sector`` (str, default ``"conus"``): SLIDER jpss sector slug.
    - ``start_utc`` / ``end_utc`` (str): ISO-8601 UTC window bounds
      (e.g. ``"2026-05-15T20:47:00Z"`` .. ``"2026-05-19T22:01:00Z"``). When
      omitted, the most-recent 4 days are used.
    - ``day_only`` (bool, default True): keep only DAYTIME passes (Day Fire is a
      daytime product; night passes are black).

    **Returns:** an ORDERED ``list[LayerURI]`` (ascending UTC). Each is a 3-band
    uint8 RGB COG (``layer_type="raster"``, ``role="context"``,
    ``style_preset="viirs_day_fire_animation"``, same ``bbox``) whose ``name`` is
    ``"VIIRS Day Fire step <N> <ISO> (<SAT>)"`` -- the scrubber-group contract:
    ``step <N>`` is the monotonic frame value the web parser keys on (the
    irregular polar-pass ISO alone is not a recognized token), and the ISO pass
    time is the per-frame display label. Frames are NOT evenly spaced; the labels
    carry the real pass times.

    NOTE: georeferencing is the approximate SLIDER sector-extent mapping (the
    JPSS polar remap has no published projection -- approximate only); the
    imagery + irregular cadence are the real CIRA product. A pass with no imagery
    over the AOI is skipped; a run that produced NO frames raises a typed error
    (honesty floor).

    **Cross-tool dependencies:**
    - Upstream: ``fetch_wfigs_incident`` (resolve the offshore island by NAME ->
      AOI bbox + the window floor).
    - Pairs with: ``fetch_firms_active_fire`` (same VIIRS instrument; the hot
      pixels co-register with the Day Fire red pixels) + ``fetch_nifc_fire_perimeters``.
    - Driven by: ``run_model_satellite_fire_animation``.
    """
    q_bbox = _round_bbox(_validate_bbox(bbox))
    if satellite not in VIIRS_SATELLITES:
        raise VIIRSDayFireInputError(
            f"unknown satellite={satellite!r}; allowed: {list(VIIRS_SATELLITES)}"
        )
    product_slug = _PRODUCT_TO_SLUG.get(product)
    if product_slug is None:
        raise VIIRSDayFireInputError(
            f"unknown product={product!r}; allowed: {sorted(_PRODUCT_TO_SLUG)}"
        )

    now = datetime.now(timezone.utc)
    end_dt = _parse_utc(end_utc) if end_utc else now
    start_dt = _parse_utc(start_utc) if start_utc else (end_dt - timedelta(days=4))
    if start_dt >= end_dt:
        raise VIIRSDayFireInputError(
            f"start_utc ({start_dt.isoformat()}) must be before end_utc "
            f"({end_dt.isoformat()})"
        )

    aoi_center_lon = (q_bbox[0] + q_bbox[2]) / 2.0

    # 1. Read the SLIDER jpss time index + window + day-filter + merge/sort.
    try:
        all_ts = fetch_slider_timestamps("jpss", sector, product_slug)
    except SliderError as exc:
        raise VIIRSDayFireUpstreamError(str(exc)) from exc
    pass_ts = _build_pass_list(
        all_ts, start_dt, end_dt, aoi_center_lon, day_only=day_only, cap=MAX_VIIRS_FRAMES
    )
    if not pass_ts:
        raise VIIRSDayFireEmptyError(
            f"no {'daytime ' if day_only else ''}VIIRS Day Fire passes for jpss/"
            f"{sector} in window {start_dt.isoformat()}..{end_dt.isoformat()} "
            f"(index has {len(all_ts)} timestamps)"
        )

    zoom = pick_zoom_for_aoi("jpss", sector, q_bbox)
    sat_label = "JPSS" if satellite == "all" else satellite.upper()

    # 2. Per-pass fetch (one read_through each -> independent cache key).
    layers: list[LayerURI] = []
    n_empty = 0
    last_err: SliderError | None = None
    for frame_no, ts_int in enumerate(pass_ts, start=1):
        iso = ts_int_to_iso(ts_int)
        params = {
            "bbox": list(q_bbox),
            "product": product_slug,
            "sector": sector,
            "ts_int": ts_int,
            "zoom": zoom,
        }
        try:
            result = read_through(
                metadata=_METADATA,
                params=params,
                ext="tif",
                fetch_fn=lambda t=ts_int: _fetch_frame_cog_bytes(
                    sector, product_slug, t, zoom, q_bbox
                ),
            )
        except SliderEmptyError as exc:
            # A pass that did not see the AOI (edge-of-swath / no coverage) is
            # skipped, not fatal -- polar coverage is naturally sparse.
            n_empty += 1
            last_err = exc
            logger.warning("fetch_viirs_day_fire: empty pass ts=%s skipped (%s)", iso, exc)
            continue
        except SliderUpstreamError as exc:
            n_empty += 1
            last_err = exc
            logger.warning("fetch_viirs_day_fire: pass ts=%s upstream-failed (%s)", iso, exc)
            continue
        assert result.uri is not None
        # NAME token = "VIIRS Day Fire step <N> <ISO> (<SAT>)". The "step <N>"
        # token is the MONOTONIC frame value the web detectSequentialGroups parser
        # keys on (the irregular polar-pass ISO alone is NOT a recognized token,
        # so without it no scrubber group forms); the ISO valid-time is kept as
        # the per-frame display label. ``frame_no`` is the position in the
        # day-filtered pass list (passes are NOT evenly spaced), giving a clean
        # ascending series for the single polar product.
        layers.append(
            LayerURI(
                layer_id=f"viirs-dayfire-{ts_int}-{q_bbox[0]:.3f}-{q_bbox[1]:.3f}",
                name=f"VIIRS Day Fire step {frame_no} {iso} ({sat_label})",
                layer_type="raster",
                uri=result.uri,
                style_preset=_VIIRS_ANIM_STYLE_PRESET,
                role="context",
                units=None,
                bbox=q_bbox,
            )
        )

    # Honesty floor: a run that produced NO frames is not success.
    if not layers:
        raise VIIRSDayFireEmptyError(
            f"every one of {len(pass_ts)} VIIRS Day Fire passes was empty/failed "
            f"over the AOI"
            + (f": {last_err}" if last_err else "")
        )
    logger.info(
        "fetch_viirs_day_fire: %d Day Fire passes (%d empty skipped) for jpss/%s "
        "window %s..%s day_only=%s zoom=%d",
        len(layers),
        n_empty,
        sector,
        start_dt.isoformat(),
        end_dt.isoformat(),
        day_only,
        zoom,
    )
    return layers


# ---------------------------------------------------------------------------
# PATH B (raw VIIRS L1b swath Day Fire composite) SEAM.
#
# The spike J4 raw-band Day Fire path is intentionally NOT built here. It would
# read the noaa-nesdis-{snpp,n20,n21}-pds VIIRS I04 (3.7um BT, R, 0-60 C gamma
# 0.4) / I02 (0.86um NIR, G, 0-100%) / I01 (0.64um visible, B, 0-100%) bands,
# geolocate + EWA-resample the SWATH (Polar2Grid-style) to EPSG:4326 -- the
# geostationary-CRS assumption does NOT apply to a curved polar swath -- and
# stack into a 3-band uint8 COG. Only needed for 375 m I-band control beyond
# SLIDER. Left as a documented seam; PATH A (SLIDER) is the recommended demo
# path.
# ---------------------------------------------------------------------------
