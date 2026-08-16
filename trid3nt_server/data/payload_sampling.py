"""Sampled payload-size estimation (resolution doctrine R-B, 2026-08-11).

The payload-warning gate quotes a tool's projected emitted-COG size so the user can
"proceed native / coarsen / cancel" against REAL numbers. An analytic
bytes-per-square-degree model is a coarse guess: it ignores the source's true native
cell size, the real compression ratio of the data, AND the fetcher's own pixel-count
guard (so it wildly over-quotes a large domain whose grid is actually px-capped).

R-B replaces the guess with a MEASUREMENT: sample a SMALL native-resolution window of
the real source, measure the output COG's bytes-per-pixel and native pixel density,
then scale by the target AOI's area (bounded by the fetcher's px cap). The measured
density is cached per source + coarse region so the gate does not re-sample every
dispatch; when sampling fails (offline / network) the estimate falls back to the
analytic model, LABELED so the gate text never claims a measured number it does not
have. This module is source-agnostic: a tool supplies a ``sample_fn`` that returns the
measured density for its own source; the cache + area-scaling + labeling live here.
"""
from __future__ import annotations

import logging
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("trid3nt_server.data.payload_sampling")

#: Per-dimension pixel cap the raster fetchers honour (fetch_topobathy _MAX_DIM). The
#: emitted grid never exceeds this on either side, so the measured payload has a
#: CEILING independent of AOI size -- the analytic model misses this and over-quotes.
DEFAULT_PX_CAP: int = 12000

#: Bounded LRU of measured densities, keyed ``"<source>|<region-bucket>"``. Small: a
#: handful of coasts per session. Guarded by a lock (the gate estimator may run in a
#: worker thread via ``asyncio.to_thread``).
_CACHE_MAX = 64
_CACHE: "OrderedDict[str, SampledDensity]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class SampledDensity:
    """A source's MEASURED emit density, from one small native-resolution window.

    ``bytes_per_px`` -- output COG bytes per pixel (captures real dtype + compression).
    ``px_per_sq_deg`` -- native pixel count per square degree (captures the source's
    true native cell size). Both are measured from the SAME sampled window."""

    bytes_per_px: float
    px_per_sq_deg: float


@dataclass(frozen=True)
class SampledEstimate:
    """A payload estimate + the provenance of HOW it was produced.

    ``mb`` -- the number the gate quotes. ``kind`` -- ``"measured"`` (from a sampled
    window) or ``"analytic"`` (the fallback model). ``px`` -- the projected (capped)
    pixel count, for the gate detail line."""

    mb: float
    kind: str
    px: int


def _region_bucket(bbox: tuple[float, float, float, float], deg: float) -> str:
    """Floor the AOI's SW corner to a ``deg``-degree grid -- the cache region key."""
    w, s, _e, _n = bbox
    return f"{math.floor(w / deg) * deg:.1f},{math.floor(s / deg) * deg:.1f}"


def _cache_get(key: str) -> SampledDensity | None:
    with _CACHE_LOCK:
        val = _CACHE.get(key)
        if val is not None:
            _CACHE.move_to_end(key)
        return val


def _cache_put(key: str, density: SampledDensity) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = density
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


def get_density(
    source_key: str,
    bbox: tuple[float, float, float, float],
    sample_fn: Callable[[tuple[float, float, float, float]], SampledDensity | None],
    *,
    region_deg: float = 1.0,
) -> SampledDensity | None:
    """Return the source's measured density for the AOI's region, sampling on a miss.

    ``sample_fn(window_bbox)`` builds a small native window and returns the measured
    :class:`SampledDensity`, or ``None`` when it cannot (offline / no coverage). The
    result is cached per ``source_key`` + coarse region so the gate samples a region
    at most once. Never raises -- a failed sample returns ``None`` (analytic fallback).
    """
    key = f"{source_key}|{_region_bucket(bbox, region_deg)}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    try:
        density = sample_fn(bbox)
    except Exception as exc:  # noqa: BLE001 -- sampling is best-effort
        logger.info("payload_sampling: sample failed for %s (%s); analytic fallback",
                    key, exc)
        return None
    if density is None or density.bytes_per_px <= 0 or density.px_per_sq_deg <= 0:
        return None
    _cache_put(key, density)
    return density


def bbox_km(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    """AOI (width_km, height_km) by the equirectangular approx at the AOI mid-latitude."""
    w, s, e, n = bbox
    mid = math.radians(0.5 * (s + n))
    width_km = max(abs(e - w) * 111.320 * math.cos(mid), 1e-6)
    height_km = max(abs(n - s) * 110.540, 1e-6)
    return width_km, height_km


def estimate_mb(
    source_key: str,
    bbox: tuple[float, float, float, float],
    *,
    analytic_mb: float,
    sample_fn: Callable[[tuple[float, float, float, float]], SampledDensity | None] | None,
    resolution_m: float | None,
    px_cap: int = DEFAULT_PX_CAP,
    region_deg: float = 1.0,
    analytic_native_res_m: float = 10.0,
) -> SampledEstimate:
    """Projected emitted-COG MB for ``bbox`` at ``resolution_m`` (``None`` = native).

    Measured path: scale the sampled density by the AOI area, bounded by ``px_cap`` per
    side. Native uses the sampled native pixel density; an explicit ``resolution_m``
    projects the pixel count from the AOI extent / requested cell. Falls back to
    ``analytic_mb`` (LABELED ``kind="analytic"``) when no measured density is available;
    the analytic fallback stays resolution-aware -- ``analytic_mb`` is the NATIVE
    (``analytic_native_res_m``) estimate and a coarser cell scales it by the pixel-count
    ratio, so the coarsening suggestion is meaningful even offline.
    """
    w, s, e, n = bbox
    sq_deg = max(0.0, e - w) * max(0.0, n - s)
    density = get_density(source_key, bbox, sample_fn, region_deg=region_deg) if sample_fn else None
    if density is None or sq_deg <= 0:
        if resolution_m is None:
            mb = analytic_mb
        else:
            ratio = analytic_native_res_m / max(float(resolution_m), 1e-6)
            mb = analytic_mb * ratio * ratio
        return SampledEstimate(mb=max(mb, 0.5), kind="analytic", px=0)

    if resolution_m is None:
        px = density.px_per_sq_deg * sq_deg
    else:
        width_km, height_km = bbox_km(bbox)
        res_m = max(float(resolution_m), 1e-6)
        px = (width_km * 1000.0 / res_m) * (height_km * 1000.0 / res_m)
    px = min(px, float(px_cap) * float(px_cap))
    mb = max(px * density.bytes_per_px / 1e6, 0.5)
    return SampledEstimate(mb=mb, kind="measured", px=int(px))
