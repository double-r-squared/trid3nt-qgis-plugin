"""The run readers, on the artifacts a solved run uploads (offline; no solve).

These are the derivations the worker used to perform in-container and no longer
does: GAIA's closure out of the solver listing, the injected mass and deposit
fraction off the deck's own pulse, and the floating slick out of the raw drogues
track. What they pin is the FORMAT the engine writes, which a guess would read
silently wrong.
"""

from __future__ import annotations

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
