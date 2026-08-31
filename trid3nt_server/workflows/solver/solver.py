"""Solver dispatch atomic tools.

This module registers two atomic tools that drive the solver-execution
substrate. Dispatch is a local container run (``local-docker``) or a
direct-binary run (``local-exec``) on this machine, selected per solver by its
``LocalSolverSpec`` exec spec. Together they implement the **solver-dispatch
surface**:

    - ``run_solver(solver, model_setup_uri, compute_class="medium")
       -> ExecutionHandle`` -- submits a solver run on the active backend.
      Currently only ``solver="sfincs"`` is supported; other values raise
      ``SolverNotRegisteredError``.

    - ``wait_for_completion(handle, poll_interval_s=10, timeout_s=1800)
       -> RunResult`` -- polls the run backing ``handle`` every
      ``poll_interval_s`` seconds, emits a ``pipeline-state`` progress update
      on every poll via ``PipelineEmitter.update_progress`` (the opt-in seam
      surfaced for M5+ solvers), and on success reads
      ``completion.json`` from the runs bucket and returns a populated
      ``RunResult``. On failure or cancellation the matching terminal
      ``RunResult`` is returned.

Both tools are uncacheable-by-construction (solver dispatchers are explicitly
enumerated): ``cacheable=False``, ``ttl_class="live-no-cache"``,
``source_class="solver_dispatch"``. They never touch the cache shim.

Cross-cutting principles (per CLAUDE.md + agents/AGENTS.md):

- **Invariant 1 (Determinism boundary): preserves.** Progress estimation is
  a wall-clock linear ramp keyed off ``handle.submitted_at`` and the
  target (900 s for ``≤15 min``) -- not an LLM estimate. The ramp
  is clamped at 95% until the Workflow returns SUCCEEDED (then jumps to
  100%) so we never falsely advertise completion.

- **Invariant 2 (Deterministic workflows): preserves.** ``run_solver`` is a
  thin solver dispatch (local container / direct binary);
  no LLM in the dispatch. The deterministic step graph (stage → invoke →
  read completion) is owned by the backend.

- **Invariant 8 (Cancellation is first-class): the headline.** Cancel chain
  end-to-end:

      WS cancel -> server.py inflight_task.cancel()
                -> asyncio.CancelledError inside emit_tool_call
                -> emit_tool_call CALLs invoke() which is our
                    wait_for_completion coroutine
                -> wait_for_completion sees CancelledError in its poll
                    sleep, terminates the live container
                    (≤30 s, Invariant-8)
                -> the supervisor writes the status="cancelled"
                    completion.json
                -> wait_for_completion re-raises CancelledError so
                    emit_tool_call's mark_cancelled branch fires

  30s budget. The backend handler terminates the run
  *before* re-raising the ``CancelledError`` so the kill is initiated
  atomically with the local cancel.

- **A.7 replace-not-reconcile: preserves.** Every progress emission goes
  through ``PipelineEmitter.update_progress(step_id, ...)``, which already
  builds the full snapshot per A.7. We never hand-roll a partial frame.

- **Uncacheable (enumeration): preserves.** Both tools declare
  ``cacheable=False`` + ``ttl_class="live-no-cache"`` + a new source class
  ``"solver_dispatch"``. The kickoff explicitly enumerates them.

Dependency-injection seams:

- ``_EMITTER_BINDING`` / ``set_emitter_binding(emitter, step_id)`` -- the
  active ``PipelineEmitter`` + the step_id this ``wait_for_completion``
  invocation is bracketed by, set by the integration site (``server.py``)
  where ``emit_tool_call`` surfaces its ``step_id`` to the tool body.

- ``_RUNS_BUCKET`` / ``set_runs_bucket(name)`` -- overrides the runs bucket
  name. Used by tests to reach a fixture bucket; production wiring leaves it
  at the env-driven default.

- ``_S3_CLIENT`` / ``set_s3_client(client)`` -- the boto3 S3 client used for
  ALL S3 staging / completion I/O. Lazily-default to the EC2 instance-role
  client (boto3-not-s3fs lesson).

Run id generation: the agent service generates a ULID per ``run_solver``
call. The same id is used to compose the runs-bucket completion path
(``s3://<runs_bucket>/<run_id>/completion.json``).

Solver backend (local-only)
---------------------------

``local-docker`` is the ONLY backend:

- ``local-docker`` -- the S3-IN → sfincs → S3-OUT envelope lives INSIDE the
  agent (testable Python); the object store is whatever ``AWS_ENDPOINT_URL``
  points at (locally: MinIO). The container is the PLAIN upstream
  ``deltares/sfincs-cpu`` binary image run via ``docker run`` on this
  machine:

      run_solver: mint run_id → download the setup manifest from S3 (boto3)
        → stage every ``inputs[]`` object into ``$TRID3NT_RUNS_DIR/<run_id>/``
        (manifest field name stays the legacy ``gs_uri``; the VALUE is an
        ``s3://`` URI resolved by scheme via boto3)
        → launch ``docker run --rm --name <run_id> -v <rundir>:/data -w /data
        $TRID3NT_SFINCS_IMAGE [sfincs_args]`` DETACHED (Popen) → return
        ExecutionHandle immediately (``workflow_name="local-docker"``,
        ``workflows_execution_id="local-docker:<run_id>"`` -- the container
        name IS the run_id, which is the Invariant-8 cancellation seam).

      supervisor (daemon thread): waits on the docker process, expands the
        manifest's ``outputs[]`` globs in the rundir, uploads outputs +
        sfincs.stdout/sfincs.stderr to ``s3://$TRID3NT_RUNS_BUCKET/<run_id>/``
        (boto3), and ALWAYS writes ``completion.json`` (exact entrypoint.py
        schema: run_id/status/exit_code/sfincs_stdout_uri/sfincs_stderr_uri/
        output_uris/started_at/finished_at/error) -- even on crash
        (status="error") or cancel (status="cancelled").

      wait_for_completion: dispatches on ``handle.workflow_name`` -- local
        handles poll the completion.json object on S3 (cadence/timeout/
        progress-ramp semantics) and build the RunResult with
        ``output_uri = s3://<runs_bucket>/<run_id>/``.

      cancel chain: ``asyncio.CancelledError`` in the poll sleep → mark the
        run cancelled + ``docker kill <run_id>`` (≤30 s, Invariant-8) → the
        supervisor wakes on process exit and writes the status="cancelled"
        completion.json → re-raise.

  ``TRID3NT_RUNS_BUCKET`` has NO default under local-docker (a missing value
  raises ``SolverDispatchError``). boto3 is used for ALL S3 I/O (s3fs falls
  back to anonymous credentials on the EC2 instance role - lesson).

Generalized local backend
----------------------------------------------------

``LocalSolverSpec`` extends the machinery to MODFLOW without forking it. The
staging → detached launch → supervisor → completion.json → S3-poll envelope is
solver-agnostic; the solver-specific knobs are bundled into a
``LocalSolverSpec`` (manifest argv key, launch argv builder, stdout/stderr
artifact names, completion-manifest field names, an optional post-exit
classifier for solver-specific status resolution, and the cancel kind):

- SFINCS keeps the ``docker run`` path verbatim
  (``_run_solver_local_docker`` builds the SFINCS spec; the completion.json
  is byte-identical to ``workers/sfincs/entrypoint.py``).
- MODFLOW (``workflows/run_modflow.py``) launches the **mf6 binary directly**
  (``exec_kind="exec"`` -- no public MODFLOW image exists; the instance gets
  the same SHA-pinned USGS 6.5.0 static binary the worker image installs).
  Its spec's ``classify_exit`` reproduces the MODFLOW entrypoint's
  list-file convergence guard, and the completion.json carries the EXACT
  ``workers/modflow/entrypoint.py`` key set (``mf6_stdout_uri`` /
  ``mf6_stderr_uri`` / ``converged`` / ``model_crs``).

Cancel kinds: ``"docker"`` → ``docker kill <run_id>`` (container name ==
run_id); ``"exec"`` → ``os.killpg`` on the detached process group
(``start_new_session=True`` makes pgid == pid). Both terminal ≤30 s
(Invariant 8). ``wait_for_completion`` dispatches on the handle's
``workflow_name`` ∈ {``local-docker``, ``local-exec``} -- the poll loop is
shared.
"""

