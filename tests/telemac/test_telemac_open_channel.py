"""The OPEN-CHANNEL hydraulics on top of any domain.

``open_water`` is engine-neutral about the water; a question that needs a
channel opened - an inflow carrying a flow, an outflow holding a level, a depth
to start at - settles that ON TOP of it, measured over the accepted mesh at the
roughness the deck is written at. Offline: no solve, no container.
"""

from __future__ import annotations
from trid3nt.workflows.telemac.authoring.selafin_io import (
    BOUNDARY_CONDITIONS_FILE,
    GEOMETRY_FILE,
)

import asyncio
from pathlib import Path

import numpy as np
import pytest

from trid3nt.inputs.observation import Observation
from trid3nt.workflows.telemac.authoring import accepted_mesh as MESH
from trid3nt.workflows.telemac.authoring import opening as D
from trid3nt.workflows.telemac.errors import TelemacError

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
    return {"artifact": _Artifact(), "min_edge_m": 20.0, "mesh_id": "mesh-1"}


def _files(**over):
    """What the mesh-files stage hands the author: the pair and its walk."""
    walk = {"roles": _ROLES, "liquid_boundary_order": ["inflow", "outflow"],
            "liquid_boundary_prescribes": ["flowrate", "elevation"]}
    walk.update(over)
    return {"engine_files": {GEOMETRY_FILE: "mesh.slf",
                             BOUNDARY_CONDITIONS_FILE: "mesh.cli"},
            "topology": walk}


@pytest.fixture()
def settled(monkeypatch):
    """The accepted mesh and its topology, answered without touching a store."""
    monkeypatch.setattr(D, "mesh_nodes", lambda mesh: (_XY, _BED))
    return _mesh


def _run(**over):
    ask = {"mesh": _mesh(), "files": _files(), "friction_law": 3,
           "friction_coefficient": 33.0, "carrier": 600.0}
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


def test_the_outflow_stage_is_the_normal_depth_over_the_outflow_section(settled):
    """The depth the section conveys the inflow at, at the deck's roughness and the measured fall."""
    channel = _run()
    assert channel["depth_m"] == pytest.approx(
        channel["outflow_stage_m"] - 94.0, abs=1e-3)
    assert channel["inflow_q_m3s"] == 600.0
    assert channel["friction_law"] == 3
    assert channel["normal"]["slope"] == pytest.approx(0.003)


def test_the_addition_hands_the_base_the_level_and_how_it_is_laid(settled):
    """What the addition MEASURED is what the base opens the water at: flat at the derived outflow level, never a
    sheet of one depth over a measured bed's bars and pools."""
    channel = _run()
    assert channel["level_m"] == channel["outflow_stage_m"]
    assert channel["opening"] == "CONSTANT ELEVATION"


def test_a_stated_discharge_reads_as_the_value_it_is(settled):
    """The inflow run carries ONE value however it arrived: the slot hands over
    a number the caller stated the same way it hands over a reading, and the
    note says which of the two this was."""
    channel = _run(carrier=Observation(value=600.0))
    assert channel["inflow_q_m3s"] == 600.0
    assert "was stated" in channel["discharge_note"]


def test_the_carrier_reading_fills_the_flow_and_says_who_reported_it(settled):
    reading = Observation(value=600.0, site_name="Willamette", sampled="2026-01-01")
    channel = _run(carrier=reading)
    assert channel["inflow_q_m3s"] == pytest.approx(600.0)
    assert "Willamette" in channel["discharge_note"]
    assert "2026-01-01" in channel["discharge_note"]


def test_a_carrier_that_never_passed_its_slot_refuses_by_name(settled):
    """Which site reports the flow, and how old the sample is, is the observation
    slot's business - this step reads a value, never a record."""
    with pytest.raises(TelemacError) as exc:
        _run(carrier={"type": "FeatureCollection"})
    assert exc.value.error_code == "TELEMAC_CARRIER_UNINGESTED"
    assert "Name the row discharge" in str(exc.value)


def test_no_carrier_at_all_opens_no_channel_and_hands_back_the_level(settled):
    """Whether a run CARRIES a flow is settled at run time: an absent carrier is
    no channel to open, so the level it was handed is what the base opens at."""
    assert _run(carrier=None, stage=98.5) == 98.5


