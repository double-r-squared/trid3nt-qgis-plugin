"""goes_animation frames hooks (ADR 0087): the GOES SLIDER-stitch animation.

Folds fetch_goes_animation + fetch_goes_blend_animation onto the frames-list output
shape (shape: animation_frames). The router owns the per-frame read_through loop +
the honesty floor + the LayerURI emission; these two hooks own the source-specific
steps:

- ``frames_plan`` -- resolve the SLIDER time index (via the shared
  ``_satellite_slider`` list[int] reader), window + even-subsample it, and build the
  ordered per-frame plans. A ``band`` in the blend-token set (or the band-less blend
  delegate spec) builds ONE composite scrubber group (GeoColor base + Fire
  Temperature glow); ``geocolor`` / ``fire_temperature`` each build their own group.
- ``frame_bytes`` -- build ONE frame's RGB COG: a single SLIDER product
  stitch-mosaic, or (for the synthetic blend product) the co-temporal GeoColor +
  Fire Temperature pair blended into one composite.

The GOES satellite spelling zoo (goes18 / "GOES West" / G18 / 18) is normalized via
the shared ``_normalize_satellite`` seam (a genuinely-unknown bird raises the loud
GOESInputError); a valid-but-unserved GOES bird raises the source's typed
GOES_ANIM_INPUT_INVALID. Frame names carry the ``step <N>`` scrubber token + the ISO
valid-time. ASCII only.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from trid3nt_contracts.source_spec import SourceSpec

from ...imagery import _satellite_slider
from ...imagery._satellite_slider import (
    SliderEmptyError,
    SliderError,
    SliderUpstreamError,
    blend_geocolor_fire_temperature,
    fetch_slider_timestamps,
    mosaic_to_cog_bytes,
    pick_zoom_for_aoi,
    stitch_slider_mosaic,
    ts_int_to_datetime,
    ts_int_to_iso,
)
from ...imagery._goes_common import _normalize_satellite
from ..errors import router_empty_error, router_input_error, router_upstream_error
from . import FrameDegraded, FramePlan, register_hook

logger = logging.getLogger(
    "trid3nt_server.agent.tools.fetchers._router.hooks.goes_animation"
)

__all__ = [
    "frames_plan",
    "frame_bytes",
    "GOES_ANIM_SATELLITES",
    "GOES_BLEND_PRODUCT",
    "MAX_ANIM_FRAMES",
    "_band_to_slider_product",
    "_parse_utc",
    "_select_frame_indices",
    "_build_frame_list",
]


# --------------------------------------------------------------------------- #
# Constants (carried verbatim from the fetch_goes_animation twin).
# --------------------------------------------------------------------------- #

#: band/product name -> SLIDER product slug (CONFIRMED from define-products.js).
_BAND_TO_SLIDER_PRODUCT: dict[str, str] = {
    "geocolor": "geocolor",
    "fire_temperature": "fire_temperature",
}

#: Supported GOES satellites for the SLIDER path (West + East operational).
GOES_ANIM_SATELLITES = ("goes-18", "goes-19")

#: SLIDER product label for the LayerURI name.
_PRODUCT_LABEL: dict[str, str] = {
    "geocolor": "GeoColor",
    "fire_temperature": "Fire Temperature",
}

#: Synthetic product slug for the GeoColor + Fire Temperature per-timestep BLEND.
GOES_BLEND_PRODUCT = "geocolor_fire_temperature_blend"
_BLEND_BASE_PRODUCT = "geocolor"
_BLEND_FIRE_PRODUCT = "fire_temperature"
_BLEND_PRODUCT_LABEL = "Fire (GeoColor + Fire Temperature)"

#: ``band`` tokens that route to the BLENDED composite (GeoColor base + Fire
#: Temperature glow in ONE scrubber group).
_BLEND_BAND_TOKENS = frozenset(
    {"blend", "blended", "combined", "geocolor_fire", "geocolor_fire_temperature",
     "geocolor+fire", "geocolor_and_fire_temperature"}
)

#: Upper bound on emitted frames (mirrors postprocess_flood.MAX_FLOOD_FRAMES=144).
MAX_ANIM_FRAMES: int = int(os.environ.get("TRID3NT_MAX_ANIM_FRAMES", "144"))

_BBOX_QUANTIZE_DP = 6


# --------------------------------------------------------------------------- #
# Pure helpers (also importable for tests).
# --------------------------------------------------------------------------- #


def _parse_utc(spec: SourceSpec, value: Any) -> datetime:
    """Parse an ISO-8601 (or 'YYYY-MM-DD HH:MM') string / datetime -> aware UTC.

    Accepts a trailing 'Z', '+00:00', a space or 'T' separator, and a bare date.
    Raises the source's typed INPUT error for an unparseable value.
    """
    if isinstance(value, datetime):
        dt = value
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
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
                "(e.g. '2026-06-22T13:30:00Z')",
                spec.input_error_suffix,
            )
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _band_to_slider_product(spec: SourceSpec, band: str) -> str:
    """Map a band/product name to the SLIDER product slug."""
    try:
        return _BAND_TO_SLIDER_PRODUCT[band]
    except KeyError:
        raise router_input_error(
            spec.error_code_prefix,
            f"unknown band/product={band!r}; allowed: {sorted(_BAND_TO_SLIDER_PRODUCT)}",
            spec.input_error_suffix,
        )


def _select_frame_indices(n: int, cap: int = MAX_ANIM_FRAMES) -> list[int]:
    """Pick up to ``cap`` evenly-spaced indices over ``n`` items, endpoints kept."""
    if n <= 0:
        return []
    if n <= cap:
        return list(range(n))
    import numpy as np

    idx = np.linspace(0, n - 1, cap).round().astype(int)
    kept = [int(i) for i in np.unique(idx)]
    logger.info(
        "goes_animation: %d in-window frames exceed cap=%d; subsampling to %d.",
        n, cap, len(kept),
    )
    return kept


def _build_frame_list(
    timestamps_int: list[int],
    start_utc: datetime,
    end_utc: datetime,
    cap: int = MAX_ANIM_FRAMES,
) -> list[int]:
    """Window the SLIDER time index to [start, end] and even-subsample to ``cap``."""
    in_window = [
        ts for ts in timestamps_int if start_utc <= ts_int_to_datetime(ts) <= end_utc
    ]
    in_window.sort()
    keep = _select_frame_indices(len(in_window), cap=cap)
    return [in_window[i] for i in keep]


def _round_bbox(bbox: Any) -> tuple[float, float, float, float]:
    return tuple(round(float(v), _BBOX_QUANTIZE_DP) for v in bbox)  # type: ignore[return-value]


def _resolve_satellite(spec: SourceSpec, satellite: str) -> str:
    """Normalize the spelling zoo -> canonical bird, then gate to the served set.

    ``_normalize_satellite`` raises the loud GOESInputError for a genuinely-unknown
    bird; a valid GOES bird this tool does not serve raises the source's typed
    GOES_ANIM_INPUT_INVALID.
    """
    satellite = _normalize_satellite(satellite)
    if satellite not in GOES_ANIM_SATELLITES:
        raise router_input_error(
            spec.error_code_prefix,
            f"unknown satellite={satellite!r}; allowed: {list(GOES_ANIM_SATELLITES)}",
            spec.input_error_suffix,
        )
    return satellite


def _resolve_window(
    spec: SourceSpec, params: dict[str, Any]
) -> tuple[datetime, datetime]:
    """Default the window to the most-recent ~6.5h; validate start < end."""
    now = datetime.now(timezone.utc)
    end_utc = params.get("end_utc")
    start_utc = params.get("start_utc")
    end_dt = _parse_utc(spec, end_utc) if end_utc else now
    start_dt = (
        _parse_utc(spec, start_utc) if start_utc
        else (end_dt - timedelta(hours=6, minutes=30))
    )
    if start_dt >= end_dt:
        raise router_input_error(
            spec.error_code_prefix,
            f"start_utc ({start_dt.isoformat()}) must be before end_utc "
            f"({end_dt.isoformat()})",
            spec.input_error_suffix,
        )
    return start_dt, end_dt


# --------------------------------------------------------------------------- #
# frames_plan: the pre-loop resolve.
# --------------------------------------------------------------------------- #


@register_hook("goes_animation.frames_plan")
def frames_plan(spec: SourceSpec, params: dict[str, Any]) -> list[FramePlan]:
    """Resolve + window the SLIDER frame set into ordered per-frame plans.

    A ``band`` in the blend-token set (or a band-less blend-delegate spec) builds ONE
    composite blend group; ``geocolor`` / ``fire_temperature`` each build their own
    group. Raises the source's typed EMPTY when the window matched no frames.
    """
    sc = spec.error_code_prefix
    q_bbox = _round_bbox(params["bbox"])
    satellite = _resolve_satellite(spec, params.get("satellite", "goes-18"))
    sector = params.get("sector", "conus")
    band = params.get("band")
    is_blend = band is None or (
        isinstance(band, str) and band.strip().lower() in _BLEND_BAND_TOKENS
    )

    start_dt, end_dt = _resolve_window(spec, params)

    # The index product: the blend anchors on GeoColor (both products share the CONUS
    # 5-minute cadence); a single-product run indexes its own product.
    if is_blend:
        index_product = _BLEND_BASE_PRODUCT
        frame_product = GOES_BLEND_PRODUCT
        product_label = _BLEND_PRODUCT_LABEL
    else:
        index_product = _band_to_slider_product(spec, band)
        frame_product = index_product
        product_label = _PRODUCT_LABEL.get(index_product, index_product)

    try:
        all_ts = fetch_slider_timestamps(satellite, sector, index_product)
    except SliderError as exc:
        raise router_upstream_error(sc, str(exc))
    frame_ts = _build_frame_list(all_ts, start_dt, end_dt, cap=MAX_ANIM_FRAMES)
    if not frame_ts:
        raise router_empty_error(
            sc,
            f"no SLIDER {frame_product} frames for {satellite}/{sector} in window "
            f"{start_dt.isoformat()}..{end_dt.isoformat()} "
            f"(index has {len(all_ts)} timestamps)",
            spec.empty_error_suffix,
        )

    zoom = pick_zoom_for_aoi(satellite, sector, q_bbox)
    sat_label = satellite.upper()

    plans: list[FramePlan] = []
    for frame_no, ts_int in enumerate(frame_ts, start=1):
        iso = ts_int_to_iso(ts_int)
        if is_blend:
            layer_id = f"goes-fire-blend-{ts_int}-{q_bbox[0]:.3f}-{q_bbox[1]:.3f}"
        else:
            layer_id = (
                f"goes-anim-{frame_product}-{ts_int}-{q_bbox[0]:.3f}-{q_bbox[1]:.3f}"
            )
        plans.append(
            FramePlan(
                cache_params={
                    "bbox": list(q_bbox),
                    "product": frame_product,
                    "satellite": satellite,
                    "sector": sector,
                    "ts_int": ts_int,
                    "zoom": zoom,
                },
                # "GOES <ProductLabel> step <N> <ISO> (<SAT>)": step <N> is the
                # monotonic scrubber token; the product label keeps GeoColor / Fire
                # Temperature / the blend in distinct stems; ISO is the display label.
                name=f"GOES {product_label} step {frame_no} {iso} ({sat_label})",
                layer_id=layer_id,
                bbox=q_bbox,
            )
        )
    return plans


# --------------------------------------------------------------------------- #
# frame_bytes: the per-frame COG builder.
# --------------------------------------------------------------------------- #


def _stitch_single(
    sat: str, sector: str, product: str, ts_int: int, zoom: int,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Stitch + reproject one SLIDER frame -> 3-band EPSG:4326 RGB COG bytes."""
    rgb, mosaic_extent = stitch_slider_mosaic(sat, sector, product, ts_int, zoom, bbox)
    return mosaic_to_cog_bytes(rgb, mosaic_extent, bbox)


