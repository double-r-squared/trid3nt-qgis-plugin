import csv

import numpy as np
import pytest

from trid3nt.workflows.pestpp.authoring.control import Observed, Parameter, write_control


def test_named_native_parameter_columns_reach_the_control_table(tmp_path):
    parameters = [Parameter("first", 1.0, 0.5, 2.0, "first"),
                  Parameter("second", 1.0, 0.5, 2.0, "second")]
    write_control(tmp_path, name="pestpp.pst", parameters=parameters,
                  observations=[Observed("head", 1.0, "heads", 1.0, "cited")],
                  templates=[], instructions=[], command="mf6",
                  parameter_data={"first": {"PARTRANS": "none", "PARCHGLIM": "relative",
                                            "PARVAL1": 0.8, "PARLBND": 0.2, "PARUBND": 3.0,
                                            "PARGP": "custom", "SCALE": 2, "OFFSET": 0.1}})
    with (tmp_path / "study_parameters.csv").open() as stream:
        first, second = csv.DictReader(stream)
    assert first["partrans"] == "none" and first["parchglim"] == "relative"
    assert float(first["parval1"]) == 0.8 and float(first["parlbnd"]) == 0.2 and float(first["parubnd"]) == 3
    assert first["pargp"] == "custom" and float(first["scale"]) == 2 and float(first["offset"]) == 0.1
    assert second["partrans"] == "log" and float(second["parval1"]) == 1


def test_pilot_point_method_and_neighbours_are_applied():
    from trid3nt.workflows.pestpp.helpers.geostat import covariance, kriging_factors

    points = np.array([[0., 0.], [2., 0.], [4., 0.]])
    cells = np.array([[1., 0.]])
    _, factors = kriging_factors(cells, points, 4., maxpts_interp=1, variogram="spherical")
    assert factors.tolist() == [[1.0]]
    matrix = covariance(points, 4., np.ones(3), variogram="gaussian")
    assert matrix[0, 1] == pytest.approx(np.exp(-0.25))
    assert covariance(points, 4., np.ones(3), variogram="spherical")[0, 1] == pytest.approx(0.3125)


def test_stage_plan_preserves_default_and_checks_dependencies():
    from trid3nt.workflows.pestpp.workflow import stage_order
    from trid3nt.workflows.pestpp.errors import PestppError

    assert stage_order(None, screen=True) == ("screen", "calibrate", "resolve")
    assert stage_order(None, screen=False) == ("calibrate", "resolve")
    assert stage_order(["calibrate"], screen=True) == ("calibrate",)
    assert stage_order(["screen"], screen=False) == ("screen",)
    with pytest.raises(PestppError, match="resolve"):
        stage_order(["resolve", "calibrate"], screen=False)


def test_native_rename_and_tie_reach_the_template_and_external_table(tmp_path):
    from trid3nt.workflows.pestpp.authoring.control import complete_values

    template = tmp_path / "input.tpl"
    template.write_text("ptf ~\n~first          ~ ~second         ~\n")
    parameters = [Parameter("first", 1.0, 0.5, 2.0, "group"),
                  Parameter("second", 1.0, 0.5, 2.0, "group")]
    write_control(tmp_path, name="pestpp.pst", parameters=parameters,
                  observations=[Observed("head", 1.0, "heads", 1.0, "cited")],
                  templates=[("input.tpl", "input")], instructions=[], command="mf6",
                  parameter_data={"first": {"PARNME": "primary", "DERCOM": 1},
                                  "second": {"PARTRANS": "tied", "PARTIED": "primary"}})
    assert template.read_text().splitlines()[1] == "~primary        ~ ~second         ~"
    with (tmp_path / "study_parameters.csv").open() as stream:
        first, second = csv.DictReader(stream)
    assert (first["parnme"], first["dercom"], second["partied"]) == ("primary", "1", "primary")
    parameters = [Parameter("primary", 1., 0.5, 2., "group"),
                  Parameter("tied", 4., 2., 8., "group", transform="tied", tied_to="primary"),
                  Parameter("fixed", 5., 4., 6., "group", transform="fixed")]
    assert complete_values(parameters, {"primary": 1.5}) == {"primary": 1.5, "tied": 6., "fixed": 5.}


