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


_VERTICAL = {"vertical_frame": "NAVD88"}
_DATUM = "NAVD88 (metres, positive up)"


def _by_depth(source_k, source_s, **aquifer):
    return _fill({}, {"thickness": 30.0, "water_table_depth": 3.0,
                      "conductivity": {"layers": source_k, "surfaces": source_s}, **aquifer})


def _depth_k(sheet, *, tops, botms, run=None):
    """NPF_K placed against a grid whose top and bottoms are stated, every cell alike."""
    from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place

    return place(sheet.filled["NPF_K"].value, slot=sheet.filled["NPF_K"].slot,
                 nlay=len(botms), run={**sheet.run, **(_VERTICAL if run is None else run)},
                 points=_POINTS, cells=_CELLS, utm_epsg=32617, frame=frame_of(sheet),
                 top=[tops] * 8, botm=[[b] * 8 for b in botms])


@pytest.mark.parametrize("top, bottom, expected", [
    # 5 m in the 10 m/d layer and 5 m in the 1 m/d layer: (5*10 + 5*1) / 10.
    (95.0, 85.0, 5.5),
    # Wholly inside the lower source layer.
    (90.0, 70.0, 1.0),
    # 5 m of overlap above 100 is none: only the 5 m under 100 and over 90 counts.
    (105.0, 95.0, 10.0)])
def test_a_layer_takes_the_thickness_weighted_mean_of_the_source_layers_it_overlaps(
        top, bottom, expected):
    sheet = _by_depth([10.0, 1.0], [100.0, 90.0, 70.0])
    assert _depth_k(sheet, tops=top, botms=[bottom]) == [pytest.approx([expected] * 8)]


def test_each_grid_layer_is_read_between_the_bottom_above_it_and_its_own():
    sheet = _by_depth([10.0, 1.0], [100.0, 90.0, 70.0], layers=2)
    # Layer 2 lies 85 to 70: none of the 10 m/d layer, 15 m of the 1 m/d one.
    assert _depth_k(sheet, tops=95.0, botms=[85.0, 70.0]) == [
        pytest.approx([5.5] * 8), pytest.approx([1.0] * 8)]


def test_a_layer_below_the_source_refuses_naming_the_count_and_where():
    sheet = _by_depth([10.0, 1.0], [100.0, 90.0, 70.0])
    with pytest.raises(ModflowError) as caught:
        _depth_k(sheet, tops=65.0, botms=[55.0])
    assert caught.value.error_code == "MODFLOW_GRIDDATA_OUTSIDE_SOURCE"
    assert "layer 1" in str(caught.value) and "8 cell(s)" in str(caught.value)
    assert "8 below its bottom" in str(caught.value) and "0 above" in str(caught.value)


def test_a_layer_above_the_source_refuses_and_takes_no_neighbouring_value():
    sheet = _by_depth([10.0, 1.0], [100.0, 90.0, 70.0])
    with pytest.raises(ModflowError) as caught:
        _depth_k(sheet, tops=120.0, botms=[110.0])
    assert "8 above the source's top" in str(caught.value)


def test_a_deeper_grid_layer_than_the_source_refuses_naming_that_layer():
    sheet = _by_depth([10.0, 1.0], [100.0, 90.0, 70.0], layers=2)
    with pytest.raises(ModflowError) as caught:
        _depth_k(sheet, tops=95.0, botms=[70.0, 60.0])
    assert "layer 2" in str(caught.value) and "layer 1" not in str(caught.value)
    assert "0 above" in str(caught.value) and "8 below" in str(caught.value)


def _tif(path, pixels, *, name, units, datum=_DATUM):
    return {**_raster(path, pixels, units, name), **({"vertical_datum": datum} if datum else {})}


