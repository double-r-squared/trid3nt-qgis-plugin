"""Offline tests for the mesh router, the mesh session and the reg_grid mesher.

The PURE surfaces, with no container, no object store and no live session: the
router's typed refusals, the explicit / discovered / declared resolution order,
the laziness of a declared recipe, the journal's replay determinism, the reset
back to the declaration, and the hand-edit record that honestly refuses to
replay.

ASCII only.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.workflows.mesh.artifact import (
    MeshArtifact,
    stash_mesh_artifact,
)
from trid3nt_server.workflows.mesh.meshers import MeshToolError, get_mesher
from trid3nt_server.emission.mesh_display import write_2dm
from trid3nt_server.workflows.mesh.session import (
    MeshSession,
    mesh_digest,
    replay_recipe,
)
from trid3nt_server.workflows.runtime import Accepts, AcceptsDeclarationError
from trid3nt_server.workflows.mesh.tool import (
    MeshRecipe,
    accepts_for,
    mesh_op,
    resolve_mesh,
    tool,
)
from trid3nt_server.workflows.telemac.templates.do_sag.declarations import ACCEPTS as _DO_SAG
from trid3nt_server.workflows.telemac.templates.river_dye.declarations import (
    ACCEPTS as _RIVER_DYE,
)

_AOI = (-83.50, 35.00, -83.40, 35.09)

#: The contract the resolution-order cases declare. A template's mesh row is its
#: statement of what a SUPPLIED mesh may be, and a run that states none accepts
#: none - so every case that expects a supplied or discovered mesh to be adopted
#: names the row that admits it.
_GRID = Accepts(mesh=("structured_grid",))

#: The registered template whose contract every ARTEMIS door reads off the
#: registry, by this name. It is read at CALL time, not here: the registry fills
#: as the templates import, which is still under way while this module imports.
#: The reach family (``_DO_SAG`` / ``_RIVER_DYE`` above) is PARKED - unregistered
#: pending the mesher ruling - so the registry has nothing to hand back for it and
#: what it DECLARES is read from where it is authored.
_AGITATION = "artemis_harbor_agitation"


def _recipe(**over):
    ask = {"mesher": "reg_grid", "kind": "structured_grid",
           "extent": _AOI, "resolution_m": 400.0}
    ask.update(over)
    return tool.build_mesh(**ask)


def _artifact(**over) -> MeshArtifact:
    base = dict(
        mesh_id="01MESH", name="Coweeta watershed", mode="reg_grid",
        display_uri="s3://cache/mesh/01MESH/mesh.2dm",
        slf_uri="s3://cache/mesh/01MESH/mesh.slf", utm_epsg=32617,
        crs_authid="EPSG:32617", has_bathymetry=True, node_count=4956,
        element_count=9727, bbox=_AOI,
        # A built mesh records the RECIPE it came from, and the KIND on that
        # record is what the resolution door checks a run's declaration against.
        provenance={"recipe": {"mesher": "reg_grid", "kind": "structured_grid"}})
    base.update(over)
    return MeshArtifact(**base)


# --------------------------------------------------------------------------- #
# Registration.
# --------------------------------------------------------------------------- #
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
    corpus_path = (Path(t.__file__).resolve().parents[1] / "workflows" / "mesh"
                   / "corpus.yaml")
    queries = (yaml.safe_load(corpus_path.read_text()) or {})["build_mesh"]
    assert queries
    assert any("build_mesh" in retrieve_visible_tools(q, None, 8) for q in queries), (
        "build_mesh surfaces in NO top-8 for any of its corpus queries")


# --------------------------------------------------------------------------- #
# Router validation: every refusal names what was wrong.
# --------------------------------------------------------------------------- #
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


def test_late_bound_reads_pass_declaration_and_refuse_serialization():
    from trid3nt_server.workflows.runtime.plan import ParamRef

    recipe = tool.build_mesh(mesher="reg_grid", extent=_AOI,
                             resolution_m=ParamRef("mesh_resolution_m"))
    with pytest.raises(MeshToolError) as excinfo:
        recipe.to_json()
    assert excinfo.value.error_code == "MESH_RECIPE_UNBOUND"


# --------------------------------------------------------------------------- #
# Resolution order: explicit > case discovery > declared default.
# --------------------------------------------------------------------------- #
def test_explicit_mesh_wins_over_a_discovered_one():
    stash_mesh_artifact("case-explicit", _artifact(name="discovered"))
    supplied = _artifact(mesh_id="01OTHER", name="supplied")
    resolution = resolve_mesh(_recipe(), explicit=supplied, accepts=_GRID,
                              case_id="case-explicit")
    assert resolution.source == "explicit"
    assert resolution.artifact is supplied


def test_explicit_mesh_no_solve_could_be_staged_on_refuses():
    with pytest.raises(MeshToolError) as excinfo:
        resolve_mesh(_recipe(), explicit=_artifact(slf_uri=None), accepts=_GRID)
    assert excinfo.value.error_code == "MESH_NOT_SOLVABLE"
    assert "no SELAFIN geometry" in str(excinfo.value)


def test_explicit_mesh_with_no_readable_record_refuses():
    reader = _FailingReader()
    with pytest.raises(MeshToolError) as excinfo:
        resolve_mesh(_recipe(), explicit="s3://cache/mesh/unknown/mesh.2dm",
                     s3_client=reader)
    assert excinfo.value.error_code == "MESH_EXPLICIT_UNREADABLE"
    assert "no readable mesh artifact record" in str(excinfo.value)


def test_explicit_mesh_uri_with_no_reader_names_the_missing_reader():
    """The caller's missing reader is not the supplied mesh's fault."""
    with pytest.raises(MeshToolError) as excinfo:
        resolve_mesh(_recipe(), explicit="s3://cache/mesh/unknown/mesh.2dm")
    assert excinfo.value.error_code == "MESH_EXPLICIT_UNREADABLE"
    assert "no object-store reader was supplied" in str(excinfo.value)


class _FailingReader:
    """An object store with no sidecar under the asked-for key."""

    def get_object(self, **_kwargs):
        raise KeyError("no such object")


def test_case_discovery_beats_the_declared_default():
    art = _artifact()
    stash_mesh_artifact("case-discovery", art)
    resolution = resolve_mesh(_recipe(), accepts=_GRID, case_id="case-discovery")
    assert resolution.source == "discovered"
    assert resolution.artifact is art


def test_case_discovery_skips_a_mesh_no_solve_could_be_staged_on():
    stash_mesh_artifact("case-skip", _artifact(slf_uri=None))
    resolution = resolve_mesh(_recipe(), accepts=_GRID, case_id="case-skip")
    assert resolution.source == "declared"
    assert resolution.recipe is not None


def test_declared_default_when_the_case_holds_nothing():
    recipe = _recipe()
    resolution = resolve_mesh(recipe, case_id="case-empty")
    assert resolution.source == "declared"
    assert resolution.recipe is recipe


def test_nothing_supplied_declared_or_discovered_refuses():
    with pytest.raises(MeshToolError) as excinfo:
        resolve_mesh(None, case_id="case-empty-2")
    assert excinfo.value.error_code == "MESH_UNRESOLVED"


# --------------------------------------------------------------------------- #
# The declared contract is ROLE-KEYED: membership per role, checked at the door.
#
# The contract is a standalone Accepts declaration in the template's own
# declarations.py, reached here the way every door reaches it - off the registry
# by tool name - rather than restated. The MESH block states what the DEFAULT
# BUILD produces and is not consulted here.
# --------------------------------------------------------------------------- #
def _tri_artifact(**over) -> MeshArtifact:
    return _artifact(
        mode="om2d", name="Point Judith Harbor of Refuge",
        provenance={"recipe": {"mesher": "om2d", "kind": "unstructured_tri"}},
        **over)


def test_the_registry_is_where_a_door_reads_a_templates_contract():
    """ONE HOME: the door names the tool, the registry hands back what that tool
    registered - and a name this build knows no workflow under yields nothing."""
    assert accepts_for(_AGITATION) is not None
    assert accepts_for("no_such_tool") is None


def test_the_proven_mesh_rows_are_what_the_templates_declare():
    assert _RIVER_DYE.kinds("mesh") == ("unstructured_tri",)
    assert _DO_SAG.kinds("mesh") == ("unstructured_tri",)
    assert (accepts_for(_AGITATION).kinds("mesh")
            == ("structured_grid", "unstructured_tri"))


def test_a_release_is_accepted_only_where_the_reach_family_wrote_the_row():
    """PER-ROLE ABSENCE IS A REFUSAL. The reach family releases a substance at a
    point and says so; nothing is released into a harbour agitation field, so that
    template has no release row and refuses one by not naming it."""
    assert _RIVER_DYE.accepts("release", "point") is True
    assert _DO_SAG.accepts("release", "point") is True
    assert accepts_for(_AGITATION).kinds("release") is None
    assert accepts_for(_AGITATION).accepts("release", "point") is False
    # A mesh row is no licence for a release, and a kind outside the row is not a
    # member of it either.
    assert _RIVER_DYE.accepts("release", "polygon") is False


def test_an_accept_set_naming_nothing_refuses_where_it_is_written():
    """An EMPTY declaration is authored nonsense, not a stricter absence: it
    explodes at import rather than at a door nobody would reach."""
    with pytest.raises(AcceptsDeclarationError):
        Accepts()
    with pytest.raises(AcceptsDeclarationError) as excinfo:
        Accepts(mesh=())
    assert "mesh=()" in str(excinfo.value)


def test_a_supplied_mesh_outside_the_mesh_row_refuses_by_name():
    """A lattice is not in the river-tracer's mesh row, so it is refused at the door
    rather than trusted and narrated several steps later."""
    with pytest.raises(MeshToolError) as excinfo:
        resolve_mesh(explicit=_artifact(), accepts=_RIVER_DYE)
    assert excinfo.value.error_code == "MESH_KIND_MISMATCH"
    message = str(excinfo.value)
    assert "'unstructured_tri'" in message and "'structured_grid'" in message


def test_a_supplied_mesh_in_the_mesh_row_is_accepted():
    """The BYO rematch: an om2d triangulation is a member of what ARTEMIS reads,
    even though the template's own default build is a lattice."""
    supplied = _tri_artifact()
    resolution = resolve_mesh(explicit=supplied, accepts=accepts_for(_AGITATION))
    assert resolution.source == "explicit"
    assert resolution.artifact is supplied


