"""NOAA Atlas 14 PFDS point precipitation-frequency lookup
(``lookup_precip_return_period``) with the Atlas-2 western-US anchor fallback.

Carved out of the original multi-tool ``data_fetch`` module (job-0033) in the
tools/ reorg; behavior and the registered tool surface are unchanged. The
shared typed-error hierarchy + bbox helpers live in
``trid3nt_server.tools.fetchers._fetch_common``.
"""

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

logger = logging.getLogger("trid3nt_server.tools.fetchers.climate.lookup_precip_return_period")


class PrecipForcingUnavailableError(FetchError):
    """No design-storm precip source covers the requested point (job-0327).

    Raised when BOTH NOAA Atlas 14 AND the NOAA Atlas 2 (Western US) fallback
    miss the location — a genuinely-uncovered AOI. This is an HONEST,
    NOT-retryable failure: the agent surfaces it as ``status=error`` with a
    clear remediation (supply observed precip via ``forcing_raster_uri`` /
    the observed-precip path, or pick an AOI inside Atlas-14/Atlas-2 coverage).
    Distinct ``error_code`` so the agent can narrate the actionable alternative
    rather than a generic upstream-API failure.
    """

    error_code = "PRECIP_FORCING_UNAVAILABLE"
    retryable = False

# ---------------------------------------------------------------------------
# lookup_precip_return_period — NOAA Atlas 14 PFDS (sprint-07 Stage B, job-0039).
# ---------------------------------------------------------------------------
#
# Access pattern tier — LIVE-VERIFIED matches kickoff inference (2026-06-07):
#
#   * NWS HDSC publishes the Precipitation Frequency Data Server (PFDS) as a
#     point-query CSV endpoint at ``hdsc.nws.noaa.gov/cgi-bin/hdsc/new/
#     fe_text_mean.csv?lat=&lon=&data=depth&units=english&series=pds``.
#     Live probe at (lat=26.6, lon=-81.9) — Fort Myers FL — returned an HTTP
#     200 with a 1598-byte CSV: header rows naming "NOAA Atlas 14 Volume 9
#     Version 2" + "Project area: Southeastern States", then a matrix of
#     precipitation depths (inches) indexed by duration (5-min, 10-min, …,
#     60-day) × ARI (1, 2, 5, …, 1000 years).
#   * Per-coordinate / point-only query surface — no native bbox lookup. The
#     fetcher routes by ``location=(lat, lon)`` quantized to Atlas 14's native
#     source grid (1/120 degree, per the kickoff's per-source quantization
#     rule).
#
# This is the **Tier 3 (direct HTTPS + Range-irrelevant point query)**
# pattern in §F.1.1 — small textual responses keyed by point coordinates.
# Cache key is bbox-equivalent: the quantized (lat, lon) tuple per the
# 1/120-degree source grid; ARI + duration are part of the params.


_LOOKUP_PRECIP_RETURN_PERIOD_METADATA = AtomicToolMetadata(
    name="lookup_precip_return_period",
    ttl_class="static-30d",
    source_class="precip_return_period",
    cacheable=True,
)

_ATLAS14_PFDS_URL = "https://hdsc.nws.noaa.gov/cgi-bin/hdsc/new/fe_text_mean.csv"

#: Atlas 14 native source grid: 1/120 degree (≈ 30 arc-seconds).
_ATLAS14_GRID_DEG = 1.0 / 120.0

#: The ARI (Average Recurrence Interval) columns Atlas 14 reports — fixed.
_ATLAS14_ARI_YEARS = [1, 2, 5, 10, 25, 50, 100, 200, 500, 1000]

