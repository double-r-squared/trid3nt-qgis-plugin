"""The stress composites: a statement by geometry, the cells it places on the mesh, the package it writes.

Offline: the mesh is a 20 m lattice in UTM 32617 (four squares, eight triangles; the
cell numbering runs square by square, lower triangle first), the geometries are
drawn on it in lon/lat and the upload is replaced by a copy beside it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pyproj import Transformer

from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.authoring.stress import place
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.runtime.sheet import fill

_POINTS = [(x, y) for y in (0.0, 10.0, 20.0) for x in (0.0, 10.0, 20.0)]
_CELLS = [tri for r in range(2) for c in range(2)
          for a in [r * 3 + c] for tri in ((a, a + 1, a + 4), (a, a + 4, a + 3))]
_TO_LONLAT = Transformer.from_crs(32617, 4326, always_xy=True).transform
_COMPOSITES = {
    "Drains": {"elevation": -1.0, "conductance": 50.0},
    "GeneralHead": {"head": 2.0, "conductance": 20.0},
    "ConstantHead": {"head": 3.0},
    "Wells": {"rate": -5.0},
    "Recharge": {"rate": 1.0e-3},
}
_PACKAGE = {"Drains": "DRN", "GeneralHead": "GHB", "ConstantHead": "CHD",
            "Wells": "WEL", "Recharge": "RCH"}


def _geo(kind: str, utm) -> dict:
    def lonlat(pair):
        return list(_TO_LONLAT(*pair))

    if kind == "Polygon":
        return {"type": kind, "coordinates": [[lonlat(p) for p in utm]]}
    if kind == "Point":
        return {"type": kind, "coordinates": lonlat(utm)}
    return {"type": kind, "coordinates": [lonlat(p) for p in utm]}


_POINT = _geo("Point", (8.0, 2.0))
_LINE = _geo("LineString", [(2.0, 5.0), (18.0, 5.0)])
_LEFT = _geo("Polygon", [(-1.0, -1.0), (11.0, -1.0), (11.0, 21.0),
                         (-1.0, 21.0), (-1.0, -1.0)])


def _lattice(tmp_path: Path) -> Path:
    rows = ["MESH2D"]
    cell = 0
    for r in range(2):
        for c in range(2):
            a = r * 3 + c + 1
            for tri in ((a, a + 1, a + 4), (a, a + 4, a + 3)):
                cell += 1
                rows.append(f"E3T {cell} {tri[0]} {tri[1]} {tri[2]} 1")
    for node, (x, y) in enumerate(_POINTS, start=1):
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


def _raster(path: Path, *, value: float, cells: int = 2,
            size: float = 10.0) -> Path:
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    with rasterio.open(
            path, "w", driver="GTiff", height=cells, width=cells, count=1,
            dtype="float32", crs="EPSG:32617",
            transform=from_origin(0.0, 20.0, size, size)) as dst:
        dst.write(np.full((cells, cells), value, dtype="float32"), 1)
    return path


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


def _statement(name: str, **over) -> dict:
    return {"geometry": "where", "layer": 1, **_COMPOSITES[name], **over}


def _fill(stated: dict, where=_LINE):
    return fill(GWF, produced={"where": where}, **{
        "DISV_NLAY": 2, "DISV_BOTM": [[-10.0] * 8, [-20.0] * 8],
        "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0, "IC_STRT": 0.0,
        "TDIS_NPER": 1, "TDIS_TIME_UNITS": "seconds",
        "TDIS_PERIODDATA": [[600.0, 1, 600.0]], **stated})


_DIMENSIONS = {"elevation": "L", "head": "L", "conductance": "L2/T",
               "rate": "L3/T"}


def _placed(name: str, geometry, *, layer: int = 1, values=None,
            frame=("meters", "days")):
    values = values or dict(_COMPOSITES[name])
    statement = {"stress": _PACKAGE[name], "composite": name,
                 "geometry": geometry, "layer": layer, "values": values,
                 "dimensions": {key: _DIMENSIONS[key] for key in values}}
    return place(statement, points=_POINTS, cells=_CELLS, utm_epsg=32617,
                 nlay=2, frame=frame)


@pytest.mark.parametrize("name", list(_COMPOSITES))
def test_a_value_left_unstated_refuses_by_name(name):
    for argument in ("layer", *_COMPOSITES[name]):
        for dropped in (True, False):
            body = _statement(name)
            if dropped:
                body.pop(argument)
            else:
                body[argument] = None
            with pytest.raises(ModflowError) as caught:
                _fill({name: body})
            assert caught.value.error_code == "MODFLOW_STRESS_UNSTATED"
            assert name in str(caught.value) and argument in str(caught.value)


@pytest.mark.parametrize("layer", [0, -1, 1.5, "1", True])
def test_a_layer_that_is_not_an_integer_from_one_refuses(layer):
    with pytest.raises(ModflowError) as caught:
        _fill({"Drains": _statement("Drains", layer=layer)})
    assert caught.value.error_code == "MODFLOW_STRESS_UNSTATED"


@pytest.mark.parametrize("name", list(_COMPOSITES))
def test_a_geometry_reading_nothing_states_nothing(name):
    sheet = _fill({name: _statement(name)}, where=None)
    assert f"{_PACKAGE[name]}_STRESS_PERIOD_DATA" not in sheet.filled
    assert f"{_PACKAGE[name]}_MAXBOUND" not in sheet.filled


def test_a_stated_composite_writes_the_statement_under_the_keyword_s_own_name():
    row = _fill({"Drains": _statement("Drains")}
                ).filled["DRN_STRESS_PERIOD_DATA"].value
    assert row["stress"] == "DRN" and row["composite"] == "Drains"
    assert row["layer"] == 1
    assert list(row["values"]) == ["elevation", "conductance"]


def test_a_point_places_the_one_cell_containing_it():
    assert _placed("ConstantHead", _POINT) == [((0, 0), 3.0)]


def test_a_point_on_a_vertex_six_cells_share_places_one_cell_and_pumps_once():
    rows = _placed("Wells", _geo("Point", (10.0, 10.0)))
    assert len(rows) == 1


def test_a_line_places_every_cell_it_crosses():
    rows = _placed("Drains", _LINE)
    assert [row[0] for row in rows] == [(0, 0), (0, 1), (0, 2), (0, 3)]
    assert all(row[1:] == (-1.0, 50.0) for row in rows)


def test_a_polygon_places_the_cells_whose_centres_it_holds():
    rows = _placed("Wells", _LEFT, layer=2)
    assert [row[0] for row in rows] == [(1, 0), (1, 1), (1, 4), (1, 5)]


def test_a_point_off_the_mesh_refuses_with_the_shared_code_and_a_hint():
    from trid3nt_server.inputs.point import PointNotViableError

    with pytest.raises(PointNotViableError) as caught:
        _placed("Wells", _geo("Point", (500.0, 500.0)))
    assert caught.value.error_code == "POINT_NOT_VIABLE"
    assert "Wells" in str(caught.value) and caught.value.hint is not None


def test_a_line_off_the_mesh_refuses_by_name():
    with pytest.raises(ModflowError) as caught:
        _placed("Drains", _geo("LineString", [(500.0, 500.0), (600.0, 600.0)]))
    assert caught.value.error_code == "MODFLOW_STRESS_OUTSIDE"


def test_a_well_in_an_inactive_cell_refuses_naming_the_nearest_active_one():
    from trid3nt_server.inputs.point import PointNotViableError
    from trid3nt_server.workflows.modflow.authoring.stress import point_cell
    from trid3nt_server.inputs.point import Point

    lon, lat = _geo("Point", (5.0, 2.0))["coordinates"]
    here = point_cell(Point(lon, lat), label="Wells", points=_POINTS, cells=_CELLS,
                      utm_epsg=32617)
    active = [1] * len(_CELLS)
    active[here] = 0
    with pytest.raises(PointNotViableError) as caught:
        point_cell(Point(lon, lat), label="Wells", points=_POINTS, cells=_CELLS,
                   utm_epsg=32617, active=active)
    assert "DISV_IDOMAIN 0" in str(caught.value)
    assert caught.value.hint["distance_m"] > 0


def test_a_layer_the_grid_does_not_have_refuses_naming_both():
    with pytest.raises(ModflowError) as caught:
        _placed("Wells", _POINT, layer=3)
    assert caught.value.error_code == "MODFLOW_STRESS_LAYER"
    assert "layer 3" in str(caught.value) and "2 layer" in str(caught.value)


def test_a_named_raster_value_is_sampled_at_each_placed_cell(tmp_path):
    raster = _raster(tmp_path / "head.tif", value=4.5)
    rows = _placed("ConstantHead", _LEFT,
                   values={"head": {"uri": str(raster), "units": "m"}})
    assert [row[1] for row in rows] == [4.5] * 4


def test_a_raster_measuring_nothing_at_a_placed_cell_refuses(tmp_path):
    raster = _raster(tmp_path / "small.tif", value=1.0, cells=1, size=5.0)
    with pytest.raises(ModflowError) as caught:
        _placed("ConstantHead", _LEFT,
                values={"head": {"uri": str(raster), "units": "m"}})
    assert caught.value.error_code == "MODFLOW_GRIDDATA_UNMEASURED"


def test_serialize_writes_each_stress_package_with_its_bound(
        staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({name: _statement(name) for name in _COMPOSITES})
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir()
    shutil.copy(out["disv"], rundir / "mesh.disv")

    written = serialize(sheet, rundir, mesh)["written"]

    assert {"gwf.drn", "gwf.ghb", "gwf.chd", "gwf.wel", "gwf.rch"} <= set(written)
    for package in ("drn", "ghb", "chd", "wel", "rch"):
        text = (rundir / f"gwf.{package}").read_text()
        assert "MAXBOUND  4" in text
        block = text.split("BEGIN period  1")[1].split("END period")[0]
        assert len([row for row in block.splitlines() if row.strip()]) == 4


def test_a_list_of_statements_writes_each_ones_cells_under_one_bound(
        staged_beside, tmp_path):
    """Wells at different cells on different layers are one package's list of statements."""
    mesh = _mesh(tmp_path)
    sheet = _fill({"Wells": [_statement("Wells", geometry="where", layer=1),
                             _statement("Wells", geometry="where", layer=2, rate=-2.0)]})
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir()
    shutil.copy(out["disv"], rundir / "mesh.disv")

    serialize(sheet, rundir, mesh)

    text = (rundir / "gwf.wel").read_text()
    assert "MAXBOUND  8" in text
    block = text.split("BEGIN period  1")[1].split("END period")[0]
    rows = [row.split() for row in block.splitlines() if row.strip()]
    assert [row[0] for row in rows] == ["1"] * 4 + ["2"] * 4
    assert {row[-1] for row in rows[:4]} == {"-5.00000000E+00"}
    assert {row[-1] for row in rows[4:]} == {"-2.00000000E+00"}


