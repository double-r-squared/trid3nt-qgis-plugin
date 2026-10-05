"""The run readers, on the artifacts a solved run uploads. Offline, no solve.

These are the derivations the worker used to perform in-container: the sediment
closure out of the solver listing, the injected mass and deposit fraction off the
sheet's own pulse, and the floating slick out of the raw drogues track. What they
pin is the FORMAT the engine writes, which a guess would read silently wrong."""

from __future__ import annotations

import math

import pytest

from trid3nt_server.workflows.telemac.modules import listing as R
from trid3nt_server.workflows.telemac.modules.outputs import wetted_fraction

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

#: The sheet's own pulse: 8 m3/s x 100 mg/L x 300 s = 240 kg injected.
#: What the run PUT IN: 8 m3/s of 100 mg/L over a 300 s window, in kilograms.
#: The deposit fraction is measured against this rather than an assumed load.
_INJECTED = 240.0


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


def _balance(t_s: float, *fluxes: float, error: str = "0.1E-14") -> str:
    """One TELEMAC-2D water-volume balance block, in the engine's own spelling."""
    lines = ["                       BALANCE OF WATER VOLUME",
             "     VOLUME IN THE DOMAIN :    1472.903     M3"]
    lines += [f"     FLUX BOUNDARY   {i}: {q:16.7E} M3/S"
              "  ( >0 : ENTERING  <0 : EXITING )"
              for i, q in enumerate(fluxes, start=1)]
    lines.append(f"     RELATIVE ERROR IN VOLUME AT T = {t_s:12.1f}     S :   {error}")
    return "\n".join(lines) + "\n"


def test_the_boundary_flux_is_the_listings_own_series():
    """The engine integrates the boundary flux itself and prints it; a server-side
    re-derivation from the depth and velocity fields is a second computation of
    the same quantity, and it read 0.0 while the solver reported tens of m3/s."""
    listing = _balance(900.0, -20.25) + _balance(1800.0, -8.5)
    times, flows = R.boundary_flux(listing, boundary=1)
    assert times == [900.0, 1800.0]
    # ONE convention: outflow positive, so the listing's own sign is negated once.
    assert flows == [20.25, 8.5]


def test_the_declared_boundary_is_the_one_that_is_read():
    """A reach prints an inflow and an outflow; reading the wrong number would
    report the carrier discharge as the basin's runoff."""
    listing = _balance(900.0, 250.0, -18.0)
    assert R.boundary_flux(listing, boundary=2)[1] == [18.0]
    assert R.boundary_flux(listing, boundary=1)[1] == [-250.0]


def test_water_running_back_in_reads_negative_and_a_printed_zero_is_not_minus_zero():
    listing = _balance(0.0, 0.0) + _balance(60.0, 20.0)
    times, flows = R.boundary_flux(listing, boundary=1)
    assert flows == [0.0, -20.0]
    assert math.copysign(1.0, flows[0]) == 1.0


def test_a_TRACER_balances_flux_lines_never_leak_into_the_water_flux():
    """A coupled run prints a second balance under its own heading; reading it as
    discharge would report kilograms per second as cubic metres per second."""
    listing = (_balance(900.0, -20.25)
               + "                       BALANCE OF TRACER  1\n"
                 "     FLUX BOUNDARY    1:   -0.5000000E+03 KG/S\n"
                 "     RELATIVE ERROR IN QUANTITY OF TRACER  1 : 0.1E-13\n"
               + _balance(1800.0, -8.5))
    assert R.boundary_flux(listing, boundary=1)[1] == [20.25, 8.5]


def test_a_listing_that_printed_no_balance_measures_nothing():
    assert R.boundary_flux("ITERATION 1\n", boundary=1) == ([], [])
    assert R.boundary_flux(_balance(900.0, -1.0), boundary=3) == ([], [])


#: The block the engine prints once, as its run closes, plus the runoff
#: routine's own accumulated rainfall lines.
_FINAL = """
      RUNOFF_SCS_CN : ACCUMULATED RAINFALL :    0.3265000E-02 M
      RUNOFF_SCS_CN : ACCUMULATED RAINFALL :    0.1567200     M
                   FINAL BALANCE OF WATER VOLUME

     RELATIVE ERROR CUMULATED ON VOLUME:   -0.1086687E-13

     INITIAL VOLUME              :     0.000000     M3
     FINAL VOLUME                :     1226447.     M3
     VOLUME THAT ENTERED THE DOMAIN:    -1564079.     M3  ( IF <0 EXIT )
     VOLUME ADDED BY SOURCE TERM   :     2790526.     M3
     TOTAL VOLUME LOST             :   -0.1699664E-07 M3
"""


