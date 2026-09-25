"""The files TELEMAC asks an accepted mesh for, written by its own author step.

Offline: the mesh is a lattice on disk, and the upload is replaced by a copy
beside it. Pinned: the pair is written from the mesh's own walk and recorded on
the mesh under the steering keywords, a mesh already carrying the pair is not
written again, and nowhere to stage refuses by name.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from trid3nt_server.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.telemac.authoring import mesh_files as MF
from trid3nt_server.workflows.telemac.authoring.selafin_io import (
    BOUNDARY_CONDITIONS_FILE,
    GEOMETRY_FILE,
)
from trid3nt_server.workflows.telemac.errors import TelemacError


def _lattice(tmp_path: Path) -> Path:
    """A 3x3 node lattice in metres, two triangles per square, bed painted."""
    rows = ["MESH2D"]
    cell = 0
    for r in range(2):
        for c in range(2):
            a = r * 3 + c + 1
            for tri in ((a, a + 1, a + 4), (a, a + 4, a + 3)):
                cell += 1
                rows.append(f"E3T {cell} {tri[0]} {tri[1]} {tri[2]} 1")
    node = 0
    for y in (0.0, 10.0, 20.0):
        for x in (0.0, 10.0, 20.0):
            node += 1
            rows.append(f"ND {node} {x} {y} -2.0")
    path = tmp_path / "mesh.2dm"
    path.write_text("\n".join(rows) + "\n")
    return path


def _record(tmp_path: Path, **over) -> dict:
    art = MeshArtifact(
        mesh_id="M01", name="lattice", mode="om2d",
        display_uri=str(_lattice(tmp_path)), crs_authid="EPSG:32617",
        has_bathymetry=True, node_count=9, element_count=8,
        bbox=(0.0, 0.0, 20.0, 20.0), utm_epsg=32617, **over)
    return {"artifact": art, "display_uri": art.display_uri}


@pytest.fixture
def staged_beside(monkeypatch, tmp_path):
    out = tmp_path / "staged"
    out.mkdir()

    def _copy(mesh_id, local):
        dest = out / f"{mesh_id}-{Path(local).name}"
        shutil.copy(local, dest)
        return str(dest)

    monkeypatch.setattr(MF, "_stage", _copy)
    return out


@pytest.mark.asyncio
async def test_the_pair_is_written_from_the_mesh_and_recorded_under_its_keywords(
        staged_beside, tmp_path):
    mesh = _record(tmp_path, boundary_roles={"open": [0, 1, 2]})
    out = await MF.telemac_mesh_files(mesh=mesh)
    files = out["engine_files"]
    assert set(files) == {GEOMETRY_FILE, BOUNDARY_CONDITIONS_FILE}
    assert all(Path(uri).is_file() for uri in files.values())
    assert mesh["artifact"].engine_files == files
    assert out["topology"]["liquid_boundary_order"] == ["open"]
    assert out["topology"]["liquid_boundary_prescribes"] == ["elevation"]


@pytest.mark.asyncio
async def test_a_mesh_naming_no_open_stretch_measures_a_closed_basin(
        staged_beside, tmp_path):
    out = await MF.telemac_mesh_files(mesh=_record(tmp_path))
    assert "names no liquid boundary" in out["topology"]["states"]


@pytest.mark.asyncio
async def test_a_mesh_already_carrying_the_pair_is_walked_and_not_rewritten(
        staged_beside, tmp_path):
    held = {GEOMETRY_FILE: "s3://m/M01/mesh.slf",
            BOUNDARY_CONDITIONS_FILE: "s3://m/M01/mesh.cli"}
    out = await MF.telemac_mesh_files(mesh=_record(
        tmp_path, engine_files=dict(held), boundary_roles={"open": [0, 1, 2]}))
    assert out["engine_files"] == held
    assert not list(staged_beside.iterdir())
    assert out["topology"]["liquid_boundary_order"] == ["open"]


@pytest.mark.asyncio
async def test_nowhere_to_stage_the_pair_refuses_by_name(monkeypatch, tmp_path):
    monkeypatch.delenv("TRID3NT_CACHE_BUCKET", raising=False)
    with pytest.raises(TelemacError) as excinfo:
        await MF.telemac_mesh_files(mesh=_record(tmp_path))
    assert excinfo.value.error_code == "TELEMAC_MESH_NOT_ACCEPTED"
    assert "TRID3NT_CACHE_BUCKET" in str(excinfo.value)
