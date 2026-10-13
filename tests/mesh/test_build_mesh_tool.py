"""Offline tests for the mesh router, the mesh session and the reg_grid mesher.

The PURE surfaces, with no container, no object store and no live session: the
router's typed refusals, what a supplied mesh's row takes and refuses,
the laziness of a declared recipe, the journal's replay determinism, the reset
back to the declaration, and the hand-edit record that refuses to replay."""

from __future__ import annotations

import dataclasses
import json

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.tools.mesh.meshers import MeshToolError, get_mesher
from trid3nt_server.render.mesh_display import write_2dm
from trid3nt_server.tools.mesh.session import (
    MeshSession,
    mesh_digest,
    replay_recipe,
)
from trid3nt_server.inputs.mesh import mesh as supplied_mesh
from trid3nt_server.tools.mesh.tool import (
    MeshRecipe,
    mesh_op,
    tool,
)
from seated import row_inputs

_AOI = (-83.50, 35.00, -83.40, 35.09)

def _recipe(**over):
    ask = {"mesher": "reg_grid", "kind": "structured_grid",
           "extent": _AOI, "resolution_m": 400.0}
    ask.update(over)
    return tool.build_mesh(**ask)


def _artifact(**over) -> MeshArtifact:
    base = dict(
        mesh_id="01MESH", name="Coweeta watershed", mode="reg_grid",
        display_uri="s3://cache/mesh/01MESH/mesh.2dm", utm_epsg=32617,
        crs_authid="EPSG:32617", has_bathymetry=True, node_count=4956,
        element_count=9727, bbox=_AOI,
        # A built mesh records the RECIPE it came from, and the KIND on that
        # record is what the resolution door checks a run's declaration against.
        provenance={"recipe": {"mesher": "reg_grid", "kind": "structured_grid"}})
    base.update(over)
    return MeshArtifact(**base)


def test_build_mesh_registered():
    rt = TOOL_REGISTRY.get("build_mesh")
    assert rt is not None
    assert rt.metadata.cacheable is False
    assert rt.metadata.ttl_class == "live-no-cache"
    assert rt.metadata.tier == "general"


def test_reg_grid_conforms_with_a_near_empty_default_recipe():
    """Same surface as every mesher, and the smallest possible registration."""
    mesher = get_mesher("reg_grid")
    assert mesher.kinds == ("structured_grid",)
    assert mesher.default_ops == ()
    assert mesher.namespaces == ()


def test_build_mesh_surfaces_in_top8():
    """The router must be reachable from its own corpus phrasings."""
    from pathlib import Path

    import yaml

    import trid3nt_server.tools as t
    from trid3nt_server.tools.search.search_tools import search_tools as dd
    from trid3nt_server.tools.search.tool_retrieval import retrieve_visible_tools

    dd._get_index()
    corpus_path = Path(t.__file__).resolve().parent / "mesh" / "corpus.yaml"
    queries = (yaml.safe_load(corpus_path.read_text()) or {})["build_mesh"]
    assert queries
    assert dd._compose_corpus_from_tree()["build_mesh"] == queries
    assert any("build_mesh" in retrieve_visible_tools(q, None, 8) for q in queries), (
        "build_mesh surfaces in NO top-8 for any of its corpus queries")


def test_an_unknown_mesher_refuses_naming_the_roster():
    with pytest.raises(MeshToolError) as excinfo:
        tool.build_mesh(mesher="no_such_mesher")
    assert excinfo.value.error_code == "MESH_UNKNOWN_MESHER"
    assert "reg_grid" in str(excinfo.value)


def test_a_kind_the_mesher_does_not_make_refuses_by_name():
    with pytest.raises(MeshToolError) as excinfo:
        tool.build_mesh(mesher="reg_grid", kind="unstructured_tri", extent=_AOI,
                        resolution_m=400.0)
    assert excinfo.value.error_code == "MESH_KIND_UNSUPPORTED"
    assert "structured_grid" in str(excinfo.value)


def test_the_kind_defaults_to_the_one_this_mesher_makes():
    assert _recipe(kind=None).kind == "structured_grid"


