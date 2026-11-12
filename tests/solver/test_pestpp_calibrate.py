"""The MODFLOW calibration study: its interface over a finished deck, its targets, and what it publishes.

The deck under tests/fixtures/pestpp/calibrate/deck is the one a finished zero-fetch water-table run
staged; study/ and calibrated_study_heads.csv were recorded from the canary's pestpp-ies study over it
and the calibrated run it published, held to one real USGS well (tests/fixtures/pestpp/README.md).
"""

from __future__ import annotations

import csv
import dataclasses
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from trid3nt_server.inputs.observation import Observation
from trid3nt_server.workflows.pestpp import outputs
from trid3nt_server.workflows.pestpp.authoring import modflow as study_deck
from trid3nt_server.workflows.pestpp.charts import parameter_chart, phi_chart, residual_chart
from trid3nt_server.workflows.pestpp.errors import PestppError
from trid3nt_server.workflows.pestpp.workflow import _bounds, ranked

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pestpp"
_CALIBRATE = _FIXTURES / "calibrate"


@pytest.fixture
def deck_dir(tmp_path):
    shutil.copytree(_CALIBRATE / "deck", tmp_path / "deck")
    return tmp_path / "deck"


def _rows(payload):
    return payload["spec"]["data"]["values"] if "spec" in payload else payload["vega_lite_spec"]["data"]["values"]


def test_the_residual_is_simulated_minus_observed_on_a_real_well():
    residuals = outputs.read_residuals(_CALIBRATE / "study")
    (row,) = _rows(residual_chart(residuals, caption="simulated minus observed head"))
    (read,) = residuals
    assert row["simulated_minus_observed"] == pytest.approx(read["modelled"] - read["measured"])
    assert row["simulated_minus_observed"] == pytest.approx(-read["residual"])
    assert row["simulated_minus_observed"] < -7.0
    with (_CALIBRATE / "calibrated_study_heads.csv").open() as fh:
        calibrated = float(next(csv.DictReader(fh))["HEAD_1"])
    assert calibrated - read["measured"] == pytest.approx(row["simulated_minus_observed"], abs=1e-9)


def test_phi_falls_over_the_recorded_study_and_the_charts_read_it():
    phi = outputs.read_phi(_CALIBRATE / "study")
    assert phi[-1]["mean"] < phi[0]["mean"]
    assert "falls from 118.3 at iteration 0 to 64.04 at iteration 3" in phi_chart(phi, caption="phi")["caption"]
    ensemble = outputs.read_parameter_ensemble(_CALIBRATE / "study")
    chart = parameter_chart(ensemble, [SimpleNamespace(name="npf_k", lower=0.1, upper=10.0)], caption="multipliers")
    (row,) = _rows(chart)
    assert row["base"] == pytest.approx(ensemble["base"]["npf_k"]) and row["lower_bound"] == 0.1


def test_the_deck_reads_its_one_flow_model(deck_dir):
    the_deck = study_deck.deck(deck_dir)
    assert (the_deck.model, the_deck.name_file, the_deck.nlay, the_deck.steps) == ("gwf", "gwf.nam", 1, 1)
    assert the_deck.packages["NPF"] == "gwf.npf" and the_deck.length_units == "meters"
    assert len(the_deck.top) == the_deck.ncpl


def test_a_multiplier_is_written_into_the_array_s_own_factor(deck_dir, tmp_path):
    the_deck = study_deck.deck(deck_dir)
    parameters, templates, _ins = study_deck.interface(
        deck_dir, tmp_path / "study", [("NPF_K", 0.1, 10.0, False)], the_deck=the_deck,
        targets=[(study_deck.Observed("head_1", 1.0, 1.0, "heads"), 0)])
    assert templates == [("gwf.npf.tpl", "gwf.npf")]
    assert (parameters[0].name, parameters[0].value, parameters[0].scale) == ("npf_k", 1.0, 1.0)
    template = (tmp_path / "study" / "gwf.npf.tpl").read_text().splitlines()
    original = (deck_dir / "gwf.npf").read_text().splitlines()
    assert template[0] == "ptf ~" and len(template) == len(original) + 1
    changed = [(a, b) for a, b in zip(original, template[1:]) if a != b]
    assert changed == [("    INTERNAL  FACTOR  1.0", "    INTERNAL  FACTOR  ~npf_k                 ~")]


