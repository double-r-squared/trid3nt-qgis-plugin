"""What the TELEMAC module dictionaries let a study tune: every REAL keyword of the host and its coupled bodies, whole
or by list entry, and the spatial fields (friction, the node-scatter map, the bed) by zones or pilot points.

The decks are built here on a small 4x4-node mesh with real SELAFIN and boundary files, and the coupled bodies are
steering files the host names, so the reader sees engine text. The Missouri fixture proves the default scalar friction
still authors byte-identical control, template, instruction and outflow-level files.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pyproj import Transformer

from trid3nt.workflows.pestpp.authoring import telemac as piece
from trid3nt.workflows.pestpp.authoring.control import Observed, prior
from trid3nt.workflows.pestpp.errors import PestppError
from trid3nt.workflows.telemac.authoring.selafin_io import read_selafin, write_telemac_pair
from trid3nt.workflows.telemac.authoring.staged_check import read_steering

_ROOT = Path(__file__).resolve().parents[2]
_MISSOURI = _ROOT / "tests" / "fixtures" / "telemac_missouri"
_BASELINE = _ROOT / "tests" / "fixtures" / "pestpp" / "telemac_baseline"
_TRIAL = _ROOT / "workers" / "pestpp" / "telemac_trial.py"


def _engine_installed() -> bool:
    from trid3nt.workflows.telemac.authoring.cas_validate import EngineHomeMissing, engine_home

    try:
        engine_home()
        return True
    except EngineHomeMissing:
        return False


_ENGINE = pytest.mark.skipif(not _engine_installed(),
                             reason="the opentelemac tree is not installed (scripts/fetch_opentelemac.sh)")


def _mesh(base: Path) -> tuple[Path, np.ndarray, np.ndarray, np.ndarray]:
    """A 4x4-node, 18-triangle mesh on 200 m cells, its deck directory, and its coordinates."""
    deck = base / "deck"
    deck.mkdir(parents=True)
    x, y = [], []
    for j in range(4):
        for i in range(4):
            x.append(i * 200.0)
            y.append(j * 200.0)
    cells = []
    for j in range(3):
        for i in range(3):
            a = j * 4 + i
            cells.append([a, a + 1, a + 5])
            cells.append([a, a + 5, a + 4])
    x, y, cells = np.array(x), np.array(y), np.array(cells, dtype=int)
    write_telemac_pair(deck, x=x, y=y, cells=cells, bed=np.linspace(10.0, 9.0, len(x)))
    return deck, x, y, cells


def _deck(deck: Path, cas: str = "t.cas", module: str = "telemac2d") -> piece.Deck:
    stated = read_steering(deck / cas)
    return piece.Deck(steering=cas, stated=stated, module=module, result="r", utm_epsg=32615,
                      cell_size=piece._deck_cell_size(deck, stated))


def _copy(base: Path, name: str) -> Path:
    where = base / name
    shutil.copytree(base / "deck", where)
    return where


def _observe() -> list[tuple[Observed, int]]:
    return [(Observed("level_1", 292.0, "levels", 0.1, "cited"), 1)]


def test_an_integer_logical_string_or_file_keyword_refuses_by_type(tmp_path):
    deck, *_ = _mesh(tmp_path)
    (deck / "t.cas").write_text(
        "LANGUAGE = 2\nGEOMETRY FILE = 'mesh.slf'\nBOUNDARY CONDITIONS FILE = 'mesh.cli'\n"
        "LAW OF BOTTOM FRICTION = 3\nFRICTION COEFFICIENT = 33.0\n")
    the_deck = _deck(deck)
    for keyword, declared in (("LAW_OF_BOTTOM_FRICTION", "INTEGER"), ("TIDAL_FLATS", "LOGICAL"),
                              ("TITLE", "STRING"), ("FRICTION_DATA_FILE", "STRING")):
        with pytest.raises(PestppError) as refused:
            piece.multiplier(deck, keyword, lower=0.5, upper=2.0, the_deck=the_deck)
        assert refused.value.error_code == "PESTPP_PARAMETER_NOT_REAL"
        assert keyword in str(refused.value) and declared in str(refused.value)


def test_an_unknown_or_uncoupled_keyword_refuses_naming_the_active_bodies(tmp_path):
    deck, *_ = _mesh(tmp_path)
    (deck / "t.cas").write_text(
        "LANGUAGE = 2\nGEOMETRY FILE = 'mesh.slf'\nBOUNDARY CONDITIONS FILE = 'mesh.cli'\n"
        "FRICTION COEFFICIENT = 33.0\n")
    the_deck = _deck(deck)
    for name in ("NOPE", "gaia: CLASSES_SHIELDS_PARAMETERS"):
        with pytest.raises(PestppError) as refused:
            piece.multiplier(deck, name, lower=0.5, upper=2.0, the_deck=the_deck)
        assert refused.value.error_code == "PESTPP_PARAMETER_UNPLACED"
        assert "telemac2d" in str(refused.value)


def test_any_real_keyword_is_templated_by_its_own_name(tmp_path):
    deck, *_ = _mesh(tmp_path)
    (deck / "t.cas").write_text(
        "LANGUAGE = 2\nGEOMETRY FILE = 'mesh.slf'\nBOUNDARY CONDITIONS FILE = 'mesh.cli'\n"
        "FRICTION COEFFICIENT = 33.0\nVELOCITY DIFFUSIVITY = 0.000001\n"
        "PRESCRIBED FLOWRATES = 10.0;20.0;30.0\n")
    the_deck = _deck(deck)
    parameter, pair = piece.multiplier(deck, "VELOCITY_DIFFUSIVITY", lower=0.5, upper=2.0, the_deck=the_deck)
    assert (parameter.name, parameter.scale, pair) == ("velocity_diffusivity", 1.0e-6, ("t.cas.tpl", "t.cas"))
    assert "VELOCITY DIFFUSIVITY = ~velocity_diffusivity" in (deck / "t.cas.tpl").read_text()

    entry, _pair = piece.multiplier(deck, "PRESCRIBED_FLOWRATES@2", lower=0.5, upper=2.0, the_deck=the_deck)
    assert (entry.name, entry.scale) == ("prescribed_flowrates_e2", 20.0)
    assert "PRESCRIBED FLOWRATES = 10.0;~prescribed_flowrates_e2~;30.0" in (deck / "t.cas.tpl").read_text()


@_ENGINE
def test_a_dictionary_defaulted_real_keyword_is_restated_and_templated(tmp_path):
    deck, *_ = _mesh(tmp_path)
    (deck / "t.cas").write_text(
        "LANGUAGE = 2\nGEOMETRY FILE = 'mesh.slf'\nBOUNDARY CONDITIONS FILE = 'mesh.cli'\n"
        "FRICTION COEFFICIENT = 33.0\n")
    the_deck = _deck(deck)
    parameter, pair = piece.multiplier(deck, "VELOCITY_DIFFUSIVITY", lower=0.5, upper=2.0, the_deck=the_deck)
    assert parameter.scale == pytest.approx(1.0e-6)
    assert "VELOCITY DIFFUSIVITY = 1e-06" in (deck / "t.cas").read_text()
    assert "VELOCITY DIFFUSIVITY = ~velocity_diffusivity" in (deck / pair[0]).read_text()


def test_an_absent_real_keyword_with_no_nonzero_default_refuses_by_name(tmp_path):
    deck, *_ = _mesh(tmp_path)
    (deck / "t.cas").write_text(
        "LANGUAGE = 2\nGEOMETRY FILE = 'mesh.slf'\nBOUNDARY CONDITIONS FILE = 'mesh.cli'\n"
        "FRICTION COEFFICIENT = 33.0\n")
    the_deck = _deck(deck)
    with pytest.raises(PestppError) as refused:
        piece.multiplier(deck, "PRESCRIBED_ELEVATIONS", lower=0.5, upper=2.0, the_deck=the_deck)
    assert refused.value.error_code == "PESTPP_PARAMETER_UNPLACED"
    with pytest.raises(PestppError) as zero:
        piece.multiplier(deck, "INITIAL_ELEVATION", lower=0.5, upper=2.0, the_deck=the_deck)
    assert zero.value.error_code == "PESTPP_PARAMETER_UNPLACED"


@_ENGINE
def test_coupled_real_keywords_template_and_restate_their_own_steering_file(tmp_path):
    deck, *_ = _mesh(tmp_path)
    (deck / "h.cas").write_text(
        "LANGUAGE = 2\nGEOMETRY FILE = 'mesh.slf'\nBOUNDARY CONDITIONS FILE = 'mesh.cli'\n"
        "FRICTION COEFFICIENT = 33.0\nCOUPLING WITH = 'WAQTEL;GAIA'\n"
        "WAQTEL STEERING FILE = 'waqtel.cas'\nGAIA STEERING FILE = 'gaia.cas'\n")
    (deck / "waqtel.cas").write_text("K2 REAERATION COEFFICIENT = 0.9\n")
    (deck / "gaia.cas").write_text("CLASSES SHIELDS PARAMETERS = -9.0;-9.0\n")
    finished = SimpleNamespace(case={"module": "telemac2d", "steering": "h.cas",
                                     "server_facts": {"utm_epsg": 32615}}, record={}, outputs=["r"])
    the_deck = piece.deck(deck, finished)
    assert the_deck.coupled == {"waqtel": "waqtel.cas", "gaia": "gaia.cas"}
    qualified, waqtel = piece.multiplier(deck, "waqtel: K2_REAERATION_COEFFICIENT", lower=0.5, upper=2.0,
                                        the_deck=the_deck)
    bare, gaia = piece.multiplier(deck, "CLASSES_SHIELDS_PARAMETERS", lower=0.5, upper=2.0, the_deck=the_deck)
    assert qualified.body == "waqtel" and waqtel == ("waqtel.cas.tpl", "waqtel.cas")
    assert bare.body == "gaia" and gaia == ("gaia.cas.tpl", "gaia.cas")
    assert "~waqtel_k2_reaeration_coefficient" in (deck / "waqtel.cas.tpl").read_text()
    assert "~gaia_classes_shields_parameters" in (deck / "gaia.cas.tpl").read_text()
    base = tmp_path / "study"
    base.mkdir()
    shutil.copytree(deck, base / "deck")
    where = _copy(base, "ies")
    piece.realize(where / "run", where, the_deck, [qualified, bare], [waqtel, gaia],
                  {qualified.name: 1.5, bare.name: 2.0})
    assert "K2 REAERATION COEFFICIENT = 1.35" in (where / "run" / "waqtel.cas").read_text()
    assert "CLASSES SHIELDS PARAMETERS = -18.0;-18.0" in (where / "run" / "gaia.cas").read_text()


def _uniform_friction(deck: Path) -> piece.Deck:
    (deck / "t.cas").write_text(
        "LANGUAGE = 2\nGEOMETRY FILE = 'mesh.slf'\nBOUNDARY CONDITIONS FILE = 'mesh.cli'\n"
        "LAW OF BOTTOM FRICTION = 3\nFRICTION COEFFICIENT = 33.0\n")
    return _deck(deck)


def test_zoned_friction_by_nodes_and_polygons_lays_the_bed_and_leaves_outside(tmp_path):
    deck, x, y, _cells = _mesh(tmp_path)
    the_deck = _uniform_friction(deck)
    base = tmp_path / "study"
    base.mkdir()
    shutil.copytree(deck, base / "deck")
    where = _copy(base, "ies")
    to_lonlat = Transformer.from_crs(32615, 4326, always_xy=True).transform
    east_ring = [list(to_lonlat(a, b)) for a, b in ((500, -10), (710, -10), (710, 710), (500, 710))]
    top_ring = [(0, 500), (500, 500), (500, 700), (0, 700)]
    zones = [{"nodes": [1, 2], "source": "the first two"},
             {"polygon": east_ring, "source": "the east column in longitude/latitude"},
             {"polygon": top_ring, "crs": "EPSG:32615", "source": "the northwest in the run's frame"}]
    parameters, pairs = piece.zoned(where, "FRICTION_COEFFICIENT", zones, lower=0.5, upper=2.0, the_deck=the_deck)
    assert [p.name for p in parameters] == ["friction_coefficient_z1", "friction_coefficient_z2",
                                            "friction_coefficient_z3"]
    pieces = piece._load_spec(where)["friction"]["pieces"]
    assert pieces[0]["nodes"] == [0, 1]
    assert pieces[1]["nodes"] == [3, 7, 11, 15]
    assert pieces[2]["nodes"] == [12, 13, 14]
    best = {parameters[0].name: 2.0, parameters[1].name: 1.5, parameters[2].name: 0.5}
    piece.realize(where / "run", where, the_deck, parameters, pairs, best)
    laid = read_selafin(where / "run" / "mesh.slf")["data"]["BOTTOM FRICTION"][0]
    assert laid[0] == pytest.approx(66.0) and laid[1] == pytest.approx(66.0)
    assert laid[3] == pytest.approx(49.5) and laid[15] == pytest.approx(49.5)
    assert laid[12] == pytest.approx(16.5) and laid[14] == pytest.approx(16.5)
    assert laid[5] == pytest.approx(33.0) and laid[10] == pytest.approx(33.0)


def test_overlapping_and_empty_zones_refuse(tmp_path):
    deck, *_ = _mesh(tmp_path)
    the_deck = _uniform_friction(deck)
    with pytest.raises(PestppError) as overlap:
        piece.zoned(deck, "FRICTION_COEFFICIENT", [{"nodes": [1, 2]}, {"nodes": [2, 3]}],
                    lower=0.5, upper=2.0, the_deck=the_deck)
    assert overlap.value.error_code == "PESTPP_ZONE_OVERLAP"
    with pytest.raises(PestppError) as empty:
        piece.zoned(deck, "FRICTION_COEFFICIENT",
                    [{"polygon": [[900, 900], [1000, 900], [1000, 1000], [900, 1000]], "crs": 32615}],
                    lower=0.5, upper=2.0, the_deck=the_deck)
    assert empty.value.error_code == "PESTPP_ZONE_EMPTY"


def test_a_node_zone_overlapping_a_friction_row_zone_refuses(tmp_path):
    deck, the_deck = _friction_data_deck(tmp_path)
    with pytest.raises(PestppError) as overlap:
        piece.zoned(deck, "FRICTION_COEFFICIENT",
                    [{"zones": [1], "source": "west rows"}, {"nodes": [1], "source": "node one"}],
                    lower=0.5, upper=2.0, the_deck=the_deck)
    assert overlap.value.error_code == "PESTPP_ZONE_OVERLAP"


def _friction_data_deck(tmp_path: Path) -> tuple[Path, piece.Deck]:
    deck, *_ = _mesh(tmp_path)
    (deck / "rog_friction.tbl").write_text("* zone law coef\n1 MANNING 0.030 NULL\n2 MANNING 0.050 NULL\nEND\n")
    (deck / "rog_zones.dat").write_text("".join(f"{node} {1 if node <= 8 else 2}\n" for node in range(1, 17)))
    (deck / "d.cas").write_text(
        "LANGUAGE = 2\nGEOMETRY FILE = 'mesh.slf'\nBOUNDARY CONDITIONS FILE = 'mesh.cli'\n"
        "LAW OF BOTTOM FRICTION = 4\nFRICTION DATA = YES\n"
        "FRICTION DATA FILE = 'rog_friction.tbl'\nZONES FILE = 'rog_zones.dat'\n")
    return deck, _deck(deck, "d.cas")


def test_friction_data_scales_the_whole_table_or_one_zone(tmp_path):
    deck, the_deck = _friction_data_deck(tmp_path)
    base = tmp_path / "study"
    base.mkdir()
    shutil.copytree(deck, base / "deck")
    whole = _copy(base, "ies")
    parameter, pair = piece.multiplier(whole, "FRICTION_COEFFICIENT", lower=0.5, upper=2.0, the_deck=the_deck)
    piece.realize(whole / "run", whole, the_deck, [parameter], [pair], {parameter.name: 2.0})
    rows = [line.split() for line in (whole / "run" / "rog_friction.tbl").read_text().splitlines()
            if line.strip() and not line.startswith(("*", "#")) and line.strip() != "END"]
    assert [float(row[2]) for row in rows] == [pytest.approx(0.060), pytest.approx(0.100)]

    zoned = _copy(base, "ies2")
    parameters, pairs = piece.zoned(zoned, "FRICTION_COEFFICIENT",
                                    [{"zones": [1], "source": "west"}, {"zones": [2], "source": "east"}],
                                    lower=0.5, upper=2.0, the_deck=the_deck)
    piece.realize(zoned / "run", zoned, the_deck, parameters, pairs,
                  {parameters[0].name: 2.0, parameters[1].name: 0.5})
    rows = [line.split() for line in (zoned / "run" / "rog_friction.tbl").read_text().splitlines()
            if line.strip() and not line.startswith(("*", "#")) and line.strip() != "END"]
    assert [float(row[2]) for row in rows] == [pytest.approx(0.060), pytest.approx(0.025)]


def test_unstated_pilot_points_are_ten_of_the_mesh_s_median_edges(tmp_path):
    deck, *_ = _mesh(tmp_path)
    the_deck = _uniform_friction(deck)
    field = piece.fields({"FRICTION_COEFFICIENT": {}}, the_deck)["FRICTION_COEFFICIENT"]
    assert the_deck.cell_size == pytest.approx(200.0)
    assert field["spacing"] == pytest.approx(2000.0) and field["a"] == pytest.approx(2000.0)
    assert field["variogram"] == "exponential" and field["contribution"] == 1.0


def test_pilot_points_on_the_mesh_lay_the_friction_and_the_scatter_map(tmp_path):
    deck, *_ = _mesh(tmp_path)
    _uniform_friction(deck)
    (deck / "t.cas").write_text(
        "LANGUAGE = 2\nGEOMETRY FILE = 'mesh.slf'\nBOUNDARY CONDITIONS FILE = 'mesh.cli'\n"
        "LAW OF BOTTOM FRICTION = 3\nFRICTION COEFFICIENT = 33.0\n"
        "FORMATTED DATA FILE 2 = 'cn2.dat'\n")
    (deck / "cn2.dat").write_text("#X Y CN2\n" + "".join(
        f"{x:.3f} {y:.3f} {55.0 + index * 0.1:.3f}\n" for index, (x, y) in enumerate(zip(
            [i * 200.0 for j in range(4) for i in range(4)], [j * 200.0 for j in range(4) for i in range(4)]))))
    the_deck = _deck(deck)
    base = tmp_path / "study"
    base.mkdir()
    shutil.copytree(deck, base / "deck")
    where = _copy(base, "ies")
    field = piece.fields({"FRICTION_COEFFICIENT": {"spacing": 400.0, "source": "a reader's choice"}}, the_deck)
    parameters, pairs = piece.pilot_points(where, "FRICTION_COEFFICIENT", field["FRICTION_COEFFICIENT"],
                                           lower=0.5, upper=2.0, the_deck=the_deck)
    assert len(parameters) == 4
    assert {p.group for p in parameters} == {"friction_coefficient"}
    assert {p.correlation for p in parameters} == {400.0}
    assert all(p.site is not None for p in parameters)
    grouped = prior(parameters)
    assert grouped["friction_coefficient"]["correlated"]["variogram"] == "exponential"
    best = {p.name: 2.0 for p in parameters}
    piece.realize(where / "run", where, the_deck, parameters, pairs, best)
    laid = read_selafin(where / "run" / "mesh.slf")["data"]["BOTTOM FRICTION"][0]
    assert laid.min() > 33.0 and laid.max() <= 66.0 + 1e-9

    scatter = _copy(base, "ies2")
    field = piece.fields({"CN2": {"spacing": 400.0}}, the_deck)["CN2"]
    points, scatter_pairs = piece.pilot_points(scatter, "CN2", field, lower=0.5, upper=2.0, the_deck=the_deck)
    piece.realize(scatter / "run", scatter, the_deck, points, scatter_pairs,
                  {name: 2.0 for name in (p.name for p in points)})
    values = [float(line.split()[2]) for line in (scatter / "run" / "cn2.dat").read_text().splitlines()
              if line.strip() and not line.startswith("#")]
    assert min(values) > 55.0 and max(values) <= 113.0 + 1e-6


def test_a_nonspatial_scalar_keyword_refuses_pilot_points(tmp_path):
    deck, *_ = _mesh(tmp_path)
    the_deck = _uniform_friction(deck)
    with pytest.raises(PestppError) as refused:
        piece.fields({"VELOCITY_DIFFUSIVITY": {}}, the_deck)
    assert refused.value.error_code == "PESTPP_PARAMETER_UNPLACED"
    assert "VELOCITY_DIFFUSIVITY" in str(refused.value)


def test_the_trial_lays_the_same_zone_field_as_realize(tmp_path):
    deck, *_ = _mesh(tmp_path)
    the_deck = _uniform_friction(deck)
    base = tmp_path / "study"
    base.mkdir()
    shutil.copytree(deck, base / "deck")
    where = _copy(base, "ies")
    parameters, pairs = piece.zoned(where, "FRICTION_COEFFICIENT",
                                    [{"nodes": [1, 2], "source": "a"}, {"nodes": [3, 4], "source": "b"}],
                                    lower=0.5, upper=2.0, the_deck=the_deck)
    best = {parameters[0].name: 1.5, parameters[1].name: 0.5}
    piece.realize(where / "run", where, the_deck, parameters, pairs, best)
    realized = read_selafin(where / "run" / "mesh.slf")["data"]["BOTTOM FRICTION"][0]
    trial_dir = tmp_path / "trial"
    shutil.copytree(where, trial_dir)
    for name, value in best.items():
        (trial_dir / f"{name}.dat").write_text(f"{value}\n")
    module = _trial_module()
    module.apply_spec(str(trial_dir))
    from_trial = read_selafin(trial_dir / "mesh.slf")["data"]["BOTTOM FRICTION"][0]
    assert np.allclose(from_trial, realized)


def _trial_module():
    spec = importlib.util.spec_from_file_location("telemac_trial_coverage", _TRIAL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@_ENGINE
@pytest.mark.asyncio
async def test_default_scalar_friction_authors_byte_identical_files(tmp_path, monkeypatch):
    from trid3nt.workflows.pestpp import staging
    from trid3nt.workflows.pestpp.study import Study
    from trid3nt.tools.mesh.shared.nodes import read_accepted_mesh_nodes

    steering = "telemac2d_telemac_dye_release.cas"
    work = tmp_path / "work"
    (work / "deck").mkdir(parents=True)
    for name in (steering, "domain.cli"):
        shutil.copy(_MISSOURI / name, work / "deck" / name)
    xy, _cells, bed, _ll = read_accepted_mesh_nodes(str(_MISSOURI / "mesh.2dm"), utm_epsg=32615)
    monkeypatch.setattr(piece, "_nodes", lambda directory, the_deck: (xy, bed))
    the_deck = piece.Deck(steering=steering, stated=read_steering(work / "deck" / steering), module="telemac2d",
                          result="r2d_domain.slf", utm_epsg=32615)
    observed = Observed("level_1", 292.0, "levels", 0.1, "cited")
    study = Study(workflow=SimpleNamespace(name="telemac-calibration"), run={}, work=work,
                  finished=SimpleNamespace(engine="telemac", run_id="finished", case={}, record={}, outputs=[]),
                  deck=the_deck, targets=[(observed, 1)], observed=[observed],
                  chosen=[("FRICTION_COEFFICIENT", 0.5, 1.5, False)], sources={"friction_coefficient": "the run"},
                  fields={}, zones={}, options={}, method="ies", frame="", tune=None, balanced=False,
                  offsets=[], stated_method=False, rows={}, plan=("calibrate",), explicit_plan=False)

    async def stage(source, **named):
        return {"run_id": "fit", "case": {"cores": 1}}

    monkeypatch.setattr(staging, "stage_study", stage)
    await study.author("ies", study.chosen, {})
    for name in ("pestpp.pst", f"{steering}.tpl", "study_points.txt.ins", "outflow_levels.json"):
        assert (work / "ies" / name).read_bytes() == (_BASELINE / name).read_bytes(), name
