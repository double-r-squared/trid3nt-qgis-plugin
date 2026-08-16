"""Offline tests for the published-deck runner core (ADR 0128).

Exercises the shared machinery - inline extraction (incl. prose trimming + a
two-deck page's ``select_index``), the deterministic rain-scale override, the
headless solve + continuity honesty gate, and the cited-deck registry - WITHOUT
network. The deck used here is a TINY deck AUTHORED FOR THIS TEST (not a
redistributed published deck): the runner fetches the real author-posted decks at
runtime from their pinned public URLs, so the offline suite must not depend on
one being reachable.
"""

from __future__ import annotations

import re

import pytest

from trid3nt_server.mesh.swmm_deck_runner import (
    PUBLISHED_DECKS,
    SWMMDeckError,
    apply_rain_scale,
    extract_inline_deck,
    list_object_names,
    solve_deck_text,
)

# A minimal, self-authored deck that solves headless (rainfall -> junction ->
# storage pond -> outfall). Carries TIMESERIES (rain-scale), STORAGE + CONTROLS
# so the parsers are exercised. NOT a redistributed published deck.
_SYNTH_DECK = """[TITLE]
Synthetic deck-runner test deck

[OPTIONS]
FLOW_UNITS           CFS
INFILTRATION         HORTON
FLOW_ROUTING         DYNWAVE
START_DATE           01/01/2024
START_TIME           00:00:00
END_DATE             01/01/2024
END_TIME             02:00:00
REPORT_STEP          00:05:00
WET_STEP             00:01:00
DRY_STEP             00:05:00
ROUTING_STEP         5
ALLOW_PONDING        YES

[RAINGAGES]
RG INTENSITY 0:05 1.0 TIMESERIES HYET

[SUBCATCHMENTS]
S1 RG J1 2.0 50 100 0.5 0

[SUBAREAS]
S1 0.01 0.1 0.05 0.05 25 OUTLET

[INFILTRATION]
S1 3.0 0.5 4.0 7 0

[JUNCTIONS]
J1 10 4 0 0 0

[STORAGE]
POND 8 6 0 FUNCTIONAL 1000 0 0 0 0

[OUTFALLS]
OUT 6 FREE NO

[CONDUITS]
C1 J1 POND 100 0.02 0 0 0
C2 POND OUT 100 0.02 0 0 0

[XSECTIONS]
C1 CIRCULAR 2 0 0 0 1
C2 CIRCULAR 2 0 0 0 1

[TIMESERIES]
HYET 0:00 0.5
HYET 0:15 2.0
HYET 0:30 1.0
HYET 0:45 0.2

[REPORT]
NODES ALL
LINKS ALL
"""


def _wrap_html(deck_text: str, footer: str = "Posted by an author. Reply Quote.") -> str:
    return f"<html><body><pre>{deck_text}</pre><p>{footer}</p></body></html>"


# --------------------------------------------------------------------------- #
# Cited-deck registry integrity.
# --------------------------------------------------------------------------- #
def test_published_decks_registry_integrity():
    assert set(PUBLISHED_DECKS) == {
        "lid_raingarden_wq", "wwtp_detention_ponds", "pump_pid_rtc"
    }
    for deck_id, deck in PUBLISHED_DECKS.items():
        assert deck.deck_id == deck_id
        assert deck.title and deck.author
        assert deck.source_url.startswith("https://")  # pinned public source
        assert deck.forcing in {"rainfall", "initial_storage", "dry_weather_flow"}
        assert deck.mass_balance_tol_pct > 0
        assert deck.capabilities  # every cited deck declares its capability
    # only the rainfall-forced deck is rain-scalable.
    assert PUBLISHED_DECKS["lid_raingarden_wq"].rain_scalable is True
    assert PUBLISHED_DECKS["wwtp_detention_ponds"].rain_scalable is False
    assert PUBLISHED_DECKS["pump_pid_rtc"].rain_scalable is False


