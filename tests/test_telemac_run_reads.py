"""The run readers, on the artifacts a solved run uploads (offline; no solve).

These are the derivations the worker used to perform in-container and no longer
does: GAIA's closure out of the solver listing, the injected mass and deposit
fraction off the deck's own pulse, and the floating slick out of the raw drogues
track. What they pin is the FORMAT the engine writes, which a guess would read
silently wrong.
"""

from __future__ import annotations

import math

import pytest

from trid3nt_server.workflows.telemac.steps import run_reads as R

#: The in-image listing shape - the block GAIA prints once its run closes.
_LISTING = """
 GAIA MASS-BALANCE OF SEDIMENTS PER CLASS:
 CUMULATED DEPOSITION           =     111.0000  ( KG )
FINAL MASS-BALANCE OF SEDIMENTS:
GAIA MASS-BALANCE OF SEDIMENTS PER CLASS:
 SEDIMENT CLASS NUMBER          =        1
 CUMULATED BED EVOLUTIONS       =     60.00000  ( KG )
 CUMULATED EROSION              =     334.4448  ( KG )
 CUMULATED DEPOSITION           =     394.4448  ( KG )
 CUMULATED LOST MASS            =    0.511E-12  ( KG )
CORRECT END OF RUN
 CUMULATED DEPOSITION           =     999.0000  ( KG )
"""

#: The deck's own pulse: 8 m3/s x 100 mg/L x 300 s = 240 kg injected.
_DECK = {"source_q_m3s": 8.0, "dye_conc_mgl": 100.0, "pulse_window_s": 300.0}


def test_the_closure_is_read_from_the_final_block_only():
    """An intermediate balance before it and a stray line after it are not it."""
    balance = R.gaia_mass_balance(_LISTING)
    assert balance["sediment_deposited_mass_kg"] == 394.4448
    assert balance["sediment_eroded_mass_kg"] == 334.4448
    assert balance["sediment_net_bed_mass_kg"] == 60.0
    # a loss below a microgram rounds to zero kg, which is what closure means
    assert balance["sediment_mass_lost_kg"] == 0.0


def test_a_listing_with_no_closure_reports_nothing():
    assert R.gaia_mass_balance("CORRECT END OF RUN") == {}


def test_a_residual_that_rounds_to_negative_zero_reads_as_zero():
    """``max(-0.0, 0.0)`` is ``-0.0``, so a signed zero reaching a consumer is a
    negative deposited mass narrated beside a map showing deposition."""
    listing = _LISTING.replace("60.00000", "-0.1E-12")
    net = R.gaia_mass_balance(listing)["sediment_net_bed_mass_kg"]
    assert net == 0.0 and math.copysign(1.0, net) == 1.0
    stats = R.sediment_scalars(listing_text=listing, deck=_DECK)
    fraction = stats["sediment_deposit_fraction"]
    assert fraction == 0.0 and math.copysign(1.0, fraction) == 1.0


def test_the_deposit_fraction_compares_the_net_bed_against_the_deck_pulse():
    stats = R.sediment_scalars(listing_text=_LISTING, deck=_DECK)
    assert stats["sediment_injected_kg"] == 240.0
    assert stats["sediment_deposit_fraction"] == 0.25


def test_a_net_gain_past_the_injection_clamps_rather_than_reading_over_one():
    listing = _LISTING.replace("60.00000", "600.0000")
    assert R.sediment_scalars(listing_text=listing,
                              deck=_DECK)["sediment_deposit_fraction"] == 1.0


def test_a_single_class_bed_reports_no_sorting_at_all():
    """Sorting is structurally impossible on one class, so no spread is claimed."""
    stats = R.sediment_scalars(listing_text=_LISTING, deck=_DECK)
    assert "sediment_n_classes" not in stats
    graded = R.sediment_scalars(
        listing_text=_LISTING,
        deck={**_DECK, "sediment_gradation": [[50.0, 0.4], [400.0, 0.6]]})
    assert graded["sediment_n_classes"] == 2


