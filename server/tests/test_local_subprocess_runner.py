"""Tests for the local subprocess runner (TRID3NT offline build).

Covers:
  1. Backend selection per solver: each pip-only engine dispatches to its own
     LocalSolverSpec factory via LOCAL_SOLVER_SPEC_REGISTRY.
  2. Command construction: build_argv returns the expected subprocess command
     for SWMM (run_inp.py), Landlab (run_chain.py), and OpenQuake (run_oq.py).
  3. PYTHONPATH injection: env_overrides prepends the repo root so worker
     imports resolve when the agent package is installed in an isolated venv.
  4. Manifest written to rundir: launch_local_solver writes manifest.json
     to the run directory before launching the subprocess.
  5. Mocked subprocess end-to-end: the supervisor thread picks up exit 0 and
     writes a correct completion.json (no real process spawned).

No AWS calls, no real subprocess (mocked via monkeypatch), no docker.
"""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

import grace2_agent.tools.solver as solver_mod
from grace2_agent.tools.solver import (
    LOCAL_SOLVER_SPEC_REGISTRY,
    set_emitter_binding,
    set_runs_bucket,
    set_s3_client,
)


# --------------------------------------------------------------------------- #
# Minimal fake S3 client (mirrors test_solver_local_docker.py)
# --------------------------------------------------------------------------- #


def _no_such_key(key: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": f"missing {key}"}}, "GetObject"
    )


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[tuple[str, str]] = []

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise _no_such_key(Key)
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket: str, Key: str, Body: Any, **_kw: Any) -> dict:  # noqa: N803
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[(Bucket, Key)] = data
        self.put_calls.append((Bucket, Key))
        return {}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def reset_seams():
    """Reset solver DI seams and in-flight run registry around each test."""
    for setter in (set_s3_client,):
        setter(None)
    set_emitter_binding(None)
    set_runs_bucket(None)
    solver_mod._LOCAL_RUNS.clear()
    try:
        yield
    finally:
        for setter in (set_s3_client,):
            setter(None)
        set_emitter_binding(None)
        set_runs_bucket(None)
        solver_mod._LOCAL_RUNS.clear()


