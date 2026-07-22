"""P2 tests for the TELEMAC river-dye local solve seam (run_telemac).

Covers the registration + the LocalSolverSpec shape + the classify_exit metrics
fold, WITHOUT docker / TELEMAC (pure Python; the container build-time smoke and
the through-the-seam dev proof cover the live path).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from grace2_agent.tools.solver import (
    LOCAL_DOCKER_WORKFLOW_NAME,
    LOCAL_SOLVER_SPEC_REGISTRY,
    SOLVER_WORKFLOW_REGISTRY,
)
from grace2_agent.workflows import run_telemac as T


def test_telemac_registered_in_solver_workflow_registry():
    # Importing run_telemac (via workflows/__init__ or directly) self-registers.
    assert T.TELEMAC_SOLVER_NAME == "telemac_river_dye"
    assert SOLVER_WORKFLOW_REGISTRY.get("telemac_river_dye") == LOCAL_DOCKER_WORKFLOW_NAME


def test_telemac_local_spec_factory_registered():
    assert "telemac_river_dye" in LOCAL_SOLVER_SPEC_REGISTRY
    factory = LOCAL_SOLVER_SPEC_REGISTRY["telemac_river_dye"]
    spec = factory()
    assert spec.solver == "telemac_river_dye"
    assert spec.workflow_name == LOCAL_DOCKER_WORKFLOW_NAME
    assert spec.args_key == "telemac_args"
    assert spec.exec_kind == "docker"
    assert spec.stdout_uri_field == "telemac_stdout_uri"
    assert spec.stderr_uri_field == "telemac_stderr_uri"
    assert spec.classify_exit is not None


def test_build_argv_is_sfincs_style_volume_mount(tmp_path, monkeypatch):
    monkeypatch.setenv("GRACE2_TELEMAC_IMAGE", "trid3nt-local/telemac:latest")
    spec = T.telemac_local_spec()
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
    monkeypatch.setenv("GRACE2_TELEMAC_IMAGE", "custom/telemac:9.9")
    spec = T.telemac_local_spec()
    argv = spec.build_argv("R", tmp_path, ["--extra"])
    assert argv[-2] == "custom/telemac:9.9"
    assert argv[-1] == "--extra"  # appended after the image (SFINCS parity)


def _write_metrics(rundir: Path, **fields) -> None:
    (rundir / "telemac_metrics.json").write_text(json.dumps(fields), encoding="utf-8")


def test_classify_exit_ok_folds_metrics(tmp_path):
    _write_metrics(
        tmp_path,
        status="ok", correct_end=True, n_frames=19, dye_cmax_final=100.0,
        result_slf="r2d_river.slf", npoin=812, nelem=1440, reach_name="snake",
        centerline_length_m=5900.0, lb_order=["inflow", "outflow"], wall_s=42.0,
    )
    status, code, err, extra = T._classify_exit(tmp_path, 0)
    assert status == "ok" and code == 0 and err is None
    assert extra["correct_end"] is True
    assert extra["n_frames"] == 19
    assert extra["result_slf"] == "r2d_river.slf"
    assert extra["npoin"] == 812
    assert extra["reach_name"] == "snake"


def test_classify_exit_nonzero_process_is_error(tmp_path):
    _write_metrics(tmp_path, correct_end=True, n_frames=5)
    status, code, err, extra = T._classify_exit(tmp_path, 137)
    assert status == "error" and code == 137
    assert "non-zero code 137" in err
    # metrics still folded so the failure carries context
    assert extra["n_frames"] == 5


def test_classify_exit_clean_exit_but_no_correct_end_is_error(tmp_path):
    _write_metrics(
        tmp_path, correct_end=False, error="TELEMAC did not reach CORRECT END OF RUN",
    )
    status, code, err, extra = T._classify_exit(tmp_path, 0)
    assert status == "error" and code == 2
    assert "CORRECT END" in err


def test_classify_exit_missing_metrics_falls_back_to_exit_code(tmp_path):
    # No metrics file at all -> trust the process exit code (clean -> ok).
    status, code, err, extra = T._classify_exit(tmp_path, 0)
    assert status == "ok" and code == 0 and err is None
    assert extra == {}
