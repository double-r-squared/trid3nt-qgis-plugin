"""The engine: its image, its verdict on an exit, and the one run path.

Status is the worker's exit code AND mf6's own normal-termination sentence
together: a clean process that never finished is an error. The worker runs
``--network none``, so everything it reads is staged. The wait is a CEILING
rather than an estimate - mf6 solves a staged DISV in seconds, and TDIS-based
sizing belongs to the author that will exist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from trid3nt_server.store import objects as storage
from trid3nt_server.workflows.solver.solver import (
    LOCAL_DOCKER_WORKFLOW_NAME,
    LocalSolverSpec,
    dispatch_and_wait,
    register_local_solver_spec,
)
from trid3nt_server.workflows.solver.diagnostics import register_diagnostics_parser

from .diagnostics import parse_modflow
from .errors import ModflowError

logger = logging.getLogger("trid3nt_server.workflows.modflow.engine")

__all__ = ["MODFLOW_SOLVER_NAME", "classify_exit", "read_run_metrics",
           "register_modflow_solver", "solve_case"]

#: The solver identifier IS the engine name: one registration per engine, keyed
#: in ``LOCAL_SOLVER_SPEC_REGISTRY``. Which MODULE ran is the manifest's
#: ``case.module``, which the worker states back in its metrics.
MODFLOW_SOLVER_NAME: str = "modflow"

#: Default worker image (override via env TRID3NT_MODFLOW_IMAGE).
DEFAULT_MODFLOW_IMAGE: str = "trid3nt-local/modflow:latest"

#: The metrics filename the worker writes into the mounted rundir and uploads
#: under the run prefix: the exit classifier reads the first, a reader of a
#: finished run the second.
_METRICS_FILENAME: str = "modflow_metrics.json"

#: The ceiling on the completion wait. A staged DISV solves in seconds; a
#: wedged container becomes a typed failure at the ceiling rather than a
#: container nobody reaps.
_WAIT_CEILING_S: float = 7200.0


def _modflow_image() -> str:
    """The MODFLOW worker image every module of the engine runs in."""
    return os.environ.get("TRID3NT_MODFLOW_IMAGE") or DEFAULT_MODFLOW_IMAGE


def _build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
    """The volume-mount launch line. ``args`` (the manifest's optional
    ``modflow_args`` key) is normally empty - the image CMD drives the
    entrypoint - and anything passed is appended after the image."""
    return ["docker", "run", "--rm", "--name", run_id,
            "-v", f"{rundir}:/data", "-w", "/data", _modflow_image(), *args]


def _metrics(rundir: Path) -> dict[str, Any]:
    """What the worker wrote, or ``{}``: a bad metrics file must not kill the write."""
    path = rundir / _METRICS_FILENAME
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
    except Exception as exc:  # noqa: BLE001
        logger.warning("modflow classify_exit: metrics read failed %s: %s",
                       path, exc)
    return {}


def classify_exit(rundir: Path, exit_code: int
                  ) -> tuple[str, int, str | None, dict[str, Any]]:
    """The post-exit verdict -> ``(status, exit_code, error, extra)``.

    The error names the MODULE the worker says it ran; the whole metrics file is
    the completion's extra."""
    metrics = _metrics(rundir)
    label = str(metrics.get("module") or MODFLOW_SOLVER_NAME)
    if exit_code != 0:
        said = str(metrics.get("error")
                   or f"{label} exited with non-zero code {exit_code}")
        return ("error", exit_code, said, metrics)
    if metrics and not bool(metrics.get("correct_end")):
        said = str(metrics.get("error")
                   or f"{label} did not reach normal termination")
        return ("error", 2, said, metrics)
    return "ok", 0, None, metrics


def _spec() -> LocalSolverSpec:
    """The engine's one ``LocalSolverSpec``."""
    return LocalSolverSpec(
        solver=MODFLOW_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="modflow_args",
        build_argv=_build_argv,
        network="none",
        stdout_name=f"{MODFLOW_SOLVER_NAME}.stdout",
        stderr_name=f"{MODFLOW_SOLVER_NAME}.stderr",
        stdout_uri_field=f"{MODFLOW_SOLVER_NAME}_stdout_uri",
        stderr_uri_field=f"{MODFLOW_SOLVER_NAME}_stderr_uri",
        classify_exit=classify_exit,
    )


def register_modflow_solver() -> None:
    """Register the engine in the local-spec registry. Idempotent.

    MODFLOW is local-docker only: the engine lives in the worker image."""
    register_local_solver_spec(MODFLOW_SOLVER_NAME, _spec)
    register_diagnostics_parser(MODFLOW_SOLVER_NAME, parse_modflow)


def read_run_metrics(run_id: str) -> dict[str, Any]:
    """Best-effort read of the run's metrics file; ``{}`` on any miss.

    Uploaded even on a FAILED run, so a worker-side error_code reaches here."""
    try:
        obj = storage.client().get_object(
            Bucket=storage.runs_bucket(), Key=f"{run_id}/{_METRICS_FILENAME}")
        loaded = json.loads(obj["Body"].read().decode("utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:  # noqa: BLE001 -- absence => no typed gate to surface
        logger.info("modflow: run metrics read miss for %s: %s", run_id, exc)
        return {}


def _run_start_iso(run_result: Any) -> str | None:
    """When the solve began; a run with no ``started_at`` states none."""
    started = getattr(run_result, "started_at", None)
    return started.isoformat() if started is not None else None


async def solve_case(*, run: dict[str, Any], cores: int) -> dict[str, Any]:
    """Dispatch the staged case to the worker and wait -> the run handle.

    The returned ``uri`` is the result the ledger replay probes; the UTM zone
    comes from the ASSEMBLER: the worker never learns it."""
    facts = run["case"]["server_facts"]
    run_result, batch_run_id = await dispatch_and_wait(
        solver=MODFLOW_SOLVER_NAME, manifest_uri=run["manifest_uri"],
        cores=cores, module=str(run["case"]["module"]),
        label=str(facts["name"]), timeout_s=_WAIT_CEILING_S,
        grid_resolution_m=facts.get("mesh_size_m"),
        active_cell_count=facts.get("ncells"))
    if run_result is None or run_result.status != "complete":
        raise ModflowError(
            f"the {facts['name']} solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or ''}",
            error_code="MODFLOW_RUN_FAILED")
    metrics = await asyncio.to_thread(read_run_metrics, batch_run_id)
    # No local path travels out of this step: a replayed record must not hand
    # back a temp file; the products step re-downloads from the run prefix.
    return {
        "run_id": batch_run_id,
        "uri": (f"s3://{storage.runs_bucket()}/{batch_run_id}/"
                f"{run['result_basename']}"),
        "utm_epsg": int(facts["utm_epsg"]),
        "metrics": metrics,
        "started_at": _run_start_iso(run_result),
    }


register_modflow_solver()
