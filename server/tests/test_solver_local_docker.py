"""job-0291 (sprint-14-aws) — local-docker solver backend tests.

The SFINCS GCS-IN → sfincs → GCS-OUT envelope from
``services/workers/sfincs/entrypoint.py`` ported into the agent, with the
container being the plain upstream ``deltares/sfincs-cpu`` image run via
``docker run`` on the same instance.

Hard constraint honored here: **NO real docker invocation on this machine**
(the daemon is blocked). Every ``docker`` call resolves to a PATH-shim bash
script that records its argv, emulates the container behaviors (ok / fail /
hang), and supports ``docker kill`` against the run-mode shim's pidfile.
All S3 I/O goes through the ``tools.solver.set_s3_client`` seam with a
dict-backed fake (boto3-shaped ``get_object``/``put_object``).

Coverage maps to the kickoff §4 test list:

1.  Default env → backend is gcp-workflows; the Cloud Workflows path stays
    byte-identical (the full pre-existing ``test_solver.py`` suite is the
    primary guard; the explicit default assertion lives here).
2.  local-docker ``run_solver``: manifest staged from S3 (legacy ``gs_uri``
    field name carrying ``s3://`` VALUES — resolved by scheme), docker
    launched detached with ``--rm --name <run_id> -v <rundir>:/data -w
    /data $GRACE2_SFINCS_IMAGE``, ExecutionHandle returned immediately.
3.  Supervisor writes the EXACT entrypoint.py completion.json schema —
    ok, error, and cancel paths — and uploads outputs + stdout/stderr.
4.  ``wait_for_completion``: happy / timeout / error; cancel chain =
    ``docker kill <run_id>`` + status="cancelled" completion (Invariant-8).
5.  Scheme-aware deck assembly (``_default_setup_uri`` + boto3 deck upload)
    and scheme-aware run-output reads/COG upload in ``postprocess_flood``.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

import grace2_agent.tools.solver as solver_mod
from grace2_agent.tools.solver import (
    LOCAL_DOCKER_WORKFLOW_NAME,
    SOLVER_BACKEND_LOCAL_DOCKER,
    SolverDispatchError,
    run_solver,
    set_emitter_binding,
    set_runs_bucket,
    set_s3_client,
    solver_backend,
    wait_for_completion,
)
from grace2_contracts.execution import ExecutionHandle, RunResult

# --------------------------------------------------------------------------- #
# Fakes — boto3-shaped S3 client + legacy GCS client + docker PATH shim
# --------------------------------------------------------------------------- #


def _no_such_key(key: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": f"missing {key}"}}, "GetObject"
    )


class FakeS3Client:
    """Dict-backed boto3-shaped fake (kickoff-sanctioned tmpdir/dict seam)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[tuple[str, str]] = []
        self.get_calls: list[tuple[str, str]] = []

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        self.get_calls.append((Bucket, Key))
        if (Bucket, Key) not in self.objects:
            raise _no_such_key(Key)
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket: str, Key: str, Body: Any, **_kw: Any) -> dict:  # noqa: N803
        data = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.objects[(Bucket, Key)] = data
        self.put_calls.append((Bucket, Key))
        return {}


class _FakeGCSBlob:
    def __init__(self, payload: bytes | None) -> None:
        self._payload = payload

    def download_as_bytes(self) -> bytes:
        if self._payload is None:
            raise FileNotFoundError("no such blob")
        return self._payload

    def download_to_filename(self, filename: str) -> None:
        if self._payload is None:
            raise FileNotFoundError("no such blob")
        Path(filename).write_bytes(self._payload)


class _FakeGCSBucket:
    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = blobs

    def blob(self, path: str) -> _FakeGCSBlob:
        return _FakeGCSBlob(self._blobs.get(path))


class FakeGCSClient:
    def __init__(self, buckets: dict[str, dict[str, bytes]]) -> None:
        self._buckets = buckets

    def bucket(self, name: str) -> _FakeGCSBucket:
        return _FakeGCSBucket(self._buckets.get(name, {}))


