"""Solver dispatch atomic tools: ``run_solver`` and ``wait_for_completion``.

Dispatch is a local container run or a direct-binary run on this machine,
selected per solver by its ``LocalSolverSpec``; both tools are uncacheable.
"""

from __future__ import annotations

import asyncio
import glob as _glob
import json
import logging
import os
import signal
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from trid3nt_contracts import new_ulid
from trid3nt_contracts.execution import ExecutionHandle, RunResult
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server import storage
from trid3nt_server.storage import StorageError
from trid3nt_server.tools import register_tool

from .compute_class import COMPUTE_CLASS_ALIAS

__all__ = [
    "run_solver",
    "wait_for_completion",
    "SolverNotRegisteredError",
    "SolverDispatchError",
    "RunOutputMissing",
    "set_emitter_binding",
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
    "dispatch_and_wait",
    "download_result",
]

logger = logging.getLogger("trid3nt_server.workflows.solver.solver")




#: Target run-time budget the progress ramp is linear in: progress is
#: (now - submitted_at) / target, and nothing else.
NFR_P_4_TARGET_SECONDS: float = 900.0

#: Default poll cadence, matched to the target budget granularity.
DEFAULT_POLL_INTERVAL_S: int = 10

#: Default overall timeout, roughly twice the target budget. Env-overridable via
#: ``TRID3NT_SOLVER_TIMEOUT_S`` so a legitimately long solve gets headroom without
#: touching a call site; an absent or unparseable value falls back to the default.
def _default_timeout_s() -> int:
    raw = (os.environ.get("TRID3NT_SOLVER_TIMEOUT_S") or "").strip()
    try:
        v = int(raw)
        return v if v > 0 else 1800
    except ValueError:
        return 1800


DEFAULT_TIMEOUT_S: int = _default_timeout_s()

#: Highest progress ever advertised before the run reports success: the clamp is
#: what stops a late run reading as finished on the strength of an estimate.
PROGRESS_CLAMP_MAX: int = 95

#: Final progress, written only when the run actually reports success.
PROGRESS_TERMINAL: int = 100


#: Solver -> workflow name registry, consumed purely as a PRESENCE GATE: an
#: unregistered solver raises, and the backend routing comes from the handle's
#: pinned sentinel rather than from this value. Every entry is contributed at
#: import by the engine that owns it, beside that engine's ``LocalSolverSpec``,
#: so a solver named here without a spec behind it cannot happen.
SOLVER_WORKFLOW_REGISTRY: dict[str, str] = {}


# --- Solver backend seam --- #

#: The container backend: ``docker run`` on this machine, with the staging and
#: upload envelope living in this module.
SOLVER_BACKEND_LOCAL_DOCKER: str = "local-docker"

#: ``ExecutionHandle.workflow_name`` sentinel for container handles. The poll
#: dispatches on it, so env churn between submit and wait cannot mis-route it.
LOCAL_DOCKER_WORKFLOW_NAME: str = "local-docker"

#: ``ExecutionHandle.workflow_name`` sentinel for image-less runs that exec a
#: solver binary directly. Same poll loop; the cancel chain kills the detached
#: process group instead of a container.
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
#: the 30 s cancellation-budget envelope.
DOCKER_KILL_TIMEOUT_S: float = 25.0


class SolverNotRegisteredError(ValueError):
    """``solver`` is not in ``SOLVER_WORKFLOW_REGISTRY``.
    Its own type, distinct from a params-invalid error, so the agent surface can
    say which solvers ARE registered rather than blaming the arguments."""


class SolverDispatchError(StorageError):
    """The backend dispatch or the completion-manifest read failed.
    The ``error_code`` attribute carries the typed code, so a downstream wrapper
    re-emits it verbatim rather than re-deriving one."""

    error_code: str = "SOLVER_DISPATCH_FAILED"


class RunOutputMissing(SolverDispatchError):
    """A completed run's named artifact was not downloadable from its prefix."""

    error_code: str = "RUN_OUTPUT_MISSING"




