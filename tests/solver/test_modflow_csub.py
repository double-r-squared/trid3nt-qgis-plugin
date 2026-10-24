"""Interbeds, time series and the boundary packages that give water to the mover, stated through the composites.

The mesh is a 3x3 lattice of 10 m squares in UTM 32617, numbered row by row from the south-west, one layer, three
periods. Offline except the validate cases, which run mf6's own --mode validate through the image.
"""

from __future__ import annotations

import shutil
import struct
from pathlib import Path

import numpy as np
import pytest
from pyproj import Transformer

from trid3nt_server.inputs.point import PointNotViableError
from trid3nt_server.tools.mesh.artifact import MeshArtifact
from trid3nt_server.workflows.modflow.authoring import mesh_files as MF
from trid3nt_server.workflows.modflow.authoring.movers import mover_rows
from trid3nt_server.workflows.modflow.authoring.simulation import serialize
from trid3nt_server.workflows.modflow.authoring.stress import place_interbeds
from trid3nt_server.workflows.modflow.authoring.validate import validate_authored_simulation
from trid3nt_server.workflows.modflow.errors import ModflowError
from trid3nt_server.workflows.modflow.modules import GWF
from trid3nt_server.workflows.modflow.modules.outputs import Solved
from trid3nt_server.workflows.runtime import primitives as P
from trid3nt_server.workflows.runtime.sheet import fill

_SIDE = 3
_N = _SIDE * _SIDE
_POINTS = [(x * 10.0, y * 10.0) for y in range(_SIDE + 1) for x in range(_SIDE + 1)]
_CELLS = [(a, a + 1, a + _SIDE + 2, a + _SIDE + 1)
          for r in range(_SIDE) for c in range(_SIDE)
          for a in [r * (_SIDE + 1) + c]]
_TO_LONLAT = Transformer.from_crs(32617, 4326, always_xy=True).transform
_MESH = dict(points=_POINTS, cells=_CELLS, utm_epsg=32617)
_UNITS = ("meters", "days")
_STREAM = {"width": 5.0, "slope": 0.001, "bed_top": 9.0, "bed_thickness": 1.0, "bed_conductivity": 1.0e-5,
           "manning": 0.03}
_NODELAY = {"layer": 1, "cdelay": "NODELAY", "pcs0": 15.0, "thick_frac": 4.0, "ssv_cc": 0.25, "sse_cr": 0.01, "theta": 0.45}
_DELAY = {**_NODELAY, "cdelay": "delay", "rnb": 1.0, "kv": 2.5e-6, "h0": 1.0}


def _point(x, y):
    return {"type": "Point", "coordinates": list(_TO_LONLAT(x, y))}


def _box(x0, y0, x1, y1):
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return {"type": "Polygon", "coordinates": [[list(_TO_LONLAT(*p)) for p in ring]]}


def _line(*utm):
    return {"type": "LineString", "coordinates": [list(_TO_LONLAT(*p)) for p in utm]}


_PRODUCED = dict(
    whole=_box(1.0, 1.0, 29.0, 29.0), south=_box(1.0, 1.0, 29.0, 9.0), centre=_box(11.0, 11.0, 19.0, 19.0),
    corner=_box(1.0, 1.0, 9.0, 9.0), stream=_line((2.0, 15.0), (28.0, 15.0)), p_stream=_point(5.0, 15.0),
    p_corner=_point(5.0, 5.0), p_centre=_point(15.0, 15.0), p_south_east=_point(25.0, 5.0))


def _fill(stated: dict, **produced):
    return fill(GWF, produced={**_PRODUCED, **produced}, **{
        "DISV_NLAY": 1, "DISV_LENGTH_UNITS": "meters", "DISV_TOP": [10.0] * _N,
        "DISV_BOTM": [[0.0] * _N], "NPF_K": 1.0e-4, "NPF_ICELLTYPE": 0, "IC_STRT": 5.0,
        "TDIS_NPER": 3, "TDIS_TIME_UNITS": "days", "TDIS_PERIODDATA": [[10.0, 1, 1.0]] * 3, **stated})


