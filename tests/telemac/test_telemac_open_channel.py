"""The OPEN-CHANNEL hydraulics on top of any domain.

``open_water`` is engine-neutral about the water; a question that needs a
channel opened - an inflow carrying a flow, an outflow holding a level, a depth
to start at - settles that ON TOP of it, measured over the accepted mesh at the
roughness the deck is written at. Offline: no solve, no container.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from trid3nt_server.inputs.observation import Observation
from trid3nt_server.workflows.telemac.authoring import assembler as D
from trid3nt_server.workflows.telemac.errors import TelemacError

#: Two faces of a straight channel 1 km apart, each a trapezoid falling 3 m
#: between them - the fall over the distance IS the friction slope.
_INFLOW_XY = [[0.0, 0.0], [0.0, 10.0], [0.0, 50.0], [0.0, 60.0]]
_OUTFLOW_XY = [[1000.0, 0.0], [1000.0, 10.0], [1000.0, 50.0], [1000.0, 60.0]]
_XY = np.array(_INFLOW_XY + _OUTFLOW_XY)
_BED = np.array([100.0, 97.0, 97.0, 100.0, 97.0, 94.0, 94.0, 97.0])
_ROLES = {"inflow": [0, 1, 2, 3], "outflow": [4, 5, 6, 7]}


class _Artifact:
    utm_epsg = 32610
    probes: dict = {}


def _mesh():
    return {"artifact": _Artifact(), "topology_uri": "topology.json",
            "min_edge_m": 20.0, "mesh_id": "mesh-1"}


@pytest.fixture()
def settled(monkeypatch):
    """The accepted mesh and its topology, answered without touching a store."""
    monkeypatch.setattr(D, "mesh_nodes", lambda mesh: (_XY, _BED))
    monkeypatch.setattr(D, "read_topology", lambda uri: {
        "roles": _ROLES, "liquid_boundary_order": ["inflow", "outflow"],
        "liquid_boundary_prescribes": ["flowrate", "elevation"]})
    return _mesh


def _run(**over):
    ask = {"mesh": _mesh(), "friction_law": 3, "friction_coefficient": 33.0,
           "carrier": 50.0}
    ask.update(over)
    return asyncio.run(D.open_channel(**ask))


def test_the_channel_measures_its_slope_between_the_two_runs_own_nodes(settled):
    """Not along a line laid beside the domain: the fall between the faces the
    water enters and leaves through, over the distance between them."""
    measured = D._measured_channel(_ROLES, _XY, _BED)
    assert measured["bed_drop_m"] == pytest.approx(3.0)
    assert measured["reach_length_m"] == pytest.approx(1000.0)
    assert measured["outflow_section"] == [[0.0, 97.0], [10.0, 94.0],
                                           [50.0, 94.0], [60.0, 97.0]]


def test_the_run_opens_at_the_same_normal_depth_the_outflow_stage_is(settled):
    """One derivation read twice: a horizontal surface at the outlet's level
    would leave every node upstream of it dry."""
    channel = _run()
    assert channel["depth_m"] == pytest.approx(
        channel["outflow_stage_m"] - 94.0, abs=1e-3)
    assert channel["inflow_q_m3s"] == 50.0
    assert channel["friction_law"] == 3
    assert channel["normal"]["slope"] == pytest.approx(0.003)


def test_the_addition_hands_the_base_the_level_and_how_it_is_laid(settled):
    """What the addition MEASURED is what the base opens the water at: the level
    and the keyword saying whether that surface follows the bed or lies flat."""
    channel = _run()
    assert channel["level_m"] == channel["outflow_stage_m"]
    assert channel["opening"] == "CONSTANT DEPTH"


def test_a_stated_discharge_reads_as_the_value_it_is(settled):
    """The inflow run carries ONE value however it arrived: the slot hands over
    a number the caller stated the same way it hands over a reading, and the
    note says which of the two this was."""
    channel = _run(carrier=Observation(value=50.0))
    assert channel["inflow_q_m3s"] == 50.0
    assert "was stated" in channel["discharge_note"]


def test_the_carrier_reading_fills_the_flow_and_says_who_reported_it(settled):
    reading = Observation(value=12.5, site_name="Willamette", sampled="2026-01-01")
    channel = _run(carrier=reading)
    assert channel["inflow_q_m3s"] == pytest.approx(12.5)
    assert "Willamette" in channel["discharge_note"]
    assert "2026-01-01" in channel["discharge_note"]


def test_a_carrier_that_never_passed_its_slot_refuses_by_name(settled):
    """Which site reports the flow, and how old the sample is, is the observation
    slot's business - this step reads a value, never a record."""
    with pytest.raises(TelemacError) as exc:
        _run(carrier={"type": "FeatureCollection"})
    assert exc.value.error_code == "TELEMAC_CARRIER_UNINGESTED"
    assert "Data.observation" in str(exc.value)