def test_rasters_per_source_layer_and_per_surface_sample_at_each_cell(tmp_path):
    inputs = {
        "k1": _tif(tmp_path / "k1.tif", [[10.0, 20.0], [30.0, 40.0]], name="k1", units="m/day",
                   datum=None),
        "s0": _tif(tmp_path / "s0.tif", [[100.0, 100.0], [100.0, 100.0]], name="s0", units="m"),
        "s1": _tif(tmp_path / "s1.tif", [[90.0, 80.0], [90.0, 80.0]], name="s1", units="m")}
    sheet = _fill(inputs, {"thickness": 30.0, "water_table_depth": 3.0, "conductivity": {
        "layers": ["k1", 1.0], "surfaces": ["s0", "s1", 70.0]}})
    # The grid layer is 95 to 85. Where s1 = 90: (5 * K1 + 5 * 1) / 10; where s1 = 80:
    # 10 m of the first layer and none of the second, so K1 itself.
    k1 = _per_cell([[10.0, 20.0], [30.0, 40.0]])
    wide = [(5 * k + 5 * 1.0) / 10 if (cell // 2) % 2 == 0 else k for cell, k in enumerate(k1)]
    assert _depth_k(sheet, tops=95.0, botms=[85.0]) == [pytest.approx(wide)]


def test_an_unmeasured_source_cell_weighs_zero(tmp_path):
    nan = float("nan")
    inputs = {"k1": _tif(tmp_path / "k1.tif", [[nan, 20.0], [30.0, 40.0]], name="k1",
                         units="m/day", datum=None)}
    sheet = _fill(inputs, {"thickness": 30.0, "water_table_depth": 3.0, "conductivity": {
        "layers": ["k1", 1.0], "surfaces": [100.0, 90.0, 70.0]}})
    got = _depth_k(sheet, tops=95.0, botms=[85.0])[0]
    # Cells 4 and 5 sit on the unmeasured pixel: only the 5 m of the 1 m/d layer counts.
    assert got[4] == got[5] == pytest.approx(1.0)
    assert got[0] == pytest.approx((5 * 30.0 + 5 * 1.0) / 10)
    with pytest.raises(ModflowError) as caught:
        _depth_k(sheet, tops=100.0, botms=[90.0])
    assert caught.value.error_code == "MODFLOW_GRIDDATA_OUTSIDE_SOURCE"
    assert "2 over cells the source leaves unmeasured" in str(caught.value)


def test_a_surface_on_another_datum_or_none_or_a_run_with_no_frame_refuses(tmp_path):
    def run(datum, frame="NAVD88"):
        inputs = {"s0": _tif(tmp_path / "s0.tif", [[100.0] * 2] * 2, name="s0", units="m",
                             datum=datum)}
        sheet = _fill(inputs, {"thickness": 30.0, "water_table_depth": 3.0, "conductivity": {
            "layers": [10.0, 1.0], "surfaces": ["s0", 90.0, 70.0]}})
        return _depth_k(sheet, tops=95.0, botms=[85.0], run={"vertical_frame": frame})

    with pytest.raises(ModflowError) as caught:
        run("National Geodetic Vertical Datum of 1929")
    assert caught.value.error_code == "MODFLOW_DATUMS_DIFFER"
    with pytest.raises(ModflowError) as caught:
        run(None)
    assert caught.value.error_code == "MODFLOW_DATUM_UNSTATED"
    with pytest.raises(ModflowError) as caught:
        run(_DATUM, frame="")
    assert caught.value.error_code == "MODFLOW_DATUM_UNSTATED"
    assert run("NAVD88") == [pytest.approx([5.5] * 8)]


def test_surfaces_not_one_longer_than_the_layers_refuse_naming_both():
    for surfaces in ([100.0, 90.0], [100.0, 90.0, 70.0, 50.0]):
        with pytest.raises(ModflowError) as caught:
            _by_depth([10.0, 1.0], surfaces)
        assert caught.value.error_code == "MODFLOW_AQUIFER_UNSTATED"
        assert "2 layer(s)" in str(caught.value) and f"{len(surfaces)} surface(s)" in str(caught.value)


@pytest.mark.parametrize("stated, named", [
    ({"surfaces": [100.0, 90.0, 70.0]}, "layers"), ({"layers": [10.0, 1.0]}, "surfaces")])
def test_a_layered_source_missing_a_key_refuses_by_name(stated, named):
    with pytest.raises(ModflowError) as caught:
        _fill({}, {"thickness": 30.0, "water_table_depth": 3.0, "conductivity": stated})
    assert caught.value.error_code == "MODFLOW_AQUIFER_UNSTATED"
    assert f"{named} is not stated" in str(caught.value)


def test_a_layered_source_under_transmissivity_refuses_by_name():
    with pytest.raises(ModflowError) as caught:
        _fill({}, {"thickness": 30.0, "water_table_depth": 3.0,
                   "transmissivity": {"layers": [1.0], "surfaces": [2.0, 1.0]}})
    assert "transmissivity" in str(caught.value)


_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "usgs_model_k"
_DM_BBOX = [-93.6729, 41.6625, -93.6369, 41.6895]
_DM_PROBE = (-93.655, 41.676)


class _Held:
    def __init__(self, uri, data=b""):
        self.uri, self.data = uri, data


def _ring_frames(monkeypatch):
    """The Des Moines fetchers' own frames over the ring excerpt, offline: K layers 1-2 and surfaces 0-2, as COG bytes by layer."""
    import trid3nt_server.tools.cache as cache_mod
    from trid3nt_server.tools import TOOL_REGISTRY
    from trid3nt_server.tools.fetchers._router.executors import animation_frames as EX
    from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_model_k import hooks as MK
    from trid3nt_server.tools.fetchers.hydrology.fetch_usgs_model_layers import hooks as ML

    archive = (_FIXTURES / "dm_ring_excerpt.zip").read_bytes()
    inner, held = {}, {}

    def get_bytes(client, url, *, headers=None, params=None):
        return archive, "application/zip", url

    def inner_rt(metadata, params, ext, fetch_fn, **kw):
        key = str(params)
        inner.setdefault(key, fetch_fn())
        return _Held(f"s3://cache-in/{len(inner)}", inner[key])

    def outer_rt(metadata, params, ext, fetch_fn, **kw):
        data = fetch_fn()
        uri = f"s3://cache/{metadata.name}-{params.get('layer', params.get('surface'))}"
        held[uri] = data
        return _Held(uri, data)

    monkeypatch.setattr(MK, "get_bytes", get_bytes)
    monkeypatch.setattr(ML, "get_bytes", get_bytes)
    monkeypatch.setattr(cache_mod, "read_through", inner_rt)
    monkeypatch.setattr(EX, "read_through", outer_rt)
    k = TOOL_REGISTRY["fetch_usgs_model_k"].fn(bbox=_DM_BBOX)
    surfaces = TOOL_REGISTRY["fetch_usgs_model_layers"].fn(bbox=_DM_BBOX)
    return k, surfaces, held


def test_a_fetched_layered_model_is_read_by_depth_at_a_cell(monkeypatch, tmp_path):
    import re
    import zipfile

    import numpy as np
    import rasterio
    from pyproj import Transformer

    from trid3nt_server.tools.fetchers._router import registration as reg
    from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place

    k_layers, surface_layers, held = _ring_frames(monkeypatch)
    datum = reg.get_spec("fetch_usgs_model_layers").vertical_datum
    assert datum == "NAVD88"

    def artifact(layer, number, units):
        path = tmp_path / f"{number}.tif"
        path.write_bytes(held[layer.uri])
        return {"uri": str(path), "name": number, "units": units, "vertical_datum": datum}

    k_by = {int(re.search(r"-l(\d+)-", l.layer_id).group(1)): l for l in k_layers}
    s_by = {0 if "-top-l1-" in l.layer_id else int(re.search(r"-bot-l(\d+)-", l.layer_id).group(1)): l
            for l in surface_layers}
    inputs = {f"k{n}": artifact(k_by[n], f"k{n}", "m/d") for n in (1, 2)}
    inputs |= {f"s{n}": artifact(s_by[n], f"s{n}", "m") for n in (0, 1, 2)}

    # The probe's pixel in the surface frame, and the cell whose centre is that pixel's centre.
    with rasterio.open(tmp_path / "s0.tif") as ds:
        r, c = ds.index(*_DM_PROBE)
        lon, lat = ds.xy(r, c)
    x, y = Transformer.from_crs(4326, 32615, always_xy=True).transform(lon, lat)
    points = [(x - 1.0, y - 1.0), (x + 1.0, y - 1.0), (x, y + 2.0)]
    cells = [(0, 1, 2)]

    # The archive's own members at the same model cell: the grid is 60 m from (429070, 4629880) in 26915.
    ux, uy = Transformer.from_crs(4326, 26915, always_xy=True).transform(lon, lat)
    row, col = int((4629880.0 - uy) // 60.0), int((ux - 429070.0) // 60.0)

    def raw(member):
        with zipfile.ZipFile(_FIXTURES / "dm_ring_excerpt.zip") as zf:
            return float(np.array(zf.read(member).decode().split(), dtype=float)
                         .reshape(427, 383)[row, col])

    k1, k2 = raw("external_files/props/l1._kx"), raw("external_files/props/l2._kx")
    s0, s1, s2 = (raw("external_files/surfs/l1_top.dat"), raw("external_files/surfs/l1_bot.dat"),
                  raw("external_files/surfs/l2_bot.dat"))
    assert s0 > s1 > s2 + 2.0 and k1 > 0.0 and k2 > 0.0

    # A two-layer Aquifer from 2 m over the source's top to 1 m over its bottom, split at the
    # middle: layer 1 ignores the 2 m above the top and takes the part of each source layer it
    # overlaps; layer 2 lies in source layer 2 alone.
    top, bottom = s0 + 2.0, s2 + 1.0
    sheet = _fill(inputs, {"thickness": top - bottom, "water_table_depth": 1.0, "layers": 2,
                           "conductivity": {"layers": ["k1", "k2"], "surfaces": ["s0", "s1", "s2"]}},
                  DISV_TOP=top)
    run = {**sheet.run, "vertical_frame": "NAVD88"}
    art = type("Art", (), {"utm_epsg": 32615})()
    from trid3nt_server.workflows.modflow.authoring import mesh_files as mf

    placed_top = mf.top_of(sheet, nlay=2, art=art, bed=None, points=points, cells=cells)
    botm = mf.botm_of(sheet, nlay=2, art=art, points=points, cells=cells, top=placed_top)
    mid = (top + bottom) / 2.0
    assert botm == [pytest.approx([mid]), pytest.approx([bottom])]
    got = place(sheet.filled["NPF_K"].value, slot=sheet.filled["NPF_K"].slot, nlay=2, run=run,
                points=points, cells=cells, utm_epsg=32615, frame=frame_of(sheet),
                top=placed_top, botm=botm)
    # Layer 1 spans [mid, top]: source layer 1 is [s1, s0] and source layer 2 [s2, s1].
    o11 = s0 - max(mid, s1)
    o12 = max(0.0, min(s1, top) - max(mid, s2))
    o22 = min(mid, s1) - bottom
    o21 = max(0.0, min(mid, s0) - max(bottom, s1))
    want1 = (o11 * k1 + o12 * k2) / (o11 + o12)
    want2 = (o21 * k1 + o22 * k2) / (o21 + o22)
    print(f"DM cell (row {row}, col {col}): K {k1:.4f}/{k2:.4f} m/d, surfaces "
          f"{s0:.3f}/{s1:.3f}/{s2:.3f} m; layer 1 {want1:.4f}, layer 2 {want2:.4f} m/d")
    assert got[0] == pytest.approx([want1], rel=1e-3)
    assert got[1] == pytest.approx([want2], rel=1e-3)


def test_a_gap_layer_of_the_fetched_ring_is_unmeasured_and_the_layers_around_it_still_place(monkeypatch, tmp_path):
    """Des Moines layers 3 and 4 carry no active cell at the probe: the frames drop layer 3 whole, and the lists
    state what is emitted (K of layers 1, 2, 4, 5, 6; the top and the bottoms of layers 1, 2, 4, 5, 6)."""
    import re

    from pyproj import Transformer
    import rasterio

    from trid3nt_server.workflows.modflow.authoring.griddata import frame_of, place

    k_layers, surface_layers, held = _ring_frames(monkeypatch)
    assert sorted(int(re.search(r"-l(\d+)-", l.layer_id).group(1)) for l in k_layers) == [1, 2, 4, 5, 6]
    assert len(surface_layers) == len(k_layers) + 1 and {l.vertical_datum for l in surface_layers} == {"NAVD88"}

    def artifact(layer, name, units):
        path = tmp_path / f"{name}.tif"
        path.write_bytes(held[layer.uri])
        return {"uri": str(path), "name": name, "units": units, "vertical_datum": layer.vertical_datum}

    k_ordered = sorted(k_layers, key=lambda l: int(re.search(r"-l(\d+)-", l.layer_id).group(1)))
    s_ordered = sorted(surface_layers, key=lambda l: 0 if "-top-l1-" in l.layer_id
                       else int(re.search(r"-bot-l(\d+)-", l.layer_id).group(1)))
    inputs = {f"k{i}": artifact(l, f"k{i}", "m/d") for i, l in enumerate(k_ordered)}
    inputs |= {f"s{i}": artifact(l, f"s{i}", "m") for i, l in enumerate(s_ordered)}

    with rasterio.open(tmp_path / "s0.tif") as ds:
        lon, lat = ds.xy(*ds.index(*_DM_PROBE))
    x, y = Transformer.from_crs(4326, 32615, always_xy=True).transform(lon, lat)
    points = [(x - 1.0, y - 1.0), (x + 1.0, y - 1.0), (x, y + 2.0)]
    cells = [(0, 1, 2)]

    # Model layers 1 and 2 span [243, 245] and [234, 243] m at the probe; layers 3 and 4 are pass-through.
    top, botm = 245.0, [243.0, 234.0]
    sheet = _fill(inputs, {"thickness": top - botm[-1], "water_table_depth": 1.0, "layers": 2,
                           "conductivity": {"layers": [f"k{i}" for i in range(5)],
                                            "surfaces": [f"s{i}" for i in range(6)]}}, DISV_TOP=top)
    got = place(sheet.filled["NPF_K"].value, slot=sheet.filled["NPF_K"].slot, nlay=2,
                run={**sheet.run, "vertical_frame": "NAVD88"}, points=points, cells=cells,
                utm_epsg=32615, frame=frame_of(sheet), top=[top], botm=[[b] for b in botm])
    assert got[0] == pytest.approx([28.973], rel=1e-3)
    assert got[1] == pytest.approx([10.289], rel=1e-3)


def test_a_by_depth_conductivity_serializes_one_k_array_per_grid_layer(staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({}, {"thickness": 30.0, "water_table_depth": 1.0, "layers": 2,
                       "conductivity": {"layers": [4.0, 0.4], "surfaces": [20.0, 0.0, -40.0]}})
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir()
    shutil.copy(out["disv"], rundir / "mesh.disv")

    serialize(sheet, rundir, mesh)

    lines = (rundir / "gwf.npf").read_text().splitlines()
    start = next(i for i, row in enumerate(lines) if row.split()[:2] == ["k", "LAYERED"])
    rows = [[float(v) for v in lines[start + at].split()] for at in (2, 4)]
    # Layer 1 lies 15 m under each cell's top: t m of the 4 m/d source layer, 15 - t of the 0.4 one.
    assert rows[0] == pytest.approx([(t * 4.0 + (15.0 - t) * 0.4) / 15.0 for t in _TOP])
    assert rows[1] == pytest.approx([0.4] * 8)
