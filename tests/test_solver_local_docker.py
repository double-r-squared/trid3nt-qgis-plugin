"""The local-docker solver backend: staging, launch, supervisor, completion.

The envelope is engine-AGNOSTIC - what varies per engine is the ``LocalSolverSpec``
its own workflow module registers. These tests drive it through the TELEMAC
river-dye spec, the one registered local-docker solver whose behaviour the rest of
the suite also depends on, so a spec-driven field (``telemac_args``,
``telemac_stdout_uri``, the ``--network none`` engine-room posture) is exercised
as a real registration rather than a fixture invention.

Hard constraint honored here: **NO real docker invocation on this machine**
(the daemon is blocked). Every ``docker`` call resolves to a PATH-shim bash
script that records its argv, emulates the container behaviors (ok / fail /
hang), and supports ``docker kill`` against the run-mode shim's pidfile.
All S3 I/O goes through the ``tools.simulation.solver.set_s3_client`` seam with a
dict-backed fake (boto3-shaped ``get_object``/``put_object``).

Coverage maps to the kickoff §4 test list:

1.  Default env → backend is gcp-workflows; the Cloud Workflows path stays
    byte-identical (the full pre-existing ``test_solver.py`` suite is the
    primary guard; the explicit default assertion lives here).
2.  local-docker ``run_solver``: manifest staged from S3 (legacy ``gs_uri``
    field name carrying ``s3://`` VALUES — resolved by scheme), docker
    launched detached with ``--rm --name <run_id> -v <rundir>:/data -w
    /data $TRID3NT_TELEMAC_IMAGE``, ExecutionHandle returned immediately.
3.  Supervisor writes the EXACT entrypoint.py completion.json schema —
    ok, error, and cancel paths — and uploads outputs + stdout/stderr.
4.  ``wait_for_completion``: happy / timeout / error; cancel chain =
    ``docker kill <run_id>`` + status="cancelled" completion (Invariant-8).
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

import trid3nt_server.workflows.solver.solver as solver_mod
from trid3nt_server.workflows.solver.solver import (
    LOCAL_DOCKER_WORKFLOW_NAME,
    SolverDispatchError,
    run_solver,
    set_emitter_binding,
    set_runs_bucket,
    set_s3_client,
    wait_for_completion,
)
from trid3nt_contracts.execution import ExecutionHandle, RunResult

#: The one registered local-docker solver; the envelope under test is its spec's
#: host, not its engine.
_SOLVER = "telemac_river_dye"

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

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise _no_such_key(Key)
        return {"ContentLength": len(self.objects[(Bucket, Key)])}


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


#: PATH-shim fake docker. Behaviors via $TRID3NT_FAKE_DOCKER_STATE/behavior:
#: ok (write outputs, exit 0) | fail (stderr, exit 2) | hang (exec sleep 300).
#: ``docker kill <name>`` kills the run-mode shim via its pidfile.
_DOCKER_SHIM = r"""#!/usr/bin/env bash
set -u
state_dir="${TRID3NT_FAKE_DOCKER_STATE:?TRID3NT_FAKE_DOCKER_STATE not set}"
printf '%s\n' "$*" >> "$state_dir/calls.log"
mode="$1"; shift
if [ "$mode" = "kill" ]; then
  name="$1"
  if [ -f "$state_dir/$name.pid" ]; then
    kill -9 "$(cat "$state_dir/$name.pid")" 2>/dev/null || true
  fi
  exit 0
fi
# mode == run: parse --name <name> and -v <src>:/data; ignore --rm/-w/--network.
name=""; vol=""
while [ $# -gt 0 ]; do
  case "$1" in
    --name) name="$2"; shift 2;;
    -v) vol="${2%%:*}"; shift 2;;
    --network) shift 2;;
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
    echo "fake telemac stdout evidence"
    printf 'SLF_RESULT_BYTES' > "$vol/r2d_river.slf"
    printf 'SLF_GEOMETRY_BYTES' > "$vol/river.slf"
    printf '{"correct_end": true}' > "$vol/telemac_metrics.json"
    exit 0
    ;;
  fail)
    echo "fake telemac stderr boom" >&2
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
    monkeypatch.setenv("TRID3NT_FAKE_DOCKER_STATE", str(state_dir))
    return state_dir