def test_geostruct_choices_reach_modflow_factors_and_prior(monkeypatch, tmp_path):
    from pathlib import Path
    from trid3nt.workflows.pestpp.authoring import modflow
    from trid3nt.workflows.pestpp.helpers import geostat

    source = Path(__file__).resolve().parents[1] / "fixtures" / "pestpp" / "calibrate" / "deck"
    the_deck = modflow.deck(source)
    state = {"spacing": 300, "variogram": {"type": "spherical", "a": 600, "contribution": 2,
                                         "nugget": 1, "anisotropy": 2, "bearing": 90},
             "maxpts_interp": 4, "remove_negative_factors": False}
    fields = modflow.fields({"NPF_K": state}, the_deck)
    factors = geostat.kriging_factors
    selected = {}

    def capture(*args, **choices):
        selected.update(choices)
        return factors(*args, **choices)

    monkeypatch.setattr(geostat, "kriging_factors", capture)
    observations = [Observed("head", 1.0, "heads", 1.0, "cited")]
    parameters, templates, instructions = modflow.interface(
        source, tmp_path / "study", [("NPF_K", 0.1, 10.0, False)], the_deck=the_deck,
        targets=[(observations[0], 0)], spatial=fields)
    write_control(tmp_path / "study", name="pestpp.pst", parameters=parameters, observations=observations,
                  templates=templates, instructions=instructions, command="mf6")
    assert selected == {"variogram": "spherical", "maxpts_interp": 4, "remove_negative_factors": False,
                        "contribution": 2, "nugget": 1, "anisotropy": 2, "bearing": 90}
    assert (tmp_path / "study" / "study_prior.cov").is_file()
    assert {(p.variogram, p.contribution, p.nugget, p.anisotropy, p.bearing) for p in parameters} == {
        ("spherical", 2, 1, 2, 90)}


@pytest.mark.asyncio
async def test_sdk_and_template_stages_share_control_and_skip_resolve(monkeypatch):
    import shutil
    from contextlib import nullcontext
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from trid3nt.sdk.study import StudyResult, calibrate, open_study, resolve, screen
    from trid3nt.workflows.pestpp import engine, outputs, staging, study
    from trid3nt.workflows.pestpp.authoring import modflow
    from trid3nt.workflows.pestpp.errors import PestppError
    from trid3nt.workflows.pestpp.templates.calibrate.calibrate import calibrate as template

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "pestpp" / "calibrate"
    finished = SimpleNamespace(engine="modflow", record={}, staged={}, run_id="finished")
    values = {"run": finished, "method": None, "observe": (), "stages": ["calibrate"],
              "parameters": {"NPF_K": [0.1, 10]}, "tune": None, "balance": False, "sigma": None,
              "parameter_data": {"npf_k": {"PARCHGLIM": "factor", "SCALE": 1.0}}}
    supplied = {"stages": values["stages"], "parameter_data": values["parameter_data"]}
    env = SimpleNamespace(run={}, supplied=supplied, data=values)
    state = SimpleNamespace(keywords={"NOPTMAX": 3}, ready=True, domain=None, settings={})
    workflow = template.workflow
    monkeypatch.setattr(study, "production", lambda _state: env)

    async def read(_env, key):
        return values[key]

    monkeypatch.setattr(study, "read", read)
    monkeypatch.setattr(staging, "fetch_deck", lambda _staged, dest: shutil.copytree(fixture / "deck", dest))
    monkeypatch.setattr(modflow, "targets",
                        lambda *_args, **_kwargs: ([(Observed("head_1", 1., "heads", 1., "cited"), 0)], []))
    recorded = []

    async def stage(source, *, tool, control, **_kwargs):
        recorded.append((tool, (source / control).read_bytes(), (source / "study_parameters.csv").read_bytes()))
        return {"case": {"cores": 1}}

    monkeypatch.setattr(staging, "stage_study", stage)
    monkeypatch.setattr(engine, "solve_case", AsyncMock(return_value={"run_id": "fit"}))
    monkeypatch.setattr(outputs, "fetch_study", lambda *_args: fixture / "study")
    read_ensemble = outputs.read_parameter_ensemble
    monkeypatch.setattr(outputs, "read_parameter_ensemble",
                        lambda files, iteration=None: {"base": {"npf_k": 1.0}} if iteration == 0 else
                        read_ensemble(files, iteration))
    monkeypatch.setattr(workflow, "_reviewed", AsyncMock())
    monkeypatch.setattr(workflow, "publish_charts", AsyncMock())
    monkeypatch.setattr(modflow, "calibrated", AsyncMock())
    result = await workflow.launch(state)
    assert isinstance(result.value, StudyResult)
    assert result.value.stages == {"calibrate": "fit"}
    assert "solve" not in result.results
    workflow.publish_charts.assert_awaited_once()
    modflow.calibrated.assert_not_awaited()
    assert [tool for tool, *_ in recorded] == ["ies"]

    env.run = {}
    model = SimpleNamespace(_workflow=workflow, _case=SimpleNamespace(name="sdk", bound=nullcontext), strict=False,
                            _filling=AsyncMock(return_value=(state, None, None)), _template_warning=lambda: None)
    async with open_study(model) as manually:
        with pytest.raises(PestppError, match="resolve requires"):
            await resolve(manually)
        assert await calibrate(manually) == "fit"
        assert (await manually.result()).value == result.value
    assert len(recorded) == 2 and recorded[0] == recorded[1]

    values["stages"] = ["screen"]
    env.run = {}
    monkeypatch.setattr(outputs, "read_sensitivity", lambda _files: [{"parameter": "npf_k", "sen_mean_abs": 0.5}])
    async with open_study(model) as manually:
        assert await screen(manually) == "fit"
        screened = await manually.result()
        assert screened.value.stages == {"screen": "fit"}
        assert screened.value.ranking == ["npf_k"]
    assert [tool for tool, *_ in recorded] == ["ies", "ies", "sen"]