_DROGUES = """TITLE = "drogues"
VARIABLES = "id","X","Y"
ZONE T="floats" SOLUTIONTIME= 0.0
1, 500.0, 0.0
2, 501.0, 1.0
3, 502.0, 2.0
ZONE T="floats" SOLUTIONTIME= 300.0
1, 700.0, 0.0
2, 701.0, 1.0
ZONE T="floats" SOLUTIONTIME= 600.0
1, 900.0, 0.0
"""


def test_the_track_is_one_zone_per_written_instant(tmp_path):
    path = tmp_path / "drogues.txt"
    path.write_text(_DROGUES)
    zones = R.parse_drogues(path)
    assert [t for t, _pts in zones] == [0.0, 300.0, 600.0]
    assert [len(pts) for _t, pts in zones] == [3, 2, 1]


def test_a_lost_float_reads_as_having_left_through_the_outlet(tmp_path):
    """TELEMAC deletes a float that crosses a liquid boundary; that is an exit."""
    path = tmp_path / "drogues.txt"
    path.write_text(_DROGUES)
    particles, slick, stats = R.oil_slick_features(path, utm_epsg=32610)
    assert stats["oil_particles_released"] == 3
    assert stats["oil_particles"] == 1
    assert stats["oil_particles_exited_domain"] == 2
    assert stats["oil_drift_m"] > 300.0
    assert len(particles["snapshots"]) == 3
    # the release, the middle and the end - never one layer per written frame
    assert len(slick["features"]) == 3
    lon, lat = slick["features"][0]["geometry"]["coordinates"][0]
    assert -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0


def test_a_track_with_no_floats_at_any_instant_draws_no_slick(tmp_path):
    path = tmp_path / "drogues.txt"
    path.write_text('ZONE T="floats" SOLUTIONTIME= 0.0\n')
    _particles, slick, stats = R.oil_slick_features(path, utm_epsg=32610)
    assert slick["features"] == [] and stats == {}


# --------------------------------------------------------------------------- #
# The outlet hydrograph: the flux the ENGINE printed across the declared boundary.
# --------------------------------------------------------------------------- #
def _balance(t_s: float, *fluxes: float, error: str = "0.1E-14") -> str:
    """One TELEMAC-2D water-volume balance block, in the engine's own spelling."""
    lines = ["                       BALANCE OF WATER VOLUME",
             "     VOLUME IN THE DOMAIN :    1472.903     M3"]
    lines += [f"     FLUX BOUNDARY   {i}: {q:16.7E} M3/S"
              "  ( >0 : ENTERING  <0 : EXITING )"
              for i, q in enumerate(fluxes, start=1)]
    lines.append(f"     RELATIVE ERROR IN VOLUME AT T = {t_s:12.1f}     S :   {error}")
    return "\n".join(lines) + "\n"


def test_the_hydrograph_is_the_listings_own_flux_series():
    """The engine integrates the boundary flux itself and prints it; a server-side
    re-derivation from the depth and velocity fields is a second computation of
    the same quantity, and it read 0.0 while the solver reported tens of m3/s."""
    listing = _balance(900.0, -20.25) + _balance(1800.0, -8.5)
    out = R.outlet_hydrograph(listing, boundary=1)
    assert out["t_s"] == [900.0, 1800.0]
    # ONE convention: outflow positive, so the listing's own sign is negated once.
    assert out["q_m3s"] == [20.25, 8.5]
    assert out["peak_discharge_m3s"] == 20.25
    assert out["peak_discharge_time_s"] == 900.0
    assert out["outlet_boundary"] == 1
    # The limb fell after its crest, so the window closed on a measured peak.
    assert out["peak_is_window_truncated"] is False


