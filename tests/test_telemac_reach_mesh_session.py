"""The reach family authors its run on an ACCEPTED mesh, and says so.

Offline. The mesh is no longer a side effect of solving: a mesh step opens a
session over the template's declaration, the accepted topology is staged into the
solve's run directory, and the sheet's timestep AND recorded edge follow the edge
the mesh was BUILT at rather than the edge that was asked for.

What is pinned here:

  1. WHAT THE MESH MEASURED, field by field, against a dumper that restates it
     from the inputs rather than reading it back off the settle. The refactor
     moved the mesh OUT of the solve and changed nothing the run states; both
     reach shapes (a mid-reach release and a top-of-reach outfall) are checked.
  2. The dt SEAM HAS A READER - a mesh artifact measured finer than the ask
     tightens the sheet's timestep, and one measured at the ask leaves it alone.
  3. What the worker is handed - the facts only the server measured, the mesh
     under the names the deck states, the outflow stage DERIVED as a normal depth
     over the reach the accepted mesh measures at its declared roles, and the
     refusals a mesh record missing its topology or its bed raises rather than
     letting the worker mesh one of its own.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.telemac.authoring import assembler as asm_mod
from trid3nt_server.workflows.telemac.helpers.errors import TelemacDyeScenarioError

_REACH = {"name": "Eel River", "slug": "eel", "lon": -124.1, "lat": 40.5,
          "bbox": (-124.2, 40.4, -124.0, 40.6)}
_SEED = {"lon": -124.1, "lat": 40.5, "source": "flowline"}
_CARRIER = {"m3s": 12.0, "basis": "fetched", "note": "NWM 12 m3/s"}
_DO_SAG = {"effluent_bod_mgl": 250.0, "effluent_q_m3s": 1.0,
           "effluent_do_mgl": 2.0, "upstream_do_mgl": 8.0, "saturation_mgl": 9.0,
           "water_temp_c": 20.0, "k1_per_day": 0.3, "k2_per_day": 0.5,
           "k2_formula": 0, "standard_mgl": 5.0}

#: THE reach the chain declared - the one line the section was cut between, the
#: mesh was built over and the author reads. Inline, because what the writer does
#: with it is measured through the centerline reader stood in below.
_CENTERLINE = {"type": "LineString",
               "coordinates": [[-124.13, 40.50], [-124.07, 40.50]]}

#: The ask both parity cases are settled from. Held apart from the expected
#: record so the dumper below restates it from the ASK rather than from anything
#: the settle produced. The reach LENGTH is the navigate's, not the settle's:
#: what reaches this seam is the accepted mesh the chain already cut.
_SHEET = {"sim_duration_s": 3600.0, "mesh_resolution_m": 14.0}


#: The BOUNDARY the stood-in mesh declares, and the bed it carries at it. The
#: four nodes below are the stood-in triangulation's own: the two western ones
#: are the inflow cap, the two eastern ones the outflow cap, and the run's
#: outflow stage is the median bed over each.
_ROLES = {"inflow": [0, 3], "outflow": [1, 2]}
_NODE_BED = [12.0, 10.2, 10.2, 12.0]


def _mesh_record(*, min_edge_m: float | None = None,
                 topology_uri: str | None = "s3://m/M01/mesh_topology.json") -> dict:
    """A mesh step's result, composed the way the mesh step composes a real one.

    Every derived field is READ off the artifact through the product's own
    readers, so this stand-in cannot report a measured edge its probes never
    held, or a provenance its artifact does not carry. A fixture free to invent
    a key is how an author went on reading a probe no build had written.
    """
    from trid3nt_server.workflows.mesh.artifact import measured_min_edge_m

    probes = ({"edge_length_m": {"min": float(min_edge_m), "max": 40.0,
                                 "mean": 20.0}}
              if min_edge_m is not None else {})
    artifact = MeshArtifact(
        mesh_id="M01", name="Eel River reach", mode="om2d",
        display_uri="s3://m/M01/mesh.2dm", slf_uri="s3://m/M01/river.slf",
        cli_uri="s3://m/M01/river.cli", topology_uri=topology_uri,
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
    """``assemble_reach`` with its world-reads stood in for.

    The AUTHORING is real: the files are written into a temp run directory by the
    author this step calls, which is what makes the parity checks below statements
    about the run rather than about a stub.
    """
    import numpy as np

    from trid3nt_server.workflows.telemac import release_layer as rel_mod

    async def _publish(*_a, **_kw):
        return False

    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(rel_mod, "publish_release_point", _publish)
    monkeypatch.setattr(asm_mod, "read_topology",
                        lambda _uri: {
                            "roles": dict(_ROLES),
                            "liquid_boundary_order": ["outflow", "inflow"],
                            "liquid_boundary_prescribes": ["elevation",
                                                           "flowrate"]})
    monkeypatch.setattr(asm_mod, "read_centerline_utm",
                        lambda _src, _epsg, **_kw:
                            np.array([[0.0, 0.0], [6000.0, 0.0]]))
    # The derived release is settled against the ACCEPTED MESH's own cells, and
    # the outflow stage is measured over the bed those same nodes carry: two
    # triangles spanning the whole stood-in centerline, painted downstream, which
    # is a mesh that holds every station on it and states its own ground. The
    # author reads the display face through its own binding and the release
    # containment reads it through the module's, so the stand-in stands at both.
    from trid3nt_server.workflows.mesh.shared import nodes as nodes_mod

    def _accepted_nodes(_uri, utm_epsg=None):
        return (np.array([[-10.0, -50.0], [6010.0, -50.0], [6010.0, 50.0],
                          [-10.0, 50.0]]),
                np.array([[0, 1, 2], [0, 2, 3]]), np.array(_NODE_BED), None)

    monkeypatch.setattr(nodes_mod, "read_accepted_mesh_nodes", _accepted_nodes)
    monkeypatch.setattr(asm_mod, "read_accepted_mesh_nodes", _accepted_nodes)
    monkeypatch.setattr(
        asm_mod, "_upload_authored",
        lambda _rundir, run_tag, names, prefix: [
            {"gs_uri": f"s3://cache/{prefix}/{run_tag}/{n}", "dest": n}
            for n in names])
    monkeypatch.setattr(
        asm_mod, "_write_manifest",
        lambda case, run_tag, **_kw: f"s3://cache/telemac/{run_tag}/manifest.json")

    async def _settle(**kwargs):
        return await asm_mod.settle_reach(centerline=_CENTERLINE,
                                          reach_polygon=None, **kwargs)

    return _settle


# --------------------------------------------------------------------------- #
# 1. What the mesh measured: the dumper, then the settle against it.
# --------------------------------------------------------------------------- #
def _expected_settled(*, mesh_size_m: float, time_step_s: float,
                      do_sag: bool) -> dict:
    """What this ask MEANS, restated from the ask.

    Independent of the settle on purpose: a parity check that read its own
    output back would pass for any refactor, including one that changed what the
    run states.
    """
    return {
        "name": "eel",
        "title": "eel REACH",
        "seed_lon": -124.1,
        "seed_lat": 40.5,
        "bed_source": "cop-dem-glo-30",
        "mesh_size_m": mesh_size_m,
        "time_step_s": time_step_s,
        # The outfall sits at the TOP of the reach it seeded; a mid-reach release
        # walks to whatever fraction the ask stated.
        "spill_fraction": 0.02 if do_sag else 0.25,
        "inflow_q_m3s": _CARRIER["m3s"],
        "duration_s": _SHEET["sim_duration_s"],
        "friction_law": 3,
        "friction_coefficient": 33.0,
        "graphic_period": 200,
        "liquid_boundary_order": ["outflow", "inflow"],
        "liquid_boundary_prescribes": ["elevation", "flowrate"],
    }


def _measured(settled: dict) -> dict:
    return {key: settled[key] for key in _expected_settled(
        mesh_size_m=0.0, time_step_s=0.0, do_sag=False)}


@pytest.mark.asyncio
@pytest.mark.parametrize("spill_fraction,do_sag", [(0.25, False), (0.02, True)])
async def test_what_the_run_measures_is_unchanged_on_an_accepted_mesh(
        settle, spill_fraction, do_sag):
    """Routing the mesh through a session changed what the run measures in NO
    field. The artifact reports the edge the ask named, so the mesh contributes
    nothing to the timestep here."""
    out = await settle(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=14.0),
                       carrier_discharge=_CARRIER, spill_fraction=spill_fraction,
                       marker_label="Outfall" if do_sag else "Release point",
                       **_SHEET)
    assert _measured(out) == _expected_settled(mesh_size_m=14.0, time_step_s=0.7,
                                               do_sag=do_sag)


@pytest.mark.asyncio
async def test_a_run_with_no_measured_mesh_measures_the_same_reach(settle):
    """No probes to read -> the requested edge decides dt, exactly as before."""
    out = await settle(reach=_REACH, seed=_SEED, mesh=_mesh_record(),
                       carrier_discharge=_CARRIER, **_SHEET)
    assert _measured(out) == _expected_settled(mesh_size_m=14.0, time_step_s=0.7,
                                               do_sag=False)


# --------------------------------------------------------------------------- #
# 2. The dt seam has a reader.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_refined_mesh_tightens_the_run_timestep(settle):
    """Refine at the gate and the run's dt follows the mesh, not the ask.

    The stability criterion is a statement about the mesh that exists. A mesh
    measured at 7 m under a 14 m ask is twice as fine, and a run that kept
    quoting the ask would run it at twice the stable step.
    """
    asked = await settle(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=14.0),
                         carrier_discharge=_CARRIER, **_SHEET)
    refined = await settle(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=7.0),
                           carrier_discharge=_CARRIER, **_SHEET)
    assert asked["time_step_s"] == 0.7
    assert refined["time_step_s"] == 0.35
    # DS-3: the EDGE the run records is the one the mesh was MEASURED at, so the
    # granularity the run is judged on and the step it is solved at are one fact.
    assert asked["mesh_size_m"] == 14.0
    assert refined["mesh_size_m"] == 7.0


# --------------------------------------------------------------------------- #
# 3. What the worker is handed, and the refusals an unaccepted mesh raises.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_server_facts_carry_what_only_the_server_measured(settle):
    """A fact re-derived in the container is a second answer that can disagree
    with the first, so the worker copies these into its metrics verbatim.

    ``result_slf`` is one of them: the deck states the RESULTS FILE, so the name
    is the server's and the container measures the file it names rather than
    deciding which file the run produced.
    """
    out = await settle(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=8.0),
                       carrier_discharge=_CARRIER, **_SHEET)
    assert out["server_facts"] == {
        "utm_epsg": 32610, "bbox": [-124.2, 40.4, -124.0, 40.6],
        "npoin": 539, "nelem": 902, "mesh_size_m": 8.0, "name": "eel",
        "duration_s": 3600.0, "time_step_s": 0.4,
        "result_slf": "r2d_river.slf", "bed_source": "cop-dem-glo-30"}


@pytest.mark.asyncio
async def test_the_mesh_travels_under_the_names_the_deck_states(settle):
    """The npz stopped travelling: what the worker is handed is the geometry pair
    the deck's own GEOMETRY / BOUNDARY CONDITIONS statements name."""
    out = await settle(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=8.0),
                       carrier_discharge=_CARRIER, **_SHEET)
    staged = {row["dest"]: row["gs_uri"] for row in out["mesh_inputs"]}
    assert staged == {"river.slf": "s3://m/M01/river.slf",
                      "river.cli": "s3://m/M01/river.cli"}


