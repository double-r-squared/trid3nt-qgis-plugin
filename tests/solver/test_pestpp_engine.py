"""The PEST++ engine: its registry rows, exit verdict, staging, output table and worker contract.

Every study file read here was recorded from a real pestpp-sen / pestpp-ies run in the worker image
(tests/fixtures/pestpp/README.md); the image and the solves themselves are the canary's, not this file's.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import trid3nt_server.workflows.solver.solver as solver_mod
from trid3nt_server.store import objects as storage
from trid3nt_server.workflows.pestpp import outputs, staging
from trid3nt_server.workflows.pestpp.engine import (
    DEFAULT_PESTPP_IMAGE,
    PESTPP_SOLVER_NAME,
    _build_argv,
    _spec,
    classify_exit,
)
from trid3nt_server.workflows.pestpp.errors import PestppError
from trid3nt_server.workflows.solver.code_provenance import engine_paths, resolve_engine
from trid3nt_server.workflows.solver.solver import LOCAL_SOLVER_SPEC_REGISTRY

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pestpp"
_WORKER = Path(__file__).resolve().parents[2] / "workers" / "pestpp" / "entrypoint.py"

#: The sentence the recorded failure's record ends on.
_ALL_FAILED = "EnsembleMethod error: all realizations failed during initial evaluation"


def _entrypoint():
    spec = importlib.util.spec_from_file_location("pestpp_worker_entrypoint", _WORKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _study(tmp_path: Path) -> Path:
    return Path(shutil.copytree(_FIXTURES / "study", tmp_path / "study"))


def test_the_engine_holds_its_registry_rows():
    assert PESTPP_SOLVER_NAME in LOCAL_SOLVER_SPEC_REGISTRY
    assert resolve_engine("pestpp") == "pestpp"
    assert engine_paths("pestpp") == ("workers/pestpp/", "trid3nt_server/workflows/pestpp/")


def test_the_spec_is_local_docker_with_network_none_and_reads_the_record():
    spec = LOCAL_SOLVER_SPEC_REGISTRY[PESTPP_SOLVER_NAME]()
    assert spec.network == "none"
    assert spec.log_name == "pestpp.rec"
    assert spec.classify_exit is classify_exit


def test_the_launch_line_mounts_the_rundir_and_appends_args(tmp_path, monkeypatch):
    monkeypatch.delenv("TRID3NT_PESTPP_IMAGE", raising=False)
    argv = _build_argv("R1", tmp_path, ["--x"])
    assert argv == ["docker", "run", "--rm", "--name", "R1", "-v", f"{tmp_path}:/data",
                    "-w", "/data", DEFAULT_PESTPP_IMAGE, "--x"]


def test_a_completed_study_is_ok(tmp_path):
    shutil.copy(_FIXTURES / "ies" / "pestpp_metrics.json", tmp_path)
    status, code, error, extra = classify_exit(tmp_path, 0)
    assert (status, code, error) == ("ok", 0, None)
    assert extra["module"] == "ies" and extra["agents"] == 7


def test_a_failed_study_is_an_error_in_the_worker_s_words(tmp_path):
    shutil.copy(_FIXTURES / "failure" / "pestpp_metrics.json", tmp_path)
    status, code, error, extra = classify_exit(tmp_path, 1)
    assert (status, code) == ("error", 1)
    assert error == "pestpp-ies exited with code 1"
    assert _ALL_FAILED in extra["listing_tail"]


def test_a_clean_exit_that_never_completed_is_an_error(tmp_path):
    (tmp_path / "pestpp_metrics.json").write_text(json.dumps(
        {"module": "sen", "correct_end": False, "error": "pestpp-sen exited cleanly but its pestpp.rec never "
                                                         "printed 'analysis complete'"}))
    status, code, error, _ = classify_exit(tmp_path, 0)
    assert (status, code) == ("error", 2)
    assert "analysis complete" in error


class _Store:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, Bucket: str, Key: str, Body: Any, **_kw: Any) -> dict:  # noqa: N803
        self.objects[(Bucket, Key)] = Body.read() if hasattr(Body, "read") else Body
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803
        import io

        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def list_objects_v2(self, Bucket: str, Prefix: str, **_kw: Any) -> dict:  # noqa: N803
        keys = sorted(k for b, k in self.objects if b == Bucket and k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}


@pytest.fixture()
def store():
    fake = _Store()
    storage.set_client(fake)
    try:
        yield fake
    finally:
        storage.set_client(None)


def test_a_failed_study_s_refusal_carries_its_record_tail(store, tmp_path):
    spec = _spec()
    shutil.copy(_FIXTURES / "failure" / "pestpp.rec", tmp_path)
    shutil.copy(_FIXTURES / "failure" / "pestpp_metrics.json", tmp_path)
    for name in (spec.stdout_name, spec.stderr_name):
        (tmp_path / name).touch()
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])
    run = solver_mod._LocalRun(
        run_id="R1", rundir=tmp_path, runs_bucket="runs", proc=proc, output_patterns=[],
        started_at="2026-11-11T00:00:00Z", stdout_path=tmp_path / spec.stdout_name,
        stderr_path=tmp_path / spec.stderr_name, spec=spec)
    solver_mod._supervise_local_run(run)
    completion = json.loads(store.objects[("runs", "R1/completion.json")])
    assert completion["status"] == "error"
    assert completion["error"].startswith("pestpp-ies exited with code 1\npestpp listing tail:")
    assert completion["error"].rstrip().endswith(_ALL_FAILED)
    assert completion["error_code"] == "PESTPP_FAILED"


def test_the_recorded_control_file_names_its_templates_and_instructions():
    templates, instructions = staging.interface_files(_FIXTURES / "study", "pestpp.pst")
    assert len(templates) == 5 and all(p.endswith(".tpl") and m.startswith("mult/") for p, m in templates)
    assert instructions == [("heads.csv.ins", "heads.csv"), ("sfr.csv.ins", "sfr.csv")]


def test_a_version_2_control_file_names_them_in_its_external_tables():
    v1 = staging.interface_files(_FIXTURES / "study", "pestpp.pst")
    assert staging.interface_files(_FIXTURES / "study_v2", "pestpp.pst") == v1


def test_a_control_file_with_no_model_file_section_refuses(tmp_path):
    text = (_FIXTURES / "study" / "pestpp.pst").read_text()
    (tmp_path / "pestpp.pst").write_text(text.split("* model input/output")[0])
    with pytest.raises(PestppError) as err:
        staging.interface_files(tmp_path, "pestpp.pst")
    assert err.value.error_code == "PESTPP_CONTROL_UNREAD"


def test_the_staged_check_accepts_the_recorded_study(tmp_path):
    staging.check_staged(_study(tmp_path), steering="pestpp.pst", inputs=[], cores=7)


def test_the_staged_check_refuses_a_missing_template(tmp_path):
    study = _study(tmp_path)
    (study / "sto_ss_cn_inst0_constant.csv.tpl").unlink()
    with pytest.raises(PestppError) as err:
        staging.check_staged(study, steering="pestpp.pst")
    assert err.value.error_code == "PESTPP_INTERFACE_MISSING"
    assert "sto_ss_cn_inst0_constant.csv.tpl" in str(err.value)


def test_the_staged_check_refuses_a_folder_a_template_writes_into(tmp_path):
    study = _study(tmp_path)
    shutil.rmtree(study / "mult")
    with pytest.raises(PestppError) as err:
        staging.check_staged(study, steering="pestpp.pst")
    assert "mult/npf_k_cn_inst0_constant.csv" in str(err.value)


def test_staging_names_the_control_file_pestpp_and_seats_container_cores(tmp_path, monkeypatch):
    study = _study(tmp_path)
    (study / "pestpp.pst").rename(study / "freyberg.pst")
    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setattr(staging, "setting", lambda name: {"CONTAINER_CORES": 5}[name])
    seen: dict[str, Any] = {}

    async def fake_stage_run(rundir, run_id, **kw):
        seen.update(kw, rundir=rundir, files=sorted(p.name for p in rundir.iterdir()))
        kw["check"](rundir, steering=kw["steering"])
        return {"run_id": run_id}

    monkeypatch.setattr(staging, "stage_run", fake_stage_run)
    asyncio.run(staging.stage_study(study, tool="ies", control="freyberg.pst", name="freyberg ies"))
    assert "pestpp.pst" in seen["files"] and "freyberg.pst" not in seen["files"]
    assert (seen["module"], seen["steering"], seen["cores"]) == ("ies", "pestpp.pst", 5)
    assert seen["results"] == ["pestpp.rec", "pestpp.phi.actual.csv"]
    assert seen["server_facts"] == {"name": "freyberg ies"}


def test_staging_refuses_a_control_file_the_directory_does_not_hold(tmp_path):
    with pytest.raises(PestppError) as err:
        asyncio.run(staging.stage_study(_study(tmp_path), tool="ies", control="nope.pst", name="x"))
    assert err.value.error_code == "PESTPP_CONTROL_MISSING"


def test_phi_falls_across_the_recorded_ensemble_iterations():
    phi = outputs.read_phi(_FIXTURES / "ies")
    assert [row["iteration"] for row in phi] == [0, 1, 2, 3]
    assert [row["total_runs"] for row in phi] == [10, 52, 94, 136]
    assert phi[0]["mean"] == 7.07652e06 and phi[-1]["mean"] == 460.883
    assert phi[-1]["base"] == 1.36708e-14


def test_the_parameter_ensemble_reads_the_last_iteration_unstated():
    last = outputs.read_parameter_ensemble(_FIXTURES / "ies")
    assert last == outputs.read_parameter_ensemble(_FIXTURES / "ies", 3)
    assert last != outputs.read_parameter_ensemble(_FIXTURES / "ies", 0)
    assert len(last) == 10 and "base" in last
    assert len(last["0"]) == 5 and last["0"]["pname:npf_k_cn_inst:0_ptype:cn_pstyle:m"] == 1.0060108731766481682


def test_the_residuals_read_each_observation_against_its_target():
    # The benchmark's targets are its base model's outputs, so the base realization's residuals are zero to
    # rounding: this file pins the columns, never the residual's sign.
    rows = outputs.read_residuals(_FIXTURES / "ies")
    control = (_FIXTURES / "study" / "pestpp.pst").read_text().split("* observation data")[1]
    targets = {line.split()[0]: float(line.split()[1]) for line in control.split("* model command line")[0]
               .strip().splitlines()}
    assert len(rows) == len(targets) == 725
    assert all(r["measured"] == targets[r["name"]] and r["group"] in r["name"] for r in rows)
    assert all(abs(r["residual"]) < 1e-7 and r["weight"] == 1.0 for r in rows)


def test_the_sensitivity_summary_reads_each_parameter():
    rows = outputs.read_sensitivity(_FIXTURES / "sen")
    assert [r["n_samples"] for r in rows] == [4] * 5
    assert rows[0]["parameter"] == "pname:npf_k_cn_inst:0_ptype:cn_pstyle:m"
    assert rows[0]["sen_mean_abs"] == 2.18166e07


def test_an_output_the_study_did_not_write_refuses_by_name(tmp_path):
    for read in outputs.OUTPUTS.values():
        with pytest.raises(PestppError) as err:
            read(tmp_path)
        assert err.value.error_code == "PESTPP_OUTPUT_MISSING"


def test_fetch_study_downloads_the_run_s_own_files(store, tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "runs_bucket", lambda: "runs")
    store.objects[("runs", "R1/pestpp.phi.actual.csv")] = b"a"
    store.objects[("runs", "R1/pestpp.rec")] = b"b"
    store.objects[("runs", "R1/completion.json")] = b"c"
    got = outputs.fetch_study("R1", tmp_path / "out")
    assert sorted(p.name for p in got.iterdir()) == ["pestpp.phi.actual.csv", "pestpp.rec"]


def test_the_strict_gate_refuses_an_unknown_case_field():
    entry = _entrypoint()
    with pytest.raises(entry.UnknownManifestFieldError):
        entry._strict_section("case", {"module": "ies", "bogus": 1}, entry._CASE_FIELDS)


@pytest.mark.parametrize("case,code", [
    ({"module": "pest"}, "PESTPP_CASE_TOOL_UNKNOWN"),
    ({"module": "ies", "steering": "freyberg.pst"}, "PESTPP_CASE_STEERING_UNREAD"),
    ({"module": "ies"}, "PESTPP_CASE_CONTROL_MISSING"),
])
def test_a_case_the_worker_cannot_start_refuses(tmp_path, case, code):
    entry = _entrypoint()
    with pytest.raises(entry.CaseError) as err:
        entry._solve_case(tmp_path, tmp_path / "work", case)
    assert err.value.error_code == code


def test_the_recorded_failure_never_completed():
    entry = _entrypoint()
    assert entry._completed(_FIXTURES / "failure") is False


_SHIM = """#!{python}
import os, sys
from pathlib import Path
Path(os.environ["SHIM_LOG"]).open("a").write(f"{{os.getcwd()}} {{' '.join(sys.argv[1:])}}\\n")
if sys.argv[-1].startswith(":"):
    Path("pestpp.rec").write_text("pestpp-ies analysis complete...\\n")
    Path("pestpp.phi.actual.csv").write_text("iteration\\n")
