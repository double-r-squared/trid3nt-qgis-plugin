"""Projected emitted-COG size from a measured sample of the real source.
Source-agnostic: a tool supplies the ``sample_fn`` that measures its own source,
while caching, area-scaling and labelling live here. A failed sample never raises
- it falls back to the caller's analytic model, LABELED, so no quoted number is
ever claimed as measured when it is not."""
from __future__ import annotations

import logging
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("trid3nt_server.tools.payload_sampling")

#: Per-dimension pixel cap the raster fetchers honour; must stay in lock-step with
#: theirs. The emitted grid never exceeds it on either side, so the projected payload
#: has a CEILING independent of AOI size.
DEFAULT_PX_CAP: int = 12000

#: Bounded LRU of measured densities, keyed ``"<source>|<region-bucket>"``. Lock-
#: guarded: the gate estimator may run in a worker thread via ``asyncio.to_thread``.
_CACHE_MAX = 64
_CACHE: "OrderedDict[str, SampledDensity]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class SampledDensity:
    """A source's measured emit density. Both fields come from the SAME sampled
    window at the source's native resolution."""

    bytes_per_px: float
    px_per_sq_deg: float


@dataclass(frozen=True)
class SampledEstimate:
    """A payload estimate carrying how it was produced. ``kind`` is exactly
    ``"measured"`` (sampled window) or ``"analytic"`` (fallback model); ``px`` is 0
    when analytic."""

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
    """Measured density for the AOI's region, sampling on a miss and caching per
    ``source_key`` + coarse region. Never raises: ``sample_fn`` returning ``None`` or
    failing yields ``None``."""
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
    ``analytic_mb`` is the caller's NATIVE-resolution estimate, used and labelled
    ``kind="analytic"`` whenever no measured density is available."""
    w, s, e, n = bbox
    sq_deg = max(0.0, e - w) * max(0.0, n - s)
    density = get_density(source_key, bbox, sample_fn, region_deg=region_deg) if sample_fn else None
    if density is None or sq_deg <= 0:
        # The analytic fallback stays resolution-aware so the coarsening suggestion is
        # meaningful offline: pixel count scales with the square of the cell-size
        # ratio against the native resolution ``analytic_mb`` was quoted at.
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