from __future__ import annotations

import asyncio
import glob as _glob
import json
import logging
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from trid3nt_contracts import new_ulid
from trid3nt_contracts.execution import ExecutionHandle, RunResult
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool

__all__ = [
    "run_solver",
    "wait_for_completion",
    "SolverNotRegisteredError",
    "SolverDispatchError",
    "set_emitter_binding",
    "set_runs_bucket",
    "set_s3_client",
    "SOLVER_BACKEND_LOCAL_DOCKER",
    "LOCAL_DOCKER_WORKFLOW_NAME",
    "LOCAL_EXEC_WORKFLOW_NAME",
    "LocalSolverSpec",
    "launch_local_solver",
    "SOLVER_WORKFLOW_REGISTRY",
    "LOCAL_SOLVER_SPEC_REGISTRY",
    "register_local_solver_spec",
    "EmitterBinding",
    "NFR_P_4_TARGET_SECONDS",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_TIMEOUT_S",
    "PROGRESS_CLAMP_MAX",
    "PROGRESS_TERMINAL",
]

logger = logging.getLogger("trid3nt_server.workflows.solver.solver")


# --------------------------------------------------------------------------- #
# Constants / configuration
# --------------------------------------------------------------------------- #


#: Target run-time budget for ≤200 km² at 30m (15 min).
#: Progress is wall-clock linear in (now - submitted_at) / target.
NFR_P_4_TARGET_SECONDS: float = 900.0

#: Default poll cadence -- matches the ≤15-min budget granularity (≥9 polls).
DEFAULT_POLL_INTERVAL_S: int = 10

#: Default overall timeout (30 min -- mirrors the Cloud Run Job task_timeout
#: gives 2× headroom over the target budget). Env-overridable via
#: ``TRID3NT_SOLVER_TIMEOUT_S`` so a legitimately long run (a large coastal
#: quadtree + SnapWave solve exceeds the 30-min pluvial budget this constant was
#: sized for) can be given more headroom on the box WITHOUT touching the call
#: sites; absent/garbage env falls back to 1800 so default behaviour is unchanged.
def _default_timeout_s() -> int:
    raw = (os.environ.get("TRID3NT_SOLVER_TIMEOUT_S") or "").strip()
    try:
        v = int(raw)
        return v if v > 0 else 1800
    except ValueError:
        return 1800


DEFAULT_TIMEOUT_S: int = _default_timeout_s()

#: Highest progress we ever advertise before the Workflow is SUCCEEDED.
#: Clamp keeps us honest under late runs -- the chip never jumps to 100% on
#: estimate alone.
PROGRESS_CLAMP_MAX: int = 95

#: Final progress when the Workflow reports SUCCEEDED.
PROGRESS_TERMINAL: int = 100


#: Solver → workflow name registry. The VALUE is the canonical
#: workflow/composer name for the solver; the registry is consumed purely as a
#: PRESENCE GATE by ``run_solver`` (an unregistered solver raises
#: ``SolverNotRegisteredError``) -- the live backend routing + the handle's pinned
#: ``workflow_name`` come from the backend sentinel
#: (``LOCAL_DOCKER_WORKFLOW_NAME`` / ``LOCAL_EXEC_WORKFLOW_NAME``), not from this
#: value. Every entry is now contributed at import by the engine that owns it
#: (``workflows/telemac/run_telemac.py`` registers the TELEMAC solvers beside
#: their ``LocalSolverSpec``), so a solver named here without a spec behind it
#: cannot happen: the two land together or not at all.
SOLVER_WORKFLOW_REGISTRY: dict[str, str] = {}


# --- Solver backend seam --- #

#: The ONE backend: plain upstream ``deltares/sfincs-cpu`` via ``docker run`` on
#: this machine; the staging/upload envelope lives in this module.
SOLVER_BACKEND_LOCAL_DOCKER: str = "local-docker"

#: ``ExecutionHandle.workflow_name`` sentinel for local-docker handles --
#: ``wait_for_completion`` dispatches on it (the handle pins its backend so
#: env churn between submit and wait cannot mis-route the poll).
LOCAL_DOCKER_WORKFLOW_NAME: str = "local-docker"

#: ``ExecutionHandle.workflow_name`` sentinel for image-less local runs that
#: exec a solver binary directly (MODFLOW's mf6 has no public
#: image; the USGS static binary runs on the instance). Same poll loop as
#: local-docker; the cancel chain kills the detached process group instead
#: of a container.
LOCAL_EXEC_WORKFLOW_NAME: str = "local-exec"

#: The two local workflow_name sentinels ``wait_for_completion`` accepts.
_LOCAL_WORKFLOW_NAMES: tuple[str, str] = (
    LOCAL_DOCKER_WORKFLOW_NAME,
    LOCAL_EXEC_WORKFLOW_NAME,
)

#: ``ExecutionHandle.workflow_location`` for local-docker handles.
LOCAL_DOCKER_WORKFLOW_LOCATION: str = "local"

#: Default rundir root under local-docker (env ``TRID3NT_RUNS_DIR``).
DEFAULT_LOCAL_RUNS_DIR: str = "/opt/trid3nt/runs"

#: Budget for the ``docker kill`` subprocess on cancel -- comfortably inside
#: the ≤30 s cancellation-budget envelope.
DOCKER_KILL_TIMEOUT_S: float = 25.0


#: The compute-class vocabulary, in ONE place: what a caller may say, mapped onto
#: the ``ExecutionHandle.ComputeClass`` contract. ``medium`` is a retained SYNONYM
#: of the contract's ``standard`` - it is still the spelling most template Params
#: declare, and renaming it is a fleet-wide model-facing change rather than a
#: dispatch one. Everything that validates a compute class reads this map; nobody
#: keeps a second copy of the set.
COMPUTE_CLASS_ALIAS: dict[str, str] = {
    "small": "small",
    "medium": "standard",
    "standard": "standard",
    "large": "large",
    "xlarge": "xlarge",
    "gpu": "gpu",
}


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class SolverNotRegisteredError(ValueError):
    """Raised by ``run_solver`` when ``solver`` is not in
    ``SOLVER_WORKFLOW_REGISTRY``. Distinct from a tool-params-invalid error
    so the agent surface can render a useful "solver X not supported in v0.1
    (ships sfincs only - TELEMAC / MODFLOW / HEC-HMS land in
    their respective milestones)" message."""


class SolverDispatchError(RuntimeError):
    """Raised when the backend dispatch (local container / direct binary)
    fails or the completion-manifest read fails. The
    agent's emitter classifier maps this to ``UPSTREAM_API_ERROR``. The
    ``error_code`` attribute carries the open-set A.6 code so a downstream
    wrapper can re-emit it verbatim."""

    error_code: str = "SOLVER_DISPATCH_FAILED"


# --------------------------------------------------------------------------- #
# DI seams
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EmitterBinding:
    """Tuple of (emitter, step_id) the active ``wait_for_completion`` invocation
    should drive progress emissions through.

    The integration site (``server.py``'s ``emit_tool_call`` wrapper) is
    responsible for binding this around each ``wait_for_completion`` call;
    until that follow-up job lands, the smoke harness binds it directly per
    the kickoff TENTATIVE recommendation.
    """

    emitter: Any
    step_id: str


_EMITTER_BINDING: EmitterBinding | None = None
_RUNS_BUCKET: str | None = None
_S3_CLIENT: Any | None = None


def set_emitter_binding(binding: EmitterBinding | None) -> None:
    """Bind the active ``(emitter, step_id)`` pair for progress emission.

    See class docstring for the integration-site discipline. ``None`` clears
    the binding (the polling loop falls back to no-op progress emission).
    """
    global _EMITTER_BINDING
    _EMITTER_BINDING = binding


def set_runs_bucket(name: str | None) -> None:
    """Override the runs-bucket name. ``None`` restores the env-based default."""
    global _RUNS_BUCKET
    _RUNS_BUCKET = name