def test_no_flow_anywhere_refuses_rather_than_opening_on_a_guess(settled):
    with pytest.raises(TelemacError) as exc:
        _run(carrier=None)
    assert exc.value.error_code == "TELEMAC_INFLOW_DISCHARGE_UNMEASURED"


def test_a_domain_with_no_inflow_run_has_no_channel_to_open(settled, monkeypatch):
    """A body whose edge names no inflow is CLOSED: there is no channel here to
    measure, so the addition adds nothing, imposes no flow and hands back the
    level it was given for the base to open the water flat at."""
    monkeypatch.setattr(D, "read_topology", lambda uri: {
        "roles": {"outflow": [4, 5, 6, 7]}, "liquid_boundary_order": ["outflow"],
        "liquid_boundary_prescribes": ["elevation"]})
    closed = _run(stage=98.5)
    assert closed["level_m"] == 98.5
    assert (closed["depth_m"], closed["opening"]) == (None, None)
    assert (closed["inflow_q_m3s"], closed["outflow_stage_m"]) == (None, None)
    assert "names no runs" in closed["discharge_note"]


#: The same two faces, LEVEL with each other: a reach cut from a surface DEM,
#: where the water top is the bed and nothing falls downstream.
_FLAT_BED = np.array([97.0, 94.0, 94.0, 97.0, 97.0, 94.0, 94.0, 97.0])


@pytest.fixture()
def flat(monkeypatch, settled):
    monkeypatch.setattr(D, "mesh_nodes", lambda mesh: (_XY, _FLAT_BED))
    return _mesh


def test_a_reach_that_does_not_fall_holds_at_the_level_that_was_measured(flat):
    """No fall is no uniform-flow depth: the outflow holds at the measured
    elevation and the run opens flat at the depth that leaves over the section."""
    channel = _run(stage=Observation(value=96.0, site_name="a gauge"))
    assert channel["outflow_stage_m"] == pytest.approx(96.0)
    assert channel["depth_m"] == pytest.approx(2.0)
    assert channel["normal"]["slope"] == 0.0


def test_a_level_at_or_below_the_bed_refuses_rather_than_opening_dry(flat):
    """A gauge's height above its OWN zero is not an elevation on the datum the
    bed is painted on, and the refusal says which two numbers disagree."""
    with pytest.raises(TelemacError) as exc:
        _run(stage=4.2)
    assert exc.value.error_code == "TELEMAC_OUTFLOW_STAGE_BELOW_BED"
    assert "94.000" in str(exc.value)


def test_a_reach_that_does_not_fall_and_has_no_level_refuses_by_name(flat):
    """The uniform-flow refusal stands: a level has to come from somewhere."""
    from trid3nt_server.workflows.telemac.helpers.uniform_flow import (
        UniformFlowError,
    )

    with pytest.raises(UniformFlowError) as exc:
        _run()
    assert exc.value.error_code == "TELEMAC_OUTFLOW_SLOPE_UNMEASURED"


def test_a_level_that_never_passed_its_slot_refuses_by_name(flat):
    with pytest.raises(TelemacError) as exc:
        _run(stage={"type": "FeatureCollection"})
    assert exc.value.error_code == "TELEMAC_STAGE_UNINGESTED"


def test_a_measured_level_over_the_reach_stands_over_the_derivation(settled):
    """A uniform-flow depth is a MODEL of the same surface a gauge measured, and
    a model does not stand over a measurement of the thing it models. The bed
    here falls three metres, so the derivation was available and was not taken."""
    channel = _run(stage=Observation(value=101.0, site_name="a gauge"))
    assert channel["opening"] == "CONSTANT ELEVATION"
    assert channel["level_m"] == pytest.approx(101.0)
    assert channel["normal"]["slope"] == 0.0


def test_a_level_that_does_not_reach_the_inflow_face_leaves_the_derivation(settled):
    """A horizontal surface at the outflow's level would leave the inflow face
    dry - the flowrate face among them - so the uniform-flow depth is what the
    run opens at, and it says which of the two it took."""
    channel = _run(stage=Observation(value=95.0, site_name="a gauge"))
    assert channel["opening"] == "CONSTANT DEPTH"
    assert channel["normal"]["slope"] > 0.0
