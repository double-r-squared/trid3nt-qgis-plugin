"""TELEMAC-2D river-dye local solve seam.

Wires the ``telemac_river_dye`` archetype into the shared local-docker solve
backend so ``run_solver(solver='telemac_river_dye', ...)`` under
``TRID3NT_SOLVER_BACKEND=local-docker`` dispatches to the
``trid3nt-local/telemac:latest`` worker image -- exactly like the
SFINCS/GeoClaw/SWAN local specs. This module carries ONLY the seam; the reach
pipeline itself is the declared step family in ``workflows/telemac/steps``.

Structural clone of ``run_geoclaw.geoclaw_local_spec`` /
``register_geoclaw_local_spec`` (same ``LocalSolverSpec`` factory + import-time
registration shape), with two DELIBERATE differences that make it correct on the
LOCAL seam:

  1. **VOLUME-MOUNT build_argv (SFINCS-canonical), unlike GeoClaw's
     ``--network host`` self-S3-I/O.** The local-docker envelope
     (``tools.simulation.solver.launch_local_solver`` + ``_supervise_local_run``)
     bind-mounts the rundir at ``/data`` and the AGENT-SIDE supervisor uploads
     the mounted outputs + writes ``completion.json``, so the TELEMAC worker
     writes its mesh/result ``.slf`` into ``/data`` and needs NO boto3
     (leaner; ``output_uris`` is populated from the mount, not self-uploaded).

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

logger = logging.getLogger("trid3nt.workflows.run_telemac")

#: The solver identifier (== the archetype). Keyed in both
#: ``SOLVER_WORKFLOW_REGISTRY`` (presence gate) and ``LOCAL_SOLVER_SPEC_REGISTRY``
#: (local-docker factory).
TELEMAC_SOLVER_NAME: str = "telemac_river_dye"

#: Default worker image (override via env TRID3NT_TELEMAC_IMAGE, mirroring
#: TRID3NT_GEOCLAW_IMAGE / TRID3NT_SWAN_IMAGE).
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
    # wind-stress forcing echo (present only when a wind run was requested).
    "wind_speed_mps",
    "wind_dir_from_deg",
    "utm_epsg",
    "centerline_length_m",
    "lb_order",
    "wall_s",
    # mesh-only preview runs (the approve-mesh gate reads these).
    "mesh_only",
    "mesh_size_m",
    "time_step_s",
    "edge_min_m",
    "edge_mean_m",
    "edge_max_m",
    "bbox4326",
    "preview_geojson",
    "bank_source",
    "bank_width_mean_m",
    # leg 1 banks gate: fold the worker's typed banks-unavailable signal into the
    # completion so run_result carries it (the composer also reads it directly).
    "error_code",
    "assumed_channel_width_m",
    # the worker's verdict on a supplied release point: honored, or relocated to
    # the spill_fraction walk and by how far. The solve step reconciles these.
    "release_point_used",
    "release_point_rejected_dist_m",
)



def _telemac_image() -> str:
    """The TELEMAC worker image every leg of the family runs in."""
    import os

    return os.environ.get("TRID3NT_TELEMAC_IMAGE") or DEFAULT_TELEMAC_IMAGE


def _telemac_build_argv(image: str):
    """The volume-mount launch line, written once for the whole family.

    Five legs run the same image the same way - the launcher has already written
    ``<rundir>/manifest.json`` and staged every input beside it, and the worker
    reads them at ``/data``. ``args`` (``manifest["telemac_args"]``) is normally
    empty because the image CMD drives the entrypoint; anything passed is appended
    after the image, as on the SFINCS spec.
    """
    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        return [
            "docker", "run", "--rm", "--name", run_id,
            "-v", f"{rundir}:/data", "-w", "/data", image, *args,
        ]

    return build_argv


# --------------------------------------------------------------------------- #
# Solver registration (mirrors register_geoclaw_solver / register_swan_solver).
# --------------------------------------------------------------------------- #
def register_telemac_solver() -> None:
    """Register ``'telemac_river_dye'`` in ``tools.simulation.solver.SOLVER_WORKFLOW_REGISTRY``.

    The registry value is consumed purely as a PRESENCE GATE by ``run_solver``;
    the live routing comes from the backend sentinel. TELEMAC is local-docker
    only (the engine lives in the worker image, never the agent venv), so it maps
    to the local-docker workflow-name sentinel. Idempotent ``setdefault``.
    """
    from trid3nt_server.workflows.solver.solver import LOCAL_DOCKER_WORKFLOW_NAME, SOLVER_WORKFLOW_REGISTRY

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
    """Build the TELEMAC river-dye ``LocalSolverSpec`` for the local-docker backend.

    The reach family and the rain-on-grid catchment both run through this spec.
    It was the LAST leg of the image still holding in-container fetches - the
    NLDI seed ladder, the navigate that IS the centerline, the NHDArea banks and
    the Copernicus/3DEP bed. All four are staged now, so this declares
    ``network="none"`` with the other four and the WHOLE image is an engine room.
    """
    from trid3nt_server.workflows.solver.solver import LOCAL_DOCKER_WORKFLOW_NAME, LocalSolverSpec

    return LocalSolverSpec(
        solver=TELEMAC_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="telemac_args",
        build_argv=_telemac_build_argv(_telemac_image()),
        network="none",
        stdout_name="telemac.stdout",
        stderr_name="telemac.stderr",
        stdout_uri_field="telemac_stdout_uri",
        stderr_uri_field="telemac_stderr_uri",
        exec_kind="docker",
        classify_exit=_classify_exit,
    )


def register_telemac_local_spec() -> None:
    """Register the TELEMAC LocalSolverSpec factory for the local-docker backend."""
    from trid3nt_server.workflows.solver.solver import register_local_solver_spec

    register_local_solver_spec(TELEMAC_SOLVER_NAME, telemac_local_spec)


# Register at import so run_solver(solver='telemac_river_dye') with
# TRID3NT_SOLVER_BACKEND=local-docker dispatches to the docker spec.
register_telemac_local_spec()


# --------------------------------------------------------------------------- #
# TOMAWAC spectral-wave solver -- SAME worker image, a manifest['wave']
# block routing the entrypoint to the tomawac pipeline through the baked tomawac
# binary. A DISTINCT solver name so the run listing / showcase separates a wave
# field from a river-dye run and the completion carries wave-specific keys.
# --------------------------------------------------------------------------- #
TOMAWAC_SOLVER_NAME: str = "tomawac_wave"

#: Wave metrics keys folded into completion.json.
_WAVE_COMPLETION_METRIC_KEYS: tuple[str, ...] = (
    "correct_end", "wave_mode", "bathy_source", "result_slf", "geometry_slf",
    "npoin", "nelem", "utm_epsg", "hs_max_m", "hs_mean_m", "hs_upwind_m",
    "hs_downwind_m", "peak_period_max_s", "wind_speed_mps", "wind_dir_from_deg",
    "depth_max_m", "depth_mean_m", "dx_m", "coarsened", "n_wet_nodes", "wall_s",
    "error_code",
)


def _classify_wave_exit(
    rundir: Path, exit_code: int
) -> tuple[str, int, str | None, dict[str, Any]]:
    """Resolve wave-run status from telemac_metrics.json (dye classify analogue)."""
    metrics: dict[str, Any] = {}
    metrics_path = rundir / _METRICS_FILENAME
    try:
        if metrics_path.exists():
            loaded = json.loads(metrics_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                metrics = loaded
    except Exception as exc:  # noqa: BLE001
        logger.warning("tomawac classify_exit: metrics read failed %s: %s", metrics_path, exc)
    extra: dict[str, Any] = {
        k: metrics[k] for k in _WAVE_COMPLETION_METRIC_KEYS if k in metrics
    }
    correct_end = bool(metrics.get("correct_end"))
    if exit_code != 0:
        status = "error"
        error: str | None = (
            metrics.get("error") or f"tomawac_wave exited with non-zero code {exit_code}")
    elif metrics and not correct_end:
        status, exit_code = "error", 2
        error = metrics.get("error") or "TOMAWAC did not reach CORRECT END OF RUN"
    else:
        status, exit_code, error = "ok", 0, None
    return status, exit_code, error, extra


def tomawac_local_spec() -> "Any":
    """Build the TOMAWAC ``LocalSolverSpec`` -- same image + volume mount as the
    river-dye spec, a wave-specific classify_exit. Runs with NO NETWORK: its bed
    arrives staged in the run directory."""
    from trid3nt_server.workflows.solver.solver import LOCAL_DOCKER_WORKFLOW_NAME, LocalSolverSpec

    return LocalSolverSpec(
        solver=TOMAWAC_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="telemac_args",
        build_argv=_telemac_build_argv(_telemac_image()),
        network="none",
        stdout_name="tomawac.stdout",
        stderr_name="tomawac.stderr",
        stdout_uri_field="tomawac_stdout_uri",
        stderr_uri_field="tomawac_stderr_uri",
        exec_kind="docker",
        classify_exit=_classify_wave_exit,
    )


def register_tomawac_solver() -> None:
    """Register ``'tomawac_wave'`` in the solver + local-spec registries."""
    from trid3nt_server.workflows.solver.solver import (
        LOCAL_DOCKER_WORKFLOW_NAME,
        SOLVER_WORKFLOW_REGISTRY,
        register_local_solver_spec,
    )

    SOLVER_WORKFLOW_REGISTRY.setdefault(TOMAWAC_SOLVER_NAME, LOCAL_DOCKER_WORKFLOW_NAME)
    register_local_solver_spec(TOMAWAC_SOLVER_NAME, tomawac_local_spec)


register_tomawac_solver()


# --------------------------------------------------------------------------- #
# ARTEMIS phase-resolving harbour-agitation solver -- SAME worker
# image, a manifest['agitation'] block routing the entrypoint to the artemis
# pipeline through the baked artemis binary. A DISTINCT solver name so the run
# listing / showcase separates a harbour-agitation field from a wave / river-dye
# run and the completion carries agitation-specific keys.
# --------------------------------------------------------------------------- #
ARTEMIS_SOLVER_NAME: str = "artemis_agitation"

#: Agitation metrics keys folded into completion.json.
_AGITATION_COMPLETION_METRIC_KEYS: tuple[str, ...] = (
    "correct_end", "wave_mode", "bathy_source", "result_slf",
    "agitation_field_slf", "npoin", "nelem", "utm_epsg", "kd_max", "hs_max_m",
    "kd_sheltered", "kd_exposed", "sheltering_ratio", "resonant_period_s",
    "response_at_resonance", "response_off_resonance", "kd_focus_peak",
    "wave_period_s", "wave_dir_deg", "wave_height_m", "reflection_coef",
    "dx_m", "depth_mean_m", "depth_max_m", "coarsened", "n_wet_nodes",
    "wall_s", "error_code",
)


def _classify_agitation_exit(
    rundir: Path, exit_code: int
) -> tuple[str, int, str | None, dict[str, Any]]:
    """Resolve agitation-run status from telemac_metrics.json (dye classify analogue)."""
    metrics: dict[str, Any] = {}
    metrics_path = rundir / _METRICS_FILENAME
    try:
        if metrics_path.exists():
            loaded = json.loads(metrics_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                metrics = loaded
    except Exception as exc:  # noqa: BLE001
        logger.warning("artemis classify_exit: metrics read failed %s: %s", metrics_path, exc)
    extra: dict[str, Any] = {
        k: metrics[k] for k in _AGITATION_COMPLETION_METRIC_KEYS if k in metrics
    }
    correct_end = bool(metrics.get("correct_end"))
    if exit_code != 0:
        status = "error"
        error: str | None = (
            metrics.get("error") or f"artemis_agitation exited with non-zero code {exit_code}")
    elif metrics and not correct_end:
        status, exit_code = "error", 2
        error = metrics.get("error") or "ARTEMIS did not reach CORRECT END OF RUN"
    else:
        status, exit_code, error = "ok", 0, None
    return status, exit_code, error, extra


def artemis_local_spec() -> "Any":
    """Build the ARTEMIS ``LocalSolverSpec`` -- same image + volume mount as the
    river-dye/tomawac specs, an agitation classify_exit. Runs with NO NETWORK: its
    bed arrives staged in the run directory."""
    from trid3nt_server.workflows.solver.solver import LOCAL_DOCKER_WORKFLOW_NAME, LocalSolverSpec

    return LocalSolverSpec(
        solver=ARTEMIS_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="telemac_args",
        build_argv=_telemac_build_argv(_telemac_image()),
        network="none",
        stdout_name="artemis.stdout",
        stderr_name="artemis.stderr",
        stdout_uri_field="artemis_stdout_uri",
        stderr_uri_field="artemis_stderr_uri",
        exec_kind="docker",
        classify_exit=_classify_agitation_exit,
    )


def register_artemis_solver() -> None:
    """Register ``'artemis_agitation'`` in the solver + local-spec registries."""
    from trid3nt_server.workflows.solver.solver import (
        LOCAL_DOCKER_WORKFLOW_NAME,
        SOLVER_WORKFLOW_REGISTRY,
        register_local_solver_spec,
    )

    SOLVER_WORKFLOW_REGISTRY.setdefault(ARTEMIS_SOLVER_NAME, LOCAL_DOCKER_WORKFLOW_NAME)
    register_local_solver_spec(ARTEMIS_SOLVER_NAME, artemis_local_spec)


register_artemis_solver()


# --------------------------------------------------------------------------- #
# TELEMAC-3D stratified / 3D-hydrodynamics solver -- SAME worker
# image, a manifest['stratified'] block routing the entrypoint to the telemac3d
# pipeline through the baked telemac3d binary. A DISTINCT solver name so the run
# listing / showcase separates a 3D stratified field from a wave / agitation /
# river-dye run and the completion carries 3D-specific keys.
# --------------------------------------------------------------------------- #
TELEMAC3D_SOLVER_NAME: str = "telemac3d_strat"

#: 3D metrics keys folded into completion.json.
_STRAT_COMPLETION_METRIC_KEYS: tuple[str, ...] = (
    "correct_end", "flow_mode", "bathy_source", "result_slf",
    "surface_field_slf", "bottom_field_slf", "npoin", "nelem", "nplan",
    "utm_epsg", "stratification_metric", "variable_label", "variable_units",
    "stratification_dt", "stratification_dt_init", "u_surface", "u_bottom",
    "depth_avg_u", "front_speed_mps", "benjamin_speed_mps", "front_ratio",
    "non_hydrostatic", "surface_value_mean", "bottom_value_mean",
    "wind_speed_mps", "dx_m", "depth_max_m", "depth_mean_m", "coarsened",
    "n_wet_nodes", "wall_s", "error_code",
)


def _classify_strat_exit(
    rundir: Path, exit_code: int
) -> tuple[str, int, str | None, dict[str, Any]]:
    """Resolve 3D-run status from telemac_metrics.json (dye classify analogue)."""
    metrics: dict[str, Any] = {}
    metrics_path = rundir / _METRICS_FILENAME
    try:
        if metrics_path.exists():
            loaded = json.loads(metrics_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                metrics = loaded
    except Exception as exc:  # noqa: BLE001
        logger.warning("telemac3d classify_exit: metrics read failed %s: %s", metrics_path, exc)
    extra: dict[str, Any] = {
        k: metrics[k] for k in _STRAT_COMPLETION_METRIC_KEYS if k in metrics
    }
    correct_end = bool(metrics.get("correct_end"))
    if exit_code != 0:
        status = "error"
        error: str | None = (
            metrics.get("error") or f"telemac3d_strat exited with non-zero code {exit_code}")
    elif metrics and not correct_end:
        status, exit_code = "error", 2
        error = metrics.get("error") or "TELEMAC-3D did not reach CORRECT END OF RUN"
    else:
        status, exit_code, error = "ok", 0, None
    return status, exit_code, error, extra


def telemac3d_local_spec() -> "Any":
    """Build the TELEMAC-3D ``LocalSolverSpec`` -- same image + volume mount as the
    river-dye/tomawac/artemis specs, a 3D classify_exit. Runs with NO NETWORK: its
    bed arrives staged in the run directory."""
    from trid3nt_server.workflows.solver.solver import LOCAL_DOCKER_WORKFLOW_NAME, LocalSolverSpec

    return LocalSolverSpec(
        solver=TELEMAC3D_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="telemac_args",
        build_argv=_telemac_build_argv(_telemac_image()),
        network="none",
        stdout_name="telemac3d.stdout",
        stderr_name="telemac3d.stderr",
        stdout_uri_field="telemac3d_stdout_uri",
        stderr_uri_field="telemac3d_stderr_uri",
        exec_kind="docker",
        classify_exit=_classify_strat_exit,
    )


def register_telemac3d_solver() -> None:
    """Register ``'telemac3d_strat'`` in the solver + local-spec registries."""
    from trid3nt_server.workflows.solver.solver import (
        LOCAL_DOCKER_WORKFLOW_NAME,
        SOLVER_WORKFLOW_REGISTRY,
        register_local_solver_spec,
    )

    SOLVER_WORKFLOW_REGISTRY.setdefault(TELEMAC3D_SOLVER_NAME, LOCAL_DOCKER_WORKFLOW_NAME)
    register_local_solver_spec(TELEMAC3D_SOLVER_NAME, telemac3d_local_spec)


register_telemac3d_solver()


# --------------------------------------------------------------------------- #
# COASTAL tidal/surge inundation solver -- SAME worker image, a
# manifest['coastal'] block routing the entrypoint to the coastal pipeline
# (telemac_coastal_build) through the baked telemac2d binary -- an open-water
# domain with ONE seaward liquid boundary forced by a LIQUID BOUNDARIES FILE.
# A DISTINCT solver name so the run listing / showcase separates a coastal
# tidal/surge run from every other leg and the completion carries coastal keys.
# --------------------------------------------------------------------------- #
TELEMAC_COASTAL_SOLVER_NAME: str = "telemac_coastal"

#: Coastal metrics keys folded into completion.json.
_COASTAL_COMPLETION_METRIC_KEYS: tuple[str, ...] = (
    "correct_end", "mode", "result_slf", "geometry_slf", "cli", "cas",
    "npoin", "nelem", "nx", "ny", "utm_epsg", "dx_m", "coarsened",
    "ocean_edge", "n_ocean_nodes", "n_wet_nodes", "depth_max_m", "topo_max_m",
    "bathy_source", "init_wl_m", "duration_s", "series_datum", "datum_offset_m",
    "liqbnd_file", "liqbnd_rows", "liqbnd_col", "sl_min_m", "sl_max_m", "t_end_s",
    "bed_cog", "bed_cog_source", "ntimestep", "peak_wl_max_m", "final_wl_max_m",
    "flooded_land_km2", "wet_peak_km2", "n_newly_flooded_nodes", "wall_s",
    "error_code",
)


def _classify_coastal_exit(
    rundir: Path, exit_code: int
) -> tuple[str, int, str | None, dict[str, Any]]:
    """Resolve coastal-run status from telemac_metrics.json (dye classify analogue)."""
    metrics: dict[str, Any] = {}
    metrics_path = rundir / _METRICS_FILENAME
    try:
        if metrics_path.exists():
            loaded = json.loads(metrics_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                metrics = loaded
    except Exception as exc:  # noqa: BLE001
        logger.warning("coastal classify_exit: metrics read failed %s: %s", metrics_path, exc)
    extra: dict[str, Any] = {
        k: metrics[k] for k in _COASTAL_COMPLETION_METRIC_KEYS if k in metrics
    }
    correct_end = bool(metrics.get("correct_end"))
    if exit_code != 0:
        status = "error"
        error: str | None = (
            metrics.get("error") or f"telemac_coastal exited with non-zero code {exit_code}")
    elif metrics and not correct_end:
        status, exit_code = "error", 2
        error = metrics.get("error") or "TELEMAC-2D coastal did not reach CORRECT END OF RUN"
    else:
        status, exit_code, error = "ok", 0, None
    return status, exit_code, error, extra


def coastal_local_spec() -> "Any":
    """Build the COASTAL ``LocalSolverSpec`` -- same image + volume mount as the
    river-dye/tomawac/artemis/3D specs, a coastal classify_exit. Runs with NO
    NETWORK: its bed arrives staged in the run directory."""
    from trid3nt_server.workflows.solver.solver import LOCAL_DOCKER_WORKFLOW_NAME, LocalSolverSpec

    return LocalSolverSpec(
        solver=TELEMAC_COASTAL_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="telemac_args",
        build_argv=_telemac_build_argv(_telemac_image()),
        network="none",
        stdout_name="coastal.stdout",
        stderr_name="coastal.stderr",
        stdout_uri_field="coastal_stdout_uri",
        stderr_uri_field="coastal_stderr_uri",
        exec_kind="docker",
        classify_exit=_classify_coastal_exit,
    )


def register_coastal_solver() -> None:
    """Register ``'telemac_coastal'`` in the solver + local-spec registries."""
    from trid3nt_server.workflows.solver.solver import (
        LOCAL_DOCKER_WORKFLOW_NAME,
        SOLVER_WORKFLOW_REGISTRY,
        register_local_solver_spec,
    )

    SOLVER_WORKFLOW_REGISTRY.setdefault(TELEMAC_COASTAL_SOLVER_NAME, LOCAL_DOCKER_WORKFLOW_NAME)
    register_local_solver_spec(TELEMAC_COASTAL_SOLVER_NAME, coastal_local_spec)


register_coastal_solver()