@dataclass(frozen=True)
class EmitterBinding:
    """The ``(emitter, step_id)`` pair the active ``wait_for_completion`` drives its
    progress emissions through. The caller binds it around each call and clears it
    after; an unbound invocation emits nothing rather than guessing a step."""

    emitter: Any
    step_id: str


_EMITTER_BINDING: EmitterBinding | None = None


def set_emitter_binding(binding: EmitterBinding | None) -> None:
    """Bind the active ``(emitter, step_id)`` pair for progress emission.
    ``None`` clears it, and the polling loop falls back to no-op emission."""
    global _EMITTER_BINDING
    _EMITTER_BINDING = binding


# The local backend envelope, one shape for every solver: stage the manifest's
# inputs from the object store, launch the solver DETACHED, hand a supervisor
# thread the process, upload the outputs and ALWAYS write completion.json, poll
# that object from the caller, and cancel by killing the container or the process
# group. Only the knobs in ``LocalSolverSpec`` differ between solvers.


def _utc_now_iso() -> str:
    """ISO8601-Z timestamp, the format completion.json records times in."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_object_bytes(uri: str) -> bytes:
    """Read one object's bytes, resolved BY SCHEME: ``s3://`` via boto3, a
    ``file://`` or bare local path through the filesystem."""
    if uri.startswith("file://"):
        return Path(uri[len("file://"):]).read_bytes()
    if not uri.startswith("s3://"):
        return Path(uri).read_bytes()
    _scheme, bucket, key = storage.split_object_uri(uri)
    resp = storage.client().get_object(Bucket=bucket, Key=key)
    return resp["Body"].read()


def _download_object(uri: str, dest: Path) -> None:
    """Download one staged input to ``dest``, resolved by scheme."""
    # Dispatch on the URI SCHEME, never on the manifest field name: the input
    # entries are keyed ``gs_uri`` while the value is an ``s3://`` URI.
    dest.parent.mkdir(parents=True, exist_ok=True)
    if uri.startswith("file://") or not uri.startswith("s3://"):
        src = Path(uri[len("file://"):] if uri.startswith("file://") else uri)
        dest.write_bytes(src.read_bytes())
        return
    _scheme, bucket, key = storage.split_object_uri(uri)
    logger.info("local-docker staging %s -> %s", uri, dest)
    resp = storage.client().get_object(Bucket=bucket, Key=key)
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
    """Everything the stage / launch / supervise / complete envelope cannot know:
    the launch argv, the artifact and completion field names, the cancel kind, and
    an optional post-exit classifier that a user cancel always overrides."""

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
    """Environment variables merged into the subprocess env; ``None`` inherits the
    parent env unchanged. A value REPLACES the matching key, so a prepend pattern
    must be assembled by the spec factory from the current env value."""

    network: str | None = None
    """The docker network this solver's container runs on. ``"none"`` is the
    ENGINE-ROOM posture - a fully staged run directory and nothing reachable - and
    is per-spec; a spec whose ``build_argv`` writes its own must leave this unset."""


def _with_declared_network(spec: LocalSolverSpec, cmd: list[str]) -> list[str]:
    """Apply the spec's declared docker network to a launch line.
    A spec that writes its own ``--network`` is refused rather than doubled: two of
    them on one command line is a launch failure."""
    # The flag goes in here rather than in each ``build_argv`` because the network a
    # container is allowed is a property of whether its inputs are staged, not of
    # how its argv is spelled, and a posture spread across closures drifts.
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


#: run_id -> live local run. In-process only: ``run_solver`` and the cancel
#: chain are co-located in the agent process (the deployed topology). The
#: supervisor pops its entry when the completion.json is written.
_LOCAL_RUNS: dict[str, _LocalRun] = {}


