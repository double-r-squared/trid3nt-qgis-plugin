"""Offline tests for the mesh router, the mesh session and the reg_grid mesher.

The PURE surfaces, with no container, no object store and no live session: the
router's typed refusals against a mesher's declared fields, the explicit /
discovered / declared resolution order, the laziness of a declared ask, the
recipe journal's replay determinism, restart truncation, and the hand-edit
record that honestly refuses to replay.

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
from trid3nt_server.workflows.lib import Accepts, AcceptsDeclarationError
from trid3nt_server.workflows.mesh.tool import (
    MeshDeclaration,
    accepts_for,
    resolve_mesh,
    tool,
    validate_spec,
)
from trid3nt_server.workflows.telemac.do_sag.declarations import ACCEPTS as _DO_SAG
from trid3nt_server.workflows.telemac.river_dye.declarations import (
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


def _declaration(**over):
    fields = {"mesher": "reg_grid", "kind": "structured_grid",
              "extent": _AOI, "resolution_m": 400.0}
    fields.update(over)
    return tool.build_mesh(**fields)


def _artifact(**over) -> MeshArtifact:
    base = dict(
        mesh_id="01MESH", name="Coweeta watershed", mode="reg_grid",
        display_uri="s3://cache/mesh/01MESH/mesh.2dm",
        slf_uri="s3://cache/mesh/01MESH/mesh.slf", utm_epsg=32617,
        crs_authid="EPSG:32617", has_bathymetry=True, node_count=4956,
        element_count=9727, bbox=_AOI, engine_compat=["telemac"],
        # A built mesh records the ask it came from, and the KIND on that record
        # is what the resolution door checks a run's declaration against.
        provenance={"spec": {"mesher": "reg_grid", "kind": "structured_grid"}})
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


def test_reg_grid_registered_with_its_declarations():
    mesher = get_mesher("reg_grid")
    assert set(mesher.fields) == {"kind", "extent", "resolution_m"}
    assert set(mesher.actions) == {"set_resolution", "set_extent",
                                   "apply_layer_edits"}
    assert mesher.actions["apply_layer_edits"].replayable is False


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
@pytest.mark.parametrize(
    "fields, code",
    [
        ({"mesher": "no_such_mesher"}, "MESH_UNKNOWN_MESHER"),
        ({"mesher": "reg_grid", "extent": _AOI}, "MESH_SPEC_MISSING_FIELD"),
        ({"mesher": "reg_grid", "extent": _AOI, "resolution_m": "coarse"},
         "MESH_SPEC_BAD_TYPE"),
        ({"mesher": "reg_grid", "kind": "unstructured_tri", "extent": _AOI,
          "resolution_m": 400.0}, "MESH_SPEC_BAD_VALUE"),
        ({"mesher": "reg_grid", "extent": _AOI, "resolution_m": 400.0,
          "refine": {"edge_length": 10}}, "MESH_SPEC_UNKNOWN_FIELD"),
    ],
)
def test_router_refuses_a_spec_the_mesher_never_declared(fields, code):
    with pytest.raises(MeshToolError) as excinfo:
        tool.build_mesh(**fields)
    assert excinfo.value.error_code == code


def test_router_refusal_names_the_declared_fields():
    with pytest.raises(MeshToolError) as excinfo:
        tool.build_mesh(mesher="reg_grid", extent=_AOI, resolution_m=400.0,
                        resolution_meters=400.0)
    message = str(excinfo.value)
    assert "resolution_meters" in message and "resolution_m" in message


def test_router_refuses_an_unregistered_edit_action():
    with pytest.raises(MeshToolError) as excinfo:
        _declaration().edit("refine_region", geometry="poly")
    assert excinfo.value.error_code == "MESH_UNKNOWN_ACTION"


def test_router_refuses_an_input_the_action_never_declared():
    with pytest.raises(MeshToolError) as excinfo:
        _declaration().edit("set_resolution", resolution_km=0.25)
    assert excinfo.value.error_code == "MESH_EDIT_UNKNOWN_INPUT"


def test_declared_defaults_fill_in():
    spec = validate_spec("reg_grid", {"extent": _AOI, "resolution_m": 400.0})
    assert spec.kind == "structured_grid"


def test_late_bound_reads_pass_declaration_and_refuse_serialization():
    from trid3nt_server.workflows.lib.plan import ParamRef

    declaration = tool.build_mesh(mesher="reg_grid", extent=_AOI,
                                  resolution_m=ParamRef("mesh_resolution_m"))
    with pytest.raises(MeshToolError) as excinfo:
        declaration.spec.to_json()
    assert excinfo.value.error_code == "MESH_SPEC_UNBOUND"


# --------------------------------------------------------------------------- #
# Resolution order: explicit > case discovery > declared default.
# --------------------------------------------------------------------------- #
def test_explicit_mesh_wins_over_a_discovered_one():
    stash_mesh_artifact("case-explicit", _artifact(name="discovered"))
    supplied = _artifact(mesh_id="01OTHER", name="supplied")
    resolution = resolve_mesh(_declaration(), explicit=supplied,
                              engine="telemac", accepts=_GRID,
                              case_id="case-explicit")
    assert resolution.source == "explicit"
    assert resolution.artifact is supplied


def test_explicit_mesh_an_engine_cannot_read_refuses():
    with pytest.raises(MeshToolError) as excinfo:
        resolve_mesh(_declaration(), explicit=_artifact(slf_uri=None),
                     engine="telemac")
    assert excinfo.value.error_code == "MESH_ENGINE_INCOMPATIBLE"


def test_explicit_mesh_with_no_readable_record_refuses():
    reader = _FailingReader()
    with pytest.raises(MeshToolError) as excinfo:
        resolve_mesh(_declaration(), explicit="s3://cache/mesh/unknown/mesh.2dm",
                     s3_client=reader)
    assert excinfo.value.error_code == "MESH_EXPLICIT_UNREADABLE"
    assert "no readable mesh artifact record" in str(excinfo.value)


def test_explicit_mesh_uri_with_no_reader_names_the_missing_reader():
    """The caller's missing reader is not the supplied mesh's fault."""
    with pytest.raises(MeshToolError) as excinfo:
        resolve_mesh(_declaration(), explicit="s3://cache/mesh/unknown/mesh.2dm")
    assert excinfo.value.error_code == "MESH_EXPLICIT_UNREADABLE"
    assert "no object-store reader was supplied" in str(excinfo.value)


class _FailingReader:
    """An object store with no sidecar under the asked-for key."""

    def get_object(self, **_kwargs):
        raise KeyError("no such object")


def test_case_discovery_beats_the_declared_default():
    art = _artifact()
    stash_mesh_artifact("case-discovery", art)
    resolution = resolve_mesh(_declaration(), engine="telemac", accepts=_GRID,
                              case_id="case-discovery")
    assert resolution.source == "discovered"
    assert resolution.artifact is art


def test_case_discovery_skips_a_mesh_the_engine_cannot_read():
    stash_mesh_artifact("case-skip", _artifact(slf_uri=None, engine_compat=[]))
    resolution = resolve_mesh(_declaration(), engine="telemac", accepts=_GRID,
                              case_id="case-skip")
    assert resolution.source == "declared"
    assert resolution.declaration is not None


def test_declared_default_when_the_case_holds_nothing():
    declaration = _declaration()
    resolution = resolve_mesh(declaration, case_id="case-empty")
    assert resolution.source == "declared"
    assert resolution.declaration is declaration


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
        provenance={"spec": {"mesher": "om2d", "kind": "unstructured_tri"}},
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
        resolve_mesh(explicit=_artifact(), engine="telemac",
                     accepts=_RIVER_DYE)
    assert excinfo.value.error_code == "MESH_KIND_MISMATCH"
    message = str(excinfo.value)
    assert "'unstructured_tri'" in message and "'structured_grid'" in message


def test_a_supplied_mesh_in_the_mesh_row_is_accepted():
    """The BYO rematch: an om2d triangulation is a member of what ARTEMIS reads,
    even though the template's own default build is a lattice."""
    supplied = _tri_artifact()
    resolution = resolve_mesh(explicit=supplied, engine="telemac",
                              accepts=accepts_for(_AGITATION))
    assert resolution.source == "explicit"
    assert resolution.artifact is supplied


