"""job-0292b (sprint-14-aws) — MODFLOW local backend tests.

MODFLOW routed through the job-0291 ``GRACE2_SOLVER_BACKEND=local-docker``
seam, as an **image-less local-exec** spec over the shared
``tools.solver.launch_local_solver`` machinery: stage the deck from S3
(boto3), run the ``mf6`` binary detached (no public MODFLOW image exists —
the instance carries the SHA-pinned USGS 6.5.0 static binary the GCP
Dockerfile installs), supervisor uploads outputs + the EXACT
``services/workers/modflow/entrypoint.py`` completion.json to
``s3://$GRACE2_RUNS_BUCKET/<run_id>/``.

Hard constraints honored here (kickoff): **NO docker / NO real mf6 on this
machine** — the ``mf6`` binary is a PATH-shim bash script (behaviors
ok / diverge / fail / hang via a state file), and ALL S3 I/O goes through the
``tools.solver.set_s3_client`` seam with a dict-backed boto3-shaped fake.
NO Gemini/Vertex/Bedrock anywhere.

Coverage maps to the kickoff §4 test list (mirrors job-0291's
``test_solver_local_docker.py``):

1.  Default env → ``submit_modflow_run`` keeps the Cloud Workflows path
    byte-identical (the pre-existing ``test_run_modflow.py`` suite is the
    primary guard; the explicit inverse assertion lives here: under the
    local backend the workflows client is NEVER touched).
2.  Local submit: deck staged from the fake S3 (legacy ``gs_uri`` field NAME
    with ``s3://`` VALUES, ``gwf/``+``gwt/`` subdir layout reconstructed),
    ``mf6`` launched detached with cwd == rundir, ``ExecutionHandle``
    returned immediately with ``workflow_name="local-exec"`` and the
    deck-staging ``run_id`` passed through (GCP ``{run_id, manifest_uri}``
    parity).
3.  Supervisor completion.json — EXACT MODFLOW entrypoint key set
    (``converged`` / ``model_crs`` / ``mf6_stdout_uri`` / ``mf6_stderr_uri``)
    for ok / diverged (exit-0-but-list-file, the design-doc §8 override) /
    crash / cancel.
4.  ``wait_for_completion``: happy / error / timeout; Invariant-8 cancel =
    process-group kill (exec kind — no container to ``docker kill``) +
    status="cancelled" completion ≤30 s.
5.  Scheme-aware deck assembly (boto3, fsspec booby-trapped) + scheme-aware
    plume postprocess (UCN read via boto3, COG upload via boto3, publish
    through the job-0290 TiTiler template path), and a full
    ``run_modflow_job`` E2E through the REAL code paths (real FloPy deck,
    real flopy UCN read, real rasterio reprojection; fake mf6 + fake S3).
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

import numpy as np
import pytest
from botocore.exceptions import ClientError

import grace2_agent.tools.solver as solver_mod
import grace2_agent.workflows.postprocess_modflow as pp
import grace2_agent.workflows.run_modflow as rm
from grace2_agent.tools.solver import (
    LOCAL_EXEC_WORKFLOW_NAME,
    set_emitter_binding,
    set_runs_bucket,
    set_s3_client,
    wait_for_completion,
)
from grace2_contracts import new_ulid
from grace2_contracts.execution import ExecutionHandle, RunResult

_HAVE_FLOPY = True
try:  # flopy backs the deck build + the UCN read in the E2E tests
    import flopy  # type: ignore[import-not-found]  # noqa: F401
except Exception:  # noqa: BLE001
    _HAVE_FLOPY = False


# --------------------------------------------------------------------------- #
# Fakes — boto3-shaped S3 client + the mf6 PATH shim
# --------------------------------------------------------------------------- #


def _no_such_key(key: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "NoSuchKey", "Message": f"missing {key}"}}, "GetObject"
    )


class FakeS3Client:
    """Dict-backed boto3-shaped fake (the job-0291 kickoff-sanctioned seam)."""

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


#: PATH-shim fake mf6. Behaviors via $GRACE2_FAKE_MF6_STATE/behavior:
#: ok (write outputs + Normal-termination mfsim.lst, exit 0) | diverge
#: (exit 0 BUT convergence-failure marker in mfsim.lst — the design-doc §8
#: list-file-authoritative case) | fail (stderr, exit 3) | hang (exec sleep).
#: A pre-seeded $state_dir/gwt_model.ucn (real flopy-readable bytes) is
#: copied into the rundir when present so the E2E postprocess reads a real
#: concentration grid.
_MF6_SHIM = r"""#!/usr/bin/env bash
set -u
state_dir="${GRACE2_FAKE_MF6_STATE:?GRACE2_FAKE_MF6_STATE not set}"
printf 'mf6 %s\n' "$*" >> "$state_dir/calls.log"
pwd >> "$state_dir/cwd.log"
behavior="ok"
[ -f "$state_dir/behavior" ] && behavior="$(cat "$state_dir/behavior")"
case "$behavior" in
  ok)
    echo "fake mf6 stdout evidence"
    if [ -f "$state_dir/gwt_model.ucn" ]; then
      cp "$state_dir/gwt_model.ucn" gwt_model.ucn
    else
      printf 'UCN_BYTES' > gwt_model.ucn
    fi
    printf 'HDS_BYTES' > gwf_model.hds
    printf 'Normal termination of simulation\n' > mfsim.lst
    exit 0
    ;;
  diverge)
    printf 'FAILED TO MEET SOLVER CONVERGENCE CRITERIA\n' > mfsim.lst
    exit 0
    ;;
  fail)
    echo "fake mf6 stderr boom" >&2
    exit 3
    ;;
  hang)
    exec sleep 300
    ;;