def test_a_list_of_statements_reading_nothing_states_nothing():
    sheet = _fill({"Wells": [_statement("Wells"), _statement("Wells", layer=2)]}, where=None)
    assert "WEL_STRESS_PERIOD_DATA" not in sheet.filled


def test_a_list_of_statements_names_every_input_it_reads():
    composite = GWF.COMPOSITES["Wells"]
    assert composite.names([_statement("Wells"), _statement("Wells", geometry="other")]) \
        == ["where", "other"]


def test_a_typed_point_places_the_one_cell_it_lies_in():
    from trid3nt_server.inputs.point import Point

    rows = _placed("Wells", Point(*_TO_LONLAT(8.0, 2.0), "the well"))
    assert rows == [((0, 0), -5.0)]


def test_a_typed_domain_places_the_cells_whose_centres_it_holds():
    from trid3nt_server.inputs.domain import Domain

    rows = _placed("Recharge", Domain(geometry=_LEFT), layer=2)
    assert [row[0] for row in rows] == [(1, 0), (1, 1), (1, 4), (1, 5)]


@pytest.mark.parametrize("frame,expected", [(("meters", "days"), -3.0),
                                            (("feet", "days"), -3.0 / 0.3048)])
def test_a_bed_stated_as_a_depth_is_its_flat_surface_on_the_model_s_frame(frame, expected):
    from trid3nt_server.inputs.bed import Bed

    rows = _placed("Drains", _LINE, frame=frame,
                   values={"elevation": Bed(kind="depth", depth_m=3.0),
                           "conductance": 50.0})
    assert [row[1] for row in rows] == [pytest.approx(expected)] * 4