@pytest.fixture()
def local_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The local-docker env matrix the kickoff names."""
    runs_dir = tmp_path / "runs"
    monkeypatch.setenv("TRID3NT_SOLVER_BACKEND", "local-docker")
    monkeypatch.setenv("TRID3NT_RUNS_DIR", str(runs_dir))
    monkeypatch.setenv("TRID3NT_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.setenv("TRID3NT_TELEMAC_IMAGE", "fake/telemac:test")
    return runs_dir


def _seed_manifest(
    s3: FakeS3Client,
    *,
    bucket: str = "deck-bucket",
    base_key: str = "cache/static-30d/telemac_setup/TESTDECK/",
    scheme: str = "s3",
    outputs: list[str] | None = None,
) -> str:
    """Seed a worker-contract manifest + its deck inputs into the fake store.

    The input entries use the LEGACY field name ``gs_uri`` — the values carry
    ``{scheme}://`` URIs and must be resolved by scheme (kickoff §1).
    """
    deck = {
        "t2d_river.cas": b"[fake telemac deck]",
        "gis/bed.tif": b"FAKE_DEM_TIF",
    }
    inputs = []
    for rel, payload in deck.items():
        key = f"{base_key}deck/{rel}"
        s3.objects[(bucket, key)] = payload
        inputs.append({"gs_uri": f"{scheme}://{bucket}/{key}", "dest": rel})
    manifest = {
        "inputs": inputs,
        "telemac_args": [],
        "outputs": outputs if outputs is not None else ["r2d_river.slf", "*.slf"],
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
# 2. local-docker run_solver — staging + detached launch + immediate handle
# --------------------------------------------------------------------------- #


def test_local_run_solver_requires_runs_bucket(
    reset_seams, local_env, docker_shim, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No GCP-named default on AWS: a missing TRID3NT_RUNS_BUCKET fails loudly."""
    monkeypatch.delenv("TRID3NT_RUNS_BUCKET", raising=False)
    s3 = FakeS3Client()
    set_s3_client(s3)
    uri = _seed_manifest(s3)
    with pytest.raises(SolverDispatchError) as exc_info:
        run_solver(solver=_SOLVER, model_setup_uri=uri)
    assert "TRID3NT_RUNS_BUCKET" in str(exc_info.value)


def test_local_run_solver_rejects_plain_path(reset_seams, local_env, docker_shim) -> None:
    set_s3_client(FakeS3Client())
    with pytest.raises(SolverDispatchError):
        run_solver(solver=_SOLVER, model_setup_uri="/tmp/manifest.json")


def test_local_run_solver_stages_manifest_and_launches_docker(
    reset_seams, local_env: Path, docker_shim: Path
) -> None:
    """The headline: manifest read from S3 (boto3 seam), every ``inputs[]``
    object staged into ``$TRID3NT_RUNS_DIR/<run_id>/`` (legacy ``gs_uri``
    field name, s3:// VALUES resolved by scheme), docker launched detached
    with the kickoff argv shape, ExecutionHandle returned immediately."""
    s3 = FakeS3Client()
    set_s3_client(s3)
    uri = _seed_manifest(s3)

    handle = run_solver(solver=_SOLVER, model_setup_uri=uri, compute_class="medium")

    # Typed handle, local-backend pinned, container name == run_id (cancel seam).
    assert isinstance(handle, ExecutionHandle)
    assert handle.solver == _SOLVER
    assert handle.compute_class == "standard"  # medium → standard alias
    assert handle.workflow_name == LOCAL_DOCKER_WORKFLOW_NAME
    assert handle.workflow_location == "local"
    assert handle.workflows_execution_id == f"local-docker:{handle.run_id}"

    # Inputs staged into the rundir — including the gis/ subdirectory entry.
    rundir = local_env / handle.run_id
    assert (rundir / "t2d_river.cas").read_bytes() == b"[fake telemac deck]"
    assert (rundir / "gis" / "bed.tif").read_bytes() == b"FAKE_DEM_TIF"

    # Let the detached shim + supervisor finish (also guards thread leak),
    # THEN assert the recorded docker argv — the Popen is asynchronous, so
    # calls.log only exists once the shim has actually executed.
    _wait_for_completion_object(s3, handle.run_id)

    # Docker argv: the spec's declared network, then
    # --rm --name <run_id> -v <rundir>:/data -w /data <image>
    calls = (docker_shim / "calls.log").read_text().strip().splitlines()
    run_calls = [c for c in calls if c.startswith("run ")]
    assert len(run_calls) == 1, calls
    assert run_calls[0] == (
        f"run --network none --rm --name {handle.run_id} -v {rundir}:/data "
        "-w /data fake/telemac:test"
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
            {"gs_uri": "gs://legacy-gcs-bucket/deck/t2d.cas", "dest": "t2d.cas"}
        ],
        "telemac_args": [],
        "outputs": ["*.slf"],
    }
    s3.objects[("deck-bucket", "mixed/manifest.json")] = json.dumps(manifest).encode()

    with pytest.raises(SolverDispatchError):
        run_solver(
            solver=_SOLVER, model_setup_uri="s3://deck-bucket/mixed/manifest.json"
        )


