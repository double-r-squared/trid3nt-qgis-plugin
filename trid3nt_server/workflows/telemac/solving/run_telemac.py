"""The TELEMAC local-docker solve seam - one engine, one image, one spec.

Status is the worker's exit code AND the CORRECT-END flag in
``telemac_metrics.json`` together: a clean process that never reached the end of
the run is an error. The worker runs ``--network none``: everything is staged."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("trid3nt.workflows.run_telemac")

#: The solver identifier IS the engine name: one registration per engine, keyed
#: in both ``SOLVER_WORKFLOW_REGISTRY`` (the presence gate ``run_solver`` reads)
#: and ``LOCAL_SOLVER_SPEC_REGISTRY``. Which MODULE ran is the manifest's
#: ``case.module``, which the worker states back in its metrics.
TELEMAC_SOLVER_NAME: str = "telemac"

#: Default worker image (override via env TRID3NT_TELEMAC_IMAGE).
DEFAULT_TELEMAC_IMAGE: str = "trid3nt-local/telemac:latest"

#: The metrics filename the worker writes into the mounted rundir.
_METRICS_FILENAME: str = "telemac_metrics.json"


def _telemac_image() -> str:
    """The TELEMAC worker image every module of the engine runs in."""
    return os.environ.get("TRID3NT_TELEMAC_IMAGE") or DEFAULT_TELEMAC_IMAGE


def _build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
    """The volume-mount launch line. ``args`` (``manifest["telemac_args"]``) is
    normally empty - the image CMD drives the entrypoint - and anything passed is
    appended after the image, as on the SFINCS spec."""
    return ["docker", "run", "--rm", "--name", run_id,
            "-v", f"{rundir}:/data", "-w", "/data", _telemac_image(), *args]


def _why(metrics: dict[str, Any], fallback: str) -> str:
    """Why the run stopped, with the engine's own demand named where it made one.

    The listing's own sentence, so this side invents no required set."""
    from ..products.run_reads import engine_demand

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


def _spec() -> Any:
    """The engine's one ``LocalSolverSpec``."""
    from trid3nt_server.workflows.solver.solver import (
        LOCAL_DOCKER_WORKFLOW_NAME,
        LocalSolverSpec,
    )

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
    from trid3nt_server.workflows.solver.solver import (
        LOCAL_DOCKER_WORKFLOW_NAME,
        SOLVER_WORKFLOW_REGISTRY,
        register_local_solver_spec,
    )

    SOLVER_WORKFLOW_REGISTRY.setdefault(TELEMAC_SOLVER_NAME,
                                        LOCAL_DOCKER_WORKFLOW_NAME)
    register_local_solver_spec(TELEMAC_SOLVER_NAME, _spec)


register_telemac_solver()