#: PATH-shim fake docker. Behaviors via $GRACE2_FAKE_DOCKER_STATE/behavior:
#: ok (write outputs, exit 0) | fail (stderr, exit 2) | hang (exec sleep 300).
#: ``docker kill <name>`` kills the run-mode shim via its pidfile.
_DOCKER_SHIM = r"""#!/usr/bin/env bash
set -u
state_dir="${GRACE2_FAKE_DOCKER_STATE:?GRACE2_FAKE_DOCKER_STATE not set}"
printf '%s\n' "$*" >> "$state_dir/calls.log"
mode="$1"; shift
if [ "$mode" = "kill" ]; then
  name="$1"
  if [ -f "$state_dir/$name.pid" ]; then
    kill -9 "$(cat "$state_dir/$name.pid")" 2>/dev/null || true
  fi
  exit 0
fi
# mode == run: parse --name <name> and -v <src>:/data; ignore --rm/-w.
name=""; vol=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) name="$2"; shift 2;;
    -v) vol="${2%%:*}"; shift 2;;
    --rm) shift;;
    -w) shift 2;;
    *) break;;
  esac
done
echo "$$" > "$state_dir/$name.pid"
behavior="ok"
[ -f "$state_dir/behavior" ] && behavior="$(cat "$state_dir/behavior")"
case "$behavior" in
  ok)
    echo "fake sfincs stdout evidence"
    printf 'NC_MAP_BYTES' > "$vol/sfincs_map.nc"
    printf 'NC_HIS_BYTES' > "$vol/sfincs_his.nc"
    exit 0
    ;;
  fail)
    echo "fake sfincs stderr boom" >&2
    exit 2
    ;;
  hang)
    exec sleep 300
    ;;
esac
"""


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def reset_seams():
    """Reset every solver DI seam + the local-run registry around each test."""
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
def docker_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the fake docker binary at the FRONT of PATH (no real docker —
    the daemon is blocked on this machine per the kickoff hard constraint)."""
    shim_dir = tmp_path / "fake-bin"
    shim_dir.mkdir()
    shim = shim_dir / "docker"
    shim.write_text(_DOCKER_SHIM, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    state_dir = tmp_path / "docker-state"
    state_dir.mkdir()
    monkeypatch.setenv("PATH", f"{shim_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("GRACE2_FAKE_DOCKER_STATE", str(state_dir))
    return state_dir


@pytest.fixture()
def local_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The local-docker env matrix the kickoff names."""
    runs_dir = tmp_path / "runs"
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    monkeypatch.setenv("GRACE2_RUNS_DIR", str(runs_dir))
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.setenv("GRACE2_SFINCS_IMAGE", "fake/sfincs-cpu:test")
    return runs_dir


def _seed_manifest(
    s3: FakeS3Client,
    *,
    bucket: str = "deck-bucket",
    base_key: str = "cache/static-30d/sfincs_setup/TESTDECK/",
    scheme: str = "s3",
    outputs: list[str] | None = None,
) -> str:
    """Seed a worker-contract manifest + its deck inputs into the fake store.

    The input entries use the LEGACY field name ``gs_uri`` — the values carry
    ``{scheme}://`` URIs and must be resolved by scheme (kickoff §1).
    """
    deck = {
        "sfincs.inp": b"[fake sfincs deck]",
        "gis/dep.tif": b"FAKE_DEM_TIF",
    }
    inputs = []
    for rel, payload in deck.items():
        key = f"{base_key}deck/{rel}"
        s3.objects[(bucket, key)] = payload
        inputs.append({"gs_uri": f"{scheme}://{bucket}/{key}", "dest": rel})
    manifest = {
        "inputs": inputs,
        "sfincs_args": [],
        "outputs": outputs if outputs is not None else ["sfincs_map.nc", "*.nc"],
    }
    manifest_key = f"{base_key}manifest.json"
    s3.objects[(bucket, manifest_key)] = json.dumps(manifest).encode()
    return f"s3://{bucket}/{manifest_key}"


