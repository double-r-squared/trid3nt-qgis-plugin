"""Composer-side dispatch of the SFINCS QUADTREE build+solve to the worker image.

``model_flood_scenario(quadtree=True)`` cannot build a variable-resolution
coastal deck in-agent -- hydromt_sfincs has no quadtree authoring and cht_sfincs
is GPL, kept worker-isolated. So the composer stages the fetched topobathy DEM +
a ``sfincs_build_spec`` (bbox + design-storm forcing + the ``options.quadtree``
refinement knobs) to the cache bucket and dispatches the worker image in
build+solve mode (``--build-spec-uri``): cht_sfincs authors the coast-following
2:1-balanced grid, the SFINCS binary solves it, and the worker rasterizes the
face-indexed output + writes the native ``sfincs_map.nc``.

This is the local-docker analogue of the geoclaw/swan ``--network host`` self-S3
build+solve specs: the container reaches the local MinIO itself (no volume mount)
and writes outputs straight to ``s3://<runs>/<run_id>/``. The regular-grid path
is untouched -- it keeps the in-agent ``build_sfincs_model`` + ``run_solver``
(``solver="sfincs"``) seam.

The refinement criteria ride in as first-class knobs on ``options.quadtree``:
``{base_resolution_m, coast_refine_level, max_refine_level, coast_band_m}`` (the
user granularity lever); the design-storm surge water-level boundary rides in via
the deck builder's return-period fallback (``options.return_period_yr``).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The ``run_solver`` / ``SOLVER_WORKFLOW_REGISTRY`` identifier for the quadtree
#: build+solve dispatch. Distinct from ``"sfincs"`` (the regular-grid pre-built
#: deck volume-mount path) so the two never collide in the spec registry.
SFINCS_QUADTREE_SOLVER_NAME = "sfincs-quadtree"

#: The worker image carrying cht_sfincs + the build+solve entrypoint. The plain
#: ``deltares/sfincs-cpu`` image the regular local-docker path uses has neither,
#: so the quadtree dispatch pins its own (override via ``TRID3NT_SFINCS_BUILD_IMAGE``).
DEFAULT_SFINCS_BUILD_IMAGE = "trid3nt-local/sfincs:latest"


class QuadtreeDispatchError(RuntimeError):
    """Staging/compose failure for the quadtree build+solve dispatch.

    Carries an A.6-style ``error_code`` the composer folds into the same failed
    ``AssessmentEnvelope`` a regular-grid dispatch failure produces.
    """

    def __init__(self, error_code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.details = details


def compose_quadtree_build_spec(
    *,
    run_id: str,
    dem_uri: str,
    bbox: tuple[float, float, float, float],
    base_resolution_m: float,
    coast_refine_level: int,
    max_refine_level: int,
    coast_band_m: float | None,
    simulation_hours: float,
    output_interval_min: float | None,
    return_period_yr: int,
) -> dict[str, Any]:
    """Compose the worker ``sfincs_build_spec`` for a quadtree build+solve.

    ``forcing={"forcing_type": "waterlevel"}`` (no timeseries) drives the deck
    builder's return-period design-surge fallback -- the documented quadtree
    boundary path. The quadtree refinement knobs ride under ``options.quadtree``.
    """
    quadtree: dict[str, Any] = {
        "base_resolution_m": float(base_resolution_m),
        "coast_refine_level": int(coast_refine_level),
        "max_refine_level": int(max_refine_level),
    }
    if coast_band_m is not None:
        quadtree["coast_band_m"] = float(coast_band_m)
    return {
        "schema_version": 1,
        "engine": "sfincs",
        "run_id": run_id,
        "bbox": [float(v) for v in bbox],
        "nlcd_vintage_year": None,  # quadtree uses Manning constants, no landcover
        "inputs": {"dem_uri": dem_uri},
        "forcing": {"forcing_type": "waterlevel"},
        "options": {
            "simulation_hours": float(simulation_hours),
            "output_interval_min": output_interval_min,
            "return_period_yr": int(return_period_yr),
            "quadtree": quadtree,
        },
    }


def _ensure_dem_s3_uri(dem_uri: str, cache_bucket: str, staging_prefix: str) -> str:
    """Return an object-store URI the worker can localize.

    ``s3://`` / ``gs://`` pass through untouched. A local path (or ``file://``)
    is uploaded to the cache bucket so the ``--network host`` container reaches
    it over MinIO/S3.
    """
    from trid3nt_server.data.cache import storage_scheme
    from trid3nt_server.data.simulation.solver.solver import _get_s3_client

    if dem_uri.startswith(("s3://", "gs://")):
        return dem_uri
    local = dem_uri[len("file://"):] if dem_uri.startswith("file://") else dem_uri
    src = Path(local)
    if not src.is_file():
        raise QuadtreeDispatchError(
            "QUADTREE_DEM_MISSING",
            f"topobathy DEM not found for staging: {dem_uri!r}",
            dem_uri=dem_uri,
        )
    key = f"{staging_prefix}dem{src.suffix or '.tif'}"
    uri = f"{storage_scheme()}://{cache_bucket}/{key}"
    try:
        _get_s3_client().upload_file(str(src), cache_bucket, key)
    except Exception as exc:  # noqa: BLE001
        raise QuadtreeDispatchError(
            "QUADTREE_STAGING_FAILED",
            f"failed to stage topobathy DEM to {uri}: {exc}",
            dem_uri=dem_uri,
        ) from exc
    logger.info("quadtree dispatch: staged DEM %s -> %s", src, uri)
    return uri


def stage_quadtree_build_solve(
    *,
    staging_id: str,
    dem_uri: str,
    bbox: tuple[float, float, float, float],
    base_resolution_m: float,
    coast_refine_level: int,
    max_refine_level: int,
    coast_band_m: float | None,
    simulation_hours: float,
    output_interval_min: float | None,
    return_period_yr: int,
) -> str:
    """Stage the DEM + build_spec + run manifest to the cache bucket.

    Returns the ``model_setup_uri`` (the run manifest) to hand to
    ``run_solver(solver="sfincs-quadtree", model_setup_uri=<this>)``. SYNC boto3
    -- the composer calls it via ``asyncio.to_thread``. Raises
    ``QuadtreeDispatchError`` on any staging failure (the composer maps it into a
    typed failed envelope, never a silent regular-grid fallback).
    """
    from trid3nt_server.data.cache import CACHE_BUCKET, storage_scheme
    from trid3nt_server.data.simulation.solver.solver import _get_s3_client

    cache_bucket = os.environ.get("TRID3NT_CACHE_BUCKET") or CACHE_BUCKET
    prefix = f"cache/live-no-cache/sfincs_quadtree/{staging_id}/"

    staged_dem_uri = _ensure_dem_s3_uri(dem_uri, cache_bucket, prefix)
    spec = compose_quadtree_build_spec(
        run_id=staging_id,
        dem_uri=staged_dem_uri,
        bbox=bbox,
        base_resolution_m=base_resolution_m,
        coast_refine_level=coast_refine_level,
        max_refine_level=max_refine_level,
        coast_band_m=coast_band_m,
        simulation_hours=simulation_hours,
        output_interval_min=output_interval_min,
        return_period_yr=return_period_yr,
    )
    spec_key = f"{prefix}sfincs_build_spec.json"
    spec_uri = f"{storage_scheme()}://{cache_bucket}/{spec_key}"
    # The run manifest launch_local_solver reads: the container self-fetches from
    # S3 (--network host build-spec-uri), so inputs/outputs are empty (no rundir
    # staging or supervisor glob-upload -- the worker writes straight to S3).
    manifest = {
        "sfincs_quadtree_args": ["--run-id", staging_id, "--build-spec-uri", spec_uri],
        "inputs": [],
        "outputs": [],
    }
    manifest_key = f"{prefix}run_manifest.json"
    manifest_uri = f"{storage_scheme()}://{cache_bucket}/{manifest_key}"
    try:
        s3 = _get_s3_client()
        s3.put_object(
            Bucket=cache_bucket, Key=spec_key,
            Body=json.dumps(spec, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        s3.put_object(
            Bucket=cache_bucket, Key=manifest_key,
            Body=json.dumps(manifest, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        raise QuadtreeDispatchError(
            "QUADTREE_STAGING_FAILED",
            f"failed to stage quadtree build_spec/manifest to {spec_uri}: {exc}",
            spec_uri=spec_uri,
        ) from exc
    logger.info(
        "quadtree dispatch staged: build_spec=%s manifest=%s (base=%.0f m, "
        "coast_level=%d, band=%s m)",
        spec_uri, manifest_uri, base_resolution_m, coast_refine_level, coast_band_m,
    )
    return manifest_uri


def sfincs_quadtree_local_spec():
    """The ``sfincs-quadtree`` LocalSolverSpec (build+solve via --network host).

    Mirrors the geoclaw/swan self-S3 spec: the container reaches MinIO/S3
    directly (creds injected as env) and writes outputs to the runs bucket, so
    no rundir volume mount and no supervisor output glob. The ``--run-id`` in the
    staged args is rewritten to the launcher's run_id so the container's output
    prefix matches the prefix ``wait_for_completion`` polls.
    """
    from trid3nt_server.data.simulation.solver.solver import (
        LOCAL_DOCKER_WORKFLOW_NAME,
        LocalSolverSpec,
    )

    image = os.environ.get("TRID3NT_SFINCS_BUILD_IMAGE") or DEFAULT_SFINCS_BUILD_IMAGE
    aws_endpoint = os.environ.get("AWS_ENDPOINT_URL", "")
    aws_access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    aws_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    aws_region = os.environ.get("AWS_REGION", "us-east-1")
    runs_bucket = os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")
    cache_bucket = os.environ.get("TRID3NT_CACHE_BUCKET", "trid3nt-cache")

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        fixed_args = list(args)
        if "--run-id" in fixed_args:
            fixed_args[fixed_args.index("--run-id") + 1] = run_id
        else:
            fixed_args = ["--run-id", run_id, *fixed_args]
        cmd = ["docker", "run", "--rm", "--name", run_id, "--network", "host"]
        env_pairs = [
            ("TRID3NT_OBJECT_STORE", "s3"),
            ("TRID3NT_RUNS_BUCKET", runs_bucket),
            ("TRID3NT_CACHE_BUCKET", cache_bucket),
            ("AWS_REGION", aws_region),
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
        solver=SFINCS_QUADTREE_SOLVER_NAME,
        workflow_name=LOCAL_DOCKER_WORKFLOW_NAME,
        args_key="sfincs_quadtree_args",
        build_argv=build_argv,
        stdout_name="sfincs.stdout",
        stderr_name="sfincs.stderr",
        stdout_uri_field="sfincs_stdout_uri",
        stderr_uri_field="sfincs_stderr_uri",
        exec_kind="docker",
        classify_exit=None,
    )


def register_sfincs_quadtree_local_spec() -> None:
    """Register the ``sfincs-quadtree`` LocalSolverSpec factory (import-time)."""
    from trid3nt_server.data.simulation.solver.solver import (
        register_local_solver_spec,
    )

    register_local_solver_spec(SFINCS_QUADTREE_SOLVER_NAME, sfincs_quadtree_local_spec)


register_sfincs_quadtree_local_spec()
