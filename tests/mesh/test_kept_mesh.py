"""A built mesh is kept under a digest of what builds it and nothing else."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.inputs.domain import Domain
from trid3nt_server.tools.mesh import step as mesh_step
from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.runtime import journal


def _recipe(bed: str, *, resolution_m: float = 20.0):
    domain = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    return {"mesher": "om2d", "kind": "unstructured_tri",
            "extent": {"geometry": domain, "name": "domain seed 91"},
            "resolution_m": resolution_m,
            "ops": [{"op": "set_bed", "kwargs": {"source": bed}}]}


def _file(tmp_path, name: str, content: bytes) -> str:
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


def test_two_fills_of_the_same_content_key_alike(tmp_path):
    one = _file(tmp_path, "bed_layer_seed_1.tif", b"elevations")
    two = _file(tmp_path, "bed_layer_seed_2.tif", b"elevations")
    assert mesh_step.mesh_key(_recipe(one)) == mesh_step.mesh_key(_recipe(two))


def test_a_changed_bed_or_resolution_is_a_new_key(tmp_path):
    bed = _file(tmp_path, "bed.tif", b"elevations")
    other = _file(tmp_path, "other.tif", b"deeper elevations")
    key = mesh_step.mesh_key(_recipe(bed))
    assert mesh_step.mesh_key(_recipe(other)) != key
    assert mesh_step.mesh_key(_recipe(bed, resolution_m=10.0)) != key


def _cut(source: str, *, east: float = 1.0) -> Domain:
    ring = [[0, 0], [east, 0], [east, 1], [0, 0]]
    return Domain(geometry={"type": "Polygon", "coordinates": [ring]},
                  name="nhdplus_nldi", uri=source)


def test_a_domain_enters_by_its_geometry_never_by_the_file_it_was_cut_from(
        tmp_path):
    bed = _file(tmp_path, "bed.tif", b"elevations")
    flowline = _file(tmp_path, "flowline.fgb", b"one reach")
    other = _file(tmp_path, "flowline_again.fgb", b"the reach fetched again")
    recipe = {**_recipe(bed), "extent": _cut(flowline)}
    again = {**_recipe(bed), "extent": _cut(other)}
    assert mesh_step.mesh_key(recipe) == mesh_step.mesh_key(again)
    wider = {**_recipe(bed), "extent": _cut(flowline, east=2.0)}
    assert mesh_step.mesh_key(wider) != mesh_step.mesh_key(recipe)


@pytest.fixture
def built(monkeypatch, tmp_path):
    monkeypatch.setenv("TRID3NT_DEV_PERSISTENCE_DIR", str(tmp_path / "store"))
    display = _file(tmp_path, "mesh.2dm", b"mesh")
    builds: list = []

    async def _gate(_session, **_kw):
        builds.append(1)
        return MeshArtifact(mesh_id=f"m{len(builds)}", name="m", mode="om2d",
                            display_uri=display, crs_authid="EPSG:32617",
                            has_bathymetry=True, node_count=3, element_count=1,
                            bbox=(0, 0, 1, 1))

    from trid3nt_server.tools.mesh import gate, session

    monkeypatch.setattr(session, "MeshSession", lambda *a, **k: None)
    monkeypatch.setattr(gate, "gate_mesh_build", _gate)
    monkeypatch.setattr(mesh_step, "_mesh_coverage", lambda *a: None)
    monkeypatch.setattr("trid3nt_server.tools.mesh.tool.recipe_from_plan_value",
                        lambda value: type("R", (), {"mesher": "om2d",
                                                     "extent": None})())
    return builds


def _build(recipe, **kw):
    return asyncio.run(mesh_step.build_declared_mesh(mesh=recipe, **kw))


def test_the_second_run_reuses_the_kept_mesh_and_says_which(built, tmp_path):
    bed = _file(tmp_path, "bed.tif", b"elevations")
    first = _build(_recipe(bed))
    asyncio.run(mesh_step.keep_mesh(first, "RUN1"))
    token = journal.bind_notes()
    again = _build(_recipe(_file(tmp_path, "bed_again.tif", b"elevations")))
    notes = journal.drain_notes(token)
    assert built == [1] and again["mesh_id"] == "m1"
    assert again["built_by"] == "RUN1"
    assert any(first["key"] in note and "RUN1" in note for note in notes)
    asyncio.run(mesh_step.keep_mesh(again, "RUN2"))
    token = journal.bind_notes()
    _build(_recipe(bed))
    assert any("RUN1" in note for note in journal.drain_notes(token))


def _run_recipe(monkeypatch, bed: str, friction: float) -> dict:
    """The recipe the run hands its mesh stage, off a fill stating ``friction``."""
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.workflows.runtime import resolve_params
    from trid3nt_server.inputs.fill import Fill, production

    workflow = TOOL_REGISTRY["telemac_dye_release"].fn.workflow
    seen: list = []

    async def _stop(*, mesh, **_kw):
        seen.append(mesh)
        raise LookupError("the recipe is all this test reads")

    state = Fill(workflow=workflow, keywords={"FRICTION_COEFFICIENT": friction},
                 params=asyncio.run(resolve_params(workflow.params, {})))
    env = production(state)
    env.run.update(domain=_cut(bed), bed=bed)
    with monkeypatch.context() as patch:
        patch.setattr(mesh_step, "build_declared_mesh", _stop)
        with pytest.raises(Exception, match="the recipe is all this test reads"):
            asyncio.run(workflow.launch(state))
    return seen[0]


def test_a_friction_change_leaves_the_key_and_restart_clean_rebuilds(
        built, monkeypatch, tmp_path):
    bed = _file(tmp_path, "bed.tif", b"elevations")
    recipe = _run_recipe(monkeypatch, bed, 33.0)
    assert mesh_step.mesh_key(_run_recipe(monkeypatch, bed, 45.0)) == \
        mesh_step.mesh_key(recipe)
    first = _build(recipe)
    asyncio.run(mesh_step.keep_mesh(first, "RUN1"))
    assert _build(_run_recipe(monkeypatch, bed, 45.0))["key"] == first["key"]
    assert built == [1]
    token = journal.bind_notes()
    _build(recipe, fresh=True)
    assert built == [1, 1]
    assert any(first["key"] in note and "restart_clean" in note
               for note in journal.drain_notes(token))
