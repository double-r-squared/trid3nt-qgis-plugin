"""``fetch_slider_timestamps`` -- CIRA/RAMMB SLIDER availability + cadence index.

Registered atomic tool exposing the SLIDER ``latest_times.json`` availability
list for a (satellite, sector, product): the timestamps for which pre-rendered
imagery tiles EXIST. It is the availability + auto-snap primitive the playground
frame-animation recipe stands on -- an agent reads it to learn which frames are
available (and at what cadence) BEFORE fetching per-frame imagery via
``fetch_goes_animation`` / ``fetch_goes_blend_animation`` / ``fetch_viirs_day_fire``,
so a requested window can be snapped to real frames instead of guessed.

Thin wrapper: it delegates to the shared ``_satellite_slider.fetch_slider_timestamps``
helper (UNCHANGED) and enriches the raw ``list[int]`` into an LLM-friendly dict
(count, ascending ints, earliest/latest ISO, a coarse cadence estimate). No
network behavior change -- it is the same single ``latest_times.json`` read.

``live-no-cache`` by construction: an availability index turns over every few
minutes (new frames land continuously), so a cached list would go stale and make
the auto-snap miss the newest frames. Always a fresh read.

ASCII only.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.tools.fetchers.imagery import _satellite_slider

__all__ = ["fetch_slider_timestamps"]


def _estimate_cadence_seconds(ts_int_ascending: list[int]) -> float | None:
    """Median gap (seconds) between consecutive frames, or None for < 2 frames."""
    if len(ts_int_ascending) < 2:
        return None
    dts = [
        _satellite_slider.ts_int_to_datetime(b)
        - _satellite_slider.ts_int_to_datetime(a)
        for a, b in zip(ts_int_ascending, ts_int_ascending[1:])
    ]
    secs = sorted(d.total_seconds() for d in dts)
    mid = len(secs) // 2
    if len(secs) % 2:
        return float(secs[mid])
    return float((secs[mid - 1] + secs[mid]) / 2.0)


_METADATA = AtomicToolMetadata(
    name="fetch_slider_timestamps",
    ttl_class="live-no-cache",
    cacheable=False,
)


@register_tool(_METADATA, read_only_hint=True, open_world_hint=True)
def fetch_slider_timestamps(
    sat: str,
    sector: str,
    product: str,
    # absorb LLM-invented kwargs.
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """List the AVAILABLE CIRA/RAMMB SLIDER imagery frames for a product.

    Reads the SLIDER ``latest_times.json`` availability index and returns the
    timestamps (ascending) for which imagery tiles exist, plus the earliest /
    latest ISO time and an estimated frame cadence. This is the AVAILABILITY +
    auto-snap primitive: call it FIRST to learn which frames exist (and how
    often they land), then fetch the actual imagery per frame with
    ``fetch_goes_animation`` / ``fetch_goes_blend_animation`` /
    ``fetch_viirs_day_fire`` and window your animation to real frames.

    When to use:
        - "which GOES/VIIRS frames are available over this window?"
        - "what is the SLIDER cadence for this product?"
        - snapping a requested animation window to the nearest real frames
          (the frame-animation playground recipe's availability step).

    When NOT to use:
        - fetching the imagery itself (use the ``*_animation`` fetchers).
        - historical raw-archive access (use ``fetch_goes_archive_animation``).

    Params:
        sat: SLIDER satellite id -- one of ``"goes-18"``, ``"goes-19"``,
            ``"jpss"``.
        sector: SLIDER sector for that satellite -- e.g. ``"conus"`` /
            ``"full_disk"`` (GOES) or ``"conus"`` / ``"northern_hemisphere"`` /
            ``"southern_hemisphere"`` (jpss).
        product: SLIDER product slug -- e.g. ``"geocolor"``,
            ``"fire_temperature"``, ``"band_02"`` (GOES ABI) or
            ``"day_land_cloud_fire"`` (VIIRS Day Fire).

    Returns:
        A dict: ``sat`` / ``sector`` / ``product`` (echoed), ``count`` (n
        frames), ``timestamps_int`` (ascending 14-digit YYYYMMDDHHMMSS ints),
        ``earliest_iso`` / ``latest_iso`` (ISO-8601 UTC or None), and
        ``cadence_seconds`` (median inter-frame gap, or None for < 2 frames).

    Raises:
        ``SliderUpstreamError`` on a network / parse failure (upstream provider
        error -- surfaced verbatim, never internalized).
    """
    ts_int = _satellite_slider.fetch_slider_timestamps(sat, sector, product)
    ts_int = sorted(int(v) for v in ts_int)
    earliest_iso = _satellite_slider.ts_int_to_iso(ts_int[0]) if ts_int else None
    latest_iso = _satellite_slider.ts_int_to_iso(ts_int[-1]) if ts_int else None
    return {
        "sat": sat,
        "sector": sector,
        "product": product,
        "count": len(ts_int),
        "timestamps_int": ts_int,
        "earliest_iso": earliest_iso,
        "latest_iso": latest_iso,
        "cadence_seconds": _estimate_cadence_seconds(ts_int),
    }
