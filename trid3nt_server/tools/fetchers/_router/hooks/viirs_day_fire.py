"""viirs_day_fire frames hooks: the JPSS/VIIRS Day Fire polar animation.

Folds fetch_viirs_day_fire onto the frames-list output shape (shape:
animation_frames). The router owns the per-frame read_through loop + honesty floor +
LayerURI emission; these two hooks own the source-specific steps:

- ``frames_plan`` -- resolve the SLIDER jpss overpass index (the merged
  multi-satellite pass set), window + day-filter (local-solar-time) + merge/sort +
  cap, and build the ordered per-frame plans labelled with the REAL irregular pass
  times.
- ``frame_bytes`` -- stitch + reproject ONE VIIRS overpass -> RGB COG.

ASCII only.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ...imagery._satellite_slider import (
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
from ..errors import router_empty_error, router_input_error, router_upstream_error
from . import FrameDegraded, FramePlan, frame_windows, register_hook

logger = logging.getLogger(
    "trid3nt_server.tools.fetchers._router.hooks.viirs_day_fire"
)

__all__ = [
    "frames_plan",
    "frame_bytes",
    "VIIRS_SATELLITES",
    "DAY_FIRE_PRODUCT_SLUG",
    "MAX_VIIRS_FRAMES",
    "_parse_utc",
    "_is_daytime_pass",
    "_build_pass_list",
]


# --------------------------------------------------------------------------- #
# Constants (carried verbatim from the fetch_viirs_day_fire twin).
# --------------------------------------------------------------------------- #

#: Conceptual JPSS satellite subsets. 'all' = the merged SLIDER jpss pass list.
VIIRS_SATELLITES = ("suomi-npp", "noaa-20", "noaa-21", "all")

#: VIIRS Day Fire product slug on the CIRA Polar SLIDER (CONFIRMED LIVE).
DAY_FIRE_PRODUCT_SLUG = "cira_natural_fire_color"

#: product name -> SLIDER jpss product slug.
_PRODUCT_TO_SLUG: dict[str, str] = {
    "day_fire": DAY_FIRE_PRODUCT_SLUG,
}

#: Local-solar-time window (hours) treated as a DAY pass.
_DAY_LST_START_H = 6.0
_DAY_LST_END_H = 19.0

MAX_VIIRS_FRAMES: int = int(os.environ.get("TRID3NT_MAX_VIIRS_FRAMES", "144"))

_BBOX_QUANTIZE_DP = 6


# --------------------------------------------------------------------------- #
# Pure helpers (also importable for tests).
# --------------------------------------------------------------------------- #


def _parse_utc(spec: SourceSpec, value: Any) -> datetime:
    """Parse an ISO-8601 string / datetime -> aware UTC. Raises the typed INPUT error."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    sc = spec.error_code_prefix
    if not isinstance(value, str) or not value.strip():
        raise router_input_error(
            sc, f"time must be an ISO-8601 string or datetime; got {value!r}",
            spec.input_error_suffix,
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
            raise router_input_error(
                sc,
                f"could not parse UTC time {value!r}; use ISO-8601 "
                "(e.g. '2026-05-15T20:47:00Z')",
                spec.input_error_suffix,
            )
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local_solar_hour(dt_utc: datetime, lon: float) -> float:
    """Approximate local-solar-time hour-of-day at ``lon`` for a UTC time (0..24)."""
    utc_hours = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0
    return (utc_hours + lon / 15.0) % 24.0


def _is_daytime_pass(ts_int: int, aoi_center_lon: float) -> bool:
    """True iff the overpass is during local DAYTIME at the AOI longitude."""
    lst = _local_solar_hour(ts_int_to_datetime(ts_int), aoi_center_lon)
    return _DAY_LST_START_H <= lst < _DAY_LST_END_H


def _select_frame_indices(n: int, cap: int = MAX_VIIRS_FRAMES) -> list[int]:
    """Pick up to ``cap`` evenly-spaced indices over ``n``, endpoints kept."""
    if n <= 0:
        return []
    if n <= cap:
        return list(range(n))
    import numpy as np

    idx = np.linspace(0, n - 1, cap).round().astype(int)
    kept = [int(i) for i in np.unique(idx)]
    logger.info(
        "viirs_day_fire: %d daytime passes exceed cap=%d; subsampling to %d.",
        n, cap, len(kept),
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
    """Window + day-filter + merge/sort the SLIDER jpss pass timestamps."""
    in_window = [
        ts for ts in timestamps_int if start_utc <= ts_int_to_datetime(ts) <= end_utc
    ]
    if day_only:
        in_window = [ts for ts in in_window if _is_daytime_pass(ts, aoi_center_lon)]
    in_window.sort()
    keep = _select_frame_indices(len(in_window), cap=cap)
    return [in_window[i] for i in keep]


def _round_bbox(bbox: Any) -> tuple[float, float, float, float]:
    return tuple(round(float(v), _BBOX_QUANTIZE_DP) for v in bbox)  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# frames_plan: the pre-loop resolve.
# --------------------------------------------------------------------------- #


@register_hook("viirs_day_fire.frames_plan")
def frames_plan(spec: SourceSpec, params: dict[str, Any]) -> list[FramePlan]:
    """Resolve + window + day-filter the SLIDER jpss overpasses into frame plans."""
    sc = spec.error_code_prefix
    q_bbox = _round_bbox(params["bbox"])
    satellite = params.get("satellite", "all")
    if satellite not in VIIRS_SATELLITES:
        raise router_input_error(
            sc, f"unknown satellite={satellite!r}; allowed: {list(VIIRS_SATELLITES)}",
            spec.input_error_suffix,
        )
    product = params.get("product", "day_fire")
    product_slug = _PRODUCT_TO_SLUG.get(product)
    if product_slug is None:
        raise router_input_error(
            sc, f"unknown product={product!r}; allowed: {sorted(_PRODUCT_TO_SLUG)}",
            spec.input_error_suffix,
        )
    day_only = bool(params.get("day_only", True))
    sector = params.get("sector", "conus")

    now = datetime.now(timezone.utc)
    end_utc = params.get("end_utc")
    start_utc = params.get("start_utc")
    end_dt = _parse_utc(spec, end_utc) if end_utc else now
    start_dt = _parse_utc(spec, start_utc) if start_utc else (end_dt - timedelta(days=4))
    if start_dt >= end_dt:
        raise router_input_error(
            sc,
            f"start_utc ({start_dt.isoformat()}) must be before end_utc "
            f"({end_dt.isoformat()})",
            spec.input_error_suffix,
        )

    aoi_center_lon = (q_bbox[0] + q_bbox[2]) / 2.0

    try:
        all_ts = fetch_slider_timestamps("jpss", sector, product_slug)
    except SliderError as exc:
        raise router_upstream_error(sc, str(exc))
    pass_ts = _build_pass_list(
        all_ts, start_dt, end_dt, aoi_center_lon, day_only=day_only, cap=MAX_VIIRS_FRAMES
    )
    if not pass_ts:
        raise router_empty_error(
            sc,
            f"no {'daytime ' if day_only else ''}VIIRS Day Fire passes for jpss/"
            f"{sector} in window {start_dt.isoformat()}..{end_dt.isoformat()} "
            f"(index has {len(all_ts)} timestamps)",
            spec.empty_error_suffix,
        )

    zoom = pick_zoom_for_aoi("jpss", sector, q_bbox)
    sat_label = "JPSS" if satellite == "all" else satellite.upper()

    plans: list[FramePlan] = []
    windows = frame_windows([ts_int_to_iso(t) for t in pass_ts])
    for frame_no, ts_int in enumerate(pass_ts, start=1):
        iso = ts_int_to_iso(ts_int)
        valid_from, valid_to = windows[frame_no - 1]
        plans.append(
            FramePlan(
                cache_params={
                    "bbox": list(q_bbox),
                    "product": product_slug,
                    "sector": sector,
                    "ts_int": ts_int,
                    "zoom": zoom,
                },
                name=f"VIIRS Day Fire step {frame_no} {iso} ({sat_label})",
                layer_id=f"viirs-dayfire-{ts_int}-{q_bbox[0]:.3f}-{q_bbox[1]:.3f}",
                bbox=q_bbox,
                valid_from=valid_from,
                valid_to=valid_to,
            )
        )
    return plans


# --------------------------------------------------------------------------- #
# frame_bytes: the per-frame COG builder.
# --------------------------------------------------------------------------- #


@register_hook("viirs_day_fire.frame_bytes")
def frame_bytes(spec: SourceSpec, params: dict[str, Any], frame: FramePlan) -> bytes:
    """Stitch + reproject one VIIRS overpass -> RGB COG. FrameDegraded on empty/upstream."""
    cp = frame.cache_params
    bbox = tuple(cp["bbox"])  # type: ignore[assignment]
    try:
        rgb, mosaic_extent = stitch_slider_mosaic(
            "jpss", cp["sector"], cp["product"], cp["ts_int"], cp["zoom"], bbox
        )
        return mosaic_to_cog_bytes(rgb, mosaic_extent, bbox)
    except (SliderEmptyError, SliderUpstreamError) as exc:
        raise FrameDegraded(str(exc)) from exc
