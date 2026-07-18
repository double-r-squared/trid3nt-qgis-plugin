"""GeoClaw (Clawpack) deck-build + staging + Batch-dispatch orchestration
(sprint-17 — the GeoClaw analogue of ``run_swmm.py`` / ``run_modflow.py``).

One module owns the GeoClaw engine's solver-dispatch surface. Unlike SWMM (whose
pyswmm runs IN-PROCESS in the agent venv) GeoClaw is a Fortran solver that lives
ONLY in the worker container image (Clawpack compiles its Fortran at install) —
there is NO in-process agent lane. So GeoClaw is BATCH-PRIMARY: the agent stages
a ``build_spec`` (the typed run args) + a topo DEM to S3 and dispatches through
the SAME generic ``run_solver`` / ``wait_for_completion`` seam SFINCS uses, then
downloads the GeoClaw ``fort.q`` frames and postprocesses them.

  1. **build_spec assembly + staging** (``stage_geoclaw_manifest``). Builds the
     worker-contract manifest (``inputs[]`` = the topo DEM + optional dtopo/surge
     forcing; ``build_spec`` = the setrun_builder field dict; ``outputs`` = the
     fort.q globs) and uploads it + the DEM to the cache bucket, returning the
     ``manifest.json`` URI to feed ``run_solver(solver='geoclaw', ...)``.

  2. **GeoClaw solver registration** (``register_geoclaw_solver``). Adds
     ``'geoclaw'`` to ``SOLVER_WORKFLOW_REGISTRY`` (idempotent ``setdefault``,
     mirroring ``register_swmm_solver``) so ``run_solver(solver='geoclaw')``
     dispatches. The orchestrator ALSO pins the registry entry in code (the
     shared-append line this lane returns) so the dispatch works even when this
     module is not imported first.

Determinism boundary (Invariant 1 / 2): no LLM call anywhere in this module. The
deck is authored deterministically (in the worker, via setrun_builder); every
number the agent narrates comes from the typed ``GeoClawDepthLayerURI`` fields the
postprocess computed — never free-generated.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from grace2_contracts import new_ulid
from grace2_contracts.geoclaw_contracts import GeoClawRunArgs

logger = logging.getLogger("grace2_agent.workflows.run_geoclaw")

__all__ = [
    "GeoClawWorkflowError",
    "GeoClawStaging",
    "build_geoclaw_build_spec",
    "stage_geoclaw_manifest",
    "register_geoclaw_solver",
    "GEOCLAW_SOLVER_NAME",
]


#: The registry key + handle ``solver`` tag for the GeoClaw engine.
GEOCLAW_SOLVER_NAME: str = "geoclaw"

#: GeoClaw fort.q output globs the postprocess reads (the AMR ASCII frames +
#: their headers + the echoed deck manifest). Kept BYTE-IDENTICAL to the worker
#: entrypoint's output list so the agent + worker agree on the harvested set; the
#: fgmax monitor (fgmax{NNNN}.txt + fgmax_grids.data) + gauge time series
#: (gauge{NNNNN}.txt) ride along for the GAP1 fgmax reader.
GEOCLAW_OUTPUT_GLOBS: list[str] = [
    "_output/fort.q*",
    "_output/fort.t*",
    "_output/fort.h*",
    "_output/fort.b*",
    "_output/fgmax*.txt",
    "_output/fgmax_grids.data",
    "_output/gauge*.txt",
    "deck_manifest.json",
]


# --------------------------------------------------------------------------- #
# Errors (mirrors SWMMWorkflowError shape).
# --------------------------------------------------------------------------- #
class GeoClawWorkflowError(RuntimeError):
    """Raised on any deck-spec / staging / dispatch failure.

    Carries an open-set A.6 ``error_code`` so the agent emitter renders a typed
    error frame. Codes:

    - ``GEOCLAW_PARAMS_INVALID`` — the run args could not be coerced.
    - ``GEOCLAW_STAGING_FAILED`` — the build_spec / DEM upload failed.
    - ``GEOCLAW_RUN_FAILED`` — the Batch solve did not complete.
    - ``GEOCLAW_BATCH_OUTPUT_MISSING`` — a 'complete' solve produced no fort.q.
    """

    error_code: str = "GEOCLAW_WORKFLOW_FAILED"

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
# Staging result — the Batch-lane handoff (mirrors SWMMStaging).
# --------------------------------------------------------------------------- #
@dataclass
class GeoClawStaging:
    """The result of assembling + staging a GeoClaw build_spec + DEM.

    Fields:
        run_id: the run identifier the output COGs are keyed under.
        manifest_uri: the ``s3://`` URI of the staged ``manifest.json``.
        build_spec: the setrun_builder field dict that was staged.
        run_args: the validated ``GeoClawRunArgs`` (echoed for provenance).
        bbox: the AOI the postprocess rasterizes onto.
    """

    run_id: str
    manifest_uri: str
    build_spec: dict[str, Any]
    run_args: GeoClawRunArgs
    bbox: tuple[float, float, float, float]
    n_active_cells: int = 0
    resolution_m: float = 0.0
    staged_inputs: list[dict[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# build_spec assembly.
# --------------------------------------------------------------------------- #
def build_geoclaw_build_spec(
    run_args: GeoClawRunArgs,
    *,
    topo_dest: str = "topo.asc",
    dtopo_dest: str | None = None,
    surge_dest: str | None = None,
    extra_topo_files: list[str] | None = None,
    base_num_cells: tuple[int, int] = (40, 40),
) -> dict[str, Any]:
    """Assemble the setrun_builder ``build_spec`` dict from the validated run args.

    The single source of truth for the worker-side deck author's input. Maps the
    typed ``GeoClawRunArgs`` onto the flat build_spec the worker's
    ``setrun_builder.parse_build_spec`` consumes. The staged DEM is referenced by
    its in-deck destination filename (``topo_dest``); a staged dtopo / surge file
    is referenced by ``dtopo_dest`` / ``surge_dest`` when present.

    ``extra_topo_files`` are the staged-destination names of additional topo/bathy
    tiles (ordered coarse -> fine, appended AFTER the primary ``topo_dest`` so the
    worker layers them finest-last). ``fgmax_arrival_tol_m`` always rides along
    (it backs the fgmax wave-arrival monitor); ``coastal_gauge_lonlat`` and the
    four USER-GATED Okada ``fault_*`` keys are threaded ONLY when supplied (the
    engine substitutes scenario defaults otherwise and MUST surface that, never
    silently fabricate them).

    Pure dict assembly — unit-testable with no network.
    """
    spec: dict[str, Any] = {
        "scenario": run_args.scenario,
        "bbox": list(run_args.bbox),
        "topo_file": topo_dest,
        "sim_duration_s": float(run_args.sim_duration_s),
        "output_frames": int(run_args.output_frames),
        "amr_levels": int(run_args.amr_levels),
        "manning_n": float(run_args.manning_n),
        "sea_level_m": float(run_args.sea_level_m),
        "base_num_cells": [int(base_num_cells[0]), int(base_num_cells[1])],
        "source_magnitude": float(run_args.source_magnitude),
        "dam_break_depth_m": float(run_args.dam_break_depth_m),
        "fgmax_arrival_tol_m": float(run_args.fgmax_arrival_tol_m),
    }
    if run_args.source_lonlat is not None:
        spec["source_lonlat"] = [
            float(run_args.source_lonlat[0]),
            float(run_args.source_lonlat[1]),
        ]
    if extra_topo_files:
        spec["extra_topo_files"] = list(extra_topo_files)
    if run_args.coastal_gauge_lonlat is not None:
        spec["coastal_gauge_lonlat"] = [
            float(run_args.coastal_gauge_lonlat[0]),
            float(run_args.coastal_gauge_lonlat[1]),
        ]
    # USER-GATED Okada fault overrides: thread ONLY the ones the user supplied.
    if run_args.fault_strike_deg is not None:
        spec["fault_strike_deg"] = float(run_args.fault_strike_deg)
    if run_args.fault_dip_deg is not None:
        spec["fault_dip_deg"] = float(run_args.fault_dip_deg)
    if run_args.fault_rake_deg is not None:
        spec["fault_rake_deg"] = float(run_args.fault_rake_deg)
    if run_args.fault_depth_km is not None:
        spec["fault_depth_km"] = float(run_args.fault_depth_km)
    if run_args.scenario == "tsunami" and dtopo_dest is not None:
        spec["dtopo_file"] = dtopo_dest
    if run_args.scenario == "surge" and surge_dest is not None:
        spec["surge_forcing_file"] = surge_dest
    return spec


# --------------------------------------------------------------------------- #
# Staging — upload the build_spec manifest + the topo DEM to S3.
# --------------------------------------------------------------------------- #
def stage_geoclaw_manifest(
    run_args: GeoClawRunArgs,
    *,
    dem_uri: str,
    run_id: str | None = None,
    dtopo_uri: str | None = None,
    surge_uri: str | None = None,
    extra_dem_uris: list[str] | None = None,
    base_num_cells: tuple[int, int] = (40, 40),
) -> GeoClawStaging:
    """Stage the GeoClaw ``manifest.json`` (build_spec + input refs) to S3.

    The GeoClaw analogue of ``stage_swmm_manifest``. Mirrors that path EXACTLY
    (no new client): the same ``cache.storage_scheme()`` scheme + the same
    ``tools.solver._get_s3_client()`` boto3 client + the same
    ``GRACE2_CACHE_BUCKET`` staging bucket the SWMM/SFINCS decks upload to.

    The worker downloads the topo DEM (and optional dtopo / surge) listed in
    ``inputs[]`` BY SCHEME and authors the deck from ``build_spec``. ``dem_uri``
    is a cache/runs ``s3://`` URI produced by ``fetch_topobathy`` / ``fetch_dem``
    upstream (it is staged BY REFERENCE — the worker downloads it directly — so we
    do not re-upload the DEM bytes here, only point at them).

    Args:
        run_args: the validated ``GeoClawRunArgs``.
        dem_uri: the ``s3://`` URI of the topo/bathy DEM (ESRI-ASCII topotype-3
            preferred; the worker references it as ``topo.asc``).
        run_id: optional ULID; minted if absent.
        dtopo_uri: optional ``s3://`` URI of a staged dtopo (tsunami scenario).
        surge_uri: optional ``s3://`` URI of a staged surge hydrograph CSV.
        extra_dem_uris: optional ordered (coarse -> fine) list of additional
            topo/bathy DEM ``s3://`` URIs; each is staged BY REFERENCE as
            ``topo_extra_{i}.asc`` and threaded into the build_spec after the
            primary topo so the worker layers them finest-last.
        base_num_cells: the GeoClaw base computational-grid resolution.

    Returns:
        ``GeoClawStaging`` carrying the manifest URI + the build_spec + bbox.

    Raises:
        GeoClawWorkflowError("GEOCLAW_STAGING_FAILED"): the upload could not
            complete (the Batch lane cannot dispatch without a reachable
            manifest — fail loudly, never a silent dead-end).
    """
    from ..tools.cache import storage_scheme
    from ..tools.solver import _get_s3_client

    rid = run_id or new_ulid()
    bbox = tuple(run_args.bbox)

    # Stage the DEM BY REFERENCE; the worker downloads it as topo.asc.
    inputs: list[dict[str, str]] = [{"gs_uri": dem_uri, "dest": "topo.asc"}]
    dtopo_dest: str | None = None
    surge_dest: str | None = None
    # Additional topo/bathy tiles (ordered coarse -> fine) staged BY REFERENCE.
    extra_topo_files: list[str] = []
    for i, uri in enumerate(extra_dem_uris or []):
        if not uri:
            continue
        dest = f"topo_extra_{i}.asc"
        inputs.append({"gs_uri": str(uri), "dest": dest})
        extra_topo_files.append(dest)
    if run_args.scenario == "tsunami" and dtopo_uri:
        dtopo_dest = "dtopo.tt3"
        inputs.append({"gs_uri": dtopo_uri, "dest": dtopo_dest})
    if run_args.scenario == "surge" and surge_uri:
        surge_dest = "surge.csv"
        inputs.append({"gs_uri": surge_uri, "dest": surge_dest})

    build_spec = build_geoclaw_build_spec(
        run_args,
        topo_dest="topo.asc",
        dtopo_dest=dtopo_dest,
        surge_dest=surge_dest,
        extra_topo_files=extra_topo_files,
        base_num_cells=base_num_cells,
    )

    manifest_dict: dict[str, Any] = {
        "inputs": inputs,
        "build_spec": build_spec,
        "outputs": list(GEOCLAW_OUTPUT_GLOBS),
    }

    scheme = storage_scheme()  # "s3" on AWS (GCP decommissioned)
    cache_bucket = os.environ.get("GRACE2_CACHE_BUCKET", "grace-2-hazard-prod-cache")
    prefix = f"cache/static-30d/geoclaw_setup/{rid}/"
    manifest_key = f"{prefix}manifest.json"
    manifest_uri = f"{scheme}://{cache_bucket}/{manifest_key}"

    try:
        s3 = _get_s3_client()
        s3.put_object(
            Bucket=cache_bucket,
            Key=manifest_key,
            Body=json.dumps(manifest_dict, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        raise GeoClawWorkflowError(
            "GEOCLAW_STAGING_FAILED",
            message=f"failed to stage GeoClaw manifest to {manifest_uri}: {exc}",
            details={"run_id": rid, "manifest_uri": manifest_uri},
        ) from exc

    logger.info(
        "stage_geoclaw_manifest run_id=%s scenario=%s dem=%s -> manifest=%s",
        rid,
        run_args.scenario,
        dem_uri,
        manifest_uri,
    )
    # n_active_cells used only for telemetry + compute-class sizing; the base grid
    # cell count is a coarse proxy (AMR refines it dynamically downstream).
    n_active = int(base_num_cells[0]) * int(base_num_cells[1])
    return GeoClawStaging(
        run_id=rid,
        manifest_uri=manifest_uri,
        build_spec=build_spec,
        run_args=run_args,
        bbox=bbox,  # type: ignore[arg-type]
        n_active_cells=n_active,
        staged_inputs=inputs,
    )


# --------------------------------------------------------------------------- #
# GeoClaw solver registration (mirrors register_swmm_solver).
# --------------------------------------------------------------------------- #
def register_geoclaw_solver() -> None:
    """Register ``'geoclaw'`` in ``tools.solver.SOLVER_WORKFLOW_REGISTRY``.

    Mirrors ``register_swmm_solver``. GeoClaw is Batch-only (the Fortran lives in
    the worker image, never in the agent venv), so it maps to the AWS-Batch
    workflow-name sentinel. ``run_solver`` only requires the KEY to be present to
    dispatch (the backend seam routes to ``_run_solver_aws_batch``, and the
    per-solver job-def is resolved from ``GRACE2_AWS_BATCH_JOB_DEF_GEOCLAW``).
    Idempotent ``setdefault`` — safe to call at import. The orchestrator ALSO
    pins this in code via the shared-append line so dispatch works regardless of
    import order.
    """
    from ..tools.solver import AWS_BATCH_WORKFLOW_NAME, SOLVER_WORKFLOW_REGISTRY

    SOLVER_WORKFLOW_REGISTRY.setdefault(GEOCLAW_SOLVER_NAME, AWS_BATCH_WORKFLOW_NAME)


# Register at import so ``run_solver(solver='geoclaw')`` is wired wherever this
# module is imported (the composer + the tool wrapper both import it).
register_geoclaw_solver()
