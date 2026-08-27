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
from trid3nt_server.workflows.mesh.tool import (
    MeshDeclaration,
    resolve_mesh,
    tool,
    validate_spec,
)

_AOI = (-83.50, 35.00, -83.40, 35.09)


def _declaration(**over):
    fields = {"mesher": "reg_grid", "kind": "structured_grid",
              "aoi": _AOI, "resolution_m": 400.0}
    fields.update(over)
    return tool.build_mesh(**fields)


def _artifact(**over) -> MeshArtifact:
    base = dict(
        mesh_id="01MESH", name="Coweeta watershed", mode="corridor_tin",
        display_uri="s3://cache/mesh/01MESH/mesh.2dm",
        slf_uri="s3://cache/mesh/01MESH/mesh.slf", utm_epsg=32617,
        crs_authid="EPSG:32617", has_bathymetry=True, node_count=4956,
        element_count=9727, bbox=_AOI, engine_compat=["telemac"])
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
    assert set(mesher.fields) == {"kind", "aoi", "resolution_m"}
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
        ({"mesher": "reg_grid", "aoi": _AOI}, "MESH_SPEC_MISSING_FIELD"),
        ({"mesher": "reg_grid", "aoi": _AOI, "resolution_m": "coarse"},
         "MESH_SPEC_BAD_TYPE"),
        ({"mesher": "reg_grid", "kind": "unstructured_tri", "aoi": _AOI,
          "resolution_m": 400.0}, "MESH_SPEC_BAD_VALUE"),
        ({"mesher": "reg_grid", "aoi": _AOI, "resolution_m": 400.0,
          "refine": {"edge_length": 10}}, "MESH_SPEC_UNKNOWN_FIELD"),
    ],
)
def test_router_refuses_a_spec_the_mesher_never_declared(fields, code):
    with pytest.raises(MeshToolError) as excinfo:
        tool.build_mesh(**fields)
    assert excinfo.value.error_code == code


def test_router_refusal_names_the_declared_fields():
    with pytest.raises(MeshToolError) as excinfo:
        tool.build_mesh(mesher="reg_grid", aoi=_AOI, resolution_m=400.0,
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
    spec = validate_spec("reg_grid", {"aoi": _AOI, "resolution_m": 400.0})
    assert spec.kind == "structured_grid"


def test_late_bound_reads_pass_declaration_and_refuse_serialization():
    from trid3nt_server.workflows.lib.plan import ParamRef

    declaration = tool.build_mesh(mesher="reg_grid", aoi=_AOI,
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
                              engine="telemac", case_id="case-explicit")
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
    resolution = resolve_mesh(_declaration(), engine="telemac",
                              case_id="case-discovery")
    assert resolution.source == "discovered"
    assert resolution.artifact is art


def test_case_discovery_skips_a_mesh_the_engine_cannot_read():
    stash_mesh_artifact("case-skip", _artifact(slf_uri=None, engine_compat=[]))
    resolution = resolve_mesh(_declaration(), engine="telemac", case_id="case-skip")
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
# Laziness: a declared ask builds NOTHING.
# --------------------------------------------------------------------------- #
def test_a_declaration_builds_nothing(tmp_path):
    """A degenerate extent declares fine and only fails when a build is demanded."""
    declaration = tool.build_mesh(mesher="reg_grid", aoi=(0.0, 0.0, 0.0, 0.0),
                                  resolution_m=100.0).edit("set_resolution", 50.0)
    assert isinstance(declaration, MeshDeclaration)
    assert len(declaration.edits) == 1
    with pytest.raises(ValueError):
        MeshSession(declaration, workdir=tmp_path).probes()


def test_building_an_unbound_declaration_refuses_by_name(tmp_path):
    """A placeholder must not reach the mesh library as a value it cannot read."""
    from trid3nt_server.workflows.lib.plan import ParamRef

    declaration = tool.build_mesh(mesher="reg_grid", aoi=_AOI,
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
    third = second.edit("set_extent", aoi=_AOI)
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
    session.edit("set_extent", aoi=(-83.50, 35.00, -83.45, 35.05))
    lines = [json.loads(ln) for ln in
             session.recipe_path.read_text().splitlines() if ln.strip()]
    assert lines[0]["spec"]["mesher"] == "reg_grid"
    assert [ln["edit"] for ln in lines[1:]] == ["set_resolution", "set_extent"]
    assert lines[1]["resolution_m"] == 250.0


def test_recipe_replays_to_an_identical_mesh(tmp_path):
    session = MeshSession(_declaration().edit("set_resolution", 250.0),
                          workdir=tmp_path)
    session.edit("set_extent", aoi=(-83.50, 35.00, -83.45, 35.05))
    replayed = replay_recipe(session.recipe_path)
    assert mesh_digest(replayed) == mesh_digest(session.mesh)


def test_the_same_spec_builds_the_same_mesh_twice(tmp_path):
    one = MeshSession(_declaration(), workdir=tmp_path / "one")
    two = MeshSession(_declaration(), workdir=tmp_path / "two")
    assert mesh_digest(one.mesh) == mesh_digest(two.mesh)


def test_restart_truncates_to_the_declared_prefix(tmp_path):
    session = MeshSession(_declaration().edit("set_resolution", 250.0),
                          workdir=tmp_path)
    session.edit("set_extent", aoi=(-83.50, 35.00, -83.45, 35.05))
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
