"""What a fill and an accepted mesh hand the MODFLOW author, and the directory it writes.

Offline: the mesh is a lattice on disk and the upload is replaced by a copy beside it.
Pinned: one GRIDDATA statement becomes the array the constructor takes and refuses the
shape it cannot be, the simulation is written around the staged DISV without touching
its bytes, and the validate caller hands the directory to the image's own mf6 and
refuses on mf6's words.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.runtime.sheet import Sheet, fill

#: One square cell's worth of mesh the placements place against: two triangles.
_POINTS = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0),
           (20.0, 0.0), (20.0, 10.0)]
_CELLS = [(0, 1, 2), (0, 2, 3), (1, 4, 5), (1, 5, 2)]


def _lattice(tmp_path: Path) -> Path:
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


def _mesh(tmp_path: Path) -> dict:
    art = MeshArtifact(
        mesh_id="M01", name="lattice", mode="om2d",
        display_uri=str(_lattice(tmp_path)), crs_authid="EPSG:32617",
        has_bathymetry=True, node_count=9, element_count=8,
        bbox=(0.0, 0.0, 20.0, 20.0), utm_epsg=32617)
    return {"artifact": art, "display_uri": art.display_uri}


def _sheet(**stated):
    return fill(GWF, **{
        "DISV_NLAY": 2, "DISV_BOTM": [[-10.0] * 8, [-20.0] * 8],
        "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0, "IC_STRT": 0.0,
        "TDIS_NPER": 1, "TDIS_TIME_UNITS": "seconds",
        "TDIS_PERIODDATA": [[600.0, 1, 600.0]], **stated})


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


def _place(value, *, name="NPF_K", nlay=2, run=None, cells=_CELLS, utm=None):
    from trid3nt_server.workflows.modflow.authoring.griddata import place

    return place(value, slot=GWF.slot(name), nlay=nlay,
                 run=run if run is not None else {}, points=_POINTS,
                 cells=cells, utm_epsg=utm, frame=("meters", "days"))


def test_a_constant_passes_through():
    assert _place(5.5) == 5.5


def test_one_number_per_layer_fits_the_dfn_shape():
    assert _place([10.0, 5.0]) == [10.0, 5.0]


def test_a_layered_list_of_the_wrong_length_refuses_by_name():
    with pytest.raises(ModflowError) as caught:
        _place([1.0, 2.0, 3.0])
    assert caught.value.error_code == "MODFLOW_GRIDDATA_SHAPE"
    assert "2 of them" in str(caught.value)


def test_one_number_per_cell_on_a_flat_array_fits_the_cells():
    assert _place([1.0, 2.0, 3.0, 4.0], name="DISV_TOP", nlay=2) == [
        1.0, 2.0, 3.0, 4.0]


def test_a_cell_count_the_mesh_does_not_carry_refuses_by_name():
    with pytest.raises(ModflowError) as caught:
        _place([1.0, 2.0, 3.0], name="DISV_TOP", nlay=2)
    assert caught.value.error_code == "MODFLOW_GRIDDATA_SHAPE"
    assert "4 cell(s)" in str(caught.value)


def test_a_nested_row_per_layer_is_the_rows_themselves():
    rows = [[1.0] * 4, [2.0] * 4]
    assert _place(rows) == rows


def test_a_row_that_does_not_span_the_cells_refuses_by_name():
    with pytest.raises(ModflowError) as caught:
        _place([[1.0] * 3, [2.0] * 4])
    assert caught.value.error_code == "MODFLOW_GRIDDATA_SHAPE"
    assert "row of 3" in str(caught.value)


def test_an_empty_or_mixed_list_refuses_by_name():
    for value in ([], [1.0, "two"]):
        with pytest.raises(ModflowError) as caught:
            _place(value, name="DISV_TOP", nlay=2)
        assert caught.value.error_code == "MODFLOW_GRIDDATA_SHAPE"


def test_a_name_no_input_of_this_run_carries_refuses_by_name():
    with pytest.raises(ModflowError) as caught:
        _place("bed_raster")
    assert caught.value.error_code == "MODFLOW_GRIDDATA_NO_INPUT"
    assert "bed_raster" in str(caught.value)


def test_a_name_sampled_with_no_mesh_refuses_by_name():
    from trid3nt_server.workflows.modflow.authoring.griddata import place

    with pytest.raises(ModflowError) as caught:
        place("bed_raster", slot=GWF.slot("NPF_K"), nlay=2,
              run={"bed_raster": "somewhere.tif"})
    assert caught.value.error_code == "MODFLOW_GRIDDATA_NO_MESH"


def _raster(path: Path, *, value: float = 77.0, width: int = 2,
            height: int = 2) -> Path:
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    with rasterio.open(
            path, "w", driver="GTiff", height=height, width=width, count=1,
            dtype="float32", crs="EPSG:32617",
            transform=from_origin(0.0, 20.0, 10.0, 10.0)) as dst:
        dst.write(np.full((height, width), value, dtype="float32"), 1)
    return path


def test_a_named_raster_is_sampled_at_the_cells_and_tiled_by_layer(tmp_path):
    raster = _raster(tmp_path / "k.tif")

    placed = _place("k", run={"k": {"uri": str(raster), "units": "m/day"}},
                    utm=32617)

    assert placed == [[77.0] * len(_CELLS), [77.0] * len(_CELLS)]


def test_a_raster_that_measures_nothing_at_a_cell_refuses(tmp_path):
    raster = _raster(tmp_path / "k.tif", width=1, height=1)

    with pytest.raises(ModflowError) as caught:
        _place("k", run={"k": {"uri": str(raster), "units": "m/day"}}, utm=32617)
    assert caught.value.error_code == "MODFLOW_GRIDDATA_UNMEASURED"


# ---- the simulation directory ------------------------------------------------


def test_the_directory_is_written_around_the_staged_disv_and_checked(
        staged_beside, tmp_path, monkeypatch):
    """The leg's prove path: mesh, fill, serialize, then the check over it."""
    mesh = _mesh(tmp_path)
    sheet = _sheet()
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir()
    shutil.copy(out["disv"], rundir / "mesh.disv")
    staged_bytes = (rundir / "mesh.disv").read_bytes()

    written = serialize(sheet, rundir, mesh)["written"]

    assert (rundir / "mesh.disv").read_bytes() == staged_bytes
    assert set(written) == {"gwf.ic", "gwf.ims", "gwf.nam", "gwf.npf",
                            "mfsim.nam", "trid3nt.tdis"}
    assert "mesh.disv" in (rundir / "gwf.nam").read_text()

    from trid3nt_server.workflows.modflow.authoring.staged_check import (
        check_staged_run)

    read = check_staged_run(
        rundir, steering="mfsim.nam",
        inputs=[{"gs_uri": out["disv"], "dest": "mesh.disv"}],
        written_by_the_engine=written, duration_s=600.0, cores=1)
    assert read["digested"] == ["mesh.disv"]
    assert read["tdis_total_s"] == 600.0


