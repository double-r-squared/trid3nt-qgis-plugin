"""The reach family authors its deck on an ACCEPTED mesh, and says so.

Offline. The mesh is no longer a side effect of solving: a mesh step opens a
session over the template's declaration, the accepted topology is staged into the
solve's run directory, and the deck's timestep AND recorded edge follow the edge
the mesh was BUILT at rather than the edge that was asked for.

What is pinned here:

  1. DECK BYTE-PARITY - the whole deck, field by field, against a dumper that
     restates it from the inputs rather than reading it back off the writer. The
     refactor moved the mesh OUT of the solve and changed nothing the deck says;
     both reach shapes (a dye tracer and a DO sag) are checked.
  2. The dt SEAM HAS A READER - a mesh artifact measured finer than the ask
     tightens the deck's timestep, and one measured at the ask leaves it alone.
  3. The CASE the worker is handed - which engine, which authored deck, which
     results are the success convention, and the facts the server echoes - the
     outflow stage DERIVED as a normal depth over the reach the accepted mesh
     measures at its declared roles, and the refusals a mesh record missing its
     topology or its bed raises rather than letting the worker mesh one of its
     own.
"""

from __future__ import annotations

import pytest

from trid3nt_server.workflows.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.telemac.steps import deck as deck_mod
from trid3nt_server.workflows.telemac.steps.errors import TelemacDyeScenarioError

_REACH = {"name": "Eel River", "slug": "eel", "lon": -124.1, "lat": 40.5,
          "bbox": (-124.2, 40.4, -124.0, 40.6)}
_SEED = {"lon": -124.1, "lat": 40.5, "source": "flowline"}
_CARRIER = {"m3s": 12.0, "basis": "fetched", "note": "NWM 12 m3/s"}
_DO_SAG = {"effluent_bod_mgl": 250.0, "effluent_q_m3s": 1.0,
           "effluent_do_mgl": 2.0, "upstream_do_mgl": 8.0, "saturation_mgl": 9.0,
           "water_temp_c": 20.0, "k1_per_day": 0.3, "k2_per_day": 0.5,
           "k2_formula": 0, "standard_mgl": 5.0}

#: THE reach the chain declared - the one line the section was cut between, the
#: mesh was built over and the deck reads. Inline, because what the writer does
#: with it is measured through the centerline reader stood in below.
_CENTERLINE = {"type": "LineString",
               "coordinates": [[-124.13, 40.50], [-124.07, 40.50]]}

#: The sheet both parity cases are written from. Held apart from the expected
#: deck so the dumper below restates the deck from the ASK rather than from
#: anything the writer produced.
_SHEET = {"reach_length_km": 6.0, "sim_duration_s": 3600.0}


#: The BOUNDARY the stood-in mesh declares, and the bed it carries at it. The
#: four nodes below are the stood-in triangulation's own: the two western ones
#: are the inflow cap, the two eastern ones the outflow cap, and the deck's
#: outflow stage is the median bed over each.
_ROLES = {"inflow": [0, 3], "outflow": [1, 2]}
_NODE_BED = [12.0, 10.2, 10.2, 12.0]