# --------------------------------------------------------------------------- #
# Inline extraction.
# --------------------------------------------------------------------------- #
def test_extract_inline_deck_trims_trailing_prose():
    deck = extract_inline_deck(_wrap_html(_SYNTH_DECK))
    assert "Posted by" not in deck  # forum footer trimmed
    assert deck.strip().splitlines()[-1] == "LINKS ALL"
    secs = set(re.findall(r"\[([A-Z_]+)\]", deck))
    assert {"OPTIONS", "STORAGE", "TIMESERIES", "CONDUITS"} <= secs


def test_extract_two_deck_page_select_index():
    # two synthetic decks concatenated on one page (the openswmm two-file case).
    two = _SYNTH_DECK + "\n\n" + _SYNTH_DECK
    d0 = extract_inline_deck(_wrap_html(two), select_index=0)
    d1 = extract_inline_deck(_wrap_html(two), select_index=1)
    assert "[OPTIONS]" in d0 and "[OPTIONS]" in d1
    # deck0 must NOT bleed into deck1 (no duplicate OPTIONS in a single block).
    assert d0.count("[OPTIONS]") == 1
    assert d1.count("[OPTIONS]") == 1
    with pytest.raises(SWMMDeckError) as ei:
        extract_inline_deck(_wrap_html(two), select_index=5)
    assert ei.value.error_code == "SWMM_DECK_PARSE_FAILED"


def test_extract_no_options_raises():
    with pytest.raises(SWMMDeckError) as ei:
        extract_inline_deck("<html><body>no deck here at all</body></html>")
    assert ei.value.error_code == "SWMM_DECK_PARSE_FAILED"


# --------------------------------------------------------------------------- #
# Deterministic rain-scale override.
# --------------------------------------------------------------------------- #
def test_apply_rain_scale_multiplies_only_timeseries():
    scaled, label = apply_rain_scale(_SYNTH_DECK, 2.0)
    assert "x4 hyetograph ordinates" in label  # 4 hyetograph values scaled
    # the 4 hyetograph ordinates are doubled: 0.5->1, 2.0->4, 1.0->2, 0.2->0.4
    assert "HYET 0:15 4" in scaled
    assert "HYET 0:00 1" in scaled
    # a non-TIMESERIES numeric line (a conduit length) is untouched.
    assert "C1 J1 POND 100 0.02 0 0 0" in scaled
    # factor 1.0 is a no-op.
    same, lbl1 = apply_rain_scale(_SYNTH_DECK, 1.0)
    assert same == _SYNTH_DECK
    assert "unchanged" in lbl1


# --------------------------------------------------------------------------- #
# Headless solve + continuity honesty gate.
# --------------------------------------------------------------------------- #
def test_solve_deck_text_success_and_scalars():
    res = solve_deck_text(_SYNTH_DECK, stem="synth_ok")
    assert abs(res.continuity_error_pct) < 5.0
    assert res.n_nodes == 3  # J1 + POND + OUT
    assert res.n_links == 2  # C1 + C2
    assert res.n_subcatchments == 1
    assert list_object_names(_SYNTH_DECK, "STORAGE") == ["POND"]


def test_solve_mass_balance_gate_raises():
    # A negative tolerance forces the gate to trip even on a clean solve, proving
    # the honesty gate is wired (abs(continuity) > tol -> raise, never publish).
    with pytest.raises(SWMMDeckError) as ei:
        solve_deck_text(_SYNTH_DECK, stem="synth_gate", mass_balance_tolerance_pct=-1.0)
    assert ei.value.error_code == "SWMM_MASS_BALANCE_EXCEEDED"


def test_solve_broken_deck_raises_run_failed():
    broken = "[OPTIONS]\nFLOW_UNITS CFS\n[JUNCTIONS]\nJ1 not a number here\n"
    with pytest.raises(SWMMDeckError) as ei:
        solve_deck_text(broken, stem="synth_broken")
    assert ei.value.error_code in {"SWMM_DECK_RUN_FAILED", "SWMM_DECK_CONTINUITY_UNREADABLE"}
