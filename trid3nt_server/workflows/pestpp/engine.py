"""The engine: its image, its verdict on an exit, and the one run path.

Status is the worker's exit code AND PEST++'s own completion sentence in the
manager's record together. The worker runs ``--network none`` with the manager
and its agents on the container's loopback, so everything a trial reads is
staged. The wait has no time limit: it ends when the container exits or the
user cancels.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from trid3nt_server.store import objects as storage
from trid3nt_server.workflows.solver.solver import (
    LOCAL_DOCKER_WORKFLOW_NAME,
    LocalSolverSpec,
    dispatch_and_wait,
    register_local_solver_spec,
)
from trid3nt_server.workflows.solver.stop import SolveStopped

from .errors import PestppError
from .staging import RECORD_FILENAME

logger = logging.getLogger("trid3nt_server.workflows.pestpp.engine")

__all__ = ["PESTPP_SOLVER_NAME", "classify_exit", "read_run_metrics",
           "register_pestpp_solver", "solve_case"]

#: The solver identifier IS the engine name. Which TOOL ran (ies, sen, glm, ...)
#: is the manifest's ``case.module``, which the worker states back in its metrics.
PESTPP_SOLVER_NAME: str = "pestpp"

#: Default worker image (override via env TRID3NT_PESTPP_IMAGE).
DEFAULT_PESTPP_IMAGE: str = "trid3nt-local/pestpp:latest"

_METRICS_FILENAME: str = "pestpp_metrics.json"


def _pestpp_image() -> str:
    return os.environ.get("TRID3NT_PESTPP_IMAGE") or DEFAULT_PESTPP_IMAGE


def _build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
    """The volume-mount launch line; ``args`` (the manifest's optional
    ``pestpp_args``) is appended after the image."""
    return ["docker", "run", "--rm", "--name", run_id,
            "-v", f"{rundir}:/data", "-w", "/data", _pestpp_image(), *args]


def _metrics(rundir: Path) -> dict[str, Any]:
    """What the worker wrote, or ``{}``: a bad metrics file must not kill the write."""
    path = rundir / _METRICS_FILENAME
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
    except Exception as exc:  # noqa: BLE001
        logger.warning("pestpp classify_exit: metrics read failed %s: %s", path, exc)
    return {}


def classify_exit(rundir: Path, exit_code: int
                  ) -> tuple[str, int, str | None, dict[str, Any]]:
    """The post-exit verdict -> ``(status, exit_code, error, extra)``; the error
    names the TOOL the worker says it ran, and the metrics file is the extra."""
    metrics = _metrics(rundir)
    label = f"pestpp-{metrics['module']}" if metrics.get("module") else PESTPP_SOLVER_NAME
    if exit_code != 0:
        said = str(metrics.get("error") or f"{label} exited with non-zero code {exit_code}")
        return ("error", exit_code, said, metrics)
    if metrics and not bool(metrics.get("correct_end")):
        return ("error", 2, str(metrics.get("error") or f"{label} did not complete"), metrics)
    return "ok", 0, None, metrics


def _spec() -> LocalSolverSpec:
    """The engine's one ``LocalSolverSpec``; its log is the manager's record."""
    return LocalSolverSpec(
        solver=PESTPP_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="pestpp_args",
        build_argv=_build_argv,
        network="none",
        stdout_name=f"{PESTPP_SOLVER_NAME}.stdout",
        stderr_name=f"{PESTPP_SOLVER_NAME}.stderr",
        stdout_uri_field=f"{PESTPP_SOLVER_NAME}_stdout_uri",
        stderr_uri_field=f"{PESTPP_SOLVER_NAME}_stderr_uri",
        classify_exit=classify_exit,
        log_name=RECORD_FILENAME,
    )


def register_pestpp_solver() -> None:
    """Register the engine in the local-spec registry. Idempotent."""
    register_local_solver_spec(PESTPP_SOLVER_NAME, _spec)


def read_run_metrics(run_id: str) -> dict[str, Any]:
    """Best-effort read of the run's metrics file; ``{}`` on any miss."""
    try:
        obj = storage.client().get_object(
            Bucket=storage.runs_bucket(), Key=f"{run_id}/{_METRICS_FILENAME}")
        loaded = json.loads(obj["Body"].read().decode("utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:  # noqa: BLE001 -- absence => no typed gate to surface
        logger.info("pestpp: run metrics read miss for %s: %s", run_id, exc)
        return {}


async def solve_case(*, run: dict[str, Any], cores: int) -> dict[str, Any]:
    """Dispatch the staged study to the worker and wait -> the run handle.

    The returned ``uri`` is the manager's record under the run prefix."""
    name = str(run["case"]["server_facts"]["name"])
    try:
        run_result, batch_run_id = await dispatch_and_wait(
            solver=PESTPP_SOLVER_NAME, manifest_uri=run["manifest_uri"],
            cores=cores, module=str(run["case"]["module"]), label=name,
            sheet=run["sheet"], run_id=run["run_id"])
    except SolveStopped as stop:
        stop.solved = {"run_id": stop.run_id,
                       "uri": f"s3://{storage.runs_bucket()}/{stop.run_id}/{run['result_basename']}",
                       "metrics": {}, "started_at": stop.started_at, "stopped": True}
        raise
    if run_result is None or run_result.status != "complete":
        raise PestppError(
            f"the {name} study did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or ''}",
            error_code="PESTPP_RUN_FAILED")
    metrics = await asyncio.to_thread(read_run_metrics, batch_run_id)
    started = getattr(run_result, "started_at", None)
    return {
        "run_id": batch_run_id,
        "uri": f"s3://{storage.runs_bucket()}/{batch_run_id}/{run['result_basename']}",
        "metrics": metrics,
        "started_at": started.isoformat() if started is not None else None,
    }


register_pestpp_solver()