@pytest.mark.asyncio
async def test_the_run_prescribes_in_the_order_the_mesh_MEASURED(settle):
    """The contour walk does not start at the inflow. A deck written inflow-first
    would put the discharge on the downstream cap and drive the reach backwards."""
    from trid3nt_server.workflows.telemac.modules import T2D
    from trid3nt_server.workflows.telemac.modules.telemac2d import Boundaries

    out = await settle(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=8.0),
                       carrier_discharge=_CARRIER, **_SHEET)
    slots, _files = T2D.COMPOSITES["boundaries"].expand(
        Boundaries(measured=out, tracers=[0.0]))
    assert slots["PRESCRIBED_FLOWRATES"] == [0.0, 12.0]


@pytest.mark.asyncio
async def test_a_mesh_record_with_no_topology_refuses_rather_than_remeshing(settle):
    with pytest.raises(TelemacDyeScenarioError) as excinfo:
        await settle(reach=_REACH, seed=_SEED,
                     mesh=_mesh_record(min_edge_m=8.0, topology_uri=None),
                     carrier_discharge=_CARRIER, **_SHEET)
    assert excinfo.value.error_code == "TELEMAC_MESH_NOT_ACCEPTED"


@pytest.mark.asyncio
async def test_the_outflow_stage_is_a_normal_depth_over_the_MEASURED_reach(settle):
    """The stage stands on the ground the geometry file carries, at the depth
    that ground conveys this run's own flow at.

    Everything in it is measured off the accepted mesh: the outflow cap's median
    bed is 10.2 m, its face cuts 100 m of that bed, and the reach falls 1.8 m
    over the 6 km the mesh was built along. A stage read from anything else - a
    plane fitted beside the mesh, a declared depth restated from the ask - would
    put the water somewhere the solve's own bathymetry does not agree with, so
    the run states every input the number was derived from.
    """
    out = await settle(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=8.0),
                       carrier_discharge=_CARRIER, **_SHEET)
    assert out["outflow_stage_m"] == 10.593
    assert out["depth_m"] == 0.393
    assert out["normal"] == {
        "stage_m": 10.593185, "depth_m": 0.393185, "slope": 0.0003,
        "drop_m": 1.8, "length_m": 6000.0, "law": "Strickler",
        "coefficient": 33.0, "q_m3s": 12.0}


