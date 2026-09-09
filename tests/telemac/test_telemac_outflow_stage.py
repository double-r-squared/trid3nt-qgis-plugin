"""The reach's outflow stage: a normal depth over the measured channel.

The downstream cap of a reach used to be held at the outflow bed plus a declared
2 m. That number was not a property of the reach - it was the same on a mountain
creek and a coastal plain river - and it was the one level the run's whole water
surface is anchored to.

What replaces it is a COMPUTATION over data the accepted mesh already carries:
the fall between the two role faces over the length the mesh was built on is the
friction slope, the outflow face's own transect is the channel, the run's own
roughness and its own prescribed discharge close the uniform-flow equation. No
gauge, no rating curve, no second datum. What cannot be measured refuses by name
rather than falling back to a level nobody derived.

Offline: arithmetic and text, no mesh build and no solve.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from trid3nt_server.workflows.telemac.authoring import assembler as D
from trid3nt_server.workflows.telemac.helpers import uniform_flow as U
from trid3nt_server.workflows.telemac.helpers.errors import TelemacDyeScenarioError

#: A trapezoid: 40 m bed at 97 m, banks rising 3 m over 10 m either side.
_SECTION = [[0.0, 100.0], [10.0, 97.0], [50.0, 97.0], [60.0, 100.0]]
_REACH = {"bed_top_m": 100.0, "bed_drop_m": 3.0, "reach_length_m": 1000.0,
          "outflow_section": _SECTION}


def _stage(**over):
    return U.normal_depth_stage(_REACH, **{"law": 3, "coefficient": 33.0,
                                           "discharge_q": 50.0, **over})


# --------------------------------------------------------------------------- #
# The measurement: what the accepted mesh says the reach is.
# --------------------------------------------------------------------------- #
def test_the_outflow_face_is_measured_as_a_transect_of_the_painted_bed():
    """A role is a run of the boundary walk, so the nodes ARE in section order.

    Offsets are the chord distances between them, which is what makes the face a
    cross-section rather than a scatter needing a re-ordering rule of its own.
    """
    xy = np.array([[0.0, 0.0], [0.0, 10.0], [0.0, 50.0], [0.0, 60.0]])
    bed = np.array([100.0, 97.0, 97.0, 100.0])
    measured = D._measured_reach(
        {"inflow": [0, 1, 2, 3], "outflow": [0, 1, 2, 3]}, xy, bed,
        [(0.0, 0.0), (1000.0, 0.0)])
    assert measured["outflow_section"] == _SECTION
    assert measured["reach_length_m"] == 1000.0


def test_the_reach_length_is_walked_along_the_line_the_mesh_was_built_over():
    """A sinuous reach is longer than the straight line between its caps, and the
    friction slope is the fall over the path the water takes."""
    xy = np.array([[0.0, 0.0], [0.0, 10.0]])
    bend = [(0.0, 0.0), (300.0, 400.0), (600.0, 0.0)]
    measured = D._measured_reach({"inflow": [0, 1], "outflow": [0, 1]},
                                 xy, np.array([97.0, 96.0]), bend)
    assert measured["reach_length_m"] == 1000.0


def test_a_node_the_bed_left_unpainted_drops_out_without_moving_the_others():
    """Closing the hole by shifting the survivors would narrow a channel nobody
    re-measured."""
    xy = np.array([[0.0, 0.0], [0.0, 10.0], [0.0, 50.0], [0.0, 60.0]])
    bed = np.array([100.0, 97.0, np.nan, 100.0])
    measured = D._measured_reach({"inflow": [0, 1, 3], "outflow": [0, 1, 2, 3]},
                                 xy, bed, [(0.0, 0.0), (1000.0, 0.0)])
    assert measured["outflow_section"] == [[0.0, 100.0], [10.0, 97.0],
                                           [60.0, 100.0]]


def test_an_outflow_face_with_no_section_left_refuses_by_name():
    xy = np.array([[0.0, 0.0], [0.0, 10.0]])
    with pytest.raises(TelemacDyeScenarioError) as exc:
        D._measured_reach({"inflow": [0, 1], "outflow": [0, 1]}, xy,
                          np.array([97.0, np.nan]), [(0.0, 0.0), (10.0, 0.0)])
    assert exc.value.error_code == "TELEMAC_MESH_SECTION_UNMEASURED"


# --------------------------------------------------------------------------- #
# The derivation: uniform flow over that channel.
# --------------------------------------------------------------------------- #
def test_the_stage_is_the_depth_at_which_the_section_conveys_the_discharge():
    """Manning's equation read back over the section the stage was solved on.

    The assertion is the physics, not the number: whatever depth came out, the
    channel at that depth must convey exactly the discharge the run prescribes
    upstream at the roughness the run writes.
    """
    derived = _stage()
    area, perimeter = U._wetted([tuple(p) for p in _SECTION], derived["stage_m"])
    conveyed = (derived["coefficient"] * area * (area / perimeter) ** (2.0 / 3.0)
                * math.sqrt(derived["slope"]))
    assert conveyed == pytest.approx(50.0, rel=1e-3)
    assert derived["depth_m"] == pytest.approx(derived["stage_m"] - 97.0)


def test_the_friction_slope_is_the_measured_fall_over_the_measured_length():
    assert _stage()["slope"] == pytest.approx(3.0 / 1000.0)


def test_a_bigger_discharge_stands_higher_in_the_same_channel():
    """The discrimination the 2 m default could not make: the level is a property
    of the reach and the flow through it."""
    assert _stage(discharge_q=250.0)["stage_m"] > _stage()["stage_m"]


def test_a_flatter_reach_stands_higher_for_the_same_flow():
    flat = dict(_REACH, bed_drop_m=0.3)
    steep = U.normal_depth_stage(_REACH, law=3, coefficient=33.0,
                                 discharge_q=50.0)
    ponded = U.normal_depth_stage(flat, law=3, coefficient=33.0,
                                  discharge_q=50.0)
    assert ponded["stage_m"] > steep["stage_m"]


def test_strickler_and_its_reciprocal_manning_derive_the_same_stage():
    """One law through two coefficients. A stage that moved when the sheet merely
    restated its roughness would mean the conveyance read the number wrong."""
    strickler = _stage(law=3, coefficient=33.0)
    manning = _stage(law=4, coefficient=1.0 / 33.0)
    assert manning["stage_m"] == pytest.approx(strickler["stage_m"], abs=1e-3)
    assert manning["law"] == "Manning" and strickler["law"] == "Strickler"


def test_a_chezy_run_derives_under_chezys_own_conveyance():
    derived = _stage(law=2, coefficient=60.0)
    area, perimeter = U._wetted([tuple(p) for p in _SECTION], derived["stage_m"])
    assert (60.0 * area * math.sqrt(area / perimeter * derived["slope"])
            == pytest.approx(50.0, rel=1e-3))


def test_the_section_closes_vertically_at_its_own_end_points():
    """A stage above the highest surveyed point rises between walls rather than
    spreading into ground the mesh does not hold - so a flat face is a rectangle
    and every discharge has a depth."""
    flat = dict(_REACH, outflow_section=[[0.0, 97.0], [60.0, 97.0]])
    derived = U.normal_depth_stage(flat, law=3, coefficient=33.0,
                                   discharge_q=50.0)
    area, perimeter = U._wetted([(0.0, 97.0), (60.0, 97.0)], derived["stage_m"])
    depth = derived["stage_m"] - 97.0
    assert area == pytest.approx(60.0 * depth)
    assert perimeter == pytest.approx(60.0 + 2.0 * depth)


# --------------------------------------------------------------------------- #
# What it refuses rather than defaulting past.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("reach", "sheet", "code"),
    [
        ({"bed_drop_m": 0.0}, {}, "TELEMAC_OUTFLOW_SLOPE_UNMEASURED"),
        ({"bed_drop_m": -2.0}, {}, "TELEMAC_OUTFLOW_SLOPE_UNMEASURED"),
        ({"reach_length_m": 0.0}, {}, "TELEMAC_OUTFLOW_SLOPE_UNMEASURED"),
        ({"outflow_section": [[0.0, 97.0]]}, {},
         "TELEMAC_OUTFLOW_SECTION_UNMEASURED"),
        ({}, {"law": 5}, "TELEMAC_OUTFLOW_FRICTION_UNREADABLE"),
        ({}, {"discharge_q": 0.0}, "TELEMAC_OUTFLOW_STAGE_UNDERIVABLE"),
        ({}, {"coefficient": 0.0}, "TELEMAC_OUTFLOW_STAGE_UNDERIVABLE"),
    ],
)
def test_an_input_the_stage_cannot_be_derived_from_refuses_by_name(reach, sheet, code):
    with pytest.raises(U.UniformFlowError) as exc:
        U.normal_depth_stage(dict(_REACH, **reach),
                             **{"law": 3, "coefficient": 33.0,
                                "discharge_q": 50.0, **sheet})
    assert exc.value.error_code == code


# --------------------------------------------------------------------------- #
# The catchment outlet: the SAME derivation, swept over a flow range.
# --------------------------------------------------------------------------- #
#: The outlet face of a catchment: a 10 m trapezoid at 10 m, banks rising 2 m.
_OUTLET_SECTION = [[0.0, 12.0], [5.0, 10.0], [15.0, 10.0], [20.0, 12.0]]


def _curve(**over):
    return U.derive_rating_curve(_OUTLET_SECTION, **{
        "law": 4, "coefficient": 0.05, "slope": 0.02,
        "q_ceiling_m3s": 51.0, **over})


def test_the_rating_curve_starts_dry_and_ends_at_the_stated_ceiling():
    """Its two ends are the two facts the engine holds it to: below the first
    point the level is that point's, above the last it is the last one's."""
    rows = _curve()["rows"]
    assert rows[0] == (0.0, 10.0)  # the dry section, at its own thalweg
    assert rows[-1][0] == pytest.approx(51.0, rel=1e-4)
    assert rows[-1][1] > rows[0][1]


def test_the_curve_rises_monotonically_so_the_engine_can_interpolate_it():
    rows = _curve()["rows"]
    assert all(b[0] > a[0] and b[1] > a[1] for a, b in zip(rows, rows[1:]))


def test_every_point_is_the_normal_depth_the_reachs_own_stage_would_be():
    """ONE derivation, two callers. A stage on the curve is the stage the reach's
    machinery solves for that same discharge over that same section."""
    q, z = _curve()["rows"][10]
    reach = U.normal_depth_stage(
        {"bed_top_m": 30.0, "bed_drop_m": 20.0, "reach_length_m": 1000.0,
         "outflow_section": _OUTLET_SECTION},
        law=4, coefficient=0.05, discharge_q=q)
    assert reach["stage_m"] == pytest.approx(z, abs=1e-3)


@pytest.mark.parametrize("over,code", [
    ({"slope": 0.0}, "TELEMAC_OUTFLOW_STAGE_UNDERIVABLE"),
    ({"coefficient": 0.0}, "TELEMAC_OUTFLOW_STAGE_UNDERIVABLE"),
    ({"q_ceiling_m3s": 0.0}, "TELEMAC_OUTFLOW_STAGE_UNDERIVABLE"),
    ({"law": 5}, "TELEMAC_OUTFLOW_FRICTION_UNREADABLE"),
])
def test_an_input_the_outlet_cannot_measure_refuses_by_name(over, code):
    with pytest.raises(U.UniformFlowError) as excinfo:
        _curve(**over)
    assert excinfo.value.error_code == code


def test_a_section_of_one_point_is_no_channel_to_derive_a_curve_over():
    with pytest.raises(U.UniformFlowError) as excinfo:
        U.derive_rating_curve([[0.0, 10.0]], law=4, coefficient=0.05,
                              slope=0.02, q_ceiling_m3s=51.0)
    assert excinfo.value.error_code == "TELEMAC_OUTFLOW_SECTION_UNMEASURED"


def test_the_curve_file_is_written_in_the_engines_own_block_format():
    """``read_fic_curves.f`` reads a header naming the boundary, a units line it
    skips, then two columns until a blank; under Q(n) the first column is the
    discharge. The file is the RATING composite's, beside the two keywords that
    name it."""
    from trid3nt_server.workflows.telemac.modules import T2D
    from trid3nt_server.workflows.telemac.modules.telemac2d import Rating

    slots, files = T2D.COMPOSITES["rating"].expand(
        Rating(at_boundary=2, of_boundaries=3,
               rows=[(0.0, 10.0), (51.0, 11.3)], note="derived Z(Q)"))
    assert slots["STAGE_DISCHARGE_CURVES"] == [0, 1, 0]
    lines = files["rog_rating.txt"].splitlines()
    assert lines[0].startswith("#")
    assert lines[1] == "Q(2) Z(2)"
    assert lines[2] == "m3/s m"
    assert lines[3].split() == ["0.000000", "10.0000"]
    assert lines[4].split() == ["51.000000", "11.3000"]


def test_the_catchments_outlet_slope_is_the_bed_over_the_elements_it_touches():
    """Measured, not chosen: the plane through the painted nodes of the elements
    the face belongs to."""
    xy = np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0], [10.0, 10.0]])
    bed = np.array([1.0, 0.8, 1.0, 0.8])  # falls 0.2 m over 10 m eastward
    cells = np.array([[0, 1, 2], [1, 3, 2]])
    assert D._bed_slope([1, 3], xy, bed, cells) == pytest.approx(0.02, rel=1e-6)


def test_a_flat_outlet_refuses_rather_than_holding_a_level_nobody_measured():
    xy = np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0], [10.0, 10.0]])
    cells = np.array([[0, 1, 2], [1, 3, 2]])
    from trid3nt_server.workflows.telemac.helpers.errors import RainOnGridError

    with pytest.raises(RainOnGridError) as excinfo:
        D._bed_slope([1, 3], xy, np.zeros(4), cells)
    assert excinfo.value.error_code == "TELEMAC_ROG_OUTLET_SLOPE_UNMEASURED"


def test_the_flow_range_is_the_gross_rain_rate_on_the_meshed_area():
    """The ceiling is a BOUND, not a guess: infiltration only removes water and
    storage only delays it, so nothing can leave faster than the rain arrives."""
    xy = np.array([[0.0, 0.0], [1000.0, 0.0], [0.0, 1000.0], [1000.0, 1000.0]])
    cells = np.array([[0, 1, 2], [1, 3, 2]])  # 1 km2
    ceiling, basis = D._rain_ceiling(
        {"kind": "design_storm", "intensity_mm_per_hr": 36.0}, cells, xy)
    assert ceiling == pytest.approx(0.036 / 3600.0 * 1.0e6)
    assert "1.000 km2" in basis and "36 mm/h" in basis

    peak, _basis = D._rain_ceiling(
        {"kind": "hyetograph", "series": [3.0, 12.5, 0.0]}, cells, xy)
    assert peak == pytest.approx(0.0125 / 3600.0 * 1.0e6)


def test_the_curve_is_spaced_EVENLY_IN_DISCHARGE_so_the_low_end_is_not_a_cliff():
    """The engine looks the curve up BY discharge and interpolates linearly, so a
    first interval carrying almost no flow and centimetres of stage makes the
    boundary swing metres on a trickle - and a boundary above a catchment that has
    not started running off yet lifts water back into it."""
    rows = _curve()["rows"]
    steps = [b[0] - a[0] for a, b in zip(rows, rows[1:])]
    assert max(steps) - min(steps) < 1e-5
    slopes = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(rows, rows[1:])]
    # the steepest interval is the first, and the curve flattens from there
    assert slopes == sorted(slopes, reverse=True)
