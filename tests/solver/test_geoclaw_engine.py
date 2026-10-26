"""The GeoClaw engine file and its worker's manifest contract.

The three registry rows (local-solver spec, diagnostics parser, engine paths),
the exit verdict off the worker's metrics - including GeoClaw's exit-0 refusal,
whose own words beat its code - the launch line, and the entrypoint's strict
gate, staged-tfinal reading and recorded-stdout verdicts: every one offline; the
image and the two real solves are the live leg gate, not this file.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from trid3nt_server.workflows.solver.code_provenance import (
    engine_paths,
    resolve_engine,
)
from trid3nt_server.workflows.solver.diagnostics import _PARSERS
from trid3nt_server.workflows.solver.solver import LOCAL_SOLVER_SPEC_REGISTRY
from trid3nt_server.workflows.geoclaw.engine import (
    DEFAULT_GEOCLAW_IMAGE,
    GEOCLAW_SOLVER_NAME,
    _build_argv,
    classify_exit,
)

_WORKER = (Path(__file__).resolve().parents[2] / "workers" / "geoclaw"
           / "entrypoint.py")

# RECORDED: leg0 spike run_chile.log - the mass line, verbatim.
_CHILE_MASS = (" Total mass at initial time:    "
               "1.0575487000653194E+017\n")

# RECORDED: leg0 spike run_chile.log - the closing line (trailing blanks of the
# record elided; the regex never anchors the line end).
_CHILE_DONE = " Done integrating to time    32400.000000000000\n"

_CHILE_STDOUT = (
    "Reading data file: claw.data\n"
    + _CHILE_MASS
    + " Done integrating to time    32400.000000000000\n"
)

# RECORDED: leg0 spike topoprobe/run_nc.log - the refusal and its continuation.
_NC_STDOUT = (
    "Reading data file: topo.data\n"
    "         first 5 lines are comments and will be skipped\n"
    " ERROR:  NetCDF library was not included in this build\n"
    "   of GeoClaw.\n"
)

# RECORDED: leg0 spike run_isaac.log - the same mass line in fixed notation.
_ISAAC_FIXED_MASS = " Total mass at initial time: 14626180424349462.\n"


def _entrypoint():
    """The worker entrypoint module, loaded straight from its file."""
    spec = importlib.util.spec_from_file_location("geoclaw_worker_entrypoint",
                                                  _WORKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_engine_holds_its_three_registry_rows():
    assert GEOCLAW_SOLVER_NAME in LOCAL_SOLVER_SPEC_REGISTRY
    assert _PARSERS["geoclaw"].__name__ == "parse_geoclaw"
    assert resolve_engine("geoclaw") == "geoclaw"
    assert engine_paths("geoclaw") == (
        "workers/geoclaw/", "trid3nt_server/workflows/geoclaw/")


def test_the_spec_is_local_docker_with_network_none():
    spec = LOCAL_SOLVER_SPEC_REGISTRY[GEOCLAW_SOLVER_NAME]()
    assert spec.solver == "geoclaw"
    assert spec.network == "none"
    assert spec.args_key == "geoclaw_args"
    assert spec.classify_exit is classify_exit
    assert spec.stdout_name == "geoclaw.stdout"


def test_the_launch_line_mounts_the_rundir_and_appends_args(tmp_path,
                                                            monkeypatch):
    monkeypatch.delenv("TRID3NT_GEOCLAW_IMAGE", raising=False)
    assert _build_argv("run1", tmp_path, []) == [
        "docker", "run", "--rm", "--name", "run1",
        "-v", f"{tmp_path}:/data", "-w", "/data", DEFAULT_GEOCLAW_IMAGE]
    monkeypatch.setenv("TRID3NT_GEOCLAW_IMAGE", "geoclaw:test")
    assert _build_argv("run1", tmp_path, ["--x"])[-2:] == ["geoclaw:test",
                                                           "--x"]


def test_a_success_metrics_is_ok(tmp_path):
    (tmp_path / "geoclaw_metrics.json").write_text(json.dumps({
        "correct_end": True, "initial_mass": 1.0575487000653194e17,
        "engine_exit_code": 0}))
    status, code, error, extra = classify_exit(tmp_path, 0)
    assert (status, code, error) == ("ok", 0, None)
    assert extra["initial_mass"] == 1.0575487000653194e17


def test_a_run_with_no_water_is_refused_first_naming_the_mass(tmp_path):
    (tmp_path / "geoclaw_metrics.json").write_text(json.dumps({
        "correct_end": True, "initial_mass": 0.0, "engine_exit_code": 0}))
    status, code, error, extra = classify_exit(tmp_path, 0)
    assert (status, code) == ("error", 2)
    assert "Total mass at initial time is 0" in error and extra["error_code"] == "GEOCLAW_NO_WATER"


def test_a_small_domain_s_mass_is_not_refused_and_stays_on_the_record(tmp_path):
    (tmp_path / "geoclaw_metrics.json").write_text(json.dumps({
        "correct_end": True, "initial_mass": 1728.0, "engine_exit_code": 0}))
    status, _code, _error, extra = classify_exit(tmp_path, 0)
    assert status == "ok" and extra["initial_mass"] == 1728.0


def test_a_nonzero_exit_is_an_error_naming_the_worker_s_sentence(tmp_path):
    (tmp_path / "geoclaw_metrics.json").write_text(json.dumps({
        "correct_end": False,
        "error": "xgeoclaw exited with code 139"}))
    status, code, error, _ = classify_exit(tmp_path, 139)
    assert (status, code) == ("error", 139)
    assert error == "xgeoclaw exited with code 139"


def test_an_exit_zero_refusal_is_an_error_in_the_engine_s_own_words(tmp_path):
    """GeoClaw's plain-Fortran stop exits 0; the metrics verdict, not the code,
    decides - the leg0 lesson in one assertion."""
    (tmp_path / "geoclaw_metrics.json").write_text(json.dumps({
        "correct_end": False, "engine_exit_code": 0, "frames": 0,
        "error": "ERROR:  NetCDF library was not included in this build"}))
    status, code, error, extra = classify_exit(tmp_path, 0)
    assert (status, code) == ("error", 2)
    assert "NetCDF library was not included" in error
    assert extra["engine_exit_code"] == 0


def test_the_strict_gate_refuses_an_unknown_case_field():
    entry = _entrypoint()
    with pytest.raises(entry.UnknownManifestFieldError) as excinfo:
        entry._strict_section("case", {"results": [], "bogus": 1},
                              entry._CASE_FIELDS)
    assert excinfo.value.error_code == "GEOCLAW_MANIFEST_UNKNOWN_FIELD"
    assert "bogus" in str(excinfo.value)


def test_a_case_without_claw_data_refuses(tmp_path):
    entry = _entrypoint()
    with pytest.raises(entry.CaseError) as excinfo:
        entry._solve_case(tmp_path, {"module": "geoclaw", "results": []}, None)
    assert excinfo.value.error_code == "GEOCLAW_CASE_CLAWDATA_MISSING"


def test_a_claw_data_with_no_readable_tfinal_refuses(tmp_path):
    entry = _entrypoint()
    (tmp_path / "claw.data").write_text("0.0                  =: t0\n")
    with pytest.raises(entry.CaseError) as excinfo:
        entry._solve_case(tmp_path, {"module": "geoclaw", "results": []}, None)
    assert excinfo.value.error_code == "GEOCLAW_CASE_TFINAL_UNREAD"


def test_the_staged_tfinal_comes_from_the_claw_data_line(tmp_path):
    entry = _entrypoint()
    (tmp_path / "claw.data").write_text(
        "#### header line\n"
        "0.0                  =: t0\n"
        "32400.0              =: tfinal\n")
    assert entry._staged_tfinal(tmp_path / "claw.data") == 32400.0


def test_the_recorded_chile_stdout_parses_as_a_success():
    entry = _entrypoint()
    verdict = entry._verdict(_CHILE_STDOUT, 32400.0, [], 0)
    assert verdict["correct_end"] is True
    assert verdict["initial_mass"] == 1.0575487000653194e17
    assert verdict["final_time"] == 32400.0
    assert "error" not in verdict


def test_the_recorded_netcdf_refusal_fails_by_its_own_words():
    entry = _entrypoint()
    verdict = entry._verdict(_NC_STDOUT, 10.0, [], 0)
    assert verdict["correct_end"] is False
    assert verdict["error_code"] == "GEOCLAW_ENGINE_REFUSED"
    assert "NetCDF library was not included in this build" in verdict["error"]


def test_a_stdout_that_never_reaches_the_done_line_is_a_typed_failure():
    entry = _entrypoint()
    verdict = entry._verdict(_CHILE_MASS, 32400.0, [], 0)
    assert verdict["correct_end"] is False
    assert verdict["error_code"] == "GEOCLAW_DONE_LINE_MISSING"
    assert verdict["initial_mass"] == 1.0575487000653194e17


def test_a_short_final_time_fails_against_the_staged_tfinal():
    entry = _entrypoint()
    stdout = _CHILE_MASS + " Done integrating to time    100.0\n"
    verdict = entry._verdict(stdout, 32400.0, [], 0)
    assert verdict["correct_end"] is False
    assert verdict["error_code"] == "GEOCLAW_FINAL_TIME_MISMATCH"
    assert "100.0" in verdict["error"] and "32400.0" in verdict["error"]


def test_the_mass_regex_parses_both_recorded_formats():
    entry = _entrypoint()
    e_format = entry._parse_stdout(_CHILE_STDOUT)
    fixed = entry._parse_stdout(_ISAAC_FIXED_MASS)
    assert e_format["initial_mass"] == 1.0575487000653194e17
    assert fixed["initial_mass"] == 14626180424349462.0


def test_a_stop_reason_is_a_refusal_too():
    entry = _entrypoint()
    verdict = entry._verdict("STOP mcapa does not agree with auxtype\n",
                             32400.0, [], 0)
    assert verdict["correct_end"] is False
    assert verdict["error_code"] == "GEOCLAW_ENGINE_REFUSED"
    assert verdict["error"].startswith("STOP")


def test_a_finished_solve_reports_correct_end_frames_and_the_engine_code(
        tmp_path, monkeypatch):
    entry = _entrypoint()
    (tmp_path / "claw.data").write_text("0.0 =: t0\n32400.0 =: tfinal\n")
    (tmp_path / "fort.q0000").write_text("q")
    (tmp_path / "fort.q0018").write_text("q")
    (tmp_path / "gauge32412.txt").write_text("g")
    (tmp_path / "fort.debug").write_text("not a frame")
    monkeypatch.setattr(entry, "_run_engine",
                        lambda data_dir, argv: (0, _CHILE_STDOUT))
    metrics = entry._solve_case(
        tmp_path, {"module": "geoclaw", "results": ["fort.q0018", "gauge32412.txt"]}, None)
    assert metrics["correct_end"] is True
    assert metrics["engine_exit_code"] == 0
    assert metrics["frames"] == 2
    assert metrics["tfinal"] == 32400.0
    assert metrics["final_time"] == 32400.0


def test_a_clean_exit_missing_its_declared_results_is_a_typed_error(
        tmp_path, monkeypatch):
    entry = _entrypoint()
    (tmp_path / "claw.data").write_text("0.0 =: t0\n32400.0 =: tfinal\n")
    monkeypatch.setattr(entry, "_run_engine",
                        lambda data_dir, argv: (0, _CHILE_STDOUT))
    metrics = entry._solve_case(tmp_path, {"module": "geoclaw", "results": ["fort.q0099"]}, None)
    assert metrics["correct_end"] is False
    assert metrics["error_code"] == "GEOCLAW_RESULTS_MISSING"
    assert "fort.q0099" in metrics["error"]


def test_a_bad_manifest_is_a_typed_metrics_write(tmp_path):
    entry = _entrypoint()
    (tmp_path / "manifest.json").write_text("{not json")
    code = entry.main(["--data-dir", str(tmp_path)])
    assert code == 2
    payload = json.loads((tmp_path / "geoclaw_metrics.json").read_text())
    assert payload["error_code"] == "GEOCLAW_MANIFEST_INVALID"
    assert payload["correct_end"] is False


def test_main_runs_a_shimmed_xgeoclaw_and_writes_ok_metrics(tmp_path,
                                                            monkeypatch):
    """The whole worker path, offline: manifest in, a PATH-shimmed xgeoclaw
    that prints the recorded lines and leaves one frame, metrics out."""
    shim = tmp_path / "bin"
    shim.mkdir()
    exe = shim / "xgeoclaw"
    exe.write_text("#!/bin/sh\n"
                   "echo ' Total mass at initial time:"
                   "    1.0575487000653194E+017'\n"
                   "touch fort.q0000\n"
                   "echo ' Done integrating to time    32400.000000000000'\n"
                   "exit 0\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    data = tmp_path / "data"
    data.mkdir()
    (data / "claw.data").write_text(
        "0.0                  =: t0\n"
        "32400.0              =: tfinal\n")
    (data / "manifest.json").write_text(json.dumps({
        "run_id": "rid1",
        "case": {"module": "geoclaw", "results": ["fort.q0000"]}}))
    entry = _entrypoint()
    code = entry.main(["--data-dir", str(data)])
    assert code == 0
    payload = json.loads((data / "geoclaw_metrics.json").read_text())
    assert payload["status"] == "ok"
    assert payload["correct_end"] is True
    assert payload["initial_mass"] == 1.0575487000653194e17
    assert payload["final_time"] == 32400.0
    assert payload["frames"] == 1
    assert payload["engine_exit_code"] == 0
    assert payload["run_id"] == "rid1"
    assert payload["wall_s"] >= 0


def test_main_maps_an_exit_zero_refusal_to_a_failed_run(tmp_path,
                                                        monkeypatch):
    shim = tmp_path / "bin"
    shim.mkdir()
    exe = shim / "xgeoclaw"
    exe.write_text("#!/bin/sh\n"
                   "echo ' ERROR:  NetCDF library was not included in this"
                   " build'\n"
                   "exit 0\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    data = tmp_path / "data"
    data.mkdir()
    (data / "claw.data").write_text("0.0 =: t0\n10.0 =: tfinal\n")
    (data / "manifest.json").write_text(json.dumps({
        "run_id": "rid-nc", "case": {"module": "geoclaw", "results": []}}))
    entry = _entrypoint()
    code = entry.main(["--data-dir", str(data)])
    assert code == 1
    payload = json.loads((data / "geoclaw_metrics.json").read_text())
    assert payload["status"] == "error"
    assert payload["correct_end"] is False
    assert payload["error_code"] == "GEOCLAW_ENGINE_REFUSED"
    assert payload["engine_exit_code"] == 0
    assert "NetCDF library was not included in this build" in payload["error"]
