"""The reach deck's outflow stage: a normal depth over the measured channel.

The downstream cap of a reach used to be held at the outflow bed plus a declared
2 m. That number was not a property of the reach - it was the same on a mountain
creek and a coastal plain river - and it was the one level the run's whole water
surface is anchored to.

What replaces it is a COMPUTATION over data the accepted mesh already carries:
the fall between the two role faces over the length the mesh was built on is the
friction slope, the outflow face's own transect is the channel, the deck's own
roughness and its own prescribed discharge close the uniform-flow equation. No
gauge, no rating curve, no second datum. What cannot be measured refuses by name
rather than falling back to a level nobody derived.

Offline: arithmetic and text, no mesh build and no solve.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from trid3nt_server.workflows.telemac.steps import author as A
from trid3nt_server.workflows.telemac.steps import deck as D
from trid3nt_server.workflows.telemac.steps.errors import TelemacDyeScenarioError

#: A trapezoid: 40 m bed at 97 m, banks rising 3 m over 10 m either side.
_SECTION = [[0.0, 100.0], [10.0, 97.0], [50.0, 97.0], [60.0, 100.0]]
_REACH = {"bed_top_m": 100.0, "bed_drop_m": 3.0, "reach_length_m": 1000.0,
          "outflow_section": _SECTION}


def _stage(**deck):
    return A._normal_depth_stage(A._Sheet({"inflow_q_m3s": 50.0, **deck}), _REACH)


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
def test_the_stage_is_the_depth_at_which_the_section_conveys_the_decks_discharge():
    """Manning's equation read back over the section the stage was solved on.

    The assertion is the physics, not the number: whatever depth came out, the
    channel at that depth must convey exactly the discharge the deck prescribes
    upstream at the roughness the deck writes.
    """
    derived = _stage()
    area, perimeter = A._wetted([tuple(p) for p in _SECTION], derived["stage_m"])
    conveyed = (derived["coefficient"] * area * (area / perimeter) ** (2.0 / 3.0)
                * math.sqrt(derived["slope"]))
    assert conveyed == pytest.approx(50.0, rel=1e-3)
    assert derived["depth_m"] == pytest.approx(derived["stage_m"] - 97.0)


def test_the_friction_slope_is_the_measured_fall_over_the_measured_length():
    assert _stage()["slope"] == pytest.approx(3.0 / 1000.0)


def test_a_bigger_discharge_stands_higher_in_the_same_channel():
    """The discrimination the 2 m default could not make: the level is a property
    of the reach and the flow through it."""
    assert _stage(inflow_q_m3s=250.0)["stage_m"] > _stage()["stage_m"]


def test_a_flatter_reach_stands_higher_for_the_same_flow():
    flat = dict(_REACH, bed_drop_m=0.3)
    steep = A._normal_depth_stage(A._Sheet({"inflow_q_m3s": 50.0}), _REACH)
    ponded = A._normal_depth_stage(A._Sheet({"inflow_q_m3s": 50.0}), flat)
    assert ponded["stage_m"] > steep["stage_m"]


def test_strickler_and_its_reciprocal_manning_derive_the_same_stage():
    """One law through two coefficients. A stage that moved when the deck merely
    restated its roughness would mean the conveyance read the number wrong."""
    strickler = _stage(friction_law=3, friction_coefficient=33.0)
    manning = _stage(friction_law=4, friction_coefficient=1.0 / 33.0)
    assert manning["stage_m"] == pytest.approx(strickler["stage_m"], abs=1e-3)
    assert manning["law"] == "Manning" and strickler["law"] == "Strickler"


def test_a_chezy_deck_derives_under_chezys_own_conveyance():
    derived = _stage(friction_law=2, friction_coefficient=60.0)
    area, perimeter = A._wetted([tuple(p) for p in _SECTION], derived["stage_m"])
    assert (60.0 * area * math.sqrt(area / perimeter * derived["slope"])
            == pytest.approx(50.0, rel=1e-3))


def test_the_section_closes_vertically_at_its_own_end_points():
    """A stage above the highest surveyed point rises between walls rather than
    spreading into ground the mesh does not hold - so a flat face is a rectangle
    and every discharge has a depth."""
    flat = dict(_REACH, outflow_section=[[0.0, 97.0], [60.0, 97.0]])
    derived = A._normal_depth_stage(A._Sheet({"inflow_q_m3s": 50.0}), flat)
    area, perimeter = A._wetted([(0.0, 97.0), (60.0, 97.0)], derived["stage_m"])
    depth = derived["stage_m"] - 97.0
    assert area == pytest.approx(60.0 * depth)
    assert perimeter == pytest.approx(60.0 + 2.0 * depth)


# --------------------------------------------------------------------------- #
# What it refuses rather than defaulting past.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("reach", "deck", "code"),
    [
        ({"bed_drop_m": 0.0}, {}, "TELEMAC_OUTFLOW_SLOPE_UNMEASURED"),
        ({"bed_drop_m": -2.0}, {}, "TELEMAC_OUTFLOW_SLOPE_UNMEASURED"),
        ({"reach_length_m": 0.0}, {}, "TELEMAC_OUTFLOW_SLOPE_UNMEASURED"),
        ({"outflow_section": [[0.0, 97.0]]}, {},
         "TELEMAC_OUTFLOW_SECTION_UNMEASURED"),
        ({}, {"friction_law": 5}, "TELEMAC_OUTFLOW_FRICTION_UNREADABLE"),
        ({}, {"inflow_q_m3s": 0.0}, "TELEMAC_OUTFLOW_STAGE_UNDERIVABLE"),
        ({}, {"friction_coefficient": 0.0}, "TELEMAC_OUTFLOW_STAGE_UNDERIVABLE"),
    ],
)
def test_an_input_the_stage_cannot_be_derived_from_refuses_by_name(reach, deck, code):
    with pytest.raises(A.DeckAuthorError) as exc:
        A._normal_depth_stage(A._Sheet({"inflow_q_m3s": 50.0, **deck}),
                              dict(_REACH, **reach))
    assert exc.value.error_code == code


# --------------------------------------------------------------------------- #
# What the deck says about it.
# --------------------------------------------------------------------------- #
def _cas(tmp_path, **deck) -> str:
    A.author_reach_deck(
        tmp_path, deck={"name": "reach", "inflow_q_m3s": 50.0,
                        "init_depth_m": 2.0, "duration_s": 600.0,
                        "time_step_s": 1.0, **deck},
        geometry="mesh.slf", boundary="mesh.cli", results="r2d.slf",
        cas_name="t2d_river.cas", liquid_boundary_order=("inflow", "outflow"),
        liquid_boundary_prescribes=("flowrate", "elevation"),
        bed=_REACH, source_utm=(500.0, 0.0))
    return (tmp_path / "t2d_river.cas").read_text()


def test_the_deck_states_the_stage_and_every_input_it_was_derived_from(tmp_path):
    """A prescribed level a reader cannot check against the geometry file is a
    number to be taken on faith."""
    cas = _cas(tmp_path)
    assert "/  Friction slope 0.003000 = 3.000 m over 1000 m" in cas
    assert "normal depth 0.792 m over the" in cas
    assert "/  measured outflow section for 50 m3/s at Strickler 33" in cas
    assert "PRESCRIBED ELEVATIONS           = 0.0;97.792" in cas


def test_the_initial_depth_is_no_longer_the_level_the_run_is_anchored_at(tmp_path):
    """It survives as the constant depth a fresh run starts from, and moving it
    no longer moves the outflow boundary."""
    shallow = _cas(tmp_path, init_depth_m=0.5)
    assert "INITIAL DEPTH                   = 0.500" in shallow
    assert "PRESCRIBED ELEVATIONS           = 0.0;97.792" in shallow


def test_the_stage_is_derived_at_the_roughness_the_deck_goes_on_to_write(tmp_path):
    cas = _cas(tmp_path, friction_law=4, friction_coefficient=0.05)
    assert "LAW OF BOTTOM FRICTION          = 4" in cas
    assert "FRICTION COEFFICIENT            = 0.05" in cas
    assert "at Manning 0.05" in cas