def test_local_manifest_dest_traversal_rejected(
    reset_seams, local_env: Path, docker_shim: Path
) -> None:
    s3 = FakeS3Client()
    set_s3_client(s3)
    s3.objects[("deck-bucket", "evil/x")] = b"x"
    manifest = {
        "inputs": [{"gs_uri": "s3://deck-bucket/evil/x", "dest": "../../escape.txt"}],
        "telemac_args": [],
        "outputs": [],
    }
    s3.objects[("deck-bucket", "evil/manifest.json")] = json.dumps(manifest).encode()
    with pytest.raises(SolverDispatchError) as exc_info:
        run_solver(solver=_SOLVER, model_setup_uri="s3://deck-bucket/evil/manifest.json")
    assert "escape" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# 3+4. Supervisor completion.json (entrypoint schema) + wait_for_completion
# --------------------------------------------------------------------------- #

#: The EXACT key set the local supervisor writes. The worker-entrypoint schema
#: PLUS two agent-side additions: the ``solver``
#: engine-identity field (ADR 0021) so read_run_diagnostics can recover the
#: engine directly instead of inferring it from the stdout field name, and the
#: ``code_sha`` / ``code_dirty`` stamp (ADR 0317) so a reader of this artifact
#: can ask whether the engine has changed since it ran. Both are written by the
#: supervisor, never by a worker, so a worker-written completion.json lacks them
#: and the readers of both fall back rather than requiring them.
_ENTRYPOINT_COMPLETION_KEYS = {
    "run_id",
    "status",
    "exit_code",
    "solver",
    "code_sha",
    "code_dirty",
    "telemac_stdout_uri",
    "telemac_stderr_uri",
    "output_uris",
    "started_at",
    "finished_at",
    "error",
}

#: What the TELEMAC spec's own ``classify_exit`` folds in on top. Named here
#: rather than absorbed into the set above: the schema a supervisor writes and
#: the extras a SPEC contributes are two different facts.
_SPEC_COMPLETION_EXTRAS = {"correct_end"}


