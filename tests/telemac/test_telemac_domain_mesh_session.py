"""Any domain authors its run on an ACCEPTED mesh, and says so.

Offline. The settle is engine-neutral about the water: it measures the mesh it
was handed, states the clock the run turns on, stages the geometry pair under
the names the deck states, and hands the box the facts only the server measured.
Pinned: what it measures, the dt seam's reader, and what the worker is handed.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.telemac.authoring import accepted_mesh as mesh_mod
from trid3nt_server.workflows.telemac.authoring import opening as asm_mod
from trid3nt_server.workflows.telemac.errors import TelemacError

#: The deck's own file statements, which is where the staged names come from.
_FILES = {"geometry": "rog.slf", "boundary": "rog.cli", "result": "r2d_rog.slf"}


def _mesh_record(*, min_edge_m: float | None = None,
                 topology_uri: str | None = "s3://m/M01/mesh_topology.json") -> dict:
    """A mesh step's result, composed the way the mesh step composes a real one.

    Every derived field is READ off the artifact through the product's own
    readers, so this stand-in cannot report a measured edge its probes never
    held."""
    from trid3nt_server.workflows.mesh.artifact import measured_min_edge_m

    probes = ({"edge_length_m": {"min": float(min_edge_m), "max": 40.0,
                                 "mean": 20.0}}
              if min_edge_m is not None else {})
    artifact = MeshArtifact(
        mesh_id="M01", name="Coweeta Creek", mode="om2d",
        display_uri="s3://m/M01/mesh.2dm", slf_uri="s3://m/M01/domain.slf",
        cli_uri="s3://m/M01/domain.cli", topology_uri=topology_uri,
        recipe_uri="s3://m/M01/mesh_recipe.jsonl",
        crs_authid="EPSG:32610", has_bathymetry=True, utm_epsg=32610,
        node_count=539, element_count=902,
        bbox=(-124.2, 40.4, -124.0, 40.6), probes=probes,
        provenance={"bed_source": "cop-dem-glo-30"})
    return {"artifact": artifact, "mesh_id": artifact.mesh_id,
            "slf_uri": artifact.slf_uri, "cli_uri": artifact.cli_uri,
            "topology_uri": artifact.topology_uri,
            "display_uri": artifact.display_uri,
            "recipe_uri": artifact.recipe_uri,
            "node_count": artifact.node_count,
            "element_count": artifact.element_count,
            "min_edge_m": measured_min_edge_m(artifact),
            "provenance": dict(artifact.provenance)}


@pytest.fixture()
def settle(monkeypatch, tmp_path):
    """``open_water`` with its world-reads stood in for."""
    import numpy as np

    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(mesh_mod, "read_topology",
                        lambda _uri: {
                            "roles": {"inflow": [0, 3], "outflow": [1, 2]},
                            "liquid_boundary_order": ["outflow", "inflow"],
                            "liquid_boundary_prescribes": ["elevation",
                                                           "flowrate"]})

    def _accepted_nodes(_uri, utm_epsg=None):
        return (np.array([[-10.0, -50.0], [6010.0, -50.0], [6010.0, 50.0],
                          [-10.0, 50.0]]),
                np.array([[0, 1, 2], [0, 2, 3]]),
                np.array([12.0, 10.2, 10.2, 12.0]), None)

    monkeypatch.setattr(asm_mod, "read_accepted_mesh_nodes", _accepted_nodes)
    monkeypatch.setattr(mesh_mod, "read_accepted_mesh_nodes", _accepted_nodes)

    async def _settle(**kwargs):
        return await asm_mod.open_water(duration_s=3600.0,
                                        mesh_resolution_m=14.0,
                                        **_FILES, **kwargs)

    return _settle


@pytest.mark.asyncio
async def test_the_settle_knows_the_mesh_the_clock_and_the_water_it_holds(
        settle):
    """Engine-neutral: a name off the artifact, the clock the ask states, the
    walk the mesh measured. Nothing stated the level and the bed is on a datum,
    so there is no water measured and nothing is claimed about it."""
    out = await settle(mesh=_mesh_record(min_edge_m=14.0))
    assert out["name"] == "coweeta_creek"
    assert out["title"] == "coweeta_creek DOMAIN"
    assert out["mesh_size_m"] == 14.0
    assert out["time_step_s"] == 0.7
    assert out["duration_s"] == 3600.0 and out["until_s"] == 3600.0
    assert out["liquid_boundary_order"] == ["outflow", "inflow"]
    assert out["liquid_boundary_prescribes"] == ["elevation", "flowrate"]
    assert out["bed_source"] == "cop-dem-glo-30"
    assert {out[key] for key in ("level_m", "depth_m", "max_depth_m", "opening",
                                 "outflow_stage_m", "inflow_q_m3s")} == {None}


@pytest.mark.asyncio
async def test_a_mesh_that_measured_no_edge_keeps_the_one_that_was_asked_for(settle):
    """No probes to read -> the requested edge decides dt, and the label says so."""
    out = await settle(mesh=_mesh_record())
    assert out["mesh_size_m"] == 14.0 and out["time_step_s"] == 0.7
    assert out["mesh_resolution_label"].endswith("asked edge (mesh unmeasured)")


@pytest.mark.asyncio
async def test_a_refined_mesh_tightens_the_run_timestep(settle):
    """Refine at the gate and the run's dt follows the mesh, not the ask: the
    stability criterion is a statement about the mesh that exists."""
    asked = await settle(mesh=_mesh_record(min_edge_m=14.0))
    refined = await settle(mesh=_mesh_record(min_edge_m=7.0))
    assert (asked["time_step_s"], refined["time_step_s"]) == (0.7, 0.35)
    # the EDGE the run records is the one the mesh was MEASURED at, so the
    # granularity the run is judged on and the step it is solved at are one fact
    assert (asked["mesh_size_m"], refined["mesh_size_m"]) == (14.0, 7.0)


@pytest.mark.asyncio
async def test_the_server_facts_carry_what_only_the_server_measured(settle):
    """A fact re-derived in the container is a second answer that can disagree
    with the first, so the worker copies these into its metrics verbatim.
    ``result_slf`` is one of them: the deck states the results file."""
    out = await settle(mesh=_mesh_record(min_edge_m=8.0))
    assert out["server_facts"] == {
        "utm_epsg": 32610, "bbox": [-124.2, 40.4, -124.0, 40.6],
        "npoin": 539, "nelem": 902, "mesh_size_m": 8.0, "name": "coweeta_creek",
        "duration_s": 3600.0, "time_step_s": 0.4,
        "result_slf": "r2d_rog.slf", "bed_source": "cop-dem-glo-30"}


@pytest.mark.asyncio
async def test_the_mesh_travels_under_the_names_the_deck_states(settle):
    """What the worker is handed is the geometry pair the deck's own GEOMETRY /
    BOUNDARY CONDITIONS statements name, never a name the settle invented."""
    out = await settle(mesh=_mesh_record(min_edge_m=8.0))
    staged = {row["dest"]: row["gs_uri"] for row in out["mesh_inputs"]}
    assert staged == {"rog.slf": "s3://m/M01/domain.slf",
                      "rog.cli": "s3://m/M01/domain.cli"}


@pytest.mark.asyncio
async def test_a_mesh_record_with_no_topology_refuses_rather_than_remeshing(settle):
    with pytest.raises(TelemacError) as excinfo:
        await settle(mesh=_mesh_record(min_edge_m=8.0, topology_uri=None))
    assert excinfo.value.error_code == "TELEMAC_MESH_NOT_ACCEPTED"


@pytest.mark.asyncio
async def test_the_stood_in_mesh_record_is_shaped_like_a_real_builds(monkeypatch):
    """The fixture is measured against the ONE writer of a real mesh record.

    A fixture free to invent a key is a second product with its own shape, and
    the suite stays green while the live template dies."""
    from trid3nt_server.workflows.mesh import gate as gate_mod
    from trid3nt_server.workflows.mesh import session as session_mod
    from trid3nt_server.workflows.mesh import step as mesh_step

    record = _mesh_record(min_edge_m=8.0)

    async def _accepted(_session, **_kw):
        return record["artifact"]

    monkeypatch.setattr(session_mod, "MeshSession", lambda *a, **k: None)
    monkeypatch.setattr(gate_mod, "gate_mesh_build", _accepted)
    real = await mesh_step.build_declared_mesh(
        mesh={"mesher": "reg_grid", "kind": None, "extent": None,
              "resolution_m": 100.0, "ops": []})
    assert set(record) == set(real)
    assert record["provenance"] == dict(real["artifact"].provenance)


@pytest.mark.asyncio
async def test_a_closed_body_whose_level_slot_came_back_empty_refuses_by_name(
        settle, monkeypatch):
    """No edge water enters by, a bed on a datum, and the level the question
    asked for unfilled: the surface is unknown, so the run refuses naming that
    slot and what was asked rather than solving a dry basin."""
    from trid3nt_contracts.coverage import SourceChoice, SourceOption

    from trid3nt_server.workflows.runtime import journal

    monkeypatch.setattr(mesh_mod, "read_topology",
                        lambda _uri: {"roles": {},
                                      "liquid_boundary_order": [],
                                      "liquid_boundary_prescribes": []})
    token = journal.bind_choices()
    try:
        journal.slot_choice(SourceChoice(
            slot="level", need="water level series", picked="",
            rows=[SourceOption(fetcher="fetch_usbr_hydromet", kind="measured",
                               excluded="no station within reach")],
            sentence="nothing measured this water's level here."))
        with pytest.raises(TelemacError) as excinfo:
            await settle(mesh=_mesh_record(min_edge_m=14.0))
    finally:
        journal.drain_choices(token)
    assert excinfo.value.error_code == "TELEMAC_LEVEL_UNMEASURED"
    assert "'level'" in str(excinfo.value)
    assert "fetch_usbr_hydromet" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_closed_body_that_asked_for_no_level_opens_the_way_its_deck_says(
        settle, monkeypatch):
    """A question declaring no level slot - rain falling on dry ground - states
    no water and is not refused for stating none."""
    monkeypatch.setattr(mesh_mod, "read_topology",
                        lambda _uri: {"roles": {},
                                      "liquid_boundary_order": [],
                                      "liquid_boundary_prescribes": []})
    out = await settle(mesh=_mesh_record(min_edge_m=14.0))
    assert out["level_m"] is None and out["opening"] is None