def set_s3_client(client: Any) -> None:
    """Bind the boto3 S3 client used for ALL local-docker S3 I/O.

    Production wiring leaves this ``None``: the lazy default builds
    ``boto3.client("s3", region_name=$AWS_REGION)``, whose endpoint and
    credentials come from the ambient environment - boto3, never s3fs, because
    s3fs falls back to anonymous access and reads corrupt bytes.
    Tests inject a tmpdir-backed fake exposing ``get_object`` /
    ``put_object``. ``None`` restores the lazy default.

    The deck-assembly (``sfincs_builder``) and run-output
    (``postprocess_flood``) S3 paths share this seam so one injection covers
    the whole staged-manifest → solve → postprocess chain.
    """
    global _S3_CLIENT
    _S3_CLIENT = client


def _get_s3_client() -> Any:
    """Return the bound S3 client or lazily construct the boto3 default.

    boto3 (NOT s3fs) for all S3 I/O -- s3fs falls back to anonymous
    credentials on the EC2 instance role. Lazy import so
    A process that never dispatches a solver never pays for boto3 at import.
    """
    if _S3_CLIENT is not None:
        return _S3_CLIENT
    try:
        import boto3  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise SolverDispatchError(
            f"boto3 not importable: {exc}; the local-docker solver backend "
            "requires boto3 for S3 staging/upload."
        ) from exc
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))


def _get_runs_bucket() -> str:
    """The overridden runs bucket, or ``TRID3NT_RUNS_BUCKET``, or the default name."""
    if _RUNS_BUCKET is not None:
        return _RUNS_BUCKET
    return os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")


def _get_local_runs_bucket() -> str:
    """Runs bucket under local-docker, with NO default at all.

    ``set_runs_bucket`` wins (the test seam); otherwise ``TRID3NT_RUNS_BUCKET``
    must be set explicitly. A default here would let every run upload into a
    bucket nobody provisioned and call it a success, so an unset variable fails
    loudly instead.
    """
    if _RUNS_BUCKET is not None:
        return _RUNS_BUCKET
    bucket = (os.environ.get("TRID3NT_RUNS_BUCKET") or "").strip()
    if not bucket:
        raise SolverDispatchError(
            "TRID3NT_RUNS_BUCKET must be set when TRID3NT_SOLVER_BACKEND="
            "local-docker; there is no default runs bucket."
        )
    return bucket


# --------------------------------------------------------------------------- #
# local-docker backend
#
# The GCS-IN → sfincs → GCS-OUT envelope from
# ``workers/sfincs/entrypoint.py`` ported into the agent: staging,
# detached ``docker run`` of the plain upstream image, a supervisor thread
# that uploads outputs and ALWAYS writes the entrypoint-schema
# completion.json, S3 completion polling, and the docker-kill cancel chain.
# --------------------------------------------------------------------------- #


def _utc_now_iso() -> str:
    """ISO8601-Z timestamp matching the entrypoint's ``_utc_now`` format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _split_object_uri(uri: str) -> tuple[str, str, str]:
    """Split ``s3://bucket/key`` → (scheme, bucket, key).

    Only the ``s3://`` scheme is supported. Raises
    ``SolverDispatchError`` on malformed or unsupported URIs.
    """
    prefix = "s3://"
    if uri.startswith(prefix):
        bucket, _, key = uri[len(prefix):].partition("/")
        if not bucket or not key:
            raise SolverDispatchError(f"malformed s3:// URI: {uri!r}")
        return "s3", bucket, key
    raise SolverDispatchError(
        f"unsupported object URI scheme: {uri!r} (expected s3://)"
    )


def _read_object_bytes(uri: str) -> bytes:
    """Read one object's bytes, resolved BY SCHEME (kickoff):
    ``s3://`` via boto3, ``file://`` / local path via the filesystem (the
    sfincs_builder local-manifest fallback)."""
    if uri.startswith("file://"):
        return Path(uri[len("file://"):]).read_bytes()
    if not uri.startswith("s3://"):
        return Path(uri).read_bytes()
    _scheme, bucket, key = _split_object_uri(uri)
    resp = _get_s3_client().get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def _download_object(uri: str, dest: Path) -> None:
    """Download one staged input to ``dest``, resolved by scheme.

    The manifest's input entries keep the LEGACY field name ``gs_uri`` but
    the VALUE is an ``s3://`` URI (the storage backend) - we dispatch
    on the URI scheme, never the field name. Only
    ``s3://`` (and ``file://`` / local paths) are resolved.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if uri.startswith("file://") or not uri.startswith("s3://"):
        src = Path(uri[len("file://"):] if uri.startswith("file://") else uri)
        dest.write_bytes(src.read_bytes())
        return
    _scheme, bucket, key = _split_object_uri(uri)
    logger.info("local-docker staging %s -> %s", uri, dest)
    resp = _get_s3_client().get_object(Bucket=bucket, Key=key)
    import shutil

    with dest.open("wb") as fh:
        shutil.copyfileobj(resp["Body"], fh)


def _upload_file_s3(s3: Any, src: Path, bucket: str, key: str) -> str:
    """Upload ``src`` to ``s3://bucket/key`` via boto3; return the s3:// URI."""
    with src.open("rb") as fh:
        s3.put_object(Bucket=bucket, Key=key, Body=fh)
    return f"s3://{bucket}/{key}"


@dataclass(frozen=True)
class LocalSolverSpec:
    """Solver-specific knobs for the shared local backend.

    The staging → detached launch → supervisor → completion.json
    envelope is solver-agnostic; this spec carries everything that is not:

    Fields:
        solver: lowercase solver identifier carried on the handle (and used in
            the generic non-zero-exit error message -- ``"sfincs exited with
            non-zero code N"`` stays byte-identical for SFINCS).
        workflow_name: the ``ExecutionHandle.workflow_name`` sentinel --
            ``"local-docker"`` (container launch) or ``"local-exec"``
            (direct binary launch). ``wait_for_completion`` accepts both.
        args_key: the manifest key carrying the solver argv tail
            (``"sfincs_args"`` / ``"mf6_args"`` -- worker-entrypoint parity).
        build_argv: ``(run_id, rundir, manifest_args) -> argv`` -- the full
            launch command. SFINCS builds the ``docker run --rm --name
            <run_id> ...`` line; MODFLOW returns ``[mf6, *args]``.
        stdout_name / stderr_name: the rundir artifact filenames (and the
            runs-prefix upload keys) -- ``sfincs.stdout`` / ``mf6.stdout`` etc.
        stdout_uri_field / stderr_uri_field: the completion.json field names
            (``sfincs_stdout_uri`` vs ``mf6_stdout_uri`` -- exact entrypoint
            schemas).
        exec_kind: ``"docker"`` → cancel via ``docker kill <run_id>``;
            ``"exec"`` → cancel via ``os.killpg`` on the detached group.
        classify_exit: optional ``(rundir, exit_code) -> (status, exit_code,
            error, extra_completion_fields)`` post-exit hook for
            solver-specific status resolution (MODFLOW's mfsim.lst
            convergence guard + the ``converged``/``model_crs`` completion
            fields). ``None`` → the plain exit-code rule (SFINCS). A user
            cancel overrides whatever the classifier returned.
    """

    solver: str
    workflow_name: str
    args_key: str
    build_argv: Callable[[str, Path, list[str]], list[str]]
    stdout_name: str
    stderr_name: str
    stdout_uri_field: str
    stderr_uri_field: str
    exec_kind: str = "docker"
    classify_exit: (
        Callable[[Path, int], tuple[str, int, str | None, dict[str, Any]]] | None
    ) = None
    env_overrides: dict[str, str] | None = None
    """Optional environment variable overrides merged into the subprocess env.

    Used by pip-only engine specs (landlab, openquake) to prepend the
    repo root to PYTHONPATH so ``workers.*`` imports resolve in the
    subprocess. ``None`` (the default) means the subprocess inherits the parent
    env unchanged (SFINCS docker + MODFLOW mf6 binary paths both work without
    any env surgery). Keys/values are plain strings; values replace (not append)
    the matching env key. Prepend patterns (e.g. PYTHONPATH) must be assembled
    by the spec factory using the current env value.
    """

    network: str | None = None
    """The docker network this solver's container runs on, when it declares one.

    ``"none"`` is the ENGINE-ROOM posture: the container is handed a fully staged
    run directory and can reach nothing. It is per-spec, and deliberately so - an
    engine whose in-container fetches have not been migrated yet would fail under
    it, so each engine adopts it as its own inputs become staged rather than by a
    global switch. ``None`` (the default) leaves the launch line untouched, which
    is the default bridge; a spec whose ``build_argv`` already writes its own
    ``--network`` keeps that and must leave this unset.
    """


