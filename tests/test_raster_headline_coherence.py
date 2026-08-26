"""A HEADLINE SCALAR AND THE RASTER PUBLISHED BESIDE IT MUST BE THE SAME ANSWER.

The failure this pins is quiet and expensive: a run says "peak concentration
28.7 mg/L" while the layer it published is painted on a scale topping out at 10,
so the reader sees a saturated blob that cannot express the number in the
sentence - or the raster's own band maximum is 12 and the sentence is simply
about a different array.

Two conditions, both necessary:

* the resolved style's RANGE CONTAINS the headline, so the picture can show it;
* the raster's BAND MAXIMUM AGREES with the headline, so they are one answer.

``check_headline_coherence`` below has no production caller yet - nothing in
``trid3nt_server`` currently compares a published band maximum against the
headline it was reported with. It lives here until a publisher wants it; the
rule is what is being pinned, not its address.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from trid3nt_contracts.styles import ScaleSpec
from trid3nt_server.emission import styles

#: A published raster and its headline are the same number rounded differently,
#: never a different computation: one percent is generous for a units or dtype
#: round trip and far too tight to hide a wrong array.
REL_TOL = 0.01
#: Below this, a relative tolerance is meaningless (a headline of exactly zero
#: has no scale of its own), so an absolute floor takes over.
ABS_TOL = 1e-6


@dataclass(frozen=True)
class Coherence:
    """The verdict plus the sentence a reader would need to act on it."""

    ok: bool
    reason: str

    def __bool__(self) -> bool:
        return self.ok


def _agree(a: float, b: float, *, rel_tol: float, abs_tol: float) -> bool:
    return abs(a - b) <= max(abs_tol, rel_tol * max(abs(a), abs(b)))


def check_headline_coherence(preset: str | None, headline: float, band_max: float, *,
                             read_range=None, override: ScaleSpec | None = None,
                             shared: tuple[float, float] | None = None,
                             rel_tol: float = REL_TOL,
                             abs_tol: float = ABS_TOL) -> Coherence:
    """Does the published raster support the headline it was reported with?

    Resolves the style exactly as the publisher does, so the range checked is the
    range painted. The message names the gap in the units the CONTRACT declares
    for the preset - a reader cannot judge "0.8 over" without knowing it is g.
    """
    resolved = styles.resolve_style(preset, read_range=read_range, override=override,
                                    shared=shared)
    units = styles.preset_units(preset)
    suffix = f" {units}" if units else ""

    if not _agree(float(headline), float(band_max), rel_tol=rel_tol, abs_tol=abs_tol):
        return Coherence(False, (
            f"headline {headline:g}{suffix} disagrees with the published band "
            f"maximum {band_max:g}{suffix}: they are not one answer"))

    lo, hi = resolved.range or (0.0, 1.0)
    if headline > hi:
        return Coherence(False, (
            f"headline {headline:g}{suffix} is {headline - hi:g}{suffix} above the "
            f"top of the scale it was painted on ({resolved.legend_note()}): the "
            "raster cannot show the number the run reported"))
    if headline < lo:
        return Coherence(False, (
            f"headline {headline:g}{suffix} is {lo - headline:g}{suffix} below the "
            f"bottom of the scale it was painted on ({resolved.legend_note()}): the "
            "raster cannot show the number the run reported"))
    return Coherence(True, f"headline {headline:g}{suffix} within "
                           f"{resolved.legend_note()}")


# --------------------------------------------------------------------------- #
# the rule
# --------------------------------------------------------------------------- #

def test_a_coherent_run_passes():
    """Headline inside the resolved range, band maximum matching it."""
    verdict = check_headline_coherence(
        "continuous_plume_concentration", headline=28.7, band_max=28.7,
        read_range=lambda _s: (0.02, 28.7))
    assert verdict.ok, verdict.reason
    assert "mg/L" in verdict.reason and "28.7" in verdict.reason


def test_a_headline_above_the_fixed_scale_fails_and_states_the_gap_in_contract_units():
    verdict = check_headline_coherence("continuous_seismic_pga", headline=1.8,
                                       band_max=1.8)
    assert not verdict.ok
    assert styles.preset_units("continuous_seismic_pga") == "g"
    assert "0.8 g" in verdict.reason, verdict.reason
    assert "fixed domain scale" in verdict.reason, (
        "the message must say which policy produced the ceiling it hit")


def test_a_headline_below_the_fixed_scale_floor_fails_the_same_way():
    verdict = check_headline_coherence("era5_2m_temperature", headline=210.0,
                                       band_max=210.0)
    assert not verdict.ok
    assert "40 K" in verdict.reason and "below" in verdict.reason, verdict.reason


def test_a_band_maximum_that_disagrees_with_the_headline_fails():
    verdict = check_headline_coherence(
        "continuous_plume_concentration", headline=28.7, band_max=12.0,
        read_range=lambda _s: (0.02, 28.7))
    assert not verdict.ok
    assert "28.7" in verdict.reason and "12" in verdict.reason
    assert "not one answer" in verdict.reason


@pytest.mark.parametrize("run_max", [0.004, 1.0, 28.7, 1750.0, 9.6e5])
def test_a_data_policy_preset_always_contains_its_own_maximum(run_max):
    """The property ``policy: data`` buys: the run cannot leave its own scale."""
    verdict = check_headline_coherence(
        "continuous_plume_concentration", headline=run_max, band_max=run_max,
        read_range=lambda _s: (0.0, run_max))
    assert verdict.ok, verdict.reason


def test_a_fixed_preset_can_be_left_behind_where_a_data_preset_cannot():
    """The same headline, the same quantity, two policies - one of them fails."""
    fixed = check_headline_coherence(
        "continuous_plume_concentration", headline=28.7, band_max=28.7,
        override=ScaleSpec(policy="fixed", range=(0.0, 10.0)))
    from_data = check_headline_coherence(
        "continuous_plume_concentration", headline=28.7, band_max=28.7,
        read_range=lambda _s: (0.0, 28.7))
    assert not fixed.ok and from_data.ok
    assert "18.7 mg/L above" in fixed.reason, fixed.reason


def test_near_zero_values_do_not_trip_the_relative_tolerance():
    """A headline of zero has no scale of its own; the absolute floor carries it."""
    verdict = check_headline_coherence(
        "continuous_plume_concentration", headline=0.0, band_max=8e-7,
        read_range=lambda _s: (0.0, 0.5))
    assert verdict.ok, verdict.reason

    # Still absolute, not permissive: a hundredfold of the floor is a real gap.
    assert not check_headline_coherence(
        "continuous_plume_concentration", headline=0.0, band_max=1e-4,
        read_range=lambda _s: (0.0, 0.5)).ok


def test_the_relative_tolerance_holds_at_scale():
    """One percent of a big number, not one percent of the floor."""
    assert check_headline_coherence(
        "continuous_head_m", headline=1000.0, band_max=1005.0,
        read_range=lambda _s: (0.0, 1005.0)).ok
    assert not check_headline_coherence(
        "continuous_head_m", headline=1000.0, band_max=1030.0,
        read_range=lambda _s: (0.0, 1030.0)).ok


def test_a_shared_comparison_range_is_the_range_that_gets_checked():
    """A compared set is painted on one range, so that is what must contain it."""
    shared = styles.shared_range([(0.0, 4.0), (0.0, 28.7)])
    assert check_headline_coherence(
        "continuous_plume_concentration", headline=28.7, band_max=28.7,
        shared=shared).ok
    assert not check_headline_coherence(
        "continuous_plume_concentration", headline=28.7, band_max=28.7,
        shared=(0.0, 4.0)).ok