def test_engine_vocabulary_is_not_a_param_of_the_generalization():
    """``bed=`` and ``boundaries=`` died as params; they are ops."""
    for word in ("bed", "boundaries", "refine"):
        with pytest.raises(TypeError):
            tool.build_mesh(mesher="reg_grid", extent=_AOI, resolution_m=400.0,
                            **{word: {}})


def test_ops_that_are_not_recipe_entries_refuse():
    with pytest.raises(MeshToolError) as excinfo:
        tool.build_mesh(mesher="reg_grid", extent=_AOI, resolution_m=400.0,
                        ops=["set_bed"])
    assert excinfo.value.error_code == "MESH_OPS_MALFORMED"


def _tri_artifact(**over) -> MeshArtifact:
    return _artifact(
        mode="om2d", name="Point Judith Harbor of Refuge",
        provenance={"recipe": {"mesher": "om2d", "kind": "unstructured_tri"}},
        **over)


def test_a_supplied_mesh_carrying_no_engine_file_is_taken_rather_than_judged():
    """A mesh is not unsolvable in the abstract: its row checks the kind and
    that a bed is painted on it, and what an engine needs staged is refused by
    that engine's author against the files it names."""
    bare = _artifact(engine_files={})
    assert supplied_mesh(bare, kinds=("structured_grid",))["artifact"] is bare


def test_a_supplied_mesh_with_no_readable_record_refuses(monkeypatch):
    from trid3nt_server.store import objects as storage

    monkeypatch.setattr(storage, "client", _FailingReader)
    with pytest.raises(MeshToolError) as excinfo:
        supplied_mesh("s3://cache/mesh/unknown/mesh.2dm")
    assert excinfo.value.error_code == "MESH_EXPLICIT_UNREADABLE"
    assert "no readable mesh artifact record" in str(excinfo.value)


class _FailingReader:
    """An object store with no sidecar under the asked-for key."""

    def get_object(self, **_kwargs):
        raise KeyError("no such object")


def test_a_supplied_mesh_of_another_kind_refuses_by_name():
    """A lattice is not what a triangle engine solves on, so it is refused at its
    row rather than trusted and narrated several steps later."""
    with pytest.raises(MeshToolError) as excinfo:
        supplied_mesh(_artifact(), kinds=("unstructured_tri",))
    assert excinfo.value.error_code == "MESH_KIND_MISMATCH"
    message = str(excinfo.value)
    assert "'unstructured_tri'" in message and "'structured_grid'" in message


def test_a_supplied_mesh_of_the_engines_kind_is_the_record_a_built_one_is():
    supplied = _tri_artifact()
    record = supplied_mesh(supplied, kinds=("unstructured_tri",))
    assert record["artifact"] is supplied
    assert record["engine_files"] == supplied.engine_files
    assert record["node_count"] == supplied.node_count


def test_a_supplied_mesh_that_states_no_kind_refuses():
    """Membership is not answerable about an unstated shape."""
    with pytest.raises(MeshToolError) as excinfo:
        supplied_mesh(_artifact(provenance={}), kinds=("unstructured_tri",))
    assert excinfo.value.error_code == "MESH_KIND_MISMATCH"
    assert "no recorded kind" in str(excinfo.value)


def test_every_template_of_the_engine_takes_a_supplied_mesh():
    from trid3nt_server.inputs.slots import MESH

    for name in ("artemis_harbor_agitation", "telemac_dye_release",
                 "telemac_rain_on_grid", "telemac3d_stratified_flow"):
        rows = row_inputs(TOOL_REGISTRY[name].fn.workflow)
        assert [row.coercion["kinds"] for row in rows if row.slot is MESH] == [
            ("unstructured_tri",)]


def test_a_recipe_builds_nothing(tmp_path):
    """A degenerate extent declares fine and only fails when a build is demanded."""
    recipe = tool.build_mesh(mesher="reg_grid", extent=(0.0, 0.0, 0.0, 0.0),
                             resolution_m=100.0)
    assert isinstance(recipe, MeshRecipe)
    with pytest.raises(ValueError):
        MeshSession(recipe, workdir=tmp_path).probes()