def _single_product_frame_bytes(
    spec: SourceSpec, sat: str, sector: str, product: str, ts_int: int, zoom: int,
    bbox: tuple[float, float, float, float],
) -> bytes:
    """Cache-mediated fetch of ONE single-product SLIDER frame COG (the blend
    consumes the GeoColor + Fire Temperature frames through this, so a frame already
    pulled for a single-product run is reused -- byte-identical cache key)."""
    from ....cache import read_through
    from ..router import synthesize_metadata

    metadata = synthesize_metadata(spec)
    result = read_through(
        metadata=metadata,
        params={
            "bbox": list(bbox), "product": product, "satellite": sat,
            "sector": sector, "ts_int": ts_int, "zoom": zoom,
        },
        ext="tif",
        fetch_fn=lambda: _stitch_single(sat, sector, product, ts_int, zoom, bbox),
    )
    return result.data


@register_hook("goes_animation.frame_bytes")
def frame_bytes(spec: SourceSpec, params: dict[str, Any], frame: FramePlan) -> bytes:
    """Build ONE frame's RGB COG (single-product stitch, or the co-temporal blend).

    Raises :class:`FrameDegraded` for a transparent / off-grid / upstream-failed
    frame (the executor records + drops it, honesty floor intact).
    """
    cp = frame.cache_params
    sat = cp["satellite"]
    sector = cp["sector"]
    product = cp["product"]
    ts_int = cp["ts_int"]
    zoom = cp["zoom"]
    bbox = tuple(cp["bbox"])  # type: ignore[assignment]
    try:
        if product == GOES_BLEND_PRODUCT:
            base = _single_product_frame_bytes(
                spec, sat, sector, _BLEND_BASE_PRODUCT, ts_int, zoom, bbox
            )
            fire = _single_product_frame_bytes(
                spec, sat, sector, _BLEND_FIRE_PRODUCT, ts_int, zoom, bbox
            )
            return blend_geocolor_fire_temperature(base, fire)
        return _stitch_single(sat, sector, product, ts_int, zoom, bbox)
    except (SliderEmptyError, SliderUpstreamError) as exc:
        raise FrameDegraded(str(exc)) from exc
