"""TELEMAC-2D river-dye local solve seam (PHASE 2).

Wires the ``telemac_river_dye`` archetype into the shared local-docker solve
backend so ``run_solver(solver='telemac_river_dye', ...)`` under
``GRACE2_SOLVER_BACKEND=local-docker`` dispatches to the
``trid3nt-local/telemac:latest`` worker image -- exactly like the
SFINCS/GeoClaw/SWAN local specs. This module carries ONLY the seam (P2); the
LLM-facing ``run_telemac`` tool + the ``model_river_dye_release_scenario``
composer are P4.

Structural clone of ``run_geoclaw.geoclaw_local_spec`` /
``register_geoclaw_local_spec`` (same ``LocalSolverSpec`` factory + import-time
registration shape), with two DELIBERATE differences that make it correct on the
LOCAL seam:

  1. **VOLUME-MOUNT build_argv (SFINCS-canonical), not GeoClaw's
     ``--network host`` self-S3-I/O.** The tested local-docker envelope
     (``tools.solver.launch_local_solver`` + ``_supervise_local_run``, proven in
     ``test_solver_local_docker.py``) stages the manifest into
     ``<rundir>/manifest.json``, bind-mounts the rundir at ``/data``, and the
     AGENT-SIDE supervisor uploads the mounted outputs + writes
     ``completion.json``. GeoClaw's spec instead mounts NOTHING and has the
     container do its own MinIO I/O; on the local seam that leaves the
     supervisor's ``output_uris`` empty (it globs an unmounted rundir). So the
     TELEMAC worker writes its mesh/result ``.slf`` into the mounted ``/data``
     and the supervisor uploads them -- the .slf lands in the runs bucket with a
     real ``output_uris`` entry, and the image needs NO boto3 (leaner).

  2. **A ``classify_exit`` hook (MODFLOW-analogue) that folds the dye metrics
     from ``telemac_metrics.json`` into the run's completion.json.** The worker
     writes ``/data/telemac_metrics.json`` (correct_end / n_frames / dye_cmax /
     npoin / reach meta); ``classify_exit`` reads it from the rundir and returns
     those as ``extra`` completion fields + resolves status from the
     CORRECT-END flag (mirroring MODFLOW's ``mfsim.lst`` convergence guard).

ASCII only.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("grace2.workflows.run_telemac")

#: The solver identifier (== the archetype). Keyed in both
#: ``SOLVER_WORKFLOW_REGISTRY`` (presence gate) and ``LOCAL_SOLVER_SPEC_REGISTRY``
#: (local-docker factory).
TELEMAC_SOLVER_NAME: str = "telemac_river_dye"

#: Default worker image (override via env GRACE2_TELEMAC_IMAGE, mirroring
#: GRACE2_GEOCLAW_IMAGE / GRACE2_SWAN_IMAGE).
DEFAULT_TELEMAC_IMAGE: str = "trid3nt-local/telemac:latest"

#: The metrics filename the worker writes into the mounted rundir.
_METRICS_FILENAME: str = "telemac_metrics.json"

#: Metrics keys folded into completion.json (a stable, small subset).
_COMPLETION_METRIC_KEYS: tuple[str, ...] = (
    "correct_end",
    "n_frames",
    "dye_var",
    "dye_cmax_final",
    "dye_cmax_overall",
    "dye_peak_time_s",
    "dye_active_frames",
    "dye_front_x_final_m",
    "result_slf",
    "npoin",
    "nelem",
    "nptfr",
    "reach_name",
    "seed_comid",
    "utm_epsg",
    "centerline_length_m",
    "lb_order",
    "wall_s",
)


# --------------------------------------------------------------------------- #
# Solver registration (mirrors register_geoclaw_solver / register_swan_solver).
# --------------------------------------------------------------------------- #
def register_telemac_solver() -> None:
    """Register ``'telemac_river_dye'`` in ``tools.solver.SOLVER_WORKFLOW_REGISTRY``.

    The registry value is consumed purely as a PRESENCE GATE by ``run_solver``;
    the live routing comes from the backend sentinel. TELEMAC is local-docker
    only (the engine lives in the worker image, never the agent venv), so it maps
    to the local-docker workflow-name sentinel. Idempotent ``setdefault``.
    """
    from ..tools.solver import LOCAL_DOCKER_WORKFLOW_NAME, SOLVER_WORKFLOW_REGISTRY

    SOLVER_WORKFLOW_REGISTRY.setdefault(TELEMAC_SOLVER_NAME, LOCAL_DOCKER_WORKFLOW_NAME)


register_telemac_solver()


# --------------------------------------------------------------------------- #
# TELEMAC LocalSolverSpec -- docker runner for the local-docker backend.
#
# exec_kind="docker": the worker image carries the full opentelemac v9.0.0 conda
# env + the P1 pipeline. VOLUME-MOUNT (SFINCS-style): the launcher stages
# manifest.json into the rundir and bind-mounts it at /data; the worker reads
# /data/manifest.json, runs the pipeline, and writes river.slf + r2d_river.slf +
# telemac_metrics.json into /data; the supervisor uploads /data -> the runs
# bucket and writes completion.json (classify_exit folds in the dye metrics).
# --------------------------------------------------------------------------- #
def _classify_exit(
    rundir: Path, exit_code: int
) -> tuple[str, int, str | None, dict[str, Any]]:
    """Resolve status from telemac_metrics.json (MODFLOW classify_exit analogue).

    The worker's own exit code is authoritative for the process, but the
    CORRECT-END flag in ``telemac_metrics.json`` is the physics-level truth. We
    combine them: a clean process exit AND ``correct_end`` -> ok; otherwise
    error. The metrics subset rides into completion.json as ``extra`` fields so
    the run summary carries the dye front / frame count / mesh size without a
    second object read.
    """
    metrics: dict[str, Any] = {}
    metrics_path = rundir / _METRICS_FILENAME
    try:
        if metrics_path.exists():
            loaded = json.loads(metrics_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                metrics = loaded
    except Exception as exc:  # noqa: BLE001 -- a bad metrics file must not kill the write
        logger.warning("telemac classify_exit: metrics read failed %s: %s", metrics_path, exc)

    extra: dict[str, Any] = {
        k: metrics[k] for k in _COMPLETION_METRIC_KEYS if k in metrics
    }

    correct_end = bool(metrics.get("correct_end"))
    if exit_code != 0:
        status = "error"
        error: str | None = (
            metrics.get("error")
            or f"telemac_river_dye exited with non-zero code {exit_code}"
        )
    elif metrics and not correct_end:
        status, exit_code = "error", 2
        error = metrics.get("error") or "TELEMAC did not reach CORRECT END OF RUN"
    else:
        status, exit_code, error = "ok", 0, None
    return status, exit_code, error, extra


def telemac_local_spec() -> "Any":
    """Build the TELEMAC river-dye ``LocalSolverSpec`` for the local-docker backend."""
    import os
    from ..tools.solver import LOCAL_DOCKER_WORKFLOW_NAME, LocalSolverSpec

    image = os.environ.get("GRACE2_TELEMAC_IMAGE") or DEFAULT_TELEMAC_IMAGE

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        # SFINCS-canonical volume-mount launch: the launcher already wrote
        # <rundir>/manifest.json; the worker reads it at /data/manifest.json.
        # ``args`` (manifest["telemac_args"]) is normally empty -- the CMD in the
        # image drives the entrypoint. Anything passed is appended after the
        # image (parity with the SFINCS spec).
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
        solver=TELEMAC_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="telemac_args",
        build_argv=build_argv,
        stdout_name="telemac.stdout",
        stderr_name="telemac.stderr",
        stdout_uri_field="telemac_stdout_uri",
        stderr_uri_field="telemac_stderr_uri",
        exec_kind="docker",
        classify_exit=_classify_exit,
    )


def register_telemac_local_spec() -> None:
    """Register the TELEMAC LocalSolverSpec factory for the local-docker backend."""
    from ..tools.solver import register_local_solver_spec

    register_local_solver_spec(TELEMAC_SOLVER_NAME, telemac_local_spec)


# Register at import so run_solver(solver='telemac_river_dye') with
# GRACE2_SOLVER_BACKEND=local-docker dispatches to the docker spec.
register_telemac_local_spec()