def test_editing_a_recipe_returns_a_new_frozen_one():
    first = _recipe()
    second = first.appending(mesh_op("set_bed", source="fetch_cudem"))
    third = second.appending(mesh_op("set_boundary_roles", outflow=[[0, 0], [1, 1]]))
    assert first.ops == ()
    assert [op.fn for op in third.ops] == ["set_bed", "set_boundary_roles"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.resolution_m = 10.0


def test_ops_are_altered_and_removed_by_index():
    recipe = _recipe(ops=[mesh_op("set_bed", source="a"),
                          mesh_op("set_bed", source="b")])
    altered = recipe.altering(1, mesh_op("set_bed", source="c"))
    assert [op.kwargs["source"] for op in altered.ops] == ["a", "c"]
    assert [op.kwargs["source"] for op in altered.without(0).ops] == ["c"]
    with pytest.raises(MeshToolError) as excinfo:
        recipe.without(5)
    assert excinfo.value.error_code == "MESH_OP_INDEX"


def test_the_journal_records_the_declaration_then_one_line_per_edit(tmp_path):
    session = MeshSession(_recipe(), workdir=tmp_path)
    session.set_params(resolution_m=250.0)
    # A roles op with no roles imposes nothing, so what this pins is the JOURNAL
    # rather than any mesher's world-reads.
    session.append_op(mesh_op("set_boundary_roles"))
    session.remove_op(0)
    lines = [json.loads(ln) for ln in
             session.recipe_path.read_text().splitlines() if ln.strip()]
    assert lines[0]["recipe"]["mesher"] == "reg_grid"
    assert lines[0]["recipe"]["resolution_m"] == 400.0
    assert [ln["event"] for ln in lines[1:]] == ["params", "append", "remove"]
    assert lines[-1]["recipe"]["ops"] == []
    assert lines[-1]["recipe"]["resolution_m"] == 250.0


def test_the_journal_replays_to_an_identical_mesh(tmp_path):
    session = MeshSession(_recipe(), workdir=tmp_path)
    session.set_params(resolution_m=250.0)
    replayed = replay_recipe(session.recipe_path)
    assert mesh_digest(replayed) == mesh_digest(session.mesh)


def test_the_same_recipe_builds_the_same_mesh_twice(tmp_path):
    one = MeshSession(_recipe(), workdir=tmp_path / "one")
    two = MeshSession(_recipe(), workdir=tmp_path / "two")
    assert mesh_digest(one.mesh) == mesh_digest(two.mesh)


def test_reset_puts_the_recipe_back_to_the_declaration(tmp_path):
    session = MeshSession(_recipe(), workdir=tmp_path)
    coarse = session.probes()["node_count"]
    session.set_params(resolution_m=150.0)
    assert session.probes()["node_count"] != coarse

    probes = session.reset()
    assert probes["node_count"] == coarse
    assert session.recipe == session.declared
    assert mesh_digest(replay_recipe(session.recipe_path)) == mesh_digest(session.mesh)


def test_a_hand_edit_is_recorded_flagged_and_refuses_to_replay(tmp_path):
    import numpy as np

    from trid3nt_server.tools.mesh.meshers import Mesh

    edited = tmp_path / "edited.2dm"
    edited.write_text(write_2dm(Mesh(
        points=np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]),
        cells=np.array([[0, 1, 2], [1, 3, 2]]), crs_authid="EPSG:4326")))

    session = MeshSession(_recipe(), workdir=tmp_path)
    session.adopt_layer(str(edited))
    assert session.mesh.element_count == 2
    assert session.regen_note is not None

    line = [json.loads(ln) for ln in
            session.recipe_path.read_text().splitlines() if ln.strip()][-1]
    assert line["event"] == "adopt"
    assert line["digest"].startswith("sha256:")
    assert line["source"] == str(edited)
    assert line["replayable"] is False

    with pytest.raises(MeshToolError) as excinfo:
        replay_recipe(session.recipe_path)
    assert excinfo.value.error_code == "MESH_RECIPE_NOT_REPLAYABLE"


