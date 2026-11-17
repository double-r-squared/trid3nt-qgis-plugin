"""A TELEMAC study's trial holds the outflow level its own friction derives, never the finished run's.

Offline, on the Missouri reach's recorded steering file, boundary file and mesh (tests/fixtures/telemac_missouri):
the run derived 291.385 m at Strickler 33 for 889.149 m3/s over its outflow section."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from trid3nt_server.workflows.pestpp.authoring import telemac as piece
from trid3nt_server.workflows.pestpp.authoring.control import Parameter
from trid3nt_server.workflows.pestpp.errors import PestppError
from trid3nt_server.workflows.telemac.authoring.staged_check import read_steering

_MISSOURI = Path(__file__).resolve().parents[1] / "fixtures" / "telemac_missouri"
_STEERING = "telemac2d_telemac_dye_release.cas"
_TRIAL = Path(__file__).resolve().parents[2] / "workers" / "pestpp" / "telemac_trial.py"


@pytest.fixture
def staged(tmp_path, monkeypatch):
    from trid3nt_server.tools.mesh.shared.nodes import read_accepted_mesh_nodes

    shutil.copy(_MISSOURI / _STEERING, tmp_path / _STEERING)
    shutil.copy(_MISSOURI / "domain.cli", tmp_path / "domain.cli")
    xy, _cells, bed, _ll = read_accepted_mesh_nodes(str(_MISSOURI / "mesh.2dm"), utm_epsg=32615)
    # The geometry file's numbering is the display face's; the .2dm is its readable record.
    monkeypatch.setattr(piece, "_nodes", lambda directory, the_deck: (xy, bed))
    the_deck = piece.Deck(steering=_STEERING, stated=read_steering(tmp_path / _STEERING), module="telemac2d",
                          result="r2d_domain.slf", utm_epsg=32615)
    return tmp_path, the_deck


def _friction(lower=0.5, upper=1.5):
    return Parameter(name="friction_coefficient", value=1.0, lower=lower, upper=upper, group="friction", scale=33.0)


def test_each_trial_reads_its_outflow_level_off_its_own_friction(staged):
    directory, the_deck = staged
    table = piece.outflow_levels(directory, the_deck=the_deck, parameters=[_friction()])
    assert json.loads((directory / piece.LEVELS).read_text()) == table
    assert table["boundary"] == 0 and table["elevations"] == [291.385, 0.0]
    assert table["coefficients"][0] == pytest.approx(16.5) and table["coefficients"][-1] == pytest.approx(49.5)
    at_33 = table["coefficients"].index(pytest.approx(33.0))
    assert table["stages"][at_33] == pytest.approx(291.385, abs=1e-3)
    # A rougher channel (smaller Strickler) conveys the same flow deeper.
    assert all(a > b for a, b in zip(table["stages"], table["stages"][1:]))


def test_the_calibrated_run_restates_the_level_at_its_best_friction(staged):
    directory, the_deck = staged
    values = piece._level_at(directory, the_deck, 25.0)
    table = piece.outflow_levels(directory, the_deck=the_deck, parameters=[_friction()])
    trial = _trial_module()
    assert values["INITIAL ELEVATION"] == values["PRESCRIBED ELEVATIONS"][0]
    assert values["PRESCRIBED ELEVATIONS"][1] == 0.0
    assert values["INITIAL ELEVATION"] > 291.385
    assert trial.level_at(table, 25.0) == pytest.approx(values["INITIAL ELEVATION"], abs=5e-3)


def test_a_level_a_gauge_held_is_not_rederived(staged):
    directory, the_deck = staged
    stated = dict(the_deck.stated, **{"PRESCRIBED ELEVATIONS": ["292.65", "0.0"]})
    held = piece.Deck(**{**the_deck.__dict__, "stated": stated})
    assert piece.outflow_levels(directory, the_deck=held, parameters=[_friction()]) is None
    assert not (directory / piece.LEVELS).exists()
    assert piece._level_at(directory, held, 25.0) == {}


def test_a_measured_outflow_under_a_sloped_opening_is_never_rederived(staged):
    """A river run held at a downstream gauge's measured level opens sloped (INITIAL CONDITIONS 'SPECIAL', its own
    routine): no trial and no calibrated rerun restates the level or the opening."""
    directory, the_deck = staged
    stated = dict(the_deck.stated, **{"PRESCRIBED ELEVATIONS": ["290.474", "0.0"],
                                      "INITIAL CONDITIONS": ["SPECIAL"], "FORTRAN FILE": ["user_fortran"]})
    held = piece.Deck(**{**the_deck.__dict__, "stated": stated})
    assert piece.outflow_levels(directory, the_deck=held, parameters=[_friction()]) is None
    assert piece._level_at(directory, held, 25.0) == {}
    assert piece._level_at(directory, held, 40.0) == {}


def test_a_coefficient_outside_the_derived_range_is_no_trial():
    trial = _trial_module()
    with pytest.raises(SystemExit):
        trial.level_at({"coefficients": [20.0, 40.0], "stages": [292.0, 291.0]}, 41.0)


def test_a_run_that_ended_unsteady_is_not_calibrated(tmp_path):
    finished = SimpleNamespace(case={"module": "telemac2d", "steering": _STEERING}, record={"notes": [
        "the run ended UNSTEADY: over its last 3 balance printouts the water entering and leaving differ by at most "
        "50.3% (tolerance 2%); at t = 1800 s 886.306 m3/s entered and 1664.68 m3/s left."]}, outputs=[])
    with pytest.raises(PestppError) as refused:
        piece.deck(tmp_path, finished)
    assert refused.value.error_code == "PESTPP_RUN_UNSTEADY"
    assert "1664.68" in str(refused.value)


def test_a_trial_runs_the_level_script_then_the_launcher():
    the_deck = piece.Deck(steering=_STEERING, stated={}, module="telemac2d", result="r", utm_epsg=32615)
    assert piece.command(the_deck) == f"python {piece.TRIAL} telemac2d {_STEERING}"


def _trial_module():
    spec = importlib.util.spec_from_file_location("telemac_trial", _TRIAL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