esac
"""


def _write_synthetic_ucn(path: Path, arr2d: "np.ndarray", totim: float = 86400.0) -> None:
    """Write a flopy-HeadFile-readable double-precision CONCENTRATION record.

    Binary layout per record (MF6 double precision): kstp,kper (i4) |
    pertim,totim (f8) | text (char*16, right-justified) | ncol,nrow,ilay (i4)
    | nrow*ncol f8 values. Verified readable via
    ``flopy.utils.HeadFile(text="CONCENTRATION")`` in this venv.
    """
    nrow, ncol = arr2d.shape
    with path.open("wb") as f:
        np.array([1, 1], dtype="<i4").tofile(f)
        np.array([totim, totim], dtype="<f8").tofile(f)
        f.write("CONCENTRATION".rjust(16).encode("ascii"))
        np.array([ncol, nrow, 1], dtype="<i4").tofile(f)
        arr2d.astype("<f8").tofile(f)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def reset_seams():
    """Reset solver + run_modflow DI seams + the local-run registry."""
    set_s3_client(None)
    set_emitter_binding(None)
    set_runs_bucket(None)
    rm.set_cache_bucket(None)
    rm.set_runs_bucket(None)
    rm.set_mf6_binary(None)
    solver_mod._LOCAL_RUNS.clear()
    try:
        yield
    finally:
        set_s3_client(None)
        set_emitter_binding(None)
        set_runs_bucket(None)
        rm.set_cache_bucket(None)
        rm.set_runs_bucket(None)
        rm.set_mf6_binary(None)
        solver_mod._LOCAL_RUNS.clear()


@pytest.fixture()
def mf6_shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the fake mf6 binary (NO real mf6 / docker on this machine) and
    point ``$GRACE2_MF6_BIN`` at it — the env convention the spec resolves."""
    shim_dir = tmp_path / "fake-bin"
    shim_dir.mkdir()
    shim = shim_dir / "mf6"
    shim.write_text(_MF6_SHIM, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    state_dir = tmp_path / "mf6-state"
    state_dir.mkdir()
    monkeypatch.setenv("GRACE2_MF6_BIN", str(shim))
    monkeypatch.setenv("GRACE2_FAKE_MF6_STATE", str(state_dir))
    return state_dir


@pytest.fixture()
def local_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The local-backend env matrix (job-0291 + job-0292b additions)."""
    runs_dir = tmp_path / "runs"
    monkeypatch.setenv("GRACE2_SOLVER_BACKEND", "local-docker")
    monkeypatch.setenv("GRACE2_RUNS_DIR", str(runs_dir))
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    monkeypatch.delenv("GRACE2_MODFLOW_LOCAL", raising=False)
    return runs_dir


_MODFLOW_OUTPUT_GLOBS = [
    "gwt_model.ucn",
    "gwf_model.hds",
    "*.cbc",
    "*.lst",
    "mfsim.lst",
    "**/gwt_model.ucn",
    "**/*.lst",
]


def _make_staging(run_id: str, *, scheme: str = "s3") -> rm.DeckStaging:
    return rm.DeckStaging(
        run_id=run_id,
        manifest_uri=f"{scheme}://deck-bucket/modflow/{run_id}/manifest.json",
        deck_base_uri=f"{scheme}://deck-bucket/modflow/{run_id}/",
        local_deck_dir="/tmp/none",
        model_crs="EPSG:32617",
        gwf_name="gwf_model",
        gwt_name="gwt_model",
        spill_lat=26.64,
        spill_lon=-81.87,
        output_globs=list(_MODFLOW_OUTPUT_GLOBS),
    )


def _seed_modflow_manifest(
    s3: FakeS3Client, run_id: str, *, bucket: str = "deck-bucket", scheme: str = "s3"
) -> str:
    """Seed the worker-contract MODFLOW manifest + deck inputs (subdir layout).

    Input entries keep the LEGACY ``gs_uri`` field NAME; the VALUES carry
    ``{scheme}://`` URIs resolved by scheme (job-0291 convention).
    """
    base_key = f"modflow/{run_id}/"
    deck = {
        "mfsim.nam": b"[mfsim]",
        "mfsim.tdis": b"[tdis]",
        "gwfgwt.exg": b"[exg]",
        "gwf/gwf_model.nam": b"[gwf nam]",
        "gwf/gwf_model.dis": b"[gwf dis]",
        "gwt/gwt_model.nam": b"[gwt nam]",
    }
    inputs = []
    for rel, payload in deck.items():
        key = f"{base_key}{rel}"
        s3.objects[(bucket, key)] = payload
        inputs.append({"gs_uri": f"{scheme}://{bucket}/{key}", "dest": rel})
    manifest = {
        "inputs": inputs,
        "mf6_args": [],
        "model_crs": "EPSG:32617",
        "outputs": list(_MODFLOW_OUTPUT_GLOBS),
    }
    manifest_key = f"{base_key}manifest.json"
    s3.objects[(bucket, manifest_key)] = json.dumps(manifest).encode()
    return f"s3://{bucket}/{manifest_key}"


def _wait_for_completion_object(
    s3: FakeS3Client, run_id: str, timeout_s: float = 15.0
) -> dict[str, Any]:
    """Block until the supervisor thread writes completion.json (≤ timeout)."""
    deadline = time.monotonic() + timeout_s
    key = ("test-runs-bucket", f"{run_id}/completion.json")
    while time.monotonic() < deadline:
        if key in s3.objects:
            return json.loads(s3.objects[key])
        time.sleep(0.02)
    raise AssertionError(
        f"supervisor did not write completion.json within {timeout_s}s "
        f"(objects: {sorted(s3.objects)})"
    )


# --------------------------------------------------------------------------- #
# 1. Backend seam — MODFLOW submit always goes through the local-exec launcher
#    (GCP Cloud Workflows is decommissioned)
# --------------------------------------------------------------------------- #


def test_local_backend_submit_uses_local_exec_launcher(
    reset_seams, local_env: Path, mf6_shim: Path
) -> None:
    """Under GRACE2_SOLVER_BACKEND=local-docker the MODFLOW submit goes through
    the local-exec launcher and returns a local-exec-pinned handle (the Cloud
    Workflows path is removed, so this is now structurally guaranteed)."""
    s3 = FakeS3Client()
    set_s3_client(s3)
    run_id = new_ulid()
    uri = _seed_modflow_manifest(s3, run_id)
    staging = _make_staging(run_id)
    assert staging.manifest_uri == uri

    handle = rm.submit_modflow_run(staging, compute_class="standard")
    assert isinstance(handle, ExecutionHandle)
    assert handle.workflow_name == LOCAL_EXEC_WORKFLOW_NAME
    _wait_for_completion_object(s3, run_id)  # let the supervisor finish


def test_local_backend_dispatch_failure_is_typed_modflow_error(
    reset_seams, local_env: Path, mf6_shim: Path
) -> None:
    """A staging failure (missing manifest) surfaces MODFLOW_DISPATCH_FAILED —
    the typed contract the local-exec dispatch carries."""
    set_s3_client(FakeS3Client())  # empty store: manifest read will fail
    staging = _make_staging(new_ulid())
    with pytest.raises(rm.MODFLOWWorkflowError) as exc_info:
        rm.submit_modflow_run(staging)
    assert exc_info.value.error_code == "MODFLOW_DISPATCH_FAILED"


# --------------------------------------------------------------------------- #
# 2. Local submit — staging + detached mf6 launch + immediate handle
# --------------------------------------------------------------------------- #


def test_local_submit_stages_deck_and_launches_mf6(
    reset_seams, local_env: Path, mf6_shim: Path
) -> None:
    """The headline: manifest read from S3 (boto3 seam), the gwf/+gwt/ subdir
    deck reconstructed in ``$GRACE2_RUNS_DIR/<run_id>/``, mf6 launched
    detached with cwd == rundir (mf6 reads mfsim.nam from CWD), handle
    returned immediately with the staged run_id passed through."""
    s3 = FakeS3Client()
    set_s3_client(s3)
    run_id = new_ulid()
    _seed_modflow_manifest(s3, run_id)
    staging = _make_staging(run_id)

    handle = rm.submit_modflow_run(staging, compute_class="standard")

    assert isinstance(handle, ExecutionHandle)
    assert handle.solver == "modflow"
    assert handle.compute_class == "standard"
    assert handle.run_id == run_id  # GCP {run_id, manifest_uri} parity
    assert handle.workflow_name == LOCAL_EXEC_WORKFLOW_NAME
    assert handle.workflows_execution_id == f"local-exec:{run_id}"

    # Deck staged with the subdir layout intact.
    rundir = local_env / run_id
    assert (rundir / "mfsim.nam").read_bytes() == b"[mfsim]"
    assert (rundir / "gwf" / "gwf_model.dis").read_bytes() == b"[gwf dis]"
    assert (rundir / "gwt" / "gwt_model.nam").read_bytes() == b"[gwt nam]"

    _wait_for_completion_object(s3, run_id)  # supervisor finished

    # mf6 invoked once, from the rundir (CWD discipline), no args.
    calls = (mf6_shim / "calls.log").read_text().strip().splitlines()
    assert calls == ["mf6"], calls
    cwds = (mf6_shim / "cwd.log").read_text().strip().splitlines()
    assert cwds == [str(rundir)], cwds


# --------------------------------------------------------------------------- #
# 3. Supervisor completion.json — EXACT MODFLOW entrypoint schema
# --------------------------------------------------------------------------- #

#: The EXACT key set services/workers/modflow/entrypoint.py writes.
_MODFLOW_COMPLETION_KEYS = {
    "run_id",
    "status",
    "exit_code",
    "converged",
    "model_crs",
    "mf6_stdout_uri",
    "mf6_stderr_uri",
    "output_uris",
    "started_at",
    "finished_at",
    "error",
}


@pytest.mark.asyncio
async def test_local_ok_completion_schema_outputs_and_wait(
    reset_seams, local_env: Path, mf6_shim: Path
) -> None:
    s3 = FakeS3Client()
    set_s3_client(s3)
    run_id = new_ulid()
    _seed_modflow_manifest(s3, run_id)

    handle = rm.submit_modflow_run(_make_staging(run_id))
    result = await wait_for_completion(handle, poll_interval_s=0)

    assert isinstance(result, RunResult)
    assert result.status == "complete"
    assert result.output_uri == f"s3://test-runs-bucket/{run_id}/"

    completion = json.loads(
        s3.objects[("test-runs-bucket", f"{run_id}/completion.json")]
    )
    assert set(completion.keys()) == _MODFLOW_COMPLETION_KEYS
    assert completion["run_id"] == run_id
    assert completion["status"] == "ok"
    assert completion["exit_code"] == 0
    assert completion["converged"] is True
    assert completion["model_crs"] == "EPSG:32617"
    assert completion["error"] is None
    assert completion["started_at"].endswith("Z")
    assert completion["finished_at"].endswith("Z")
    assert completion["mf6_stdout_uri"] == (
        f"s3://test-runs-bucket/{run_id}/mf6.stdout"
    )
    assert completion["mf6_stderr_uri"] == (
        f"s3://test-runs-bucket/{run_id}/mf6.stderr"
    )

    # Outputs glob-expanded (incl. the recursive ** nets, de-duplicated)
    # and uploaded under the runs prefix.
    expected = {
        f"s3://test-runs-bucket/{run_id}/gwt_model.ucn",
        f"s3://test-runs-bucket/{run_id}/gwf_model.hds",
        f"s3://test-runs-bucket/{run_id}/mfsim.lst",
    }
    assert set(completion["output_uris"]) == expected
    assert s3.objects[("test-runs-bucket", f"{run_id}/gwt_model.ucn")] == b"UCN_BYTES"
    assert b"stdout evidence" in s3.objects[
        ("test-runs-bucket", f"{run_id}/mf6.stdout")
    ]


@pytest.mark.asyncio
async def test_local_diverged_exit_zero_overridden_by_list_file(
    reset_seams, local_env: Path, mf6_shim: Path
) -> None:
    """mf6 exit 0 BUT mfsim.lst carries the convergence-failure marker →
    status=error / exit_code=2 / solver_diverged / converged=false — the
    MODFLOW entrypoint's design-doc §8 list-file-authoritative override."""
    (mf6_shim / "behavior").write_text("diverge")
    s3 = FakeS3Client()
    set_s3_client(s3)
    run_id = new_ulid()
    _seed_modflow_manifest(s3, run_id)

    handle = rm.submit_modflow_run(_make_staging(run_id))
    result = await wait_for_completion(handle, poll_interval_s=0)

    assert result.status == "failed"
    assert result.error_message == "solver_diverged"

    completion = json.loads(
        s3.objects[("test-runs-bucket", f"{run_id}/completion.json")]
    )
    assert set(completion.keys()) == _MODFLOW_COMPLETION_KEYS
    assert completion["status"] == "error"
    assert completion["exit_code"] == 2
    assert completion["converged"] is False
    assert completion["error"] == "solver_diverged"


@pytest.mark.asyncio
async def test_local_crash_always_writes_completion(
    reset_seams, local_env: Path, mf6_shim: Path
) -> None:
    """mf6 crash (exit 3) → completion.json STILL written (status=error,
    raw exit code surfaced, stderr evidence uploaded)."""
    (mf6_shim / "behavior").write_text("fail")
    s3 = FakeS3Client()
    set_s3_client(s3)
    run_id = new_ulid()
    _seed_modflow_manifest(s3, run_id)

    handle = rm.submit_modflow_run(_make_staging(run_id))
    result = await wait_for_completion(handle, poll_interval_s=0)

    assert result.status == "failed"
    assert "non-zero code 3" in (result.error_message or "")

    completion = json.loads(
        s3.objects[("test-runs-bucket", f"{run_id}/completion.json")]
    )
    assert completion["status"] == "error"
    assert completion["exit_code"] == 3
    assert completion["converged"] is False
    assert b"stderr boom" in s3.objects[("test-runs-bucket", f"{run_id}/mf6.stderr")]


# --------------------------------------------------------------------------- #
# 4. Cancel (Invariant 8 — process-group kill) + timeout
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_local_cancel_chain_killpg_plus_cancelled_completion(
    reset_seams, local_env: Path, mf6_shim: Path
) -> None:
    """Invariant-8 for the exec kind: cancelling the wait coroutine kills the
    detached mf6 PROCESS GROUP (no container to docker-kill) and the
    supervisor writes the status="cancelled" completion — well inside the
    ≤30 s budget — then CancelledError re-raises."""
    (mf6_shim / "behavior").write_text("hang")
    s3 = FakeS3Client()
    set_s3_client(s3)
    run_id = new_ulid()
    _seed_modflow_manifest(s3, run_id)

    handle = rm.submit_modflow_run(_make_staging(run_id))
    run = solver_mod._LOCAL_RUNS[handle.run_id]  # grab before the pop
    assert run.spec.exec_kind == "exec"

    cancel_started = time.monotonic()
    task = asyncio.create_task(wait_for_completion(handle, poll_interval_s=0))
    await asyncio.sleep(0.2)  # let the poll loop start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    completion = _wait_for_completion_object(s3, run_id, timeout_s=15.0)
    elapsed = time.monotonic() - cancel_started
    assert elapsed < 30.0, f"cancel chain took {elapsed:.1f}s (NFR-R-3 budget is 30s)"
    assert set(completion.keys()) == _MODFLOW_COMPLETION_KEYS
    assert completion["status"] == "cancelled"
    run.supervisor.join(timeout=5.0)

    # A fresh wait on the same handle maps the cancelled completion to a
    # RunResult{status="cancelled"} (post-cancel observability).
    result = await wait_for_completion(handle, poll_interval_s=0)
    assert result.status == "cancelled"
    assert result.cancellation_reason


@pytest.mark.asyncio
async def test_local_wait_timeout_kills_process_group(
    reset_seams, local_env: Path, mf6_shim: Path
) -> None:
    (mf6_shim / "behavior").write_text("hang")
    s3 = FakeS3Client()
    set_s3_client(s3)
    run_id = new_ulid()
    _seed_modflow_manifest(s3, run_id)

    handle = rm.submit_modflow_run(_make_staging(run_id))
    run = solver_mod._LOCAL_RUNS[handle.run_id]
    result = await wait_for_completion(handle, poll_interval_s=0, timeout_s=1)

    assert result.status == "failed"
    assert result.error_code == "SOLVER_TIMEOUT"
    # Timeout ≠ user cancel: the supervisor records error, not cancelled
    # (the killed process group exits non-zero, no cancel flag set).
    completion = _wait_for_completion_object(s3, run_id, timeout_s=15.0)
    assert completion["status"] == "error"
    run.supervisor.join(timeout=5.0)


# --------------------------------------------------------------------------- #
# 5a. Deck assembly — scheme-aware prefix + boto3 upload (kickoff §3)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not _HAVE_FLOPY, reason="flopy not installed")
def test_deck_base_uri_scheme_aware(
    reset_seams, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deck prefix follows cache.storage_scheme(): s3:// always after the
    GCP decommission — the gs legacy seam is gone, so even an explicit
    GRACE2_STORAGE_BACKEND=gcs override resolves to s3://."""
    args = __import__(
        "grace2_contracts.modflow_contracts", fromlist=["MODFLOWRunArgs"]
    ).MODFLOWRunArgs(
        spill_location_latlon=(26.64, -81.87),
        contaminant="benzene",
        release_rate_kg_s=0.01,
        duration_days=30.0,
    )
    # Unset -> S3 default (GCP decommissioned).
    monkeypatch.delenv("GRACE2_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("GRACE2_CACHE_BUCKET", "cache-bkt")
    staging = rm.build_and_stage_modflow_deck(
        args, workdir=tmp_path / "default", stage_to_gcs=False
    )
    assert staging.manifest_uri.startswith("s3://cache-bkt/modflow/")
    for entry in staging.manifest_inputs:
        assert entry["gs_uri"].startswith("s3://cache-bkt/modflow/")

    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    staging_s3 = rm.build_and_stage_modflow_deck(
        args, workdir=tmp_path / "s3", stage_to_gcs=False
    )
    assert staging_s3.manifest_uri.startswith("s3://cache-bkt/modflow/")
    assert staging_s3.manifest_uri.endswith("/manifest.json")
    for entry in staging_s3.manifest_inputs:
        # Legacy field NAME, s3:// VALUE — staging resolves by scheme.
        assert entry["gs_uri"].startswith("s3://cache-bkt/modflow/")

    # A stray legacy override no longer resurrects gs:// — S3-only.
    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "gcs")
    staging_gs = rm.build_and_stage_modflow_deck(
        args, workdir=tmp_path / "gs", stage_to_gcs=False
    )
    assert staging_gs.manifest_uri.startswith("s3://cache-bkt/modflow/")


@pytest.mark.skipif(not _HAVE_FLOPY, reason="flopy not installed")
def test_deck_staging_uploads_via_boto3_under_s3(
    reset_seams, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under GRACE2_STORAGE_BACKEND=s3 the deck + manifest upload goes via
    boto3 (NOT fsspec/s3fs — booby-trapped) and every manifest input cites
    an object that actually exists in the store (staging-ready)."""
    from grace2_contracts.modflow_contracts import MODFLOWRunArgs

    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_CACHE_BUCKET", "deck-bucket")
    monkeypatch.delenv("GRACE2_MODFLOW_LOCAL", raising=False)
    s3 = FakeS3Client()
    set_s3_client(s3)

    angry_fsspec = MagicMock()
    angry_fsspec.filesystem.side_effect = AssertionError(
        "fsspec used for an s3:// deck upload — job-0292b requires boto3"
    )
    args = MODFLOWRunArgs(
        spill_location_latlon=(26.64, -81.87),
        contaminant="benzene",
        release_rate_kg_s=0.01,
        duration_days=30.0,
    )
    with patch.dict("sys.modules", {"fsspec": angry_fsspec}, clear=False):
        staging = rm.build_and_stage_modflow_deck(
            args, workdir=tmp_path, stage_to_gcs=True
        )

    base_key = f"modflow/{staging.run_id}/"
    manifest = json.loads(s3.objects[("deck-bucket", f"{base_key}manifest.json")])
    assert manifest["model_crs"] == "EPSG:32617"
    assert manifest["mf6_args"] == []
    assert manifest["outputs"]
    dests = {e["dest"] for e in manifest["inputs"]}
    assert "mfsim.nam" in dests
    assert any(d.startswith("gwf/") for d in dests)
    assert any(d.startswith("gwt/") for d in dests)
    for entry in manifest["inputs"]:
        assert entry["gs_uri"].startswith(f"s3://deck-bucket/{base_key}"), entry
        _, _, key = entry["gs_uri"][len("s3://"):].partition("/")
        assert ("deck-bucket", key) in s3.objects, f"manifest cites missing {key}"


# --------------------------------------------------------------------------- #
# 5b. Plume postprocess — scheme-aware UCN read + COG upload + publish
# --------------------------------------------------------------------------- #


def test_resolve_ucn_path_s3_prefix_and_direct(reset_seams) -> None:
    s3 = FakeS3Client()
    set_s3_client(s3)
    s3.objects[("test-runs-bucket", "RUNX/gwt_model.ucn")] = b"UCN_BYTES"
    local = pp._resolve_ucn_path("s3://test-runs-bucket/RUNX/")
    assert local.name == "gwt_model.ucn"
    assert local.read_bytes() == b"UCN_BYTES"
    local2 = pp._resolve_ucn_path("s3://test-runs-bucket/RUNX/gwt_model.ucn")
    assert local2.read_bytes() == b"UCN_BYTES"


def test_resolve_ucn_path_s3_failure_is_typed(reset_seams) -> None:
    set_s3_client(FakeS3Client())
    with pytest.raises(pp.PostprocessMODFLOWError) as exc_info:
        pp._resolve_ucn_path("s3://test-runs-bucket/MISSING/")
    assert exc_info.value.error_code == "PLUME_OUTPUT_READ_FAILED"


def test_upload_cog_scheme_aware(
    reset_seams, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cog = tmp_path / "plume.tif"
    cog.write_bytes(b"COG_BYTES")
    s3 = FakeS3Client()
    set_s3_client(s3)

    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_RUNS_BUCKET", "test-runs-bucket")
    dest = pp._upload_cog(cog, "RUNX", None)
    assert dest == "s3://test-runs-bucket/RUNX/plume_concentration_4326.tif"
    assert (
        s3.objects[("test-runs-bucket", "RUNX/plume_concentration_4326.tif")]
        == b"COG_BYTES"
    )

    # No GCP-named default on AWS: missing bucket env is a TYPED failure (the
    # silent file:// fallback was the job-0241 debug-invisible no-render bug).
    monkeypatch.delenv("GRACE2_RUNS_BUCKET", raising=False)
    with pytest.raises(pp.PostprocessMODFLOWError) as exc_info:
        pp._upload_cog(cog, "RUNX", None)
    assert exc_info.value.error_code == "PLUME_COG_UPLOAD_FAILED"
    assert "GRACE2_RUNS_BUCKET" in str(exc_info.value)


def test_dispatch_publish_layer_passes_s3_through(reset_seams) -> None:
    """s3:// COGs reach publish_layer (the job-0290 TiTiler path) instead of
    being skipped — the job-0254 PlumeLayerURI rendering gap, closed."""
    with patch(
        "grace2_agent.tools.publish_layer.publish_layer",
        return_value="https://tiles.example/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png?url=x",
    ) as fake_publish:
        out = pp._dispatch_publish_layer("s3://bkt/RUNX/plume.tif", "plume-RUNX")
    assert out is not None and "{z}/{x}/{y}" in out
    fake_publish.assert_called_once()
    _, kwargs = fake_publish.call_args
    assert kwargs["layer_uri"] == "s3://bkt/RUNX/plume.tif"
    assert kwargs["style_preset"] == pp.PLUME_STYLE_PRESET


def test_dispatch_publish_layer_still_skips_file_uri(reset_seams) -> None:
    with patch("grace2_agent.tools.publish_layer.publish_layer") as fake_publish:
        out = pp._dispatch_publish_layer("file:///tmp/plume.tif", "plume-RUNX")
    assert out is None
    fake_publish.assert_not_called()


def test_publish_layer_raw_s3_for_plume_preset(
    reset_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TiTiler exit: the plume publish returns the raw s3:// COG uri and the
    red-ramp render params (0-10 reds) ride the stashed LEGEND keyed by that
    uri (the plugin renders from it)."""
    from grace2_agent.tools import publish_layer as pl_mod
    from grace2_agent.tools.publish_layer import pop_legend_for_uri, publish_layer

    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    # No network: the fake key never resolves; the registry preset still pins
    # the style (byte-identical resolver behavior).
    monkeypatch.setattr(pl_mod, "_read_raster_bytes", lambda uri: None)
    out = publish_layer(
        layer_uri="s3://test-runs-bucket/RUNX/plume_concentration_4326.tif",
        layer_id="plume-concentration-RUNX",
        style_preset="continuous_plume_concentration",
    )
    assert out == "s3://test-runs-bucket/RUNX/plume_concentration_4326.tif"
    assert "/cog/tiles/" not in out
    legend = pop_legend_for_uri(out)
    assert legend is not None and legend.kind == "continuous"
    assert (legend.colormap, legend.vmin, legend.vmax) == ("reds", 0.0, 10.0)


# --------------------------------------------------------------------------- #
# 6. Full local-backend E2E through run_modflow_job (real deck, real flopy
#    UCN read, real rasterio reprojection; fake mf6 + fake S3)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not _HAVE_FLOPY, reason="flopy not installed")
@pytest.mark.asyncio
async def test_run_modflow_job_local_backend_e2e(
    reset_seams,
    local_env: Path,
    mf6_shim: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job-0291-mirror E2E: run_modflow_job under the local backend drives
    REAL code end-to-end — real FloPy deck build + boto3 deck staging to the
    fake S3, manifest staged back down, fake mf6 emitting a REAL
    flopy-readable UCN, supervisor completion to S3, the shared
    wait_for_completion poll, real flopy concentration read + rasterio
    UTM→EPSG:4326 COG reprojection, boto3 COG upload, and the raw-s3 COG
    publish (TiTiler exit) — yielding a typed PlumeLayerURI with non-zero
    metrics.
    """
    from grace2_agent.tools.run_modflow_tool import run_modflow_job
    from grace2_contracts.modflow_contracts import PlumeLayerURI

    monkeypatch.setenv("GRACE2_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("GRACE2_CACHE_BUCKET", "deck-bucket")
    s3 = FakeS3Client()
    set_s3_client(s3)

    # Seed a REAL 40x40 concentration grid (the gwt_adapter demo grid shape:
    # 50 m cells) the shim copies into the rundir as gwt_model.ucn.
    grid = np.zeros((40, 40))
    grid[20, 20] = 7.5  # peak, mg/L
    grid[20, 21] = 2.0
    _write_synthetic_ucn(mf6_shim / "gwt_model.ucn", grid)

    # Keep the REAL poll loop but drop its 10 s default cadence (test speed —
    # job-0291's tests pass poll_interval_s=0 the same way).
    real_wait = solver_mod.wait_for_completion

    async def fast_wait(handle: ExecutionHandle, **_kw: Any) -> RunResult:
        return await real_wait(handle, poll_interval_s=0)

    monkeypatch.setattr(solver_mod, "wait_for_completion", fast_wait)

    result = await run_modflow_job(
        spill_location_latlon=(26.64, -81.87),
        contaminant="benzene",
        release_rate_kg_s=0.01,
        duration_days=30.0,
    )

    assert isinstance(result, PlumeLayerURI), f"got {result!r}"
    run_id = result.layer_id.replace("plume-concentration-", "")

    # Completion: EXACT MODFLOW entrypoint schema, converged, CRS echoed.
    completion = json.loads(
        s3.objects[("test-runs-bucket", f"{run_id}/completion.json")]
    )
    assert set(completion.keys()) == _MODFLOW_COMPLETION_KEYS
    assert completion["status"] == "ok"
    assert completion["converged"] is True
    assert completion["model_crs"] == "EPSG:32617"

    # Typed narration metrics (Invariant 1) from the REAL flopy read:
    # peak 7.5 mg/L; two cells > floor × 2500 m² = 0.005 km².
    assert result.max_concentration_mgl == pytest.approx(7.5)
    assert result.plume_area_km2 == pytest.approx(0.005)
    assert result.units == "mg/L"
    assert result.style_preset == "continuous_plume_concentration"

    # Reprojected bbox lands near Fort Myers (the spill point).
    assert result.bbox is not None
    min_lon, min_lat, max_lon, max_lat = result.bbox
    assert -82.5 < min_lon < -81.0
    assert 26.0 < min_lat < 27.5

    # COG uploaded to the S3 runs prefix via boto3.
    assert (
        "test-runs-bucket",
        f"{run_id}/plume_concentration_4326.tif",
    ) in s3.objects

    # Published as the raw s3:// COG (TiTiler exit) — the layer envelope
    # carries the COG uri the QGIS plugin opens directly via GDAL, and the
    # red-ramp render params ride the stashed legend keyed by that uri.
    from grace2_agent.tools.publish_layer import pop_legend_for_uri

    assert result.uri.startswith("s3://test-runs-bucket/")
    assert result.uri.endswith(".tif")
    assert "/cog/tiles/" not in result.uri
    legend = pop_legend_for_uri(result.uri)
    assert legend is not None and legend.kind == "continuous"
    assert (legend.colormap, legend.vmin, legend.vmax) == ("reds", 0.0, 10.0)