def test_a_template_that_declares_no_mesh_row_refuses_the_supply():
    """ABSENCE IS A REFUSAL: no mesh row means no tested supplied-mesh path."""
    with pytest.raises(MeshToolError) as excinfo:
        resolve_mesh(explicit=_tri_artifact())
    assert excinfo.value.error_code == "MESH_SUPPLY_UNDECLARED"
    assert "no supplied-mesh compatibility" in str(excinfo.value)


def test_a_supplied_mesh_that_states_no_kind_refuses():
    """Membership in a declared row is not answerable about an unstated shape."""
    with pytest.raises(MeshToolError) as excinfo:
        resolve_mesh(explicit=_artifact(provenance={}),
                     accepts=accepts_for(_AGITATION))
    assert excinfo.value.error_code == "MESH_KIND_MISMATCH"
    assert "no recorded kind" in str(excinfo.value)


def test_case_discovery_offers_only_members_of_the_mesh_row():
    """The same membership test, in the arm where a non-member is simply not a
    candidate: the run builds its declared mesh instead."""
    stash_mesh_artifact("case-wrong-kind", _artifact())
    resolution = resolve_mesh(_recipe(), accepts=_RIVER_DYE,
                              case_id="case-wrong-kind")
    assert resolution.source == "declared"


def test_case_discovery_offers_nothing_without_a_mesh_row():
    stash_mesh_artifact("case-no-set", _artifact())
    resolution = resolve_mesh(_recipe(), case_id="case-no-set")
    assert resolution.source == "declared"


