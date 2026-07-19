"""Offline unit tests: release-point seed preference (pure function; no network).

Pins the 2026-07-18 live failure class: the LLM passed only bare
release_lat/release_lon (no river name), reach resolution centered on the
geocoded CITY, and the corridor grabbed the nearest water body - a Longview
prompt meshed the Cowlitz instead of the Columbia, and the built mesh did not
even contain the requested release point (release lon -122.9345 vs mesh_bbox
[-122.9225 .. -122.8821]).

``resolve_centerline_seed`` is the pure decision seam
``fetch_river_centerline`` now routes through:

  * ``seed_from_release`` armed + plausible coords -> the RELEASE point wins
    (kind "release-position"; the gnis river_name preference still applies
    downstream, centered on this point),
  * ``seed_from_release`` off (absent coords OR a gate-picked click) -> the
    geocode seed is kept byte-for-byte (kind "position"), so the proven
    location-seeded paths and the BK-3b previewed-mesh reproducibility are
    unchanged,
  * implausible coords (partial / non-numeric / out-of-range / NaN / inf)
    always keep the seed - honest degrade, never a crash.

Run: python -m pytest services/workers/telemac/tests/ -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from telemac_river_dye_build import resolve_centerline_seed

# The live Longview case: the geocoded city center vs the requested Columbia
# release point that fell outside the built (Cowlitz) mesh bbox.
LONGVIEW_CITY = (-122.9382, 46.1382)
COLUMBIA_RELEASE = (-122.9345, 46.1060)


def test_absent_release_keeps_seed():
    assert resolve_centerline_seed(*LONGVIEW_CITY, None, None) == (
        *LONGVIEW_CITY, "position")


def test_absent_release_keeps_seed_even_when_armed():
    assert resolve_centerline_seed(
        *LONGVIEW_CITY, None, None, seed_from_release=True
    ) == (*LONGVIEW_CITY, "position")


def test_armed_plausible_release_wins():
    # THE regression pin: the Columbia release point must relocate the reach.
    assert resolve_centerline_seed(
        *LONGVIEW_CITY, *COLUMBIA_RELEASE, seed_from_release=True
    ) == (*COLUMBIA_RELEASE, "release-position")


def test_gate_click_never_relocates_the_reach():
    # BK-3b: a gate-picked click arrives with seed_from_release=False - the
    # approved solve must reproduce the previewed (location-seeded) mesh, so
    # the click moves the SOURCE only, never the seed.
    assert resolve_centerline_seed(
        *LONGVIEW_CITY, *COLUMBIA_RELEASE, seed_from_release=False
    ) == (*LONGVIEW_CITY, "position")


def test_default_is_seed_kept():
    # seed_from_release defaults to False (manifest key absent) - the proven
    # location-seeded path must be byte-equivalent with no release keys.
    assert resolve_centerline_seed(
        *LONGVIEW_CITY, *COLUMBIA_RELEASE
    ) == (*LONGVIEW_CITY, "position")


def test_numeric_strings_are_plausible():
    # Manifest JSON roundtrips may stringify - float() coercion is accepted.
    assert resolve_centerline_seed(
        *LONGVIEW_CITY, "-122.9345", "46.1060", seed_from_release=True
    ) == (-122.9345, 46.1060, "release-position")


@pytest.mark.parametrize("rel", [
    (None, 46.106),             # partial: lat only
    (-122.9345, None),          # partial: lon only
    ("Columbia River", 46.1),   # non-numeric
    (200.0, 46.1),              # lon out of range
    (-122.9345, 95.0),          # lat out of range
    (float("nan"), 46.1),       # NaN fails the range gate
    (-122.9345, float("inf")),  # inf fails the range gate
])
def test_implausible_release_keeps_seed(rel):
    assert resolve_centerline_seed(
        *LONGVIEW_CITY, *rel, seed_from_release=True
    ) == (*LONGVIEW_CITY, "position")


# A gate-picked click that overwrote release_lon/release_lat downstream of an
# approved preview (the BK-3b decouple: the ORIGINAL call coords ride
# seed_release_lon/seed_release_lat so the reach still seeds from them).
GATE_CLICK = (-122.9000, 46.1200)


def test_seed_release_wins_over_click_release():
    # THE blocker pin: with a click in release_lon/release_lat AND the
    # original call coords in seed_release_*, the reach seeds from the
    # ORIGINALS (the pair the approved preview meshed from), never the click.
    assert resolve_centerline_seed(
        *LONGVIEW_CITY, *GATE_CLICK, seed_from_release=True,
        seed_release_lon=COLUMBIA_RELEASE[0],
        seed_release_lat=COLUMBIA_RELEASE[1],
    ) == (*COLUMBIA_RELEASE, "release-position")


def test_implausible_seed_release_degrades_to_release():
    # An implausible seed_release pair falls back to the release coords
    # (honest degrade, matching the pre-existing armed behavior).
    assert resolve_centerline_seed(
        *LONGVIEW_CITY, *COLUMBIA_RELEASE, seed_from_release=True,
        seed_release_lon=200.0, seed_release_lat=46.1,
    ) == (*COLUMBIA_RELEASE, "release-position")


def test_seed_release_ignored_when_not_armed():
    # seed_from_release=False keeps the geocode seed byte-for-byte even when
    # seed_release_* are present - the unarmed path is unchanged.
    assert resolve_centerline_seed(
        *LONGVIEW_CITY, *GATE_CLICK, seed_from_release=False,
        seed_release_lon=COLUMBIA_RELEASE[0],
        seed_release_lat=COLUMBIA_RELEASE[1],
    ) == (*LONGVIEW_CITY, "position")


def test_seed_release_alone_seeds_reach():
    # Release coords absent (e.g. an implausible click was dropped) but the
    # armed originals present - the reach still seeds from the originals so
    # the approved solve reproduces the previewed mesh.
    assert resolve_centerline_seed(
        *LONGVIEW_CITY, None, None, seed_from_release=True,
        seed_release_lon=COLUMBIA_RELEASE[0],
        seed_release_lat=COLUMBIA_RELEASE[1],
    ) == (*COLUMBIA_RELEASE, "release-position")