def test_two_arrays_of_one_file_share_one_template_and_a_constant_is_its_own_scale(deck_dir, tmp_path):
    the_deck = study_deck.deck(deck_dir)
    parameters, templates, _ins = study_deck.interface(
        deck_dir, tmp_path / "study", [("NPF_K", 0.1, 10.0, False), ("NPF_ICELLTYPE", 0.5, 2.0, False)], the_deck=the_deck,
        targets=[(study_deck.Observed("head_1", 1.0, 1.0, "heads"), 0)])
    assert templates == [("gwf.npf.tpl", "gwf.npf")]
    text = (tmp_path / "study" / "gwf.npf.tpl").read_text()
    assert "~npf_k" in text and "CONSTANT  ~npf_icelltype" in text
    assert [p.scale for p in parameters] == [1.0, 1.0]


def test_an_array_no_package_of_the_run_states_refuses(deck_dir):
    with pytest.raises(PestppError, match="names no package") as refused:
        study_deck.multiplier(deck_dir, "STO_SS", lower=0.1, upper=10.0, the_deck=study_deck.deck(deck_dir))
    assert refused.value.error_code == "PESTPP_PARAMETER_UNPLACED"


def test_the_best_values_are_written_through_the_scale_and_read_back_as_the_deck_s_sheet(deck_dir, tmp_path):
    the_deck = study_deck.deck(deck_dir)
    before = study_deck.deck_sheet(deck_dir, the_deck).filled["NPF_K"].value
    parameters, templates, _ins = study_deck.interface(
        deck_dir, tmp_path / "study", [("NPF_K", 0.1, 10.0, False)], the_deck=the_deck,
        targets=[(study_deck.Observed("head_1", 1.0, 1.0, "heads"), 0)])
    study_deck.substitute(tmp_path / "study", templates, parameters, {"npf_k": 0.5})
    sheet = study_deck.deck_sheet(tmp_path / "study", the_deck)
    assert sheet.filled["NPF_K"].value[0][0] == pytest.approx(before[0][0] * 0.5)
    assert {"DISV_BOTM", "OC_HEAD_FILERECORD", "TDIS_TIME_UNITS", "IC_STRT"} <= set(sheet.filled)


def test_heads_are_observed_through_an_obs_file_the_model_names(deck_dir):
    the_deck = study_deck.deck(deck_dir)
    targets = [(study_deck.Observed("head_1", 1.0, 1.0, "heads"), 6), (study_deck.Observed("head_2", 2.0, 1.0, "heads"), 9)]
    assert study_deck.observe_heads(deck_dir, targets, the_deck=the_deck) == ("study_heads.csv.ins", "study_heads.csv")
    assert "OBS6  study_heads.obs" in (deck_dir / "gwf.nam").read_text()
    assert "  head_1 HEAD 1 7\n  head_2 HEAD 1 10\n" in (deck_dir / "study_heads.obs").read_text()
    assert (deck_dir / "study_heads.csv.ins").read_text() == "pif ~\nl2 ~,~ !head_1! ~,~ !head_2!\n"
    with pytest.raises(PestppError, match="saves 2 time steps"):
        study_deck.observe_heads(deck_dir, targets, the_deck=dataclasses.replace(the_deck, steps=2, packages={}))


