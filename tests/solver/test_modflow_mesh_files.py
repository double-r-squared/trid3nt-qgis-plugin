"""The DISV file MODFLOW asks an accepted mesh for, written by its own author step.

Offline: the mesh is a lattice on disk, and the upload is replaced by a copy beside it.
Pinned: the file is written from the mesh's walk and the sheet's layering, recorded under
its key on the mesh, a layering already carried is not written again, and what the sheet
must state for a layering to exist refuses by name.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.runtime.sheet import fill


def _lattice(tmp_path: Path, *, bed: str = "-2.0") -> Path:
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
            rows.append(f"ND {node} {x} {y} {bed}")
    path = tmp_path / "mesh.2dm"
    path.write_text("\n".join(rows) + "\n")
    return path


def _mesh(tmp_path: Path, **over) -> dict:
    art = MeshArtifact(
        mesh_id="M01", name="lattice", mode="om2d",
        display_uri=str(_lattice(tmp_path, bed=str(over.pop("bed", "-2.0")))),
        crs_authid="EPSG:32617",
        has_bathymetry=over.pop("has_bathymetry", True),
        node_count=9, element_count=8,
        bbox=(0.0, 0.0, 20.0, 20.0), utm_epsg=32617, **over)
    return {"artifact": art, "display_uri": art.display_uri}


def _sheet(nlay: int = 2):
    return fill(GWF, DISV_NLAY=nlay,
                DISV_BOTM=[[-10.0] * 8 for _ in range(nlay)])


@pytest.fixture
def staged_beside(monkeypatch, tmp_path):
    out = tmp_path / "staged"
    out.mkdir()

    def _copy(mesh_id, local):
        dest = out / local.name
        shutil.copy(local, dest)
        return str(dest)

    monkeypatch.setattr(MF, "_stage", _copy)
    return out


@pytest.mark.asyncio
async def test_the_disv_is_written_from_the_mesh_and_recorded_under_its_key(
        staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    out = await MF.modflow_mesh_files(sheet=_sheet(), mesh=mesh)

    files = out["engine_files"]
    assert len(files) == 1
    key = next(iter(files))
    assert key.startswith("disv-") and len(key) == len("disv-") + 16
    assert out["disv"] == files[key] and Path(files[key]).is_file()
    text = Path(files[key]).read_text()
    assert "NLAY  2" in text and "NCPL  8" in text
    assert mesh["artifact"].engine_files == files


@pytest.mark.asyncio
async def test_a_second_run_on_the_same_layering_reads_the_record_it_left(
        staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    first = await MF.modflow_mesh_files(sheet=_sheet(), mesh=mesh)
    second = await MF.modflow_mesh_files(sheet=_sheet(), mesh=mesh)

    assert second["engine_files"] == first["engine_files"]
    assert list(staged_beside.iterdir()) == [Path(first["disv"])]


@pytest.mark.asyncio
async def test_another_layering_on_the_same_mesh_writes_its_own_file(
        staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    two = await MF.modflow_mesh_files(sheet=_sheet(2), mesh=mesh)
    three = await MF.modflow_mesh_files(sheet=_sheet(3), mesh=mesh)

    assert len(two["engine_files"]) == 1 and len(three["engine_files"]) == 2
    assert set(two["engine_files"]) <= set(three["engine_files"])
    assert three["disv"] != two["disv"]
    assert len(list(staged_beside.iterdir())) == 2


@pytest.mark.asyncio
async def test_nowhere_to_stage_the_disv_refuses_by_name(monkeypatch, tmp_path):
    monkeypatch.delenv("TRID3NT_CACHE_BUCKET", raising=False)
    with pytest.raises(ModflowError) as caught:
        await MF.modflow_mesh_files(sheet=_sheet(), mesh=_mesh(tmp_path))
    assert caught.value.error_code == "MODFLOW_MESH_NOT_ACCEPTED"
    assert "TRID3NT_CACHE_BUCKET" in str(caught.value)


@pytest.mark.asyncio
async def test_a_run_with_no_accepted_mesh_refuses_by_name(staged_beside, tmp_path):
    with pytest.raises(ModflowError) as caught:
        await MF.modflow_mesh_files(sheet=_sheet(), mesh={})
    assert caught.value.error_code == "MODFLOW_MESH_NOT_ACCEPTED"


@pytest.mark.asyncio
async def test_a_sheet_stating_the_mesh_own_tables_refuses(staged_beside, tmp_path):
    sheet = fill(GWF, DISV_NLAY=2,
                 DISV_BOTM=[[-10.0] * 8, [-20.0] * 8],
                 DISV_VERTICES=[[0, 0.0, 0.0], [1, 1.0, 0.0]])
    with pytest.raises(ModflowError) as caught:
        await MF.modflow_mesh_files(sheet=sheet, mesh=_mesh(tmp_path))
    assert caught.value.error_code == "MODFLOW_MESH_AUTHORED"
    assert "DISV_VERTICES" in str(caught.value)


@pytest.mark.asyncio
async def test_a_sheet_stating_no_layers_refuses(staged_beside, tmp_path):
    with pytest.raises(ModflowError) as caught:
        await MF.modflow_mesh_files(sheet=fill(GWF), mesh=_mesh(tmp_path))
    assert caught.value.error_code == "MODFLOW_LAYERING_UNSTATED"
    assert "DISV_NLAY" in str(caught.value)


@pytest.mark.asyncio
async def test_a_sheet_stating_no_bottom_refuses(staged_beside, tmp_path):
    with pytest.raises(ModflowError) as caught:
        await MF.modflow_mesh_files(sheet=fill(GWF, DISV_NLAY=2),
                                    mesh=_mesh(tmp_path))
    assert caught.value.error_code == "MODFLOW_LAYERING_UNSTATED"
    assert "DISV_BOTM" in str(caught.value)


@pytest.mark.asyncio
async def test_a_mesh_whose_bed_is_no_paint_refuses_a_derived_top(
        staged_beside, tmp_path):
    with pytest.raises(ModflowError) as caught:
        await MF.modflow_mesh_files(
            sheet=_sheet(), mesh=_mesh(tmp_path, has_bathymetry=False))
    assert caught.value.error_code == "MODFLOW_LAYERING_UNSTATED"
    assert "DISV_TOP" in str(caught.value)


@pytest.mark.asyncio
async def test_a_cell_touching_a_hole_in_the_bed_refuses(staged_beside, tmp_path):
    mesh = _mesh(tmp_path, bed="-2.0")
    path = Path(mesh["artifact"].display_uri)
    path.write_text(path.read_text().replace(" -2.0", " nan", 1))

    with pytest.raises(ModflowError) as caught:
        await MF.modflow_mesh_files(sheet=_sheet(), mesh=mesh)
    assert caught.value.error_code == "MODFLOW_TOP_UNMEASURED"
    assert "2 of 8" in str(caught.value)