def test_a_template_that_declares_no_mesh_row_refuses_the_supply():
    """ABSENCE IS A REFUSAL: no mesh row means no tested supplied-mesh path."""
    with pytest.raises(MeshToolError) as excinfo:
        resolve_mesh(explicit=_tri_artifact(), engine="telemac")
    assert excinfo.value.error_code == "MESH_SUPPLY_UNDECLARED"
    assert "no supplied-mesh compatibility" in str(excinfo.value)


def test_a_supplied_mesh_that_states_no_kind_refuses():
    """Membership in a declared row is not answerable about an unstated shape."""
    with pytest.raises(MeshToolError) as excinfo:
        resolve_mesh(explicit=_artifact(provenance={}), engine="telemac",
                     accepts=accepts_for(_AGITATION))
    assert excinfo.value.error_code == "MESH_KIND_MISMATCH"
    assert "no recorded kind" in str(excinfo.value)


def test_case_discovery_offers_only_members_of_the_mesh_row():
    """The same membership test, in the arm where a non-member is simply not a
    candidate: the run builds its declared mesh instead."""
    stash_mesh_artifact("case-wrong-kind", _artifact())
    resolution = resolve_mesh(_declaration(), engine="telemac",
                              accepts=_RIVER_DYE, case_id="case-wrong-kind")
    assert resolution.source == "declared"


