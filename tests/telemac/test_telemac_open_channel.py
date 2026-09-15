"""The OPEN-CHANNEL hydraulics on top of any domain.

``settle_domain`` is engine-neutral about the water; a question that needs a
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
           "discharge_m3s": 50.0}
    ask.update(over)
    return asyncio.run(D.settle_open_channel(**ask))


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


def test_the_step_is_the_accepted_meshs_own_measured_edge(settled):
    """This step runs AHEAD of the domain step, so it reads the edge itself
    rather than waiting for a number it cannot see yet."""
    from trid3nt_server.workflows.telemac.helpers.time_step import (
        suggest_time_step_s,
    )

    assert _run()["time_step_s"] == suggest_time_step_s(20.0)


def test_a_stated_discharge_stands_over_any_record(settled):
    reading = Observation(value=9.0, site_name="a gauge", sampled="2026-01-01")
    channel = _run(discharge_m3s=50.0, carrier=reading)
    assert channel["inflow_q_m3s"] == 50.0
    assert "was stated" in channel["discharge_note"]


def test_the_carrier_reading_fills_the_flow_and_says_who_reported_it(settled):
    reading = Observation(value=12.5, site_name="Willamette", sampled="2026-01-01")
    channel = _run(discharge_m3s=None, carrier=reading)
    assert channel["inflow_q_m3s"] == pytest.approx(12.5)
    assert "Willamette" in channel["discharge_note"]
    assert "2026-01-01" in channel["discharge_note"]


def test_a_carrier_that_never_passed_its_slot_refuses_by_name(settled):
    """Which site reports the flow, and how old the sample is, is the observation
    slot's business - this step reads a value, never a record."""
    with pytest.raises(TelemacError) as exc:
        _run(discharge_m3s=None, carrier={"type": "FeatureCollection"})
    assert exc.value.error_code == "TELEMAC_CARRIER_UNINGESTED"
    assert "Data.observation" in str(exc.value)


def test_no_flow_anywhere_refuses_rather_than_opening_on_a_guess(settled):
    with pytest.raises(TelemacError) as exc:
        _run(discharge_m3s=None, carrier=None)
    assert exc.value.error_code == "TELEMAC_INFLOW_DISCHARGE_UNMEASURED"


def test_a_domain_with_no_inflow_run_has_no_channel_to_open(settled, monkeypatch):
    monkeypatch.setattr(D, "read_topology", lambda uri: {
        "roles": {"outflow": [4, 5, 6, 7]}, "liquid_boundary_order": ["outflow"],
        "liquid_boundary_prescribes": ["elevation"]})
    with pytest.raises(TelemacError) as exc:
        _run()
    assert exc.value.error_code == "TELEMAC_MESH_BED_UNMEASURED"
    assert "'inflow'" in str(exc.value)