# --------------------------------------------------------------------------- #
# Laziness: a declared recipe builds NOTHING.
# --------------------------------------------------------------------------- #
def test_a_recipe_builds_nothing(tmp_path):
    """A degenerate extent declares fine and only fails when a build is demanded."""
    recipe = tool.build_mesh(mesher="reg_grid", extent=(0.0, 0.0, 0.0, 0.0),
                             resolution_m=100.0)
    assert isinstance(recipe, MeshRecipe)
    with pytest.raises(ValueError):
        MeshSession(recipe, workdir=tmp_path).probes()


def test_building_an_unbound_recipe_refuses_by_name(tmp_path):
    """A placeholder must not reach the mesh library as a value it cannot read."""
    from trid3nt_server.workflows.runtime.plan import ParamRef

    recipe = tool.build_mesh(mesher="reg_grid", extent=_AOI,
                             resolution_m=ParamRef("mesh_resolution_m"))
    with pytest.raises(MeshToolError) as excinfo:
        MeshSession(recipe, workdir=tmp_path).probes()
    assert excinfo.value.error_code == "MESH_RECIPE_UNBOUND"
    assert "resolution_m" in str(excinfo.value)


def test_an_unbound_op_kwarg_refuses_at_build(tmp_path):
    from trid3nt_server.workflows.runtime.plan import ParamRef

    recipe = _recipe(ops=[mesh_op("set_bed", source=ParamRef("bed_uri"))])
    with pytest.raises(MeshToolError) as excinfo:
        MeshSession(recipe, workdir=tmp_path).probes()
    assert excinfo.value.error_code == "MESH_RECIPE_UNBOUND"
    assert "ops[0].source" in str(excinfo.value)