def _mesh_record(*, min_edge_m: float | None = None,
                 topology_uri: str | None = "s3://m/M01/mesh_topology.json") -> dict:
    """A mesh step's result, composed the way the mesh step composes a real one.

    Every derived field is READ off the artifact through the product's own
    readers, so this stand-in cannot report a measured edge its probes never
    held, or a provenance its artifact does not carry. A fixture free to invent
    a key is how a deck went on reading a probe no build had written.
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
def writer(monkeypatch, tmp_path):
    """``write_reach_deck`` with its world-reads stood in for.

    The AUTHORING is real: the decks are written into a temp run directory by the
    author this step calls, which is what makes the parity checks below statements
    about the run rather than about a stub.
    """
    import numpy as np

    from trid3nt_server.workflows.telemac import release_layer as rel_mod

    async def _publish(*_a, **_kw):
        return False

    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(rel_mod, "publish_release_point", _publish)
    monkeypatch.setattr(deck_mod, "read_topology",
                        lambda _uri: {
                            "roles": dict(_ROLES),
                            "liquid_boundary_order": ["outflow", "inflow"],
                            "liquid_boundary_prescribes": ["elevation",
                                                           "flowrate"]})
    monkeypatch.setattr(deck_mod, "read_centerline_utm",
                        lambda _src, _epsg, **_kw:
                            np.array([[0.0, 0.0], [6000.0, 0.0]]))
    # The derived release is settled against the ACCEPTED MESH's own cells, and
    # the outflow stage is measured over the bed those same nodes carry: two
    # triangles spanning the whole stood-in centerline, painted downstream, which
    # is a mesh that holds every station on it and states its own ground. The
    # deck reads the display face through its own binding and the release
    # containment reads it through the module's, so the stand-in stands at both.
    from trid3nt_server.workflows.mesh.shared import nodes as nodes_mod

    def _accepted_nodes(_uri, utm_epsg=None):
        return (np.array([[-10.0, -50.0], [6010.0, -50.0], [6010.0, 50.0],
                          [-10.0, 50.0]]),
                np.array([[0, 1, 2], [0, 2, 3]]), np.array(_NODE_BED), None)

    monkeypatch.setattr(nodes_mod, "read_accepted_mesh_nodes", _accepted_nodes)
    monkeypatch.setattr(deck_mod, "read_accepted_mesh_nodes", _accepted_nodes)
    monkeypatch.setattr(
        deck_mod, "_stage_authored",
        lambda _rundir, run_tag, names: [
            {"gs_uri": f"s3://cache/telemac/{run_tag}/{n}", "dest": n}
            for n in names])

    async def _write(**kwargs):
        return await deck_mod.write_reach_deck(centerline=_CENTERLINE, **kwargs)

    return _write


# --------------------------------------------------------------------------- #
# 1. Deck byte-parity: the dumper, then the writer against it.
# --------------------------------------------------------------------------- #
def _expected_deck(*, mesh_size_m: float, time_step_s: float,
                   do_sag: bool) -> dict:
    """The deck this sheet MEANS, restated from the ask.

    Independent of the writer on purpose: a parity check that read the writer's
    own output back would pass for any refactor, including one that changed what
    the deck says.
    """
    deck = {
        "name": "eel",
        "seed_lon": -124.1,
        "seed_lat": 40.5,
        "nav_direction": "DM",
        "distance_km": _SHEET["reach_length_km"],
        "bed_source": "cop-dem-glo-30",
        "mesh_size_m": mesh_size_m,
        "time_step_s": time_step_s,
        "dye_conc_mgl": 100.0,
        # The outfall sits at the TOP of the reach it seeded; a dye release walks
        # to whatever fraction the sheet asked for.
        "spill_frac": 0.02 if do_sag else 0.25,
        "pulse_window_s": 300.0,
        "source_q_m3s": 8.0,
        "inflow_q_m3s": _CARRIER["m3s"],
        "duration_s": _SHEET["sim_duration_s"],
    }
    if do_sag:
        deck.update({
            "substance_class": "do_sag",
            "decay_law": 1,
            "decay_coef": 2.0,
            "do_sag_effluent_bod_mgl": _DO_SAG["effluent_bod_mgl"],
            "do_sag_effluent_q_m3s": _DO_SAG["effluent_q_m3s"],
            "do_sag_effluent_do_mgl": _DO_SAG["effluent_do_mgl"],
            "do_sag_upstream_do_mgl": _DO_SAG["upstream_do_mgl"],
            "do_sat_mgl": _DO_SAG["saturation_mgl"],
            "do_water_temp_c": _DO_SAG["water_temp_c"],
            "do_k1_per_day": _DO_SAG["k1_per_day"],
            "do_k2_per_day": _DO_SAG["k2_per_day"],
            "do_k2_formula": _DO_SAG["k2_formula"],
            "do_standard_mgl": _DO_SAG["standard_mgl"],
        })
    return deck


@pytest.mark.asyncio
@pytest.mark.parametrize("substance,do_sag_config,do_sag", [
    ("dye", None, False),
    ("sewage", _DO_SAG, True),
])
async def test_the_deck_is_byte_identical_on_an_accepted_mesh(
        writer, substance, do_sag_config, do_sag):
    """Routing the mesh through a session changed the deck in NO field.

    The artifact reports the edge the ask named, so the mesh contributes nothing
    to the timestep here and the deck is the one this sheet always wrote.
    """
    out = await writer(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=14.0),
                       carrier_discharge=_CARRIER, substance=substance,
                       do_sag_config=do_sag_config, **_SHEET)
    assert out["deck"] == _expected_deck(mesh_size_m=14.0, time_step_s=0.7,
                                         do_sag=do_sag)


@pytest.mark.asyncio
async def test_a_run_with_no_measured_mesh_writes_the_same_deck(writer):
    """No probes to read -> the requested edge decides dt, exactly as before."""
    out = await writer(reach=_REACH, seed=_SEED, mesh=_mesh_record(),
                       carrier_discharge=_CARRIER, substance="dye", **_SHEET)
    assert out["deck"] == _expected_deck(mesh_size_m=14.0, time_step_s=0.7,
                                         do_sag=False)


# --------------------------------------------------------------------------- #
# 2. The dt seam has a reader.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_refined_mesh_tightens_the_deck_timestep(writer):
    """Refine at the gate and the deck's dt follows the mesh, not the ask.

    The stability criterion is a statement about the mesh that exists. A mesh
    measured at 7 m under a 14 m ask is twice as fine, and a deck that kept
    quoting the ask would run it at twice the stable step.
    """
    asked = await writer(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=14.0),
                         carrier_discharge=_CARRIER, substance="dye", **_SHEET)
    refined = await writer(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=7.0),
                           carrier_discharge=_CARRIER, substance="dye", **_SHEET)
    assert asked["deck"]["time_step_s"] == 0.7
    assert refined["deck"]["time_step_s"] == 0.35
    # DS-3: the EDGE the deck records is the one the mesh was MEASURED at, so the
    # granularity the run is judged on and the step it is solved at are one fact.
    assert asked["deck"]["mesh_size_m"] == 14.0
    assert refined["deck"]["mesh_size_m"] == 7.0


# --------------------------------------------------------------------------- #
# 3. The CASE, and the refusals an unaccepted mesh raises.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_case_names_the_engine_the_authored_deck_and_the_results(writer):
    out = await writer(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=8.0),
                       carrier_discharge=_CARRIER, substance="dye", **_SHEET)
    assert out["case"]["module"] == "telemac2d"
    assert out["case"]["steering"] == "t2d_river.cas"
    assert out["case"]["results"] == ["r2d_river.slf", "restart_river.slf"]
    assert out["case"]["family"] == "reach"
    assert out["mesh_id"] == "M01"


@pytest.mark.asyncio
async def test_the_echo_carries_what_only_the_server_measured(writer):
    """A fact re-derived in the container is a second answer that can disagree
    with the first, so the worker copies these into its metrics verbatim.

    ``result_slf`` is one of them: the author wrote the deck's RESULTS FILE
    statement, so the name is the server's and the container measures the file it
    names rather than deciding which file the run produced.
    """
    out = await writer(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=8.0),
                       carrier_discharge=_CARRIER, substance="dye", **_SHEET)
    assert out["case"]["echo"] == {
        "utm_epsg": 32610, "bbox": [-124.2, 40.4, -124.0, 40.6],
        "npoin": 539, "nelem": 902, "mesh_size_m": 8.0,
        "result_slf": "r2d_river.slf", "bed_source": "cop-dem-glo-30"}
    assert out["case"]["echo"]["result_slf"] in out["case"]["results"]


@pytest.mark.asyncio
async def test_the_mesh_travels_under_the_names_the_deck_states(writer):
    """The npz stopped travelling: what the worker is handed is the geometry pair
    the deck's own GEOMETRY / BOUNDARY CONDITIONS lines name."""
    out = await writer(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=8.0),
                       carrier_discharge=_CARRIER, substance="dye", **_SHEET)
    staged = {row["dest"]: row["gs_uri"] for row in out["inputs"]}
    assert staged["river.slf"] == "s3://m/M01/river.slf"
    assert staged["river.cli"] == "s3://m/M01/river.cli"
    assert "t2d_river.cas" in staged
    assert not [d for d in staged if d.endswith(".npz")]