def test_a_recipe_edit_after_a_hand_edit_refuses_rather_than_discarding_it(tmp_path):
    import numpy as np

    from trid3nt_server.tools.mesh.meshers import Mesh

    edited = tmp_path / "edited.2dm"
    edited.write_text(write_2dm(Mesh(
        points=np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]),
        cells=np.array([[0, 1, 2], [1, 3, 2]]), crs_authid="EPSG:4326")))
    session = MeshSession(_recipe(), workdir=tmp_path)
    session.adopt_layer(str(edited))

    with pytest.raises(MeshToolError) as excinfo:
        session.set_params(resolution_m=200.0)
    assert excinfo.value.error_code == "MESH_REGEN_WOULD_DISCARD_HAND_EDIT"
    # The one structured revert still gets out of it.
    session.reset()
    assert session.regen_note is None
    assert session.mesh.element_count > 2


def test_accept_freezes_the_recipe_as_the_artifacts_provenance(tmp_path):
    session = MeshSession(_recipe(), workdir=tmp_path, case_id="case-accept",
                          name="Coweeta lattice")
    art = session.accept()
    assert art.node_count == session.mesh.node_count
    assert art.element_count == session.mesh.element_count
    assert art.crs_authid == "EPSG:4326"
    assert art.has_bathymetry is False
    assert art.utm_epsg is None
    assert art.recipe_uri == str(session.recipe_path)
    assert art.provenance["recipe"] == session.recipe.to_json()
    assert art.provenance["recipe"]["kind"] == "structured_grid"


def test_snapshot_is_the_display_face(tmp_path):
    session = MeshSession(_recipe(), workdir=tmp_path, name="lattice")
    layer = session.snapshot()
    assert layer.layer_type == "mesh"
    assert layer.style == {"kind": "reference", "geometry": "line"}
    assert layer.uri.endswith("mesh.2dm")
    assert layer.crs_authid == "EPSG:4326"
    assert (tmp_path / "mesh.2dm").read_text().startswith("MESH2D")


def test_probes_measure_the_lattice_and_number_the_recipe(tmp_path):
    """What is measured is what the gate card quotes, and nothing beside it."""
    probes = MeshSession(_recipe(), workdir=tmp_path).probes()
    assert probes["boundary_loops"] == 1
    assert probes["min_angle_deg"] == pytest.approx(90.0, abs=1e-6)
    assert probes["edge_length_m"]["mean"] == pytest.approx(400.0, rel=0.05)
    assert probes["ops"] == []
    assert not {"area_km2", "nodes_per_cell", "boundary_nodes"} & set(probes)
    assert "histogram" not in probes["edge_length_m"]


def test_the_accepted_artifact_carries_what_was_measured_on_it(tmp_path):
    session = MeshSession(_recipe(), workdir=tmp_path)
    art = session.accept()
    assert art.probes["node_count"] == art.node_count
    assert art.probes["edge_length_m"]["min"] > 0.0


def test_the_measured_minimum_edge_is_read_off_the_artifact(tmp_path):
    from trid3nt_server.tools.mesh.artifact import measured_min_edge_m

    art = MeshSession(_recipe(), workdir=tmp_path).accept()
    assert measured_min_edge_m(art) == pytest.approx(
        art.probes["edge_length_m"]["min"])


@pytest.mark.parametrize("art", [
    None,
    _artifact(),                                        # never probed
    _artifact(probes={"node_count": 12}),               # probed, but cell-less
    _artifact(probes={"edge_length_m": {"min": 0.0}}),  # a degenerate measurement
])
def test_an_unmeasured_mesh_reports_no_minimum_edge(art):
    from trid3nt_server.tools.mesh.artifact import measured_min_edge_m

    assert measured_min_edge_m(art) is None