def test_case_discovery_offers_nothing_without_a_mesh_row():
    stash_mesh_artifact("case-no-set", _artifact())
    resolution = resolve_mesh(_declaration(), engine="telemac",
                              case_id="case-no-set")
    assert resolution.source == "declared"


# --------------------------------------------------------------------------- #
# Laziness: a declared ask builds NOTHING.
# --------------------------------------------------------------------------- #
def test_a_declaration_builds_nothing(tmp_path):
    """A degenerate extent declares fine and only fails when a build is demanded."""
    declaration = tool.build_mesh(mesher="reg_grid", extent=(0.0, 0.0, 0.0, 0.0),
                                  resolution_m=100.0).edit("set_resolution", 50.0)
    assert isinstance(declaration, MeshDeclaration)
    assert len(declaration.edits) == 1
    with pytest.raises(ValueError):
        MeshSession(declaration, workdir=tmp_path).probes()


def test_building_an_unbound_declaration_refuses_by_name(tmp_path):
    """A placeholder must not reach the mesh library as a value it cannot read."""
    from trid3nt_server.workflows.lib.plan import ParamRef

    declaration = tool.build_mesh(mesher="reg_grid", extent=_AOI,
                                  resolution_m=ParamRef("mesh_resolution_m"))
    with pytest.raises(MeshToolError) as excinfo:
        MeshSession(declaration, workdir=tmp_path).probes()
    assert excinfo.value.error_code == "MESH_SPEC_UNBOUND"
    assert "reg_grid.resolution_m" in str(excinfo.value)


def test_an_unbound_declared_edit_input_refuses_at_build(tmp_path):
    from trid3nt_server.workflows.lib.plan import ParamRef

    declaration = _declaration().edit("set_resolution", ParamRef("cell_size_m"))
    with pytest.raises(MeshToolError) as excinfo:
        MeshSession(declaration, workdir=tmp_path).probes()
    assert excinfo.value.error_code == "MESH_SPEC_UNBOUND"
    assert "set_resolution.resolution_m" in str(excinfo.value)


def test_an_unbound_session_edit_input_refuses(tmp_path):
    from trid3nt_server.workflows.lib.plan import ParamRef

    session = MeshSession(_declaration(), workdir=tmp_path)
    with pytest.raises(MeshToolError) as excinfo:
        session.edit("set_resolution", ParamRef("cell_size_m"))
    assert excinfo.value.error_code == "MESH_SPEC_UNBOUND"
    assert session.chain == ()