def _expand_local_outputs(patterns: list[str], rundir: Path) -> list[Path]:
    """Glob-expand the manifest ``outputs[]`` in the rundir: files only,
    de-duplicated, sorted, and ``**`` walked recursively."""
    seen: set[Path] = set()
    for pat in patterns:
        for hit in _glob.glob(str(rundir / pat), recursive=True):
            p = Path(hit)
            if p.is_file():
                seen.add(p.resolve())
    return sorted(seen)


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
    engine: str | None = None,
    code: dict[str, Any] | None = None,
) -> None:
    """Write ``s3://<runs_bucket>/<run_id>/completion.json``, the terminal signal
    ``wait_for_completion`` polls for. The stdout/stderr field names and the
    ``extra`` fold are spec-driven, so each solver writes its own key set."""
    # The envelope's own fields land AFTER the classifier's fold: the fold is
    # whatever the worker measured, and where the two share a name the
    # supervisor's answer is the one a reader of the terminal signal needs.
    # ``engine`` is recorded so a reader resolves the engine directly instead of
    # inferring it from a stdout field name.
    payload = {
        **(code or {}),
        **(extra or {}),
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        "engine": engine,
        stdout_uri_field: stdout_uri,
        stderr_uri_field: stderr_uri,
        "output_uris": output_uris,
        "started_at": started_at,
        "finished_at": _utc_now_iso(),
        "error": error,
    }
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
    stdout/stderr and the expanded outputs, and ALWAYS write completion.json -
    on crash and on cancel too. No upload failure may prevent that write."""
    status = "error"
    exit_code = 1
    error_msg: str | None = None
    output_uris: list[str] = []
    stdout_uri: str | None = None
    stderr_uri: str | None = None
    completion_extra: dict[str, Any] = {}

    try:
        exit_code = run.proc.wait()
        # The spec's own post-exit classifier first, the plain exit-code rule
        # otherwise. A user cancel overrides either verdict below.
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
        s3 = storage.client()
    except Exception as exc:  # noqa: BLE001 -- no client, nothing more we can do
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
            engine=run.spec.solver,
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
    """Generic local-backend launcher: stage, launch detached, supervise, return.
    NON-BLOCKING - the handle comes back before the solve finishes; ``run_id`` is
    passed in when the deck was staged under it, and minted here otherwise."""
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
    runs_bucket = storage.local_runs_bucket()  # fail fast on missing env

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

    # The manifest is written to rundir/manifest.json so a subprocess-runner spec
    # can pass a file:// URI to its entrypoint without a second S3 read. A spec
    # that passes its arguments on the command line simply ignores the file.
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
            input_uri = item["gs_uri"]  # the field NAME only; the value is a uri
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
                env=proc_env,  # None inherits the parent env unchanged
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
    """``run_solver`` body on the local backend. Every solver is looked up in
    ``LOCAL_SOLVER_SPEC_REGISTRY``, and one with no entry RAISES rather than
    borrowing another spec: a wrong-engine dispatch is worse than a loud failure."""
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


# Per-solver local-spec registry -- EVERY solver, no exceptions.
#
# Maps solver name -> callable returning a LocalSolverSpec. The callable form
# (factory, not a pre-built spec) avoids circular imports: each workflow module
# registers itself at import time via register_local_solver_spec(), and the
# factory is only CALLED inside _run_solver_local_docker, by which time the
# module is fully loaded. Engines with a public image use exec_kind="docker";
# pip-only engines with none use exec_kind="exec".

#: solver name -> zero-arg callable returning a LocalSolverSpec.
LOCAL_SOLVER_SPEC_REGISTRY: dict[str, Any] = {}


def register_local_solver_spec(solver: str, factory: Any) -> None:
    """Register a per-solver ``LocalSolverSpec`` factory, at import time.
    ``factory`` is a zero-arg callable called at DISPATCH time, so a spec can name
    its image without importing this module back. Idempotent: last writer wins."""
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
    """Kind-aware best-effort kill: an exec-kind run gets a process-group SIGKILL,
    a docker-kind or unknown run gets ``docker kill <run_id>`` - the container name
    being the only lever left once the in-process supervisor is gone."""
    run = _LOCAL_RUNS.get(run_id)
    if run is not None and run.spec.exec_kind == "exec":
        _killpg_local_run(run)
        return
    if run is None:
        logger.warning(
            "local kill for unknown run_id=%s (no in-process supervisor); "
            "issuing docker kill only -- an exec-kind run cannot be reached "
            "after an agent restart",
            run_id,
        )
    _docker_kill(run_id)


def _request_local_cancel(run_id: str) -> None:
    """Flag the run cancelled, then kill the container or process group.
    The supervisor wakes on process exit and writes the ``status="cancelled"``
    completion.json, so the cancel is terminal within the kill budget."""
    run = _LOCAL_RUNS.get(run_id)
    if run is not None:
        run.cancel_requested.set()
    _kill_local_run(run_id)


def _try_get_completion_s3(runs_bucket: str, run_id: str) -> dict[str, Any] | None:
    """Poll ``s3://<runs_bucket>/<run_id>/completion.json`` once. ``None`` when the
    object is absent or transiently unreadable; malformed JSON RAISES, an
    object-store PUT being atomic, so a parse failure is real corruption."""
    s3 = storage.client()
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
    """Map a local completion manifest onto a ``RunResult``.
    ``status="ok"`` yields ``complete`` with ``output_uri`` set to the run PREFIX,
    not a file; ``"cancelled"`` yields ``cancelled``, anything else ``failed``."""
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
    """``wait_for_completion`` body for a local handle: poll the completion.json
    object under the run prefix on the caller's cadence, ramping progress and
    stopping at the timeout."""
    runs_bucket = storage.local_runs_bucket()
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
                # A timeout is not a user cancel: kill WITHOUT the cancelled flag so the
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
        # Kill and write the cancelled completion FIRST, then re-raise, so the
        # caller's cancel branch fires only once the run is actually terminating.
        logger.info(
            "wait_for_completion(local-docker) CANCELLED handle_id=%s; "
            "issuing docker kill %s",
            handle.handle_id,
            handle.run_id,
        )
        _request_local_cancel(handle.run_id)
        raise




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
    # Absorbs kwargs the model invents; the normalizer catches these upstream too.
    **_extra_ignored: Any,
) -> ExecutionHandle:
    """Submit a solver execution to the local solver backend.

    Use this when: an engine template has already staged a model setup and the
    solve has to be dispatched. Returns an ``ExecutionHandle`` that is also the
    cancellation seam - feed it to ``wait_for_completion`` for the ``RunResult``.

    Do NOT use this for: cancelling a running execution (the cancel envelope
    reaches the run through ``wait_for_completion``); polling one (use
    ``wait_for_completion``); inspecting a completed run's outputs (they land in
    ``RunResult.output_uri``).

    Params: ``solver`` a lowercase identifier registered in
    ``SOLVER_WORKFLOW_REGISTRY``; ``model_setup_uri`` the ``s3://`` manifest the
    engine template composed; ``compute_class`` the sizing bucket.

    Returns an ``ExecutionHandle`` whose ``workflow_name`` pins the backend and
    whose ``run_id`` is what a cancel terminates. An unregistered solver raises
    ``SolverNotRegisteredError``; a failed dispatch ``SolverDispatchError``.
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