def test_the_timestep_follows_the_measured_edge_not_the_requested_one():
    """Gate-time refinement tightens dt without anybody restating the number.

    The ask stays coarse and the mesh BUILT is finer, so the CFL-safe step comes off
    the mesh; reading the ask hands the solver a step it cannot carry."""
    from trid3nt_server.workflows.telemac.helpers.time_step import suggest_time_step_s

    requested = suggest_time_step_s(40.0)
    refined = suggest_time_step_s(
        40.0, mesh=_artifact(probes={"edge_length_m": {"min": 8.0}}))
    assert requested == 1.0
    assert refined == pytest.approx(0.4)


def test_the_timestep_falls_back_to_the_ask_when_no_mesh_exists_yet():
    """An estimate made before any mesh exists has only the ask to go on."""
    from trid3nt_server.workflows.telemac.helpers.time_step import suggest_time_step_s

    assert suggest_time_step_s(10.0) == suggest_time_step_s(10.0, mesh=None)
    assert suggest_time_step_s(10.0, mesh=_artifact()) == 0.5


@pytest.mark.asyncio
async def test_a_bbox_at_the_door_becomes_the_recipes_extent(monkeypatch):
    from trid3nt_server.tools.mesh.meshers import MeshToolError

    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "test-cache")
    seen: dict = {}

    def _accept(self):
        seen["recipe"] = self.recipe
        raise MeshToolError("MESH_BUILD_FAILED", "stopped after validation")

    monkeypatch.setattr(MeshSession, "accept", _accept)
    with pytest.raises(MeshToolError):
        await TOOL_REGISTRY["build_mesh"].fn(
            mesher="reg_grid", bbox=(-75.8, 36.1, -75.7, 36.2), resolution_m=200.0,
            input_mode="auto")
    assert seen["recipe"].extent == (-75.8, 36.1, -75.7, 36.2)
    assert seen["recipe"].resolution_m == 200.0


@pytest.mark.asyncio
async def test_wire_ops_become_recipe_entries(monkeypatch):
    """The ops list off the wire is the same ordered program a template writes."""
    from trid3nt_server.tools.mesh.meshers import MeshToolError

    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "test-cache")
    seen: dict = {}

    def _accept(self):
        seen["recipe"] = self.recipe
        raise MeshToolError("MESH_BUILD_FAILED", "stopped after validation")

    monkeypatch.setattr(MeshSession, "accept", _accept)
    with pytest.raises(MeshToolError):
        await TOOL_REGISTRY["build_mesh"].fn(
            mesher="reg_grid", bbox=(-75.8, 36.1, -75.7, 36.2), resolution_m=200.0,
            ops=[{"fn": "set_bed", "source": "fetch_cudem",
                  "interp": "bilinear"}], input_mode="auto")
    ops = seen["recipe"].ops
    assert [op.fn for op in ops] == ["set_bed"]
    assert dict(ops[0].kwargs) == {"source": "fetch_cudem",
                                   "interp": "bilinear"}


@pytest.mark.asyncio
async def test_a_malformed_wire_op_refuses_naming_what_each_mesher_answers_to(
        monkeypatch):
    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "test-cache")
    with pytest.raises(MeshToolError) as excinfo:
        await TOOL_REGISTRY["build_mesh"].fn(
            mesher="reg_grid", bbox=(-75.8, 36.1, -75.7, 36.2), ops=["set_bed"])
    assert excinfo.value.error_code == "MESH_OPS_MALFORMED"
    assert "set_bed" in str(excinfo.value)


def test_a_lattice_refuses_a_polygon_domain_and_names_the_mesher_that_takes_one():
    """A regular grid IS an origin plus counts; masking it to a polygon is not."""
    recipe = tool.build_mesh(mesher="reg_grid",
                             extent={"type": "Polygon", "coordinates": []},
                             resolution_m=400.0)
    with pytest.raises(MeshToolError) as excinfo:
        get_mesher("reg_grid").build(recipe)
    assert excinfo.value.error_code == "MESH_POLYGON_DOMAIN_UNSUPPORTED"
    assert excinfo.value.escalation["overrides"]["mesher"] == "om2d"


