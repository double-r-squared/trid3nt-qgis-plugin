"""The reach family authors its deck on an ACCEPTED mesh, and says so.

Offline. The corridor mesh is no longer a side effect of solving: a mesh step
opens a session over the template's declaration, the accepted topology is staged
into the solve's run directory, and the deck's timestep follows the edge the mesh
was BUILT at rather than the edge that was asked for.

What is pinned here:

  1. DECK BYTE-PARITY - the whole deck, field by field, against a dumper that
     restates it from the inputs rather than reading it back off the writer. The
     refactor moved the mesh OUT of the solve and changed nothing the deck says;
     both reach shapes (a dye tracer and a DO sag) are checked.
  2. The dt SEAM HAS A READER - a mesh artifact measured finer than the ask
     tightens the deck's timestep, and one measured at the ask leaves it alone.
  3. The accepted topology is STAGED - and a mesh record that carries none
     refuses rather than letting the worker mesh one of its own.
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
_DO_SAG = {"bod_mgl": 20.0, "upstream_do_mgl": 8.0, "saturation_mgl": 9.0,
           "water_temp_c": 20.0, "k1_per_day": 0.3, "k2_per_day": 0.5,
           "k2_formula": 0, "standard_mgl": 5.0}

#: The sheet both parity cases are written from. Held apart from the expected
#: deck so the dumper below restates the deck from the ASK rather than from
#: anything the writer produced.
_SHEET = {"reach_length_km": 6.0, "channel_width_m": 60.0,
          "sim_duration_s": 3600.0}


def _mesh_record(*, min_edge_m: float | None = None,
                 topology_uri: str | None = "s3://m/M01/river_mesh.npz") -> dict:
    """A mesh step's result, with the probes an artifact would carry."""
    artifact = None
    if min_edge_m is not None:
        artifact = MeshArtifact(
            mesh_id="M01", name="Eel River corridor", mode="corridor_tin",
            display_uri="s3://m/M01/mesh.2dm", slf_uri="s3://m/M01/river.slf",
            crs_authid="EPSG:32610", has_bathymetry=False,
            node_count=539, element_count=902,
            bbox=(-124.2, 40.4, -124.0, 40.6),
            probes={"edge_length_m": {"min": float(min_edge_m), "max": 40.0,
                                      "mean": 20.0}})
    return {"artifact": artifact, "mesh_id": "M01",
            "slf_uri": "s3://m/M01/river.slf", "cli_uri": "s3://m/M01/river.cli",
            "topology_uri": topology_uri, "min_edge_m": min_edge_m}


@pytest.fixture()
def writer(monkeypatch):
    """``write_reach_deck`` with its two world-reads stood in for."""
    from trid3nt_server.workflows.telemac import release_layer as rel_mod

    async def _river(**_kw):
        return {
            "inputs": [{"gs_uri": "s3://c/c.geojson",
                        "dest": "river_centerline.geojson"}],
            "provenance": {"seed_lon": -124.1, "seed_lat": 40.5,
                           "seed_rung": "position-named-flowline",
                           "centerline_uri": "s3://c/centerline.geojson",
                           "centerline_sha256": "0" * 64,
                           "centerline_comids": [1],
                           "bed_source": "cop-dem-glo-30"},
        }

    async def _publish(*_a, **_kw):
        return False

    monkeypatch.setattr(deck_mod, "resolve_reach_river", _river)
    monkeypatch.setattr(rel_mod, "publish_release_point", _publish)
    return deck_mod.write_reach_deck


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
        "channel_width_m": _SHEET["channel_width_m"],
        "bank_source": "nhd_area",
        "bed_source": "cop-dem-glo-30",
        "mesh_size_m": mesh_size_m,
        "time_step_s": time_step_s,
        "dye_conc_mgl": 100.0,
        "spill_frac": 0.25,
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
            "do_sag_bod_mgl": _DO_SAG["bod_mgl"],
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
    # the EDGE the deck records is still the ask - what the mesh was built at is
    # the mesher's to answer for, and the two are different facts.
    assert refined["deck"]["mesh_size_m"] == asked["deck"]["mesh_size_m"] == 14.0


# --------------------------------------------------------------------------- #
# 3. The accepted topology is staged, or the run refuses.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_accepted_topology_is_staged_for_the_solve(writer):
    out = await writer(reach=_REACH, seed=_SEED, mesh=_mesh_record(min_edge_m=8.0),
                       carrier_discharge=_CARRIER, substance="dye", **_SHEET)
    assert {"gs_uri": "s3://m/M01/river_mesh.npz", "dest": "river_mesh.npz"} \
        in out["inputs"]
    assert out["mesh_id"] == "M01"


@pytest.mark.asyncio
async def test_a_mesh_record_with_no_topology_refuses_rather_than_remeshing(writer):
    with pytest.raises(TelemacDyeScenarioError) as excinfo:
        await writer(reach=_REACH, seed=_SEED,
                     mesh=_mesh_record(min_edge_m=8.0, topology_uri=None),
                     carrier_discharge=_CARRIER, substance="dye", **_SHEET)
    assert excinfo.value.error_code == "TELEMAC_MESH_NOT_ACCEPTED"


def test_the_mesh_only_run_brings_its_topology_back(monkeypatch):
    """The bundle a later solve adopts has to survive its own run directory."""
    import json

    import trid3nt_server.workflows.solver.solver as solver_mod

    captured: dict = {}

    class _S3:
        def put_object(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(solver_mod, "_get_s3_client", lambda: _S3())
    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "test-cache")
    deck_mod.stage_manifest({"name": "r"}, "RUNTAG", mesh_only=True)
    manifest = json.loads(captured["Body"])
    assert "river_mesh.npz" in manifest["outputs"]
