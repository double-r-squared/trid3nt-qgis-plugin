"""Pilot points over a real MODFLOW deck: placed, kriged and correlated as pyEMU 1.7.0 does, laid by the trial.

The deck under tests/fixtures/pestpp/des_moines_deck is the Des Moines canary's finished one-layer water table (12,880
DISV cells); pyemu_pilot_points.json is what pyEMU 1.7.0 itself computed over that grid in the pestpp image
(tests/fixtures/pestpp/README.md).
"""

from __future__ import annotations

import csv
import dataclasses
import gzip
import importlib.util
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from trid3nt_server.workflows.pestpp.authoring import modflow as piece
from trid3nt_server.workflows.pestpp.authoring import telemac
from trid3nt_server.workflows.pestpp.authoring.control import Observed, Parameter, prior, write_control
from trid3nt_server.workflows.pestpp.errors import PestppError
from trid3nt_server.workflows.pestpp.helpers.geostat import covariance, kriging_factors

_DECK = Path(__file__).resolve().parents[1] / "fixtures" / "pestpp" / "des_moines_deck"
_TRIAL = Path(__file__).resolve().parents[2] / "workers" / "pestpp" / "modflow_trial.py"
_PYEMU = json.loads((_DECK / "pyemu_pilot_points.json").read_text())


@pytest.fixture(scope="module")
def deck_dir(tmp_path_factory):
    where = tmp_path_factory.mktemp("deck")
    for path in _DECK.iterdir():
        if path.suffix == ".gz":
            (where / path.stem).write_bytes(gzip.decompress(path.read_bytes()))
        elif path.suffix != ".json":
            shutil.copy2(path, where / path.name)
    return where


@pytest.fixture(scope="module")
def the_deck(deck_dir):
    return dataclasses.replace(piece.deck(deck_dir), cell_size=100.0)


def _field(the_deck, **stated):
    return piece.fields({"NPF_K": stated}, the_deck)["NPF_K"]


def test_the_points_are_placed_as_pyemu_places_them_over_the_real_grid(deck_dir, the_deck):
    centres, cells = piece._grid(deck_dir, the_deck)
    sites = piece._placed(centres, cells, 1000.0)
    assert sites.shape == (88, 2)
    assert np.abs(sites - np.array(_PYEMU["points"])).max() == 0.0


def test_the_kriging_factors_and_the_prior_covariance_are_pyemus():
    points = np.array(_PYEMU["points"])
    cells = np.array([_PYEMU["cells"][k] for k in _PYEMU["factors"]])
    index, factor = kriging_factors(cells, points, 1000.0)
    for row, k in enumerate(_PYEMU["factors"]):
        theirs = dict(zip(_PYEMU["factors"][k]["names"], _PYEMU["factors"][k]["factors"]))
        mine = {f"pp_{j:04d}": f for j, f in zip(index[row], factor[row]) if f > 0.0}
        assert set(mine) == set(theirs)
        assert max(abs(mine[n] - theirs[n]) for n in mine) < 1e-12
    assert factor.sum(axis=1) == pytest.approx(1.0)
    held = covariance(points, 1000.0, np.full(len(points), _PYEMU["covariance_variance"]))
    assert np.abs(held - np.array(_PYEMU["covariance"])).max() < 1e-15


def test_unstated_pilot_points_are_pyemus_ten_cells_and_a_range_of_that_spacing(the_deck):
    field = _field(the_deck)
    assert (field["spacing"], field["a"], field["variogram"], field["stated"]) == (1000.0, 1000.0, "exponential", False)
    assert "pyEMU 1.7.0 PstFrom default" in field["spacing_source"] and field["a_source"] == field["spacing_source"]
    stated = _field(the_deck, spacing=500.0, source="a reader's choice")
    assert (stated["spacing"], stated["a"], stated["stated"]) == (500.0, 500.0, True)
    assert stated["a_source"] == "pyEMU 1.7.0 PstFrom default: the range is the spacing"
    ranged = _field(the_deck, variogram={"a": 2500.0, "source": "a variogram fitted elsewhere"})
    assert (ranged["spacing"], ranged["a"], ranged["a_source"]) == (1000.0, 2500.0, "a variogram fitted elsewhere")


