"""A built mesh is kept under a digest of what builds it and nothing else."""

from __future__ import annotations

import asyncio

import pytest

from trid3nt_server.mesh import step as mesh_step
from trid3nt_server.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.runtime import journal


def _recipe(bed: str, *, resolution_m: float = 20.0, friction: float | None = None):
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

    from trid3nt_server.mesh import gate, session

    monkeypatch.setattr(session, "MeshSession", lambda *a, **k: None)
    monkeypatch.setattr(gate, "gate_mesh_build", _gate)
    monkeypatch.setattr(mesh_step, "_mesh_coverage", lambda *a: None)
    monkeypatch.setattr("trid3nt_server.mesh.tool.recipe_from_plan_value",
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
    assert any(first["key"] in note and "RUN1" in note for note in notes)
    asyncio.run(mesh_step.keep_mesh(again, "RUN2"))
    token = journal.bind_notes()
    _build(_recipe(bed))
    assert any("RUN1" in note for note in journal.drain_notes(token))


def test_a_friction_change_leaves_the_key_and_restart_clean_rebuilds(built,
                                                                     tmp_path):
    bed = _file(tmp_path, "bed.tif", b"elevations")
    first = _build(_recipe(bed))
    asyncio.run(mesh_step.keep_mesh(first, "RUN1"))
    assert _build(_recipe(bed, friction=45.0))["key"] == first["key"]
    assert built == [1]
    _build(_recipe(bed), fresh=True)
    assert built == [1, 1]