def _csub(*stated, **produced):
    keywords = {"STO_ICONVERT": 0, "STO_SS": 0.0, "STO_SY": 0.0, "STO_TRANSIENT": {0: True}, "CSUB_CG_THETA": 0.3,
                "CSUB_CG_SKE_CR": 1.0e-5, "CSUB_BETA": 0.0, "CHD_MAXBOUND": 1, "CHD_STRESS_PERIOD_DATA": {0: [[0, 0, 5.0]]}}
    for item in stated:
        keywords.update(item)
    return _fill(keywords, **produced)


def _mesh(tmp_path: Path) -> dict:
    rows = ["MESH2D"]
    for cell, ring in enumerate(_CELLS, start=1):
        rows.append(f"E4Q {cell} " + " ".join(str(v + 1) for v in ring) + " 1")
    for node, (x, y) in enumerate(_POINTS, start=1):
        rows.append(f"ND {node} {x} {y} -2.0")
    path = tmp_path / "mesh.2dm"
    path.write_text("\n".join(rows) + "\n")
    art = MeshArtifact(
        mesh_id="M01", name="lattice", mode="om2d", display_uri=str(path), crs_authid="EPSG:32617",
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


class _Flat(str):
    """A file's text whose membership test ignores the column padding flopy writes."""

    def __contains__(self, needle):
        return " ".join(needle.split()) in " ".join(self.split())


def _written(sheet, tmp_path, package):
    mesh = _mesh(tmp_path)
    out = MF._mesh_files(sheet, mesh)
    rundir = tmp_path / "run"
    rundir.mkdir(exist_ok=True)
    shutil.copy(out["disv"], rundir / "mesh.disv")
    assert package in serialize(sheet, rundir, mesh)["written"]
    return rundir, _Flat((rundir / package).read_text())


def _refused(sheet, tmp_path):
    mesh = _mesh(tmp_path)
    rundir = tmp_path / "run"
    rundir.mkdir(exist_ok=True)
    with pytest.raises((ModflowError, PointNotViableError)) as caught:
        out = MF._mesh_files(sheet, mesh)
        shutil.copy(out["disv"], rundir / "mesh.disv")
        serialize(sheet, rundir, mesh)
    return str(caught.value)


def _interbeds(sheet, nlay=1, **over):
    return place_interbeds(sheet.filled["CSUB_PACKAGEDATA"].value["interbeds"], nlay=nlay, frame=_UNITS, **{**_MESH, **over})


# ---- Interbeds: one interbed per cell the geometry places

def test_every_cell_a_geometry_places_is_one_interbed_numbered_in_statement_then_cell_order(staged_beside, tmp_path):
    sheet = _csub({"Interbeds": [{"geometry": "south", **_NODELAY}, {"geometry": "centre", **_DELAY}]})
    rows = _interbeds(sheet)
    assert [row[:3] for row in rows] == [[0, (0, 0), "nodelay"], [1, (0, 1), "nodelay"], [2, (0, 2), "nodelay"],
                                         [3, (0, 4), "delay"]]
    assert rows[0][3:] == [15.0, 4.0, -999.0, 0.25, 0.01, 0.45, -999.0, -999.0]
    assert rows[3][3:] == [15.0, 4.0, 1.0, 0.25, 0.01, 0.45, 2.5e-6, 1.0]
    rundir, text = _written(sheet, tmp_path, "gwf.csub")
    assert "NINTERBEDS  4" in text
    assert "1 1 1 nodelay 15.00000000 4.00000000 -999.00000000 0.25000000 0.01000000 0.45000000 -999.00000000 -999.00000000" in text
    assert "4 1 5 delay 15.00000000 4.00000000 1.00000000 0.25000000 0.01000000 0.45000000 2.50000000E-06 1.00000000" in text
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_all_layers_of_a_cell_are_each_an_interbed():
    sheet = _csub({"Interbeds": {"geometry": "corner", **_NODELAY, "layer": "all"}})
    rows = place_interbeds(sheet.filled["CSUB_PACKAGEDATA"].value["interbeds"], nlay=2, frame=_UNITS, **_MESH)
    assert [(row[0], row[1]) for row in rows] == [(0, (0, 0)), (1, (1, 0))]


def test_an_inactive_cell_holds_no_interbed():
    sheet = _csub({"Interbeds": {"geometry": "south", **_NODELAY}})
    active = [[1, 0, 1, 1, 1, 1, 1, 1, 1]]
    assert [row[1] for row in _interbeds(sheet, active=active)] == [(0, 0), (0, 2)]


def test_a_raster_names_a_value_whose_dimension_the_options_leave_alone(tmp_path):
    import rasterio
    from rasterio.transform import from_origin

    grid = np.full((3, 3), 5.0e-6, dtype="float32")
    path = tmp_path / "kv.tif"
    with rasterio.open(path, "w", driver="GTiff", height=3, width=3, count=1, dtype="float32", crs="EPSG:32617",
                       transform=from_origin(0.0, 30.0, 10.0, 10.0)) as dst:
        dst.write(grid, 1)
    sheet = _csub({"Interbeds": {"geometry": "centre", **_DELAY, "kv": "kv"}}, kv={"uri": str(path), "units": "m/day"})
    assert _interbeds(sheet)[0][-2] == pytest.approx(5.0e-6)


@pytest.mark.parametrize("change,fragment", [
    ({"cdelay": "sometimes"}, "cdelay"),
    ({"layer": 0}, "layer"),
    ({"thick_frac": None}, "states no thick_frac"),
    ({"ssv_cc": None}, "states no ssv_cc"),
    ({"pcs0": {"uri": "stress.tif", "units": "m"}}, "pcs0 is one number"),
    ({"kv": 1.0}, "NODELAY reads no kv"),
    ({"rnb": 1.0}, "NODELAY reads no rnb"),
    ({"h0": 1.0}, "NODELAY reads no h0"),
    ({"bogus": 1.0}, "bogus is none of them"),
])
def test_an_interbed_missing_a_value_or_stating_one_its_type_does_not_read_refuses_by_name(change, fragment):
    with pytest.raises(ModflowError) as caught:
        _csub({"Interbeds": {"geometry": "south", **{k: v for k, v in {**_NODELAY, **change}.items() if v is not None}}})
    assert fragment in str(caught.value)


@pytest.mark.parametrize("missing", ["rnb", "kv", "h0"])
def test_a_delay_interbed_states_its_own_values(missing):
    with pytest.raises(ModflowError) as caught:
        _csub({"Interbeds": {"geometry": "south", **{k: v for k, v in _DELAY.items() if k != missing}}})
    assert f"states no {missing}" in str(caught.value)


def test_a_name_is_the_boundary_name_exactly_when_boundnames_is_true():
    named = _csub({"Interbeds": {"geometry": "south", **_NODELAY, "name": "ib"}, "CSUB_BOUNDNAMES": True})
    assert [row[-1] for row in _interbeds(named, boundnames=True)] == ["ib"] * 3
    with pytest.raises(ModflowError) as caught:
        _interbeds(_csub({"Interbeds": {"geometry": "south", **_NODELAY}, "CSUB_BOUNDNAMES": True}), boundnames=True)
    assert "CSUB_BOUNDNAMES is true" in str(caught.value)
    with pytest.raises(ModflowError) as caught:
        _interbeds(named)
    assert "CSUB_BOUNDNAMES is not true" in str(caught.value)


def test_a_geometry_holding_no_cell_refuses():
    with pytest.raises(ModflowError) as caught:
        _interbeds(_csub({"Interbeds": {"geometry": "far", **_NODELAY}}, far=_box(100.0, 100.0, 110.0, 110.0)))
    assert "touches no cell" in str(caught.value)


def test_the_interbed_count_and_list_beside_interbeds_refuse(staged_beside, tmp_path):
    sheet = _csub({"Interbeds": {"geometry": "south", **_NODELAY}, "CSUB_NINTERBEDS": 3})
    assert "CSUB_NINTERBEDS" in _refused(sheet, tmp_path)


def test_a_csub_deck_by_keywords_needs_no_period_block(staged_beside, tmp_path):
    sheet = _csub({"CSUB_NINTERBEDS": 1, "CSUB_PACKAGEDATA": [[0, [0, 4], "nodelay", 15.0, 4.0, 1.0, 0.25, 0.01, 0.45, 999.0, 999.0]]})
    rundir, text = _written(sheet, tmp_path, "gwf.csub")
    assert "BEGIN period" not in text
    assert validate_authored_simulation(rundir) == {"rc": 0}


# ---- TimeSeries: the package's series file

_LOAD = {"package": "CSUB", "names": ["LOAD"], "rows": [[0.0, 0.8], [10.0, 0.4], [30.0, 0.0]], "interpolation": "linear", "sfac": 1.05}


def test_a_series_is_the_package_s_file_and_its_rows_name_it(staged_beside, tmp_path):
    sheet = _csub({"Interbeds": {"geometry": "centre", **_NODELAY}, "TimeSeries": _LOAD, "CSUB_MAXSIG0": 1,
                   "CSUB_STRESS_PERIOD_DATA": {0: [[[0, 4], "LOAD"]]}})
    rundir, text = _written(sheet, tmp_path, "gwf.csub")
    assert "TS6 FILEIN csub.ts" in text and "1 5 LOAD" in text
    series = _Flat((rundir / "csub.ts").read_text())
    assert "NAMES LOAD" in series and "METHODS linear" in series and "SFACS 1.05" in series
    assert "10.00000000 0.40000000" in series
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_boundary_package_reads_a_series_by_name(staged_beside, tmp_path):
    sheet = _fill({"CHD_MAXBOUND": 1, "CHD_STRESS_PERIOD_DATA": {0: [[0, 0, "head"]]},
                   "TimeSeries": {"package": "CHD", "names": ["head"], "rows": [[0.0, 5.0], [30.0, 6.0]]}})
    rundir, text = _written(sheet, tmp_path, "gwf.chd")
    assert "TS6 FILEIN chd.ts" in text and "1 1 head" in text
    attributes = (rundir / "chd.ts").read_text().split("BEGIN timeseries")[0]
    assert "METHODS" not in attributes and "SFACS" not in attributes


@pytest.mark.parametrize("series,fragment", [
    ({"rows": [[0.0, 1.0], [0.0, 2.0]]}, "increasing time"),
    ({"rows": [[0.0, 1.0, 2.0]]}, "rows of 2 numbers"),
    ({"names": ["a", "a"], "rows": [[0.0, 1.0, 2.0]]}, "distinct series names"),
    ({"interpolation": "cubic"}, "interpolation"),
    ({"sfac": [1.0, 2.0]}, "sfac"),
    ({"package": ""}, "prefix"),
    ({"extra": 1}, "extra is none of them"),
])
def test_a_series_out_of_shape_refuses_by_name(series, fragment):
    with pytest.raises(ModflowError) as caught:
        _csub({"TimeSeries": {**_LOAD, **series}})
    assert fragment in str(caught.value)


def test_a_series_file_stated_by_its_keyword_refuses(staged_beside, tmp_path):
    sheet = _csub({"CSUB_NINTERBEDS": 0, "CSUB_TS_FILERECORD": "csub.ts"})
    assert "CSUB_TS_FILERECORD" in _refused(sheet, tmp_path)


# ---- the givers: WEL, DRN, GHB and RIV give water to the mover by their row

_GIVERS = {"Wells": ("WEL", {"layer": 1, "rate": -1.0}), "Drains": ("DRN", {"layer": 1, "elevation": 2.0, "conductance": 5.0}),
           "GeneralHead": ("GHB", {"layer": 1, "head": 6.0, "conductance": 5.0}),
           "Rivers": ("RIV", {"layer": 1, "stage": 8.0, "conductance": 5.0, "rbot": 1.0})}
_STREAMS = {"Streams": {"geometry": "stream", **_STREAM, "name": "creek"}}


@pytest.mark.parametrize("composite", list(_GIVERS))
def test_a_boundary_statement_gives_water_through_its_row_number(composite, staged_beside, tmp_path):
    package, values = _GIVERS[composite]
    sheet = _fill({composite: [{"geometry": "south", **values, "name": "first"},
                               {"geometry": "centre", **values, "name": "second"}], **_STREAMS,
                   "WaterMover": {"from": "second", "to": "creek", "to_at": "p_stream", "type": "FACTOR", "value": 0.5}})
    rundir, text = _written(sheet, tmp_path, "gwf.mvr")
    prefix = package.lower()
    assert f"{prefix}_0  4  sfr_0  1  FACTOR       0.50000000" in text
    assert "MOVER" in (rundir / f"gwf.{prefix}").read_text()
    assert validate_authored_simulation(rundir) == {"rc": 0}


def test_a_point_in_the_boundary_s_cell_names_the_row_and_several_rows_in_one_cell_need_a_name():
    package, values = _GIVERS["Wells"]
    sheet = _fill({"Wells": {"geometry": "south", **values, "layer": "all", "name": "pump"}})
    features = [{"package": "WEL", "name": "pump", "number": n, "cells": {n}} for n in range(3)]
    used, periods = mover_rows([{"from": "pump", "from_at": _point(15.0, 5.0), "to": "creek", "type": "UPTO", "value": 1.0}],
                               [*features, {"package": "SFR", "name": "creek", "number": 0, "cells": {7}}], **_MESH)
    assert used == ["WEL", "SFR"] and periods == {0: [["WEL", 1, "SFR", 0, "UPTO", 1.0]]}
    with pytest.raises(ModflowError) as caught:
        mover_rows([{"from": "pump", "to": "creek", "type": "UPTO", "value": 1.0}],
                   [*features, {"package": "SFR", "name": "creek", "number": 0, "cells": {7}}], **_MESH)
    assert "names 3 features" in str(caught.value)
    assert sheet.filled["WEL_STRESS_PERIOD_DATA"].value["name"] == "pump"


def test_a_boundary_takes_no_water_from_the_mover(staged_beside, tmp_path):
    package, values = _GIVERS["Drains"]
    sheet = _fill({"Drains": {"geometry": "south", **values, "name": "relief"}, **_STREAMS,
                   "WaterMover": {"from": "creek", "from_at": "p_stream", "to": "relief", "type": "FACTOR", "value": 1.0}})
    message = _refused(sheet, tmp_path)
    assert "DRN boundary" in message and "SFR, LAK, MAW and UZF" in message


def test_a_boundary_with_no_mover_option_stated_false_refuses(staged_beside, tmp_path):
    package, values = _GIVERS["Rivers"]
    sheet = _fill({"Rivers": {"geometry": "centre", **values, "name": "creek2"}, **_STREAMS, "RIV_MOVER": False,
                   "WaterMover": {"from": "creek2", "to": "creek", "to_at": "p_stream", "type": "FACTOR", "value": 1.0}})
    assert "RIV_MOVER" in _refused(sheet, tmp_path)


def test_a_river_statement_places_stage_conductance_and_bottom(staged_beside, tmp_path):
    package, values = _GIVERS["Rivers"]
    rundir, text = _written(_fill({"Rivers": {"geometry": "corner", **values}}), tmp_path, "gwf.riv")
    assert "MAXBOUND  1" in text and "1 1 8.00000000E+00 5.00000000E+00 1.00000000E+00" in text
    assert validate_authored_simulation(rundir) == {"rc": 0}


# ---- compaction and subsidence: the files the CSUB output records name

def _binary(path: Path, text: str, steps: list[list[list[float]]]) -> None:
    """A head-layout binary: per saved time and layer a header (kstp, kper, pertim, totim, text, ncpl, 1, layer) then the cell values."""
    with open(path, "wb") as out:
        for kstp, layers in enumerate(steps, start=1):
            for layer, values in enumerate(layers, start=1):
                out.write(struct.pack("<iidd16siii", kstp, 1, float(kstp), float(kstp), text.rjust(16).encode(), len(values), 1, layer))
                out.write(np.asarray(values, dtype="<f8").tobytes())


def _solved(tmp_path, monkeypatch, fake_s3, **over):
    mesh = tmp_path / "mesh.2dm"
    rows = ["MESH2D"]
    for cell, ring in enumerate(_CELLS, start=1):
        rows.append(f"E4Q {cell} " + " ".join(str(v + 1) for v in ring) + " 1")
    for node, (x, y) in enumerate(_POINTS, start=1):
        rows.append(f"ND {node} {x} {y} -2.0")
    mesh.write_text("\n".join(rows) + "\n")
    art = MeshArtifact(mesh_id="M01", name="lattice", mode="om2d", display_uri=str(mesh), crs_authid="EPSG:32617",
                       has_bathymetry=True, node_count=len(_POINTS), element_count=len(_CELLS),
                       bbox=(0.0, 0.0, 30.0, 30.0), utm_epsg=32617)
    compaction = [[[0.1 * n * (cell + 1) for cell in range(_N)]] for n in (1, 2)]
    _binary(tmp_path / "compaction.bin", "CSUB-COMPACTION", compaction)
    _binary(tmp_path / "zdisp.bin", "CSUB-ZDISPLACE", [[[-v for v in layer] for layer in step] for step in compaction])
    monkeypatch.setattr("trid3nt_server.workflows.solver.solver.download_result",
                        lambda run_id, basename: str(shutil.copy(tmp_path / basename, tmp_path / f"dl_{basename}")))
    sheet = _csub({"Interbeds": {"geometry": "centre", **_NODELAY}, "CSUB_COMPACTION_FILERECORD": "compaction.bin",
                   "CSUB_ZDISPLACEMENT_FILERECORD": "zdisp.bin", **over})
    run = {"run_id": "R1", "utm_epsg": 32617, "sheet": sheet, "reference_time": "2024-06-01T00:00:00Z",
           "vertical_frame": "NAVD88", "mesh": {"artifact": art, "display_uri": str(mesh)}}
    return Solved(run, GWF)


def test_compaction_and_subsidence_read_off_the_files_csub_saved(tmp_path, monkeypatch, fake_s3):
    solved = _solved(tmp_path, monkeypatch, fake_s3)
    compaction = GWF.READS["field"](P.field("COMPACTION", t="every", plane=1), solved)
    assert compaction.units == "meters" and compaction.frames == 2
    assert compaction.values[1].tolist() == pytest.approx([0.2 * (cell + 1) for cell in range(_N)])
    subsidence = GWF.READS["field"](P.field("SUBSIDENCE", t=-1, plane=1), solved)
    assert subsidence.values[0].tolist() == pytest.approx([-0.2 * (cell + 1) for cell in range(_N)])


def test_a_run_without_the_csub_package_has_no_compaction_row_and_one_that_did_not_save_it_says_which_keyword(tmp_path, monkeypatch, fake_s3):
    solved = _solved(tmp_path, monkeypatch, fake_s3)
    assert solved.carried("COMPACTION") and solved.carried("HEAD")
    assert solved.unsaved("COMPACTION") is None
    bare = Solved({**solved.run, "sheet": _fill({"IC_STRT": 5.0})}, GWF)
    assert not bare.carried("COMPACTION") and not bare.carried("SUBSIDENCE") and bare.carried("HEAD")
    unsaved = Solved({**solved.run, "sheet": _csub({"CSUB_NINTERBEDS": 0})}, GWF)
    assert unsaved.carried("COMPACTION") and "CSUB_COMPACTION_FILERECORD" in str(unsaved.unsaved("COMPACTION"))