def test_unstated_pilot_points_count_ten_of_the_grid_s_median_edges(deck_dir):
    """The deck's own grid: 12,880 triangles of the 100 m mesh, edges 64.0 to 215.4 m, median 122.94 m; pyEMU's ten
    cells are ten of those edges, 1229.4 m, which lays 63 points (the minimum edge's 640 m laid 221)."""
    own = piece.deck(deck_dir)
    assert own.cell_size == pytest.approx(122.944, abs=1e-3)
    field = _field(own)
    assert field["spacing"] == pytest.approx(1229.44, abs=1e-2) and field["a"] == field["spacing"]
    assert "median edge" in field["spacing_source"]
    centres, cells = piece._grid(deck_dir, own)
    assert len(piece._placed(centres, cells, field["spacing"])) == 63
    assert _field(own, spacing=1000.0, source="a reader's choice")["spacing"] == 1000.0


def test_an_ensemble_study_over_pilot_points_localizes_unless_the_run_states_otherwise():
    from trid3nt_server.workflows.pestpp.workflow import LOCALIZED, localized

    field = {"NPF_K": {"spacing": 1000.0}}
    assert localized({"NOPTMAX": 3}, "ies", field) == ({"NOPTMAX": 3, "IES_AUTOADALOC": True}, True)
    assert "9.1.11 Localization" in LOCALIZED[2]
    assert localized({"IES_AUTOADALOC": False}, "ies", field) == ({"IES_AUTOADALOC": False}, False)
    assert localized({}, "glm", field) == ({}, False)
    assert localized({}, "ies", {}) == ({}, False)


def test_the_calibrated_points_at_their_bounds_are_counted_per_group_as_pest_holds_them():
    """The recorded pilot-point study (221 points at the minimum edge's spacing, 19 realizations): its prior ensemble
    holds 208 of 4218 values at a bound (PEST++ truncates its draws there), its last 1559, and the base realization
    pins 37 points low, 51 high and SFR_RHK high."""
    from trid3nt_server.errors import PARAMETERS_AT_BOUNDS, parameters_at_bounds_warning
    from trid3nt_server.workflows.pestpp import outputs
    from trid3nt_server.workflows.pestpp.authoring.control import at_bounds

    recorded = _DECK.parent / "pilot_points"
    counts = {}
    for label, iteration in (("prior", 0), ("last", None)):
        ensemble = outputs.read_parameter_ensemble(recorded, iteration)
        parameters = [Parameter(n, 1.0, 0.1, 10.0, "npf_k") if n.startswith("npf_k_pp")
                      else Parameter(n, 1.0, 0.0006096, 60.96, "sfr_rhk", scale=0.5) for n in ensemble["base"]]
        counts[label] = sum(c["lower"] + c["upper"] for real in ensemble.values()
                            for c in at_bounds(parameters, real).values())
        base = at_bounds(parameters, ensemble["base"])
    assert counts == {"prior": 208, "last": 1559}
    assert base == {"npf_k": {"lower": 37, "upper": 51, "of": 221}, "sfr_rhk": {"lower": 0, "upper": 1, "of": 1}}
    note = parameters_at_bounds_warning(groups=base)
    assert note.startswith(PARAMETERS_AT_BOUNDS) and "npf_k 37 at the lower and 51 at the upper of 221" in note
    # 5 is no float's log10 round trip: a value pestpp-ies clipped at log10(5) reads back an ulp off, and is at it
    import math
    clipped = 10.0 ** math.log10(5.0)
    assert clipped != 5.0
    assert at_bounds([Parameter("p", 1.0, 0.2, 5.0, "g")], {"p": clipped}) == {"g": {"lower": 0, "upper": 1, "of": 1}}
    assert at_bounds([Parameter("p", 1.0, 0.2, 5.0, "g")], {"p": 4.99}) == {}


def test_pilot_points_refuse_what_they_cannot_lay(the_deck):
    with pytest.raises(PestppError) as typed:
        _field(the_deck, variogram={"type": "gaussian"})
    assert typed.value.error_code == "PESTPP_VARIOGRAM_UNKNOWN"
    with pytest.raises(PestppError) as unread:
        _field(the_deck, nugget=0.1)
    assert unread.value.error_code == "PESTPP_PILOT_POINTS_UNREAD"
    with pytest.raises(PestppError) as friction:
        telemac.fields({"FRICTION_COEFFICIENT": {}}, None)
    assert friction.value.error_code == "PESTPP_PARAMETER_UNPLACED"


