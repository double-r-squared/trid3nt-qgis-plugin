"""GeoClaw's Boussinesq module: one dictionary, one body, one worker executable.

Offline. ``geoclaw_bouss`` is the geoclaw rundata plus Clawpack's BoussData group, extracted by the same
extractor; a template body written over ``geoclaw`` runs on it unchanged; the worker names the executable by
the staged module and refuses a module it has none for. The image build and the two real solves are the
live gate, not this file. ASCII only.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from trid3nt_server.workflows.geoclaw.authoring.rundata import serialize
from trid3nt_server.workflows.geoclaw.errors import GeoclawError
from trid3nt_server.workflows.geoclaw.modules import GEO, GEOB, SlotRefused, body_on, wrapper_for
from trid3nt_server.workflows.geoclaw.templates.tsunami_inundation import tsunami_inundation as template
from trid3nt_server.workflows.runtime.sheet import fill

_BOUSS = ("BOUSS_BOUSS_EQUATIONS", "BOUSS_BOUSS_MIN_LEVEL", "BOUSS_BOUSS_MAX_LEVEL", "BOUSS_BOUSS_MIN_DEPTH",
          "BOUSS_BOUSS_SOLVER", "BOUSS_BOUSS_TSTART")
_WORKER = Path(__file__).resolve().parents[2] / "workers" / "geoclaw" / "entrypoint.py"


def _entrypoint():
    spec = importlib.util.spec_from_file_location("geoclaw_entrypoint_bouss", _WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_boussinesq_dictionary_is_the_geoclaw_one_plus_clawpacks_boussdata():
    assert GEOB.MODULE == "geoclaw_bouss" and len(GEOB.MODULE_INPUT) == len(GEO.MODULE_INPUT) + 6 == 165
    assert set(GEO.MODULE_INPUT) < set(GEOB.MODULE_INPUT)
    assert tuple(i for i in GEOB.MODULE_INPUT if i.startswith("BOUSS_")) == _BOUSS
    assert not any(i.startswith("BOUSS_") for i in GEO.MODULE_INPUT)
    defaults = {i: GEOB.MODULE_INPUT[i].engine_default for i in _BOUSS}
    assert defaults == {"BOUSS_BOUSS_EQUATIONS": 2, "BOUSS_BOUSS_MIN_LEVEL": 1, "BOUSS_BOUSS_MAX_LEVEL": 10,
                        "BOUSS_BOUSS_MIN_DEPTH": 10.0, "BOUSS_BOUSS_SOLVER": 3, "BOUSS_BOUSS_TSTART": 0.0}
    assert all(slot.rubrique == ("bouss.data",) for i, slot in GEOB.MODULE_INPUT.items() if i in _BOUSS)
    assert set(GEOB.COMPOSITES) == set(GEO.COMPOSITES) and wrapper_for("geoclaw_bouss") is GEOB


def test_a_boussinesq_keyword_unknown_to_the_module_refuses_at_import_by_name():
    with pytest.raises(SlotRefused) as caught:
        class Misspelled(GEOB):
            BOUSS_BOUSS_EQUATION = 2

    assert "BOUSS_BOUSS_EQUATION" in str(caught.value)
    with pytest.raises(SlotRefused) as caught:
        class Shallow(GEO):
            BOUSS_BOUSS_EQUATIONS = 2

    assert "BOUSS_BOUSS_EQUATIONS" in str(caught.value)
    with pytest.raises(SlotRefused) as caught:
        class Choice(GEOB):
            BOUSS_BOUSS_EQUATIONS = 3

    assert "BOUSS_BOUSS_EQUATIONS" in str(caught.value)


def test_a_template_body_runs_on_the_boussinesq_module_with_its_assertions_unchanged():
    rebased = body_on(template.STEERING, "geoclaw_bouss")

    assert rebased.MODULE == "geoclaw_bouss" and issubclass(rebased, GEOB)
    assert dict(rebased.ASSERTED) == dict(template.STEERING.ASSERTED)
    assert body_on(template.STEERING, "geoclaw") is template.STEERING
    with pytest.raises(SlotRefused) as caught:
        body_on(template.STEERING, "geoclaw_nonlinear")
    assert "geoclaw_nonlinear" in str(caught.value) and "geoclaw_bouss" in str(caught.value)


def test_the_sheet_writes_bouss_data_by_clawpacks_own_writer_on_the_boussinesq_module_only(tmp_path):
    stated = dict(CLAW_TFINAL=10.0, CLAW_NUM_OUTPUT_TIMES=2)
    serialize(fill(GEOB, **stated, BOUSS_BOUSS_EQUATIONS=1, BOUSS_BOUSS_MIN_DEPTH=1.0), tmp_path / "b")
    text = (tmp_path / "b" / "bouss.data").read_text()

    assert "1                    =: bouss_equations" in text and "1.0                  =: bouss_min_depth" in text
    assert "3                    =: bouss_solver" in text
    serialize(fill(GEO, **stated), tmp_path / "s")
    assert not (tmp_path / "s" / "bouss.data").exists()


def test_the_worker_names_the_executable_by_the_module_and_the_process_count():
    entry = _entrypoint()

    assert entry._engine_argv("geoclaw", 1) == ["xgeoclaw"]
    assert entry._engine_argv("geoclaw_bouss", 4) == ["mpiexec", "-n", "4", "xgeoclaw_bouss"]
    assert {"module", "cores", "threads"} <= entry._CASE_FIELDS


def test_a_stated_thread_count_reaches_OMP_NUM_THREADS_and_unstated_leaves_the_environment(monkeypatch):
    entry = _entrypoint()
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)

    assert entry._engine_env(3)["OMP_NUM_THREADS"] == "3"
    assert "OMP_NUM_THREADS" not in entry._engine_env(None)
    assert entry._engine_argv("geoclaw_bouss", 2, 3) == ["mpiexec", "-n", "2", "xgeoclaw_bouss"]


@pytest.mark.parametrize("case, code", [
    ({"module": "geoclaw_nonlinear", "results": []}, "GEOCLAW_CASE_MODULE_UNKNOWN"),
    ({"results": []}, "GEOCLAW_CASE_MODULE_UNSTATED"),
    ({"module": "geoclaw", "cores": 4, "results": []}, "GEOCLAW_CASE_CORES_UNUSED"),
    ({"module": "geoclaw", "threads": 2, "results": []}, "GEOCLAW_CASE_THREADS_UNUSED")])
def test_the_worker_refuses_a_module_it_has_no_executable_for_by_name(tmp_path, case, code):
    entry = _entrypoint()
    (tmp_path / "claw.data").write_text("0.0 =: t0\n10.0 =: tfinal\n")
    with pytest.raises(entry.CaseError) as caught:
        entry._solve_case(tmp_path, case, None)
    assert caught.value.error_code == code


def test_a_boussinesq_case_runs_the_staged_module_under_mpiexec_and_reports_it(tmp_path, monkeypatch):
    entry = _entrypoint()
    (tmp_path / "claw.data").write_text("0.0 =: t0\n10.0 =: tfinal\n")
    seen = []
    monkeypatch.setattr(entry, "_run_engine", lambda data_dir, argv, env=None: (seen.append((argv, env["OMP_NUM_THREADS"])) or 0,
                        " Total zeta at initial time:    0.0\n Done integrating to time    10.0\n"))
    metrics = entry._solve_case(tmp_path, {"module": "geoclaw_bouss", "cores": 3, "threads": 2, "results": []}, None)

    assert seen == [(["mpiexec", "-n", "3", "xgeoclaw_bouss"], "2")]
    assert metrics["correct_end"] is True and metrics["module"] == "geoclaw_bouss"
    assert metrics["initial_zeta"] == 0.0 and "initial_mass" not in metrics


def test_each_executable_is_held_to_the_initial_line_it_prints():
    entry = _entrypoint()
    zeta = " Total zeta at initial time:   -1.5E-03\n Done integrating to time    10.0\n"
    mass = " Total mass at initial time:    1.0E+10\n Done integrating to time    10.0\n"

    ok = entry._verdict(zeta, 10.0, [], 0, False, "geoclaw_bouss")
    assert ok["correct_end"] is True and ok["initial_zeta"] == -1.5e-3
    for text, module, code in [(mass, "geoclaw_bouss", "GEOCLAW_ZETA_LINE_MISSING"), (zeta, "geoclaw", "GEOCLAW_MASS_LINE_MISSING")]:
        refused = entry._verdict(text, 10.0, [], 0, False, module)
        assert refused["correct_end"] is False and refused["error_code"] == code


def test_a_run_names_its_module_and_its_process_count_and_a_wrong_one_refuses_by_name(monkeypatch):
    from trid3nt_server.workflows.geoclaw import workflow as gw

    steered = gw.GeoclawWorkflow._steered
    me = SimpleNamespace(steering=template.STEERING)

    assert steered(me, {}) is template.STEERING
    assert steered(me, {"module": "geoclaw_bouss", "cores": 1, "threads": 1}).MODULE == "geoclaw_bouss"
    for run, code in [({"module": "geoclaw_nonlinear"}, "GEOCLAW_MODULE_UNKNOWN"),
                      ({"cores": 2}, "GEOCLAW_CORES_UNUSED"),
                      ({"threads": 2}, "GEOCLAW_THREADS_UNUSED"),
                      ({"module": "geoclaw_bouss", "threads": 0}, "GEOCLAW_THREADS_OUT_OF_RANGE"),
                      ({"module": "geoclaw_bouss", "threads": (os.cpu_count() or 1) + 1}, "GEOCLAW_THREADS_OUT_OF_RANGE"),
                      ({"module": "geoclaw_bouss", "cores": 0}, "GEOCLAW_CORES_OUT_OF_RANGE"),
                      ({"module": "geoclaw_bouss", "cores": (os.cpu_count() or 1) + 1}, "GEOCLAW_CORES_OUT_OF_RANGE")]:
        with pytest.raises(GeoclawError) as caught:
            steered(me, run)
        assert caught.value.error_code == code


def test_the_staged_case_names_the_module_and_carries_cores_only_past_one(tmp_path):
    from trid3nt_server.workflows.geoclaw import workflow
    from trid3nt_server.workflows.solver import staging

    captured = []

    async def fake_stage_run(rundir, run_tag, **kwargs):
        captured.append(kwargs["case"])
        return {"case": kwargs["case"], "manifest_uri": "s3://x"}

    sheet = fill(GEOB, CLAW_TFINAL=10.0, CLAW_NUM_OUTPUT_TIMES=2, GAUGE_GAUGES=[[7, 0.5, 0.5, 0.0, 1e10]])
    original, original_serialize = staging.stage_run, workflow.serialize
    staging.stage_run, workflow.serialize = fake_stage_run, lambda sheet, rundir: {"written": []}
    try:
        for cores, threads in ((1, None), (3, 2)):
            asyncio.run(workflow.stage_sheet(sheet=sheet, run_tag="tagtagtag", prefix="geoclaw", name="x", cores=cores,
                                             threads=threads))
    finally:
        staging.stage_run, workflow.serialize = original, original_serialize

    assert captured == [{"module": "geoclaw_bouss", "results": ["gauge00007.txt"]},
                        {"module": "geoclaw_bouss", "cores": 3, "threads": 2, "results": ["gauge00007.txt"]}]
