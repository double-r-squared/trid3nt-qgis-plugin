"""The engine: its image, its verdict on an exit, and the one run path.

Status is the worker's exit code AND the CORRECT-END flag in the metrics file
together: a clean process that never reached the end of the run is an error. The
worker runs ``--network none``, so everything it reads is staged. The wait is
sized off the sheet's own horizon and step, against the worst honest mesh; a
sheet that states no horizon is bounded rather than estimated.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Mapping

from trid3nt_server import storage
from trid3nt_server.workflows.solver.solver import (
    LOCAL_DOCKER_WORKFLOW_NAME,
    LocalSolverSpec,
    SOLVER_WORKFLOW_REGISTRY,
    dispatch_and_wait,
    register_local_solver_spec,
)

from .errors import TelemacError
from .helpers.time_step import MESH_NODE_CAP, estimate_telemac_solve_seconds

logger = logging.getLogger("trid3nt_server.workflows.telemac.engine")

__all__ = ["TELEMAC_SOLVER_NAME", "classify_exit", "read_run_metrics",
           "register_telemac_solver", "solve_case"]

#: The solver identifier IS the engine name: one registration per engine, keyed
#: in both ``SOLVER_WORKFLOW_REGISTRY`` (the presence gate ``run_solver`` reads)
#: and ``LOCAL_SOLVER_SPEC_REGISTRY``. Which MODULE ran is the manifest's
#: ``case.module``, which the worker states back in its metrics.
TELEMAC_SOLVER_NAME: str = "telemac"

#: Default worker image (override via env TRID3NT_TELEMAC_IMAGE).
DEFAULT_TELEMAC_IMAGE: str = "trid3nt-local/telemac:latest"

#: The metrics filename the worker writes into the mounted rundir and uploads
#: under the run prefix: the exit classifier reads the first, a reader of a
#: finished run the second.
_METRICS_FILENAME: str = "telemac_metrics.json"

#: Floor on the completion wait: the worst honest mesh (the node cap) with this
#: headroom bounds the rest. A cap-sized solve outruns a shorter wait, and the
#: publish leg is then lost to the timeout.
_MIN_WAIT_S: float = 1800.0
_WAIT_HEADROOM: float = 1.5

#: The wait for a sheet that states no horizon: a steady harmonic run has no
#: duration and no timestep to size one from. It is a bound on the wait, not an
#: estimate of the run - it exists so a wedged container becomes a typed failure
#: instead of a daemon that never returns.
_TIMELESS_WAIT_S: float = 86400.0


def _telemac_image() -> str:
    """The TELEMAC worker image every module of the engine runs in."""
    return os.environ.get("TRID3NT_TELEMAC_IMAGE") or DEFAULT_TELEMAC_IMAGE


def _build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
    """The volume-mount launch line. ``args`` (``manifest["telemac_args"]``) is
    normally empty - the image CMD drives the entrypoint - and anything passed is
    appended after the image."""
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

    The error sentence names the MODULE the worker says it ran; the whole metrics
    file is the completion's extra, because the worker is ours and writes
    module-level metrics only."""
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
        exec_kind="docker",
        classify_exit=classify_exit,
    )


def register_telemac_solver() -> None:
    """Register the engine in the solver + local-spec registries. Idempotent.

    TELEMAC is local-docker only: the engine lives in the worker image."""
    SOLVER_WORKFLOW_REGISTRY.setdefault(TELEMAC_SOLVER_NAME,
                                        LOCAL_DOCKER_WORKFLOW_NAME)
    register_local_solver_spec(TELEMAC_SOLVER_NAME, _spec)


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


def _wait_seconds(facts: Mapping[str, Any]) -> float:
    """How long to wait on this run, off the sheet's own duration and timestep."""
    duration_s, time_step_s = facts.get("duration_s"), facts.get("time_step_s")
    if duration_s is None or time_step_s is None:
        return _TIMELESS_WAIT_S
    return max(_MIN_WAIT_S, estimate_telemac_solve_seconds(
        MESH_NODE_CAP, float(duration_s), float(time_step_s)) * _WAIT_HEADROOM)


def _run_start_iso(run_result: Any) -> str | None:
    """When the solve began, as the mesh's time origin.

    A SELAFIN records no origin; a run with no ``started_at`` states none."""
    started = getattr(run_result, "started_at", None)
    return started.isoformat() if started is not None else None


async def solve_case(*, run: dict[str, Any],
                     compute_class: str = "medium") -> dict[str, Any]:
    """Dispatch the staged case to the worker and wait -> the run handle.

    The returned ``uri`` is the result SELAFIN a ledger replay probes, and the
    UTM zone comes from the ASSEMBLER: the worker never learns it."""
    facts = run["case"]["server_facts"]
    run_result, batch_run_id = await dispatch_and_wait(
        solver=TELEMAC_SOLVER_NAME, manifest_uri=run["manifest_uri"],
        compute_class=compute_class, module=str(run["case"]["module"]),
        label=str(facts["name"]), timeout_s=_wait_seconds(facts),
        grid_resolution_m=facts.get("mesh_size_m"),
        active_cell_count=facts.get("nelem"))
    if run_result is None or run_result.status != "complete":
        raise TelemacError(
            f"the {facts['name']} solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or ''}",
            error_code="TELEMAC_RUN_FAILED")
    metrics = await asyncio.to_thread(read_run_metrics, batch_run_id)
    # No local path travels out of this step: it is the ledger's record of the
    # solve, and a replayed record must not hand back a temp file that a later
    # process cannot see. The products step re-downloads from the run prefix.
    return {
        "run_id": batch_run_id,
        "uri": (f"s3://{storage.runs_bucket()}/{batch_run_id}/"
                f"{run['result_basename']}"),
        "utm_epsg": int(facts["utm_epsg"]),
        "metrics": metrics,
        "started_at": _run_start_iso(run_result),
    }


register_telemac_solver()