def test_edit_chaining_returns_a_new_frozen_declaration():
    first = _declaration()
    second = first.edit("set_resolution", 200.0)
    third = second.edit("set_extent", extent=_AOI)
    assert first.edits == ()
    assert [e.action for e in third.edits] == ["set_resolution", "set_extent"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.spec = second.spec


def test_positional_edit_values_bind_to_the_declared_input():
    declaration = _declaration().edit("set_resolution", 250.0)
    assert dict(declaration.edits[0].inputs) == {"resolution_m": 250.0}


# --------------------------------------------------------------------------- #
# The recipe IS the record.
# --------------------------------------------------------------------------- #
def test_recipe_journals_the_spec_then_one_line_per_edit(tmp_path):
    session = MeshSession(_declaration().edit("set_resolution", 250.0),
                          workdir=tmp_path)
    session.edit("set_extent", extent=(-83.50, 35.00, -83.45, 35.05))
    lines = [json.loads(ln) for ln in
             session.recipe_path.read_text().splitlines() if ln.strip()]
    assert lines[0]["spec"]["mesher"] == "reg_grid"
    assert [ln["edit"] for ln in lines[1:]] == ["set_resolution", "set_extent"]
    assert lines[1]["resolution_m"] == 250.0


def test_recipe_replays_to_an_identical_mesh(tmp_path):
    session = MeshSession(_declaration().edit("set_resolution", 250.0),
                          workdir=tmp_path)
    session.edit("set_extent", extent=(-83.50, 35.00, -83.45, 35.05))
    replayed = replay_recipe(session.recipe_path)
    assert mesh_digest(replayed) == mesh_digest(session.mesh)


def test_the_same_spec_builds_the_same_mesh_twice(tmp_path):
    one = MeshSession(_declaration(), workdir=tmp_path / "one")
    two = MeshSession(_declaration(), workdir=tmp_path / "two")
    assert mesh_digest(one.mesh) == mesh_digest(two.mesh)


def test_restart_truncates_to_the_declared_prefix(tmp_path):
    session = MeshSession(_declaration().edit("set_resolution", 250.0),
                          workdir=tmp_path)
    session.edit("set_extent", extent=(-83.50, 35.00, -83.45, 35.05))
    narrowed = session.probes()["node_count"]

    probes = session.restart()
    assert probes["edits_applied"] == ["set_resolution"]
    assert probes["node_count"] != narrowed
    lines = [json.loads(ln) for ln in
             session.recipe_path.read_text().splitlines() if ln.strip()]
    assert [ln["edit"] for ln in lines[1:]] == ["set_resolution"]
    assert mesh_digest(replay_recipe(session.recipe_path)) == mesh_digest(session.mesh)


def test_a_hand_edit_is_recorded_hashed_and_refuses_to_replay(tmp_path):
    import numpy as np

    from trid3nt_server.workflows.mesh.meshers import Mesh

    edited = tmp_path / "edited.2dm"
    edited.write_text(write_2dm(Mesh(
        points=np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]),
        cells=np.array([[0, 1, 2], [1, 3, 2]]), crs_authid="EPSG:4326")))

    session = MeshSession(_declaration(), workdir=tmp_path)
    session.edit("apply_layer_edits", layer=str(edited))
    assert session.mesh.element_count == 2

    line = [json.loads(ln) for ln in
            session.recipe_path.read_text().splitlines() if ln.strip()][-1]
    assert line["edit"] == "apply_layer_edits"
    assert line["layer"].startswith("sha256:")
    assert line["source"] == str(edited)
    assert line["replayable"] is False

    with pytest.raises(MeshToolError) as excinfo:
        replay_recipe(session.recipe_path)
    assert excinfo.value.error_code == "MESH_RECIPE_NOT_REPLAYABLE"


# --------------------------------------------------------------------------- #
# Accept: the artifact a case discovers, and the recipe beside it.
# --------------------------------------------------------------------------- #
def test_accept_records_the_artifact_with_its_recipe(tmp_path):
    session = MeshSession(_declaration(), workdir=tmp_path, case_id="case-accept",
                          name="Coweeta lattice")
    art = session.accept()
    assert art.node_count == session.mesh.node_count
    assert art.element_count == session.mesh.element_count
    assert art.crs_authid == "EPSG:4326"
    assert art.has_bathymetry is False
    assert art.engine_compat == []
    assert art.utm_epsg is None
    assert art.recipe_uri == str(session.recipe_path)
    assert art.provenance["spec"]["mesher"] == "reg_grid"

    from trid3nt_server.workflows.mesh.artifact import stashed_mesh_artifacts

    assert stashed_mesh_artifacts("case-accept")[-1] is art


def test_a_bed_less_mesh_is_declined_by_a_bed_needing_engine(tmp_path):
    from trid3nt_server.workflows.mesh.artifact import mesh_compatible_with_engine

    art = MeshSession(_declaration(), workdir=tmp_path).accept()
    ok, reason = mesh_compatible_with_engine(art, "telemac")
    assert ok is False
    assert "SELAFIN" in reason