@pytest.fixture()
def local_backend_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set env vars for local-docker backend with a temp runs dir."""
    runs_dir = tmp_path / "runs"
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    monkeypatch.setenv("GRACE2_RUNS_DIR", str(runs_dir))
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    return runs_dir


def _seed_engine_manifest(
    s3: FakeS3Client,
    solver: str,
    *,
    bucket: str = "deck-bucket",
    args_key: str = "build_spec",
) -> str:
    """Seed a minimal manifest for a pip-only engine (no deck files, just build_spec)."""
    manifest = {
        "inputs": [],
        args_key: {"solver": solver, "test": True},
        "outputs": ["output.csv"],
    }
    key = f"test/{solver}/manifest.json"
    s3.objects[(bucket, key)] = json.dumps(manifest).encode()
    return f"s3://{bucket}/{key}"


def _wait_completion(
    s3: FakeS3Client, run_id: str, timeout_s: float = 15.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    bucket = "test-runs-bucket"
    key = f"{run_id}/completion.json"
    while time.monotonic() < deadline:
        if (bucket, key) in s3.objects:
            return json.loads(s3.objects[(bucket, key)])
        time.sleep(0.02)
    raise AssertionError(
        f"supervisor never wrote completion.json within {timeout_s}s "
        f"(keys: {sorted(s3.objects.keys())})"
    )


# --------------------------------------------------------------------------- #
# 1. Registry presence: each solver maps to a factory in LOCAL_SOLVER_SPEC_REGISTRY
# --------------------------------------------------------------------------- #


def test_swmm_registered_in_local_spec_registry() -> None:
    """run_swmm.register_swmm_solver() must populate LOCAL_SOLVER_SPEC_REGISTRY."""
    # Force the registration by importing the module (idempotent).
    import grace2_agent.workflows.run_swmm as _swmm_mod  # noqa: F401
    del _swmm_mod

    assert "swmm" in LOCAL_SOLVER_SPEC_REGISTRY, (
        "swmm missing from LOCAL_SOLVER_SPEC_REGISTRY; "
        "run_swmm.register_swmm_solver() must call register_local_solver_spec"
    )


def test_landlab_registered_in_local_spec_registry() -> None:
    import grace2_agent.workflows.run_landlab as _ll_mod
    del _ll_mod

    assert "landlab" in LOCAL_SOLVER_SPEC_REGISTRY, (
        "landlab missing from LOCAL_SOLVER_SPEC_REGISTRY"
    )


def test_openquake_registered_in_local_spec_registry() -> None:
    import grace2_agent.workflows.model_seismic_hazard_scenario as _oq_mod
    del _oq_mod

    assert "openquake" in LOCAL_SOLVER_SPEC_REGISTRY, (
        "openquake missing from LOCAL_SOLVER_SPEC_REGISTRY"
    )


# --------------------------------------------------------------------------- #
# 2. Command construction: build_argv returns the correct subprocess command
# --------------------------------------------------------------------------- #


def test_swmm_build_argv_calls_run_inp_py() -> None:
    from grace2_agent.workflows.run_swmm import swmm_local_spec

    spec = swmm_local_spec()
    run_id = "TEST-001"
    rundir = Path("/tmp/runs/TEST-001")
    # SWMM takes bare .inp filenames (not --manifest); pass a sample filename.
    argv = spec.build_argv(run_id, rundir, ["mesh.inp"])

    assert argv[0] == sys.executable, f"expected sys.executable, got {argv[0]}"
    # Second arg must be a path ending with run_inp.py.
    assert argv[1].endswith("run_inp.py"), f"unexpected script: {argv[1]}"
    # SWMM run_inp.py takes bare .inp filenames, not --manifest.
    assert "mesh.inp" in argv, f"mesh.inp not in argv: {argv}"


def test_landlab_build_argv_calls_run_chain_py() -> None:
    from grace2_agent.workflows.run_landlab import landlab_local_spec

    spec = landlab_local_spec()
    run_id = "TEST-002"
    rundir = Path("/tmp/runs/TEST-002")
    argv = spec.build_argv(run_id, rundir, [])

    assert argv[0] == sys.executable
    assert argv[1].endswith("run_chain.py"), f"unexpected script: {argv[1]}"
    assert "--manifest" in argv


def test_openquake_build_argv_calls_run_oq_py() -> None:
    from grace2_agent.workflows.model_seismic_hazard_scenario import openquake_local_spec

    spec = openquake_local_spec()
    run_id = "TEST-003"
    rundir = Path("/tmp/runs/TEST-003")
    argv = spec.build_argv(run_id, rundir, [])

    assert argv[0] == sys.executable
    assert argv[1].endswith("run_oq.py"), f"unexpected script: {argv[1]}"
    assert "--manifest" in argv


# --------------------------------------------------------------------------- #
# 3. PYTHONPATH injection: env_overrides prepends the repo root
# --------------------------------------------------------------------------- #


def test_swmm_spec_has_pythonpath_override() -> None:
    from grace2_agent.workflows.run_swmm import swmm_local_spec

    spec = swmm_local_spec()
    assert spec.env_overrides is not None, "swmm spec must set env_overrides"
    assert "PYTHONPATH" in spec.env_overrides, (
        "swmm spec must inject PYTHONPATH so services.workers.* imports resolve"
    )
    # The repo root must be the first path element.
    first_path = spec.env_overrides["PYTHONPATH"].split(":")[0]
    assert Path(first_path).is_dir(), f"PYTHONPATH first element not a dir: {first_path}"


def test_landlab_spec_has_pythonpath_override() -> None:
    from grace2_agent.workflows.run_landlab import landlab_local_spec

    spec = landlab_local_spec()
    assert spec.env_overrides is not None
    assert "PYTHONPATH" in spec.env_overrides
    first_path = spec.env_overrides["PYTHONPATH"].split(":")[0]
    assert Path(first_path).is_dir()


def test_openquake_spec_has_pythonpath_override() -> None:
    from grace2_agent.workflows.model_seismic_hazard_scenario import openquake_local_spec

    spec = openquake_local_spec()
    assert spec.env_overrides is not None
    assert "PYTHONPATH" in spec.env_overrides
    first_path = spec.env_overrides["PYTHONPATH"].split(":")[0]
    assert Path(first_path).is_dir()


# --------------------------------------------------------------------------- #
# 4. Manifest written to rundir before subprocess launch
# --------------------------------------------------------------------------- #


def test_launch_writes_manifest_to_rundir(
    reset_seams,
    local_backend_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """launch_local_solver must write manifest.json to the rundir so the shim
    can read it via the ``--manifest manifest.json`` flag."""
    from grace2_agent.tools.solver import launch_local_solver, LocalSolverSpec

    s3 = FakeS3Client()
    set_s3_client(s3)

    # Seed a minimal SWMM-style manifest.
    manifest = {
        "inputs": [],
        "build_spec": {"solver": "swmm", "test": True},
        "outputs": [],
    }
    s3.objects[("deck-bucket", "test/manifest.json")] = json.dumps(manifest).encode()
    model_setup_uri = "s3://deck-bucket/test/manifest.json"

    # Use a dummy spec whose build_argv captures the rundir so we can check it.
    captured_rundir: list[Path] = []

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        captured_rundir.append(rundir)
        # Return a real executable that exits immediately (no side effects).
        return [sys.executable, "-c", "import sys; sys.exit(0)"]

    def classify_exit(rundir: Path, exit_code: int) -> tuple:
        return ("ok", exit_code, None, {})

    spec = LocalSolverSpec(
        solver="swmm",
        workflow_name="local-exec",
        args_key="build_spec",
        build_argv=build_argv,
        stdout_name="out.stdout",
        stderr_name="out.stderr",
        stdout_uri_field="stdout_uri",
        stderr_uri_field="stderr_uri",
        exec_kind="exec",
        classify_exit=classify_exit,
    )

    handle = launch_local_solver(spec, model_setup_uri, compute_class="standard")
    assert handle is not None

    # Wait for the supervisor to finish so the rundir is stable.
    _wait_completion(s3, handle.run_id)

    # manifest.json must exist in the rundir the spec received.
    assert len(captured_rundir) == 1
    rundir = captured_rundir[0]
    manifest_path = rundir / "manifest.json"
    assert manifest_path.exists(), f"manifest.json not written to rundir {rundir}"
    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert written.get("build_spec", {}).get("solver") == "swmm"


# --------------------------------------------------------------------------- #
# 5. Mocked subprocess: supervisor writes correct completion.json on exit 0
# --------------------------------------------------------------------------- #


def test_subprocess_runner_exit0_produces_ok_completion(
    reset_seams,
    local_backend_env: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a real subprocess that exits 0, the supervisor must write
    completion.json with status='ok' and upload it to the runs bucket."""
    from grace2_agent.tools.solver import launch_local_solver, LocalSolverSpec

    s3 = FakeS3Client()
    set_s3_client(s3)

    manifest = {
        "inputs": [],
        "build_spec": {"solver": "landlab", "test": True},
        "outputs": [],
    }
    s3.objects[("deck-bucket", "test/landlab.json")] = json.dumps(manifest).encode()

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        # A subprocess that exits cleanly with no output.
        return [sys.executable, "-c", "import sys; sys.exit(0)"]

    def classify_exit(rundir: Path, exit_code: int) -> tuple:
        if exit_code != 0:
            return ("error", exit_code, f"exited {exit_code}", {})
        return ("ok", 0, None, {})

    spec = LocalSolverSpec(
        solver="landlab",
        workflow_name="local-exec",
        args_key="build_spec",
        build_argv=build_argv,
        stdout_name="landlab.stdout",
        stderr_name="landlab.stderr",
        stdout_uri_field="landlab_stdout_uri",
        stderr_uri_field="landlab_stderr_uri",
        exec_kind="exec",
        classify_exit=classify_exit,
    )

    handle = launch_local_solver(
        spec, "s3://deck-bucket/test/landlab.json", compute_class="standard"
    )

    completion = _wait_completion(s3, handle.run_id)
    assert completion["status"] == "ok", completion
    assert completion["exit_code"] == 0
    assert completion["run_id"] == handle.run_id
    assert completion["error"] is None
    # Stdout/stderr must have been uploaded even if empty.
    assert ("test-runs-bucket", f"{handle.run_id}/landlab.stdout") in s3.objects