"""


def test_main_runs_one_manager_and_its_agents_in_their_own_copies(tmp_path, monkeypatch):
    entry = _entrypoint()
    bindir, data, work = tmp_path / "bin", _study(tmp_path), tmp_path / "work"
    bindir.mkdir()
    shim = bindir / "pestpp-ies"
    shim.write_text(_SHIM.format(python=sys.executable))
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("SHIM_LOG", str(tmp_path / "shim.log"))
    (data / "manifest.json").write_text(json.dumps({"case": {
        "module": "ies", "steering": "pestpp.pst", "cores": 3,
        "results": ["pestpp.rec", "pestpp.phi.actual.csv"], "server_facts": {"name": "x"}}}))
    assert entry.main(["--data-dir", str(data), "--work-dir", str(work)]) == 0
    metrics = json.loads((data / "pestpp_metrics.json").read_text())
    assert (metrics["status"], metrics["agents"], metrics["completed"]) == ("ok", 3, True)
    calls = sorted((tmp_path / "shim.log").read_text().splitlines())
    assert f"{data} pestpp.pst /h :4004" in calls
    for index in range(3):
        assert f"{work / f'agent_{index}'} pestpp.pst /h localhost:4004" in calls
        assert (work / f"agent_{index}" / "pestpp.pst").is_file()
        assert not (work / f"agent_{index}" / "manifest.json").exists()
        assert not (work / f"agent_{index}" / "pestpp.rec").exists()
