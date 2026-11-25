"""What the MODFLOW 6 dictionary lets a study tune: every REAL array and list column, by its MF6 name.

The decks are built here by flopy (a 2x2 DISV grid, two layers where a layer matters) and their list packages
written as mf6 writes them, so the reader sees real engine text. A deck the calibrate fixture carries couples GWT.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import flopy
import pytest

from trid3nt.workflows.pestpp.authoring import modflow as piece
from trid3nt.workflows.pestpp.errors import PestppError

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pestpp"


def _grid(gwf, nlay, origin=(0.0, 0.0)):
    x0, y0 = origin
    flopy.mf6.ModflowGwfdisv(
        gwf, nlay=nlay, ncpl=4, nvert=9, top=10.0,
        botm=[10.0 - 5.0 * (k + 1) for k in range(nlay)], length_units="meters",
        vertices=[[0, x0, y0], [1, x0 + 100, y0], [2, x0 + 200, y0], [3, x0, y0 + 100],
                  [4, x0 + 100, y0 + 100], [5, x0 + 200, y0 + 100],
                  [6, x0, y0 + 200], [7, x0 + 100, y0 + 200], [8, x0 + 200, y0 + 200]],
        cell2d=[[0, x0 + 50, y0 + 50, 4, 0, 3, 4, 1], [1, x0 + 150, y0 + 50, 4, 1, 4, 5, 2],
                [2, x0 + 50, y0 + 150, 4, 3, 6, 7, 4], [3, x0 + 150, y0 + 150, 4, 4, 7, 8, 5]])


def _riv(where: Path, instance: str, conds=(100.0, 100.0, 100.0, 100.0)) -> None:
    file = f"gwf.{instance}.riv"
    rows = "".join(f"  1 {cell} {8.0 + cell} {cond}\n" for cell, cond in enumerate(conds, start=1))
    (where / file).write_text(
        "BEGIN options\nEND options\nBEGIN dimensions\n  MAXBOUND 4\nEND dimensions\nBEGIN period 1\n"
        + rows + "END period 1\n")
    nam = (where / "gwf.nam").read_text().replace("END packages", f"  RIV6  {file}  {instance}\nEND packages")
    (where / "gwf.nam").write_text(nam)


def _build(where: Path, nlay: int = 1, k=None, rivs: int = 0, conds=(100.0, 100.0, 100.0, 100.0),
           origin=(0.0, 0.0)) -> piece.Deck:
    sim = flopy.mf6.MFSimulation(sim_ws=str(where), exe_name="mf6")
    flopy.mf6.ModflowTdis(sim, nper=1, perioddata=[(1.0, 1, 1.0)])
    flopy.mf6.ModflowIms(sim)
    gwf = flopy.mf6.ModflowGwf(sim, modelname="gwf")
    _grid(gwf, nlay, origin)
    flopy.mf6.ModflowGwfnpf(gwf, k=k if k is not None else 5.0, icelltype=1)
    flopy.mf6.ModflowGwfic(gwf, strt=9.0)
    sim.write_simulation(silent=True)
    for index in range(rivs):
        _riv(where, f"riv_{index}", conds)
    return piece.deck(where)


def _riv_file(where: Path, instance: str) -> str:
    return (where / f"gwf.{instance}.riv").read_text()


def test_a_real_array_read_from_its_own_file_is_tuned(tmp_path):
    deck_dir = tmp_path / "deck"
    _build(deck_dir)
    lines, out, skip = (deck_dir / "gwf.npf").read_text().splitlines(), [], False
    for line in lines:
        if line.strip() == "k":
            out.extend([line, "    OPEN/CLOSE  k.dat  FACTOR  1.0"])
            skip = True
            continue
        if skip:
            skip = not line.strip().startswith("END")
            continue
        out.append(line)
    (deck_dir / "gwf.npf").write_text("\n".join(out) + "\n")
    (deck_dir / "k.dat").write_text("5.0 5.0 5.0 5.0\n")
    the_deck = piece.deck(deck_dir)
    parameter, pair = piece.multiplier(deck_dir, "NPF_K", lower=0.5, upper=2.0, the_deck=the_deck)
    assert pair == ("gwf.npf.tpl", "gwf.npf") and parameter.scale == 1.0
    assert "OPEN/CLOSE  k.dat  FACTOR  ~npf_k" in (deck_dir / "gwf.npf.tpl").read_text()


def test_per_layer_factors_differ(tmp_path):
    the_deck = _build(tmp_path / "deck", nlay=2, k=[[5.0, 6.0, 7.0, 8.0], [9.0, 10.0, 11.0, 12.0]])
    piece.multiplier(tmp_path / "deck", "NPF_K@2", lower=0.1, upper=10.0, the_deck=the_deck)
    template = (tmp_path / "deck" / "gwf.npf.tpl").read_text()
    first, second = template.split("    INTERNAL  FACTOR  ")[1:3]
    assert first.startswith("1.0") and second.startswith("~npf_k_l2")


def test_a_second_riv_package_is_tuned_by_its_name(tmp_path):
    the_deck = _build(tmp_path / "deck", rivs=2)
    with pytest.raises(PestppError) as ambiguous:
        piece.multiplier(tmp_path / "deck", "RIV_COND", lower=0.5, upper=2.0, the_deck=the_deck)
    assert ambiguous.value.error_code == "PESTPP_PARAMETER_UNPLACED" and "riv_0" in str(ambiguous.value)
    parameter, pair = piece.multiplier(tmp_path / "deck", "RIV_1_COND", lower=0.5, upper=2.0, the_deck=the_deck)
    assert pair == ("gwf.riv_1.riv.tpl", "gwf.riv_1.riv") and parameter.scale == 100.0
    assert "~riv_1_cond" in (tmp_path / "deck" / "gwf.riv_1.riv.tpl").read_text()


def test_a_list_column_per_zone_writes_one_parameter_per_zone_and_leaves_other_rows(tmp_path):
    the_deck = _build(tmp_path / "deck", rivs=1)
    parameters, pairs = piece.zoned(
        tmp_path / "deck", "RIV_COND",
        [{"cells": [1], "source": "the first cell"}, {"cells": [2], "source": "the second cell"}],
        lower=0.5, upper=2.0, the_deck=the_deck)
    assert [(p.name, p.zone, p.zone_source) for p in parameters] == [
        ("riv_cond_z1", "cells", "the first cell"), ("riv_cond_z2", "cells", "the second cell")]
    rows = [piece._tokens(line) for line in (tmp_path / "deck" / "gwf.riv_0.riv.tpl").read_text().splitlines()
            if line.strip().startswith("1 ")]
    assert [row[3].replace(" ", "") for row in rows] == ["~riv_cond_z1~", "~riv_cond_z2~", "100.0", "100.0"]


def test_a_polygon_zone_naming_its_crs_is_read_in_that_frame(tmp_path):
    origin = (500000.0, 4600000.0)
    the_deck = _build(tmp_path / "deck", rivs=1, origin=origin)
    polygon = [[origin[0] + 100.0, origin[1]], [origin[0] + 200.0, origin[1]],
               [origin[0] + 200.0, origin[1] + 200.0], [origin[0] + 100.0, origin[1] + 200.0]]
    parameters, _pairs = piece.zoned(tmp_path / "deck", "RIV_COND",
                                   [{"polygon": polygon, "crs": "EPSG:32615", "source": "the east half"}],
                                   lower=0.5, upper=2.0, the_deck=the_deck, frame_epsg=32615)
    assert [p.name for p in parameters] == ["riv_cond_z1"]
    rows = [piece._tokens(line) for line in (tmp_path / "deck" / "gwf.riv_0.riv.tpl").read_text().splitlines()
            if line.strip().startswith("1 ")]
    assert [row[3].replace(" ", "") for row in rows] == ["100.0", "~riv_cond_z1~", "100.0", "~riv_cond_z1~"]


def test_a_polygon_zone_without_a_crs_is_longitude_latitude(tmp_path):
    from pyproj import Transformer

    origin = (500000.0, 4600000.0)
    the_deck = _build(tmp_path / "deck", rivs=1, origin=origin)
    to_lonlat = Transformer.from_crs(32615, 4326, always_xy=True).transform
    corners = [(origin[0] + 110.0, origin[1] + 10.0), (origin[0] + 190.0, origin[1] + 10.0),
               (origin[0] + 190.0, origin[1] + 190.0), (origin[0] + 110.0, origin[1] + 190.0)]
    polygon = [list(to_lonlat(x, y)) for x, y in corners]
    parameters, _pairs = piece.zoned(tmp_path / "deck", "RIV_COND",
                                   [{"polygon": polygon, "source": "the east half as lon/lat"}],
                                   lower=0.5, upper=2.0, the_deck=the_deck, frame_epsg=32615)
    assert [p.name for p in parameters] == ["riv_cond_z1"]
    rows = [piece._tokens(line) for line in (tmp_path / "deck" / "gwf.riv_0.riv.tpl").read_text().splitlines()
            if line.strip().startswith("1 ")]
    assert [row[3].replace(" ", "") for row in rows] == ["100.0", "~riv_cond_z1~", "100.0", "~riv_cond_z1~"]


def test_overlapping_zones_refuse(tmp_path):
    the_deck = _build(tmp_path / "deck", rivs=1)
    with pytest.raises(PestppError) as overlap:
        piece.zoned(tmp_path / "deck", "RIV_COND", [{"cells": [1, 2]}, {"cells": [2]}],
                    lower=0.5, upper=2.0, the_deck=the_deck)
    assert overlap.value.error_code == "PESTPP_ZONE_OVERLAP"


def test_multi_layer_pilot_points_krige_per_layer(tmp_path):
    the_deck = _build(tmp_path / "deck", nlay=2, k=[[5.0, 6.0, 7.0, 8.0], [9.0, 10.0, 11.0, 12.0]])
    field = piece.fields({"NPF_K": {"spacing": 100.0, "source": "a reader's choice"}}, the_deck)["NPF_K"]
    parameters, pairs = piece.pilot_points(tmp_path / "deck", "NPF_K", field, lower=0.1, upper=10.0,
                                           the_deck=the_deck)
    assert sorted(p.name for p in parameters) == ["npf_k_l1_pp0000", "npf_k_l2_pp0000"]
    assert pairs == [("npf_k_l1.pp.dat.tpl", "npf_k_l1.pp.dat"), ("npf_k_l2.pp.dat.tpl", "npf_k_l2.pp.dat")]
    spec = json.loads((tmp_path / "deck" / piece.PILOT_POINTS).read_text())["arrays"]
    assert [entry["array"] for entry in spec] == ["gwf.npf.k.l1.dat", "gwf.npf.k.l2.dat"]
    assert [len((tmp_path / "deck" / entry["array"]).read_text().split()) for entry in spec] == [4, 4]
    assert "OPEN/CLOSE  gwf.npf.k.l1.dat" in (tmp_path / "deck" / "gwf.npf").read_text()


def test_a_gwt_array_is_tuned_when_the_deck_couples_gwt(tmp_path):
    deck_dir = tmp_path / "deck"
    shutil.copytree(_FIXTURES / "calibrate" / "deck", deck_dir)
    the_deck = piece.deck(deck_dir)
    parameter, pair = piece.multiplier(deck_dir, "MST_POROSITY", lower=0.5, upper=2.0, the_deck=the_deck)
    assert pair == ("gwt.mst.tpl", "gwt.mst") and parameter.scale == 0.3
    assert "~mst_porosity" in (deck_dir / "gwt.mst.tpl").read_text()


def test_describe_keywords_lists_a_list_column_read_only():
    from trid3nt.tools.search.describe_keywords.describe_keywords import describe_keywords

    entry = describe_keywords(module="gwf", keyword="RIV_COND")["entry"]
    assert entry["settable"] is False and entry["calibration_target"] is True
    assert entry["package"] == "RIV" and entry["column"] == "COND"
    matches = describe_keywords(module="gwf", query="river conductance")["calibration_columns"]
    assert any(row["package"] == "RIV" and row["keyword"] == "COND" for row in matches)
    assert all(row["settable"] is False for row in matches)


def test_a_zoned_column_whose_rows_differ_is_laid_by_the_trial(tmp_path):
    the_deck = _build(tmp_path / "deck", rivs=1, conds=(100.0, 110.0, 120.0, 130.0))
    parameters, pairs = piece.zoned(
        tmp_path / "deck", "RIV_COND", [{"cells": [1, 2], "source": "west"},
                                        {"cells": [3, 4], "source": "east"}],
        lower=0.5, upper=2.0, the_deck=the_deck)
    assert [p.name for p in parameters] == ["riv_cond_z1", "riv_cond_z2"]
    assert pairs == [("riv_cond_z1.zone.dat.tpl", "riv_cond_z1.zone.dat"),
                     ("riv_cond_z2.zone.dat.tpl", "riv_cond_z2.zone.dat")]
    assert "~" not in (tmp_path / "deck" / "gwf.riv_0.riv").read_text()
    spec = json.loads((tmp_path / "deck" / piece.PILOT_POINTS).read_text())["columns"]
    assert [(entry["name"], entry["file"], entry["lines"]) for entry in spec] == [
        ("riv_cond_z1", "gwf.riv_0.riv", [6, 7]), ("riv_cond_z2", "gwf.riv_0.riv", [8, 9])]
    (tmp_path / "deck" / "riv_cond_z1.zone.dat").write_text("riv_cond_z1 2.0\n")
    (tmp_path / "deck" / "riv_cond_z2.zone.dat").write_text("riv_cond_z2 3.0\n")
    assert piece.lay(tmp_path / "deck") == [piece.PILOT_POINTS, "riv_cond_z1.zone.dat", "riv_cond_z2.zone.dat"]
    rows = [line.split() for line in (tmp_path / "deck" / "gwf.riv_0.riv").read_text().splitlines()
            if line.strip().startswith("1 ")]
    assert [float(row[3]) for row in rows] == [200.0, 220.0, 360.0, 390.0]


def test_a_native_table_restates_a_differing_column_with_one_scalar(tmp_path):
    _build(tmp_path / "deck", rivs=1, conds=(100.0, 110.0, 120.0, 130.0))
    piece.patch_columns(tmp_path / "deck", {"RIV_COND": 250.0})
    rows = [line.split() for line in (tmp_path / "deck" / "gwf.riv_0.riv").read_text().splitlines()
            if line.strip().startswith("1 ")]
    assert [float(row[3]) for row in rows] == [250.0, 250.0, 250.0, 250.0]