def test_a_reading_is_a_head_at_its_cell_once_per_site(monkeypatch, deck_dir):
    the_deck = dataclasses.replace(study_deck.deck(deck_dir), top=(10.0, 20.0, 30.0))
    monkeypatch.setattr("trid3nt_server.workflows.modflow.authoring.accepted_mesh.mesh_nodes",
                        lambda mesh: (None, None, None, None))
    cells = {(-1.0, 1.0): 0, (-2.0, 2.0): 1, (-3.0, 3.0): 2}
    monkeypatch.setattr("trid3nt_server.workflows.modflow.authoring.stress.at_cell",
                        lambda at, **_: cells[tuple(at["coordinates"])])
    wells = [Observation(value=20.13, units="ft", quantity="water_table_depth", site_id="A", lon=-1.0, lat=1.0),
             Observation(value=38.33, units="ft", quantity="groundwater_head", datum="NAVD88", site_id="A",
                         lon=-1.0, lat=1.0),
             Observation(value=19.6, units="ft", quantity="groundwater_head", datum="NGVD29", site_id="B",
                         lon=-2.0, lat=2.0),
             Observation(value=3.0, units="m", quantity="groundwater_head", datum="NAVD88", site_id="C",
                         lon=-3.0, lat=3.0)]
    targets, left = study_deck.head_targets(wells, the_deck=the_deck, mesh={}, utm_epsg=32610, frame="NAVD88")
    assert [(t.name, round(t.value, 6), cell) for t, cell in targets] == [
        ("head_1", round(10.0 - 20.13 * 0.3048, 6), 0), ("head_2", 3.0, 2)]
    assert len(left) == 2 and left[0].startswith("A: it is held once") and "NGVD29" in left[1]


def test_bounds_are_stated_around_one_or_refused():
    assert _bounds("NPF_K", {"lower": 0.1, "upper": 10, "source": "s"}) == (0.1, 10.0, "s", False)
    assert _bounds("NPF_K", [0.5, 2]) == (0.5, 2.0, "", False)
    assert _bounds("SFR_RHK", {"lower_value": 0.0003048, "upper_value": 30.48, "source": "s"}) == (
        0.0003048, 30.48, "s", True)
    for stated in (None, {"lower": 0.1}, [2.0, 5.0], {"lower_value": 3.0, "upper_value": 1.0}):
        with pytest.raises(PestppError) as refused:
            _bounds("NPF_K", stated)
        assert refused.value.error_code == "PESTPP_BOUNDS_UNSTATED"


def test_a_ranking_orders_by_the_mean_absolute_elementary_effect():
    sensitivity = outputs.read_sensitivity(_FIXTURES / "sen")
    order = ranked(sensitivity)
    effects = {row["parameter"]: row["sen_mean_abs"] for row in sensitivity}
    assert [effects[name] for name in order] == sorted(effects.values(), reverse=True)


def test_the_study_s_control_file_states_its_own_delimiter_and_the_run_s_options():
    lines = (_CALIBRATE / "study" / "pestpp.pst").read_text().splitlines()
    assert {"noptmax 3", "ies_num_reals 10", "additional_ins_delimiters ,"} <= set(lines)


def test_an_unstated_noptmax_refuses_the_study():
    import trid3nt_server.main as main

    main._import_tools_registry()
    from trid3nt_server.tools import TOOL_REGISTRY

    workflow = TOOL_REGISTRY["pestpp_calibrate_modflow"].fn.workflow
    refused = workflow.judged(SimpleNamespace(keywords={}, carried={}))
    assert refused["noptmax"].code == "PESTPP_ITERATIONS_UNSTATED"
    assert "noptmax" not in workflow.judged(SimpleNamespace(keywords={"noptmax": 3}, carried={}))
    assert {"run", "parameters", "observe", "event_time"} <= {row.name for row in workflow.inputs}