def _with_declared_network(spec: LocalSolverSpec, cmd: list[str]) -> list[str]:
    """Apply the spec's declared docker network to a launch line.

    The flag goes in HERE rather than in each ``build_argv`` because the network a
    container is allowed is a property of whether its inputs are staged, not of
    how its argv is spelled - and because a posture spread across five identical
    closures is one that drifts. A spec that already writes its own ``--network``
    is left alone: two of them on one command line is a launch failure, and the
    closure's is the one somebody wrote on purpose.
    """
    if not spec.network or spec.exec_kind != "docker":
        return cmd
    if "--network" in cmd:
        raise SolverDispatchError(
            f"solver {spec.solver!r} declares network={spec.network!r} AND its "
            "build_argv writes its own --network; declare it in one place.")
    if cmd[:2] != ["docker", "run"]:
        raise SolverDispatchError(
            f"solver {spec.solver!r} declares network={spec.network!r} but its "
            f"launch line does not start with 'docker run': {cmd[:2]}")
    return [*cmd[:2], "--network", spec.network, *cmd[2:]]


@dataclass
class _LocalRun:
    """In-process registry entry for one local-backend solver run."""

    run_id: str
    rundir: Path
    runs_bucket: str
    proc: subprocess.Popen
    output_patterns: list[str]
    started_at: str  # ISO8601-Z, entrypoint format
    stdout_path: Path
    stderr_path: Path
    spec: LocalSolverSpec
    #: WHICH CODE dispatched this run - stamped at launch, carried into
    #: completion.json, so a reader of the artifact can ask whether the engine
    #: has moved since rather than assuming it has not.
    code: dict[str, Any] = field(default_factory=dict)
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    supervisor: threading.Thread | None = None


#: run_id → live local run. In-process only: ``run_solver`` and the cancel
#: chain are co-located in the agent process (the deployed topology). The
#: supervisor pops its entry when the completion.json is written.
_LOCAL_RUNS: dict[str, _LocalRun] = {}


def _expand_local_outputs(patterns: list[str], rundir: Path) -> list[Path]:
    """Glob-expand the manifest ``outputs[]`` in the rundir -- mirrors the
    entrypoints' ``_expand_outputs`` (files only, de-duplicated, sorted).
    ``recursive=True`` so ``**`` patterns behave like the SFINCS/MODFLOW
    worker entrypoints (the MODFLOW manifest carries
    ``**/gwt_model.ucn`` / ``**/*.lst`` belt-and-suspenders nets)."""
    seen: set[Path] = set()
    for pat in patterns:
        for hit in _glob.glob(str(rundir / pat), recursive=True):
            p = Path(hit)
            if p.is_file():
                seen.add(p.resolve())
    return sorted(seen)


def _discover_publish_manifest_uri(
    s3: Any, runs_bucket: str, run_id: str
) -> str | None:
    """The ``publish_manifest.json`` a self-S3 worker wrote under the run prefix.

    A container that reaches the object store itself writes both its manifests
    and its own completion.json; the supervisor's completion write lands LAST and
    overwrites the worker's. Without this probe the pointer dies with it and
    ``read_publish_manifest`` -- which requires the pointer, never globs -- hands
    every consumer an empty metrics carrier. Returns ``None`` when the worker
    wrote no manifest (the mounted-rundir specs).
    """
    key = f"{run_id}/publish_manifest.json"
    try:
        s3.head_object(Bucket=runs_bucket, Key=key)
    except Exception:  # noqa: BLE001 -- absent or unreadable: no pointer to add
        return None
    return f"s3://{runs_bucket}/{key}"


