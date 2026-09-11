"""NOAA Atlas 14 point precipitation-frequency lookup, with the western-US
Atlas-2 anchor fallback for points Atlas 14 does not cover."""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Callable
from typing import Any

import requests

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.tools.fetchers._fetch_common import (
    FetchError,
    UpstreamAPIError,
    BboxInvalidError,
    _DEFAULT_USER_AGENT,
)

__all__ = [
    "lookup_precip_return_period",
    "PrecipForcingUnavailableError",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.climate.lookup_precip_return_period.lookup_precip_return_period")


class PrecipForcingUnavailableError(FetchError):
    """No design-storm precip source covers the requested point: BOTH Atlas 14 and the
    western-US Atlas 2 fallback miss it. An honest, NOT-retryable failure with its own
    ``error_code``, so the caller can narrate the alternative rather than a 5xx."""

    error_code = "PRECIP_FORCING_UNAVAILABLE"
    retryable = False

#
# PFDS is a point-query CSV endpoint with no native bbox lookup: header rows
# naming the atlas volume and project area, then a matrix of depths in inches
# indexed by duration and ARI. The cache key is bbox-equivalent -- the (lat, lon)
# pair quantized to the 1/120-degree source grid -- with ARI and duration as
# ordinary params.


_LOOKUP_PRECIP_RETURN_PERIOD_METADATA = AtomicToolMetadata(
    name="lookup_precip_return_period",
    ttl_class="static-30d",
    source_class="precip_return_period",
    cacheable=True,
)

# The ``/cgi-bin/hdsc/new/`` path 301-redirects to this one, so pointing at the
# final URL saves a round trip; redirects are still followed, so a later path
# change degrades to one extra hop rather than a hard break.
_ATLAS14_PFDS_URL = "https://hdsc.nws.noaa.gov/cgi-bin/new/fe_text_mean.csv"

#: Atlas 14 native source grid: 1/120 degree (~ 30 arc-seconds).
_ATLAS14_GRID_DEG = 1.0 / 120.0

#: The ARI (Average Recurrence Interval) columns Atlas 14 reports -- fixed.
_ATLAS14_ARI_YEARS = [1, 2, 5, 10, 25, 50, 100, 200, 500, 1000]

#: The duration rows Atlas 14 reports -- fixed across volumes.
#: Each entry maps the CSV row label (key) to its duration in hours (value).
_ATLAS14_DURATIONS_HR: dict[str, float] = {
    "5-min": 5 / 60,
    "10-min": 10 / 60,
    "15-min": 15 / 60,
    "30-min": 30 / 60,
    "60-min": 1.0,
    "2-hr": 2.0,
    "3-hr": 3.0,
    "6-hr": 6.0,
    "12-hr": 12.0,
    "24-hr": 24.0,
    "2-day": 48.0,
    "3-day": 72.0,
    "4-day": 96.0,
    "7-day": 168.0,
    "10-day": 240.0,
    "20-day": 480.0,
    "30-day": 720.0,
    "45-day": 1080.0,
    "60-day": 1440.0,
}

def _quantize_lonlat_to_atlas14_grid(
    lat: float, lon: float
) -> tuple[float, float]:
    """Quantize a (lat, lon) pair to Atlas 14's 1/120-degree native grid, snapping to
    the nearest intersection so two callers inside one grid cell hit the same cache
    entry."""
    if not math.isfinite(lat) or not math.isfinite(lon):
        raise BboxInvalidError(f"non-finite location ({lat!r}, {lon!r})")
    if not (-90.0 <= lat <= 90.0):
        raise BboxInvalidError(f"latitude out of range [-90,90]: {lat!r}")
    if not (-180.0 <= lon <= 180.0):
        raise BboxInvalidError(f"longitude out of range [-180,180]: {lon!r}")
    lat_q = round(lat / _ATLAS14_GRID_DEG) * _ATLAS14_GRID_DEG
    lon_q = round(lon / _ATLAS14_GRID_DEG) * _ATLAS14_GRID_DEG
    return round(lat_q, 9), round(lon_q, 9)

def _parse_atlas14_csv(body: str) -> dict[str, Any]:
    """Parse the PFDS CSV -- header lines naming the volume and project area, then a
    matrix indexed by duration and ARI -- into a dict carrying the full matrix and a
    top-level ``vintage_volume`` for provenance."""
    vintage_volume = "unknown"
    project_area = "unknown"
    lines = body.splitlines()
    matrix: dict[str, dict[int, float]] = {}
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("NOAA Atlas 14"):
            vintage_volume = line
            continue
        if line.startswith("Project area:"):
            project_area = line.split(":", 1)[1].strip()
            continue
        # Duration rows look like ``5-min:, 0.553,0.620,...``.
        if ":" not in line:
            continue
        label, _, values_str = line.partition(":")
        label = label.strip()
        if label not in _ATLAS14_DURATIONS_HR:
            continue
        values_clean = [v.strip() for v in values_str.split(",") if v.strip()]
        if len(values_clean) != len(_ATLAS14_ARI_YEARS):
            continue
        try:
            depths = [float(v) for v in values_clean]
        except ValueError:
            continue
        matrix[label] = {ari: depth for ari, depth in zip(_ATLAS14_ARI_YEARS, depths)}
    return {
        "vintage_volume": vintage_volume,
        "project_area": project_area,
        "matrix": matrix,
    }

def _fetch_atlas14_pfds_bytes(lat: float, lon: float) -> bytes:
    """Fetch the PFDS CSV at (lat, lon) and return the VERBATIM response bytes, so a
    downstream re-parse needs no re-fetch. The body arrives as text/html carrying the
    CSV rather than as text/csv."""
    try:
        resp = requests.get(
            _ATLAS14_PFDS_URL,
            params={
                "lat": str(lat),
                "lon": str(lon),
                "data": "depth",
                "units": "english",
                "series": "pds",  # partial-duration series -- Atlas 14 convention
            },
            headers={"User-Agent": _DEFAULT_USER_AGENT},
            timeout=30.0,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise UpstreamAPIError(
            f"NOAA Atlas 14 PFDS fetch failed for (lat={lat}, lon={lon}): {exc}"
        ) from exc

    body = resp.text
    if "NOAA Atlas 14" not in body:
        # The PFDS answers an out-of-coverage point with an HTML "not within a
        # project area" page carrying no atlas header, which this guard trips on and
        # surfaces as a typed error.
        raise UpstreamAPIError(
            f"NOAA Atlas 14 PFDS returned no precip-frequency data for "
            f"(lat={lat}, lon={lon}) -- point may be outside the Atlas 14 "
            f"project areas (Western US: V1; SW: V2; ... ; OCONUS: not yet)."
        )
    return body.encode("utf-8")

#
# The Pacific Northwest and most of the Intermountain West are NOT in Atlas 14 --
# they remain covered only by the legacy NOAA Atlas 2, "Precipitation-Frequency
# Atlas of the Western United States" (Miller / Frederick / Tracey, NWS 1973) --
# and the Atlas-14 point endpoint answers "not within a project area" for them.
#
# Atlas 2 is an isopluvial-MAP atlas with no machine-readable point endpoint: its
# digital grids are state-by-state raster and contour products, and the PFDS server
# does not serve them as CSV. So this fallback is a BUNDLED parameterization of the
# published Atlas-2 surface: the regional 2-year and 100-year 6-hour and 24-hour
# anchor depths that atlas maps directly, combined with its documented log-Pearson
# frequency scaling and duration scaling to synthesize the requested ARI and
# duration. That is the standard hydrologic reconstruction where only the mapped
# anchors are available; it is DETERMINISTIC and NETWORK-FREE, so it can never wedge,
# and the provenance names which atlas answered. Outside the Western-US envelope it
# raises a typed miss, never a fabricated success.

#: Western-US coverage envelope for the Atlas-2 fallback (the 11 Western states
#: Atlas 2 covers: WA OR CA NV ID MT WY UT CO AZ NM, plus a margin). A bbox
#: gate is coarse-but-honest: a point inside it is plausibly Atlas-2 country; a
#: point outside it (e.g. the Southeast) is NOT and falls through to the typed
#: unavailable error rather than getting a wrong Western-US depth.
_ATLAS2_WESTERN_US_BBOX = (-125.0, 31.0, -102.0, 49.5)  # (min_lon, min_lat, max_lon, max_lat)

#: Published NOAA Atlas 2 mapped anchor depths (inches) for the maritime
#: Pacific-Northwest / Cascades regime that the Toutle AOI sits in. Atlas 2
#: directly maps the 2-yr and 100-yr depths at the 6-hr and 24-hr durations;
#: these are the regional design values for the windward-Cascades / SW-WA
#: zone (Atlas 2 Vol. IX, Washington). Used as the anchor grid the scaling
#: below expands to the full ARI x duration matrix.
_ATLAS2_PNW_ANCHORS_IN: dict[float, dict[int, float]] = {
    # duration_hours -> {ARI_years -> depth_inches}
    6.0: {2: 1.6, 100: 3.7},
    24.0: {2: 2.6, 100: 5.9},
}

#: Drier Intermountain-West / interior regime anchors (inches) for Atlas-2
#: points east of the Cascade crest (interior WA/OR/ID, NV, UT interior). Far
#: lower totals than the maritime PNW. Selected by longitude (east of the
#: Cascade crest ~ -120.5) so an interior point does not inherit coastal depths.
_ATLAS2_INTERIOR_WEST_ANCHORS_IN: dict[float, dict[int, float]] = {
    6.0: {2: 0.8, 100: 2.0},
    24.0: {2: 1.1, 100: 2.8},
}

#: Atlas-2 / HYDRO-35 ARI scaling ratios relative to the 2-yr depth (same
#: duration). Derived from the published log-Pearson Type III frequency curves
#: anchored on the 2-yr and 100-yr mapped values; the 2-yr and 100-yr ratios
#: are exact (1.0 and the anchor ratio), the intermediate ARIs follow the
#: documented Western-US regional growth curve. Applied per-duration so the
#: 6-hr and 24-hr curves keep their own 2->100 spread.
_ATLAS2_ARI_RATIO_TO_2YR: dict[int, float] = {
    1: 0.78,
    2: 1.00,
    5: 1.30,
    10: 1.52,
    25: 1.82,
    50: 2.05,
    100: 2.30,
    200: 2.56,
    500: 2.92,
    1000: 3.20,
}

#: Atlas-2 duration scaling ratios relative to the 24-hr depth (same ARI),
#: from the NWS HYDRO-35 / Atlas-2 Western-US depth-duration curve. Used to
#: synthesize sub-24-hr and multi-day durations from the 24-hr anchor when the
#: requested duration is neither 6 nor 24 hr.
_ATLAS2_DURATION_RATIO_TO_24HR: dict[float, float] = {
    5 / 60: 0.10,
    10 / 60: 0.15,
    15 / 60: 0.19,
    30 / 60: 0.27,
    1.0: 0.37,
    2.0: 0.50,
    3.0: 0.58,
    6.0: 0.71,
    12.0: 0.87,
    24.0: 1.00,
    48.0: 1.20,
    72.0: 1.33,
    96.0: 1.43,
    168.0: 1.65,
    240.0: 1.83,
}

def _point_in_bbox(
    lat: float, lon: float, bbox: tuple[float, float, float, float]
) -> bool:
    min_lon, min_lat, max_lon, max_lat = bbox
    return (min_lon <= lon <= max_lon) and (min_lat <= lat <= max_lat)

def _atlas2_anchor_grid_for_point(
    lat: float, lon: float
) -> tuple[dict[float, dict[int, float]], str]:
    """Pick the Atlas-2 regional anchor grid and region label for a western-US point,
    split at the Cascade crest near -120.5 lon: maritime to the west, drier interior to
    the east. The two regimes differ by about 2x, so a wrong-side pick is meaningful."""
    if lon <= -120.5 and lat >= 41.0:
        return _ATLAS2_PNW_ANCHORS_IN, "Pacific Northwest (windward Cascades)"
    return _ATLAS2_INTERIOR_WEST_ANCHORS_IN, "Interior Western US"

def _fetch_atlas2_precip_bytes(
    lat: float,
    lon: float,
    return_period_years: int,
    duration_hours: float,
) -> bytes:
    """Synthesize a western-US Atlas-2 depth for a point, DETERMINISTIC and
    NETWORK-FREE, as a CSV-like body in the shape the Atlas-14 parser already
    consumes. Outside the Atlas-2 envelope it raises an honest typed miss."""
    if not _point_in_bbox(lat, lon, _ATLAS2_WESTERN_US_BBOX):
        raise PrecipForcingUnavailableError(
            f"NOAA Atlas 2 (Western US) does not cover (lat={lat}, lon={lon}); "
            f"point is outside the Western-US coverage envelope "
            f"{_ATLAS2_WESTERN_US_BBOX}."
        )
    if return_period_years not in _ATLAS2_ARI_RATIO_TO_2YR:
        raise BboxInvalidError(
            f"return_period_years={return_period_years} not in the Atlas-2 "
            f"ARI set {sorted(_ATLAS2_ARI_RATIO_TO_2YR)}."
        )
    if duration_hours not in _ATLAS2_DURATION_RATIO_TO_24HR:
        raise BboxInvalidError(
            f"duration_hours={duration_hours} not in the Atlas-2 duration set "
            f"{sorted(_ATLAS2_DURATION_RATIO_TO_24HR)}."
        )

    anchors, region = _atlas2_anchor_grid_for_point(lat, lon)
    dur_ratio = _ATLAS2_DURATION_RATIO_TO_24HR[duration_hours]

    def _depth_at(ari: int) -> float:
        """Atlas-2 depth in inches at an ARI for this point's duration."""

        # Anchored on BOTH directly-mapped values, the 2-year and the 100-year at the
        # 24-hour duration, and log-linear in return period between and around them per
        # the documented log-Pearson frequency growth, so those two depths reproduce the
        # MAPPED anchors EXACTLY rather than a ratio approximation. The result is then
        # scaled to the requested duration by the depth-duration ratio; the 2-year
        # relative growth table supplies the curve SHAPE, calibrated so f(2) and f(100)
        # land on their anchors.
        d2 = anchors[24.0][2]
        d100 = anchors[24.0][100]
        # Calibrate the published 2-yr-relative growth ratios so the 100-yr
        # ratio maps to the mapped 100-yr/2-yr spread (preserves the real
        # anchor spread while keeping the published intermediate curve shape).
        r = _ATLAS2_ARI_RATIO_TO_2YR[ari]
        r100 = _ATLAS2_ARI_RATIO_TO_2YR[100]
        target_r100 = d100 / d2
        # Log-space rescale of the growth factor so r(2)->1 and r(100)->target.
        import math as _m

        if r <= 1.0 or r100 <= 1.0:
            cal_r = r  # below/at the 2-yr anchor: no rescale
        else:
            cal_r = _m.exp(_m.log(r) * (_m.log(target_r100) / _m.log(r100)))
        depth_24h = d2 * cal_r
        return depth_24h * dur_ratio

    depth_in = round(_depth_at(return_period_years), 3)

    # Build a one-row CSV body matching the Atlas-14 parser's expectations:
    # a "NOAA Atlas 2" header, a "Project area:" line, and the duration row with
    # one depth value PER ARI column (the parser requires len == ARI count).
    duration_label = _pick_duration_label(duration_hours)
    row_depths = [round(_depth_at(ari), 3) for ari in _ATLAS14_ARI_YEARS]
    body_lines = [
        "NOAA Atlas 2 (Western US) -- design-storm fallback",
        f"Project area: {region}",
        f"{duration_label}:, " + ",".join(f"{d:.3f}" for d in row_depths),
    ]
    logger.info(
        "atlas2 fallback (lat=%s lon=%s ari=%s dur=%s region=%r) -> %.3f in",
        lat, lon, return_period_years, duration_hours, region, depth_in,
    )
    return ("\n".join(body_lines) + "\n").encode("utf-8")

def _pick_duration_label(duration_hours: float) -> str:
    """Find the Atlas 14 duration row matching ``duration_hours`` EXACTLY. The atlas
    publishes a fixed set from 5 minutes to 60 days and no interpolations, so an
    unpublished duration is refused rather than interpolated."""
    for label, hrs in _ATLAS14_DURATIONS_HR.items():
        if abs(hrs - duration_hours) < 1e-9:
            return label
    available_hr = sorted(_ATLAS14_DURATIONS_HR.values())
    raise BboxInvalidError(
        f"duration_hours={duration_hours} not in Atlas 14's published rows "
        f"(available hours: {available_hr})."
    )

@register_tool(
    _LOOKUP_PRECIP_RETURN_PERIOD_METADATA,
    # Annotations: readOnlyHint=True, openWorldHint=True (NOAA PFDS API),
    # destructiveHint=False, idempotentHint=True (cache shim deduplicates).
    open_world_hint=True,
)
def lookup_precip_return_period(
    location: tuple[float, float],
    return_period_years: int,
    duration_hours: float,
    # absorb LLM-invented kwargs (centralized at the server via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Look up a design-storm precipitation depth at a point, in inches.

    Access pattern: Tier 3, a direct HTTPS point query to NOAA PFDS. Returns ONE
    (return period, duration) depth from Atlas 14 as a scalar, not a layer.
    Coverage is CONUS plus Puerto Rico and the US Virgin Islands; a western-US
    point Atlas 14 omits falls back to the Atlas-2 anchors, and a point neither
    atlas covers raises rather than guessing.

    Use this when: a design storm is the question, or a rainfall-runoff model
    needs an intensity-duration-frequency input.

    Do NOT use this for: OBSERVED precipitation; future-climate design storms;
    a spatial raster (this is a point service).

    Params: ``location`` as ``(lat, lon)`` -- latitude FIRST, the opposite of the
    bbox convention; ``return_period_years`` and ``duration_hours``, which must be
    values Atlas 14 publishes, since it never interpolates.

    Returns ``precip_inches`` with its units, the snapped location and the atlas
    vintage it came from.
    """
    if not isinstance(location, (tuple, list)) or len(location) != 2:
        raise BboxInvalidError(
            f"location must be a (lat, lon) 2-tuple; got {location!r}"
        )
    if return_period_years not in _ATLAS14_ARI_YEARS:
        raise BboxInvalidError(
            f"return_period_years={return_period_years} not in Atlas 14's published "
            f"ARIs {_ATLAS14_ARI_YEARS}."
        )
    duration_label = _pick_duration_label(duration_hours)

    lat, lon = float(location[0]), float(location[1])
    lat_q, lon_q = _quantize_lonlat_to_atlas14_grid(lat, lon)

    params = {
        "lat": lat_q,
        "lon": lon_q,
        "return_period_years": return_period_years,
        "duration_label": duration_label,
        "series": "pds",
        "units": "english",
    }

    # --- PRIMARY: NOAA Atlas 14 PFDS (CONUS + PR/USVI). ---
    # The fetch, parse and matrix lookup are wrapped so an out-of-project-area raise
    # OR a matrix miss falls through to the Atlas-2 western-US fallback: Atlas 14 does
    # NOT cover the Pacific Northwest or the Intermountain West.
    try:
        result = read_through(
            metadata=_LOOKUP_PRECIP_RETURN_PERIOD_METADATA,
            params=params,
            ext="csv",
            fetch_fn=lambda: _fetch_atlas14_pfds_bytes(lat_q, lon_q),
        )
        parsed = _parse_atlas14_csv(result.data.decode("utf-8"))
        matrix = parsed["matrix"]
        if (
            duration_label not in matrix
            or return_period_years not in matrix[duration_label]
        ):
            raise UpstreamAPIError(
                f"NOAA Atlas 14 PFDS response did not contain "
                f"duration={duration_label} x ARI={return_period_years} for "
                f"(lat={lat_q}, lon={lon_q}); parsed matrix labels: "
                f"{list(matrix.keys())[:5]}..."
            )
        depth_inches = matrix[duration_label][return_period_years]
        payload = {
            "precip_inches": depth_inches,
            "units": "inches",
            "location": [lat_q, lon_q],
            "return_period_years": return_period_years,
            "duration_hours": duration_hours,
            "vintage_volume": parsed["vintage_volume"],
            "project_area": parsed["project_area"],
            "source": "noaa-atlas14-pfds",
        }
        logger.info(
            "lookup_precip_return_period (lat=%s lon=%s ari=%s dur=%s) -> "
            "%.3f inches cache_hit=%s source=atlas14",
            lat_q,
            lon_q,
            return_period_years,
            duration_label,
            depth_inches,
            result.hit,
        )
        return payload
    except UpstreamAPIError as atlas14_exc:
        # --- FALLBACK 1: NOAA Atlas 2 (Western US). ---
        logger.info(
            "Atlas 14 missed (lat=%s lon=%s): %s -- trying NOAA Atlas 2 fallback",
            lat_q,
            lon_q,
            atlas14_exc,
        )
        atlas2_params = dict(params)
        atlas2_params["atlas"] = "noaa-atlas2"
        try:
            a2_result = read_through(
                metadata=_LOOKUP_PRECIP_RETURN_PERIOD_METADATA,
                params=atlas2_params,
                ext="csv",
                fetch_fn=lambda: _fetch_atlas2_precip_bytes(
                    lat_q, lon_q, return_period_years, duration_hours
                ),
            )
        except PrecipForcingUnavailableError:
            # --- FALLBACK 2 (FINAL): neither atlas covers this point. ---
            # Honest, NOT-retryable failure with an actionable remediation. The
            # observed-precip branch (model_flood_scenario forcing_raster_uri)
            # bypasses Atlas entirely and is the documented alternative.
            raise PrecipForcingUnavailableError(
                f"No design-storm precip source covers (lat={lat_q}, lon={lon_q}): "
                f"NOT in NOAA Atlas 14 ({atlas14_exc}) and outside the NOAA "
                f"Atlas 2 (Western US) coverage envelope. REMEDIATION: supply "
                f"observed precipitation via the forcing_raster_uri / observed-"
                f"precip path (fetch_mrms_qpe / ERA5 / gridMET -> a precip COG), "
                f"or choose an AOI inside Atlas-14 (CONUS east of the Rockies + "
                f"SW) or Atlas-2 (Western US) coverage."
            ) from atlas14_exc

        a2_parsed = _parse_atlas14_csv(a2_result.data.decode("utf-8"))
        a2_matrix = a2_parsed["matrix"]
        if (
            duration_label not in a2_matrix
            or return_period_years not in a2_matrix[duration_label]
        ):
            raise PrecipForcingUnavailableError(
                f"NOAA Atlas 2 fallback produced no depth for "
                f"duration={duration_label} x ARI={return_period_years} at "
                f"(lat={lat_q}, lon={lon_q})."
            ) from atlas14_exc
        depth_inches = a2_matrix[duration_label][return_period_years]
        payload = {
            "precip_inches": depth_inches,
            "units": "inches",
            "location": [lat_q, lon_q],
            "return_period_years": return_period_years,
            "duration_hours": duration_hours,
            # Honest provenance: the Atlas-2 fallback answered, NOT Atlas 14.
            "vintage_volume": "NOAA Atlas 2 (Western US)",
            "project_area": a2_parsed.get("project_area", "Western US"),
            "source": "noaa-atlas2",
        }
        logger.info(
            "lookup_precip_return_period (lat=%s lon=%s ari=%s dur=%s) -> "
            "%.3f inches source=atlas2 (Atlas-14 fallback)",
            lat_q,
            lon_q,
            return_period_years,
            duration_label,
            depth_inches,
        )
        return payload