def test_subprocess_runner_nonzero_exit_produces_error_completion(
    reset_seams,
    local_backend_env: Path,
) -> None:
    """A subprocess that exits non-zero must produce status='error' in
    completion.json."""
    from grace2_agent.tools.solver import launch_local_solver, LocalSolverSpec

    s3 = FakeS3Client()
    set_s3_client(s3)

    manifest = {"inputs": [], "build_spec": {}, "outputs": []}
    s3.objects[("b", "m.json")] = json.dumps(manifest).encode()

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        return [sys.executable, "-c", "import sys; sys.exit(3)"]

    def classify_exit(rundir: Path, exit_code: int) -> tuple:
        if exit_code != 0:
            return ("error", exit_code, f"exited {exit_code}", {})
        return ("ok", 0, None, {})

    spec = LocalSolverSpec(
        solver="openquake",
        workflow_name="local-exec",
        args_key="build_spec",
        build_argv=build_argv,
        stdout_name="oq.stdout",
        stderr_name="oq.stderr",
        stdout_uri_field="oq_stdout_uri",
        stderr_uri_field="oq_stderr_uri",
        exec_kind="exec",
        classify_exit=classify_exit,
    )

    handle = launch_local_solver(spec, "s3://b/m.json", compute_class="standard")
    completion = _wait_completion(s3, handle.run_id)
    assert completion["status"] == "error"
    assert completion["exit_code"] == 3
    assert completion["error"] is not None


