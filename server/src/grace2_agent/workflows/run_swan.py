"""SWAN deck-build-spec assembly + staging + Batch-dispatch registration
(Phase 1 -- the SWAN analogue of ``run_geoclaw.py``).

One module owns the SWAN engine's solver-dispatch surface. Like GeoClaw, SWAN is
a Fortran solver that lives ONLY in the worker container image (GPL isolation: the
SWAN binary NEVER enters the agent venv -- the agent only composes a JSON
build_spec). So SWAN is BATCH-PRIMARY: the agent stages a ``build_spec`` (the typed
run args) + a topo/bathy DEM reference to S3 and dispatches through the SAME
generic ``run_solver`` / ``wait_for_completion`` seam SFINCS/GeoClaw use, then
downloads the SWAN output (``swan_out.mat``) and postprocesses it.

  1. **build_spec assembly + staging** (``stage_swan_manifest``). Builds the
     worker-contract manifest (``inputs[]`` = the bathy DEM + optional wind grid;
     ``build_spec`` = the deck_builder field dict; ``outputs`` = the SWAN output
     globs) and uploads it + a DEM reference to the cache bucket, returning the
     ``manifest.json`` URI to feed ``run_solver(solver='swan', ...)``.

  2. **SWAN solver registration** (``register_swan_solver``). Adds ``'swan'`` to
     ``SOLVER_WORKFLOW_REGISTRY`` (idempotent ``setdefault``, mirroring
     ``register_geoclaw_solver``) so ``run_solver(solver='swan')`` dispatches. The
     per-solver Batch job-def resolves from ``GRACE2_AWS_BATCH_JOB_DEF_SWAN`` and
     stays INERT (honest typed error) until NATE flips that env after ``tofu
     apply`` registers the job-def -- exactly the SWMM/GeoClaw posture.

Determinism boundary (Invariant 1 / 2): no LLM call anywhere in this module. The
deck is authored deterministically (in the worker, via deck_builder); every wave
number the agent narrates comes from the typed ``WaveFieldLayerURI`` fields the
postprocess computed -- never free-generated.

LATER-STEP SEAM (do NOT wire in v0.1): a one-way SWAN->SFINCS wave-setup coupling
(``wave_setup_forcing_from_swan``) would convert SWAN's radiation-stress gradient
into a bzs water-level offset and inject it through the SFINCS forcing seam. The
engine spike marks this a LATER step; v0.1 is STANDALONE wave-field only. This
module deliberately exposes NO ``wave`` forcing member and does NOT touch
``sfincs_forcing_adapter``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from grace2_contracts import new_ulid
from grace2_contracts.swan_contracts import (
    DEFAULT_BOUNDARY_DIR_DEG,
    DEFAULT_BOUNDARY_HS_M,
    DEFAULT_BOUNDARY_SPREAD_DEG,
    DEFAULT_BOUNDARY_TP_S,
    SwanRunArgs,
    SwanWaveBoundary,
)

logger = logging.getLogger("grace2_agent.workflows.run_swan")

__all__ = [
    "SwanWorkflowError",
    "SwanStaging",
    "synthesize_demo_wave_boundary",
    "build_swan_build_spec",
    "stage_swan_manifest",
    "register_swan_solver",
    "SWAN_SOLVER_NAME",
    "SWAN_OUTPUT_GLOBS",
]


#: The registry key + handle ``solver`` tag for the SWAN engine.
SWAN_SOLVER_NAME: str = "swan"

#: SWAN output globs the postprocess reads (the gridded Matlab output + the echoed
#: deck manifest + the SWAN PRINT/Errfile diagnostics for the honesty gate).
SWAN_OUTPUT_GLOBS: list[str] = [
    "swan_out.mat",
    "deck_manifest.json",
    "PRINT",
    "Errfile",
    "swan.stdout",
    "swan.stderr",
]


# --------------------------------------------------------------------------- #
# Errors (mirrors GeoClawWorkflowError shape).
# --------------------------------------------------------------------------- #
class SwanWorkflowError(RuntimeError):
    """Raised on any deck-spec / staging / dispatch failure.

    Carries an open-set A.6 ``error_code`` so the agent emitter renders a typed
    error frame. Codes:

    - ``SWAN_PARAMS_INVALID`` -- the run args could not be coerced.
    - ``SWAN_STAGING_FAILED`` -- the build_spec / DEM upload failed.
    - ``SWAN_RUN_FAILED`` -- the Batch solve did not complete.
    - ``SWAN_BATCH_OUTPUT_MISSING`` -- a 'complete' solve produced no wave field.
    """

    error_code: str = "SWAN_WORKFLOW_FAILED"

    def __init__(
        self,
        error_code: str,
        *,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message or error_code)
        self.error_code = error_code
        self.details: dict[str, Any] = dict(details or {})


# --------------------------------------------------------------------------- #
# Staging result -- the Batch-lane handoff (mirrors GeoClawStaging).
# --------------------------------------------------------------------------- #
@dataclass
class SwanStaging:
    """The result of assembling + staging a SWAN build_spec + DEM.

    Fields:
        run_id: the run identifier the output COGs are keyed under.
        manifest_uri: the ``s3://`` URI of the staged ``manifest.json``.
        build_spec: the deck_builder field dict that was staged.
        run_args: the validated ``SwanRunArgs`` (echoed for provenance).
        bbox: the AOI the postprocess rasterizes onto.
    """

    run_id: str
    manifest_uri: str
    build_spec: dict[str, Any]
    run_args: SwanRunArgs
    bbox: tuple[float, float, float, float]
    n_active_cells: int = 0
    resolution_m: float = 0.0
    staged_inputs: list[dict[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Parametric demo wave-boundary synthesis (the section-5a LLM-easy path).
# --------------------------------------------------------------------------- #
def synthesize_demo_wave_boundary(
    bbox: tuple[float, float, float, float],
) -> SwanWaveBoundary:
    """Synthesize a demo parametric offshore wave boundary for the AOI.

    The Phase-1 stand-in for the ERA5 Hs/Tp/Dir triple (the spike's section-5a
    cheap boundary). Returns a moderate-storm sea-state on the side most likely to
    face open water. The chosen ``side`` is the LONGER horizontal vs vertical
    extent heuristic: a wide (E-W) AOI is forced from the South side (a typical US
    Gulf/Atlantic offshore-facing seaward boundary); a tall (N-S) AOI from the
    East side. This is a DEMO default -- narrated as such by the composer, NOT a
    calibrated forcing.

    NOTE (LATER step): a real ERA5-derived boundary would extend
    ``fetch_era5_reanalysis`` with mean_wave_period + mean_wave_direction (the
    spike's section-5a) and feed those here. v0.1 uses the demo values; the seam
    is the ``SwanRunArgs.boundary`` field the agent/composer may already set.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    width = max_lon - min_lon
    height = max_lat - min_lat
    # For the US West Coast (lon < -100) the open Pacific is to the WEST.
    # For Gulf/Atlantic coasts the ocean is typically to the SOUTH.
    # Wide (E-W) AOIs default to S; tall (N-S) AOIs default to E.
    # West-coast override: any AOI west of -100 deg uses the W boundary.
    center_lon = (min_lon + max_lon) / 2.0
    if center_lon < -100.0:
        side = "W"
    else:
        side = "S" if width >= height else "E"
    # Set dir_deg consistent with the chosen side (waves coming FROM that side).
    side_inward_dir = {"N": 0.0, "E": 90.0, "S": 180.0, "W": 270.0}
    dir_deg = side_inward_dir[side]
    return SwanWaveBoundary(
        hs_m=DEFAULT_BOUNDARY_HS_M,
        tp_s=DEFAULT_BOUNDARY_TP_S,
        dir_deg=dir_deg,
        spread_deg=DEFAULT_BOUNDARY_SPREAD_DEG,
        side=side,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Boundary side/direction consistency.
# --------------------------------------------------------------------------- #
#: Nautical "coming-from" direction (deg) for an inward-facing boundary on each
#: AOI side: a boundary on the SOUTH edge faces open water to the south, so its
#: waves come FROM the south (180 deg) and propagate NORTH into the domain.
_SIDE_INWARD_DIR_DEG: dict[str, float] = {"N": 0.0, "E": 90.0, "S": 180.0, "W": 270.0}


def _angular_distance_deg(a: float, b: float) -> float:
    """Smallest absolute angle (deg, 0..180) between two nautical bearings."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _coerce_boundary_inward(boundary: SwanWaveBoundary) -> SwanWaveBoundary:
    """Force the boundary's wave direction to drive energy INTO the domain.

    SWAN ``BOUN SIDE`` only injects the part of the imposed directional spectrum
    that travels INTO the grid. A boundary on side X faces open water beyond edge
    X, so physically the waves must come FROM that side (nautical "coming-from"
    direction = the side's compass bearing, see ``_SIDE_INWARD_DIR_DEG``). When
    the LLM supplies a ``(side, dir)`` pair pointing the energy OUT through the
    same edge (e.g. the live 2026-06-23 Mexico Beach run: side=S with dir=0 deg,
    waves "from the north" imposed on the SOUTHERN open-water boundary), almost
    no energy enters -- SWAN paints only a razor-thin boundary sliver and the
    wave raster looks empty. If the supplied direction is more than 90 deg from
    the side-inward bearing (i.e. the energy is net-outgoing) we snap it to the
    side-inward bearing and log it. A sane pair (e.g. side=S, dir=160) is within
    90 deg and left untouched, so a deliberately oblique sea-state is preserved.
    """
    inward = _SIDE_INWARD_DIR_DEG.get(str(boundary.side).strip().upper())
    if inward is None:
        return boundary
    if _angular_distance_deg(float(boundary.dir_deg), inward) <= 90.0:
        return boundary
    logger.info(
        "swan boundary: dir=%.1f deg on side %s is net-OUTGOING (>90 deg from "
        "the side-inward bearing %.1f deg); snapping to %.1f deg so wave energy "
        "enters the domain (was an empty-raster no-op)",
        float(boundary.dir_deg),
        boundary.side,
        inward,
        inward,
    )
    return boundary.model_copy(update={"dir_deg": inward})


# --------------------------------------------------------------------------- #
# build_spec assembly.
# --------------------------------------------------------------------------- #
def build_swan_build_spec(
    run_args: SwanRunArgs,
    *,
    bottom_dest: str = "bottom.bot",
    wind_dest: str | None = None,
    mesh_cells: tuple[int, int] = (100, 100),
) -> dict[str, Any]:
    """Assemble the deck_builder ``build_spec`` dict from the validated run args.

    The single source of truth for the worker-side deck author's input. Maps the
    typed ``SwanRunArgs`` onto the flat build_spec the worker's
    ``deck_builder.parse_build_spec`` consumes. The staged DEM is referenced by its
    in-deck destination filename (``bottom_dest``); a staged wind grid is referenced
    by ``wind_dest`` when present. When ``run_args.boundary`` is ``None`` a demo
    boundary is synthesized from the AOI (so the deck always has a valid boundary).

    Pure dict assembly -- unit-testable with no network (mirrors
    ``build_geoclaw_build_spec``).
    """
    boundary = run_args.boundary or synthesize_demo_wave_boundary(tuple(run_args.bbox))
    boundary = _coerce_boundary_inward(boundary)

    spec: dict[str, Any] = {
        "mode": run_args.mode,
        "bbox": list(run_args.bbox),
        "bottom_file": bottom_dest,
        "mx": int(mesh_cells[0]),
        "my": int(mesh_cells[1]),
        "n_dir": int(run_args.n_dir),
        "n_freq": int(run_args.n_freq),
        "freq_low_hz": float(run_args.freq_low_hz),
        "freq_high_hz": float(run_args.freq_high_hz),
        "boundary": {
            "hs_m": float(boundary.hs_m),
            "tp_s": float(boundary.tp_s),
            "dir_deg": float(boundary.dir_deg),
            "spread_deg": float(boundary.spread_deg),
            "side": boundary.side,
        },
        "friction": bool(run_args.friction),
        "breaking": bool(run_args.breaking),
        "triads": bool(run_args.triads),
        "sim_duration_s": float(run_args.sim_duration_s),
        "time_step_s": float(run_args.time_step_s),
        "output_frames": int(run_args.output_frames),
        "output_quantities": ["HSIGN", "RTP", "DIR"],
    }
    if run_args.wind_uri is not None and wind_dest is not None:
        spec["wind_file"] = wind_dest
    return spec


# --------------------------------------------------------------------------- #
# Staging -- upload the build_spec manifest + the bathy DEM reference to S3.
# --------------------------------------------------------------------------- #
def stage_swan_manifest(
    run_args: SwanRunArgs,
    *,
    dem_uri: str,
    run_id: str | None = None,
    wind_uri: str | None = None,
    mesh_cells: tuple[int, int] = (100, 100),
) -> SwanStaging:
    """Stage the SWAN ``manifest.json`` (build_spec + input refs) to S3.

    The SWAN analogue of ``stage_geoclaw_manifest``. Mirrors that path EXACTLY (no
    new client): the same ``cache.storage_scheme()`` scheme + the same
    ``tools.solver._get_s3_client()`` boto3 client + the same
    ``GRACE2_CACHE_BUCKET`` staging bucket the SFINCS/GeoClaw decks upload to.

    The worker downloads the bathy DEM (and optional wind grid) listed in
    ``inputs[]`` BY SCHEME, samples the DEM onto the SWAN bottom input grid, and
    authors the ``.swn`` from ``build_spec``. ``dem_uri`` is a cache/runs ``s3://``
    URI produced by ``fetch_topobathy`` upstream (staged BY REFERENCE -- the worker
    downloads it directly).

    Args:
        run_args: the validated ``SwanRunArgs``.
        dem_uri: the ``s3://`` URI of the topo/bathy DEM (the worker references it
            as ``bathy.tif`` and samples it onto the SWAN bottom input grid).
        run_id: optional ULID; minted if absent.
        wind_uri: optional ``s3://`` URI of a staged ERA5 wind grid.
        mesh_cells: the SWAN computational + bottom-input mesh resolution.

    Returns:
        ``SwanStaging`` carrying the manifest URI + the build_spec + bbox.

    Raises:
        SwanWorkflowError("SWAN_STAGING_FAILED"): the upload could not complete
            (the Batch lane cannot dispatch without a reachable manifest -- fail
            loudly, never a silent dead-end).
    """
    from ..tools.cache import storage_scheme
    from ..tools.solver import _get_s3_client

    rid = run_id or new_ulid()
    bbox = tuple(run_args.bbox)

    # Stage the DEM BY REFERENCE; the worker downloads it as bathy.tif then samples
    # it onto the SWAN bottom input grid (bottom.bot is AUTHORED by the worker, not
    # staged -- so the staged input is the raw DEM, the deck's bottom_file is the
    # name the worker writes the sampled array to).
    inputs: list[dict[str, str]] = [{"gs_uri": dem_uri, "dest": "bathy.tif"}]
    wind_dest: str | None = None
    if run_args.wind_uri or wind_uri:
        wind_dest = "wind.dat"
        inputs.append({"gs_uri": wind_uri or run_args.wind_uri, "dest": wind_dest})

    build_spec = build_swan_build_spec(
        run_args,
        bottom_dest="bottom.bot",
        wind_dest=wind_dest,
        mesh_cells=mesh_cells,
    )

    scheme = storage_scheme()  # "s3" on AWS (GCP decommissioned)
    cache_bucket = os.environ.get("GRACE2_CACHE_BUCKET", "grace-2-hazard-prod-cache")
    prefix = f"cache/static-30d/swan_setup/{rid}/"
    manifest_key = f"{prefix}manifest.json"
    manifest_uri = f"{scheme}://{cache_bucket}/{manifest_key}"

    manifest_dict: dict[str, Any] = {
        "inputs": inputs,
        "build_spec": build_spec,
        "outputs": list(SWAN_OUTPUT_GLOBS),
        "swan_args": ["--run-id", rid, "--manifest-uri", manifest_uri],
    }

    try:
        s3 = _get_s3_client()
        s3.put_object(
            Bucket=cache_bucket,
            Key=manifest_key,
            Body=json.dumps(manifest_dict, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        raise SwanWorkflowError(
            "SWAN_STAGING_FAILED",
            message=f"failed to stage SWAN manifest to {manifest_uri}: {exc}",
            details={"run_id": rid, "manifest_uri": manifest_uri},
        ) from exc

    logger.info(
        "stage_swan_manifest run_id=%s mode=%s dem=%s -> manifest=%s",
        rid,
        run_args.mode,
        dem_uri,
        manifest_uri,
    )
    # n_active_cells used only for telemetry + compute-class sizing; the mesh cell
    # count is a coarse proxy for the spectral solve cost.
    n_active = int(mesh_cells[0]) * int(mesh_cells[1])
    return SwanStaging(
        run_id=rid,
        manifest_uri=manifest_uri,
        build_spec=build_spec,
        run_args=run_args,
        bbox=bbox,  # type: ignore[arg-type]
        n_active_cells=n_active,
        staged_inputs=inputs,
    )


# --------------------------------------------------------------------------- #
# SWAN solver registration (mirrors register_geoclaw_solver).
# --------------------------------------------------------------------------- #
def register_swan_solver() -> None:
    """Register ``'swan'`` in ``tools.solver.SOLVER_WORKFLOW_REGISTRY``.

    Mirrors ``register_geoclaw_solver``. SWAN is Batch-only (the GPL Fortran lives
    in the worker image, never in the agent venv). ``run_solver`` only requires
    the KEY to be present to dispatch (the local-docker backend seam routes to
    ``_run_solver_local_docker``). Idempotent ``setdefault`` -- safe to call at
    import. The orchestrator may ALSO pin a static literal in
    ``SOLVER_WORKFLOW_REGISTRY`` (the composer name); if so it is evaluated first
    and this ``setdefault`` is a no-op. (The registry value is a presence-gate
    only; the local sentinel is used since the AWS Batch arm was removed.)
    """
    from ..tools.solver import LOCAL_DOCKER_WORKFLOW_NAME, SOLVER_WORKFLOW_REGISTRY

    SOLVER_WORKFLOW_REGISTRY.setdefault(SWAN_SOLVER_NAME, LOCAL_DOCKER_WORKFLOW_NAME)


# Register at import so ``run_solver(solver='swan')`` is wired wherever this
# module is imported (the composer + the tool wrapper both import it).
register_swan_solver()


# --------------------------------------------------------------------------- #
# SWAN LocalSolverSpec -- docker runner for the local-docker backend.
# --------------------------------------------------------------------------- #

#: Default SWAN image under local-docker (env GRACE2_SWAN_IMAGE).
DEFAULT_SWAN_IMAGE: str = "trid3nt-local/swan:latest"


def swan_local_spec() -> "Any":
    """Build the SWAN LocalSolverSpec for the local-docker backend."""
    import os
    from pathlib import Path
    from ..tools.solver import LOCAL_DOCKER_WORKFLOW_NAME, LocalSolverSpec

    image = os.environ.get("GRACE2_SWAN_IMAGE") or DEFAULT_SWAN_IMAGE
    aws_endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    aws_access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    aws_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    aws_region = os.environ.get("AWS_REGION", "us-east-1")
    runs_bucket = os.environ.get("GRACE2_RUNS_BUCKET", "trid3nt-runs")

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        # args comes from manifest["swan_args"] = ["--run-id", rid, "--manifest-uri", uri]
        # Replace any staging --run-id with the launcher's run_id so container
        # outputs land under the same S3 prefix that the supervisor polls.
        fixed_args = list(args)
        if "--run-id" in fixed_args:
            idx = fixed_args.index("--run-id")
            fixed_args[idx + 1] = run_id
        else:
            fixed_args = ["--run-id", run_id] + fixed_args
        cmd = [
            "docker", "run", "--rm",
            "--name", run_id,
            "--network", "host",
        ]
        env_pairs = [
            ("GRACE2_RUNS_BUCKET", runs_bucket),
            ("GRACE2_OBJECT_STORE", "s3"),
            ("GRACE2_SWAN_SCRATCH", "/opt/grace2/work"),
            ("AWS_REGION", aws_region),
            ("OMP_NUM_THREADS", "4"),
            ("PYTHONUNBUFFERED", "1"),
        ]
        if aws_endpoint:
            env_pairs.append(("AWS_ENDPOINT_URL", aws_endpoint))
        if aws_access_key:
            env_pairs.append(("AWS_ACCESS_KEY_ID", aws_access_key))
        if aws_secret_key:
            env_pairs.append(("AWS_SECRET_ACCESS_KEY", aws_secret_key))
        for k, v in env_pairs:
            cmd += ["-e", f"{k}={v}"]
        cmd.append(image)
        cmd.extend(fixed_args)
        return cmd

    return LocalSolverSpec(
        solver=SWAN_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="swan_args",
        build_argv=build_argv,
        stdout_name="swan.stdout",
        stderr_name="swan.stderr",
        stdout_uri_field="swan_stdout_uri",
        stderr_uri_field="swan_stderr_uri",
        exec_kind="docker",
        classify_exit=None,
    )


def register_swan_local_spec() -> None:
    """Register the SWAN LocalSolverSpec factory for the local-docker backend."""
    from ..tools.solver import register_local_solver_spec
    register_local_solver_spec(SWAN_SOLVER_NAME, swan_local_spec)


# Register at import so run_solver(solver='swan') with
# GRACE2_SOLVER_BACKEND=local-docker dispatches to the docker spec.
register_swan_local_spec()