@pytest.mark.asyncio
async def test_the_deck_prescribes_in_the_order_the_mesh_MEASURED(writer, tmp_path):
    """The contour walk does not start at the inflow. A deck authored inflow-first
    would put the discharge on the downstream cap and drive the reach backwards."""
    out = await writer(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=8.0),
                       carrier_discharge=_CARRIER, substance="dye", **_SHEET)
    cas = (tmp_path / f"telemac-{out['run_tag']}" / "t2d_river.cas").read_text()
    flowrates = next(ln for ln in cas.splitlines()
                     if ln.startswith("PRESCRIBED FLOWRATES"))
    assert flowrates.split("=")[1].strip() == "0.0;12.0"


@pytest.mark.asyncio
async def test_a_mesh_record_with_no_topology_refuses_rather_than_remeshing(writer):
    with pytest.raises(TelemacDyeScenarioError) as excinfo:
        await writer(reach=_REACH, seed=_SEED,
                     mesh=_mesh_record(min_edge_m=8.0, topology_uri=None),
                     carrier_discharge=_CARRIER, substance="dye", **_SHEET)
    assert excinfo.value.error_code == "TELEMAC_MESH_NOT_ACCEPTED"


@pytest.mark.asyncio
async def test_the_outflow_stage_is_a_normal_depth_over_the_MEASURED_reach(
        writer, tmp_path):
    """The stage stands on the ground the geometry file carries, at the depth
    that ground conveys this run's own flow at.

    Everything in it is measured off the accepted mesh: the outflow cap's median
    bed is 10.2 m, its face cuts 100 m of that bed, and the reach falls 1.8 m
    over the 6 km the mesh was built along. A stage read from anything else - a
    plane fitted beside the mesh, a declared depth restated from the ask - would
    put the water somewhere the solve's own bathymetry does not agree with, so
    the deck states every input and the number can be checked against the mesh.
    """
    out = await writer(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=8.0),
                       carrier_discharge=_CARRIER, substance="dye", **_SHEET)
    cas = (tmp_path / f"telemac-{out['run_tag']}" / "t2d_river.cas").read_text()
    elevations = next(ln for ln in cas.splitlines()
                      if ln.startswith("PRESCRIBED ELEVATIONS"))
    assert elevations.split("=")[1].strip() == "10.593;0.0"
    assert "/  Measured bed: inflow 12.000 m, outflow 10.200 m" in cas
    assert "/  Friction slope 0.000300 = 1.800 m over 6000 m" in cas
    assert "/  outflow stage = 10.593 m: normal depth 0.393 m over the" in cas
    assert "/  measured outflow section for 12 m3/s at Strickler 33" in cas


@pytest.mark.asyncio
async def test_a_mesh_with_no_painted_bed_refuses_rather_than_inventing_a_stage(
        writer, monkeypatch):
    """A bedless mesh has no ground for a stage to be measured from."""
    monkeypatch.setattr(deck_mod, "read_accepted_mesh_nodes",
                        lambda _uri, utm_epsg=None: (None, None, None, None))
    with pytest.raises(TelemacDyeScenarioError) as excinfo:
        await writer(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=8.0),
                     carrier_discharge=_CARRIER, substance="dye", **_SHEET)
    assert excinfo.value.error_code == "TELEMAC_MESH_BED_UNMEASURED"


@pytest.mark.asyncio
async def test_the_stood_in_mesh_record_is_shaped_like_a_real_builds(monkeypatch):
    """The fixture is measured against the ONE writer of a real mesh record.

    A fixture free to invent a key is not a smaller version of the product - it
    is a second product with its own shape, and the suite stays green while the
    live template dies. That is how the deck went on reading a probe no build
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
    # deck's bed_source would be read from a record nothing wrote.
    assert record["provenance"] == dict(record["artifact"].provenance)