def test_a_required_keyword_nobody_stated_refuses_by_name(staged_beside, tmp_path):
    sheet = _sheet()
    filled = {k: v for k, v in sheet.filled.items() if k != "NPF_K"}
    with pytest.raises(ModflowError) as caught:
        serialize(Sheet(body=GWF, filled=filled), tmp_path / "run",
                  _mesh(tmp_path))
    assert caught.value.error_code == "MODFLOW_KEYWORD_UNSTATED"
    assert "NPF_K" in str(caught.value)


def test_a_stated_package_s_required_grid_data_is_stated_too(staged_beside, tmp_path):
    with pytest.raises(ModflowError) as caught:
        serialize(_sheet(STO_SS=1.0e-5), tmp_path / "run", _mesh(tmp_path))
    assert caught.value.error_code == "MODFLOW_KEYWORD_UNSTATED"
    assert "STO_ICONVERT" in str(caught.value) and "STO_SY" in str(caught.value)


def test_a_sheet_stating_another_discretization_refuses(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = fill(GWF, DIS_NLAY=3, DISV_NLAY=2,
                 DISV_BOTM=[[-10.0] * 8, [-20.0] * 8])
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, tmp_path / "run", mesh)
    assert caught.value.error_code == "MODFLOW_WRONG_GRID"


def test_a_sheet_stating_the_name_file_s_file_list_refuses(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = fill(GWF, NAM_PACKAGES=[["DISV6", "mesh.disv", "disv"]],
                 DISV_NLAY=2, DISV_BOTM=[[-10.0] * 8, [-20.0] * 8])
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, tmp_path / "run", mesh)
    assert caught.value.error_code == "MODFLOW_NAM_PACKAGES"


def test_a_module_outside_the_three_refuses(staged_beside, tmp_path):
    class Other:
        MODULE = "prg"

    sheet = Sheet(body=Other, filled={})
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, tmp_path / "run", _mesh(tmp_path))
    assert caught.value.error_code == "MODFLOW_MODULE_UNKNOWN"


def test_a_bare_identifier_refuses_by_name(staged_beside, tmp_path):
    sheet = Sheet(body=GWF, filled={"BARE": None})
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, tmp_path / "run", _mesh(tmp_path))
    assert caught.value.error_code == "MODFLOW_KEYWORD_SHAPE"


# ---- the validate caller ------------------------------------------------------


def _cp(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["docker"], returncode,
                                       stdout=stdout, stderr=stderr)


def test_the_validate_caller_hands_the_directory_to_the_images_own_mf6(
        monkeypatch, tmp_path):
    from trid3nt_server.workflows.modflow.authoring import validate as V

    seen: dict = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return _cp(0, stdout="MODFLOW 6.7.0\nNormal termination of simulation.\n")

    monkeypatch.setattr(V, "run_image_script", fake)
    assert V.validate_authored_simulation(tmp_path) == {"rc": 0}
    assert seen["engine"] == "modflow" and seen["script"] == "validate.py"
    assert seen["argv"] == [] and seen["rundir"] == tmp_path
    assert seen["image"] == V._modflow_image()


def test_a_parser_that_refuses_the_directory_carries_its_own_words(
        monkeypatch, tmp_path):
    from trid3nt_server.workflows.modflow.authoring import validate as V

    monkeypatch.setattr(
        V, "run_image_script",
        lambda **_: _cp(1, stdout="ERROR: mesh.disv File ... not found"))
    with pytest.raises(ModflowError) as caught:
        V.validate_authored_simulation(tmp_path)
    assert caught.value.error_code == "MODFLOW_VALIDATE_FAILED"
    assert "mesh.disv" in str(caught.value)


def test_a_clean_exit_that_never_finished_refuses(monkeypatch, tmp_path):
    from trid3nt_server.workflows.modflow.authoring import validate as V

    monkeypatch.setattr(V, "run_image_script", lambda **_: _cp(0, stdout=""))
    with pytest.raises(ModflowError) as caught:
        V.validate_authored_simulation(tmp_path)
    assert caught.value.error_code == "MODFLOW_VALIDATE_FAILED"
