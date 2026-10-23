"""The Aquifer coercion: a one-layer aquifer's cells stated from rasters, evaluated per cell.

Offline: the mesh is a 20 m lattice in UTM 32617 with a bed that slopes (four squares,
eight triangles; one raster pixel per square); the upload is replaced by a copy beside it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.runtime.sheet import fill

_POINTS = [(x, y) for y in (0.0, 10.0, 20.0) for x in (0.0, 10.0, 20.0)]
_CELLS = [tri for r in range(2) for c in range(2)
          for a in [r * 3 + c] for tri in ((a, a + 1, a + 4), (a, a + 4, a + 3))]
_BED = [5.0 + 0.1 * x + 0.2 * y for x, y in _POINTS]
_TOP = [sum(_BED[v] for v in cell) / 3.0 for cell in _CELLS]
# One pixel per square, painted from the top row down: the square (r, c) reads [1 - r][c].
_THICKNESS = [[10.0, 20.0], [30.0, 40.0]]
_DEPTH = [[1.0, 2.0], [3.0, 4.0]]
_TRANSMISSIVITY = [[100.0, 100.0], [300.0, 800.0]]


def _per_cell(pixels):
    return [pixels[1 - cell // 4][(cell // 2) % 2] for cell in range(8)]


def _lattice(tmp_path: Path) -> Path:
    rows = ["MESH2D"]
    for cell, tri in enumerate(_CELLS, start=1):
        rows.append(f"E3T {cell} {tri[0] + 1} {tri[1] + 1} {tri[2] + 1} 1")
    for node, ((x, y), z) in enumerate(zip(_POINTS, _BED), start=1):
        rows.append(f"ND {node} {x} {y} {z}")
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


def _raster(path: Path, pixels, units: str | None, name: str):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    with rasterio.open(
            path, "w", driver="GTiff", height=2, width=2, count=1,
            dtype="float32", crs="EPSG:32617",
            transform=from_origin(0.0, 20.0, 10.0, 10.0)) as dst:
        dst.write(np.asarray(pixels, dtype="float32"), 1)
    return {"uri": str(path), "name": name, **({"units": units} if units else {})}


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


def _inputs(tmp_path, *, thickness="m", depth="m", transmissivity="m2/day",
            thickness_pixels=_THICKNESS):
    return {
        "thick": _raster(tmp_path / "thick.tif", thickness_pixels, thickness, "thickness"),
        "depth": _raster(tmp_path / "depth.tif", _DEPTH, depth, "depth"),
        "trans": _raster(tmp_path / "trans.tif", _TRANSMISSIVITY, transmissivity, "trans"),
        "cond": _raster(tmp_path / "cond.tif", _DEPTH, "m/day", "cond")}


def _fill(inputs, aquifer, **stated):
    return fill(GWF, produced=inputs, **{
        "NPF_ICELLTYPE": 0, "DISV_LENGTH_UNITS": "meters", "TDIS_TIME_UNITS": "days",
        "TDIS_NPER": 1, "TDIS_PERIODDATA": [[10.0, 1, 1.0]],
        "Aquifer": aquifer, **stated})


_FULL = {"thickness": "thick", "water_table_depth": "depth",
         "transmissivity": "trans"}


def _layering(sheet, tmp_path):
    from trid3nt_server.workflows.modflow.authoring.accepted_mesh import mesh_nodes

    mesh = _mesh(tmp_path)
    points, cells, bed, _ = mesh_nodes(mesh)
    return MF._layering(sheet, nlay=1, art=mesh["artifact"], bed=bed,
                        points=points, cells=cells)


def test_the_composite_states_a_one_layer_aquifer_with_no_number_computed(tmp_path):
    sheet = _fill(_inputs(tmp_path), _FULL)
    assert sheet.filled["DISV_NLAY"].value == 1
    assert sheet.filled["DISV_BOTM"].value["cells"] == "below_top"
    assert sheet.filled["IC_STRT"].value["cells"] == "below_top"
    assert sheet.filled["NPF_K"].value["cells"] == "quotient"
    assert "NPF_ICELLTYPE" in sheet.filled and "STO_SY" not in sheet.filled


def test_a_stated_layer_count_splits_the_aquifer_and_confines_all_but_the_top(tmp_path):
    sheet = _fill(_inputs(tmp_path), {**_FULL, "layers": 3})
    assert sheet.filled["DISV_NLAY"].value == 3
    # Only layer 1 is convertible; the layers below it are confined.
    assert sheet.filled["NPF_ICELLTYPE"].value == [1, 0, 0]
    # The bottoms split the total depth into three equal layers.
    from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place

    top, _, _ = _layering(sheet, tmp_path)
    botm = place(sheet.filled["DISV_BOTM"].value, slot=sheet.filled["DISV_BOTM"].slot,
                 nlay=3, run=sheet.run, points=_POINTS, cells=_CELLS, utm_epsg=32617,
                 frame=frame_of(sheet), top=top)
    total = _per_cell(_THICKNESS)
    assert botm == [
        pytest.approx([t - d / 3 for t, d in zip(top, total)]),
        pytest.approx([t - 2 * d / 3 for t, d in zip(top, total)]),
        pytest.approx([t - d for t, d in zip(top, total)])]


def test_a_layer_count_below_one_refuses_naming_it(tmp_path):
    with pytest.raises(ModflowError) as caught:
        _fill(_inputs(tmp_path), {**_FULL, "layers": 0})
    assert caught.value.error_code == "MODFLOW_AQUIFER_UNSTATED"
    assert "layers" in str(caught.value)


def test_a_non_integer_layer_count_refuses(tmp_path):
    with pytest.raises(ModflowError) as caught:
        _fill(_inputs(tmp_path), {**_FULL, "layers": 2.5})
    assert caught.value.error_code == "MODFLOW_AQUIFER_UNSTATED"


def test_a_stated_conductivity_is_the_raster_itself(tmp_path):
    sheet = _fill(_inputs(tmp_path), {"thickness": "thick", "water_table_depth": "depth",
                                       "conductivity": "cond"})
    assert sheet.filled["NPF_K"].value["cells"] == "raster"


@pytest.mark.parametrize("argument", ["thickness", "water_table_depth"])
@pytest.mark.parametrize("dropped", [True, False])
def test_a_missing_argument_refuses_naming_it(tmp_path, argument, dropped):
    body = dict(_FULL)
    body.pop(argument) if dropped else body.update({argument: None})
    with pytest.raises(ModflowError) as caught:
        _fill(_inputs(tmp_path), body)
    assert caught.value.error_code == "MODFLOW_AQUIFER_UNSTATED"
    assert argument in str(caught.value)


def test_neither_conductivity_nor_transmissivity_refuses(tmp_path):
    with pytest.raises(ModflowError) as caught:
        _fill(_inputs(tmp_path), {"thickness": "thick", "water_table_depth": "depth"})
    assert caught.value.error_code == "MODFLOW_AQUIFER_UNSTATED"
    assert "neither" in str(caught.value)


def test_both_conductivity_and_transmissivity_refuses(tmp_path):
    with pytest.raises(ModflowError) as caught:
        _fill(_inputs(tmp_path), {**_FULL, "conductivity": "cond"})
    assert caught.value.error_code == "MODFLOW_AQUIFER_UNSTATED"
    assert "both" in str(caught.value)


def test_an_argument_the_composite_does_not_take_refuses(tmp_path):
    with pytest.raises(ModflowError) as caught:
        _fill(_inputs(tmp_path), {**_FULL, "porosity": "cond"})
    assert caught.value.error_code == "MODFLOW_AQUIFER_UNSTATED"
    assert "porosity" in str(caught.value)


def test_the_bottom_lies_a_thickness_below_the_top_and_the_head_a_depth_below_it(tmp_path):
    sheet = _fill(_inputs(tmp_path), _FULL)
    top, botm, _ = _layering(sheet, tmp_path)
    assert top == pytest.approx(_TOP)
    assert botm == [pytest.approx([t - d for t, d in zip(_TOP, _per_cell(_THICKNESS))])]

    from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place

    strt = place(sheet.filled["IC_STRT"].value, slot=sheet.filled["IC_STRT"].slot,
                 nlay=1, run=sheet.run, points=_POINTS, cells=_CELLS, utm_epsg=32617,
                 frame=frame_of(sheet), top=top)
    assert strt == [pytest.approx([t - d for t, d in zip(_TOP, _per_cell(_DEPTH))])]


def test_a_stated_top_is_the_one_the_bottom_lies_below(tmp_path):
    sheet = _fill(_inputs(tmp_path), _FULL, DISV_TOP=50.0)
    top, botm, _ = _layering(sheet, tmp_path)
    assert top == 50.0
    assert botm == [pytest.approx([50.0 - d for d in _per_cell(_THICKNESS)])]


def test_a_transmissivity_over_a_thickness_is_the_conductivity_per_cell(tmp_path):
    from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place

    sheet = _fill(_inputs(tmp_path), _FULL)
    k = place(sheet.filled["NPF_K"].value, slot=sheet.filled["NPF_K"].slot, nlay=1,
              run=sheet.run, points=_POINTS, cells=_CELLS, utm_epsg=32617,
              frame=frame_of(sheet))
    assert k == [pytest.approx([t / h for t, h in zip(
        _per_cell(_TRANSMISSIVITY), _per_cell(_THICKNESS))])]


def test_feet_squared_over_feet_onto_a_metre_model_gives_the_same_conductivity(tmp_path):
    from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place

    in_feet = _inputs(tmp_path, thickness="ft", transmissivity="ft2/day",
                      thickness_pixels=[[v / 0.3048 for v in row] for row in _THICKNESS])
    in_feet["trans"] = _raster(tmp_path / "trans_ft.tif",
                               [[v / 0.09290304 for v in row] for row in _TRANSMISSIVITY],
                               "ft2/day", "trans")
    sheet = _fill(in_feet, _FULL)
    k = place(sheet.filled["NPF_K"].value, slot=sheet.filled["NPF_K"].slot, nlay=1,
              run=sheet.run, points=_POINTS, cells=_CELLS, utm_epsg=32617,
              frame=frame_of(sheet))
    assert k == [pytest.approx([t / h for t, h in zip(
        _per_cell(_TRANSMISSIVITY), _per_cell(_THICKNESS))], rel=1e-5)]


def test_a_thickness_at_or_below_zero_refuses_naming_the_count(tmp_path):
    inputs = _inputs(tmp_path, thickness_pixels=[[10.0, 0.0], [-3.0, 40.0]])
    sheet = _fill(inputs, _FULL)
    with pytest.raises(ModflowError) as caught:
        _layering(sheet, tmp_path)
    assert caught.value.error_code == "MODFLOW_AQUIFER_THICKNESS"
    assert "4 of 8" in str(caught.value)


def test_a_thickness_at_or_below_zero_refuses_the_conductivity_too(tmp_path):
    from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place

    sheet = _fill(_inputs(tmp_path, thickness_pixels=[[10.0, 0.0], [30.0, 40.0]]), _FULL)
    with pytest.raises(ModflowError) as caught:
        place(sheet.filled["NPF_K"].value, slot=sheet.filled["NPF_K"].slot, nlay=1,
              run=sheet.run, points=_POINTS, cells=_CELLS, utm_epsg=32617,
              frame=frame_of(sheet))
    assert caught.value.error_code == "MODFLOW_AQUIFER_THICKNESS"


def test_a_raster_without_a_unit_refuses(tmp_path):
    sheet = _fill(_inputs(tmp_path, thickness=None), _FULL)
    with pytest.raises(ModflowError) as caught:
        _layering(sheet, tmp_path)
    assert caught.value.error_code == "MODFLOW_UNITS_UNSTATED"


def test_cells_below_the_top_on_a_grid_of_two_layers_split_equally(tmp_path):
    from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place

    sheet = _fill(_inputs(tmp_path), _FULL)
    botm = place(sheet.filled["DISV_BOTM"].value, slot=sheet.filled["DISV_BOTM"].slot,
                 nlay=2, run=sheet.run, points=_POINTS, cells=_CELLS, utm_epsg=32617,
                 frame=frame_of(sheet), top=50.0)
    # Two equal layers of the total depth: layer 1's bottom is half the depth below the top,
    # layer 2's bottom is the full depth.
    total = _per_cell(_THICKNESS)
    assert botm == [
        pytest.approx([50.0 - d / 2 for d in total]),
        pytest.approx([50.0 - d for d in total])]


def test_the_lattice_with_an_aquifer_recharge_and_drains_serializes(
        staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    inputs = _inputs(tmp_path)
    inputs["recharge"] = _raster(tmp_path / "rch.tif", [[365.25] * 2] * 2, "mm/yr",
                                 "recharge")
    inputs["where"] = {"type": "Polygon", "coordinates": [[
        list(p) for p in _lonlat_ring()]]}
    sheet = _fill(inputs, _FULL, Recharge={"geometry": "where", "layer": 1,
                                            "rate": "recharge"},
                  Drains={"geometry": "where", "layer": 1, "elevation": 1.0,
                          "conductance": 50.0})
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir()
    shutil.copy(out["disv"], rundir / "mesh.disv")

    written = serialize(sheet, rundir, mesh)["written"]

    assert {"gwf.rch", "gwf.drn", "gwf.ic", "gwf.npf"} <= set(written)
    rch = (rundir / "gwf.rch").read_text().split("BEGIN period  1")[1]
    rates = [float(row.split()[-1]) for row in rch.splitlines() if row.strip().startswith("1")]
    assert rates == [pytest.approx(1.0e-3)] * 8
    drn = (rundir / "gwf.drn").read_text().split("BEGIN period  1")[1]
    assert [float(row.split()[-1]) for row in drn.splitlines()
            if row.strip().startswith("1")] == [50.0] * 8
    assert _array(rundir / "mesh.disv", "botm") == pytest.approx(
        [t - d for t, d in zip(_TOP, _per_cell(_THICKNESS))], rel=1e-6)
    assert _array(rundir / "gwf.ic", "strt") == pytest.approx(
        [t - d for t, d in zip(_TOP, _per_cell(_DEPTH))], rel=1e-6)
    assert _array(rundir / "gwf.npf", "k") == pytest.approx(
        [t / h for t, h in zip(_per_cell(_TRANSMISSIVITY), _per_cell(_THICKNESS))],
        rel=1e-6)


def _array(path, name):
    lines = path.read_text().splitlines()
    start = next(i for i, row in enumerate(lines) if row.strip() == name)
    return [float(v) for v in lines[start + 2].split()]


def _lonlat_ring():
    from pyproj import Transformer

    to = Transformer.from_crs(32617, 4326, always_xy=True).transform
    return [to(*p) for p in ((-1.0, -1.0), (21.0, -1.0), (21.0, 21.0),
                             (-1.0, 21.0), (-1.0, -1.0))]


def test_stated_numbers_are_the_model_frame_s_own_at_every_cell(tmp_path):
    sheet = _fill({}, {"thickness": 30.0, "water_table_depth": 3.0,
                       "conductivity": 5.0})
    top, botm, _ = _layering(sheet, tmp_path)
    assert botm == [pytest.approx([t - 30.0 for t in top])]

    from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place

    k = place(sheet.filled["NPF_K"].value, slot=sheet.filled["NPF_K"].slot,
              nlay=1, run=sheet.run, points=_POINTS, cells=_CELLS, utm_epsg=32617,
              frame=frame_of(sheet), top=top)
    assert k == [[5.0] * len(_CELLS)]


def test_a_saturated_thickness_lies_below_the_water_table_not_the_land_surface(tmp_path):
    sheet = _fill({}, {"saturated_thickness": 28.0, "water_table_depth": 2.0,
                       "transmissivity": 140.0})
    top, botm, _ = _layering(sheet, tmp_path)
    assert botm == [pytest.approx([t - 2.0 - 28.0 for t in top])]

    from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place

    k = place(sheet.filled["NPF_K"].value, slot=sheet.filled["NPF_K"].slot,
              nlay=1, run=sheet.run, points=_POINTS, cells=_CELLS, utm_epsg=32617,
              frame=frame_of(sheet), top=top)
    assert k == [[pytest.approx(5.0)] * len(_CELLS)]


def test_layers_split_the_saturated_thickness_under_the_water_table(tmp_path):
    from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place

    sheet = _fill({}, {"saturated_thickness": 28.0, "water_table_depth": 2.0,
                       "transmissivity": 140.0, "layers": 2})
    top, _, _ = _layering(sheet, tmp_path)
    botm = place(sheet.filled["DISV_BOTM"].value, slot=sheet.filled["DISV_BOTM"].slot,
                 nlay=2, run=sheet.run, points=_POINTS, cells=_CELLS, utm_epsg=32617,
                 frame=frame_of(sheet), top=top)
    # The top layer holds the water table: it reaches half the saturated thickness under it.
    assert botm == [pytest.approx([t - 2.0 - 14.0 for t in top]),
                    pytest.approx([t - 2.0 - 28.0 for t in top])]
    head = place(sheet.filled["IC_STRT"].value, slot=sheet.filled["IC_STRT"].slot,
                 nlay=2, run=sheet.run, points=_POINTS, cells=_CELLS, utm_epsg=32617,
                 frame=frame_of(sheet), top=top)
    assert head == [pytest.approx([t - 2.0 for t in top])] * 2


@pytest.mark.parametrize("stated", [
    {"thickness": 30.0, "saturated_thickness": 28.0}, {}])
def test_one_thickness_and_only_one_is_stated(stated):
    with pytest.raises(ModflowError) as caught:
        _fill({}, {"water_table_depth": 2.0, "transmissivity": 140.0, **stated})
    assert caught.value.error_code == "MODFLOW_AQUIFER_UNSTATED"
    assert "saturated_thickness" in str(caught.value)


def _k_by_layer(sheet, nlay):
    from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place

    return place(sheet.filled["NPF_K"].value, slot=sheet.filled["NPF_K"].slot, nlay=nlay,
                 run=sheet.run, points=_POINTS, cells=_CELLS, utm_epsg=32617,
                 frame=frame_of(sheet))


def test_a_conductivity_list_lands_each_entry_on_its_own_layer(tmp_path):
    sheet = _fill(_inputs(tmp_path), {"thickness": "thick", "water_table_depth": "depth",
                                       "layers": 3, "conductivity": [5.0, 0.5, 0.05]})
    assert _k_by_layer(sheet, 3) == [[5.0] * 8, [0.5] * 8, [0.05] * 8]


def test_a_conductivity_list_of_rasters_samples_each_layer_from_its_own_raster(tmp_path):
    inputs = _inputs(tmp_path)
    inputs["deep"] = _raster(tmp_path / "deep.tif", [[10.0, 20.0], [30.0, 40.0]], "m/day", "deep")
    sheet = _fill(inputs, {"thickness": "thick", "water_table_depth": "depth",
                           "layers": 2, "conductivity": ["cond", "deep"]})
    assert _k_by_layer(sheet, 2) == [pytest.approx(_per_cell(_DEPTH)),
                                     pytest.approx(_per_cell([[10.0, 20.0], [30.0, 40.0]]))]


def test_a_conductivity_list_may_mix_a_number_and_a_raster(tmp_path):
    sheet = _fill(_inputs(tmp_path), {"thickness": "thick", "water_table_depth": "depth",
                                       "layers": 2, "conductivity": ["cond", 0.25]})
    assert _k_by_layer(sheet, 2) == [pytest.approx(_per_cell(_DEPTH)), [0.25] * 8]


def test_a_conductivity_list_longer_or_shorter_than_the_layers_refuses_naming_both(tmp_path):
    for listed in ([1.0, 2.0], [1.0, 2.0, 3.0, 4.0]):
        with pytest.raises(ModflowError) as caught:
            _fill(_inputs(tmp_path), {"thickness": "thick", "water_table_depth": "depth",
                                       "layers": 3, "conductivity": listed})
        assert caught.value.error_code == "MODFLOW_AQUIFER_UNSTATED"
        assert f"lists {len(listed)}" in str(caught.value) and "layers is 3" in str(caught.value)


def test_a_conductivity_list_with_no_layer_count_is_one_layer_and_refuses_two_values(tmp_path):
    with pytest.raises(ModflowError) as caught:
        _fill(_inputs(tmp_path), {"thickness": "thick", "water_table_depth": "depth",
                                   "conductivity": [1.0, 2.0]})
    assert "layers is 1" in str(caught.value)


def test_a_transmissivity_list_refuses_by_name(tmp_path):
    with pytest.raises(ModflowError) as caught:
        _fill(_inputs(tmp_path), {**_FULL, "layers": 2, "transmissivity": ["trans", "trans"]})
    assert caught.value.error_code == "MODFLOW_AQUIFER_UNSTATED"
    assert "transmissivity" in str(caught.value)


def test_a_single_conductivity_still_repeats_on_every_layer(tmp_path):
    sheet = _fill(_inputs(tmp_path), {"thickness": "thick", "water_table_depth": "depth",
                                       "layers": 2, "conductivity": 3.0})
    assert sheet.filled["NPF_K"].value["cells"] == "raster"
    assert _k_by_layer(sheet, 2) == [[3.0] * 8, [3.0] * 8]


def test_a_two_layer_list_serializes_one_k_array_per_layer_top_first(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill(_inputs(tmp_path), {"thickness": "thick", "water_table_depth": "depth",
                                       "layers": 2, "conductivity": [4.0, 0.4]})
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir()
    shutil.copy(out["disv"], rundir / "mesh.disv")

    serialize(sheet, rundir, mesh)

    lines = (rundir / "gwf.npf").read_text().splitlines()
    start = next(i for i, row in enumerate(lines) if row.split()[:2] == ["k", "LAYERED"])
    rows = [[float(v) for v in lines[start + at].split()] for at in (2, 4)]
    assert rows == [[4.0] * 8, [0.4] * 8]
    assert not any(row.strip().startswith("k33") for row in lines)
