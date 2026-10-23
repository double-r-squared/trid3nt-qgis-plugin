"""The period settings of the placement composites: every setting mf6 defines for SFR, LAK, MAW and UZF, by its own name.

A setting is a number from period 1, a mapping keyed by period (from 1), or not stated; one not stated is
absent from the package. Offline except the validate cases, which run mf6's own --mode validate through the image.
The mesh is a 3x3 lattice of 10 m squares in UTM 32617, numbered row by row from the south-west, three periods;
every row below is computed by hand.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from pyproj import Transformer

from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.authoring.stress import place_lakes, place_maw, place_streams, place_uzf
from trid3nt_server.workflows.modflow.authoring.validate import validate_authored_simulation
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.runtime.sheet import fill

_SIDE = 3
_N = _SIDE * _SIDE
_POINTS = [(x * 10.0, y * 10.0) for y in range(_SIDE + 1) for x in range(_SIDE + 1)]
_CELLS = [(a, a + 1, a + _SIDE + 2, a + _SIDE + 1)
          for r in range(_SIDE) for c in range(_SIDE)
          for a in [r * (_SIDE + 1) + c]]
_TO_LONLAT = Transformer.from_crs(32617, 4326, always_xy=True).transform
_BOTM = {1: [[0.0] * _N], 2: [[5.0] * _N, [0.0] * _N], 3: [[7.0] * _N, [4.0] * _N, [0.0] * _N]}
_MESH = dict(points=_POINTS, cells=_CELLS, utm_epsg=32617)
_GRID = {1: {"top": [10.0] * _N, "botm": _BOTM[1]}, 2: {"top": [10.0] * _N, "botm": _BOTM[2]}}


def _point(x, y):
    return {"type": "Point", "coordinates": list(_TO_LONLAT(x, y))}


def _box(x0, y0, x1, y1):
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return {"type": "Polygon", "coordinates": [[list(_TO_LONLAT(*p)) for p in ring]]}


def _line(*utm):
    return {"type": "LineString", "coordinates": [list(_TO_LONLAT(*p)) for p in utm]}


def _fill(stated: dict, nlay: int = 1, **produced):
    return fill(GWF, produced=produced, **{
        "DISV_NLAY": nlay, "DISV_LENGTH_UNITS": "meters", "DISV_TOP": [10.0] * _N,
        "DISV_BOTM": _BOTM[nlay], "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0, "IC_STRT": 5.0,
        "TDIS_NPER": 3, "TDIS_TIME_UNITS": "days", "TDIS_PERIODDATA": [[10.0, 1, 1.0]] * 3, **stated})


def _lattice(tmp_path: Path) -> Path:
    rows = ["MESH2D"]
    for cell, ring in enumerate(_CELLS, start=1):
        rows.append(f"E4Q {cell} " + " ".join(str(v + 1) for v in ring) + " 1")
    for node, (x, y) in enumerate(_POINTS, start=1):
        rows.append(f"ND {node} {x} {y} -2.0")
    path = tmp_path / "mesh.2dm"
    path.write_text("\n".join(rows) + "\n")
    return path


def _mesh(tmp_path: Path) -> dict:
    art = MeshArtifact(
        mesh_id="M01", name="lattice", mode="om2d",
        display_uri=str(_lattice(tmp_path)), crs_authid="EPSG:32617",
        has_bathymetry=True, node_count=len(_POINTS), element_count=len(_CELLS),
        bbox=(0.0, 0.0, 30.0, 30.0), utm_epsg=32617)
    return {"artifact": art, "display_uri": art.display_uri}


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


def _written(sheet, tmp_path, package):
    mesh = _mesh(tmp_path)
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir(exist_ok=True)
    shutil.copy(out["disv"], rundir / "mesh.disv")
    assert package in serialize(sheet, rundir, mesh)["written"]
    return rundir, (rundir / package).read_text()


# ---- MAW: gwf-maw.dfn, block period: status, flowing_well, rate, well_head, head_limit, shut_off, rate_scaling, auxiliary

_WELL = {"radius": 0.15, "bottom": 0.0, "start_head": 8.0, "condeqn": "THIEM", "screen_top": 10.0,
         "screen_bottom": 0.0, "rate": -3.0}


def _wells(statement=None, *, declared=(), flowing_wells=True, **settings):
    sheet = _fill({"MultiAquiferWells": {"geometry": "where", **_WELL, **settings, **(statement or {})}},
                  nlay=2, where=_point(15.0, 15.0))
    return place_maw(sheet.filled["MAW_PACKAGEDATA"].value["wells"], nlay=2, auxiliary=declared,
                     flowing_wells=flowing_wells, **_MESH, **_GRID[2])


@pytest.mark.parametrize("settings,rows", [
    ({"rate": None, "status": "constant", "well_head": 2.0},
     [[0, "status", "CONSTANT"], [0, "well_head", 2.0]]),
    ({"rate": None, "status": "INACTIVE"}, [[0, "status", "INACTIVE"]]),
    ({"head_limit": 0.5}, [[0, "rate", -3.0], [0, "head_limit", 0.5]]),
    ({"head_limit": "off"}, [[0, "rate", -3.0], [0, "head_limit", "OFF"]]),
    ({"fwelev": 1.0, "fwcond": 2.0, "fwrlen": 3.0}, [[0, "flowing_well", 1.0, 2.0, 3.0], [0, "rate", -3.0]]),
    ({"head_limit": 0.5, "minrate": -5.0, "maxrate": -1.0},
     [[0, "rate", -3.0], [0, "head_limit", 0.5], [0, "shut_off", -5.0, -1.0]]),
    ({"pump_elevation": 1.0, "scaling_length": 2.0}, [[0, "rate", -3.0], [0, "rate_scaling", 1.0, 2.0]]),
    ({"rate": -1.5}, [[0, "rate", -1.5]]),
])
def test_each_maw_setting_writes_the_row_mf6_reads_it_by(settings, rows):
    assert _wells(**settings)[2] == {0: rows}


def test_flowing_well_values_without_the_flowing_wells_option_refuse_because_mf6_would_ignore_them():
    with pytest.raises(ModflowError) as caught:
        _wells(flowing_wells=False, fwelev=1.0, fwcond=2.0, fwrlen=3.0)
    assert "MAW_FLOWING_WELLS" in str(caught.value) and "fwelev" in str(caught.value)


def test_a_maw_setting_not_stated_is_absent():
    assert _wells()[2] == {0: [[0, "rate", -3.0]]}


def test_a_maw_setting_keyed_by_period_is_a_row_of_each_period_it_names():
    perioddata = _wells(rate={1: -3.0, "3": -1.0}, status={2: "CONSTANT", 3: "active"}, well_head={2: 2.0})[2]
    assert perioddata == {0: [[0, "rate", -3.0]],
                          1: [[0, "status", "CONSTANT"], [0, "well_head", 2.0]],
                          2: [[0, "status", "ACTIVE"], [0, "rate", -1.0]]}


def test_a_maw_record_keyed_by_period_is_stated_whole_in_the_same_periods():
    perioddata = _wells(minrate={1: -5.0, 2: -4.0}, maxrate={1: -1.0, 2: -0.5}, head_limit=0.5)[2]
    assert perioddata[1] == [[0, "shut_off", -4.0, -0.5]]
    with pytest.raises(ModflowError) as caught:
        _wells(minrate={1: -5.0}, maxrate={2: -1.0})
    assert "minrate, maxrate" in str(caught.value) and "different periods" in str(caught.value)


@pytest.mark.parametrize("settings,named", [
    ({"fwelev": 1.0}, "fwcond, fwrlen"), ({"fwelev": 1.0, "fwcond": 2.0}, "fwrlen"),
    ({"minrate": -5.0}, "maxrate"), ({"scaling_length": 2.0}, "pump_elevation")])
def test_a_maw_record_stated_in_part_refuses_naming_what_is_missing(settings, named):
    with pytest.raises(ModflowError) as caught:
        _wells(**settings)
    assert named in str(caught.value) and caught.value.error_code == "MODFLOW_STRESS_UNSTATED"


def test_an_active_well_with_no_rate_yet_refuses_by_period():
    with pytest.raises(ModflowError) as caught:
        _wells(rate=None, status={1: "CONSTANT", 3: "ACTIVE"})
    assert "rate" in str(caught.value) and "period 3" in str(caught.value)
    assert _wells(rate={3: -1.0}, status={1: "CONSTANT", 3: "ACTIVE"}, well_head=2.0)[2][2] \
        == [[0, "status", "ACTIVE"], [0, "rate", -1.0]]


@pytest.mark.parametrize("settings", [
    {"status": "RUNNING"}, {"head_limit": "ON"}, {"head_limit": [1.0, 2.0]}, {"well_head": True},
    {"rate": {0: -3.0}}, {"rate": {1.5: -3.0}}, {"rate": {}}, {"rate": {1: "a"}}, {"fwelev": float("nan")}])
def test_a_maw_setting_that_is_not_one_the_engine_takes_refuses(settings):
    with pytest.raises(ModflowError) as caught:
        _wells(**settings)
    assert next(iter(settings)) in str(caught.value)


def test_a_maw_setting_takes_a_run_input_by_name():
    sheet = _fill({"MultiAquiferWells": {"geometry": "where", **_WELL, "rate": "pumping"}},
                  nlay=2, where=_point(15.0, 15.0), pumping=-7.0)
    assert sheet.filled["MAW_PACKAGEDATA"].value["wells"][0]["values"]["rate"] == -7.0


def test_a_maw_auxiliary_is_the_packagedata_value_and_the_keyed_ones_are_rows():
    packagedata, _, perioddata = _wells(declared=("conc",), auxiliary={"CONC": 2.0})
    assert packagedata[0][6:] == [2.0] and perioddata == {0: [[0, "rate", -3.0]]}
    packagedata, _, perioddata = _wells(declared=("conc",), auxiliary={"conc": {1: 2.0, 3: 4.0}})
    assert packagedata[0][6:] == [2.0]
    assert perioddata == {0: [[0, "rate", -3.0], [0, "auxiliary", "conc", 2.0]],
                          2: [[0, "auxiliary", "conc", 4.0]]}


def test_a_maw_auxiliary_the_option_does_not_name_or_a_named_one_unstated_refuses():
    with pytest.raises(ModflowError) as caught:
        _wells(declared=("conc",), auxiliary={"salt": 1.0})
    assert "salt" in str(caught.value) and "MAW_AUXILIARY" in str(caught.value)
    with pytest.raises(ModflowError) as caught:
        _wells(declared=("conc",))
    assert "conc" in str(caught.value) and "period 1" in str(caught.value)


def test_maw_settings_write_and_mf6_validates(staged_beside, tmp_path):
    sheet = _fill({"MultiAquiferWells": [
        {"geometry": "a", **_WELL, "fwelev": 9.0, "fwcond": 5.0, "fwrlen": 1.0, "head_limit": 0.5,
         "minrate": -4.0, "maxrate": -1.0, "auxiliary": {"conc": {1: 1.0, 2: 2.0}}},
        {"geometry": "b", **_WELL, "rate": {1: -3.0}, "status": {1: "ACTIVE", 2: "CONSTANT"},
         "well_head": {2: 6.0}, "pump_elevation": 1.0, "scaling_length": 2.0, "auxiliary": {"conc": 0.0}}],
        "MAW_AUXILIARY": ["conc"], "MAW_NO_WELL_STORAGE": True, "MAW_FLOWING_WELLS": True}, nlay=2,
        a=_point(15.0, 15.0), b=_point(5.0, 25.0))
    rundir, text = _written(sheet, tmp_path, "gwf.maw")
    assert "  1  flowing_well" in text and "  1  head_limit" in text and "  1  shut_off" in text
    assert "  2  status  CONSTANT" in text and "  2  well_head" in text and "  2  rate_scaling" in text
    assert "BEGIN period  2" in text
    assert validate_authored_simulation(rundir) == {"rc": 0}


# ---- LAK: gwf-lak.dfn, blocks period and outlets: status, stage, rainfall, evaporation, runoff, inflow,
# ---- withdrawal, auxiliary; outlets rate, invert, width, slope, rough

_LAKE = {"stage": 12.0, "bottom": 0.0, "leakance": 0.1}
_CENTRE = _box(11.0, 11.0, 19.0, 19.0)
_LEFT = _box(1.0, 11.0, 9.0, 19.0)


def _lakes(*statements, declared=()):
    produced = {f"area{n}": area for n, (area, _) in enumerate(statements)}
    sheet = _fill({"Lakes": [{"geometry": f"area{n}", **_LAKE, **body} for n, (_, body) in enumerate(statements)]},
                  **produced)
    return place_lakes(sheet.filled["LAK_PACKAGEDATA"].value["lakes"], nlay=1, frame=("meters", "days"),
                       auxiliary=declared, **_MESH, **_GRID[1])


@pytest.mark.parametrize("settings,rows", [
    ({"runoff": 0.5}, [[0, "runoff", 0.5]]),
    ({"inflow": 0.5, "withdrawal": 0.25}, [[0, "inflow", 0.5], [0, "withdrawal", 0.25]]),
    ({"status": "constant"}, [[0, "status", "CONSTANT"]]),
    ({"rainfall": 0.01, "evaporation": 0.02, "runoff": 0.5},
     [[0, "rainfall", 0.01], [0, "evaporation", 0.02], [0, "runoff", 0.5]]),
])
def test_each_lak_setting_writes_the_row_mf6_reads_it_by(settings, rows):
    assert _lakes((_CENTRE, settings))[2] == {0: rows}


def test_a_lak_setting_keyed_by_period_is_a_row_of_each_period_it_names():
    perioddata = _lakes((_CENTRE, {"inflow": {1: 1.0, 3: 2.0}, "status": {2: "CONSTANT"}, "stage": {1: 12.0, 2: 11.0}}))[2]
    assert perioddata == {0: [[0, "stage", 12.0], [0, "inflow", 1.0]],
                          1: [[0, "status", "CONSTANT"], [0, "stage", 11.0]],
                          2: [[0, "inflow", 2.0]]}


def test_a_lak_stage_keyed_by_period_is_the_starting_stage_from_its_first_period():
    assert _lakes((_CENTRE, {"stage": {1: 12.5, 2: 11.0}}))[0] == [[0, 12.5, 4]]
    with pytest.raises(ModflowError) as caught:
        _lakes((_CENTRE, {"stage": {2: 11.0}}))
    assert "period 1" in str(caught.value)


@pytest.mark.parametrize("settings", [{"status": "SIMPLE"}, {"runoff": {1: "x"}}, {"inflow": {0: 1.0}}, {"withdrawal": True}])
def test_a_lak_setting_that_is_not_one_the_engine_takes_refuses(settings):
    with pytest.raises(ModflowError) as caught:
        _lakes((_CENTRE, settings))
    assert next(iter(settings)) in str(caught.value)


def test_a_lak_auxiliary_is_the_packagedata_value():
    packagedata, _, perioddata, _ = _lakes((_CENTRE, {"auxiliary": {"conc": {1: 1.0, 2: 3.0}}}), declared=("conc",))
    assert packagedata == [[0, 12.0, 4, 1.0]]
    assert perioddata == {0: [[0, "auxiliary", "conc", 1.0]], 1: [[0, "auxiliary", "conc", 3.0]]}


def test_a_specified_outlet_writes_its_rate_and_mf6_s_unread_values():
    _, _, perioddata, outlets = _lakes((_CENTRE, {"outlets": [{"to": "outside", "couttype": "specified", "rate": -1.0}]}))
    assert outlets == [[0, 0, -1, "SPECIFIED", -999.0, -999.0, -999.0, -999.0]]
    assert perioddata == {0: [[0, "rate", -1.0]]}


def test_a_manning_outlet_into_another_lake_writes_its_table_and_keyed_values_as_rows():
    outlet = {"to": 2, "couttype": "MANNING", "invert": {1: 11.0, 2: 10.5}, "width": 2.0, "rough": 0.03, "slope": 0.001}
    _, _, perioddata, outlets = _lakes((_CENTRE, {"outlets": [outlet]}), (_LEFT, {}))
    assert outlets == [[0, 0, 1, "MANNING", 11.0, 2.0, 0.03, 0.001]]
    assert perioddata == {0: [[0, "invert", 11.0]], 1: [[0, "invert", 10.5]]}


def test_a_weir_outlet_reads_invert_and_width_only():
    _, _, _, outlets = _lakes((_CENTRE, {"outlets": [{"to": 0, "couttype": "WEIR", "invert": 11.0, "width": 2.0}]}))
    assert outlets == [[0, 0, -1, "WEIR", 11.0, 2.0, -999.0, -999.0]]


@pytest.mark.parametrize("outlet,named", [
    ({"to": 0, "couttype": "SPECIFIED"}, "rate"),
    ({"to": 0, "couttype": "MANNING", "invert": 1.0, "width": 1.0, "rough": 0.03}, "slope"),
    ({"to": 0, "couttype": "SPECIFIED", "rate": -1.0, "invert": 1.0}, "invert"),
    ({"to": 0, "couttype": "WEIR", "invert": 1.0, "width": 1.0, "slope": 0.1}, "slope"),
    ({"to": 0, "couttype": "WEIR", "invert": {2: 1.0}, "width": 1.0}, "invert"),
    ({"to": 0, "couttype": "DAM"}, "DAM"), ({"to": -1, "couttype": "SPECIFIED", "rate": 1.0}, "to"),
    ({"to": "x", "couttype": "SPECIFIED", "rate": 1.0}, "to")])
def test_an_outlet_whose_values_the_type_does_not_read_or_leaves_out_refuses_by_name(outlet, named):
    with pytest.raises(ModflowError) as caught:
        _lakes((_CENTRE, {"outlets": [outlet]}))
    assert named in str(caught.value)


def test_an_outlet_into_no_lake_or_its_own_or_from_several_polygons_refuses():
    specified = {"couttype": "SPECIFIED", "rate": -1.0}
    for to in (3, 1):
        with pytest.raises(ModflowError) as caught:
            _lakes((_CENTRE, {"outlets": [{"to": to, **specified}]}), (_LEFT, {}))
        assert "outlet 1" in str(caught.value)
    two = {"type": "MultiPolygon", "coordinates": [_CENTRE["coordinates"], _LEFT["coordinates"]]}
    with pytest.raises(ModflowError) as caught:
        _lakes((two, {"outlets": [{"to": 0, **specified}]}))
    assert "one lake" in str(caught.value)


def test_lak_settings_and_outlets_write_and_mf6_validates(staged_beside, tmp_path):
    sheet = _fill({"Lakes": [
        {"geometry": "a", **_LAKE, "stage": {1: 12.0, 2: 11.0}, "rainfall": 0.01, "inflow": {2: 3.0},
         "outlets": [{"to": 2, "couttype": "MANNING", "invert": 11.0, "width": 2.0, "rough": 0.03, "slope": 0.001}]},
        {"geometry": "b", **_LAKE, "stage": 9.0, "status": {1: "ACTIVE", 3: "CONSTANT"},
         "outlets": [{"to": "outside", "couttype": "SPECIFIED", "rate": {1: -0.5, 2: -0.25}}]}]},
        a=_CENTRE, b=_LEFT)
    rundir, text = _written(sheet, tmp_path, "gwf.lak")
    assert "NOUTLETS  2" in text and "BEGIN outlets" in text and "MANNING" in text
    assert "  1  inflow" in text and "  2  rate" in text and "  2  status  CONSTANT" in text
    assert validate_authored_simulation(rundir) == {"rc": 0}


@pytest.mark.parametrize("keyword,value", [("LAK_NOUTLETS", 1), ("LAK_OUTLETS", [[0, 0, -1, "SPECIFIED", -999.0, -999.0, -999.0, -999.0]])])
def test_stating_the_outlet_keywords_beside_composite_outlets_refuses(staged_beside, tmp_path, keyword, value):
    sheet = _fill({"Lakes": {"geometry": "a", **_LAKE, "outlets": [{"to": 0, "couttype": "SPECIFIED", "rate": -1.0}]},
                   keyword: value}, a=_CENTRE)
    mesh = _mesh(tmp_path)
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir()
    shutil.copy(out["disv"], rundir / "mesh.disv")
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, rundir, mesh)
    assert keyword in str(caught.value)


# ---- SFR: gwf-sfr.dfn, block period: status, bedk, manning, stage, inflow, rainfall, evaporation, runoff,
# ---- upstream_fraction, auxiliary (diversion and cross_section name a diversion topology and a table file)

_STREAM = {"width": 5.0, "slope": 0.001, "bed_top": 9.0, "bed_thickness": 1.0, "bed_conductivity": 1.0e-5,
           "manning": 0.03}
_MAIN = _line((2.0, 15.0), (28.0, 15.0))


def _streams(*statements, declared=()):
    produced = {f"line{n}": line for n, (line, _) in enumerate(statements)}
    sheet = _fill({"Streams": [{"geometry": f"line{n}", **_STREAM, **body} for n, (_, body) in enumerate(statements)]},
                  **produced)
    return place_streams(sheet.filled["SFR_PACKAGEDATA"].value["streams"], nlay=1, frame=("meters", "days"),
                         auxiliary=declared, **_MESH)


@pytest.mark.parametrize("settings,keyword,value", [
    ({"bedk": 2.0e-5}, "bedk", 2.0e-5), ({"stage": 9.5}, "stage", 9.5), ({"rainfall": 0.01}, "rainfall", 0.01),
    ({"evaporation": 0.02}, "evaporation", 0.02), ({"runoff": 0.5}, "runoff", 0.5),
    ({"upstream_fraction": 0.8}, "upstream_fraction", 0.8), ({"status": "inactive"}, "status", "INACTIVE")])
def test_each_sfr_setting_is_written_to_every_reach_of_the_line(settings, keyword, value):
    perioddata = _streams((_MAIN, settings))[2]
    assert perioddata == {0: [[reach, keyword, value] for reach in range(3)]}


def test_an_sfr_setting_not_stated_is_absent():
    assert _streams((_MAIN, {}))[2] == {}


def test_sfr_inflow_enters_each_line_at_its_upstream_reach():
    perioddata = _streams((_MAIN, {"inflow": 4.0}), (_line((15.0, 28.0), (15.0, 13.0)), {"inflow": 1.0}))[2]
    assert sorted(perioddata[0]) == [[0, "inflow", 4.0], [1, "inflow", 1.0]]


def test_an_sfr_setting_keyed_by_period_is_a_row_of_each_period_it_names():
    perioddata = _streams((_MAIN, {"status": {2: "SIMPLE"}, "stage": {2: 9.5, 3: 9.0}}))[2]
    assert perioddata == {1: [[reach, name, value] for reach in range(3)
                               for name, value in (("status", "SIMPLE"), ("stage", 9.5))],
                          2: [[reach, "stage", 9.0] for reach in range(3)]}


def test_an_sfr_manning_keyed_by_period_is_the_reach_s_own_from_its_first_period():
    packagedata, _, perioddata = _streams((_MAIN, {"manning": {1: 0.03, 3: 0.05}}))
    assert [row[9] for row in packagedata] == [0.03] * 3
    assert perioddata == {0: [[reach, "manning", 0.03] for reach in range(3)],
                          2: [[reach, "manning", 0.05] for reach in range(3)]}
    with pytest.raises(ModflowError) as caught:
        _streams((_MAIN, {"manning": {2: 0.05}}))
    assert "manning" in str(caught.value) and "period 1" in str(caught.value)


@pytest.mark.parametrize("settings", [{"status": "CONSTANT"}, {"stage": True}, {"inflow": {0: 1.0}},
                                      {"diversion": {1: 1.0}}, {"cross_section": "x.tab"}])
def test_an_sfr_setting_that_is_not_one_the_composite_takes_refuses(settings):
    with pytest.raises(ModflowError) as caught:
        _streams((_MAIN, settings))
    assert next(iter(settings)) in str(caught.value)


def test_an_sfr_auxiliary_is_the_packagedata_value_and_the_keyed_ones_are_rows():
    packagedata, _, perioddata = _streams((_MAIN, {"auxiliary": {"conc": {1: 1.0, 2: 2.0}}}), declared=("conc",))
    assert [row[13:] for row in packagedata] == [[1.0]] * 3
    assert perioddata == {0: [[reach, "auxiliary", "conc", 1.0] for reach in range(3)],
                          1: [[reach, "auxiliary", "conc", 2.0] for reach in range(3)]}


def test_sfr_settings_write_and_mf6_validates(staged_beside, tmp_path):
    sheet = _fill({"Streams": {"geometry": "main", **_STREAM, "inflow": {1: 2.0, 2: 1.0}, "rainfall": 0.01,
                               "status": {1: "ACTIVE", 3: "SIMPLE"}, "stage": {3: 9.5},
                               "manning": {1: 0.03, 2: 0.04}, "auxiliary": {"conc": 1.0}},
                   "SFR_AUXILIARY": ["conc"], "SFR_SAVE_FLOWS": True}, main=_MAIN)
    rundir, text = _written(sheet, tmp_path, "gwf.sfr")
    assert "  1  inflow" in text and "  1  rainfall" in text and "  1  manning" in text
    period_3 = text.split("BEGIN period  3", 1)[1]
    assert "status  SIMPLE" in period_3 and "status  SIMPLE" not in text.split("BEGIN period  3")[0]
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_stating_the_period_keyword_beside_composite_settings_refuses_and_beside_none_does_not(staged_beside, tmp_path):
    rows = {0: [[0, "inflow", 1.0]]}
    sheet = _fill({"Streams": {"geometry": "main", **_STREAM, "rainfall": 0.01}, "SFR_PERIODDATA": rows}, main=_MAIN)
    mesh = _mesh(tmp_path)
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir()
    shutil.copy(out["disv"], rundir / "mesh.disv")
    with pytest.raises(ModflowError) as caught:
        serialize(sheet, rundir, mesh)
    assert "SFR_PERIODDATA" in str(caught.value)
    sheet = _fill({"Streams": {"geometry": "main", **_STREAM}, "SFR_PERIODDATA": rows}, main=_MAIN)
    assert "gwf.sfr" in serialize(sheet, rundir, mesh)["written"]


# ---- UZF: gwf-uzf.dfn, block period: finf, pet, extdp, extwc, ha, hroot, rootact, aux

_ZONE = {"surfdep": 0.1, "vks": 0.5, "thtr": 0.1, "thts": 0.45, "thti": 0.105, "eps": 4.0, "finf": 0.02}
_ET = {"pet": 0.01, "extdp": 1.5}
_UNREAD = [-999.0] * 4


def _zones(*, options=None, declared=(), **settings):
    sheet = _fill({"UnsaturatedZone": {"geometry": "where", **_ZONE, **settings}, "UZF_NTRAILWAVES": 7,
                   "UZF_NWAVESETS": 40, **{f"UZF_{name.upper()}": True for name in (options or {})}},
                  nlay=3, where=_CENTRE)
    return place_uzf(sheet.filled["UZF_PACKAGEDATA"].value["zones"], nlay=3, frame=("meters", "days"),
                     options=options or {}, auxiliary=declared, **_MESH)[1]


def test_a_uzf_finf_keyed_by_period_is_a_land_row_of_each_period_it_names():
    assert _zones(finf={1: 0.02, 3: 0.01}) == {0: [[0, 0.02, *[-999.0] * 6]], 2: [[0, 0.01, *[-999.0] * 6]]}


def test_a_uzf_et_value_keyed_by_period_carries_the_others_in_force():
    perioddata = _zones(options={"simulate_et": True}, **{**_ET, "pet": {1: 0.01, 2: 0.03}})
    assert perioddata == {0: [[0, 0.02, 0.01, 1.5, *_UNREAD]], 1: [[0, 0.02, 0.03, 1.5, *_UNREAD]]}
    perioddata = _zones(options={"simulate_et": True}, **{**_ET, "finf": {1: 0.02, 3: 0.04}, "extdp": {1: 1.5, 2: 2.5}})
    assert perioddata == {0: [[0, 0.02, 0.01, 1.5, *_UNREAD]], 1: [[0, 0.02, 0.01, 2.5, *_UNREAD]],
                          2: [[0, 0.04, 0.01, 2.5, *_UNREAD]]}


def test_a_uzf_value_keyed_by_period_states_the_first_period():
    with pytest.raises(ModflowError) as caught:
        _zones(finf={2: 0.02})
    assert "finf" in str(caught.value) and "period 1" in str(caught.value)
    with pytest.raises(ModflowError) as caught:
        _zones(options={"simulate_et": True}, **{**_ET, "pet": {3: 0.02}})
    assert "pet" in str(caught.value) and "period 1" in str(caught.value)


def test_a_uzf_auxiliary_is_a_column_of_every_land_row_keyed_by_period_or_not():
    assert _zones(declared=("conc",), auxiliary={"conc": 1.5}) == {0: [[0, 0.02, *[-999.0] * 6, 1.5]]}
    perioddata = _zones(declared=("conc",), finf={1: 0.02, 3: 0.01}, auxiliary={"conc": {1: 1.5, 2: 2.5}})
    assert perioddata == {0: [[0, 0.02, *[-999.0] * 6, 1.5]], 1: [[0, 0.02, *[-999.0] * 6, 2.5]],
                          2: [[0, 0.01, *[-999.0] * 6, 2.5]]}


def test_a_uzf_auxiliary_the_option_does_not_name_or_a_named_one_unstated_refuses():
    with pytest.raises(ModflowError) as caught:
        _zones(declared=("conc",), auxiliary={"salt": 1.0})
    assert "salt" in str(caught.value) and "UZF_AUXILIARY" in str(caught.value)
    with pytest.raises(ModflowError) as caught:
        _zones(declared=("conc",))
    assert "conc" in str(caught.value)


def test_a_uzf_value_keyed_by_something_that_is_not_a_period_refuses():
    with pytest.raises(ModflowError) as caught:
        _fill({"UnsaturatedZone": {"geometry": "where", **_ZONE, "finf": {0: 0.02}}}, nlay=3, where=_CENTRE)
    assert "finf" in str(caught.value)


def test_uzf_settings_write_and_mf6_validates(staged_beside, tmp_path):
    sheet = _fill({"UnsaturatedZone": {"geometry": "where", **_ZONE, "finf": {1: 0.02, 3: 0.01}, **_ET,
                                       "pet": {1: 0.01, 2: 0.02}, "auxiliary": {"conc": {1: 1.0, 2: 2.0}}},
                   "UZF_NTRAILWAVES": 7, "UZF_NWAVESETS": 40, "UZF_SIMULATE_ET": True, "UZF_AUXILIARY": ["conc"]},
                  nlay=3, where=_CENTRE)
    rundir, text = _written(sheet, tmp_path, "gwf.uzf")
    assert "BEGIN period  3" in text and "auxiliary  conc" in text
    assert validate_authored_simulation(rundir) == {"rc": 0}