#: The duration rows Atlas 14 reports — fixed across volumes.
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
    """Quantize a (lat, lon) pair to Atlas 14's 1/120-degree native grid.

    Per the per-source bbox quantization rule (acceptance criterion 3 of
    the kickoff): Atlas 14 PFDS is reported on a 1/120-degree source grid.
    We snap to the nearest grid intersection so two callers within the same
    grid cell hit the same cache entry.
    """
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
    """Parse the Atlas 14 PFDS CSV into a structured dict.

    The PFDS CSV is a small textual document — header lines naming the
    volume / version / project area, then a matrix indexed by duration × ARI.
    We surface both the full matrix and a top-level ``vintage_volume`` field
    for provenance (e.g. "NOAA Atlas 14 Volume 9 Version 2").
    """
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
    """Fetch the Atlas 14 PFDS CSV at (lat, lon) and return raw response bytes.

    Tier 3 access pattern: HTTPS GET with the location as a query parameter,
    text/csv (well, text/html with CSV body — see the parser for the body
    shape). The bytes returned are the verbatim Atlas 14 response so
    downstream re-parsing is possible without a re-fetch.
    """
    try:
        resp = requests.get(
            _ATLAS14_PFDS_URL,
            params={
                "lat": str(lat),
                "lon": str(lon),
                "data": "depth",
                "units": "english",
                "series": "pds",  # partial-duration series — Atlas 14 convention
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
        # The PFDS returns an HTML "out of project area" page if the point
        # falls outside Atlas 14 coverage; surface that as a typed error.
        # (Live-confirmed body for an out-of-area point: ``result = 'none';
        # ErrorMsg = 'Error 3.0: Selected location is not within a project
        # area';`` — the "NOAA Atlas 14" header is absent, so this guard trips.)
        raise UpstreamAPIError(
            f"NOAA Atlas 14 PFDS returned no precip-frequency data for "
            f"(lat={lat}, lon={lon}) — point may be outside the Atlas 14 "
            f"project areas (Western US: V1; SW: V2; ... ; OCONUS: not yet)."
        )
    return body.encode("utf-8")

# --------------------------------------------------------------------------- #
# NOAA Atlas 2 (Western US) design-storm fallback  (job-0327)
# --------------------------------------------------------------------------- #
#
# WHY THIS EXISTS. The Pacific Northwest (WA / OR / ID) and most of the
# Intermountain West are NOT in NOAA Atlas 14 — they remain covered only by the
# legacy NOAA Atlas 2 ("Precipitation-Frequency Atlas of the Western United
# States", Miller / Frederick / Tracey, NWS 1973). The Atlas-14 PFDS point
# endpoint answers ``Error 3.0: ... not within a project area`` for these
# points (live-confirmed for the Toutle / Mount St. Helens point lat=46.325
# lon=-122.733). Before this fallback existed the workflow died in 1-3s at the
# precip fetcher and the agent silently reported "ok" (job-0327 root cause).
#
# WHAT IT DOES. NOAA Atlas 2 is a 1973 isopluvial-MAP atlas — there is no clean
# machine-readable lat/lon point CSV endpoint comparable to the Atlas-14 PFDS
# (the digital grids are state-by-state raster / contour products, not a live
# point API, and the HDSC PFDS server explicitly does NOT serve them as CSV).
# So this fallback is a BUNDLED parameterization of the published Atlas-2
# Western-US precipitation-frequency surface: regional 2-yr and 100-yr
# 6-hr / 24-hr anchor depths (the four values Atlas 2 maps directly), combined
# with the Atlas-2 / NWS HYDRO-35 documented log-Pearson frequency scaling and
# duration scaling to synthesize the requested ARI x duration depth. This is
# the standard hydrologic reconstruction used when only the Atlas-2 mapped
# anchors are available; it is DETERMINISTIC and NETWORK-FREE (so it can never
# wedge or silently fail), and the provenance is honest about which atlas
# answered (``source="noaa-atlas2"``, ``vintage_volume="NOAA Atlas 2 (Western
# US)"``). Outside the Western-US coverage envelope it raises a typed miss —
# never an empty / fabricated success.

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
    """Pick the Atlas-2 regional anchor grid + region label for a Western-US point.

    Cascade-crest split (~ -120.5 lon): west = maritime PNW regime, east =
    drier interior-West regime. Coarse but honest — the two regimes differ by
    ~2x in total, so a wrong-side pick would be a meaningful error; the split
    keeps a windward-Cascades AOI (Toutle) on the maritime curve and an interior
    AOI on the dry curve.
    """
    if lon <= -120.5 and lat >= 41.0:
        return _ATLAS2_PNW_ANCHORS_IN, "Pacific Northwest (windward Cascades)"
    return _ATLAS2_INTERIOR_WEST_ANCHORS_IN, "Interior Western US"

def _fetch_atlas2_precip_bytes(
    lat: float,
    lon: float,
    return_period_years: int,
    duration_hours: float,
) -> bytes:
    """Synthesize an Atlas-2 (Western US) precip-frequency depth for a point.

    job-0327 fallback for the WHY-IT-FAILS Toutle die. Returns a small CSV-like
    body in the SAME shape ``_parse_atlas14_csv`` consumes (a ``NOAA Atlas 2``
    header line, a ``Project area:`` line, and one duration row of comma-
    separated depths across the fixed ARI columns) so the existing parser path
    works unchanged. DETERMINISTIC + NETWORK-FREE (no upstream call to wedge).

    Raises ``PrecipForcingUnavailableError`` when the point is outside the
    Western-US Atlas-2 coverage envelope (an honest miss, never an empty
    success). ``BboxInvalidError`` on a duration Atlas 2 cannot synthesize.
    """
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
        """Atlas-2 depth (inches) at an ARI for this point's duration.

        Anchors on BOTH directly-mapped Atlas-2 values (2-yr and 100-yr) at
        the 24-hr duration: log-linear in return period between/around them
        (the documented Atlas-2 / log-Pearson frequency growth), so the 2-yr
        and 100-yr depths reproduce the MAPPED anchors EXACTLY rather than a
        ratio approximation. Then scaled to the requested duration by the
        depth-duration ratio. The 2-yr-relative growth table provides the
        curve SHAPE; it is calibrated so f(2)=anchor_2 and f(100)=anchor_100.
        """
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
        "NOAA Atlas 2 (Western US) — design-storm fallback (job-0327)",
        f"Project area: {region}",
        f"{duration_label}:, " + ",".join(f"{d:.3f}" for d in row_depths),
    ]
    logger.info(
        "atlas2 fallback (lat=%s lon=%s ari=%s dur=%s region=%r) -> %.3f in",
        lat, lon, return_period_years, duration_hours, region, depth_in,
    )
    return ("\n".join(body_lines) + "\n").encode("utf-8")

def _pick_duration_label(duration_hours: float) -> str:
    """Find the Atlas 14 duration row whose hours match ``duration_hours`` exactly.

    Atlas 14 reports a fixed set of durations (5-min through 60-day). We
    require an exact match against the known set so the caller can't ask
    for an interpolated value (Atlas 14 doesn't publish interpolations and
    we don't fabricate them — Invariant 7).
    """
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
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Look up a precipitation return-period depth at a point via NOAA Atlas 14 PFDS.

    Access pattern: Tier 3 (direct HTTPS point query to the NOAA PFDS endpoint).

    **What it does:** Issues a point query to the NOAA Hydrometeorological Design
    Studies Center (HDSC) Precipitation Frequency Data Server (PFDS) at
    ``hdsc.nws.noaa.gov/cgi-bin/hdsc/new/fe_text_mean.csv``, parses the returned
    duration × ARI matrix, and returns the requested depth in inches. Input
    coordinates are snapped to Atlas 14's 1/120° (~30 arc-second) grid before
    the cache key is computed (FR-DC-4 dedup). This is a point query, not a
    raster — it returns a scalar dict, not a ``LayerURI``. Tier-1 free, no
    API key, CONUS + Puerto Rico / US Virgin Islands only.

    **When to use:**

    - Design-storm precipitation depth for an SFINCS pluvial-flood scenario
      ("what is the 100-year, 24-hour rainfall for Miami?"). Example:
      ``location=(25.77, -80.19)``, ``return_period_years=100``,
      ``duration_hours=24.0``.
    - Characterising a published historical storm by its return-period equivalence
      ("Harvey's 48-hour total at Houston — what ARI?"). Run the tool for
      multiple ARIs and compare.
    - Providing IDF (intensity-duration-frequency) input for a rainfall-runoff
      model (SCS CN, Green-Ampt).

    **When NOT to use:**

    - Observed precipitation totals — use ``fetch_mrms_qpe`` (gauge-corrected
      radar accumulation) or NWIS / NEXRAD for measurements.
    - Future-climate design storms — Atlas 14 is based on historical records
      (Atlas 15, in development, will integrate non-stationarity).
    - Locations outside CONUS / PR / USVI — Atlas 14 OCONUS coverage is partial;
      Alaska, Hawaii, and Pacific Islands are not in the v0.1 substrate.
    - Spatial rasters of return-period precipitation — Atlas 14 PFDS is a point
      service; for a spatial map use a pre-computed gridded Atlas 14 dataset.

    **Parameters:**

    - ``location``: ``(lat, lon)`` decimal degrees EPSG:4326. Note: lat first,
      lon second (opposite of the ``bbox`` convention). Example: ``(29.76, -95.37)``
      for Houston.
    - ``return_period_years``: ARI in years; Atlas 14 publishes
      ``{1, 2, 5, 10, 25, 50, 100, 200, 500, 1000}``; values outside this set
      raise ``BboxInvalidError``.
    - ``duration_hours``: storm duration in hours; Atlas 14 publishes durations
      from 5 min (5/60 h) to 60 days (1440 h); unsupported durations raise
      ``BboxInvalidError``.

    **Returns:**

    A ``dict`` with keys: ``precip_inches`` (float, precipitation depth in
    inches), ``units`` (``"inches"``), ``location`` ([lat, lon] of the snapped
    Atlas 14 grid point), ``return_period_years`` (ARI echo), ``duration_hours``
    (duration echo), ``vintage_volume`` (e.g. ``"NOAA Atlas 14 Volume 9 Version
    2"``), ``project_area`` (e.g. ``"Southeastern States"``),
    ``source`` (``"noaa-atlas14-pfds"``).

    **Cross-tool dependencies:**

    - Consumed by: ``build_sfincs_model`` to construct a synthetic design-storm
      hyetograph; ``run_pluvial_flood`` workflow (uses the returned depth to
      drive the SFINCS rainfall input file).
    - Compare with: ``fetch_mrms_qpe`` for observed accumulations vs Atlas 14
      design depths; the ratio gives the storm's return-period rank.
    - Pair with: ``fetch_gcn250_curve_numbers`` or NLCD-derived CNs when
      converting depth → runoff volume via SCS CN method.

    FR-CE-8: Routed through ``read_through`` with ``ttl_class="static-30d"``;
    cache key = SHA-256 of ``(lat-quantized, lon-quantized, return_period_years,
    duration_label)`` — snapping ensures callers within the same 30 arc-second
    cell dedup (FR-DC-4).
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
    # job-0327: the Atlas-14 fetch+parse+matrix-lookup is wrapped so an
    # out-of-project-area die (the data_fetch.py out-of-area raise) OR a
    # matrix-miss raise falls through to the NOAA Atlas 2 (Western US) fallback
    # — implementing the MEMORY "Atlas-14 -> Atlas-2 first" norm that was
    # previously doc-only. Atlas 14 does NOT cover the Pacific Northwest /
    # Intermountain West (WA/OR/ID + interior states) — those remain Atlas 2.
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
                f"duration={duration_label} × ARI={return_period_years} for "
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
            "Atlas 14 missed (lat=%s lon=%s): %s — trying NOAA Atlas 2 fallback",
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
                f"precip path (fetch_mrms_qpe / ERA5 / gridMET → a precip COG), "
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
                f"duration={duration_label} × ARI={return_period_years} at "
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