def test_a_domain_with_no_inflow_run_has_no_channel_to_open(settled):
    """A body whose edge names no inflow is CLOSED: there is no channel here to
    measure, so the addition adds nothing, imposes no flow and hands back the
    level it was given for the base to open the water flat at."""
    closed = _run(stage=98.5, files=_files(
        roles={"outflow": [4, 5, 6, 7]}, liquid_boundary_order=["outflow"],
        liquid_boundary_prescribes=["elevation"]))
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
    from trid3nt.workflows.telemac.helpers.uniform_flow import (
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
    dry - the flowrate face among them - so the uniform-flow depth is derived and the run opens flat at it."""
    channel = _run(stage=Observation(value=95.0, site_name="a gauge"))
    assert channel["opening"] == "CONSTANT ELEVATION"
    assert channel["normal"]["slope"] > 0.0
    assert channel["level_m"] > 97.0


def test_a_derived_level_under_the_whole_inflow_face_refuses_by_name(settled):
    """50 m3/s is conveyed 0.8 m over the outflow thalweg, 2.2 m under the inflow face's lowest node: opened flat,
    the face the flow is prescribed on would be dry."""
    with pytest.raises(TelemacError) as exc:
        _run(carrier=50.0)
    assert exc.value.error_code == "TELEMAC_INFLOW_FACE_DRY"
    assert "97.000" in str(exc.value)



#: The Missouri calibration reach's recorded mesh and boundary file (tests/fixtures/telemac_missouri/README.md).
_MISSOURI = Path(__file__).resolve().parents[1] / "fixtures" / "telemac_missouri"


def _missouri_roles():
    roles = {"inflow": [], "outflow": []}
    for line in (_MISSOURI / "domain.cli").read_text().splitlines():
        code = tuple(line.split()[:3])
        if code in (("4", "5", "5"), ("5", "4", "4")):
            roles["inflow" if code[0] == "4" else "outflow"].append(int(line.split()[11]) - 1)
    return roles


def test_the_missouri_reach_opens_flat_at_its_derived_outflow_level(monkeypatch):
    """The recorded survey bed carries bars 3-4 m over its pools: a sheet of the 5.896 m normal depth over it stood
    at 291.06-298.17 m and drained through the outflow for the whole run. Flat at 291.385 m it starts under its
    uniform-flow surface with the inflow face wet."""
    from trid3nt.tools.mesh.shared.nodes import read_accepted_mesh_nodes

    xy, _cells, bed, _ll = read_accepted_mesh_nodes(str(_MISSOURI / "mesh.2dm"), utm_epsg=32615)
    monkeypatch.setattr(D, "mesh_nodes", lambda mesh: (xy, bed))
    roles = _missouri_roles()
    channel = _run(files=_files(roles=roles), carrier=889.1489829888001)
    assert channel["opening"] == "CONSTANT ELEVATION"
    assert channel["level_m"] == pytest.approx(291.385, abs=1e-3)
    assert float(np.min(bed[roles["inflow"]])) < channel["level_m"]


#: The main stem NLDI walked downstream from the Missouri release (tests/fixtures/nhdplus_nldi/README.md): the line the
#: reach was cut along, which a river run's domain carries as its centerline.
_MISSOURI_WALK = Path(__file__).resolve().parents[1] / "fixtures" / "nhdplus_nldi" / "missouri_release_dm_7km.fgb"


def _missouri_held(monkeypatch, level_m):
    from types import SimpleNamespace

    from trid3nt.tools.mesh.shared.nodes import read_accepted_mesh_nodes

    xy, _cells, bed, _ll = read_accepted_mesh_nodes(str(_MISSOURI / "mesh.2dm"), utm_epsg=32615)
    monkeypatch.setattr(D, "mesh_nodes", lambda mesh: (xy, bed))
    mesh = {"artifact": type("A", (), {"utm_epsg": 32615, "probes": {}})(), "min_edge_m": 40.0, "mesh_id": "m"}
    domain = SimpleNamespace(companions={"centerline": str(_MISSOURI_WALK)})
    channel = _run(mesh=mesh, files=_files(roles=_missouri_roles()), carrier=889.1489829888001,
                   stage=Observation(value=level_m, site_name="a gauge below the reach"), domain=domain)
    return channel, xy, bed


def test_a_reach_held_under_its_inflows_level_opens_sloped_along_its_walked_line(monkeypatch):
    """The recorded Missouri reach at Strickler 33 and 889.149 m3/s: its inflow section conveys the flow at 293.95 m on
    the fall the measured bed makes along the walked line (0.888 m over 7037 m). A level measured under it at the
    outflow opens the reach on the plane between the two, never flat at the outflow's level over the whole reach."""
    channel, xy, bed = _missouri_held(monkeypatch, 292.0)
    surface = channel["surface"]
    assert channel["opening"] == D.SLOPED == "SPECIAL"
    assert channel["outflow_stage_m"] == pytest.approx(292.0) and surface["z_out"] == pytest.approx(292.0)
    assert surface["z_in"] == pytest.approx(293.95, abs=1e-3)
    assert surface["length_m"] == pytest.approx(7037.4, abs=0.5)
    roles = _missouri_roles()
    faces = D.surface_at(surface, np.array([xy[roles[r]].mean(axis=0) for r in ("inflow", "outflow")]))
    assert faces == pytest.approx([293.95, 292.0], abs=1e-3)
    levels = D.surface_at(surface, xy)
    assert levels.min() >= 292.0 - 1e-9 and levels.max() <= 293.95 + 1e-9
    assert bool(np.all(levels > bed))


def test_a_level_over_the_inflows_own_opens_flat_at_it(monkeypatch):
    channel, _xy, _bed = _missouri_held(monkeypatch, 294.5)
    assert channel["opening"] == "CONSTANT ELEVATION" and channel["surface"] is None
    assert channel["level_m"] == pytest.approx(294.5)


def test_the_sloped_openings_routine_is_the_engines_own_hook_in_fixed_form(monkeypatch):
    channel, _xy, _bed = _missouri_held(monkeypatch, 292.0)
    surface = channel["surface"]
    text = D.surface_routine(surface)
    assert "SUBROUTINE USER_CONDIN_H" in text and f"NL = {len(surface['line'])}" in text
    assert "Z_IN = 293.950D0" in text and "Z_OUT = 292.000D0" in text
    assert all(len(line) <= 72 for line in text.splitlines())


def test_the_sloped_opening_is_compiled_beside_the_boundary_lists():
    from trid3nt.workflows.telemac.modules.telemac2d import _boundaries

    surface = {"line": [[0.0, 0.0], [1000.0, 0.0]], "s_in": 0.0, "s_out": 1000.0, "z_in": 98.0, "z_out": 96.0}
    measured = {"liquid_boundary_order": ["inflow", "outflow"], "liquid_boundary_prescribes": ["flowrate", "elevation"],
                "inflow_q_m3s": 600.0, "outflow_stage_m": 96.0, "inflow_q_series": None,
                "outflow_stage_series": None, "start_time_s": 0.0, "until_s": 60.0, "time_step_s": 1.0}
    keywords, files = _boundaries({"measured": {**measured, "surface": surface}, "tracers": []})
    assert keywords["FORTRAN_FILE"] == "user_fortran"
    assert "SUBROUTINE USER_CONDIN_H" in files["user_fortran/user_condin_h.f"]
    keywords, files = _boundaries({"measured": measured, "tracers": []})
    assert "FORTRAN_FILE" not in keywords and files == {}


def test_the_derived_outflows_friction_slope_is_measured_along_the_walked_line(monkeypatch):
    """The recorded Missouri reach meanders: its faces stand 6481.3 m apart in a straight line and 7037.4 m apart along
    the walked line the reach was cut from. The fall the bed makes between them is over the river's length, so the
    slope is gentler and the same flow stands higher: 291.489 m, not the 291.385 m the chord gave."""
    from types import SimpleNamespace

    from trid3nt.tools.mesh.shared.nodes import read_accepted_mesh_nodes

    xy, _cells, bed, _ll = read_accepted_mesh_nodes(str(_MISSOURI / "mesh.2dm"), utm_epsg=32615)
    monkeypatch.setattr(D, "mesh_nodes", lambda mesh: (xy, bed))
    mesh = {"artifact": type("A", (), {"utm_epsg": 32615, "probes": {}})(), "min_edge_m": 40.0, "mesh_id": "m"}
    channel = _run(mesh=mesh, files=_files(roles=_missouri_roles()), carrier=889.1489829888001,
                   domain=SimpleNamespace(companions={"centerline": str(_MISSOURI_WALK)}))
    assert channel["reach_length_m"] == pytest.approx(7037.4, abs=0.5)
    assert channel["normal"]["length_m"] == pytest.approx(7037.4, abs=0.5)
    assert channel["outflow_stage_m"] == pytest.approx(291.489, abs=1e-3)
    # A domain holding no walked line (a drawn one) measures the chord, as before.
    assert _run(mesh=mesh, files=_files(roles=_missouri_roles()), carrier=889.1489829888001
                )["outflow_stage_m"] == pytest.approx(291.385, abs=1e-3)