def _wait_for_completion_object(
    s3: FakeS3Client, run_id: str, timeout_s: float = 15.0
) -> dict[str, Any]:
    """Block until the supervisor thread writes completion.json (≤ timeout)."""
    deadline = time.monotonic() + timeout_s
    key = (f"test-runs-bucket", f"{run_id}/completion.json")
    while time.monotonic() < deadline:
        if key in s3.objects:
            return json.loads(s3.objects[key])
        time.sleep(0.02)
    raise AssertionError(
        f"supervisor did not write completion.json within {timeout_s}s "
        f"(objects: {sorted(s3.objects)})"
    )


# --------------------------------------------------------------------------- #
# 1. Backend seam default — aws-batch (GCP decommissioned)
# --------------------------------------------------------------------------- #


def test_solver_backend_is_always_local_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The AWS Batch (and legacy GCP) dispatch arms are removed: this build is
    # local-docker-only, so solver_backend() ALWAYS resolves to local-docker
    # regardless of GRACE2_SOLVER_BACKEND (the env read is gone).
    monkeypatch.delenv("GRACE2_SOLVER_BACKEND", raising=False)
    assert solver_backend() == SOLVER_BACKEND_LOCAL_DOCKER
    for val in ("aws-batch", "gcp-workflows", "gcp-workflows-someday", "local-docker"):
        monkeypatch.setenv("GRACE2_SOLVER_BACKEND", val)
        assert solver_backend() == SOLVER_BACKEND_LOCAL_DOCKER


# --------------------------------------------------------------------------- #
# 2. local-docker run_solver — staging + detached launch + immediate handle
# --------------------------------------------------------------------------- #