def test_a_crest_on_the_last_sample_is_labelled_the_window_closing():
    """A hydrograph still rising when the run ends has no peak inside it: the
    reported peak, volume and coefficient are floors, and the read says so."""
    listing = _balance(900.0, -8.5) + _balance(1800.0, -20.25)
    assert R.outlet_hydrograph(listing, boundary=1)["peak_is_window_truncated"] is True


def test_a_single_sample_is_not_a_truncation_claim():
    """One balance block is a cadence fact, not evidence about a rising limb."""
    listing = _balance(900.0, -8.5)
    assert R.outlet_hydrograph(listing, boundary=1)["peak_is_window_truncated"] is False


def test_the_reported_volume_is_the_integral_of_that_same_series():
    listing = _balance(0.0, -0.0) + _balance(3600.0, -10.0)
    out = R.outlet_hydrograph(listing, boundary=1)
    # trapezoid over one hour of a 0 -> 10 m3/s limb.
    assert out["runoff_volume_m3"] == pytest.approx(18000.0)


def test_the_declared_boundary_is_the_one_that_is_read():
    """A reach prints an inflow and an outflow; reading the wrong number would
    report the carrier discharge as the basin's runoff."""
    listing = _balance(900.0, 250.0, -18.0)
    assert R.outlet_hydrograph(listing, boundary=2)["q_m3s"] == [18.0]
    assert R.outlet_hydrograph(listing, boundary=1)["q_m3s"] == [-250.0]


def test_water_running_back_in_reads_negative_and_yields_no_runoff():
    listing = _balance(0.0, 0.0) + _balance(60.0, 20.0)
    out = R.outlet_hydrograph(listing, boundary=1)
    assert out["q_m3s"] == [0.0, -20.0]
    # A basin that only took water in produced no runoff volume to report.
    assert out["runoff_volume_m3"] == 0.0


def test_a_TRACER_balances_flux_lines_never_leak_into_the_water_hydrograph():
    """A coupled run prints a second balance under its own heading; reading it as
    discharge would report kilograms per second as cubic metres per second."""
    listing = (_balance(900.0, -20.25)
               + "                       BALANCE OF TRACER  1\n"
                 "     FLUX BOUNDARY    1:   -0.5000000E+03 KG/S\n"
                 "     RELATIVE ERROR IN QUANTITY OF TRACER  1 : 0.1E-13\n"
               + _balance(1800.0, -8.5))
    assert R.outlet_hydrograph(listing, boundary=1)["q_m3s"] == [20.25, 8.5]


def test_a_listing_that_printed_no_balance_measures_nothing():
    assert R.outlet_hydrograph("ITERATION 1\n", boundary=1) == {}
    assert R.outlet_hydrograph(_balance(900.0, -1.0), boundary=3) == {}


def test_the_engines_own_volume_closure_is_the_last_one_it_printed():
    listing = _balance(900.0, -1.0, error="0.4E-03") + \
        _balance(1800.0, -1.0, error="0.9E-03")
    assert R.continuity_rel_error(listing) == 0.9e-3
    assert R.continuity_rel_error("no closure here") is None


# --------------------------------------------------------------------------- #
# The wetted fraction: what the run says about the domain it did NOT wet.
# --------------------------------------------------------------------------- #
#: The depth variable name a solved TELEMAC-2D result actually carries: SELAFIN
#: pads the name to 32 chars and trails the unit. Read off a real r2d_river.slf.
_DEPTH_VAR = "WATER DEPTH     M"