@pytest.mark.asyncio
async def test_a_mesh_with_no_painted_bed_refuses_rather_than_inventing_a_stage(
        settle, monkeypatch):
    """A bedless mesh has no ground for a stage to be measured from."""
    monkeypatch.setattr(asm_mod, "read_accepted_mesh_nodes",
                        lambda _uri, utm_epsg=None: (None, None, None, None))
    with pytest.raises(TelemacDyeScenarioError) as excinfo:
        await settle(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=8.0),
                     carrier_discharge=_CARRIER, **_SHEET)
    assert excinfo.value.error_code == "TELEMAC_MESH_BED_UNMEASURED"


@pytest.mark.asyncio
async def test_the_stood_in_mesh_record_is_shaped_like_a_real_builds(monkeypatch):
    """The fixture is measured against the ONE writer of a real mesh record.

    A fixture free to invent a key is not a smaller version of the product - it
    is a second product with its own shape, and the suite stays green while the
    live template dies. That is how an author went on reading a probe no build
    had written once its writer was deleted, so the stand-in's keys are read off
    the mesh step's own return rather than typed out beside it.
    """
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
    # The record's provenance IS the artifact's. A stand-in that fills one and
    # leaves the other empty is a mesh no build could have produced, and the
    # sheet's bed_source would be read from a record nothing wrote.
    assert record["provenance"] == dict(record["artifact"].provenance)