def _sfr_deck(directory, rhk=(0.5, 0.5)):
    """A two-cell DISV flow model with a two-reach SFR package, written by flopy as a run's deck is."""
    import flopy

    sim = flopy.mf6.MFSimulation(sim_ws=str(directory), exe_name="mf6")
    flopy.mf6.ModflowTdis(sim, nper=1, perioddata=[(1.0, 1, 1.0)])
    flopy.mf6.ModflowIms(sim)
    gwf = flopy.mf6.ModflowGwf(sim, modelname="gwf")
    flopy.mf6.ModflowGwfdisv(gwf, nlay=1, ncpl=2, nvert=6, top=10.0, botm=[0.0], length_units="meters",
                             vertices=[[0, 0, 0], [1, 100, 0], [2, 200, 0], [3, 0, 100], [4, 100, 100], [5, 200, 100]],
                             cell2d=[[0, 50, 50, 4, 0, 3, 4, 1], [1, 150, 50, 4, 1, 4, 5, 2]])
    flopy.mf6.ModflowGwfnpf(gwf, k=[5.0, 7.0], icelltype=1)
    flopy.mf6.ModflowGwfic(gwf, strt=9.0)
    flopy.mf6.ModflowGwfsfr(gwf, nreaches=2, connectiondata=[(0, -1), (1, 0)], perioddata=[(0, "inflow", 1.0)],
                            packagedata=[(0, (0, 0), 100.0, 5.0, 0.001, 8.0, 0.5, rhk[0], 0.05, 1, 1.0, 0),
                                         (1, (0, 1), 100.0, 5.0, 0.001, 7.9, 0.5, rhk[1], 0.05, 1, 1.0, 0)])
    sim.write_simulation(silent=True)
    return study_deck.deck(directory)


def test_a_list_column_is_tuned_through_its_rows_and_bounded_in_the_runs_unit(tmp_path):
    the_deck = _sfr_deck(tmp_path / "deck")
    parameter, pair = study_deck.multiplier(tmp_path / "deck", "SFR_RHK", lower=0.0003048, upper=30.48,
                                            the_deck=the_deck, absolute=True)
    assert pair == ("gwf.sfr.tpl", "gwf.sfr") and parameter.scale == 0.5
    assert (parameter.lower, parameter.upper) == pytest.approx((0.0003048 / 0.5, 30.48 / 0.5))
    rows = [line.split() for line in (tmp_path / "deck" / "gwf.sfr.tpl").read_text().splitlines()
            if line.strip().startswith(("1 ", "2 ")) and "~" in line]
    assert [row[8] for row in rows] == ["~sfr_rhk", "~sfr_rhk"] and [row[9] for row in rows] == ["~", "~"]
    second, _ = study_deck.multiplier(tmp_path / "deck", "SFR_MAN", lower=0.5, upper=2.0, the_deck=the_deck)
    assert second.scale == 0.05
    study_deck.substitute(tmp_path / "deck", [pair], [parameter, second], {"sfr_rhk": 2.0, "sfr_man": 1.0})
    written = [line.split() for line in (tmp_path / "deck" / "gwf.sfr").read_text().splitlines()
               if line.strip()[:2] in ("1 ", "2 ")][:2]
    assert [float(row[8]) for row in written] == [1.0, 1.0] and [float(row[9]) for row in written] == [0.05, 0.05]


def test_a_list_column_stating_two_values_or_a_value_outside_its_bounds_refuses(tmp_path):
    the_deck = _sfr_deck(tmp_path / "two", rhk=(0.5, 0.7))
    with pytest.raises(PestppError, match="2 different values"):
        study_deck.multiplier(tmp_path / "two", "SFR_RHK", lower=0.5, upper=2.0, the_deck=the_deck)
    the_deck = _sfr_deck(tmp_path / "one")
    with pytest.raises(PestppError, match="outside its stated bounds") as refused:
        study_deck.multiplier(tmp_path / "one", "SFR_RHK", lower=1.0, upper=30.0, the_deck=the_deck, absolute=True)
    assert refused.value.error_code == "PESTPP_BOUNDS_UNSTATED"
    with pytest.raises(PestppError, match="read INTERNAL"):
        study_deck.multiplier(tmp_path / "one", "NPF_K", lower=1.0, upper=30.0, the_deck=the_deck, absolute=True)
