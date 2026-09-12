"""``fetch_idf_curve`` - the full NOAA Atlas 14 IDF curve chart for a point.

The PFDS endpoint ``lookup_precip_return_period`` reads a single design depth
from, read instead for the whole duration x ARI matrix and charted. A point
outside the Atlas 14 project areas raises a typed no-coverage error: the Atlas-2
two-anchor reconstruction is not a published IDF family.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.tools.cache import read_through
from trid3nt_server.emission.charts import build_chart_payload
from ..._fetch_common import FetchError
from ..lookup_precip_return_period import lookup_precip_return_period as _pfds

__all__ = [
    "fetch_idf_curve",
    "IdfCurveError",
    "IdfCurveInputError",
    "IdfCurveNoCoverageError",
    "IdfCurveUpstreamError",
]

logger = logging.getLogger("trid3nt_server.tools.fetchers.climate.fetch_idf_curve.fetch_idf_curve")




class IdfCurveError(FetchError):
    """Base class for fetch_idf_curve failures."""

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



_METADATA = AtomicToolMetadata(
    name="fetch_idf_curve",
    ttl_class="static-30d",
    source_class="idf_curve",
    cacheable=True,
)


def _fetch_pfds_matrix_bytes(lat: float, lon: float) -> bytes:
    """The full Atlas 14 PFDS CSV at the snapped point."""
    return _pfds._fetch_atlas14_pfds_bytes(lat, lon)




def _resolve_latlon(location: Any) -> tuple[float, float]:
    """A 2-element ``location`` is ``(lat, lon)``, lat FIRST; a 4-element one is a
    ``(min_lon, min_lat, max_lon, max_lat)`` bbox reduced to its centre.
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




@register_tool(
    _METADATA,
    # Hits the external NOAA PFDS API, so open_world_hint=True is honest.
    open_world_hint=True,
)
def fetch_idf_curve(
    location: tuple[float, float],
    y_axis: str = "intensity",
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Chart the full NOAA Atlas 14 IDF (intensity-duration-frequency) curve for a point.

    Use for the whole Atlas 14 IDF CHART -- "IDF curve for Houston", picking a
    design storm before a pluvial rainfall-runoff run, comparing cloudburst vs
    soaker depths. Do NOT use for: one specific depth
    (``lookup_precip_return_period``, same endpoint, scalar); the climate
    baseline (``fetch_climate_normals``); observed rainfall (``fetch_mrms_qpe``
    / ``fetch_usgs_nwis_gauges``); points outside Atlas 14 coverage (Pacific
    NW/Intermountain West; OCONUS beyond PR/USVI) -- raises an honest
    no-coverage error.

    Params:
        location: ``(lat, lon)`` decimal degrees (lat first), or a 4-element
            bbox whose center is used.
        y_axis: ``"intensity"`` (default, in/hr, log y) or ``"depth"``
            (inches, linear y).

    Returns a Vega-Lite chart of the full duration x ARI matrix, 19 durations by
    10 return periods, with a provenance caption.
    """
    lat, lon = _resolve_latlon(location)
    mode = str(y_axis or "intensity").strip().lower()
    if mode not in ("intensity", "depth"):
        raise IdfCurveInputError(
            f"y_axis must be 'intensity' or 'depth'; got {y_axis!r}"
        )

    lat_q, lon_q = _pfds._quantize_lonlat_to_atlas14_grid(lat, lon)

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
    except _pfds.UpstreamAPIError as exc:
        # The PFDS answers "not within a project area" for an out-of-coverage
        # point, and the fetcher raises the same error class for that and for a
        # true network failure, so only the message separates them.
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

    parsed = _pfds._parse_atlas14_csv(result.data.decode("utf-8"))
    matrix: dict[str, dict[int, float]] = parsed["matrix"]
    if not matrix:
        raise IdfCurveUpstreamError(
            f"NOAA Atlas 14 PFDS response for (lat={lat_q}, lon={lon_q}) parsed "
            "to an empty duration x ARI matrix."
        )

    # One inline row per (duration, ARI) cell.
    intensity = mode == "intensity"
    rows: list[dict[str, Any]] = []
    for label, hours in _pfds._ATLAS14_DURATIONS_HR.items():
        depths = matrix.get(label)
        if not depths:
            continue  # honest: chart only what the PFDS published
        for ari in _pfds._ATLAS14_ARI_YEARS:
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
    ari_order = [f"{a}-yr" for a in _pfds._ATLAS14_ARI_YEARS]
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
        "fetch_idf_curve (lat=%s lon=%s mode=%s) -> %d rows cache_hit=%s",
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