def test_editing_a_recipe_returns_a_new_frozen_one():
    first = _recipe()
    second = first.appending(mesh_op("set_bed", source="fetch_topobathy"))
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


# --------------------------------------------------------------------------- #
# The recipe IS the record.
# --------------------------------------------------------------------------- #
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

    from trid3nt_server.workflows.mesh.meshers import Mesh

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

    from trid3nt_server.workflows.mesh.meshers import Mesh

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


# --------------------------------------------------------------------------- #
# Accept: the artifact a case discovers, and the recipe frozen onto it.
# --------------------------------------------------------------------------- #
def test_accept_freezes_the_recipe_as_the_artifacts_provenance(tmp_path):
    session = MeshSession(_recipe(), workdir=tmp_path, case_id="case-accept",
                          name="Coweeta lattice")
    art = session.accept()
    assert art.node_count == session.mesh.node_count
    assert art.element_count == session.mesh.element_count
    assert art.crs_authid == "EPSG:4326"
    assert art.has_bathymetry is False
    assert art.unsolvable_reason() is not None
    assert art.utm_epsg is None
    assert art.recipe_uri == str(session.recipe_path)
    assert art.provenance["recipe"] == session.recipe.to_json()
    assert art.provenance["recipe"]["kind"] == "structured_grid"

    from trid3nt_server.workflows.mesh.artifact import stashed_mesh_artifacts

    assert stashed_mesh_artifacts("case-accept")[-1] is art


def test_a_geometry_less_mesh_says_why_no_solve_can_be_staged_on_it(tmp_path):
    """The readiness question is the ARTIFACT's, answered off the facts it carries."""
    art = MeshSession(_recipe(), workdir=tmp_path).accept()
    reason = art.unsolvable_reason()
    assert reason is not None and "SELAFIN" in reason


def test_snapshot_is_the_display_face(tmp_path):
    session = MeshSession(_recipe(), workdir=tmp_path, name="lattice")
    layer = session.snapshot()
    assert layer.layer_type == "mesh"
    assert layer.style == {"kind": "reference", "geometry": "line"}
    assert layer.uri.endswith("mesh.2dm")
    assert layer.crs_authid == "EPSG:4326"
    assert (tmp_path / "mesh.2dm").read_text().startswith("MESH2D")


def test_probes_measure_the_lattice_and_number_the_recipe(tmp_path):
    probes = MeshSession(_recipe(), workdir=tmp_path).probes()
    assert probes["nodes_per_cell"] == 4
    assert probes["boundary_loops"] == 1
    assert probes["min_angle_deg"] == pytest.approx(90.0, abs=1e-6)
    assert probes["edge_length_m"]["mean"] == pytest.approx(400.0, rel=0.05)
    assert len(probes["edge_length_m"]["histogram"]["counts"]) == 10
    assert probes["ops"] == []


# --------------------------------------------------------------------------- #
# The measured edge travels with the artifact, and the timestep reads it.
# --------------------------------------------------------------------------- #
def test_the_accepted_artifact_carries_what_was_measured_on_it(tmp_path):
    session = MeshSession(_recipe(), workdir=tmp_path)
    art = session.accept()
    assert art.probes["node_count"] == art.node_count
    assert art.probes["edge_length_m"]["min"] > 0.0


