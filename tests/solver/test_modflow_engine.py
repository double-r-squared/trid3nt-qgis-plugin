"""The MODFLOW engine file and its worker's manifest contract.

The three registry rows (local-solver spec, diagnostics parser, engine paths),
the exit verdict off the worker's metrics, the launch line, and the entrypoint's
strict gate and success convention - every one offline; the image and a real
mf6 solve are the live leg-3 gate, not this file.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from trid3nt_server.workflows.solver.code_provenance import (
    engine_paths,
    resolve_engine,
)
from trid3nt_server.workflows.solver.diagnostics import _PARSERS
from trid3nt_server.workflows.solver.solver import LOCAL_SOLVER_SPEC_REGISTRY
from trid3nt_server.workflows.modflow.engine import (
    DEFAULT_MODFLOW_IMAGE,
    MODFLOW_SOLVER_NAME,
    _build_argv,
    classify_exit,
)

_WORKER = (Path(__file__).resolve().parents[2] / "workers" / "modflow"
           / "entrypoint.py")


def _entrypoint():
    """The worker entrypoint module, loaded straight from its file."""
    spec = importlib.util.spec_from_file_location("modflow_worker_entrypoint",
                                                  _WORKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_engine_holds_its_three_registry_rows():
    assert MODFLOW_SOLVER_NAME in LOCAL_SOLVER_SPEC_REGISTRY
    assert _PARSERS["modflow"].__name__ == "parse_modflow"
    assert resolve_engine("modflow") == "modflow"
    assert engine_paths("modflow") == (
        "workers/modflow/", "trid3nt_server/workflows/modflow/")


def test_the_spec_is_local_docker_with_network_none():
    spec = LOCAL_SOLVER_SPEC_REGISTRY[MODFLOW_SOLVER_NAME]()
    assert spec.solver == "modflow"
    assert spec.network == "none"
    assert spec.args_key == "modflow_args"
    assert spec.classify_exit is classify_exit
    assert spec.stdout_name == "modflow.stdout"


def test_the_launch_line_mounts_the_rundir_and_appends_args(tmp_path,
                                                            monkeypatch):
    monkeypatch.delenv("TRID3NT_MODFLOW_IMAGE", raising=False)
    assert _build_argv("run1", tmp_path, []) == [
        "docker", "run", "--rm", "--name", "run1",
        "-v", f"{tmp_path}:/data", "-w", "/data", DEFAULT_MODFLOW_IMAGE]
    monkeypatch.setenv("TRID3NT_MODFLOW_IMAGE", "modflow:test")
    assert _build_argv("run1", tmp_path, ["--x"])[-2:] == ["modflow:test",
                                                           "--x"]


def test_a_clean_exit_with_normal_termination_is_ok(tmp_path):
    (tmp_path / "modflow_metrics.json").write_text(json.dumps({
        "module": "gwf", "correct_end": True, "normal_termination": True}))
    status, code, error, extra = classify_exit(tmp_path, 0)
    assert (status, code, error) == ("ok", 0, None)
    assert extra["module"] == "gwf"


def test_a_completed_run_records_the_steps_its_staged_clock_marched(tmp_path):
    (tmp_path / "modflow_metrics.json").write_text(json.dumps({
        "module": "gwt", "correct_end": True}))
    (tmp_path / "trid3nt.tdis").write_text(
        "BEGIN dimensions\n  NPER  2\nEND dimensions\n\nBEGIN perioddata\n"
        "    365.0  24  1.0\n    10.0  3  1.0\nEND perioddata\n")
    assert classify_exit(tmp_path, 0)[3]["ntimestep"] == 27


def test_a_run_whose_clock_is_unreadable_records_no_step_count(tmp_path):
    (tmp_path / "modflow_metrics.json").write_text(json.dumps({
        "module": "gwf", "correct_end": True}))
    assert "ntimestep" not in classify_exit(tmp_path, 0)[3]


def test_a_nonzero_exit_is_an_error_naming_the_worker_s_sentence(tmp_path):
    (tmp_path / "modflow_metrics.json").write_text(json.dumps({
        "module": "gwf", "correct_end": False,
        "error": "mf6 exited with code 1"}))
    status, code, error, _ = classify_exit(tmp_path, 1)
    assert (status, code) == ("error", 1)
    assert error == "mf6 exited with code 1"


def test_a_clean_exit_that_never_finished_is_an_error(tmp_path):
    (tmp_path / "modflow_metrics.json").write_text(json.dumps({
        "module": "gwf", "correct_end": False, "normal_termination": False,
        "error": "mf6 exited cleanly but its listing never printed "
                 "'normal termination'"}))
    status, code, error, _ = classify_exit(tmp_path, 0)
    assert (status, code) == ("error", 2)
    assert "normal termination" in error


def test_the_strict_gate_refuses_an_unknown_case_field():
    entry = _entrypoint()
    with pytest.raises(entry.UnknownManifestFieldError) as excinfo:
        entry._strict_section("case", {"module": "gwf", "bogus": 1},
                              entry._CASE_FIELDS)
    assert excinfo.value.error_code == "MODFLOW_MANIFEST_UNKNOWN_FIELD"
    assert "bogus" in str(excinfo.value)


def test_a_steering_file_mf6_never_reads_refuses(tmp_path):
    entry = _entrypoint()
    (tmp_path / "mfsim.nam").write_text("")
    with pytest.raises(entry.CaseError) as excinfo:
        entry._solve_case(tmp_path, {"module": "gwf", "steering": "other.nam"},
                          None)
    assert excinfo.value.error_code == "MODFLOW_CASE_STEERING_UNREAD"


def test_a_case_naming_no_simulation_refuses(tmp_path):
    entry = _entrypoint()
    with pytest.raises(entry.CaseError) as excinfo:
        entry._solve_case(tmp_path, {"module": "gwf"}, None)
    assert excinfo.value.error_code == "MODFLOW_CASE_SIMULATION_MISSING"


def test_a_case_naming_an_unknown_module_or_mode_refuses(tmp_path):
    entry = _entrypoint()
    (tmp_path / "mfsim.nam").write_text("")
    for body, code in [({"module": "prt"}, "MODFLOW_CASE_MODULE_UNKNOWN"),
                       ({"module": "gwf", "mode": "run"},
                        "MODFLOW_CASE_MODE_UNKNOWN")]:
        with pytest.raises(entry.CaseError) as excinfo:
            entry._solve_case(tmp_path, body, None)
        assert excinfo.value.error_code == code


def test_a_finished_solve_reports_correct_end(tmp_path, monkeypatch):
    entry = _entrypoint()
    (tmp_path / "mfsim.nam").write_text("BEGIN OPTIONS\nEND OPTIONS\n")
    (tmp_path / "mfsim.lst").write_text(
        "sim list\nNORMAL TERMINATION OF MF6\n")
    (tmp_path / "heads.hds").write_bytes(b"x")
    monkeypatch.setattr(entry, "_run_mf6", lambda data_dir, mode: 0)
    metrics = entry._solve_case(
        tmp_path, {"module": "gwf", "results": ["heads.hds"],
                   "server_facts": {"name": "island"}}, "run1")
    assert metrics["correct_end"] is True
    assert metrics["normal_termination"] is True
    assert metrics["module"] == "gwf" and metrics["name"] == "island"


def test_a_clean_exit_missing_its_declared_results_is_a_typed_error(
        tmp_path, monkeypatch):
    entry = _entrypoint()
    (tmp_path / "mfsim.nam").write_text("")
    (tmp_path / "mfsim.lst").write_text("NORMAL TERMINATION")
    monkeypatch.setattr(entry, "_run_mf6", lambda data_dir, mode: 0)
    metrics = entry._solve_case(
        tmp_path, {"module": "gwf", "results": ["heads.hds"]}, None)
    assert metrics["correct_end"] is False
    assert metrics["error_code"] == "MODFLOW_RESULTS_MISSING"


def test_a_validate_run_passes_the_flag_through(tmp_path, monkeypatch):
    entry = _entrypoint()
    (tmp_path / "mfsim.nam").write_text("")
    (tmp_path / "mfsim.lst").write_text("NORMAL TERMINATION")
    seen: list[list[str]] = []

    def fake_run(data_dir, mode):
        seen.append(["mf6"] + (["--mode", "validate"] if mode == "validate"
                               else []))
        return 0

    monkeypatch.setattr(entry, "_run_mf6", fake_run)
    metrics = entry._solve_case(
        tmp_path, {"module": "gwf", "mode": "validate"}, None)
    assert seen == [["mf6", "--mode", "validate"]]
    assert metrics["mode"] == "validate"


def test_a_bad_manifest_is_a_typed_metrics_write(tmp_path, capsys):
    entry = _entrypoint()
    (tmp_path / "manifest.json").write_text("{not json")
    code = entry.main(["--data-dir", str(tmp_path)])
    assert code == 2
    payload = json.loads((tmp_path / "modflow_metrics.json").read_text())
    assert payload["error_code"] == "MODFLOW_MANIFEST_INVALID"
    assert payload["correct_end"] is False


def test_main_runs_a_shimmed_mf6_and_writes_ok_metrics(tmp_path, monkeypatch):
    """The whole worker path, offline: manifest in, a PATH-shimmed mf6 that
    prints normal termination, metrics out."""
    shim = tmp_path / "bin"
    shim.mkdir()
    mf6 = shim / "mf6"
    mf6.write_text("#!/bin/sh\n"
                   "echo 'MODFLOW 6.7.0'\n"
                   "echo 'NORMAL TERMINATION OF MF6' > mfsim.lst\n"
                   "exit 0\n")
    mf6.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{__import__('os').environ['PATH']}")
    data = tmp_path / "data"
    data.mkdir()
    (data / "mfsim.nam").write_text("")
    (data / "manifest.json").write_text(json.dumps({
        "case": {"module": "gwf", "results": ["mfsim.lst"],
                 "server_facts": {"name": "shim"}}}))
    entry = _entrypoint()
    code = entry.main(["--data-dir", str(data)])
    assert code == 0
    payload = json.loads((data / "modflow_metrics.json").read_text())
    assert payload["status"] == "ok"
    assert payload["correct_end"] is True
    assert payload["normal_termination"] is True
    assert payload["name"] == "shim"
    assert payload["wall_s"] >= 0
