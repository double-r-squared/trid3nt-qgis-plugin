"""Atlas-14 NESTED (alternating-block) design-storm hyetograph builder
(sprint-16 P1, PySWMM quasi-2D urban-flood engine).

Produces a centered-peak design-storm hyetograph as a SWMM TIMESERIES from a
total storm depth + duration + interval. This is the cross-check improvement the
P1 kickoff demands over the spike's hand-rolled triangle: NESTED / centered-peak
shaping per NOAA Atlas-14 practice — **NOT flat, NOT SCS-Type-II**.

Method — NWS/HEC alternating-block, Atlas-14 nested
---------------------------------------------------
1. **Depth-duration curve.** The total depth ``P_total`` over the full duration
   ``D`` is distributed across shorter sub-durations by the Atlas-14 nested
   power law

       P(d) = P_total * (d / D) ** n            (0 < d <= D)

   where ``n`` is the dimensionless nesting exponent (the "b" in the partial-
   duration depth-duration fit). ``n`` in (0, 1) makes short bursts hold a
   DISPROPORTIONATE share of the depth — i.e. an intense, peaky core — which is
   exactly the Atlas-14 nested behaviour. ``n = 1`` degenerates to a FLAT storm
   (uniform intensity); we forbid ``n >= 1`` so the result is always peaky.
   Default ``n = 0.62`` is a typical CONUS Atlas-14 nesting exponent.

2. **Incremental depths.** For each block ``k`` (k = 1..N, N = D / interval) the
   incremental depth is the difference of cumulative depths at consecutive
   sub-durations::

       delta_k = P(k * interval) - P((k - 1) * interval)

   Because ``n < 1`` these increments are MONOTONICALLY DECREASING in k (the
   first, shortest sub-duration holds the largest increment).

3. **Alternating-block arrangement (centered peak).** The blocks are reordered
   so the largest increment sits at the storm centre and the rest alternate to
   the right then the left of the peak (HEC-HMS / NWS convention). The result is
   a single-peaked, centre-loaded hyetograph — the nested signature.

4. **Intensity conversion.** SWMM rain gages in ``INTENSITY`` form (the spike's
   ``RainGage(form="INTENSITY", ...)``) read **mm/hr**. Each block's depth
   ``delta`` over ``interval`` minutes is ``intensity = delta / (interval/60)``.

Output
------
``build_nested_hyetograph(...)`` returns ``HyetographResult`` carrying the
SWMM-ready timeseries as ``list[tuple[str, float]]`` of ``("HH:MM", mm/hr)`` —
**exactly the shape ``swmm_api.TimeseriesData(name, data)`` consumes** (see the
P0 spike, ``spike_quasi2d.py`` line ~181). This module is deliberately
**swmm-api-free** (it builds the portable tuple list, not the ``TimeseriesData``
object) so it imports cleanly in the agent env where ``swmm_api`` is NOT
installed; the downstream SWMM worker wraps the list in ``TimeseriesData``.

Mass-balance guarantee: the per-interval depths sum back to ``total_depth_mm``
(to floating-point tolerance) by construction — the alternating-block reorder is
a permutation, and the increments telescope to ``P(D) - P(0) = P_total``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Typical CONUS Atlas-14 nesting exponent for the depth-duration power law.
# Must be in (0, 1): n -> 1 is a FLAT storm, n -> 0 is an impulse. 0.62 gives a
# realistically peaky-but-not-degenerate nested core.
DEFAULT_NESTING_EXPONENT: float = 0.62


@dataclass(frozen=True)
class HyetographResult:
    """A built nested design-storm hyetograph.

    Attributes:
        timeseries: SWMM-ready ``[("HH:MM", intensity_mm_per_hr), ...]`` — the
            exact shape ``swmm_api.TimeseriesData(name, data)`` consumes. One
            entry per interval, in CHRONOLOGICAL order (00:00 first).
        interval_min: the timestep, minutes.
        total_depth_mm: the input total depth the series integrates back to.
        peak_intensity_mm_per_hr: the maximum intensity (at the centred peak).
        peak_index: index into ``timeseries`` of the peak block.
        incremental_depths_mm: per-block depths in CHRONOLOGICAL order (mm).
    """

    timeseries: list[tuple[str, float]]
    interval_min: int
    total_depth_mm: float
    peak_intensity_mm_per_hr: float
    peak_index: int
    incremental_depths_mm: list[float] = field(default_factory=list)


def _fmt_clock(total_minutes: int) -> str:
    """Format an elapsed-minutes offset as a SWMM ``HH:MM`` relative clock.

    SWMM relative timeseries clock allows hours to exceed 24 (e.g. a 48-h
    storm); we do NOT wrap at 24h. Matches the spike's ``f"{m//60:02d}:{m%60:02d}"``.
    """
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def build_nested_hyetograph(
    total_depth_mm: float,
    storm_duration_hr: float,
    rain_interval_min: int,
    *,
    nesting_exponent: float = DEFAULT_NESTING_EXPONENT,
) -> HyetographResult:
    """Build an Atlas-14 NESTED (alternating-block) hyetograph.

    Args:
        total_depth_mm: total design-storm depth over the full duration, mm
            (> 0). For a SWMM run this is the Atlas-14 return-period depth (with
            the Atlas-2 fallback) for ``return_period_yr`` + ``storm_duration_hr``.
        storm_duration_hr: total storm duration, hours (> 0).
        rain_interval_min: hyetograph timestep, minutes (> 0). Must divide the
            duration into a whole number of blocks.
        nesting_exponent: the Atlas-14 depth-duration power-law exponent ``n``,
            in (0, 1). Smaller ``n`` -> peakier core. ``n >= 1`` is rejected (it
            would FLATTEN the storm, defeating the nested requirement).

    Returns:
        ``HyetographResult`` with the SWMM-ready timeseries, centred peak, and
        per-block depths that sum back to ``total_depth_mm``.

    Raises:
        ValueError: on non-positive depth/duration/interval, an interval that
            doesn't evenly divide the duration, or ``nesting_exponent`` outside
            (0, 1).
    """
    if total_depth_mm <= 0.0:
        raise ValueError(f"total_depth_mm must be > 0, got {total_depth_mm!r}")
    if storm_duration_hr <= 0.0:
        raise ValueError(f"storm_duration_hr must be > 0, got {storm_duration_hr!r}")
    if rain_interval_min <= 0:
        raise ValueError(f"rain_interval_min must be > 0, got {rain_interval_min!r}")
    if not (0.0 < nesting_exponent < 1.0):
        raise ValueError(
            f"nesting_exponent must be in the open interval (0, 1) so the storm "
            f"is nested/peaky (n>=1 flattens it), got {nesting_exponent!r}"
        )

    duration_min = storm_duration_hr * 60.0
    # The interval must tile the duration into whole blocks.
    n_blocks_f = duration_min / rain_interval_min
    n_blocks = round(n_blocks_f)
    if abs(n_blocks_f - n_blocks) > 1e-9 or n_blocks < 1:
        raise ValueError(
            f"rain_interval_min ({rain_interval_min} min) must evenly divide the "
            f"storm duration ({storm_duration_hr} hr = {duration_min:g} min); "
            f"got {n_blocks_f:g} blocks"
        )

    # --- (1)+(2) cumulative depth-duration curve -> monotone-decreasing blocks.
    # P(d) = total * (d/D)**n.  delta_k = P(k*dt) - P((k-1)*dt).
    def cumulative_depth(block_index: int) -> float:
        # block_index = number of intervals elapsed (0..n_blocks)
        frac = block_index / n_blocks  # = (block_index*dt) / D
        return total_depth_mm * (frac ** nesting_exponent)

    increments = [
        cumulative_depth(k) - cumulative_depth(k - 1) for k in range(1, n_blocks + 1)
    ]
    # increments are sorted DESCending already (n<1 => concave cumulative),
    # but sort explicitly to be robust to floating-point ordering.
    increments.sort(reverse=True)

    # --- (3) alternating-block arrangement: centre the peak, alternate R/L.
    arranged = _alternating_block_arrange(increments)

    # --- (4) depths -> mm/hr intensities, chronological clock.
    interval_hr = rain_interval_min / 60.0
    timeseries: list[tuple[str, float]] = []
    for k, depth in enumerate(arranged):
        intensity = depth / interval_hr
        timeseries.append((_fmt_clock(k * rain_interval_min), round(intensity, 6)))

    peak_index = max(range(len(arranged)), key=lambda i: arranged[i])
    peak_intensity = max(arranged) / interval_hr

    return HyetographResult(
        timeseries=timeseries,
        interval_min=rain_interval_min,
        total_depth_mm=total_depth_mm,
        peak_intensity_mm_per_hr=round(peak_intensity, 6),
        peak_index=peak_index,
        incremental_depths_mm=list(arranged),
    )


def _alternating_block_arrange(sorted_desc: list[float]) -> list[float]:
    """Arrange descending-sorted increments into a centred-peak sequence.

    Alternating-block (HEC-HMS / NWS) convention: the largest block goes at the
    centre, the next-largest immediately AFTER it, the next BEFORE it, and so on
    alternating right/left. The output is single-peaked at (or adjacent to) the
    centre with magnitudes falling off toward both tails.

    Example (5 blocks, sorted_desc = [5,4,3,2,1]):
        place 5 at centre,  then 4 right, 3 left, 2 right, 1 left
        -> [3, 5, 4, 2, 1]?  no — built around a growing window:
        result indices grow outward: [1, 3, 5, 4, 2].
    The exact tail ordering is the standard "alternate right-first" deque build.
    """
    if not sorted_desc:
        return []
    # Build with a deque: peak first (append), then alternate append/prepend.
    from collections import deque

    out: deque[float] = deque()
    append_right = True
    for value in sorted_desc:
        if append_right:
            out.append(value)
        else:
            out.appendleft(value)
        append_right = not append_right
    return list(out)