# --------------------------------------------------------------------------- #
# 6. env_overrides propagated to subprocess environment
# --------------------------------------------------------------------------- #


def test_env_overrides_set_in_subprocess_environment(
    reset_seams,
    local_backend_env: Path,
) -> None:
    """env_overrides must appear in the subprocess environment.
    We verify by having the subprocess write os.environ['GRACE2_TEST_PYPATH']
    to a file and checking the file contents."""
    from grace2_agent.tools.solver import launch_local_solver, LocalSolverSpec

    s3 = FakeS3Client()
    set_s3_client(s3)

    manifest = {"inputs": [], "build_spec": {}, "outputs": []}
    s3.objects[("b", "env_test.json")] = json.dumps(manifest).encode()

    # Use a temp path the subprocess can write to.
    import tempfile

    output_file = Path(tempfile.mktemp(suffix=".txt"))

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        # Write the injected env var value to the output file.
        script = (
            f"import os, pathlib; "
            f"pathlib.Path({str(output_file)!r}).write_text("
            f"os.environ.get('GRACE2_TEST_PYPATH', 'MISSING'))"
        )
        return [sys.executable, "-c", script]

    def classify_exit(rundir: Path, exit_code: int) -> tuple:
        return ("ok" if exit_code == 0 else "error", exit_code, None, {})

    spec = LocalSolverSpec(
        solver="swmm",
        workflow_name="local-exec",
        args_key="build_spec",
        build_argv=build_argv,
        stdout_name="out.stdout",
        stderr_name="out.stderr",
        stdout_uri_field="stdout_uri",
        stderr_uri_field="stderr_uri",
        exec_kind="exec",
        classify_exit=classify_exit,
        env_overrides={"GRACE2_TEST_PYPATH": "/injected/repo/root"},
    )

    handle = launch_local_solver(spec, "s3://b/env_test.json", compute_class="standard")
    _wait_completion(s3, handle.run_id)

    assert output_file.exists(), "subprocess did not write output file"
    value = output_file.read_text()
    assert value == "/injected/repo/root", (
        f"env override not propagated to subprocess; got {value!r}"
    )
    output_file.unlink(missing_ok=True)
