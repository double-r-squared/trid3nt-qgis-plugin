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


# --------------------------------------------------------------------------- #
# The outlet hydrograph: the flux through the nodes the outlet role landed on.
# --------------------------------------------------------------------------- #
def _one_strip_selafin(path, *, u_east):
    """A 2x1 strip whose EAST edge is the outlet, at one metre depth.

    Nodes 0,2 are the west cap and 1,3 the east one; the east edge is 10 m long,
    so a 1 m depth moving east at ``u_east`` leaves at 10 * u_east m3/s.
    """
    import numpy as np

    from tests.test_postprocess_telemac import _write_synthetic_selafin

    varnames = ["VELOCITY U      M/S", "VELOCITY V      M/S", "WATER DEPTH     M"]
    x = [0.0, 20.0, 0.0, 20.0]
    y = [0.0, 0.0, 10.0, 10.0]
    ikle = [[1, 2, 3], [2, 4, 3]]
    times = [0.0, 60.0]
    data = {
        "VELOCITY U      M/S": [np.full(4, 0.0), np.full(4, float(u_east))],
        "VELOCITY V      M/S": [np.zeros(4), np.zeros(4)],
        "WATER DEPTH     M": [np.ones(4), np.ones(4)],
    }
    _write_synthetic_selafin(path, varnames, x, y, ikle, times, data)
    return path


def test_water_leaving_through_the_outlet_reads_positive(tmp_path):
    slf = _one_strip_selafin(tmp_path / "r2d_rog.slf", u_east=2.0)
    out = R.outlet_hydrograph(slf, outlet_nodes=[1, 3])
    assert out["outlet_segments"] == 1
    # 10 m of edge x 1 m depth x 2 m/s.
    assert out["q_m3s"] == [0.0, 20.0]
    assert out["peak_discharge_m3s"] == 20.0
    assert out["peak_discharge_time_s"] == 60.0
    assert out["runoff_volume_m3"] == 600.0


def test_water_running_back_into_the_basin_reads_negative(tmp_path):
    slf = _one_strip_selafin(tmp_path / "r2d_rog.slf", u_east=-2.0)
    out = R.outlet_hydrograph(slf, outlet_nodes=[1, 3])
    assert out["q_m3s"] == [0.0, -20.0]
    # A basin that only took water in produced no runoff volume to report.
    assert out["runoff_volume_m3"] == 0.0


def test_an_interior_edge_is_never_an_outlet_segment(tmp_path):
    """Only element edges no second element shares are the mesh's own rim, so a
    role that lands on a diagonal measures nothing."""
    slf = _one_strip_selafin(tmp_path / "r2d_rog.slf", u_east=2.0)
    assert R.outlet_hydrograph(slf, outlet_nodes=[1, 2]) == {}


def test_the_engines_own_volume_closure_is_the_last_one_it_printed():
    listing = ("RELATIVE ERROR IN VOLUME  : 0.4E-03\n"
               "RELATIVE ERROR IN VOLUME  : 0.9E-03\n")
    assert R.continuity_rel_error(listing) == 0.9e-3
    assert R.continuity_rel_error("no closure here") is None