def _selafin(monkeypatch, depths):
    """Two disjoint triangles - a 50 m2 channel and a 150 m2 bar beside it.

    The areas differ so the measurement can be told apart from a node count: half
    the nodes wet is a QUARTER of the domain wet, and only one of those two
    numbers is the conveyance a reader is looking for.

    The depth key carries the SELAFIN 32-char name padding with its unit trailing,
    exactly as a solved result does - a bare 'WATER DEPTH' key would let a lookup
    that no real run can satisfy pass here.
    """
    import numpy as np

    from trid3nt_server.workflows.telemac import postprocess_telemac as P

    mesh = {"x": np.array([0.0, 10.0, 0.0, 20.0, 50.0, 20.0]),
            "y": np.array([0.0, 0.0, 10.0, 0.0, 0.0, 10.0]),
            "ikle": np.array([[0, 1, 2], [3, 4, 5]]),
            "varnames": [_DEPTH_VAR],
            "data": {_DEPTH_VAR: np.array([[0.0] * 6, list(depths)])}}
    monkeypatch.setattr(P, "read_selafin", lambda _path: mesh)


def test_the_wetted_fraction_is_area_weighted_off_the_final_frame(monkeypatch):
    """The small wet triangle is a quarter of the domain, not half of it.

    And the LAST frame decides it: frame zero is bone dry above, so a reader
    taking the first record would call this run empty.
    """
    _selafin(monkeypatch, [1.0, 1.0, 1.0, 0.0, 0.0, 0.0])
    got = R.wetted_fraction("ignored.slf")
    assert got["mesh_area_m2"] == pytest.approx(200.0)
    assert got["wet_area_m2"] == pytest.approx(50.0)
    assert got["wetted_fraction"] == pytest.approx(0.25)


def test_a_film_thinner_than_the_tolerance_is_not_conveyance(monkeypatch):
    """A drying bar keeps a film; counting it wet makes the number say nothing."""
    _selafin(monkeypatch, [0.001] * 6)
    assert R.wetted_fraction("ignored.slf")["wetted_fraction"] == 0.0
    _selafin(monkeypatch, [1.0] * 6)
    assert R.wetted_fraction("ignored.slf")["wetted_fraction"] == 1.0


def test_a_result_with_no_depth_measures_nothing(monkeypatch):
    import numpy as np

    from trid3nt_server.workflows.telemac import postprocess_telemac as P

    monkeypatch.setattr(P, "read_selafin", lambda _path: {
        "x": np.zeros(3), "y": np.zeros(3), "ikle": np.array([[0, 1, 2]]),
        "varnames": ["DYE             MG/L"],
        "data": {"DYE             MG/L": np.zeros((1, 3))}})
    assert R.wetted_fraction("ignored.slf") == {}


def test_the_reach_run_says_out_loud_what_it_did_not_wet(monkeypatch):
    """The heuristic lands on the run journal, and it GATES nothing.

    A run that wet a quarter of its bankfull domain is a correct low-flow run
    with an overstated conveyance width, and the only thing wrong with it is a
    reader who cannot tell. So the number is said; nothing is refused over it.
    """
    import asyncio

    from trid3nt_server.workflows.lib.journal import bind_notes, drain_notes
    from trid3nt_server.workflows.telemac.steps import products as PR

    _selafin(monkeypatch, [1.0, 1.0, 1.0, 0.0, 0.0, 0.0])
    token = bind_notes()
    try:
        asyncio.run(PR._journal_wetted_fraction("ignored.slf"))
    finally:
        notes = drain_notes(token)
    assert len(notes) == 1
    assert "wetted fraction: 25%" in notes[0]
    assert "0.02 m" in notes[0] and "active channel" in notes[0]


def test_an_unmeasurable_result_costs_the_run_nothing(monkeypatch):
    import asyncio

    from trid3nt_server.workflows.lib.journal import bind_notes, drain_notes
    from trid3nt_server.workflows.telemac import postprocess_telemac as P
    from trid3nt_server.workflows.telemac.steps import products as PR

    def _boom(_path):
        raise RuntimeError("not a SELAFIN")

    monkeypatch.setattr(P, "read_selafin", _boom)
    token = bind_notes()
    try:
        asyncio.run(PR._journal_wetted_fraction("ignored.slf"))
    finally:
        assert drain_notes(token) == []
