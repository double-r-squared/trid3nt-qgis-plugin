"""The TELEMAC local-docker solve seam - three solver names, one image, one spec.

Every leg of the family runs the SAME worker image the SAME way: the launcher
writes ``<rundir>/manifest.json`` and stages every input beside it, bind-mounts
the rundir at ``/data``, and the agent-side supervisor uploads the mounted
outputs and writes ``completion.json``. The worker therefore needs no boto3 and
runs ``--network none``: everything it reads arrives staged.

The three NAMES stay because a run listing is read by a human - a harbour
agitation field and a river-dye plume are not the same kind of run and must not
share a row identity. What they share is everything else, so the spec is one
factory and the exit classification is one closure over the label the error
sentence names.

Status is the worker's exit code AND the CORRECT-END flag in
``telemac_metrics.json`` together: a clean process that never reached the end of
the run is an error. The metrics subset rides into ``completion.json`` as
``extra`` so a run summary carries the physics without a second object read.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("trid3nt.workflows.run_telemac")

#: Solver identifiers. Each is keyed in both ``SOLVER_WORKFLOW_REGISTRY`` (the
#: presence gate ``run_solver`` reads) and ``LOCAL_SOLVER_SPEC_REGISTRY``.
TELEMAC_SOLVER_NAME: str = "telemac_river_dye"
ARTEMIS_SOLVER_NAME: str = "artemis_agitation"
TELEMAC3D_SOLVER_NAME: str = "telemac3d_strat"

#: Default worker image (override via env TRID3NT_TELEMAC_IMAGE).
DEFAULT_TELEMAC_IMAGE: str = "trid3nt-local/telemac:latest"

#: The metrics filename the worker writes into the mounted rundir.
_METRICS_FILENAME: str = "telemac_metrics.json"

#: Metrics keys folded into completion.json. ONE list across the family: a key a
#: leg never writes is simply absent from its metrics and never lands, so a
#: per-leg list only duplicated that filtering in a second place.
_COMPLETION_METRIC_KEYS: frozenset[str] = frozenset((
    # every leg. ``bbox`` is the ONE spelling of the solved domain's lon/lat
    # extent: the server measures it and echoes it, and a second name for it was
    # a second answer a reader had to pick between.
    "correct_end", "error_code", "module", "family", "result_slf",
    "npoin", "nelem", "ntimestep",
    # the failure path's evidence. The worker writes it only when the run did
    # not reach a correct end, and it is the only listing a reader has when the
    # solve died before the listing file was uploaded.
    "listing_tail",
    "nptfr", "utm_epsg", "dx_m", "coarsened", "n_wet_nodes", "depth_max_m",
    "depth_mean_m", "bathy_source", "bed_source", "bbox", "wall_s",
    # reach / river dye
    "n_frames", "dye_peak_time_s", "reach_name", "mesh_size_m",
    "time_step_s", "edge_min_m", "edge_mean_m", "edge_max_m",
    "preview_geojson",
    # wind stress, on whichever leg was asked for it
    "wind_speed_mps", "wind_dir_from_deg",
    # artemis
    "wave_mode", "hs_max_m",
    "agitation_field_slf", "kd_max", "kd_sheltered", "kd_exposed",
    "sheltering_ratio", "resonant_period_s", "response_at_resonance",
    "response_off_resonance", "kd_focus_peak", "wave_period_s", "wave_dir_deg",
    "wave_height_m", "reflection_coef",
    # telemac3d
    "flow_mode", "surface_field_slf", "bottom_field_slf", "nplan",
    "stratification_metric", "variable_label", "variable_units",
    "stratification_dt", "stratification_dt_init", "u_surface", "u_bottom",
    "depth_avg_u", "front_speed_mps", "benjamin_speed_mps", "front_ratio",
    "non_hydrostatic", "surface_value_mean", "bottom_value_mean",
))


def _telemac_image() -> str:
    """The TELEMAC worker image every leg of the family runs in."""
    return os.environ.get("TRID3NT_TELEMAC_IMAGE") or DEFAULT_TELEMAC_IMAGE


def _build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
    """The volume-mount launch line. ``args`` (``manifest["telemac_args"]``) is
    normally empty - the image CMD drives the entrypoint - and anything passed is
    appended after the image, as on the SFINCS spec."""
    return ["docker", "run", "--rm", "--name", run_id,
            "-v", f"{rundir}:/data", "-w", "/data", _telemac_image(), *args]


def _classify(label: str) -> Callable[[Path, int], tuple[str, int, str | None,
                                                         dict[str, Any]]]:
    """The exit classifier for one leg -> ``(status, exit_code, error, extra)``.

    ``label`` names the leg in the error sentence a human reads; nothing else
    about the classification differs across the family.
    """
    def classify_exit(rundir: Path, exit_code: int
                      ) -> tuple[str, int, str | None, dict[str, Any]]:
        metrics: dict[str, Any] = {}
        path = rundir / _METRICS_FILENAME
        try:
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    metrics = loaded
        except Exception as exc:  # noqa: BLE001 -- a bad metrics file must not kill the write
            logger.warning("%s classify_exit: metrics read failed %s: %s",
                           label, path, exc)
        extra = {k: v for k, v in metrics.items() if k in _COMPLETION_METRIC_KEYS}
        if exit_code != 0:
            return ("error", exit_code,
                    metrics.get("error")
                    or f"{label} exited with non-zero code {exit_code}", extra)
        if metrics and not bool(metrics.get("correct_end")):
            return ("error", 2,
                    metrics.get("error")
                    or f"{label} did not reach CORRECT END OF RUN", extra)
        return "ok", 0, None, extra
    return classify_exit


def make_spec(solver: str, stream_prefix: str) -> Callable[[], Any]:
    """The ``LocalSolverSpec`` factory for one solver name.

    ``stream_prefix`` names the leg's stdout/stderr objects so a run directory
    reads as the run it was, rather than as five files called ``telemac.*``.
    """
    def spec() -> Any:
        from trid3nt_server.workflows.solver.solver import (
            LOCAL_DOCKER_WORKFLOW_NAME,
            LocalSolverSpec,
        )

        return LocalSolverSpec(
            solver=solver,
            workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
            args_key="telemac_args",
            build_argv=_build_argv,
            network="none",
            stdout_name=f"{stream_prefix}.stdout",
            stderr_name=f"{stream_prefix}.stderr",
            stdout_uri_field=f"{stream_prefix}_stdout_uri",
            stderr_uri_field=f"{stream_prefix}_stderr_uri",
            exec_kind="docker",
            classify_exit=_classify(solver),
        )
    return spec


#: solver name -> the stdout/stderr prefix its run directory is read by.
_SOLVERS: dict[str, str] = {
    TELEMAC_SOLVER_NAME: "telemac",
    ARTEMIS_SOLVER_NAME: "artemis",
    TELEMAC3D_SOLVER_NAME: "telemac3d",
}


def register_telemac_solvers() -> None:
    """Register every leg in the solver + local-spec registries.

    The ``SOLVER_WORKFLOW_REGISTRY`` value is consumed purely as a PRESENCE GATE
    by ``run_solver``; the live routing comes from the backend sentinel. TELEMAC
    is local-docker only - the engine lives in the worker image, never the agent
    venv. Idempotent ``setdefault``.
    """
    from trid3nt_server.workflows.solver.solver import (
        LOCAL_DOCKER_WORKFLOW_NAME,
        SOLVER_WORKFLOW_REGISTRY,
        register_local_solver_spec,
    )

    for solver, prefix in _SOLVERS.items():
        SOLVER_WORKFLOW_REGISTRY.setdefault(solver, LOCAL_DOCKER_WORKFLOW_NAME)
        register_local_solver_spec(solver, make_spec(solver, prefix))


register_telemac_solvers()