def test_the_final_balance_is_the_engines_own_closure_of_the_whole_run():
    out = R.final_balance(_FINAL)
    assert out == {"initial_volume_m3": 0.0, "final_volume_m3": 1226447.0,
                   "boundary_volume_m3": -1564079.0, "source_volume_m3": 2790526.0,
                   "lost_volume_m3": pytest.approx(-1.699664e-08),
                   "rain_depth_m": pytest.approx(0.15672)}


def test_a_listing_without_the_final_block_states_no_volume():
    assert R.final_balance(_balance(900.0, -1.0)) == {}
    # the rain depth rides on its own: a run cut short still accumulated it
    assert R.final_balance("  RUNOFF_SCS_CN : ACCUMULATED RAINFALL :    0.25 M\n") == {
        "rain_depth_m": 0.25}


def test_the_engines_own_volume_closure_is_the_last_one_it_printed():
    listing = _balance(900.0, -1.0, error="0.4E-03") + \
        _balance(1800.0, -1.0, error="0.9E-03")
    assert R.continuity_rel_error(listing) == 0.9e-3
    assert R.continuity_rel_error("no closure here") is None


#: The depth variable name a solved TELEMAC-2D result actually carries: SELAFIN
#: pads the name to 32 chars and trails the unit. Read off a real r2d_river.slf.
_DEPTH_VAR = "WATER DEPTH"


def _mesh(depths):
    """Two disjoint triangles - a 50 m2 channel and a 150 m2 bar beside it.

    The areas differ so the measurement can be told from a node count, and the depth
    key carries the SELAFIN name padding with its unit trailing, as a real result does."""
    import numpy as np

    return {"x": np.array([0.0, 10.0, 0.0, 20.0, 50.0, 20.0]),
            "y": np.array([0.0, 0.0, 10.0, 0.0, 0.0, 10.0]),
            "ikle": np.array([[0, 1, 2], [3, 4, 5]]),
            "varnames": [_DEPTH_VAR],
            "data": {_DEPTH_VAR: np.array([[0.0] * 6, list(depths)])}}


def test_the_wetted_fraction_is_area_weighted_off_the_final_frame():
    """The small wet triangle is a quarter of the domain, not half of it.

    And the LAST frame decides it: frame zero is bone dry above, so a reader
    taking the first record would call this run empty.
    """
    got = wetted_fraction(_mesh([1.0, 1.0, 1.0, 0.0, 0.0, 0.0]))
    assert got["mesh_area_m2"] == pytest.approx(200.0)
    assert got["wet_area_m2"] == pytest.approx(50.0)
    assert got["wetted_fraction"] == pytest.approx(0.25)


def test_a_film_thinner_than_the_tolerance_is_not_conveyance():
    """A drying bar keeps a film; counting it wet makes the number say nothing."""
    assert wetted_fraction(_mesh([0.001] * 6))["wetted_fraction"] == 0.0
    assert wetted_fraction(_mesh([1.0] * 6))["wetted_fraction"] == 1.0


def test_a_result_with_no_depth_measures_nothing():
    import numpy as np

    assert wetted_fraction({
        "x": np.zeros(3), "y": np.zeros(3), "ikle": np.array([[0, 1, 2]]),
        "varnames": ["DYE"],
        "data": {"DYE": np.zeros((1, 3))}}) == {}


#: How LECDON asks, verbatim from the image's own lecdon_telemac3d.F: the phrase
#: opens the block and the keyword names stand under it.
_DEMAND = """
 THE LAW OF BOTTOM FRICTION  5 IS ASKED
 GIVE THE CORRESPONDING FRICTION COEFFICIENT

 PLANTE: PROGRAM STOPPED AFTER AN ERROR
"""


def test_the_engine_s_own_demand_is_read_by_name_out_of_the_listing():
    assert R.engine_demand(_DEMAND) == "GIVE THE CORRESPONDING FRICTION COEFFICIENT"
    assert R.engine_demand(
        " THE FOLLOWING KEYWORD IS MANDATORY:\n"
        " BOUNDARY CONDITIONS FILE (FICHIER DES CONDITIONS AUX LIMITES)\n"
    ) == ("THE FOLLOWING KEYWORD IS MANDATORY:; "
          "BOUNDARY CONDITIONS FILE (FICHIER DES CONDITIONS AUX LIMITES)")


def test_a_listing_that_demanded_nothing_reads_as_nothing():
    assert R.engine_demand(" MURD3D: ITERATION NO. REACHED 100 , STOP.") is None
    assert R.engine_demand("") is None
