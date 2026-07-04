"""Unit tests for the Atlas-14 NESTED (alternating-block) hyetograph builder
(sprint-16 P1, ``grace2_agent.workflows.swmm_hyetograph``).

The contract the SWMM engine relies on:
1. **Mass conservation** — the per-interval depths integrate back to the input
   total depth (the SWMM run's rainfall volume must be the design-storm depth).
2. **Nested / centered-peak shape** — the peak sits at/near the storm centre and
   intensities fall off toward both tails (NOT flat, NOT a leading/trailing
   SCS-Type-II ramp). The peak intensity exceeds the flat-storm average.
3. **SWMM TIMESERIES shape** — output is ``[("HH:MM", mm/hr), ...]`` in
   chronological order, exactly what ``swmm_api.TimeseriesData(name, data)``
   consumes. (The module is swmm-api-free so it imports in the agent env.)
"""

from __future__ import annotations

import pytest

from grace2_agent.workflows.swmm_hyetograph import (
    DEFAULT_NESTING_EXPONENT,
    build_nested_hyetograph,
)


# --------------------------------------------------------------------------- #
# (1) Mass conservation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("total", "duration_hr", "interval_min"),
    [
        (152.4, 6.0, 5),   # 100-yr-ish, 6 h, 5-min steps (the spike timing)
        (101.6, 24.0, 15),  # longer storm, coarser step
        (50.0, 1.0, 5),    # short intense storm
        (200.0, 2.0, 10),  # the spike's 2-h framing
    ],
)
def test_integrates_back_to_total_depth(total, duration_hr, interval_min) -> None:
    """Sum(intensity * interval_hr) over all blocks == total input depth."""
    res = build_nested_hyetograph(total, duration_hr, interval_min)
    interval_hr = interval_min / 60.0
    integrated = sum(intensity * interval_hr for _, intensity in res.timeseries)
    assert integrated == pytest.approx(total, rel=1e-4)
    # The incremental depths also sum back (the reorder is a permutation).
    assert sum(res.incremental_depths_mm) == pytest.approx(total, rel=1e-9)


def test_block_count_matches_duration_over_interval() -> None:
    res = build_nested_hyetograph(152.4, 6.0, 5)
    assert len(res.timeseries) == int(6.0 * 60 / 5) == 72


# --------------------------------------------------------------------------- #
# (2) Nested / centered-peak shape
# --------------------------------------------------------------------------- #


def test_peak_is_centered() -> None:
    """The maximum intensity sits at (or immediately adjacent to) the centre."""
    res = build_nested_hyetograph(152.4, 6.0, 5)
    n = len(res.timeseries)
    centre = (n - 1) / 2.0
    # alternating-block centres the peak within one block of the midpoint
    assert abs(res.peak_index - centre) <= 1.0


def test_shape_is_not_flat() -> None:
    """A nested storm is peaky: peak intensity strictly exceeds the mean."""
    total, dur, interval = 152.4, 6.0, 5
    res = build_nested_hyetograph(total, dur, interval)
    intensities = [i for _, i in res.timeseries]
    mean_intensity = total / dur  # mm/hr average over the whole storm
    assert res.peak_intensity_mm_per_hr > mean_intensity * 1.5
    # not every block equal (a flat storm would have identical intensities)
    assert max(intensities) > min(intensities) + 1e-6


def test_monotone_falloff_from_peak_both_sides() -> None:
    """Intensities decrease monotonically moving outward from the peak on both
    sides — the single-peaked nested signature."""
    res = build_nested_hyetograph(152.4, 6.0, 5)
    intensities = [i for _, i in res.timeseries]
    p = res.peak_index
    # left of peak: non-decreasing up to the peak
    for k in range(1, p + 1):
        assert intensities[k] >= intensities[k - 1] - 1e-9
    # right of peak: non-increasing after the peak
    for k in range(p + 1, len(intensities)):
        assert intensities[k] <= intensities[k - 1] + 1e-9