def test_local_run_solver_requires_runs_bucket(
    reset_seams, local_env, docker_shim, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No GCP-named default on AWS: a missing GRACE2_RUNS_BUCKET fails loudly."""
    monkeypatch.delenv("GRACE2_RUNS_BUCKET", raising=False)
    s3 = FakeS3Client()
    set_s3_client(s3)
    uri = _seed_manifest(s3)
    with pytest.raises(SolverDispatchError) as exc_info:
        run_solver(solver="sfincs", model_setup_uri=uri)
    assert "GRACE2_RUNS_BUCKET" in str(exc_info.value)


def test_local_run_solver_rejects_plain_path(reset_seams, local_env, docker_shim) -> None:
    set_s3_client(FakeS3Client())
    with pytest.raises(SolverDispatchError):
        run_solver(solver="sfincs", model_setup_uri="/tmp/manifest.json")


def test_local_run_solver_stages_manifest_and_launches_docker(
    reset_seams, local_env: Path, docker_shim: Path
) -> None:
    """The headline: manifest read from S3 (boto3 seam), every ``inputs[]``
    object staged into ``$GRACE2_RUNS_DIR/<run_id>/`` (legacy ``gs_uri``
    field name, s3:// VALUES resolved by scheme), docker launched detached
    with the kickoff argv shape, ExecutionHandle returned immediately."""
    s3 = FakeS3Client()
    set_s3_client(s3)
    uri = _seed_manifest(s3)

    handle = run_solver(solver="sfincs", model_setup_uri=uri, compute_class="medium")

    # Typed handle, local-backend pinned, container name == run_id (cancel seam).
    assert isinstance(handle, ExecutionHandle)
    assert handle.solver == "sfincs"
    assert handle.compute_class == "standard"  # medium → standard alias
    assert handle.workflow_name == LOCAL_DOCKER_WORKFLOW_NAME
    assert handle.workflow_location == "local"
    assert handle.workflows_execution_id == f"local-docker:{handle.run_id}"

    # Inputs staged into the rundir — including the gis/ subdirectory entry.
    rundir = local_env / handle.run_id
    assert (rundir / "sfincs.inp").read_bytes() == b"[fake sfincs deck]"
    assert (rundir / "gis" / "dep.tif").read_bytes() == b"FAKE_DEM_TIF"

    # Let the detached shim + supervisor finish (also guards thread leak),
    # THEN assert the recorded docker argv — the Popen is asynchronous, so
    # calls.log only exists once the shim has actually executed.
    _wait_for_completion_object(s3, handle.run_id)

    # Docker argv: docker run --rm --name <run_id> -v <rundir>:/data -w /data <image>
    calls = (docker_shim / "calls.log").read_text().strip().splitlines()
    run_calls = [c for c in calls if c.startswith("run ")]
    assert len(run_calls) == 1, calls
    assert run_calls[0] == (
        f"run --rm --name {handle.run_id} -v {rundir}:/data -w /data "
        "fake/sfincs-cpu:test"
    ), run_calls[0]


def test_local_manifest_gs_uri_field_with_gs_scheme_rejected(
    reset_seams, local_env: Path, docker_shim: Path
) -> None:
    """GCP decommissioned: a legacy ``gs://`` value in the ``gs_uri`` field is
    no longer resolvable (the GCS staging fallback is removed) — staging the
    deck fails with a typed ``SolverDispatchError`` (unsupported scheme)."""
    s3 = FakeS3Client()
    set_s3_client(s3)
    manifest = {
        "inputs": [
            {"gs_uri": "gs://legacy-gcs-bucket/deck/sfincs.inp", "dest": "sfincs.inp"}
        ],
        "sfincs_args": [],
        "outputs": ["*.nc"],
    }
    s3.objects[("deck-bucket", "mixed/manifest.json")] = json.dumps(manifest).encode()

    with pytest.raises(SolverDispatchError):
        run_solver(
            solver="sfincs", model_setup_uri="s3://deck-bucket/mixed/manifest.json"
        )


def test_local_manifest_dest_traversal_rejected(
    reset_seams, local_env: Path, docker_shim: Path
) -> None:
    s3 = FakeS3Client()
    set_s3_client(s3)
    s3.objects[("deck-bucket", "evil/x")] = b"x"
    manifest = {
        "inputs": [{"gs_uri": "s3://deck-bucket/evil/x", "dest": "../../escape.txt"}],
        "sfincs_args": [],
        "outputs": [],
    }
    s3.objects[("deck-bucket", "evil/manifest.json")] = json.dumps(manifest).encode()
    with pytest.raises(SolverDispatchError) as exc_info:
        run_solver(solver="sfincs", model_setup_uri="s3://deck-bucket/evil/manifest.json")
    assert "escape" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# 3+4. Supervisor completion.json (entrypoint schema) + wait_for_completion
# --------------------------------------------------------------------------- #

#: The EXACT key set services/workers/sfincs/entrypoint.py writes.
_ENTRYPOINT_COMPLETION_KEYS = {
    "run_id",
    "status",
    "exit_code",
    "sfincs_stdout_uri",
    "sfincs_stderr_uri",
    "output_uris",
    "started_at",
    "finished_at",
    "error",
}


@pytest.mark.asyncio
async def test_local_ok_path_completion_schema_outputs_and_wait(
    reset_seams, local_env: Path, docker_shim: Path
) -> None:
    s3 = FakeS3Client()
    set_s3_client(s3)
    uri = _seed_manifest(s3)

    handle = run_solver(solver="sfincs", model_setup_uri=uri)
    result = await wait_for_completion(handle, poll_interval_s=0)

    # RunResult: complete; output_uri is the runs PREFIX (kickoff-pinned).
    assert isinstance(result, RunResult)
    assert result.status == "complete"
    assert result.output_uri == f"s3://test-runs-bucket/{handle.run_id}/"
    assert result.run_id == handle.run_id
    assert result.handle_id == handle.handle_id

    # completion.json — EXACT entrypoint.py schema.
    completion = json.loads(
        s3.objects[("test-runs-bucket", f"{handle.run_id}/completion.json")]
    )
    assert set(completion.keys()) == _ENTRYPOINT_COMPLETION_KEYS
    assert completion["run_id"] == handle.run_id
    assert completion["status"] == "ok"
    assert completion["exit_code"] == 0
    assert completion["error"] is None
    assert completion["started_at"].endswith("Z")
    assert completion["finished_at"].endswith("Z")

    # outputs[] glob expansion uploaded (de-duplicated across the 2 patterns)
    # + stdout/stderr evidence uploaded alongside.
    expected_outputs = {
        f"s3://test-runs-bucket/{handle.run_id}/sfincs_map.nc",
        f"s3://test-runs-bucket/{handle.run_id}/sfincs_his.nc",
    }
    assert set(completion["output_uris"]) == expected_outputs
    assert completion["sfincs_stdout_uri"] == (
        f"s3://test-runs-bucket/{handle.run_id}/sfincs.stdout"
    )
    assert completion["sfincs_stderr_uri"] == (
        f"s3://test-runs-bucket/{handle.run_id}/sfincs.stderr"
    )
    assert (
        s3.objects[("test-runs-bucket", f"{handle.run_id}/sfincs_map.nc")]
        == b"NC_MAP_BYTES"
    )
    assert b"stdout evidence" in s3.objects[
        ("test-runs-bucket", f"{handle.run_id}/sfincs.stdout")
    ]


@pytest.mark.asyncio
async def test_local_error_path_always_writes_completion(
    reset_seams, local_env: Path, docker_shim: Path
) -> None:
    """Container crash (exit 2) → completion.json is STILL written
    (status="error", entrypoint parity) and wait surfaces SOLVER_FAILED."""
    (docker_shim / "behavior").write_text("fail")
    s3 = FakeS3Client()
    set_s3_client(s3)
    uri = _seed_manifest(s3)

    handle = run_solver(solver="sfincs", model_setup_uri=uri)
    result = await wait_for_completion(handle, poll_interval_s=0)

    assert result.status == "failed"
    assert result.error_code == "SOLVER_FAILED"
    assert result.error_message is not None
    assert "non-zero code 2" in result.error_message

    completion = json.loads(
        s3.objects[("test-runs-bucket", f"{handle.run_id}/completion.json")]
    )
    assert set(completion.keys()) == _ENTRYPOINT_COMPLETION_KEYS
    assert completion["status"] == "error"
    assert completion["exit_code"] == 2
    # stderr evidence still uploaded on the error path.
    assert b"stderr boom" in s3.objects[
        ("test-runs-bucket", f"{handle.run_id}/sfincs.stderr")
    ]


@pytest.mark.asyncio
async def test_local_cancel_chain_docker_kill_plus_cancelled_completion(
    reset_seams, local_env: Path, docker_shim: Path
) -> None:
    """Invariant-8: cancelling the wait coroutine issues ``docker kill
    <run_id>`` and the supervisor writes the status="cancelled" completion —
    all well inside the ≤30 s budget — then CancelledError re-raises."""
    (docker_shim / "behavior").write_text("hang")
    s3 = FakeS3Client()
    set_s3_client(s3)
    uri = _seed_manifest(s3)

    handle = run_solver(solver="sfincs", model_setup_uri=uri)
    run = solver_mod._LOCAL_RUNS[handle.run_id]  # grab before the pop

    cancel_started = time.monotonic()
    task = asyncio.create_task(wait_for_completion(handle, poll_interval_s=0))
    await asyncio.sleep(0.2)  # let the poll loop start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # docker kill was issued against the container name (== run_id).
    calls = (docker_shim / "calls.log").read_text()
    assert f"kill {handle.run_id}" in calls

    # The supervisor wakes on the killed process and writes the cancelled
    # completion; total cancel-to-terminal well under the 30 s NFR-R-3 budget.
    completion = _wait_for_completion_object(s3, handle.run_id, timeout_s=15.0)
    elapsed = time.monotonic() - cancel_started
    assert elapsed < 30.0, f"cancel chain took {elapsed:.1f}s (NFR-R-3 budget is 30s)"
    assert set(completion.keys()) == _ENTRYPOINT_COMPLETION_KEYS
    assert completion["status"] == "cancelled"
    run.supervisor.join(timeout=5.0)

    # A fresh wait on the same handle maps the cancelled completion to a
    # RunResult{status="cancelled"} (post-cancel observability).
    result = await wait_for_completion(handle, poll_interval_s=0)
    assert result.status == "cancelled"
    assert result.cancellation_reason


@pytest.mark.asyncio
async def test_local_wait_timeout_returns_solver_timeout_and_kills(
    reset_seams, local_env: Path, docker_shim: Path
) -> None:
    (docker_shim / "behavior").write_text("hang")
    s3 = FakeS3Client()
    set_s3_client(s3)
    uri = _seed_manifest(s3)

    handle = run_solver(solver="sfincs", model_setup_uri=uri)
    run = solver_mod._LOCAL_RUNS[handle.run_id]
    result = await wait_for_completion(handle, poll_interval_s=0, timeout_s=1)

    assert result.status == "failed"
    assert result.error_code == "SOLVER_TIMEOUT"
    assert "completion.json" in (result.error_message or "")
    # Timeout best-effort kills the container (mirrors the GCP cancel).
    calls = (docker_shim / "calls.log").read_text()
    assert f"kill {handle.run_id}" in calls
    # Timeout ≠ user cancel: the supervisor records error, not cancelled.
    completion = _wait_for_completion_object(s3, handle.run_id, timeout_s=15.0)
    assert completion["status"] == "error"
    run.supervisor.join(timeout=5.0)


@pytest.mark.asyncio
async def test_local_wait_emits_progress_via_emitter_binding(
    reset_seams, local_env: Path, docker_shim: Path
) -> None:
    """The local poll keeps the GCP path's progress-emission semantics."""

    class _CapturingEmitter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        async def update_progress(self, step_id: str, pct: int) -> None:
            self.calls.append((step_id, pct))

    s3 = FakeS3Client()
    set_s3_client(s3)
    uri = _seed_manifest(s3)
    emitter = _CapturingEmitter()
    set_emitter_binding(solver_mod.EmitterBinding(emitter=emitter, step_id="s1"))

    handle = run_solver(solver="sfincs", model_setup_uri=uri)
    result = await wait_for_completion(handle, poll_interval_s=0)

    assert result.status == "complete"
    assert emitter.calls, "no progress emissions on the local poll"
    assert emitter.calls[-1] == ("s1", solver_mod.PROGRESS_TERMINAL)
    for _sid, pct in emitter.calls[:-1]:
        assert 0 <= pct <= solver_mod.PROGRESS_CLAMP_MAX


# --------------------------------------------------------------------------- #
# 5a. Deck assembly — scheme-aware setup URI + boto3 deck upload (job-0291 §2)
# --------------------------------------------------------------------------- #


def test_default_setup_uri_is_scheme_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    from grace2_agent.workflows.sfincs_builder import _default_setup_uri

    # GCP is decommissioned: the setup URI is always s3:// regardless of any
    # GRACE2_STORAGE_BACKEND override (the gs legacy seam is gone).
    monkeypatch.delenv("GRACE2_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("GRACE2_CACHE_BUCKET", "cache-bkt")
    assert _default_setup_uri((0, 0, 1, 1)).startswith(
        "s3://cache-bkt/cache/static-30d/sfincs_setup/"
    )
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    uri = _default_setup_uri((0, 0, 1, 1))
    assert uri.startswith("s3://cache-bkt/cache/static-30d/sfincs_setup/")
    assert uri.endswith("/manifest.json")
    # A stray legacy override no longer resurrects gs:// — S3-only.
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "gcs")
    assert _default_setup_uri((0, 0, 1, 1)).startswith(
        "s3://cache-bkt/cache/static-30d/sfincs_setup/"
    )


def test_build_sfincs_model_uploads_deck_via_boto3_under_s3(
    reset_seams, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under GRACE2_STORAGE_BACKEND=s3 the deck + manifest upload goes via
    boto3 (NOT fsspec/s3fs) and the manifest's legacy-named ``gs_uri``
    fields carry ``s3://.../deck/...`` values the local-docker staging can
    resolve by scheme."""
    from grace2_agent.workflows.sfincs_builder import (
        BuildOptions,
        ForcingSpec,
        build_sfincs_model,
    )

    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    s3 = FakeS3Client()
    set_s3_client(s3)

    mapping_path = tmp_path / "manning.csv"
    mapping_path.write_text(
        "nlcd_class,manning_n,description\n11,0.025,Open Water\n", encoding="utf-8"
    )

    class _FakeSfincsModel:
        def __init__(self, root: str, mode: str) -> None:
            self._root = root

        def build(self, opt: Any) -> None:
            deck_dir = Path(self._root)
            deck_dir.mkdir(parents=True, exist_ok=True)
            (deck_dir / "sfincs.inp").write_text("[fake]", encoding="utf-8")
            (deck_dir / "gis").mkdir(exist_ok=True)
            (deck_dir / "gis" / "dep.tif").write_bytes(b"TIF")

        def write(self) -> None:
            pass

    fake_module = MagicMock()
    fake_module.SfincsModel = _FakeSfincsModel
    # fsspec must NOT be touched on the s3 branch — make it explode if it is.
    angry_fsspec = MagicMock()
    angry_fsspec.filesystem.side_effect = AssertionError(
        "fsspec used for an s3:// deck upload — job-0291 requires boto3"
    )

    fixed_manifest_uri = (
        "s3://deck-bucket/cache/static-30d/sfincs_setup/S3DECK01/manifest.json"
    )
    forcing = ForcingSpec(
        forcing_type="pluvial_synthetic",
        precip_inches=10.0,
        duration_hours=24.0,
        return_period_years=100,
        provenance={},
    )
    with (
        patch.dict(
            "sys.modules",
            {"hydromt_sfincs": fake_module, "fsspec": angry_fsspec},
            clear=False,
        ),
        patch(
            "grace2_agent.workflows.sfincs_builder._extract_unique_nlcd_classes",
            return_value={11},
        ),
        patch(
            "grace2_agent.workflows.sfincs_builder._stage_gcs_local",
            side_effect=lambda uri: uri,
        ),
        patch(
            "grace2_agent.workflows.sfincs_builder._default_setup_uri",
            return_value=fixed_manifest_uri,
        ),
    ):
        setup = build_sfincs_model(
            dem_uri="s3://test/dem.tif",
            landcover_uri="s3://test/landcover.tif",
            river_geometry_uri=None,
            forcing=forcing,
            bbox=(-81.92, 26.55, -81.80, 26.68),
            options=BuildOptions(grid_resolution_m=30.0, simulation_hours=24.0),
            nlcd_vintage_year=2021,
            manning_mapping_csv=mapping_path,
        )

    assert setup.setup_uri == fixed_manifest_uri

    base_key = "cache/static-30d/sfincs_setup/S3DECK01/"
    manifest = json.loads(s3.objects[("deck-bucket", f"{base_key}manifest.json")])
    assert manifest["outputs"], manifest
    assert manifest["inputs"], manifest
    dests = {e["dest"] for e in manifest["inputs"]}
    assert "sfincs.inp" in dests
    assert "gis/dep.tif" in dests
    for entry in manifest["inputs"]:
        # Legacy field NAME, s3:// VALUE under the deck/ sub-prefix — and the
        # object the URI names actually exists in the store (staging-ready).
        assert entry["gs_uri"].startswith(f"s3://deck-bucket/{base_key}deck/"), entry
        _, _, key = entry["gs_uri"][len("s3://"):].partition("/")
        assert ("deck-bucket", key) in s3.objects, f"manifest cites missing {key}"


def test_stage_gcs_local_handles_s3_uri(reset_seams) -> None:
    """HydroMT catalog staging resolves s3:// via the boto3 seam (job-0291)."""
    import uuid

    from grace2_agent.workflows.sfincs_builder import _stage_gcs_local

    s3 = FakeS3Client()
    set_s3_client(s3)
    key = f"stage-test/{uuid.uuid4().hex}/dem.tif"
    s3.objects[("stage-bkt", key)] = b"DEM_BYTES"
    local = _stage_gcs_local(f"s3://stage-bkt/{key}")
    assert Path(local).read_bytes() == b"DEM_BYTES"
    # Second call hits the content-keyed cache (no second get_object).
    gets_before = len(s3.get_calls)
    assert _stage_gcs_local(f"s3://stage-bkt/{key}") == local
    assert len(s3.get_calls) == gets_before


def test_to_vsigs_maps_s3_to_vsis3() -> None:
    from grace2_agent.workflows.sfincs_builder import _to_vsigs

    assert _to_vsigs("s3://bkt/key.tif") == "/vsis3/bkt/key.tif"
    assert _to_vsigs("/vsis3/bkt/key.tif") == "/vsis3/bkt/key.tif"
    # GCP decommissioned: gs:// is no longer special-cased — passed through as a
    # local path (the resolver layer is the gate).
    assert _to_vsigs("gs://bkt/key.tif") == "gs://bkt/key.tif"


# --------------------------------------------------------------------------- #
# 5b. postprocess_flood — scheme-aware run-output read + COG upload (§3)
# --------------------------------------------------------------------------- #


def test_postprocess_resolves_s3_run_output_via_boto3(reset_seams) -> None:
    from grace2_agent.workflows.postprocess_flood import _resolve_run_output_to_local

    s3 = FakeS3Client()
    set_s3_client(s3)
    s3.objects[("test-runs-bucket", "RUNX/sfincs_map.nc")] = b"NETCDF_BYTES"
    local = _resolve_run_output_to_local("s3://test-runs-bucket/RUNX/")
    assert local.name == "sfincs_map.nc"
    assert local.read_bytes() == b"NETCDF_BYTES"
    # Direct-file form too.
    local2 = _resolve_run_output_to_local("s3://test-runs-bucket/RUNX/sfincs_map.nc")
    assert local2.read_bytes() == b"NETCDF_BYTES"


def test_postprocess_s3_read_failure_is_typed(reset_seams) -> None:
    from grace2_agent.workflows.postprocess_flood import (
        PostprocessError,
        _resolve_run_output_to_local,
    )

    set_s3_client(FakeS3Client())
    with pytest.raises(PostprocessError) as exc_info:
        _resolve_run_output_to_local("s3://test-runs-bucket/MISSING/")
    assert exc_info.value.error_code == "RUN_OUTPUT_READ_FAILED"


def test_postprocess_cog_upload_scheme_aware(
    reset_seams, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grace2_agent.workflows.postprocess_flood import (
        PostprocessError,
        _upload_cog_to_runs_bucket,
    )

    cog = tmp_path / "flood.tif"
    cog.write_bytes(b"COG_BYTES")
    s3 = FakeS3Client()
    set_s3_client(s3)

    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    dest = _upload_cog_to_runs_bucket(cog, "RUNX")
    assert dest == "s3://test-runs-bucket/RUNX/flood_depth_peak.tif"
    assert s3.objects[("test-runs-bucket", "RUNX/flood_depth_peak.tif")] == b"COG_BYTES"

    # No GCP-named default on AWS: missing bucket env is a typed failure.
    monkeypatch.delenv("GRACE2_RUNS_BUCKET", raising=False)
    with pytest.raises(PostprocessError) as exc_info:
        _upload_cog_to_runs_bucket(cog, "RUNX")
    assert exc_info.value.error_code == "COG_UPLOAD_FAILED"
    assert "GRACE2_RUNS_BUCKET" in str(exc_info.value)


def test_composer_default_runs_prefix_scheme_aware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from grace2_agent.workflows.model_flood_scenario import _default_runs_prefix

    monkeypatch.delenv("GRACE2_STORAGE_BACKEND", raising=False)
    assert _default_runs_prefix("R1") == "gs://grace-2-hazard-prod-runs/R1/"
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    assert _default_runs_prefix("R1") == "s3://test-runs-bucket/R1/"