def test_a_recharge_carrying_a_concentration_states_the_auxiliary_column():
    sheet = _fill({"Recharge": _statement("Recharge", concentration=0.45)})
    row = sheet.filled["RCH_STRESS_PERIOD_DATA"].value
    assert list(row["values"]) == ["rate", "concentration"]
    assert row["dimensions"]["concentration"] == "M/L3"
    assert sheet.filled["RCH_AUXILIARY"].value == ["CONCENTRATION"]


def test_a_recharge_stating_no_concentration_carries_no_auxiliary():
    assert "RCH_AUXILIARY" not in _fill({"Recharge": _statement("Recharge")}).filled


def test_a_concentration_on_some_statements_and_not_others_refuses_by_name():
    with pytest.raises(ModflowError) as caught:
        _fill({"Recharge": [_statement("Recharge", concentration=0.45),
                            _statement("Recharge")]})
    assert caught.value.error_code == "MODFLOW_STRESS_UNSTATED"
    assert "concentration" in str(caught.value)


def test_serialize_writes_the_concentration_as_the_package_s_auxiliary(
        staged_beside, tmp_path):
    mesh = _mesh(tmp_path)
    sheet = _fill({"Recharge": [_statement("Recharge", concentration=0.0),
                                _statement("Recharge", rate=0.04, concentration=0.45)]})
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir()
    shutil.copy(out["disv"], rundir / "mesh.disv")

    serialize(sheet, rundir, mesh)

    text = (rundir / "gwf.rch").read_text()
    assert "auxiliary  CONCENTRATION" in text
    block = text.split("BEGIN period  1")[1].split("END period")[0]
    rows = [row.split() for row in block.splitlines() if row.strip()]
    assert len(rows) == 8 and {row[-1] for row in rows[4:]} == {"4.50000000E-01"}


