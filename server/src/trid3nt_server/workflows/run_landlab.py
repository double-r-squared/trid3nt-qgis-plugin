"""Landlab surface-process engine — solver name + deck staging (sprint-17).

The Landlab analogue of ``run_swmm`` / ``run_modflow``'s staging half. Landlab
is a NEW engine and runs OFF-BOX ONLY (the scale-to-zero island norm: heavy /
sandboxed compute on AWS Batch, the agent box stays tiny) — there is no
in-process lane. So this module is thinner than ``run_swmm`` (no local-exec
spec): it stages the DEM COG + a worker-contract ``manifest.json`` carrying the
``build_spec`` to S3, and returns the manifest URI to feed STRAIGHT to
``run_solver(solver='landlab', model_setup_uri=<manifest>, ...)``.

The Landlab worker (``services/workers/landlab/entrypoint.py``) reads the
manifest, downloads the DEM, BUILDS a ``RasterModelGrid`` over the AOI, runs the
documented component chain (LandslideProbability / OverlandFlow), and writes the
output field COG + completion.json (with the typed narration ``result`` block)
to ``s3://<runs_bucket>/<run_id>/`` — the SAME completion poll
(``wait_for_completion``) every other Batch solver reuses.

Determinism boundary (Invariant 2): no LLM in the chain — the worker runs the
documented Landlab components deterministically.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from trid3nt_contracts.landlab_contracts import LandlabRunArgs

logger = logging.getLogger("trid3nt_server.workflows.run_landlab")

__all__ = [
    "LandlabWorkflowError",
    "LandlabStaging",
    "stage_landlab_manifest",
    "build_landlab_build_spec",
    "LANDLAB_SOLVER_NAME",
    "landlab_local_spec",
    "register_landlab_solver",
]

#: The registry key + handle ``solver`` tag for the Landlab surface-process
#: engine. The orchestrator wires SOLVER_WORKFLOW_REGISTRY["landlab"] +
#: TRID3NT_AWS_BATCH_JOB_DEF_LANDLAB (the shared-append snippets) — NOT this lane.
LANDLAB_SOLVER_NAME: str = "landlab"


class LandlabWorkflowError(RuntimeError):
    """Raised on any DEM-fetch / staging / Batch-output failure.

    Carries an open-set A.6 ``error_code`` so the agent emitter renders a typed
    error frame (the emitter's ``_classify_exception`` reads ``error_code`` off
    the exception). Codes:

    - ``LANDLAB_PARAMS_INVALID`` — the run args could not be coerced.
    - ``LANDLAB_DEM_FETCH_FAILED`` — both DEM sources failed for the AOI.
    - ``LANDLAB_STAGING_FAILED`` — the DEM/manifest upload could not complete.
    - ``LANDLAB_BATCH_OUTPUT_MISSING`` — a 'complete' solve produced no field COG.
    - ``LANDLAB_RUN_FAILED`` — the Batch solve did not complete.
    """

    error_code: str = "LANDLAB_WORKFLOW_FAILED"

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


@dataclass
class LandlabStaging:
    """The result of staging a Landlab run (the DEM COG + build_spec manifest).

    Fields:
        run_id: the run identifier the staged objects + output COGs are keyed
            under.
        manifest_uri: the ``s3://`` URI of the uploaded ``manifest.json``
            (feed STRAIGHT to ``run_solver(solver='landlab', ...)``).
        dem_uri: the ``s3://`` URI of the staged DEM COG.
        run_args: the validated ``LandlabRunArgs`` (echoed for provenance).
        build_spec: the ``build_spec`` dict embedded in the manifest (echoed for
            provenance / telemetry).
    """

    run_id: str
    manifest_uri: str
    dem_uri: str
    run_args: LandlabRunArgs
    build_spec: dict[str, Any]


def build_landlab_build_spec(run_args: LandlabRunArgs) -> dict[str, Any]:
    """Assemble the worker ``build_spec`` from the validated run args.

    The worker's ``component_chain.run_component_chain`` reads these keys; the
    set sent depends on the ``analysis`` (the landslide chain ignores the
    rainfall params and vice-versa, but sending all is harmless — the chain only
    reads the ones it needs). Pure (no IO), unit-testable on a synthetic
    ``LandlabRunArgs``.

    levers STEP 3: ``advanced_physics`` (flow_director / overland_alpha /
    mannings_n) is validated against ``PHYSICS_REGISTRY["landlab"]`` and the
    resolved keys are MERGED into the build_spec (the chain reads them directly
    at the FlowAccumulator / OverlandFlow seam). ``None`` => no keys merged =>
    byte-identical component chain. An invalid key/value raises a typed
    ``LandlabWorkflowError("LANDLAB_PHYSICS_INVALID")``.
    """
    from .physics_registry import (
        PhysicsRegistryError,
        validate_and_resolve_physics,
    )

    try:
        resolved = validate_and_resolve_physics(
            "landlab", getattr(run_args, "advanced_physics", None)
        )
    except PhysicsRegistryError as exc:
        raise LandlabWorkflowError(
            "LANDLAB_PHYSICS_INVALID",
            message=f"invalid advanced_physics: {exc}",
            details={"engine": "landlab", "key": getattr(exc, "key", None)},
        ) from exc

    spec = {
        "analysis": run_args.analysis,
        "target_resolution_m": float(run_args.target_resolution_m),
        # infinite-slope LandslideProbability parameters
        "soil_transmissivity_m2_day": float(run_args.soil_transmissivity_m2_day),
        "soil_cohesion_pa": float(run_args.soil_cohesion_pa),
        "soil_internal_friction_deg": float(run_args.soil_internal_friction_deg),
        "soil_density_kg_m3": float(run_args.soil_density_kg_m3),
        "soil_thickness_m": float(run_args.soil_thickness_m),
        "recharge_mm_day": float(run_args.recharge_mm_day),
        "n_monte_carlo": int(run_args.n_monte_carlo),
        # OverlandFlow parameters
        "rainfall_intensity_mm_hr": float(run_args.rainfall_intensity_mm_hr),
        "storm_duration_hr": float(run_args.storm_duration_hr),
    }
    # Merge the validated physics overrides (the chain reads flow_director /
    # overland_alpha / mannings_n). Absent => byte-identical.
    spec.update(resolved)
    return spec


def stage_landlab_manifest(
    run_args: LandlabRunArgs,
    *,
    dem_path: str,
    run_id: str,
) -> LandlabStaging:
    """Upload the AOI DEM COG + a worker-contract ``manifest.json`` to S3.

    Mirrors ``run_swmm.stage_swmm_manifest`` EXACTLY (no new client): the same
    ``cache.storage_scheme()`` scheme + the same
    ``tools.simulation.solver._get_s3_client()`` boto3 client + the same
    ``TRID3NT_CACHE_BUCKET`` staging bucket the SFINCS/SWMM decks upload land in.

    Writes:
      - ``.../dem.tif``       — the staged AOI DEM COG (``dem_path`` on disk).
      - ``.../manifest.json`` — the manifest the Landlab worker reads:
        ``inputs[]`` carry the legacy ``gs_uri`` field NAME with an ``s3://``
        VALUE (resolved by scheme in the worker), ``dest='dem.tif'``;
        ``build_spec`` carries the run parameters; ``dem_dest='dem.tif'``;
        ``outputs`` glob the field ``*.tif`` the postprocess reads.

    Args:
        run_args: the validated ``LandlabRunArgs``.
        dem_path: the on-disk AOI DEM GeoTIFF the worker grid is built from.
        run_id: the run id the staged objects + output COGs are keyed under.

    Returns:
        A ``LandlabStaging`` carrying the manifest URI to feed ``run_solver``.

    Raises:
        LandlabWorkflowError("LANDLAB_STAGING_FAILED"): the upload could not
            complete (the off-box lane cannot dispatch without a reachable
            manifest — fail loudly, never a silent dead-end).
    """
    from ..tools.cache import CACHE_BUCKET, storage_scheme
    from ..tools.simulation.solver import _get_s3_client

    scheme = storage_scheme()  # "s3" on AWS (GCP decommissioned)
    cache_bucket = os.environ.get("TRID3NT_CACHE_BUCKET") or CACHE_BUCKET
    # Per-run prefix under the cache bucket's staged-deck source class (mirrors
    # the SWMM swmm_setup/ prefix). The run_id keys the staged objects.
    prefix = f"cache/static-30d/landlab_setup/{run_id}/"
    dem_key = f"{prefix}dem.tif"
    manifest_key = f"{prefix}manifest.json"
    dem_uri = f"{scheme}://{cache_bucket}/{dem_key}"
    manifest_uri = f"{scheme}://{cache_bucket}/{manifest_key}"

    build_spec = build_landlab_build_spec(run_args)

    # The worker downloads inputs[] BY SCHEME (the field name is the legacy
    # ``gs_uri``; the VALUE is the s3:// URI). build_spec carries the run params;
    # outputs[] glob the field COG.
    manifest_dict: dict[str, Any] = {
        "inputs": [{"gs_uri": dem_uri, "dest": "dem.tif"}],
        "dem_dest": "dem.tif",
        "build_spec": build_spec,
        "outputs": ["*.tif"],
    }

    try:
        s3 = _get_s3_client()
        with open(dem_path, "rb") as fh:
            s3.put_object(Bucket=cache_bucket, Key=dem_key, Body=fh)
        s3.put_object(
            Bucket=cache_bucket,
            Key=manifest_key,
            Body=json.dumps(manifest_dict, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        raise LandlabWorkflowError(
            "LANDLAB_STAGING_FAILED",
            message=f"failed to stage Landlab DEM + manifest to {manifest_uri}: {exc}",
            details={"run_id": run_id, "manifest_uri": manifest_uri},
        ) from exc

    logger.info(
        "stage_landlab_manifest run_id=%s analysis=%s manifest=%s dem=%s",
        run_id,
        run_args.analysis,
        manifest_uri,
        dem_uri,
    )
    return LandlabStaging(
        run_id=run_id,
        manifest_uri=manifest_uri,
        dem_uri=dem_uri,
        run_args=run_args,
        build_spec=build_spec,
    )


# --------------------------------------------------------------------------- #
# Landlab LocalSolverSpec -- subprocess runner for the local-docker backend.
#
# exec_kind="exec": there is no public Landlab container image. The worker
# entrypoint (services/workers/landlab/entrypoint.py) runs as a subprocess
# in the current venv's Python via ``sys.executable -m
# services.workers.landlab.entrypoint``, with the repo root on
# PYTHONPATH so the worker's ``from services.workers.*`` imports resolve.
#
# The worker reads a file:// manifest URI (TRID3NT_OBJECT_STORE=file) from the
# local runs dir; no S3 / MinIO required for the offline build.
# --------------------------------------------------------------------------- #

#: Path to the Landlab subprocess CLI shim (mirrors run_inp.py for SWMM).
_LANDLAB_RUN_CHAIN = (
    Path(__file__).resolve().parents[4]
    / "services"
    / "workers"
    / "landlab"
    / "run_chain.py"
)

#: Repo root so the subprocess can resolve ``services.workers.*`` imports.
_TRID3NT_REPO_ROOT = str(Path(__file__).resolve().parents[4])


def landlab_local_spec() -> Any:
    """Build the Landlab ``LocalSolverSpec`` for the local-docker backend.

    Mirrors ``run_swmm.swmm_local_spec``: ``exec_kind="exec"`` (no public
    Landlab container image -- the worker is a pip-dep entrypoint), the
    manifest carries ``build_spec`` + a staged DEM path, and the spec runs
    the entrypoint as ``sys.executable -m services.workers.landlab.entrypoint
    --run-id <id> --manifest-uri <uri>`` in a subprocess.

    The repo root is prepended to PYTHONPATH so the worker's
    ``from services.workers.*`` imports resolve in the current venv (no
    editable install of the worker package is required).

    This spec is the SUBPROCESS RUNNER for the offline / local build. The
    cloud Batch path (``run_solver(solver='landlab', model_setup_uri=s3://...)``)
    is unchanged.
    """
    from ..tools.simulation.solver import LOCAL_EXEC_WORKFLOW_NAME, LocalSolverSpec

    repo_root = _TRID3NT_REPO_ROOT
    # Prepend the repo root to PYTHONPATH so the worker can import
    # ``services.workers.landlab.component_chain`` etc. directly.
    existing_pypath = os.environ.get("PYTHONPATH", "")
    new_pypath = f"{repo_root}:{existing_pypath}" if existing_pypath else repo_root

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        # Run the thin CLI shim (mirrors SWMM's run_inp.py pattern). The shim
        # reads manifest.json from rundir (written by launch_local_solver before
        # launching) and the DEM staged there; exits 0 on success.
        del args, run_id  # shim reads everything from manifest.json in CWD
        return [
            sys.executable,
            str(_LANDLAB_RUN_CHAIN),
            "--manifest",
            "manifest.json",
        ]

    def classify_exit(
        rundir: Path, exit_code: int
    ) -> "tuple[str, int, str | None, dict]":
        if exit_code != 0:
            return "error", exit_code, f"landlab worker exited with code {exit_code}", {}
        # Fold run_chain.py's typed result-block sidecar (landlab_result.json)
        # into completion.json's top-level "result" key -- mirrors the AWS
        # Batch entrypoint's completion.json shape exactly, so
        # model_landslide_scenario._download_batch_landlab_outputs gets the
        # worker's AUTHORITATIVE narration scalars (min_factor_of_safety in
        # particular is not recoverable from the probability field alone)
        # instead of silently falling back to a recomputed 0.0. A missing /
        # unparseable sidecar degrades honestly to the empty-extra fallback
        # (never a hard failure -- the field COG is the primary contract).
        result_path = rundir / "landlab_result.json"
        if result_path.exists():
            try:
                result_block = json.loads(result_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - honest degrade, not a hard failure
                logger.warning(
                    "landlab classify_exit: could not parse %s", result_path
                )
                return "ok", 0, None, {}
            return "ok", 0, None, {"result": result_block}
        return "ok", 0, None, {}

    return LocalSolverSpec(
        solver=LANDLAB_SOLVER_NAME,
        workflow_name=LOCAL_EXEC_WORKFLOW_NAME,
        args_key="build_spec",
        build_argv=build_argv,
        stdout_name="landlab.stdout",
        stderr_name="landlab.stderr",
        stdout_uri_field="landlab_stdout_uri",
        stderr_uri_field="landlab_stderr_uri",
        exec_kind="exec",
        classify_exit=classify_exit,
        env_overrides={"PYTHONPATH": new_pypath},
    )


def register_landlab_solver() -> None:
    """Register ``'landlab'`` local spec in ``tools.simulation.solver.LOCAL_SOLVER_SPEC_REGISTRY``.

    Mirrors ``run_swmm.register_swmm_solver``. Idempotent -- safe to call at
    module import. The factory is a zero-arg lambda wrapping ``landlab_local_spec``
    (deferred construction avoids any circular-import hazard at import time).
    """
    from ..tools.simulation.solver import LOCAL_SOLVER_SPEC_REGISTRY, register_local_solver_spec

    _ = LOCAL_SOLVER_SPEC_REGISTRY  # ensure the registry is initialised
    register_local_solver_spec(LANDLAB_SOLVER_NAME, landlab_local_spec)


# Register at import so ``run_solver(solver='landlab')`` with
# TRID3NT_SOLVER_BACKEND=local-docker dispatches to the subprocess spec.
register_landlab_solver()