def test_snapshot_is_the_display_face(tmp_path):
    session = MeshSession(_declaration(), workdir=tmp_path, name="lattice")
    layer = session.snapshot()
    assert layer.layer_type == "mesh"
    assert layer.style_preset == "mesh_wireframe"
    assert layer.uri.endswith("mesh.2dm")
    assert layer.crs_authid == "EPSG:4326"
    assert (tmp_path / "mesh.2dm").read_text().startswith("MESH2D")


def test_probes_measure_the_lattice(tmp_path):
    probes = MeshSession(_declaration(), workdir=tmp_path).probes()
    assert probes["nodes_per_cell"] == 4
    assert probes["boundary_loops"] == 1
    assert probes["min_angle_deg"] == pytest.approx(90.0, abs=1e-6)
    assert probes["edge_length_m"]["mean"] == pytest.approx(400.0, rel=0.05)
    assert len(probes["edge_length_m"]["histogram"]["counts"]) == 10


# --------------------------------------------------------------------------- #
# The measured edge travels with the artifact, and the timestep reads it.
# --------------------------------------------------------------------------- #
def test_the_accepted_artifact_carries_what_was_measured_on_it(tmp_path):
    session = MeshSession(_declaration(), workdir=tmp_path)
    art = session.accept()
    assert art.probes["node_count"] == art.node_count
    assert art.probes["edge_length_m"]["min"] > 0.0


def test_the_measured_minimum_edge_is_read_off_the_artifact(tmp_path):
    from trid3nt_server.workflows.mesh.artifact import measured_min_edge_m

    art = MeshSession(_declaration(), workdir=tmp_path).accept()
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
    from trid3nt_server.workflows.telemac.steps.reach import suggest_time_step_s

    requested = suggest_time_step_s(40.0)
    refined = suggest_time_step_s(
        40.0, mesh=_artifact(probes={"edge_length_m": {"min": 8.0}}))
    assert requested == 1.0
    assert refined == pytest.approx(0.4)


def test_the_timestep_falls_back_to_the_ask_when_no_mesh_exists_yet():
    """An estimate made before any mesh exists has only the ask to go on."""
    from trid3nt_server.workflows.telemac.steps.reach import suggest_time_step_s

    assert suggest_time_step_s(10.0) == suggest_time_step_s(10.0, mesh=None)
    assert suggest_time_step_s(10.0, mesh=_artifact()) == 0.5