_NINE = [(r * 4 + c, r * 4 + c + 1, r * 4 + c + 5, r * 4 + c + 4)
         for r in range(3) for c in range(3)]


def test_the_edge_is_the_cells_with_a_face_no_other_cell_shares():
    from trid3nt_server.workflows.modflow.authoring.stress import edge_cells

    assert edge_cells(_NINE) == [0, 1, 2, 3, 5, 6, 7, 8]


def test_an_edge_column_holds_the_starting_head_on_every_solved_layer():
    points = [(10.0 * c, 10.0 * r) for r in range(4) for c in range(4)]
    start = [[float(i) for i in range(9)], [100.0 + i for i in range(9)]]
    statement = {"stress": "CHD", "composite": "ConstantHead",
                 "geometry": {"cells": "edge"}, "layer": "all",
                 "values": {"head": {"cells": "start"}}, "dimensions": {"head": "L"}}
    active = [[1] * 9, [1, 1, 1, 1, 1, 0, 1, 1, 1]]
    rows = place(statement, points=points, cells=_NINE, utm_epsg=32617, nlay=2,
                 active=active, start=start)
    assert [row[0] for row in rows if row[0][0] == 0] == [(0, i) for i in (0, 1, 2, 3, 5, 6, 7, 8)]
    assert [row[0] for row in rows if row[0][0] == 1] == [(1, i) for i in (0, 1, 2, 3, 6, 7, 8)]
    assert {r[0]: r[1] for r in rows}[(1, 7)] == 107.0


def test_a_starting_head_with_no_initial_condition_refuses_by_name():
    statement = {"stress": "CHD", "composite": "ConstantHead",
                 "geometry": {"cells": "edge"}, "layer": "all",
                 "values": {"head": {"cells": "start"}}, "dimensions": {"head": "L"}}
    with pytest.raises(ModflowError) as caught:
        place(statement, points=_POINTS, cells=_NINE, utm_epsg=32617, nlay=1)
    assert caught.value.error_code == "MODFLOW_STRESS_UNSTATED"


def test_a_starting_head_stated_as_one_number_holds_every_edge_cell_at_it():
    statement = {"stress": "CHD", "composite": "ConstantHead",
                 "geometry": {"cells": "edge"}, "layer": "all",
                 "values": {"head": {"cells": "start"}}, "dimensions": {"head": "L"}}
    rows = place(statement, points=_POINTS, cells=_NINE, utm_epsg=32617, nlay=1,
                 start=12.5)
    assert len(rows) == 8 and {row[1] for row in rows} == {12.5}