@pytest.mark.asyncio
async def test_local_ok_path_completion_schema_outputs_and_wait(
    reset_seams, local_env: Path, docker_shim: Path
) -> None:
    s3 = FakeS3Client()
    set_s3_client(s3)
    uri = _seed_manifest(s3)

    handle = run_solver(solver=_SOLVER, model_setup_uri=uri)
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
    assert set(completion.keys()) == (
        _ENTRYPOINT_COMPLETION_KEYS | _SPEC_COMPLETION_EXTRAS)
    assert completion["run_id"] == handle.run_id
    assert completion["status"] == "ok"
    assert completion["exit_code"] == 0
    assert completion["error"] is None
    assert completion["started_at"].endswith("Z")
    assert completion["finished_at"].endswith("Z")

    # outputs[] glob expansion uploaded (de-duplicated across the 2 patterns)
    # + stdout/stderr evidence uploaded alongside.
    expected_outputs = {
        f"s3://test-runs-bucket/{handle.run_id}/r2d_river.slf",
        f"s3://test-runs-bucket/{handle.run_id}/river.slf",
    }
    assert set(completion["output_uris"]) == expected_outputs
    assert completion["telemac_stdout_uri"] == (
        f"s3://test-runs-bucket/{handle.run_id}/telemac.stdout"
    )
    assert completion["telemac_stderr_uri"] == (
        f"s3://test-runs-bucket/{handle.run_id}/telemac.stderr"
    )
    assert (
        s3.objects[("test-runs-bucket", f"{handle.run_id}/r2d_river.slf")]
        == b"SLF_RESULT_BYTES"
    )
    assert b"stdout evidence" in s3.objects[
        ("test-runs-bucket", f"{handle.run_id}/telemac.stdout")
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

    handle = run_solver(solver=_SOLVER, model_setup_uri=uri)
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
        ("test-runs-bucket", f"{handle.run_id}/telemac.stderr")
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

    handle = run_solver(solver=_SOLVER, model_setup_uri=uri)
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

    handle = run_solver(solver=_SOLVER, model_setup_uri=uri)
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

    handle = run_solver(solver=_SOLVER, model_setup_uri=uri)
    result = await wait_for_completion(handle, poll_interval_s=0)

    assert result.status == "complete"
    assert emitter.calls, "no progress emissions on the local poll"
    assert emitter.calls[-1] == ("s1", solver_mod.PROGRESS_TERMINAL)
    for _sid, pct in emitter.calls[:-1]:
        assert 0 <= pct <= solver_mod.PROGRESS_CLAMP_MAX



# --------------------------------------------------------------------------- #
# publish_manifest_uri survives the supervisor's completion write
# --------------------------------------------------------------------------- #


def _write_completion(s3: FakeS3Client, run_id: str) -> dict:
    solver_mod._write_local_completion(
        s3,
        runs_bucket="test-runs-bucket",
        run_id=run_id,
        status="ok",
        exit_code=0,
        output_uris=[],
        stdout_uri=None,
        stderr_uri=None,
        started_at="2026-08-20T00:00:00Z",
        error=None,
        solver="telemac_river_dye",
    )
    return json.loads(s3.objects[("test-runs-bucket", f"{run_id}/completion.json")])


def test_completion_carries_publish_manifest_uri_when_worker_wrote_one() -> None:
    """A self-S3 worker writes publish_manifest.json under the run prefix and its
    own completion.json; the supervisor's write lands LAST and overwrites it. The
    manifest POINTER must survive -- read_publish_manifest requires it and never
    globs, so losing it strips every consumer's metrics carrier."""
    s3 = FakeS3Client()
    run_id = "RID-WITH-MANIFEST"
    s3.objects[("test-runs-bucket", f"{run_id}/publish_manifest.json")] = (
        b'{"schema_version": 1, "engine": "telemac", "layers": []}'
    )

    completion = _write_completion(s3, run_id)

    assert completion["publish_manifest_uri"] == (
        f"s3://test-runs-bucket/{run_id}/publish_manifest.json"
    )


def test_completion_omits_publish_manifest_uri_when_absent() -> None:
    """No worker manifest under the run prefix -> no invented pointer (the
    mounted-rundir specs write no publish_manifest at all)."""
    s3 = FakeS3Client()
    completion = _write_completion(s3, "RID-NO-MANIFEST")

    assert "publish_manifest_uri" not in completion


def test_spec_supplied_publish_manifest_uri_is_not_clobbered() -> None:
    """A classify_exit that already resolved the pointer wins over the probe."""
    s3 = FakeS3Client()
    run_id = "RID-SPEC-WINS"
    s3.objects[("test-runs-bucket", f"{run_id}/publish_manifest.json")] = b"{}"

    solver_mod._write_local_completion(
        s3,
        runs_bucket="test-runs-bucket",
        run_id=run_id,
        status="ok",
        exit_code=0,
        output_uris=[],
        stdout_uri=None,
        stderr_uri=None,
        started_at="2026-08-20T00:00:00Z",
        error=None,
        extra={"publish_manifest_uri": "s3://elsewhere/manifest.json"},
        solver="telemac_river_dye",
    )
    completion = json.loads(
        s3.objects[("test-runs-bucket", f"{run_id}/completion.json")]
    )
    assert completion["publish_manifest_uri"] == "s3://elsewhere/manifest.json"