_WAIT_FOR_COMPLETION_METADATA = AtomicToolMetadata(
    name="wait_for_completion",
    ttl_class="live-no-cache",
    source_class="solver_dispatch",
    cacheable=False,
)


def _progress_percent(handle_submitted_at: datetime, now: datetime) -> int:
    """The wall-clock-linear progress estimate, clamped to ``PROGRESS_CLAMP_MAX``
    while the run is still going. Wall-clock arithmetic, never an estimate from a
    model, and clamped so completion is never advertised before it happens."""
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
    # Absorbs kwargs the model invents; the normalizer catches these upstream too.
    **_extra_ignored: Any,
) -> RunResult:
    """Poll the solver run backing ``handle`` until terminal.

    Use this when: the agent holds an ``ExecutionHandle`` from ``run_solver`` and
    needs the ``RunResult`` (and its ``output_uri``) before continuing. It blocks
    while the solver runs, and is cancellable through the cancel chain.

    Do NOT use this for: starting a new run (use ``run_solver``); short
    synchronous tool calls - atomic tools are sub-second, this is the
    solver-class blocking pattern.

    Params: ``handle`` from ``run_solver`` (its ``workflow_name`` pins the
    backend); ``poll_interval_s``; ``timeout_s``, on which the run is cancelled
    best-effort and a failed ``RunResult`` returns with ``SOLVER_TIMEOUT``.

    Returns the terminal ``RunResult``: ``status="complete"`` carries the
    ``output_uri``, ``"failed"`` an error code and message, ``"cancelled"`` a
    reason. On cancellation the backend terminates the live run BEFORE
    re-raising, so the kill is initiated with the cancel rather than after it.
    """
    if poll_interval_s < 0:
        raise SolverDispatchError(
            f"poll_interval_s must be non-negative; got {poll_interval_s!r}"
        )
    if timeout_s <= 0:
        raise SolverDispatchError(
            f"timeout_s must be positive; got {timeout_s!r}"
        )

    # The HANDLE pins its backend, not the env: env churn between submit and wait
    # cannot mis-route the poll. Both local sentinels share the completion poll.
    if handle.workflow_name in _LOCAL_WORKFLOW_NAMES:
        return await _wait_for_completion_local(handle, poll_interval_s, timeout_s)

    raise SolverDispatchError(
        f"unsupported handle backend {handle.workflow_name!r}: "
        f"expected one of {_LOCAL_WORKFLOW_NAMES}."
    )




