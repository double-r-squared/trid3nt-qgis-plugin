"""HEC-RAS 6.x local-docker solve seam (engine #11 landing).

Wires the ``hecras_riverine_flood`` archetype into the shared local-docker solve
backend so ``run_solver(solver='hecras_riverine_flood', ...)`` dispatches to the
``trid3nt-local/hecras:latest`` worker image (the M3 image carrying HEC's official
public-domain 6.6 Linux computation engines). Structural clone of
``run_telemac.telemac_local_spec`` / ``register_telemac_local_spec``:

  1. **VOLUME-MOUNT build_argv (SFINCS/TELEMAC-canonical).** The launcher stages
     ``manifest.json`` into the rundir and bind-mounts it at ``/data``; the worker
     entrypoint stages the BAKED shipped-geometry Muncie deck into ``/data``,
     applies the unsteady flow-forcing reparameterization, runs
     ``RasGeomPreprocess`` then ``RasUnsteady`` (appending a ``Results`` group to
     the plan HDF in place), and writes ``hecras_metrics.json``. The agent-side
     supervisor uploads the manifest ``outputs[]`` (the solved plan HDF + metrics)
     and writes ``completion.json``; the composer's postprocess downloads the
     solved plan HDF and rasterizes the peak-depth COG.

  2. **A ``classify_exit`` hook (MODFLOW/TELEMAC-analogue)** that folds the volume
     accounting + the ``correct_end`` sentinel flag from ``hecras_metrics.json``
     into completion.json and resolves status from CORRECT-END (the entrypoint
     already gates on the ``Finished`` sentinel + a Results group; correct_end is
     the physics-level truth mirrored here so a mid-run abort reads as error).

HEC-RAS is LOCAL-DOCKER ONLY (the Linux engines live in the worker image, never
the agent venv). ASCII only.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("trid3nt.workflows.run_hecras")

#: The solver identifier (== the template/archetype tool name). Keyed in both
#: ``SOLVER_WORKFLOW_REGISTRY`` (presence gate) and ``LOCAL_SOLVER_SPEC_REGISTRY``.
HECRAS_SOLVER_NAME: str = "hecras_riverine_flood"

#: The levee-breach template's solver identifier -- the SAME worker image (the
#: archetype + breach toggle ride in the manifest), a distinct name so the dispatch
#: + logs read the capability honestly. Both map to the same LocalSolverSpec build.
HECRAS_LEVEE_BREACH_SOLVER_NAME: str = "hecras_levee_breach"

#: The fresh-AOI 2D-flood template's solver identifier (promotion). The
#: SAME worker image, but the manifest carries a COMPOSED deck as ``inputs`` (no
#: baked archetype): the entrypoint's M3-gate path (``plan_hdf`` + ``geom_suffix``,
#: no archetype) runs RasGeomPreprocess + RasUnsteady on the staged fresh-authored
#: deck. The authoring stage (a distinct image) runs upstream in the composer.
HECRAS_FLOOD2D_SOLVER_NAME: str = "hecras_flood_2d"

#: Every registered HEC-RAS solver name (one solver image, per-capability names).
HECRAS_SOLVER_NAMES: tuple[str, ...] = (
    HECRAS_SOLVER_NAME, HECRAS_LEVEE_BREACH_SOLVER_NAME, HECRAS_FLOOD2D_SOLVER_NAME,
)

#: Default worker image (override via env TRID3NT_HECRAS_IMAGE, mirroring
#: TRID3NT_TELEMAC_IMAGE / TRID3NT_SFINCS_IMAGE).
DEFAULT_HECRAS_IMAGE: str = "trid3nt-local/hecras:latest"

#: The metrics filename the worker writes into the mounted rundir.
_METRICS_FILENAME: str = "hecras_metrics.json"

#: Metrics keys folded into completion.json (a stable, small subset).
_COMPLETION_METRIC_KEYS: tuple[str, ...] = (
    "correct_end",
    "archetype",
    "plan_hdf",
    "flow_scale",
    "baseline_peak_cfs",
    "peak_inflow_cfs",
    "breach_enabled",
    "breach_count_active",
    "ran_geompre",
)


def register_hecras_solver() -> None:
    """Register the HEC-RAS solver names in ``SOLVER_WORKFLOW_REGISTRY``.

    The registry value is a PRESENCE GATE for ``run_solver``; the live routing
    comes from the backend sentinel. HEC-RAS is local-docker only, so every name
    maps to the local-docker workflow-name sentinel. Idempotent ``setdefault``.
    """
    from trid3nt_server.workflows.solver.solver import (
        LOCAL_DOCKER_WORKFLOW_NAME,
        SOLVER_WORKFLOW_REGISTRY,
    )

    for name in HECRAS_SOLVER_NAMES:
        SOLVER_WORKFLOW_REGISTRY.setdefault(name, LOCAL_DOCKER_WORKFLOW_NAME)


register_hecras_solver()


def _classify_exit(
    rundir: Path, exit_code: int
) -> tuple[str, int, str | None, dict[str, Any]]:
    """Resolve status from hecras_metrics.json (TELEMAC classify_exit analogue).

    A clean process exit AND ``correct_end`` -> ok; otherwise error. The metrics
    subset rides into completion.json as ``extra`` fields so the run summary
    carries the flow-forcing provenance without a second object read.
    """
    metrics: dict[str, Any] = {}
    metrics_path = rundir / _METRICS_FILENAME
    try:
        if metrics_path.exists():
            loaded = json.loads(metrics_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                metrics = loaded
    except Exception as exc:  # noqa: BLE001 -- a bad metrics file must not kill the write
        logger.warning("hecras classify_exit: metrics read failed %s: %s", metrics_path, exc)

    # Fold the volume-accounting error percent onto completion too (M3 gate signal).
    extra: dict[str, Any] = {k: metrics[k] for k in _COMPLETION_METRIC_KEYS if k in metrics}
    va = metrics.get("volume_accounting")
    if isinstance(va, dict) and "Error Percent" in va:
        try:
            extra["volume_error_pct"] = float(va["Error Percent"])
        except (TypeError, ValueError):
            pass

    correct_end = bool(metrics.get("correct_end"))
    if exit_code != 0:
        status = "error"
        error: str | None = (
            metrics.get("error")
            or f"hecras_riverine_flood exited with non-zero code {exit_code}"
        )
    elif metrics and not correct_end:
        status, exit_code = "error", 2
        error = metrics.get("error") or "HEC-RAS did not reach a Finished sentinel / Results group"
    else:
        status, exit_code, error = "ok", 0, None
    return status, exit_code, error, extra


def hecras_local_spec(solver_name: str = HECRAS_SOLVER_NAME) -> "Any":
    """Build a HEC-RAS ``LocalSolverSpec`` for the local-docker backend.

    ``solver_name`` names the capability (riverine-flood or levee-breach); both
    drive the SAME worker image (the archetype + knobs ride in the manifest)."""
    import os

    from trid3nt_server.workflows.solver.solver import (
        LOCAL_DOCKER_WORKFLOW_NAME,
        LocalSolverSpec,
    )

    image = os.environ.get("TRID3NT_HECRAS_IMAGE") or DEFAULT_HECRAS_IMAGE

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        # Volume-mount launch: the launcher already wrote <rundir>/manifest.json;
        # the worker reads it at /data/manifest.json. ``args`` (manifest
        # ["hecras_args"]) is normally empty -- the image ENTRYPOINT drives.
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
        solver=solver_name,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="hecras_args",
        build_argv=build_argv,
        stdout_name="hecras.stdout",
        stderr_name="hecras.stderr",
        stdout_uri_field="hecras_stdout_uri",
        stderr_uri_field="hecras_stderr_uri",
        exec_kind="docker",
        classify_exit=_classify_exit,
    )


def register_hecras_local_spec() -> None:
    """Register the HEC-RAS LocalSolverSpec factory for every capability name."""
    from trid3nt_server.workflows.solver.solver import register_local_solver_spec

    for name in HECRAS_SOLVER_NAMES:
        register_local_solver_spec(name, lambda n=name: hecras_local_spec(n))


register_hecras_local_spec()
