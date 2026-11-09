"""The engine: its image, its verdict on an exit, and the one run path.

Status is the worker's exit code AND the CORRECT-END flag together: a clean process that never reached the end is an error.
The worker runs ``--network none``, so everything it reads is staged.
The wait has no time limit: it ends when the container exits or the user cancels.
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

from trid3nt_server.workflows.solver.diagnostics import register_diagnostics_parser

from .diagnostics import parse_telemac
from .errors import TelemacError
from .progress import step_of

logger = logging.getLogger("trid3nt_server.workflows.telemac.engine")

__all__ = ["TELEMAC_SOLVER_NAME", "classify_exit", "read_run_metrics",
           "register_telemac_solver", "solve_case"]

#: The solver identifier IS the engine name: one registration per engine, keyed
#: in ``LOCAL_SOLVER_SPEC_REGISTRY``. Which MODULE ran is the manifest's
#: ``case.module``, which the worker states back in its metrics.
TELEMAC_SOLVER_NAME: str = "telemac"

#: Default worker image (override via env TRID3NT_TELEMAC_IMAGE).
DEFAULT_TELEMAC_IMAGE: str = "trid3nt-local/telemac:latest"

#: The metrics filename the worker writes into the mounted rundir and uploads
#: under the run prefix: the exit classifier reads the first, a reader of a
#: finished run the second.
_METRICS_FILENAME: str = "telemac_metrics.json"

def _telemac_image() -> str:
    """The TELEMAC worker image every module of the engine runs in."""
    return os.environ.get("TRID3NT_TELEMAC_IMAGE") or DEFAULT_TELEMAC_IMAGE


def _build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
    """The volume-mount launch line. ``args`` (the manifest's optional
    ``telemac_args`` key) is normally empty - the image CMD drives the
    entrypoint - and anything passed is appended after the image."""
    return ["docker", "run", "--rm", "--name", run_id,
            "-v", f"{rundir}:/data", "-w", "/data", _telemac_image(), *args]


def _why(metrics: dict[str, Any], fallback: str) -> str:
    """Why the run stopped, with the engine's own demand named where it made one.

    The listing's own sentence, so this side invents no required set."""
    from .modules.listing import engine_demand

    said = str(metrics.get("error") or fallback)
    demand = engine_demand(str(metrics.get("listing_tail") or ""))
    return f"{said} - the engine asked for: {demand}" if demand else said


def _metrics(rundir: Path) -> dict[str, Any]:
    """What the worker wrote, or ``{}``: a bad metrics file must not kill the write."""
    path = rundir / _METRICS_FILENAME
    try:
        if path.exists():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemac classify_exit: metrics read failed %s: %s",
                       path, exc)
    return {}


def classify_exit(rundir: Path, exit_code: int
                  ) -> tuple[str, int, str | None, dict[str, Any]]:
    """The post-exit verdict -> ``(status, exit_code, error, extra)``.

    The error names the MODULE the worker says it ran; the whole metrics file is the completion's extra."""
    metrics = _metrics(rundir)
    label = str(metrics.get("module") or TELEMAC_SOLVER_NAME)
    if exit_code != 0:
        return ("error", exit_code, _why(
            metrics, f"{label} exited with non-zero code {exit_code}"), metrics)
    if metrics and not bool(metrics.get("correct_end")):
        return ("error", 2, _why(
            metrics, f"{label} did not reach CORRECT END OF RUN"), metrics)
    return "ok", 0, None, metrics


def _spec() -> LocalSolverSpec:
    """The engine's one ``LocalSolverSpec``."""
    return LocalSolverSpec(
        solver=TELEMAC_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="telemac_args",
        build_argv=_build_argv,
        network="none",
        stdout_name=f"{TELEMAC_SOLVER_NAME}.stdout",
        stderr_name=f"{TELEMAC_SOLVER_NAME}.stderr",
        stdout_uri_field=f"{TELEMAC_SOLVER_NAME}_stdout_uri",
        stderr_uri_field=f"{TELEMAC_SOLVER_NAME}_stderr_uri",
        classify_exit=classify_exit,
        progress=step_of,
        log_name="full_listing.log",
    )


def register_telemac_solver() -> None:
    """Register the engine in the local-spec registry. Idempotent.

    TELEMAC is local-docker only: the engine lives in the worker image."""
    register_local_solver_spec(TELEMAC_SOLVER_NAME, _spec)
    register_diagnostics_parser(TELEMAC_SOLVER_NAME, parse_telemac)


def read_run_metrics(run_id: str) -> dict[str, Any]:
    """Best-effort read of the run's metrics file; ``{}`` on any miss.

    Uploaded even on a FAILED run, so a worker-side error_code reaches here."""
    try:
        obj = storage.client().get_object(
            Bucket=storage.runs_bucket(), Key=f"{run_id}/{_METRICS_FILENAME}")
        loaded = json.loads(obj["Body"].read().decode("utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:  # noqa: BLE001 -- absence => no typed gate to surface
        logger.info("telemac: run metrics read miss for %s: %s", run_id, exc)
        return {}


def _run_start_iso(run_result: Any) -> str | None:
    """When the solve began; a run with no ``started_at`` states none."""
    started = getattr(run_result, "started_at", None)
    return started.isoformat() if started is not None else None


def _result_uri(run_id: str, run: dict[str, Any]) -> str:
    return f"s3://{storage.runs_bucket()}/{run_id}/{run['result_basename']}"


async def solve_case(*, run: dict[str, Any], cores: int) -> dict[str, Any]:
    """Dispatch the staged case to the worker and wait -> the run handle.

    The returned ``uri`` is the result SELAFIN a ledger replay probes, and the
    UTM zone comes from the ASSEMBLER: the worker never learns it."""
    facts = run["case"]["server_facts"]
    try:
        run_result, batch_run_id = await dispatch_and_wait(
            solver=TELEMAC_SOLVER_NAME, manifest_uri=run["manifest_uri"],
            cores=cores, module=str(run["case"]["module"]),
            label=str(facts["name"]), sheet=run["sheet"],
            run_id=run["run_id"], grid_resolution_m=facts.get("mesh_size_m"),
            active_cell_count=facts.get("nelem"))
    except SolveStopped as stop:
        # The worker never reached its metrics: the handle states what a stop leaves, the result file as far as it was written.
        stop.solved = {"run_id": stop.run_id, "uri": _result_uri(stop.run_id, run), "utm_epsg": int(facts["utm_epsg"]),
                       "metrics": {}, "started_at": stop.started_at, "stopped": True}
        raise
    if run_result is None or run_result.status != "complete":
        raise TelemacError(
            f"the {facts['name']} solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or ''}",
            error_code="TELEMAC_RUN_FAILED")
    metrics = await asyncio.to_thread(read_run_metrics, batch_run_id)
    # No local path travels out of this step: a replayed record must not hand back a temp file; the products step re-downloads from the run prefix.
    return {
        "run_id": batch_run_id,
        "uri": _result_uri(batch_run_id, run),
        "utm_epsg": int(facts["utm_epsg"]),
        "metrics": metrics,
        "started_at": _run_start_iso(run_result),
    }


register_telemac_solver()