def test_not_scs_type_ii_leading_or_trailing_load() -> None:
    """SCS-Type-II loads the peak ~67% through the storm (off-centre, trailing).
    The nested form must NOT do that — peak stays centred, and the first and
    last blocks are the SMALLEST (tails), not a ramp to a late peak."""
    res = build_nested_hyetograph(152.4, 6.0, 5)
    intensities = [i for _, i in res.timeseries]
    # the two tail blocks are the smallest in the series
    assert intensities[0] == pytest.approx(min(intensities))
    assert intensities[-1] == pytest.approx(min(intensities), abs=intensities[0])
    # peak is NOT at the 2/3 mark (the SCS-Type-II location)
    two_thirds = int(round(len(intensities) * 2 / 3))
    assert res.peak_index != two_thirds


def test_smaller_exponent_makes_a_peakier_storm() -> None:
    """Lower nesting exponent -> more depth concentrated in the core -> higher
    peak intensity (monotone in the exponent)."""
    peaky = build_nested_hyetograph(152.4, 6.0, 5, nesting_exponent=0.4)
    milder = build_nested_hyetograph(152.4, 6.0, 5, nesting_exponent=0.8)
    assert peaky.peak_intensity_mm_per_hr > milder.peak_intensity_mm_per_hr
    # both still integrate to the same total
    interval_hr = 5 / 60.0
    for res in (peaky, milder):
        integrated = sum(i * interval_hr for _, i in res.timeseries)
        assert integrated == pytest.approx(152.4, rel=1e-4)


# --------------------------------------------------------------------------- #
# (3) SWMM TIMESERIES shape
# --------------------------------------------------------------------------- #


def test_timeseries_is_swmm_shape() -> None:
    """Output is [("HH:MM", float), ...] in chronological order from 00:00."""
    res = build_nested_hyetograph(152.4, 6.0, 5)
    assert res.timeseries[0][0] == "00:00"
    # second entry is at +5 min
    assert res.timeseries[1][0] == "00:05"
    # last entry is at (n-1)*interval = 71*5 = 355 min = 05:55
    assert res.timeseries[-1][0] == "05:55"
    for clock, intensity in res.timeseries:
        assert isinstance(clock, str) and ":" in clock
        assert isinstance(intensity, float)
        assert intensity >= 0.0


def test_clock_does_not_wrap_past_24h() -> None:
    """A >24 h storm keeps an incrementing hour field (SWMM relative clock)."""
    res = build_nested_hyetograph(300.0, 26.0, 60)  # 26 one-hour blocks
    assert len(res.timeseries) == 26
    assert res.timeseries[-1][0] == "25:00"  # 25 h offset, not wrapped to 01:00


def test_default_exponent_is_used() -> None:
    a = build_nested_hyetograph(152.4, 6.0, 5)
    b = build_nested_hyetograph(
        152.4, 6.0, 5, nesting_exponent=DEFAULT_NESTING_EXPONENT
    )
    assert a.timeseries == b.timeseries


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("total", [0.0, -10.0])
def test_rejects_nonpositive_depth(total) -> None:
    with pytest.raises(ValueError):
        build_nested_hyetograph(total, 6.0, 5)


@pytest.mark.parametrize("dur", [0.0, -6.0])
def test_rejects_nonpositive_duration(dur) -> None:
    with pytest.raises(ValueError):
        build_nested_hyetograph(152.4, dur, 5)


@pytest.mark.parametrize("interval", [0, -5])
def test_rejects_nonpositive_interval(interval) -> None:
    with pytest.raises(ValueError):
        build_nested_hyetograph(152.4, 6.0, interval)


def test_rejects_interval_not_dividing_duration() -> None:
    # 6 h = 360 min; 7-min interval does not divide evenly.
    with pytest.raises(ValueError):
        build_nested_hyetograph(152.4, 6.0, 7)


@pytest.mark.parametrize("n", [0.0, 1.0, 1.5, -0.2])
def test_rejects_exponent_outside_open_unit_interval(n) -> None:
    """n must be in (0, 1): n>=1 would flatten the storm (no longer nested)."""
    with pytest.raises(ValueError):
        build_nested_hyetograph(152.4, 6.0, 5, nesting_exponent=n)


def test_single_block_storm() -> None:
    """Degenerate 1-block storm: all depth in one interval, peak at index 0."""
    res = build_nested_hyetograph(60.0, 1.0, 60)  # one 60-min block
    assert len(res.timeseries) == 1
    assert res.peak_index == 0
    # 60 mm over 1 h = 60 mm/hr
    assert res.timeseries[0][1] == pytest.approx(60.0)