# --------------------------------------------------------------------------- #
# The router refuses what a mesher never declared - including an extent.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_a_mesher_that_takes_no_extent_refuses_one_by_name(monkeypatch):
    """A mesher that ADOPTS a geometry is reached by no AOI, so it declines one.

    Dropping it silently read as a lever that shaped the mesh - the user names an
    extent, gets a mesh of whatever file was handed over, and nothing says the
    two never met. Stood up for the test rather than borrowed from the roster:
    every mesher the tree carries cuts its domain from an extent, and the refusal
    is about what a mesher DECLARES, not about which ones are registered.
    """
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.mesh import meshers as mesher_registry
    from trid3nt_server.workflows.mesh.meshers import (
        MeshField,
        Mesher,
        MeshToolError,
    )

    monkeypatch.setitem(
        mesher_registry._MESHERS, "adopts_a_geometry",
        Mesher(name="adopts_a_geometry", build=lambda spec: None,
               fields={"geometry": MeshField(
                   "geometry", types=(str,), required=True,
                   doc="the mesh file this mesher adopts")}))
    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "test-cache")
    fn = TOOL_REGISTRY["build_mesh"].fn
    for spatial in ({"bbox": (-75.8, 36.1, -75.7, 36.2)}, {"location": "Norfolk"}):
        with pytest.raises(MeshToolError) as excinfo:
            await fn(mesher="adopts_a_geometry", geometry="/tmp/nowhere.slf",
                     **spatial)
        assert excinfo.value.error_code == "MESH_SPEC_UNKNOWN_FIELD"
        assert next(iter(spatial)) in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_mesher_that_declares_an_extent_still_takes_one(monkeypatch):
    """The refusal is about what THIS mesher declares, not about extents."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.mesh.meshers import MeshToolError

    monkeypatch.setenv("TRID3NT_CACHE_BUCKET", "test-cache")
    seen: dict = {}

    def _accept(self, action, *values, **inputs):  # noqa: ANN001
        seen.update(self.spec.fields)
        raise MeshToolError("MESH_BUILD_FAILED", "stopped after validation")

    monkeypatch.setattr(MeshSession, "accept",
                        lambda self: _accept(self, None))
    with pytest.raises(MeshToolError):
        await TOOL_REGISTRY["build_mesh"].fn(
            mesher="reg_grid", bbox=(-75.8, 36.1, -75.7, 36.2), resolution_m=200.0)
    assert seen["extent"] == (-75.8, 36.1, -75.7, 36.2)


# --------------------------------------------------------------------------- #
# A registered action's inputs are its generated tool's parameters.
# --------------------------------------------------------------------------- #
def test_an_optional_input_before_a_required_one_refuses_at_registration():
    """The generated tool is a REAL signature in declaration order, and Python
    has no required parameter after one that defaults - so the source would not
    compile, at whatever later moment a gate first opened over this mesher."""
    from trid3nt_server.workflows.mesh.meshers import (
        EditAction,
        MeshField,
        MeshToolError,
        register_mesher,
    )

    action = EditAction(
        name="misdeclared", apply=lambda mesh, **_kw: mesh,
        inputs={"tolerance_m": MeshField("tolerance_m", types=(int, float)),
                "geometry": MeshField("geometry", types=(str,), required=True)})
    with pytest.raises(MeshToolError) as excinfo:
        register_mesher("mesher_with_a_misdeclared_action", lambda spec: None,
                        actions=(action,))
    assert excinfo.value.error_code == "MESH_ACTION_INPUT_ORDER"
    assert "geometry" in str(excinfo.value) and "tolerance_m" in str(excinfo.value)


def test_every_registered_action_compiles_into_a_real_signature():
    """The guard's own premise, checked against the whole roster."""
    import inspect

    from trid3nt_server.workflows.mesh.gate import _edit_tool
    from trid3nt_server.workflows.mesh.meshers import registered_meshers

    for name in registered_meshers():
        for action in get_mesher(name).actions.values():
            _metadata, fn = _edit_tool("MESH01", action)
            assert list(inspect.signature(fn).parameters) == list(action.inputs)


# --------------------------------------------------------------------------- #
# The containment rule: a crop is an edit, a move is a rerun.
# --------------------------------------------------------------------------- #
def _coverage(session) -> tuple:
    from trid3nt_server.workflows.mesh.meshers import staged_coverage

    return staged_coverage(session.mesh)


def test_an_extent_inside_the_staged_coverage_crops_as_a_journaled_edit(tmp_path):
    """Within coverage the box narrows and the recipe carries the crop."""
    session = MeshSession(_declaration(), workdir=tmp_path)
    before = session.probes()["node_count"]
    crop = (-83.48, 35.02, -83.44, 35.06)

    session.edit("set_extent", extent=crop)

    assert session.probes()["node_count"] < before
    lines = [json.loads(ln) for ln in
             session.recipe_path.read_text().splitlines() if ln.strip()]
    assert [ln["edit"] for ln in lines[1:]] == ["set_extent"]
    assert lines[1]["extent"] == list(crop)
    assert mesh_digest(replay_recipe(session.recipe_path)) == mesh_digest(session.mesh)


def test_the_staged_coverage_is_what_the_mesh_states_it_was_built_over(tmp_path):
    session = MeshSession(_declaration(), workdir=tmp_path)
    coverage = _coverage(session)
    assert coverage[0] <= _AOI[0] and coverage[1] <= _AOI[1]
    assert coverage[2] >= _AOI[2] and coverage[3] >= _AOI[3]


@pytest.mark.parametrize("extent, why", [
    ((-84.00, 35.00, -83.90, 35.09), "moved clear of the coverage"),
    ((-83.55, 35.00, -83.40, 35.09), "wider on one side"),
    ((-83.50, 35.00, -83.40, 35.20), "taller on one side"),
    ((-83.52, 34.98, -83.38, 35.11), "larger on every side"),
])
def test_an_extent_outside_the_staged_coverage_refuses_as_an_edit(tmp_path, extent,
                                                                  why):
    """Containment is BINARY: partial coverage would mesh unfetched ground."""
    session = MeshSession(_declaration(), workdir=tmp_path / why.replace(" ", "_"))
    with pytest.raises(MeshToolError) as excinfo:
        session.edit("set_extent", extent=extent)
    assert excinfo.value.error_code == "MESH_EXTENT_OUTSIDE_COVERAGE"
    assert session.chain == ()


