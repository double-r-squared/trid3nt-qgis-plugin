"""``compute_idf_curve`` atomic tool -- full NOAA Atlas 14 IDF curve chart.

Builds the complete intensity-duration-frequency (IDF) curve family for a
point from the NOAA Atlas 14 Precipitation Frequency Data Server (PFDS) --
the SAME endpoint ``lookup_precip_return_period`` queries, but instead of
extracting one (duration x ARI) cell this tool consumes the FULL matrix the
PFDS CSV already returns (19 durations from 5-min to 60-day x 10 ARIs from
1-yr to 1000-yr) and renders it as the house chart-emission payload
(``chart_tools.build_chart_payload`` -> Vega-Lite v5, inline data):

    x     duration (hours, LOG scale)
    y     intensity (in/hr, LOG scale; default) or depth (inches)
    color one line per return period (1..1000 yr)

This is the classic engineering IDF chart used to pick a design storm for
SFINCS / SWMM / SCS-CN work; the companion scalar lookup stays
``lookup_precip_return_period``.

Data source / reuse
===================

NOAA HDSC PFDS point CSV (Tier 3 direct HTTPS point query) via the EXACT
fetch + parse helpers ``lookup_precip_return_period`` uses
(``lookup_precip_return_period._fetch_atlas14_pfds_bytes`` + ``._parse_atlas14_csv``),
with the same 1/120-degree grid quantization for cache-key stability. Routed
through ``read_through`` (``static-30d`` / ``idf_curve``) so repeat calls at
the same snapped point reuse the cached CSV.

Coverage honesty: the PFDS covers the Atlas 14 project areas (most of CONUS +
PR/USVI; NOT the Pacific Northwest / most of the Intermountain West). An
out-of-area point raises a typed ``IdfCurveNoCoverageError`` pointing at
``lookup_precip_return_period``'s Atlas-2 (Western US) single-value fallback
-- the Atlas-2 parameterization is a 2-anchor reconstruction and is NOT
faithful enough to present as a full 190-point published IDF family.

The tool returns the ``ChartEmissionPayload`` dict (``envelope_type ==
"chart-emission"``): the agent loop emits the chart card + persists a
``SessionChartRecord``; no map layer is produced.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.fetchers.climate import lookup_precip_return_period as _df
from trid3nt_server.tools.cache import read_through
from trid3nt_server.tools.processing.charts_common import build_chart_payload

__all__ = [
    "compute_idf_curve",
    "IdfCurveError",
    "IdfCurveInputError",
    "IdfCurveNoCoverageError",
    "IdfCurveUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.processing.compute_idf_curve")


# ---------------------------------------------------------------------------
# Error types (FR-AS-11 typed-error surface).
# ---------------------------------------------------------------------------


class IdfCurveError(RuntimeError):
    """Base class for compute_idf_curve failures."""

    error_code: str = "IDF_CURVE_ERROR"
    retryable: bool = True


class IdfCurveInputError(IdfCurveError):
    """Bad inputs (malformed location / mode)."""

    error_code = "IDF_CURVE_INPUT_INVALID"
    retryable = False


class IdfCurveNoCoverageError(IdfCurveError):
    """The point is outside the NOAA Atlas 14 project areas (honest miss)."""

    error_code = "IDF_CURVE_NO_COVERAGE"
    retryable = False


class IdfCurveUpstreamError(IdfCurveError):
    """The PFDS fetch / parse failed at the network layer."""

    error_code = "IDF_CURVE_UPSTREAM_ERROR"
    retryable = True


# ---------------------------------------------------------------------------
# Metadata + fetch seam.
# ---------------------------------------------------------------------------

_METADATA = AtomicToolMetadata(
    name="compute_idf_curve",
    ttl_class="static-30d",
    source_class="idf_curve",
    cacheable=True,
)


def _fetch_pfds_matrix_bytes(lat: float, lon: float) -> bytes:
    """Fetch the full Atlas 14 PFDS CSV at the snapped point.

    Module-level seam (tests monkeypatch it with a canned PFDS response);
    delegates to the EXACT fetcher ``lookup_precip_return_period`` uses.
    """
    return _df._fetch_atlas14_pfds_bytes(lat, lon)


# ---------------------------------------------------------------------------
# Location normalization.
# ---------------------------------------------------------------------------


def _resolve_latlon(location: Any) -> tuple[float, float]:
    """Accept ``(lat, lon)`` or a 4-element bbox (its center is used).

    A 2-tuple is ``(lat, lon)`` (the ``lookup_precip_return_period``
    convention: lat first). A 4-tuple/list is treated as a ``(min_lon,
    min_lat, max_lon, max_lat)`` EPSG:4326 bbox and reduced to its center.
    """
    if not isinstance(location, (tuple, list)) or len(location) not in (2, 4):
        raise IdfCurveInputError(
            f"location must be a (lat, lon) 2-tuple or a (min_lon, min_lat, "
            f"max_lon, max_lat) bbox; got {location!r}"
        )
    try:
        vals = [float(v) for v in location]
    except (TypeError, ValueError) as exc:
        raise IdfCurveInputError(
            f"location contains non-numeric values: {location!r}"
        ) from exc
    if not all(math.isfinite(v) for v in vals):
        raise IdfCurveInputError(f"location contains non-finite values: {location!r}")
    if len(vals) == 2:
        lat, lon = vals
    else:
        west, south, east, north = vals
        lon = 0.5 * (west + east)
        lat = 0.5 * (south + north)
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise IdfCurveInputError(
            f"resolved point (lat={lat}, lon={lon}) out of range "
            "(lat in [-90,90], lon in [-180,180]); note location is (lat, lon), "
            "NOT (lon, lat)."
        )
    return lat, lon


# ---------------------------------------------------------------------------
# Registered tool.
# ---------------------------------------------------------------------------


@register_tool(
    _METADATA,
    # Annotations: hits the external NOAA PFDS API (like
    # lookup_precip_return_period), so open_world_hint=True is honest --
    # listed in test_tool_annotations._OPEN_WORLD_COMPUTE_EXCEPTIONS.
    open_world_hint=True,
)
def compute_idf_curve(
    location: tuple[float, float],
    y_axis: str = "intensity",
    # job-0164: absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Chart the full NOAA Atlas 14 IDF (intensity-duration-frequency) curve for a point.

    Use this (not fetch_climate_normals or a fetch_* rainfall tool) when you want the NOAA Atlas 14 IDF rainfall-frequency CHART.

    Access pattern: Tier 3 (direct HTTPS point query to the NOAA PFDS endpoint).

    **What it does:** Queries the NOAA Atlas 14 Precipitation Frequency Data
    Server at the point (snapped to the Atlas 14 1/120-degree grid), parses the
    FULL published duration x return-period matrix (19 durations, 5-min to
    60-day x 10 ARIs, 1-yr to 1000-yr), and returns the house chart-emission
    payload: a Vega-Lite line chart with duration on a LOG x axis, rainfall
    intensity (in/hr; or depth in inches with ``y_axis="depth"``) on the y
    axis, and one line per return period. This is the classic design-storm IDF
    chart; for a single scalar depth use ``lookup_precip_return_period``.

    **When to use:**
    - "Show me the IDF curve for Houston" / "rainfall
      intensity-duration-frequency chart at this site".
    - Picking a design storm (duration + return period) before a pluvial
      SFINCS / SWMM run.
    - Comparing short-duration cloudburst vs long-duration soaker design
      depths at one location.

    **When NOT to use:**
    - One specific depth (e.g. the 100-yr 24-hr) -- use
      ``lookup_precip_return_period`` (same endpoint, scalar answer).
    - Observed rainfall -- use ``fetch_mrms_qpe`` / ``fetch_usgs_nwis_gauges``.
    - Points outside the Atlas 14 project areas (Pacific Northwest /
      Intermountain West; OCONUS beyond PR/USVI): this tool raises an honest
      typed no-coverage error (the Atlas-2 fallback in
      ``lookup_precip_return_period`` is a 2-anchor reconstruction, not a
      publishable full IDF family).

    **Parameters:**
    - ``location``: ``(lat, lon)`` decimal degrees (lat FIRST, the
      ``lookup_precip_return_period`` convention), OR a 4-element
      ``(min_lon, min_lat, max_lon, max_lat)`` bbox whose center is used.
    - ``y_axis``: ``"intensity"`` (default; in/hr, log y -- the classic IDF
      form) or ``"depth"`` (inches, linear y -- a DDF chart).

    **Returns:** A ``ChartEmissionPayload`` dict (``envelope_type ==
    "chart-emission"``): Vega-Lite v5 spec with the full inline matrix (one row
    per duration x ARI), title, and a caption carrying the Atlas 14 volume +
    project area provenance. The agent loop renders it as an inline chart card.

    **Data source:** NOAA HDSC PFDS (``fe_text_mean.csv``; partial-duration
    series, english units) -- the same endpoint + parser as
    ``lookup_precip_return_period``. FR-CE-8: the CSV is routed through
    ``read_through`` (``static-30d``/``idf_curve``) keyed on the snapped grid
    point.
    """
    lat, lon = _resolve_latlon(location)
    mode = str(y_axis or "intensity").strip().lower()
    if mode not in ("intensity", "depth"):
        raise IdfCurveInputError(
            f"y_axis must be 'intensity' or 'depth'; got {y_axis!r}"
        )

    lat_q, lon_q = _df._quantize_lonlat_to_atlas14_grid(lat, lon)

    params = {
        "lat": lat_q,
        "lon": lon_q,
        "series": "pds",
        "units": "english",
        "product": "idf_matrix",
    }
    try:
        result = read_through(
            metadata=_METADATA,
            params=params,
            ext="csv",
            fetch_fn=lambda: _fetch_pfds_matrix_bytes(lat_q, lon_q),
        )
    except _df.UpstreamAPIError as exc:
        # The PFDS answers "not within a project area" for out-of-coverage
        # points (data_fetch raises UpstreamAPIError for both that and true
        # network failures; the message disambiguates). Treat the documented
        # out-of-area body as an honest coverage miss.
        if "project area" in str(exc):
            raise IdfCurveNoCoverageError(
                f"NOAA Atlas 14 does not cover (lat={lat_q}, lon={lon_q}); no "
                "published full IDF family exists for this point. For a single "
                "design depth in the Western US, lookup_precip_return_period "
                "falls back to the NOAA Atlas 2 parameterization."
            ) from exc
        raise IdfCurveUpstreamError(
            f"NOAA Atlas 14 PFDS fetch failed for (lat={lat_q}, lon={lon_q}): {exc}"
        ) from exc

    parsed = _df._parse_atlas14_csv(result.data.decode("utf-8"))
    matrix: dict[str, dict[int, float]] = parsed["matrix"]
    if not matrix:
        raise IdfCurveUpstreamError(
            f"NOAA Atlas 14 PFDS response for (lat={lat_q}, lon={lon_q}) parsed "
            "to an empty duration x ARI matrix."
        )

    # ---- Build the inline rows: one per (duration, ARI) cell. -------------
    intensity = mode == "intensity"
    rows: list[dict[str, Any]] = []
    for label, hours in _df._ATLAS14_DURATIONS_HR.items():
        depths = matrix.get(label)
        if not depths:
            continue  # honest: chart only what the PFDS published
        for ari in _df._ATLAS14_ARI_YEARS:
            depth_in = depths.get(ari)
            if depth_in is None or not math.isfinite(depth_in) or depth_in <= 0:
                continue
            value = depth_in / hours if intensity else depth_in
            rows.append(
                {
                    "duration_hr": round(float(hours), 6),
                    "duration": label,
                    "value": round(float(value), 5),
                    "return_period": f"{ari}-yr",
                    "ari_years": ari,
                }
            )
    if not rows:
        raise IdfCurveUpstreamError(
            f"NOAA Atlas 14 PFDS matrix for (lat={lat_q}, lon={lon_q}) carried "
            "no plottable positive depths."
        )

    y_title = "Intensity (in/hr)" if intensity else "Depth (inches)"
    # Classic IDF charts are log-log; a depth (DDF) chart reads better linear.
    y_scale: dict[str, Any] = {"type": "log"} if intensity else {}
    ari_order = [f"{a}-yr" for a in _df._ATLAS14_ARI_YEARS]
    spec: dict[str, Any] = {
        "title": "NOAA Atlas 14 IDF curve",
        "data": {"values": rows},
        "mark": {"type": "line", "point": True, "tooltip": True},
        "encoding": {
            "x": {
                "field": "duration_hr",
                "type": "quantitative",
                "scale": {"type": "log"},
                "title": "Duration (hours)",
            },
            "y": {
                "field": "value",
                "type": "quantitative",
                "title": y_title,
                **({"scale": y_scale} if y_scale else {}),
            },
            "color": {
                "field": "return_period",
                "type": "nominal",
                "sort": ari_order,
                "title": "Return period",
            },
        },
        "width": "container",
    }

    n_durations = len({r["duration_hr"] for r in rows})
    n_aris = len({r["ari_years"] for r in rows})
    caption = (
        f"{parsed['vintage_volume']} · {parsed['project_area']} · point "
        f"({lat_q:.4f}, {lon_q:.4f}) · {n_durations} durations x {n_aris} "
        f"return periods · partial-duration series, "
        f"{'intensity in/hr' if intensity else 'depth inches'}"
    )
    logger.info(
        "compute_idf_curve (lat=%s lon=%s mode=%s) -> %d rows cache_hit=%s",
        lat_q,
        lon_q,
        mode,
        len(rows),
        result.hit,
    )
    return build_chart_payload(
        vega_lite_spec=spec,
        title=f"IDF curve ({lat_q:.3f}, {lon_q:.3f})",
        caption=caption,
    )