def test_the_measured_minimum_edge_is_read_off_the_artifact(tmp_path):
    from trid3nt_server.workflows.mesh.artifact import measured_min_edge_m

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
    from trid3nt_server.workflows.mesh.artifact import measured_min_edge_m

    assert measured_min_edge_m(art) is None


def test_the_timestep_follows_the_measured_edge_not_the_requested_one():
    """Gate-time refinement tightens dt without anybody restating the number.

    The ask stays at a coarse edge and the mesh that was BUILT is finer, so the
    CFL-safe step has to come off the mesh; reading the ask would hand the solver
    a step the mesh it runs on cannot carry.
    """
    from trid3nt_server.workflows.telemac.helpers.reach import suggest_time_step_s

    requested = suggest_time_step_s(40.0)
    refined = suggest_time_step_s(
        40.0, mesh=_artifact(probes={"edge_length_m": {"min": 8.0}}))
    assert requested == 1.0
    assert refined == pytest.approx(0.4)


def test_the_timestep_falls_back_to_the_ask_when_no_mesh_exists_yet():
    """An estimate made before any mesh exists has only the ask to go on."""
    from trid3nt_server.workflows.telemac.helpers.reach import suggest_time_step_s

    assert suggest_time_step_s(10.0) == suggest_time_step_s(10.0, mesh=None)
    assert suggest_time_step_s(10.0, mesh=_artifact()) == 0.5


# --------------------------------------------------------------------------- #
# The router's own door: bbox / location resolve into the ONE extent param.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_bbox_at_the_door_becomes_the_recipes_extent(monkeypatch):
    from trid3nt_server.workflows.mesh.meshers import MeshToolError

    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "test-cache")
    seen: dict = {}

    def _accept(self):
        seen["recipe"] = self.recipe
        raise MeshToolError("MESH_BUILD_FAILED", "stopped after validation")

    monkeypatch.setattr(MeshSession, "accept", _accept)
    with pytest.raises(MeshToolError):
        await TOOL_REGISTRY["build_mesh"].fn(
            mesher="reg_grid", bbox=(-75.8, 36.1, -75.7, 36.2), resolution_m=200.0)
    assert seen["recipe"].extent == (-75.8, 36.1, -75.7, 36.2)
    assert seen["recipe"].resolution_m == 200.0


@pytest.mark.asyncio
async def test_wire_ops_become_recipe_entries(monkeypatch):
    """The ops list off the wire is the same ordered program a template writes."""
    from trid3nt_server.workflows.mesh.meshers import MeshToolError

    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "test-cache")
    seen: dict = {}

    def _accept(self):
        seen["recipe"] = self.recipe
        raise MeshToolError("MESH_BUILD_FAILED", "stopped after validation")

    monkeypatch.setattr(MeshSession, "accept", _accept)
    with pytest.raises(MeshToolError):
        await TOOL_REGISTRY["build_mesh"].fn(
            mesher="reg_grid", bbox=(-75.8, 36.1, -75.7, 36.2), resolution_m=200.0,
            ops=[{"fn": "set_bed", "source": "fetch_topobathy",
                  "interp": "bilinear"}])
    ops = seen["recipe"].ops
    assert [op.fn for op in ops] == ["set_bed"]
    assert dict(ops[0].kwargs) == {"source": "fetch_topobathy",
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


@pytest.mark.asyncio
async def test_the_escalated_bbox_is_the_box_the_rerun_actually_models(monkeypatch):
    """A named override reaches the domain verbatim, place name notwithstanding.

    A rerun seats overrides on the parent's own sheet, so the box arrives at the
    acquisition step beside the place name the parent ran with - and an escalation
    that named a value the step then dropped would be a dead end dressed as a
    corrective.
    """
    from trid3nt_server.workflows.shared.aoi import acquire_aoi

    def _never(*_a, **_kw):  # a geocode here would mean the box was dropped
        raise AssertionError("the supplied extent is the domain; nothing to geocode")

    monkeypatch.setitem(
        TOOL_REGISTRY, "geocode_location",
        dataclasses.replace(TOOL_REGISTRY["geocode_location"], fn=_never))
    moved = (-84.00, 35.00, -83.90, 35.09)

    resolved = await acquire_aoi(location="Cataloochee, North Carolina", bbox=moved)

    assert tuple(resolved["bbox"]) == moved