def test_the_array_is_laid_by_its_pilot_points_and_the_trial_lays_it_the_same(deck_dir, the_deck, tmp_path):
    study = tmp_path / "study"
    shutil.copytree(deck_dir, study)
    parameters, pair = piece.pilot_points(study, "NPF_K", _field(the_deck), lower=0.1, upper=10.0, the_deck=the_deck)
    assert len(parameters) == 88 and pair == ("npf_k.pp.dat.tpl", "npf_k.pp.dat")
    assert {p.group for p in parameters} == {"npf_k"} and {p.correlation for p in parameters} == {1000.0}
    assert parameters[0].site == tuple(_PYEMU["points"][0])
    npf = (study / "gwf.npf").read_text()
    assert "OPEN/CLOSE  gwf.npf.k.dat  FACTOR  1.0" in npf and "INTERNAL" not in npf.split("  k\n")[1].split("END")[0]
    base = np.array([float(v) for v in (study / "gwf.npf.k.dat").read_text().split()])
    assert len(base) == 12880 and list(base[:5]) == [2.4537166, 2.45371649, 2.4537166, 2.45371655, 1.00264633]
    rng = np.random.default_rng(7)
    values = {p.name: float(10.0 ** rng.uniform(-1.0, 1.0)) for p in parameters}
    (study / "npf_k.pp.dat").write_text("".join(f"{name} {value!r}\n" for name, value in values.items()))
    trial = tmp_path / "trial"
    shutil.copytree(study, trial)
    own = piece.lay(study)
    assert own == [piece.PILOT_POINTS, "npf_k.pp.dat", "npf_k.pp.npz"]
    laid = np.array([float(v) for v in (study / "gwf.npf.k.dat").read_text().split()])
    spec = importlib.util.spec_from_file_location("modflow_trial", _TRIAL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.lay(str(trial)) == ["gwf.npf.k.dat"]
    assert (trial / "gwf.npf.k.dat").read_text() == (study / "gwf.npf.k.dat").read_text()
    held = np.load(study / "npf_k.pp.npz")
    logs = np.log10([values[str(n)] for n in held["names"]])
    for cell in (0, 4321, 12879):
        expected = base[cell] * 10.0 ** sum(w * logs[j] for j, w in zip(held["index"][cell], held["weight"][cell]))
        assert laid[cell] == pytest.approx(expected, rel=1e-12)
    (study / "npf_k.pp.dat").write_text("".join(f"{p.name} 2.0\n" for p in parameters))
    piece.lay(study)
    doubled = np.array([float(v) for v in (study / "gwf.npf.k.dat").read_text().split()])
    assert doubled == pytest.approx(2.0 * base, rel=1e-12)


def test_the_study_interface_lays_pilot_points_first_and_refuses_value_bounds_on_them(deck_dir, the_deck, tmp_path):
    with pytest.raises(PestppError) as absolute:
        piece.interface(deck_dir, tmp_path / "a", [("NPF_K", 0.5, 50.0, True)], the_deck=the_deck, targets=[],
                        spatial={"NPF_K": _field(the_deck)})
    assert absolute.value.error_code == "PESTPP_BOUNDS_UNSTATED"
    parameters, templates, _ins = piece.interface(
        deck_dir, tmp_path / "b", [("SFR_RHK", 0.1, 10.0, False), ("NPF_K", 0.1, 10.0, False)], the_deck=the_deck,
        targets=[(Observed("head_1", 270.0, "heads", 1.0, "cited"), 0)], spatial={"NPF_K": _field(the_deck)})
    assert [p.name for p in parameters][:1] == ["npf_k_pp0000"] and parameters[-1].name == "sfr_rhk"
    assert templates[0] == ("npf_k.pp.dat.tpl", "npf_k.pp.dat")
    assert piece.command(the_deck) == f"python {piece.TRIAL}"


def test_the_prior_pest_draws_from_correlates_the_points_and_leaves_the_rest_alone(tmp_path):
    points = [tuple(p) for p in _PYEMU["points"][:3]]
    parameters = [Parameter(f"npf_k_pp{k:04d}", 1.0, 0.1, 10.0, "npf_k", site=site, correlation=1000.0)
                  for k, site in enumerate(points)] + [Parameter("sfr_rhk", 1.0, 0.01, 100.0, "sfr_rhk")]
    write_control(tmp_path, name="pestpp.pst", parameters=parameters,
                  observations=[Observed("head_1", 270.0, "heads", 1.0, "cited")],
                  templates=[("m.tpl", "m.in")], instructions=[("m.ins", "m.out")], command="mf6")
    control = (tmp_path / "pestpp.pst").read_text()
    assert "parcov study_prior.unc" in control
    unc = (tmp_path / "study_prior.unc").read_text().splitlines()
    assert unc[:3] == ["START STANDARD_DEVIATION", "sfr_rhk 1.0", "END STANDARD_DEVIATION"]
    assert unc[3:] == ["START COVARIANCE_MATRIX", "file study_prior.cov", "END COVARIANCE_MATRIX"]
    cov = (tmp_path / "study_prior.cov").read_text().splitlines()
    assert cov[0] == "3 3 1" and cov[4] == "* row and column names" and cov[5:] == [p.name for p in parameters[:3]]
    matrix = np.array([[float(v) for v in line.split()] for line in cov[1:4]])
    assert matrix == pytest.approx(np.array(_PYEMU["covariance"])[:3, :3], abs=1e-15)
    held = prior(parameters)
    assert held["npf_k"]["sd"] == pytest.approx(0.5) and held["npf_k"]["parameters"] == 3
    assert held["npf_k"]["correlated"] == {"variogram": "exponential", "a": 1000.0, "contribution": 1.0, "nugget": 0.0}
    assert "correlated" not in held["sfr_rhk"] and held["sfr_rhk"]["sd"] == pytest.approx(1.0)
    assert prior(parameters, {"PAR_SIGMA_RANGE": 6.0})["npf_k"]["sd"] == pytest.approx(2.0 / 6.0)
    alone = tmp_path / "alone"
    alone.mkdir()
    write_control(alone, name="pestpp.pst", parameters=parameters[3:],
                  observations=[Observed("head_1", 270.0, "heads", 1.0, "cited")],
                  templates=[("m.tpl", "m.in")], instructions=[("m.ins", "m.out")], command="mf6")
    assert "parcov" not in (alone / "pestpp.pst").read_text() and not (alone / "study_prior.unc").exists()
    with (tmp_path / "study_parameters.csv").open() as fh:
        assert [row["parnme"] for row in csv.DictReader(fh)] == [p.name for p in parameters]


def test_the_record_states_the_prior_and_the_ensembles_pest_drew_from_it():
    from trid3nt_server.workflows.pestpp.workflow import _priors

    recorded = _DECK.parent / "pilot_points"
    with (recorded / "pestpp.0.par.csv").open() as fh:
        names = [n for n in next(csv.reader(fh)) if n != "real_name"]
    points = [n for n in names if n.startswith("npf_k_pp")]
    assert len(points) == 221
    parameters = [Parameter(n, 1.0, 0.1, 10.0, "npf_k", site=(0.0, float(k)), correlation=1000.0)
                  for k, n in enumerate(points)] + [Parameter("sfr_rhk", 1.0, 0.0006096, 60.96, "sfr_rhk", scale=0.5)]
    field = {"spacing": 1000.0, "a": 1000.0, "variogram": "exponential", "stated": False}
    held = _priors(parameters, {}, {"NPF_K": field}, recorded, "ies")
    k = held["npf_k"]
    assert (k["mean"], k["sd"], k["sd_of"], k["parameters"]) == (1.0, pytest.approx(0.5), "log10", 221)
    assert k["pilot_points"]["count"] == 221 and k["correlated"]["a"] == 1000.0
    # PEST++ drew the prior about the mean at about the stated sd (truncated at the bounds), and the fit narrowed it
    assert abs(k["prior_ensemble"]["mean"]) < 0.05 and 0.4 < k["prior_ensemble"]["sd"] < 0.5
    assert k["posterior_ensemble"]["sd"] < k["prior_ensemble"]["sd"] / 3
    assert held["sfr_rhk"]["sd"] == pytest.approx(1.25) and "pilot_points" not in held["sfr_rhk"]