def _write_local_completion(
    s3: Any,
    *,
    runs_bucket: str,
    run_id: str,
    status: str,
    exit_code: int,
    output_uris: list[str],
    stdout_uri: str | None,
    stderr_uri: str | None,
    started_at: str,
    error: str | None,
    stdout_uri_field: str = "sfincs_stdout_uri",
    stderr_uri_field: str = "sfincs_stderr_uri",
    extra: dict[str, Any] | None = None,
    solver: str | None = None,
    code: dict[str, Any] | None = None,
) -> None:
    """Write ``s3://<runs_bucket>/<run_id>/completion.json`` -- EXACT
    worker-entrypoint schema (the ``wait_for_completion`` terminal signal).

    the stdout/stderr field names + an ``extra`` field dict are
    spec-driven so the MODFLOW completion carries ``mf6_stdout_uri`` /
    ``mf6_stderr_uri`` / ``converged`` / ``model_crs`` exactly like
    ``workers/modflow/entrypoint.py``; the SFINCS defaults are
    byte-identical.

    V&V wave: ``solver`` is the lowercase engine identifier
    (``run.spec.solver``) recorded so ``read_run_diagnostics`` can resolve the
    engine directly instead of inferring it from the stdout field name. It is
    inserted immediately after ``exit_code`` (before the spec's ``extra`` fold;
    no engine spec's ``extra`` carries a ``solver`` key, so it is never
    clobbered) and is forward-only: legacy completion.json objects lack it, so
    the reader falls back to the stdout-field-name inference.

    ``publish_manifest_uri`` is discovered from the run prefix when the spec did
    not supply one, so a self-S3 worker's manifest pointer survives this write
    overwriting the worker's own completion.json.
    """
    payload = {
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        "solver": solver,
        **(code or {}),
        **(extra or {}),
        stdout_uri_field: stdout_uri,
        stderr_uri_field: stderr_uri,
        "output_uris": output_uris,
        "started_at": started_at,
        "finished_at": _utc_now_iso(),
        "error": error,
    }
    if not payload.get("publish_manifest_uri"):
        manifest_uri = _discover_publish_manifest_uri(s3, runs_bucket, run_id)
        if manifest_uri is not None:
            payload["publish_manifest_uri"] = manifest_uri
    s3.put_object(
        Bucket=runs_bucket,
        Key=f"{run_id}/completion.json",
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    logger.info(
        "local-docker wrote completion -> s3://%s/%s/completion.json (status=%s)",
        runs_bucket,
        run_id,
        status,
    )


def _supervise_local_run(run: _LocalRun) -> None:
    """Supervisor body (daemon thread): wait on the solver process, upload
    stdout/stderr + glob-expanded outputs to the S3 runs prefix, and ALWAYS
    write completion.json -- even on crash (status="error") or cancel
    (status="cancelled"). Mirrors the entrypoints' best-effort discipline:
    no upload failure may prevent the terminal completion write."""
    status = "error"
    exit_code = 1
    error_msg: str | None = None
    output_uris: list[str] = []
    stdout_uri: str | None = None
    stderr_uri: str | None = None
    completion_extra: dict[str, Any] = {}

    try:
        exit_code = run.proc.wait()
        # Solver-specific post-exit classification first (the
        # MODFLOW spec's mfsim.lst convergence guard); the plain exit-code
        # rule otherwise (SFINCS). A user cancel
        # overrides either verdict below.
        if run.spec.classify_exit is not None:
            try:
                status, exit_code, error_msg, completion_extra = (
                    run.spec.classify_exit(run.rundir, exit_code)
                )
            except Exception as exc:  # noqa: BLE001 -- classifier must not kill the write
                logger.exception(
                    "local classify_exit failed run_id=%s", run.run_id
                )
                status = "error"
                error_msg = f"classify_exit raised {type(exc).__name__}: {exc}"
        elif exit_code == 0:
            status = "ok"
            error_msg = None
        else:
            status = "error"
            error_msg = f"{run.spec.solver} exited with non-zero code {exit_code}"
        if run.cancel_requested.is_set():
            status = "cancelled"
            error_msg = (
                "run cancelled (docker kill via Invariant-8 cancel chain)"
                if run.spec.exec_kind == "docker"
                else "run cancelled (process-group kill via Invariant-8 cancel chain)"
            )
    except Exception as exc:  # noqa: BLE001 -- defensive: wait() itself failed
        logger.exception("local-docker supervisor wait failed run_id=%s", run.run_id)
        status = "error"
        error_msg = f"{type(exc).__name__}: {exc}"

    try:
        s3 = _get_s3_client()
    except Exception as exc:  # noqa: BLE001 -- no client ⇒ nothing more we can do
        logger.error(
            "local-docker supervisor could not build S3 client run_id=%s: %s "
            " -- completion.json NOT written (poller will time out)",
            run.run_id,
            exc,
        )
        _LOCAL_RUNS.pop(run.run_id, None)
        return

    # Always upload stdout/stderr (entrypoint parity -- evidence even on error).
    try:
        if run.stdout_path.exists():
            stdout_uri = _upload_file_s3(
                s3,
                run.stdout_path,
                run.runs_bucket,
                f"{run.run_id}/{run.spec.stdout_name}",
            )
        if run.stderr_path.exists():
            stderr_uri = _upload_file_s3(
                s3,
                run.stderr_path,
                run.runs_bucket,
                f"{run.run_id}/{run.spec.stderr_name}",
            )
    except Exception as exc:  # noqa: BLE001 -- best-effort
        logger.warning(
            "local-docker stdout/stderr upload failed run_id=%s: %s", run.run_id, exc
        )

    try:
        for path in _expand_local_outputs(run.output_patterns, run.rundir):
            rel = path.relative_to(run.rundir).as_posix()
            uri = _upload_file_s3(s3, path, run.runs_bucket, f"{run.run_id}/{rel}")
            output_uris.append(uri)
    except Exception as exc:  # noqa: BLE001 -- reflect, but still write completion
        logger.exception(
            "local-docker output upload failed run_id=%s: %s", run.run_id, exc
        )
        if status == "ok":
            status = "error"
            error_msg = f"output upload to s3://{run.runs_bucket}/{run.run_id}/ failed: {exc}"

    try:
        _write_local_completion(
            s3,
            runs_bucket=run.runs_bucket,
            run_id=run.run_id,
            status=status,
            exit_code=exit_code,
            output_uris=output_uris,
            stdout_uri=stdout_uri,
            stderr_uri=stderr_uri,
            started_at=run.started_at,
            error=error_msg,
            stdout_uri_field=run.spec.stdout_uri_field,
            stderr_uri_field=run.spec.stderr_uri_field,
            extra=completion_extra,
            solver=run.spec.solver,
            code=run.code,
        )
    except Exception:  # noqa: BLE001 -- terminal-signal write failed; log loudly
        logger.exception(
            "local-docker completion.json write FAILED run_id=%s -- "
            "wait_for_completion will hit its timeout",
            run.run_id,
        )
    finally:
        _LOCAL_RUNS.pop(run.run_id, None)


def launch_local_solver(
    spec: LocalSolverSpec,
    model_setup_uri: str,
    *,
    run_id: str | None = None,
    compute_class: str = "medium",
) -> ExecutionHandle:
    """Generic local-backend launcher (envelope, spec seam).

    Non-blocking -- mirrors the Cloud Workflows submit semantics: stage the
    manifest's inputs from the object store, launch the solver detached
    (``spec.build_argv`` -- a ``docker run`` line or a direct binary), hand the
    supervisor to a daemon thread, return the ``ExecutionHandle`` immediately.

    Args:
        spec: the solver-specific knobs (see ``LocalSolverSpec``).
        model_setup_uri: ``s3://`` / ``gs://`` / ``file://`` URI of the
            worker-contract manifest; input URIs inside resolve by scheme.
        run_id: optional pre-minted run id (the MODFLOW deck is staged under
            ``modflow/<run_id>/`` BEFORE submit, so its run_id must flow
            through -- parity with the ``{run_id, manifest_uri}`` workflow
            argument). Minted fresh when ``None`` (the SFINCS path).
        compute_class: class, alias-mapped onto the schema literal.
    """
    if not (
        model_setup_uri.startswith("s3://")
        or model_setup_uri.startswith("gs://")
        or model_setup_uri.startswith("file://")
    ):
        raise SolverDispatchError(
            f"model_setup_uri must be an s3:// / gs:// / file:// URI under "
            f"the local-docker backend; got {model_setup_uri!r}"
        )
    schema_compute_class = COMPUTE_CLASS_ALIAS.get(compute_class)
    if schema_compute_class is None:
        raise SolverDispatchError(
            f"compute_class {compute_class!r} not recognized; allowed: "
            f"{sorted(COMPUTE_CLASS_ALIAS)}"
        )
    runs_bucket = _get_local_runs_bucket()  # fail fast on missing env

    run_id = run_id or new_ulid()
    submitted_at = datetime.now(timezone.utc)
    rundir = (
        Path(os.environ.get("TRID3NT_RUNS_DIR") or DEFAULT_LOCAL_RUNS_DIR) / run_id
    )
    rundir.mkdir(parents=True, exist_ok=True)

    # --- Manifest read + input staging (the entrypoint's download phase) ---
    try:
        manifest = json.loads(_read_object_bytes(model_setup_uri))
    except SolverDispatchError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SolverDispatchError(
            f"local-docker manifest read failed {model_setup_uri}: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise SolverDispatchError(
            f"manifest at {model_setup_uri} must be a JSON object"
        )
    inputs = manifest.get("inputs", []) or []
    solver_args = [str(a) for a in (manifest.get(spec.args_key, []) or [])]
    output_patterns = [str(p) for p in (manifest.get("outputs", []) or [])]

    # Write the manifest to rundir/manifest.json so subprocess-runner specs
    # (landlab, openquake) can pass a file:// URI to their worker entrypoints
    # without requiring a separate S3 read from the subprocess. This is a
    # no-op for docker/exec specs that do not use the manifest URI at runtime
    # (SFINCS passes sfincs_args; MODFLOW passes mf6_args; SWMM passes inp path).
    # WHICH CODE is dispatching. It lands BESIDE the manifest rather than inside
    # it: manifest.json is the worker's input contract and several entrypoints
    # gate it strictly, so run provenance goes in its own file. The run record
    # carries the same values into completion.json.
    from .code_provenance import code_identity

    code = code_identity()
    try:
        (rundir / "code_provenance.json").write_text(
            json.dumps(code, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 -- a provenance note never fails a run
        logger.warning("local-docker could not write code_provenance.json to %s: %s",
                       rundir, exc)
    manifest_rundir_path = rundir / "manifest.json"
    try:
        manifest_rundir_path.write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 - non-fatal; subprocess specs re-read from original URI on failure
        logger.warning(
            "local-docker could not write manifest.json to rundir %s: %s "
            "(subprocess specs that rely on file:// will fail)",
            rundir,
            exc,
        )

    rundir_resolved = rundir.resolve()
    for item in inputs:
        try:
            input_uri = item["gs_uri"]  # legacy field NAME; value resolved by scheme
            dest_rel = item["dest"]
        except (TypeError, KeyError) as exc:
            raise SolverDispatchError(
                f"manifest input entry malformed (need gs_uri + dest): {item!r}"
            ) from exc
        dest = rundir / dest_rel
        # Host-side path-traversal guard: staging writes to the instance
        # filesystem, so a dest that climbs out of the rundir is refused.
        if rundir_resolved not in dest.resolve().parents:
            raise SolverDispatchError(
                f"manifest input dest escapes the rundir: {dest_rel!r}"
            )
        try:
            _download_object(input_uri, dest)
        except SolverDispatchError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SolverDispatchError(
                f"local-docker input staging failed {input_uri} -> {dest}: {exc}"
            ) from exc

    # --- Detached launch (docker: container name == run_id is the cancel
    # seam; exec: the detached process group is -- start_new_session=True
    # makes pgid == pid for os.killpg) ---
    stdout_path = rundir / spec.stdout_name
    stderr_path = rundir / spec.stderr_name
    cmd = spec.build_argv(run_id, rundir, solver_args)
    cmd = _with_declared_network(spec, cmd)
    logger.info("local-%s exec: %s", spec.exec_kind, " ".join(cmd))
    # Build the subprocess environment: start from the current process env and
    # merge any spec-level overrides (e.g. PYTHONPATH for pip-only workers that
    # use ``workers.*`` imports from the repo root).
    proc_env: dict[str, str] | None = None
    if spec.env_overrides:
        import copy as _copy
        proc_env = _copy.copy(os.environ.copy())
        proc_env.update(spec.env_overrides)

    try:
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            proc = subprocess.Popen(  # noqa: S603 -- argv list, no shell
                cmd,
                stdout=out,
                stderr=err,
                cwd=str(rundir),
                start_new_session=True,  # detach from the agent's signal group
                env=proc_env,  # None = inherit parent env (default / SFINCS / MODFLOW)
            )
    except Exception as exc:  # noqa: BLE001 -- docker/solver binary missing, etc.
        raise SolverDispatchError(
            f"local-{spec.exec_kind} launch failed ({' '.join(cmd[:6])} ...): {exc}"
        ) from exc

    run = _LocalRun(
        run_id=run_id,
        rundir=rundir,
        runs_bucket=runs_bucket,
        proc=proc,
        output_patterns=output_patterns,
        started_at=_utc_now_iso(),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        spec=spec,
        code=code,
    )
    _LOCAL_RUNS[run_id] = run
    supervisor = threading.Thread(
        target=_supervise_local_run,
        args=(run,),
        name=f"{spec.solver}-local-supervisor-{run_id}",
        daemon=True,
    )
    run.supervisor = supervisor
    supervisor.start()

    handle = ExecutionHandle(
        handle_id=new_ulid(),
        run_id=run_id,
        solver=spec.solver,
        compute_class=schema_compute_class,  # type: ignore[arg-type]
        workflows_execution_id=f"{spec.workflow_name}:{run_id}",
        workflow_name=spec.workflow_name,
        workflow_location=LOCAL_DOCKER_WORKFLOW_LOCATION,
        submitted_at=submitted_at,
    )
    logger.info(
        "local-%s submitted run_id=%s handle_id=%s argv0=%s inputs=%d",
        spec.exec_kind,
        run_id,
        handle.handle_id,
        cmd[0] if cmd else "?",
        len(inputs),
    )
    return handle


def _run_solver_local_docker(
    solver: str, model_setup_uri: str, compute_class: str
) -> ExecutionHandle:
    """``run_solver`` body under ``TRID3NT_SOLVER_BACKEND=local-docker``.

    Every solver is looked up in ``LOCAL_SOLVER_SPEC_REGISTRY``, which its own
    workflow module fills at import time (deferred via callables, so a spec can
    name its image and its exit classifier without importing this module back).
    A solver with no entry raises ``SolverDispatchError`` rather than borrowing
    somebody else's spec: a wrong-engine dispatch is worse than a loud failure.
    """
    factory = LOCAL_SOLVER_SPEC_REGISTRY.get(solver)
    if factory is not None:
        try:
            spec = factory()
        except Exception as exc:  # noqa: BLE001
            raise SolverDispatchError(
                f"local-docker spec factory for solver {solver!r} raised "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return launch_local_solver(spec, model_setup_uri, compute_class=compute_class)
    raise SolverDispatchError(
        f"solver {solver!r} has no LOCAL_SOLVER_SPEC_REGISTRY entry -- its "
        "workflow module must call register_local_solver_spec() (never a "
        "wrong-spec dispatch)"
    )


# --------------------------------------------------------------------------- #
# Per-solver local-spec registry -- EVERY solver, no exceptions.
#
# Maps solver name -> callable returning a LocalSolverSpec. The callable form
# (factory, not a pre-built spec) avoids circular imports: each workflow module
# registers itself at import time via register_local_solver_spec(), and the
# factory is only CALLED inside _run_solver_local_docker, by which time the
# module is fully loaded. Engines with a public image use exec_kind="docker";
# pip-only engines with none use exec_kind="exec".
# --------------------------------------------------------------------------- #

#: solver name -> zero-arg callable returning a LocalSolverSpec.
LOCAL_SOLVER_SPEC_REGISTRY: dict[str, Any] = {}


def register_local_solver_spec(solver: str, factory: Any) -> None:
    """Register a per-solver LocalSolverSpec factory for the local-docker backend.

    Call at module import time from each workflow module that owns a pip-only
    engine (e.g. ``run_swmm``, ``run_landlab``, ``model_openquake_psha``).
    The factory is a zero-arg callable returning a fresh ``LocalSolverSpec``
    instance (deferred construction avoids circular imports -- the solver module
    is partially loaded when it first registers). Idempotent: a second call with
    the same key overwrites silently (the last writer wins, which is harmless
    since all callers build the same spec).

    Args:
        solver: lowercase solver identifier (must match ``SOLVER_WORKFLOW_REGISTRY``).
        factory: ``() -> LocalSolverSpec`` -- called inside
            ``_run_solver_local_docker`` at dispatch time.
    """
    LOCAL_SOLVER_SPEC_REGISTRY[solver] = factory


def _docker_kill(run_id: str) -> None:
    """Best-effort ``docker kill <run_id>`` (container name == run_id)."""
    try:
        proc = subprocess.run(  # noqa: S603 -- argv list, no shell
            ["docker", "kill", run_id],
            capture_output=True,
            timeout=DOCKER_KILL_TIMEOUT_S,
            check=False,
        )
        logger.info(
            "docker kill %s rc=%d stderr=%s",
            run_id,
            proc.returncode,
            proc.stderr.decode(errors="replace").strip()[:200],
        )
    except Exception as exc:  # noqa: BLE001 -- cancel chain still propagates
        logger.warning("docker kill %s raised %s", run_id, exc)


def _killpg_local_run(run: _LocalRun) -> None:
    """Best-effort SIGKILL to the detached process group of an exec-kind run
    (``start_new_session=True`` at launch makes pgid == pid)."""
    try:
        os.killpg(run.proc.pid, signal.SIGKILL)
        logger.info("killpg(%d) issued for run_id=%s", run.proc.pid, run.run_id)
    except ProcessLookupError:
        logger.info(
            "killpg for run_id=%s: process group already gone", run.run_id
        )
    except Exception as exc:  # noqa: BLE001 -- cancel chain still propagates
        logger.warning("killpg for run_id=%s raised %s", run.run_id, exc)


def _kill_local_run(run_id: str) -> None:
    """Kind-aware best-effort kill: exec-kind runs get a
    process-group SIGKILL; docker-kind (and unknown -- e.g. after an agent
    restart, where ``docker kill`` against the container name is the only
    remaining lever) get ``docker kill <run_id>``."""
    run = _LOCAL_RUNS.get(run_id)
    if run is not None and run.spec.exec_kind == "exec":
        _killpg_local_run(run)
        return
    if run is None:
        logger.warning(
            "local kill for unknown run_id=%s (no in-process supervisor); "
            "issuing docker kill only -- an exec-kind run cannot be reached "
            "after an agent restart (OQ-291-LOCAL-CANCEL-CROSS-PROCESS)",
            run_id,
        )
    _docker_kill(run_id)


def _request_local_cancel(run_id: str) -> None:
    """Invariant-8 local cancel: flag the run cancelled, then kill the
    container / process group (kind-aware). The supervisor wakes
    on process exit and writes the status="cancelled" completion.json --
    terminal within ≤30 s."""
    run = _LOCAL_RUNS.get(run_id)
    if run is not None:
        run.cancel_requested.set()
    _kill_local_run(run_id)


def _try_get_completion_s3(runs_bucket: str, run_id: str) -> dict[str, Any] | None:
    """Poll ``s3://<runs_bucket>/<run_id>/completion.json`` once.

    Returns the parsed manifest, ``None`` when the object is not there yet
    (or on a transient read error -- the timeout catches persistent faults,
    mirroring the Workflows-poll resilience). Malformed JSON raises
    ``SolverDispatchError`` (S3 PUTs are atomic, so a parse failure is real
    corruption, not a partial write).
    """
    s3 = _get_s3_client()
    try:
        resp = s3.get_object(Bucket=runs_bucket, Key=f"{run_id}/completion.json")
        data = resp["Body"].read()
    except Exception as exc:  # noqa: BLE001
        code = ""
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            code = str(response.get("Error", {}).get("Code", ""))
        if code in ("NoSuchKey", "404", "NoSuchBucket"):
            return None
        logger.warning(
            "local-docker completion poll degraded s3://%s/%s/completion.json: %s; "
            "will retry next poll",
            runs_bucket,
            run_id,
            exc,
        )
        return None
    try:
        manifest = json.loads(data)
    except Exception as exc:  # noqa: BLE001
        raise SolverDispatchError(
            f"completion manifest s3://{runs_bucket}/{run_id}/completion.json "
            f"is not valid JSON: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise SolverDispatchError(
            f"completion manifest s3://{runs_bucket}/{run_id}/completion.json "
            "is not a JSON object"
        )
    return manifest


def _build_local_run_result(
    handle: ExecutionHandle, manifest: dict[str, Any], runs_bucket: str
) -> RunResult:
    """Map a local-docker completion manifest onto a ``RunResult``.

    ``status="ok"`` → ``complete`` with ``output_uri = s3://<runs_bucket>/
    <run_id>/`` (the runs PREFIX, kickoff-pinned -- ``postprocess_flood``
    resolves ``sfincs_map.nc`` inside it); ``"cancelled"`` → ``cancelled``;
    anything else → ``failed`` with the manifest's structured error.
    """
    manifest_status = str(manifest.get("status", "")).lower()
    started_at = _to_utc(manifest.get("started_at"))
    completed_at = _to_utc(manifest.get("finished_at")) or datetime.now(timezone.utc)

    if manifest_status == "ok":
        return RunResult(
            run_id=handle.run_id,
            handle_id=handle.handle_id,
            status="complete",
            output_uri=f"s3://{runs_bucket}/{handle.run_id}/",
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=_duration(started_at, completed_at),
        )
    if manifest_status == "cancelled":
        return RunResult(
            run_id=handle.run_id,
            handle_id=handle.handle_id,
            status="cancelled",
            output_uri=None,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=_duration(started_at, completed_at),
            cancellation_reason=str(
                manifest.get("error") or "local-docker run cancelled"
            ),
        )
    return RunResult(
        run_id=handle.run_id,
        handle_id=handle.handle_id,
        status="failed",
        output_uri=None,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=_duration(started_at, completed_at),
        error_code=_solver_error_code(manifest),
        error_message=str(manifest.get("error") or "solver reported failure"),
    )


async def _wait_for_completion_local(
    handle: ExecutionHandle, poll_interval_s: int, timeout_s: int
) -> RunResult:
    """``wait_for_completion`` body for local-docker handles: poll the
    completion.json object on S3 with the same cadence/timeout/progress-ramp
    semantics as the Cloud Workflows poll."""
    runs_bucket = _get_local_runs_bucket()
    deadline = handle.submitted_at.timestamp() + float(timeout_s)
    loop = asyncio.get_running_loop()

    logger.info(
        "wait_for_completion(local-docker) handle_id=%s run_id=%s "
        "poll_interval=%ds timeout=%ds",
        handle.handle_id,
        handle.run_id,
        poll_interval_s,
        timeout_s,
    )

    try:
        while True:
            manifest = await loop.run_in_executor(
                None, _try_get_completion_s3, runs_bucket, handle.run_id
            )
            now = datetime.now(timezone.utc)

            if manifest is not None:
                if str(manifest.get("status", "")).lower() == "ok":
                    await _emit_progress(PROGRESS_TERMINAL)
                else:
                    await _emit_progress(
                        _progress_percent(handle.submitted_at, now)
                    )
                return _build_local_run_result(handle, manifest, runs_bucket)

            await _emit_progress(_progress_percent(handle.submitted_at, now))

            if now.timestamp() >= deadline:
                logger.warning(
                    "wait_for_completion(local-docker) timed out handle_id=%s "
                    "after %ds; killing container %s",
                    handle.handle_id,
                    timeout_s,
                    handle.run_id,
                )
                # Timeout ≠ user cancel: kill WITHOUT the cancelled flag so the
                # supervisor records status="error" (mirrors the worker path's
                # best-effort cancel + SOLVER_TIMEOUT result). Kind-aware
                #: docker kill or process-group kill.
                await loop.run_in_executor(None, _kill_local_run, handle.run_id)
                return RunResult(
                    run_id=handle.run_id,
                    handle_id=handle.handle_id,
                    status="failed",
                    output_uri=None,
                    started_at=None,
                    completed_at=now,
                    duration_seconds=None,
                    error_code="SOLVER_TIMEOUT",
                    error_message=(
                        f"wait_for_completion exceeded {timeout_s}s budget while "
                        f"polling s3://{runs_bucket}/{handle.run_id}/completion.json"
                    ),
                )

            await asyncio.sleep(poll_interval_s)

    except asyncio.CancelledError:
        # Invariant 8: docker kill + cancelled completion within ≤30 s, then
        # re-raise so emit_tool_call's mark_cancelled branch fires.
        logger.info(
            "wait_for_completion(local-docker) CANCELLED handle_id=%s; "
            "issuing docker kill %s",
            handle.handle_id,
            handle.run_id,
        )
        _request_local_cancel(handle.run_id)
        raise


# --------------------------------------------------------------------------- #
# run_solver
# --------------------------------------------------------------------------- #


_RUN_SOLVER_METADATA = AtomicToolMetadata(
    name="run_solver",
    ttl_class="live-no-cache",
    source_class="solver_dispatch",
    cacheable=False,
)


@register_tool(
    _RUN_SOLVER_METADATA,
    # Annotations: readOnlyHint=False (submits a solver run that ultimately
    # writes output artifacts to the runs bucket), openWorldHint=False
    # (local container / direct binary -- no public external API),
    # destructiveHint=False (writes go to a new runs/ prefix; no existing
    # state overwritten), idempotentHint=False (each call creates a new
    # run with a distinct run_id).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
def run_solver(
    solver: str,
    model_setup_uri: str,
    compute_class: str = "medium",
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> ExecutionHandle:
    """Submit a solver execution to the local Docker solver backend.

    Use this when: the agent has a staged model (e.g. from
    ``set_sfincs_parameters``) and needs to actually run the solver. Returns
    an ``ExecutionHandle`` whose ``workflow_name`` pins the backend and which
    is the Invariant-8 cancellation seam -- feed it to ``wait_for_completion``
    to poll progress and obtain the ``RunResult``.

    Do NOT use this for: cancelling a running execution (use the WS
    ``cancel`` envelope -- the cancel chain reaches the run automatically via
    ``wait_for_completion``'s cancel handler); polling a running execution
    (use ``wait_for_completion``); inspecting a completed run's outputs
    (those land in ``RunResult.output_uri``).

    Params:
        solver: lowercase solver identifier. v0.1 supports ``"sfincs"``
            only; other values raise ``SolverNotRegisteredError`` (other
            solvers land per lazy per-milestone deploy).
        model_setup_uri: ``s3://`` URI of the manifest the solver envelope
            reads (``{"inputs":[...], "sfincs_args":[...], "outputs":[...]}``);
            input URIs inside are resolved by scheme. The
            ``model_flood_scenario`` workflow composes this from the atomic
            tool substrate.
        compute_class: compute class -- selects the sizing bucket
            (small/standard/large/xlarge/gpu). Default ``"medium"``.

    Returns:
        ``ExecutionHandle{handle_id, run_id, solver, compute_class,
        workflows_execution_id, workflow_name, workflow_location,
        submitted_at}`` -- the Invariant-8 cancellation contract. The
        ``workflow_name`` pins the backend (``local-docker`` / ``local-exec``)
        so ``wait_for_completion`` routes correctly.

    This tool is uncacheable-by-construction (solver dispatch is
    explicitly enumerated). The cache shim is NOT invoked.

    Invariant 8 (cancellation): the returned handle carries everything
    ``wait_for_completion`` needs to terminate the live run on the matching
    cancel envelope.

    Raises:
        SolverNotRegisteredError: ``solver`` not in
            ``SOLVER_WORKFLOW_REGISTRY``.
        SolverDispatchError: the backend dispatch failed (docker/binary
            missing, S3 staging error, malformed manifest). The exception is
            re-raised so the emitter classifier surfaces
            ``UPSTREAM_API_ERROR`` to the client.
    """
    if not isinstance(solver, str) or not solver.strip():
        raise SolverNotRegisteredError(
            f"solver must be a non-empty string; got {solver!r}"
        )
    workflow_name = SOLVER_WORKFLOW_REGISTRY.get(solver)
    if workflow_name is None:
        raise SolverNotRegisteredError(
            f"solver {solver!r} not registered for v0.1; supported: "
            f"{sorted(SOLVER_WORKFLOW_REGISTRY)} (lazy per-milestone deploy "
            "per sprint-07 strategy -- TELEMAC / MODFLOW / HEC-HMS land in "
            "their respective milestones)."
        )
    if not isinstance(model_setup_uri, str) or not model_setup_uri:
        raise SolverDispatchError(
            f"model_setup_uri must be a non-empty string; got {model_setup_uri!r}"
        )

    # --- Backend seam: local-docker is the only backend, so dispatch is
    # unconditional. The handle pins its backend (workflow_name=local-docker) so
    # wait_for_completion routes correctly. ---
    return _run_solver_local_docker(
        solver=solver,
        model_setup_uri=model_setup_uri,
        compute_class=compute_class,
    )


# --------------------------------------------------------------------------- #
# wait_for_completion
# --------------------------------------------------------------------------- #


_WAIT_FOR_COMPLETION_METADATA = AtomicToolMetadata(
    name="wait_for_completion",
    ttl_class="live-no-cache",
    source_class="solver_dispatch",
    cacheable=False,
)


def _progress_percent(handle_submitted_at: datetime, now: datetime) -> int:
    """Compute the wall-clock-linear progress estimate clamped to
    ``PROGRESS_CLAMP_MAX`` while the Workflow is still running.

    Invariant 1 (Determinism boundary): this is wall-clock arithmetic, not
    an LLM estimate. The ramp is intentionally simple and conservative --
    a real per-step progress signal would require teaching the SFINCS
    entrypoint to write running progress to ``progress.json`` between
    timesteps, which is a follow-up job.
    """
    elapsed = max(0.0, (now - handle_submitted_at).total_seconds())
    raw = (elapsed / NFR_P_4_TARGET_SECONDS) * 100.0
    capped = min(PROGRESS_CLAMP_MAX, max(0, int(raw)))
    return capped


async def _emit_progress(progress_percent: int) -> None:
    """Push a progress update to the active emitter binding (if any)."""
    binding = _EMITTER_BINDING
    if binding is None:
        return
    try:
        await binding.emitter.update_progress(binding.step_id, progress_percent)
    except Exception as exc:  # noqa: BLE001 -- emission must never fail the poll
        logger.warning("emitter.update_progress raised: %s", exc)


@register_tool(
    _WAIT_FOR_COMPLETION_METADATA,
    # Annotations: readOnlyHint=False (emits pipeline-state progress envelopes
    # as a side effect on every poll tick -- stateful even though it does not
    # write to the object store directly), openWorldHint=False (polls the S3
    # completion.json; no public external API),
    # destructiveHint=False (reads completion.json from the runs bucket; does
    # not overwrite anything), idempotentHint=False (each call emits progress
    # events; cancellation path terminates the live container).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def wait_for_completion(
    handle: ExecutionHandle,
    poll_interval_s: int = DEFAULT_POLL_INTERVAL_S,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> RunResult:
    """Poll the solver run backing ``handle`` until terminal.

    Use this when: the agent has an ``ExecutionHandle`` from ``run_solver``
    and needs the ``RunResult`` (and the ``output_uri``) before continuing
    the pipeline. The tool blocks while the solver runs but is cancellable
    via the WS ``cancel`` chain (Invariant 8 -- see module docstring).

    Do NOT use this for: starting a new run (use ``run_solver``); short,
    synchronous tool calls (atomic tools are sub-second; this is the
    solver-class blocking pattern).

    Params:
        handle: the ``ExecutionHandle`` returned by ``run_solver``. The
            ``workflow_name`` field pins the backend (``local-docker`` /
            ``local-exec``) so the poll routes correctly.
        poll_interval_s: seconds between completion polls. Default 10s --
            matches the <=15-min budget granularity (>=9 polls per run).
        timeout_s: hard ceiling. Defaults to 1800 s (30 min -- gives 2×
            headroom over the target budget). On timeout the tool returns
            ``RunResult{status="failed", error_code="SOLVER_TIMEOUT"}``
            and best-effort cancels the run.

    Returns:
        ``RunResult{run_id, handle_id, status, output_uri?, started_at,
        completed_at, duration_seconds, error_code?, error_message?,
        cancellation_reason?}`` -- terminal outcome. ``status="complete"``
        carries the ``output_uri`` parsed from ``completion.json``;
        ``"failed"`` carries the error code/message; ``"cancelled"``
        carries a ``cancellation_reason``.

    This tool is uncacheable-by-construction. The cache shim is
    NOT invoked.

    Invariant 8 (cancellation): when the M1 WS cancel chain raises
    ``asyncio.CancelledError`` inside this coroutine's poll-sleep, the
    backend handler terminates the live container before
    re-raising so cancellation is initiated within ≤30 s.
    """
    if poll_interval_s < 0:
        raise SolverDispatchError(
            f"poll_interval_s must be non-negative; got {poll_interval_s!r}"
        )
    if timeout_s <= 0:
        raise SolverDispatchError(
            f"timeout_s must be positive; got {timeout_s!r}"
        )

    # --- backend seam: a handle pins its backend (the handle's
    # workflow_name, not the env, decides -- env churn between submit and wait
    # cannot mis-route the poll). ``local-docker`` / ``local-exec`` (MODFLOW
    # direct-binary) share the S3 completion poll. ---
    if handle.workflow_name in _LOCAL_WORKFLOW_NAMES:
        return await _wait_for_completion_local(handle, poll_interval_s, timeout_s)

    raise SolverDispatchError(
        f"unsupported handle backend {handle.workflow_name!r}: "
        f"expected one of {_LOCAL_WORKFLOW_NAMES}."
    )


# --------------------------------------------------------------------------- #
# Result-building helpers
# --------------------------------------------------------------------------- #


def _to_utc(value: Any) -> datetime | None:
    """Coerce a value that may be a ``datetime``, a proto Timestamp, or a
    string into a UTC ``datetime``. Returns ``None`` on failure."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    # Proto Timestamp has a ``ToDatetime`` method.
    to_datetime = getattr(value, "ToDatetime", None)
    if callable(to_datetime):
        try:
            dt = to_datetime()
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def _duration(started_at: datetime | None, completed_at: datetime) -> float | None:
    if started_at is None:
        return None
    return max(0.0, (completed_at - started_at).total_seconds())


def _solver_error_code(manifest: dict[str, Any]) -> str:
    """Map a completion-manifest error to an open-set A.6 SCREAMING_SNAKE_CASE
    error code. Keep narrow; the catch-all bucket is ``SOLVER_FAILED``.

    Surfaced - when lands more
    solver-specific failure modes (SFINCS_MASS_BALANCE_DIVERGED,
    MODEL_DECK_INVALID, etc.) the registry expands here.

    Heavy-compute offload: the combined build+solve worker writes an explicit
    ``error_code`` into completion.json (e.g. ``HYDROMT_BUILD_FAILED``,
    ``LULC_MAPPING_MISMATCH``, ``RUN_OUTPUT_EMPTY``) so a BUILD-phase failure
    surfaces the SAME typed code the in-agent build produced. Prefer it when
    present; otherwise fall back to the generic ``SOLVER_FAILED`` bucket.
    """
    explicit = manifest.get("error_code")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return "SOLVER_FAILED"