def test_the_refusal_names_the_one_rerun_path_and_the_new_box(tmp_path):
    """The answer to a moved extent is the rerun primitive, never a second one."""
    from trid3nt_server.workflows.mesh.meshers import RESTAGE_TOOL

    session = MeshSession(_declaration(), workdir=tmp_path)
    moved = (-84.00, 35.00, -83.90, 35.09)
    with pytest.raises(MeshToolError) as excinfo:
        session.edit("set_extent", extent=moved)

    assert RESTAGE_TOOL == "rerun_workflow"
    assert TOOL_REGISTRY[RESTAGE_TOOL] is not None
    message = str(excinfo.value)
    assert f"{RESTAGE_TOOL}(run_id=" in message
    assert str(list(moved)) in message
    assert excinfo.value.escalation == {"tool": RESTAGE_TOOL,
                                        "overrides": {"bbox": list(moved)}}


@pytest.mark.parametrize("extent, code", [
    ((-83.50, 35.00, -83.40), "MESH_EXTENT_MALFORMED"),
    ((-83.40, 35.00, -83.50, 35.09), "MESH_EXTENT_MALFORMED"),
    ((-83.50, 35.09, -83.40, 35.00), "MESH_EXTENT_MALFORMED"),
])
def test_an_extent_that_is_not_a_box_refuses_before_containment(tmp_path, extent,
                                                                code):
    session = MeshSession(_declaration(), workdir=tmp_path / str(len(extent)))
    with pytest.raises(MeshToolError) as excinfo:
        session.edit("set_extent", extent=extent)
    assert excinfo.value.error_code == code


def test_a_crop_keeps_the_coverage_its_inputs_were_staged_over(tmp_path):
    """A crop stages nothing, so it narrows the mesh and not the coverage.

    Judging the second extent change against the first crop would refuse a box the
    staged inputs already cover, and would make undoing a crop a restage.
    """
    session = MeshSession(_declaration(), workdir=tmp_path)
    staged = _coverage(session)
    session.edit("set_extent", extent=(-83.48, 35.02, -83.44, 35.06))
    assert _coverage(session) == staged

    wider = (-83.49, 35.01, -83.42, 35.07)
    session.edit("set_extent", extent=wider)
    assert tuple(round(v, 6) for v in session.mesh.meta["extent"]) == wider
    assert _coverage(session) == staged


def test_a_resolution_change_keeps_the_coverage_too(tmp_path):
    """Every re-derivation carries it: a finer lattice restages nothing either."""
    session = MeshSession(_declaration(), workdir=tmp_path)
    staged = _coverage(session)
    session.edit("set_extent", extent=(-83.48, 35.02, -83.44, 35.06))
    session.edit("set_resolution", resolution_m=400.0)
    assert _coverage(session) == staged


def test_a_mesh_that_states_no_coverage_refuses_the_crop_rather_than_guessing():
    """Containment is judged against staged coverage; no coverage, no judgement."""
    from trid3nt_server.workflows.mesh.meshers import Mesh, contained_extent

    bare = Mesh(points=None, cells=None, crs_authid="EPSG:4326", bed=None, meta={})
    with pytest.raises(MeshToolError) as excinfo:
        contained_extent(bare, (-83.49, 35.01, -83.45, 35.05), edit="set_extent")
    assert excinfo.value.error_code == "MESH_COVERAGE_UNKNOWN"


@pytest.mark.asyncio
async def test_the_escalated_bbox_is_the_box_the_rerun_actually_models(monkeypatch):
    """The named override reaches the domain verbatim, place name notwithstanding.

    The refusal above sends the caller to ``rerun_workflow`` with the new box under
    ``bbox``. A rerun seats overrides on the parent's own sheet, so the box arrives
    at the acquisition step beside the place name the parent ran with - and an
    escalation that named a value the step then dropped would be a dead end
    dressed as a corrective.
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