async def dispatch_and_wait(*, solver: str, manifest_uri: str, compute_class: str,
                            module: str, label: str, timeout_s: float,
                            grid_resolution_m: float | None = None,
                            active_cell_count: int | None = None
                            ) -> tuple[Any, str]:
    """Dispatch a staged manifest, drive the cards, wait, and hand back the result.

    Judges nothing: a non-complete status is the caller's error to raise."""
    from trid3nt_server.render.pipeline_emitter import (
        current_emitter,
        mint_dispatch_and_sim_cards,
        route_sim_terminal,
    )

    from .solve_progress import drive_live_solve_progress

    emitter = current_emitter()
    handle = run_solver(solver=solver, model_setup_uri=manifest_uri,
                        compute_class=compute_class)
    run_id = handle.run_id
    logger.info("%s dispatching %s -> %s", solver, label, manifest_uri)
    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=solver, module=module, handle=handle,
        compute_class=compute_class)
    if emitter is not None and sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=sim_step_id))
    progress = asyncio.ensure_future(drive_live_solve_progress(
        emitter=emitter, run_id=run_id, solver=solver,
        grid_resolution_m=grid_resolution_m, active_cell_count=active_cell_count,
        vcpus=None, eta_seconds=None))

    run_result = None
    try:
        run_result = await wait_for_completion(handle, timeout_s=timeout_s)
    except asyncio.CancelledError:
        logger.info("%s %s solve cancelled awaiting solver", solver, label)
        await route_sim_terminal(emitter, sim_step_id, run_result=None)
        raise
    finally:
        progress.cancel()
        try:
            await progress
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        set_emitter_binding(None)
    await route_sim_terminal(emitter, sim_step_id, run_result=run_result)
    return run_result, (getattr(run_result, "run_id", None) or run_id)


def download_result(run_id: str, basename: str) -> str:
    """Download one of a run's result files to a local path a postprocess reads.

    The caller owns the temp file: read it, then unlink it."""
    bucket = storage.runs_bucket()
    local = str(Path(tempfile.mkdtemp(prefix=f"run-{run_id}-")) / basename)
    try:
        body = storage.client().get_object(
            Bucket=bucket, Key=f"{run_id}/{basename}")["Body"].read()
        with open(local, "wb") as fh:
            fh.write(body)
    except Exception as exc:  # noqa: BLE001
        raise RunOutputMissing(
            f"run {run_id} completed but s3://{bucket}/{run_id}/{basename} was "
            f"not downloadable: {exc}") from exc
    return local


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
    """Map a completion-manifest error to a SCREAMING_SNAKE_CASE error code.
    A worker's own explicit ``error_code`` wins, so a build-phase failure surfaces
    its own typed code; the catch-all bucket is ``SOLVER_FAILED``."""
    explicit = manifest.get("error_code")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    return "SOLVER_FAILED"
