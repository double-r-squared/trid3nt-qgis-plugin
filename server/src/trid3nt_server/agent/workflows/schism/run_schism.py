"""SCHISM local-docker solve seam (engine #12 landing, ADR 0118).

Wires the ``schism_tidal_hydro`` archetype into the shared local-docker solve
backend so ``run_solver(solver='schism_tidal_hydro', ...)`` dispatches to the
``trid3nt-local/schism:latest`` worker image (SCHISM v5.11.0 hydro-core, ADR
0115). Structural clone of ``run_hecras`` / ``run_telemac``:

  1. **VOLUME-MOUNT build_argv (SFINCS/TELEMAC/HEC-RAS-canonical).** The launcher
     stages the manifest + the GENERATED case files (``hgrid.gr3`` / ``vgrid.in``
     / ``param.nml`` / ``bctides.in`` / ``station.in`` + the analytical reference
     for the verification archetype) into the rundir via the manifest ``inputs[]``
     (each ``{"gs_uri": ..., "dest": ...}``), bind-mounts the rundir at ``/data``,
     and the worker ENTRYPOINT (``entrypoint.py``) runs the selected executable
     variant under mpirun, gates on SCHISM's "Run completed successfully" sentinel
     (never the exit code -- the HEC-RAS lesson), and writes ``schism_metrics.json``
     + scribed ``outputs/*.nc``. The agent-side supervisor uploads the manifest
     ``outputs[]`` (``outputs/*.nc``, ``staout_*``, ``schism_metrics.json``) and
     writes completion.json; the composer's postprocess downloads out2d + the
     station output.

  2. **A ``classify_exit`` hook** that reads ``schism_metrics.json`` and resolves
     status from the worker's own ``status`` (the sentinel truth) so a mid-run
     abort reads as error, not empty success.

SCHISM is LOCAL-DOCKER ONLY. ASCII only.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("trid3nt.workflows.run_schism")

#: The solver identifier (== the template/archetype tool name). Keyed in both
#: ``SOLVER_WORKFLOW_REGISTRY`` (presence gate) and ``LOCAL_SOLVER_SPEC_REGISTRY``.
SCHISM_SOLVER_NAME: str = "schism_tidal_hydro"

#: Default worker image (override via env TRID3NT_SCHISM_IMAGE, mirroring
#: TRID3NT_TELEMAC_IMAGE / TRID3NT_HECRAS_IMAGE).
DEFAULT_SCHISM_IMAGE: str = "trid3nt-local/schism:latest"

#: The metrics filename the worker writes into the mounted rundir.
_METRICS_FILENAME: str = "schism_metrics.json"

#: Metrics keys folded into completion.json (a stable, small subset).
_COMPLETION_METRIC_KEYS: tuple[str, ...] = (
    "status",
    "variant",
    "executable",
    "wall_s",
    "n_netcdf_outputs",
    "run_id",
)


def register_schism_solver() -> None:
    """Register ``'schism_tidal_hydro'`` in ``SOLVER_WORKFLOW_REGISTRY``.

    The registry value is a PRESENCE GATE for ``run_solver``; SCHISM is
    local-docker only, so it maps to the local-docker workflow-name sentinel.
    Idempotent ``setdefault``.
    """
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        LOCAL_DOCKER_WORKFLOW_NAME,
        SOLVER_WORKFLOW_REGISTRY,
    )

    SOLVER_WORKFLOW_REGISTRY.setdefault(SCHISM_SOLVER_NAME, LOCAL_DOCKER_WORKFLOW_NAME)


register_schism_solver()


def _classify_exit(
    rundir: Path, exit_code: int
) -> tuple[str, int, str | None, dict[str, Any]]:
    """Resolve status from schism_metrics.json (HEC-RAS/TELEMAC classify_exit analogue).

    The worker entrypoint gates on SCHISM's "Run completed successfully" sentinel
    and records ``status`` in the metrics; a clean process exit is NOT trusted on
    its own (SCHISM exits 0 even on a mid-run abort). The metrics subset rides into
    completion.json as ``extra`` fields so the run summary carries the solve
    provenance without a second object read.
    """
    metrics: dict[str, Any] = {}
    metrics_path = rundir / _METRICS_FILENAME
    try:
        if metrics_path.exists():
            loaded = json.loads(metrics_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                metrics = loaded
    except Exception as exc:  # noqa: BLE001 -- a bad metrics file must not kill the write
        logger.warning("schism classify_exit: metrics read failed %s: %s", metrics_path, exc)

    extra: dict[str, Any] = {k: metrics[k] for k in _COMPLETION_METRIC_KEYS if k in metrics}
    worker_status = str(metrics.get("status") or "")

    if worker_status == "ok":
        return "ok", 0, None, extra
    # No/failed sentinel OR a nonzero process exit -> error.
    error = (
        metrics.get("error")
        or metrics.get("error_code")
        or f"SCHISM did not reach the completion sentinel (exit={exit_code})"
    )
    return "error", (exit_code or 5), str(error), extra


def schism_local_spec() -> "Any":
    """Build the SCHISM ``LocalSolverSpec`` for the local-docker backend."""
    from trid3nt_server.agent.tools.simulation.solver.solver import (
        LOCAL_DOCKER_WORKFLOW_NAME,
        LocalSolverSpec,
    )

    image = os.environ.get("TRID3NT_SCHISM_IMAGE") or DEFAULT_SCHISM_IMAGE

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        # Volume-mount launch: the launcher already staged <rundir>/manifest.json +
        # the case files; the worker ENTRYPOINT (entrypoint.py) reads
        # /data/manifest.json for variant/ncompute/nscribe and drives mpirun.
        # ``args`` (manifest ["schism_args"]) is normally empty.
        return [
            "docker",
            "run",
            "--rm",
            "--name",
            run_id,
            "-v",
            f"{rundir}:/data",
            "-w",
            "/data",
            image,
            *args,
        ]

    return LocalSolverSpec(
        solver=SCHISM_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="schism_args",
        build_argv=build_argv,
        stdout_name="schism.stdout",
        stderr_name="schism.stderr",
        stdout_uri_field="schism_stdout_uri",
        stderr_uri_field="schism_stderr_uri",
        exec_kind="docker",
        classify_exit=_classify_exit,
    )


def register_schism_local_spec() -> None:
    """Register the SCHISM LocalSolverSpec factory for the local-docker backend."""
    from trid3nt_server.agent.tools.simulation.solver.solver import register_local_solver_spec

    register_local_solver_spec(SCHISM_SOLVER_NAME, schism_local_spec)


register_schism_local_spec()
