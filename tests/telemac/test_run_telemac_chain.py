"""Tests for the TELEMAC local solve seam (run_telemac).

Covers the one engine registration + the LocalSolverSpec shape + the exit
classification's metrics fold, WITHOUT docker / TELEMAC (pure Python; the
container build-time smoke and the through-the-seam dev proof cover the live
path).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trid3nt_server.workflows.solver.solver import (
    LOCAL_DOCKER_WORKFLOW_NAME,
    LOCAL_SOLVER_SPEC_REGISTRY,
    SOLVER_WORKFLOW_REGISTRY,
)
from trid3nt_server.workflows.telemac.solving import run_telemac as T


def test_the_solver_identifier_is_the_engine_name():
    # Importing run_telemac (via workflows/__init__ or directly) self-registers.
    assert T.TELEMAC_SOLVER_NAME == "telemac"
    assert SOLVER_WORKFLOW_REGISTRY.get("telemac") == LOCAL_DOCKER_WORKFLOW_NAME


def test_the_engine_registers_once_and_no_question_registers_a_solver():
    question_named = [name for name in LOCAL_SOLVER_SPEC_REGISTRY
                      if name.startswith("telemac") and name != "telemac"]
    assert question_named == []


def test_telemac_local_spec_factory_registered():
    assert "telemac" in LOCAL_SOLVER_SPEC_REGISTRY
    factory = LOCAL_SOLVER_SPEC_REGISTRY["telemac"]
    spec = factory()
    assert spec.solver == "telemac"
    assert spec.network == "none"
    assert spec.workflow_name == LOCAL_DOCKER_WORKFLOW_NAME
    assert spec.args_key == "telemac_args"
    assert spec.exec_kind == "docker"
    assert spec.stdout_uri_field == "telemac_stdout_uri"
    assert spec.stderr_uri_field == "telemac_stderr_uri"
    assert spec.classify_exit is not None


def test_build_argv_is_sfincs_style_volume_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("TRID3NT_TELEMAC_IMAGE", "trid3nt-local/telemac:latest")
    spec = LOCAL_SOLVER_SPEC_REGISTRY["telemac"]()
    rundir = tmp_path / "run-01"
    rundir.mkdir()
    argv = spec.build_argv("RUNID123", rundir, [])
    # docker run --rm --name RUNID123 -v <rundir>:/data -w /data <image>
    assert argv[:5] == ["docker", "run", "--rm", "--name", "RUNID123"]
    assert "-v" in argv and f"{rundir}:/data" in argv
    assert "-w" in argv and "/data" in argv
    assert argv[-1] == "trid3nt-local/telemac:latest"
    # No self-S3-I/O env injection (unlike geoclaw's --network host spec).
    assert "--network" not in argv


def test_build_argv_honors_image_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TRID3NT_TELEMAC_IMAGE", "custom/telemac:9.9")
    spec = LOCAL_SOLVER_SPEC_REGISTRY["telemac"]()
    argv = spec.build_argv("R", tmp_path, ["--extra"])
    assert argv[-2] == "custom/telemac:9.9"
    assert argv[-1] == "--extra"  # appended after the image (SFINCS parity)


def _write_metrics(rundir: Path, **fields) -> None:
    (rundir / "telemac_metrics.json").write_text(json.dumps(fields), encoding="utf-8")


def test_classify_exit_ok_folds_metrics(tmp_path):
    _write_metrics(
        tmp_path,
        status="ok", correct_end=True, n_frames=19, module="telemac2d",
        result_slf="r2d_river.slf", npoin=812, nelem=1440, reach_name="snake",
        wall_s=42.0,
    )
    status, code, err, extra = T.classify_exit(tmp_path, 0)
    assert status == "ok" and code == 0 and err is None
    assert extra["correct_end"] is True
    assert extra["n_frames"] == 19
    assert extra["result_slf"] == "r2d_river.slf"
    assert extra["npoin"] == 812
    assert extra["reach_name"] == "snake"


def test_classify_exit_names_the_module_the_worker_says_it_ran(tmp_path):
    _write_metrics(tmp_path, correct_end=False, module="artemis")
    _status, _code, err, _extra = T.classify_exit(tmp_path, 3)
    assert "artemis exited with non-zero code 3" in err


def test_classify_exit_nonzero_process_is_error(tmp_path):
    _write_metrics(tmp_path, correct_end=True, n_frames=5)
    status, code, err, extra = T.classify_exit(tmp_path, 137)
    assert status == "error" and code == 137
    assert "telemac exited with non-zero code 137" in err
    # metrics still folded so the failure carries context
    assert extra["n_frames"] == 5


def test_classify_exit_clean_exit_but_no_correct_end_is_error(tmp_path):
    _write_metrics(
        tmp_path, correct_end=False, error="TELEMAC did not reach CORRECT END OF RUN",
    )
    status, code, err, extra = T.classify_exit(tmp_path, 0)
    assert status == "error" and code == 2
    assert "CORRECT END" in err


def test_classify_exit_folds_the_listing_tail_a_failed_run_carries(tmp_path):
    # The listing file may never reach the run prefix when the solve dies; the
    # excerpt the worker wrote into its report is then the only listing there is.
    _write_metrics(
        tmp_path, correct_end=False, error="PLANTE",
        listing_tail="STOP 1\n PLANTE: PROGRAM STOPPED AFTER AN ERROR\n",
    )
    status, _code, _err, extra = T.classify_exit(tmp_path, 1)
    assert status == "error"
    assert "PLANTE" in extra["listing_tail"]


def test_classify_exit_names_the_keyword_lecdon_asked_for(tmp_path):
    """The sheet refuses only on the dictionary's OBLIG files; everything else
    the engine will not start without, it demands by name in its own listing,
    and that sentence is what reaches the caller."""
    _write_metrics(
        tmp_path, correct_end=False,
        listing_tail=" THE FOLLOWING KEYWORD IS MANDATORY:\n"
                     " GEOMETRY FILE (FICHIER DE GEOMETRIE)\n"
                     "\n PLANTE: PROGRAM STOPPED AFTER AN ERROR\n",
    )
    _status, _code, err, _extra = T.classify_exit(tmp_path, 1)
    assert "the engine asked for" in err
    assert "GEOMETRY FILE (FICHIER DE GEOMETRIE)" in err


def test_classify_exit_invents_no_demand_where_the_engine_made_none(tmp_path):
    _write_metrics(tmp_path, correct_end=False,
                   listing_tail=" MURD3D: ITERATION NO. REACHED 100 , STOP.\n")
    _status, _code, err, _extra = T.classify_exit(tmp_path, 1)
    assert "the engine asked for" not in err


def test_classify_exit_missing_metrics_falls_back_to_exit_code(tmp_path):
    # No metrics file at all -> trust the process exit code (clean -> ok).
    status, code, err, extra = T.classify_exit(tmp_path, 0)
    assert status == "ok" and code == 0 and err is None
    assert extra == {}
