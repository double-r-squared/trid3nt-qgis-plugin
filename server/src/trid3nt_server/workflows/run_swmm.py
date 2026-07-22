"""PySWMM quasi-2D urban-flood deck build + run orchestration (sprint-16 P4,
Path A — the LOCAL lane, the end-to-end wiring).

The SWMM analogue of ``run_modflow.py`` (the MODFLOW deck-build + submit +
local-exec quintet) and ``tools/solver.py``'s ``run_solver`` dispatch path. One
module owns the urban-flood engine's solver-dispatch surface:

  1. **Deck build + staging** (``build_and_stage_swmm_deck``). Calls the engine
     core's ``swmm_mesh_builder.build_swmm_mesh`` (a DEM -> quasi-2D node/link
     SWMM ``.inp`` deck; FROZEN, landed b5013cf) and returns a ``SWMMStaging``
     carrying the local ``.inp`` path + the ``BuildResult`` provenance the
     run + postprocess paths read. No cloud staging is required for the dev
     primary path — the deck is solved in-process where it was built.

  2. **LOCAL EXECUTION (``run_swmm_local``)** — the DEV PRIMARY PATH. Runs the
     built deck headless via pyswmm IN-PROCESS (``swmm_mesh_builder.run_swmm_deck``)
     — no container, no Cloud Workflows, no Batch. This is BOTH the dev/test
     seam AND the live-evidence path: pyswmm 2.1.0 + swmm-api 0.4.73 are in the
     agent venv and SWMM5 is fully headless, so the urban engine needs no
     external solver substrate to produce a real solved ``.out``. The
     mass-balance honesty gate (Flow Routing Continuity error) fires inside
     ``run_swmm_deck`` before this returns.

  3. **A SWMM ``LocalSolverSpec``** (``swmm_local_spec``) + ``'swmm'`` registered
     in ``SOLVER_WORKFLOW_REGISTRY``. This mirrors the MODFLOW ``exec_kind="exec"``
     local spec so ``run_solver(solver='swmm', ...)`` is wired for the
     staged-manifest local-backend path (``TRID3NT_SOLVER_BACKEND=local-docker``)
     when an out-of-process worker lane is ever needed. The spec runs ``pyswmm``
     against a staged ``.inp`` via a tiny CLI shim
     (``services/workers/swmm/run_inp.py``): ``exec_kind="exec"`` (there is no
     public SWMM image; pyswmm is a pip dep, not a container), the completion
     manifest carries ``swmm_stdout_uri`` / ``swmm_stderr_uri`` + a ``continuity``
     post-exit classifier. The dev primary path (2) does NOT touch this — it
     runs pyswmm directly on the in-memory ``BuildResult`` — but the spec keeps
     the SWMM engine's dispatch surface symmetric with SFINCS/MODFLOW.

Determinism boundary (Invariant 1 / 2): no LLM call anywhere in this module.
The deck build is deterministic ``swmm_mesh_builder``; the run is an in-process
pyswmm solve; the local-exec path is a subprocess run of the same builder. Every
number the agent narrates comes from the typed ``SWMMDepthLayerURI`` fields the
postprocess computed — never free-generated.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.swmm_contracts import SWMMRunArgs

from .swmm_mesh_builder import (
    BuildResult,
    RunResult,
    SWMMMeshError,
    build_swmm_mesh,
    read_flow_routing_continuity,
    run_swmm_deck,
)

logger = logging.getLogger("trid3nt_server.workflows.run_swmm")

__all__ = [
    "SWMMWorkflowError",
    "SWMMStaging",
    "build_and_stage_swmm_deck",
    "stage_swmm_manifest",
    "run_swmm_local",
    "is_local_mode",
    "swmm_local_spec",
    "register_swmm_solver",
    "SWMM_SOLVER_NAME",
]


#: The registry key + handle ``solver`` tag for the urban-flood engine.
SWMM_SOLVER_NAME: str = "swmm"


# --------------------------------------------------------------------------- #
# Errors (mirrors MODFLOWWorkflowError / SWMMMeshError shape)
# --------------------------------------------------------------------------- #
class SWMMWorkflowError(RuntimeError):
    """Raised on any deck-build / staging / local-run failure in the LOCAL lane.

    Carries an open-set A.6 ``error_code`` so the agent emitter renders a typed
    error frame (the emitter's ``_classify_exception`` reads ``error_code`` off
    the exception). Codes:

    - ``SWMM_PARAMS_INVALID`` — the run args could not be coerced.
    - ``SWMM_DECK_BUILD_FAILED`` — ``build_swmm_mesh`` raised (wraps the typed
      ``SWMMMeshError`` codes: SWMM_EMPTY_MESH / SWMM_DEM_UNREADABLE /
      SWMM_DEPENDENCY_MISSING).
    - ``SWMM_LOCAL_RUN_FAILED`` — the in-process pyswmm solve raised (wraps
      SWMM_RUN_FAILED / SWMM_CONTINUITY_UNREADABLE).
    - ``SWMM_MASS_BALANCE_EXCEEDED`` — the honesty gate: Flow Routing Continuity
      error exceeded the tolerance (re-raised verbatim from ``run_swmm_deck``).
    """

    error_code: str = "SWMM_WORKFLOW_FAILED"

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
# Staging result — the local-lane handoff (mirrors DeckStaging).
# --------------------------------------------------------------------------- #
@dataclass
class SWMMStaging:
    """The result of building (+ optionally staging) a quasi-2D SWMM deck.

    Carries the on-disk ``.inp`` path + the full ``BuildResult`` provenance
    (grid_shape / crs / transform / resolution_m / barriers / dropped-building
    count) the run + postprocess paths read. For the dev primary path nothing
    is uploaded — the deck is solved in-process where it was built, and the
    ``BuildResult`` IS the local-lane handoff (analogue of ``DeckStaging``).

    Fields:
        run_id: the run identifier the output COGs are keyed under.
        inp_path: the on-disk SWMM ``.inp`` deck path (``run_swmm_deck`` reads
            this; the ``.out`` / ``.rpt`` land alongside it).
        build: the ``swmm_mesh_builder.BuildResult`` (the scatter + georegistration
            provenance the postprocess needs).
        run_args: the validated ``SWMMRunArgs`` (echoed for provenance).
        building_footprints: the GeoJSON FeatureCollection of footprints (echoed
            so postprocess can count ``n_buildings_affected`` honestly).
    """

    run_id: str
    inp_path: str
    build: BuildResult
    run_args: SWMMRunArgs
    building_footprints: Any = None
    # WQ (sprint-WQ): the resolved (name, unit) pollutants authored on the deck
    # (echoed from BuildResult.pollutants; empty on a hydraulics-only run) so the
    # composer knows whether to run the WQ postprocess without re-parsing.
    pollutants: list[tuple[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Local-mode flag (mirrors run_modflow.is_local_mode).
# --------------------------------------------------------------------------- #
def is_local_mode() -> bool:
    """True when the SWMM engine should run IN-PROCESS via pyswmm (the default).

    The urban engine is headless-pyswmm by nature, so unlike SFINCS/MODFLOW the
    LOCAL lane is the DEFAULT (``TRID3NT_SWMM_LOCAL`` unset -> local). Set
    ``TRID3NT_SWMM_LOCAL=0`` to force the out-of-process staged-manifest dispatch
    (``run_solver(solver='swmm')`` via the local-exec spec) instead — used only
    when a sandboxed worker lane is wired. The default in-process path needs no
    external substrate.
    """
    raw = os.environ.get("TRID3NT_SWMM_LOCAL")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# Deck build + staging.
# --------------------------------------------------------------------------- #
def build_and_stage_swmm_deck(
    run_args: SWMMRunArgs,
    *,
    dem_path: str,
    building_footprints: Any = None,
    run_id: str | None = None,
    workdir: str | Path | None = None,
    enable_autoscale: bool = True,
) -> SWMMStaging:
    """Build a quasi-2D SWMM ``.inp`` deck from a DEM + the run args.

    The SWMM analogue of ``build_and_stage_modflow_deck``. Calls the engine
    core ``build_swmm_mesh`` (FROZEN) with every ``SWMMRunArgs`` field threaded
    through (return-period / storm depth handling is the COMPOSER's job — by the
    time this is called ``run_args.total_rain_depth_mm`` is populated, or the
    builder's hyetograph default is used). The deck + its run outputs live in a
    scratch dir the caller cleans up after postprocess.

    Args:
        run_args: the validated ``SWMMRunArgs`` (forcing + structure params).
        dem_path: an on-disk DEM (GeoTIFF) path the mesh builder reads. The
            composer resolves ``fetch_3dep_extra`` / ``fetch_dem`` cache URIs to
            a local path before calling this; tests pass a synthetic GeoTIFF.
        building_footprints: optional GeoJSON FeatureCollection of building
            footprints (``fetch_buildings(source=osm)`` shape) — drives the
            building obstruction mode AND the postprocess ``n_buildings_affected``
            count. ``None`` for a plain run.
        run_id: optional ULID; minted if absent.
        workdir: optional scratch base; a temp dir is used otherwise.
        enable_autoscale: when True (default) the mesh builder runs its adaptive
            budget and may COARSEN ``run_args.target_resolution_m`` to fit the
            cell cap. When False (the #154 gate's ``narrow_scope`` path) the
            builder honours ``target_resolution_m`` EXACTLY — the gate already
            clamped it under the cap, so the user's chosen rung is final.

    Returns:
        ``SWMMStaging`` carrying the ``.inp`` path + ``BuildResult`` + echoed
        run args / footprints.

    Raises:
        SWMMWorkflowError("SWMM_DECK_BUILD_FAILED"): the mesh build raised.
    """
    import tempfile

    rid = run_id or new_ulid()
    base = (
        Path(workdir)
        if workdir is not None
        else Path(tempfile.mkdtemp(prefix=f"swmm-{rid}-"))
    )
    base.mkdir(parents=True, exist_ok=True)
    inp_path = str(base / "mesh.inp")

    # The builder owns the hyetograph build (P1 nested), the adaptive-mesh
    # budget, the building obstruction modes, the SCS-CN / Green-Ampt
    # infiltration, the barrier snapping (red wall / green flap), and the single
    # boundary outfall. Thread every SWMMRunArgs field through.
    # levers STEP 3: validate + resolve advanced_physics (OPTIONS overrides).
    # None => {} (byte-identical DYNWAVE deck). A bad key/value raises a typed
    # SWMM_PHYSICS_INVALID (honest correction, never a silently-wrong deck).
    from .physics_registry import (
        PhysicsRegistryError,
        applied_physics_delta,
        validate_and_resolve_physics,
    )

    try:
        resolved_physics = validate_and_resolve_physics(
            "swmm", getattr(run_args, "advanced_physics", None)
        )
    except PhysicsRegistryError as exc:
        raise SWMMWorkflowError(
            "SWMM_PHYSICS_INVALID",
            message=f"invalid advanced_physics: {exc}",
            details={"run_id": rid, "engine": "swmm", "key": getattr(exc, "key", None)},
        ) from exc
    if resolved_physics:
        logger.info(
            "run_swmm advanced_physics applied run_id=%s delta=%s",
            rid,
            applied_physics_delta("swmm", resolved_physics),
        )

    # WQ (sprint-WQ): resolve the pollutant KEYWORDS -> demo PollutantSpec presets
    # HERE (composer's job), so the builder stays a pure deck author. An advanced
    # caller may pass fully-specified ``pollutant_specs`` to override the presets.
    # None/[] => no WQ sections => a byte-identical hydraulics-only deck.
    from trid3nt_contracts.swmm_contracts import resolve_pollutant_presets

    pollutant_specs = list(getattr(run_args, "pollutant_specs", None) or []) or (
        resolve_pollutant_presets(getattr(run_args, "pollutants", None))
    )

    total_depth = run_args.total_rain_depth_mm
    build_kwargs: dict[str, Any] = dict(
        dem_path=dem_path,
        out_inp_path=inp_path,
        storm_duration_hr=float(run_args.storm_duration_hr),
        rain_interval_min=int(run_args.rain_interval_min),
        target_resolution_m=float(run_args.target_resolution_m),
        building_footprints=building_footprints,
        building_representation=run_args.building_representation,
        infiltration_method=run_args.infiltration_method,
        manning_overland=float(run_args.manning_overland),
        barriers=run_args.barriers,
        enable_autoscale=bool(enable_autoscale),
        advanced_physics=resolved_physics or None,
        pollutants=pollutant_specs or None,
        dry_buildup_days=int(getattr(run_args, "dry_buildup_days", 0) or 0),
        washoff_model=str(getattr(run_args, "washoff_model", "exp") or "exp"),
    )
    # total_rain_depth_mm is optional on SWMMRunArgs (the Atlas-14 lookup may
    # not have populated it); the builder has a sane default, so only override
    # when supplied.
    if total_depth is not None:
        build_kwargs["total_rain_depth_mm"] = float(total_depth)

    try:
        build = build_swmm_mesh(**build_kwargs)
    except SWMMMeshError as exc:
        raise SWMMWorkflowError(
            exc.error_code if exc.error_code in {"SWMM_EMPTY_MESH", "SWMM_DEM_UNREADABLE", "SWMM_DEPENDENCY_MISSING"} else "SWMM_DECK_BUILD_FAILED",
            message=f"build_swmm_mesh failed: {exc}",
            details={"run_id": rid, **getattr(exc, "details", {})},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise SWMMWorkflowError(
            "SWMM_DECK_BUILD_FAILED",
            message=f"build_swmm_mesh raised: {exc}",
            details={"run_id": rid, "dem_path": dem_path},
        ) from exc

    logger.info(
        "build_and_stage_swmm_deck run_id=%s inp=%s active_cells=%d res=%.1fm "
        "buildings_dropped=%d walls=%d flap_gates=%d",
        rid,
        build.inp_path,
        build.n_active_cells,
        build.resolution_m,
        build.n_buildings_dropped,
        build.n_walls,
        build.n_flap_gates,
    )
    return SWMMStaging(
        run_id=rid,
        inp_path=build.inp_path,
        build=build,
        run_args=run_args,
        building_footprints=building_footprints,
        pollutants=list(getattr(build, "pollutants", []) or []),
    )


# --------------------------------------------------------------------------- #
# Out-of-process (Batch / local-exec) staging — upload the .inp + a manifest.
# --------------------------------------------------------------------------- #
def stage_swmm_manifest(staging: SWMMStaging) -> str:
    """Upload the built ``.inp`` + a worker-contract ``manifest.json`` to S3.

    The out-of-process analogue of ``build_and_stage_modflow_deck``'s staging
    half / the SFINCS ``build_sfincs_model`` manifest write. Mirrors that path
    EXACTLY (no new client): uses the same ``cache.storage_scheme()`` scheme +
    the same ``tools.simulation.solver._get_s3_client()`` boto3 client + the same
    ``TRID3NT_CACHE_BUCKET`` staging bucket the SFINCS deck uploads land in.

    Writes:

      - ``.../mesh.inp``     — the built deck (``staging.inp_path``).
      - ``.../manifest.json`` — the manifest the SWMM worker entrypoint reads
        (``services/workers/swmm/entrypoint.py``): ``inputs[]`` carry the legacy
        ``gs_uri`` field NAME with an ``s3://`` VALUE (resolved by scheme in the
        worker), ``dest='mesh.inp'``; ``swmm_args=['mesh.inp']``; ``outputs``
        glob the ``.out`` + ``.rpt`` the postprocess reads.

    Args:
        staging: the ``SWMMStaging`` from ``build_and_stage_swmm_deck`` (the
            ``.inp`` on disk + the ``run_id`` the staged objects are keyed under).

    Returns:
        The ``s3://`` URI of the uploaded ``manifest.json`` — feed it STRAIGHT to
        ``run_solver(solver='swmm', model_setup_uri=<this>, ...)``.

    Raises:
        SWMMWorkflowError("SWMM_STAGING_FAILED"): the upload could not complete
            (the out-of-process lane cannot dispatch without a reachable
            manifest — fail loudly, never a silent dead-end).
    """
    from ..tools.cache import CACHE_BUCKET, storage_scheme
    from ..tools.simulation.solver import _get_s3_client

    scheme = storage_scheme()  # "s3" on AWS (GCP decommissioned)
    cache_bucket = os.environ.get("TRID3NT_CACHE_BUCKET") or CACHE_BUCKET
    # Per-run prefix under the cache bucket's staged-deck source class (mirrors
    # the SFINCS sfincs_setup/ prefix). The run_id keys the staged objects.
    prefix = f"cache/static-30d/swmm_setup/{staging.run_id}/"
    inp_key = f"{prefix}mesh.inp"
    manifest_key = f"{prefix}manifest.json"
    inp_uri = f"{scheme}://{cache_bucket}/{inp_key}"
    manifest_uri = f"{scheme}://{cache_bucket}/{manifest_key}"

    # The worker downloads inputs[] BY SCHEME (the field name is the legacy
    # ``gs_uri``; the VALUE is the s3:// URI). outputs[] glob the .out the
    # postprocess reads + the .rpt for continuity provenance + *.tif for worker
    # postprocess COGs. postprocess_spec carries the georegistration provenance
    # the worker-side SWMM postprocess (run_swmm_postprocess) needs to scatter
    # node depths onto the grid and reproject to EPSG:4326 COG.
    _bbox = list(staging.run_args.bbox)
    manifest_dict: dict[str, Any] = {
        "inputs": [{"gs_uri": inp_uri, "dest": "mesh.inp"}],
        "swmm_args": ["mesh.inp"],
        "outputs": ["*.out", "*.rpt", "*.tif"],
        "postprocess_spec": {
            "grid_shape": list(staging.build.grid_shape),
            "resolution_m": staging.build.resolution_m,
            "crs": staging.build.crs,
            "transform": list(staging.build.transform),
            "bbox": _bbox,
        },
    }

    import json

    try:
        s3 = _get_s3_client()
        # Upload the .inp deck.
        with open(staging.inp_path, "rb") as fh:
            s3.put_object(Bucket=cache_bucket, Key=inp_key, Body=fh)
        # Upload the manifest.
        s3.put_object(
            Bucket=cache_bucket,
            Key=manifest_key,
            Body=json.dumps(manifest_dict, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:  # noqa: BLE001
        raise SWMMWorkflowError(
            "SWMM_STAGING_FAILED",
            message=f"failed to stage SWMM deck/manifest to {manifest_uri}: {exc}",
            details={"run_id": staging.run_id, "manifest_uri": manifest_uri},
        ) from exc

    logger.info(
        "stage_swmm_manifest run_id=%s inp=%s -> manifest=%s",
        staging.run_id,
        inp_uri,
        manifest_uri,
    )
    return manifest_uri


# --------------------------------------------------------------------------- #
# LOCAL EXECUTION — the dev primary path (pyswmm in-process).
# --------------------------------------------------------------------------- #
def run_swmm_local(staging: SWMMStaging) -> RunResult:
    """Run the staged deck headless via pyswmm IN-PROCESS (the dev primary path).

    No container, no Cloud Workflows, no Batch — pyswmm is in the agent venv and
    SWMM5 is fully headless, so the deck built by ``build_and_stage_swmm_deck``
    is solved right here. Delegates to the engine core ``run_swmm_deck`` (which
    owns the mass-balance honesty gate: it raises ``SWMM_MASS_BALANCE_EXCEEDED``
    if the Flow Routing Continuity error exceeds the tolerance rather than
    publishing a silently-wrong layer).

    Returns:
        The ``swmm_mesh_builder.RunResult`` (``out_path`` + ``rpt_path`` +
        ``continuity_error_pct`` + ``peak_depth_grid`` + ``n_steps``) — the
        postprocess reads ``run.out_path`` for the per-timestep node depths.

    Raises:
        SWMMWorkflowError: wraps the typed ``SWMMMeshError`` codes; the
            mass-balance gate's ``SWMM_MASS_BALANCE_EXCEEDED`` is re-raised
            verbatim so the agent narrates the honesty failure.
    """
    tol = float(staging.run_args.mass_balance_tolerance_pct)
    logger.info(
        "run_swmm_local run_id=%s inp=%s tolerance=%.1f%%",
        staging.run_id,
        staging.inp_path,
        tol,
    )
    try:
        run = run_swmm_deck(staging.build, mass_balance_tolerance_pct=tol)
    except SWMMMeshError as exc:
        # Re-raise the typed code (SWMM_MASS_BALANCE_EXCEEDED / SWMM_RUN_FAILED /
        # SWMM_CONTINUITY_UNREADABLE / SWMM_DEPENDENCY_MISSING) so the agent
        # surface renders the honest failure rather than a generic crash.
        code = exc.error_code if exc.error_code in {
            "SWMM_MASS_BALANCE_EXCEEDED",
            "SWMM_CONTINUITY_UNREADABLE",
            "SWMM_DEPENDENCY_MISSING",
        } else "SWMM_LOCAL_RUN_FAILED"
        raise SWMMWorkflowError(
            code,
            message=str(exc),
            details={"run_id": staging.run_id, **getattr(exc, "details", {})},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise SWMMWorkflowError(
            "SWMM_LOCAL_RUN_FAILED",
            message=f"pyswmm in-process solve failed: {exc}",
            details={"run_id": staging.run_id, "inp_path": staging.inp_path},
        ) from exc

    logger.info(
        "run_swmm_local complete run_id=%s out=%s continuity=%+.3f%% "
        "n_steps=%d max_depth_m=%.4g n_wet=%d wall=%.1fs",
        staging.run_id,
        run.out_path,
        run.continuity_error_pct,
        run.n_steps,
        run.max_depth_m,
        run.n_wet_cells,
        run.wall_seconds,
    )
    return run


# --------------------------------------------------------------------------- #
# SWMM LocalSolverSpec — the out-of-process staged-manifest lane (symmetry with
# SFINCS/MODFLOW). ``exec_kind="exec"`` (pyswmm is a pip dep, no public image);
# the worker shim runs pyswmm against the staged .inp.
# --------------------------------------------------------------------------- #

#: The worker CLI shim that runs pyswmm against a staged ``.inp`` (mirrors the
#: SFINCS/MODFLOW worker entrypoints). Resolved relative to the repo so the
#: local-exec spec can invoke ``python run_inp.py mesh.inp`` in the rundir.
_SWMM_WORKER_RUN_INP = (
    Path(__file__).resolve().parents[4]
    / "services"
    / "workers"
    / "swmm"
    / "run_inp.py"
)

#: Repo root for PYTHONPATH injection (subprocess must resolve services.workers.*).
_SWMM_TRID3NT_REPO_ROOT = str(Path(__file__).resolve().parents[4])


def swmm_local_spec() -> Any:
    """Build the SWMM ``LocalSolverSpec`` for the shared local backend.

    Mirrors ``run_modflow._modflow_local_spec``: ``exec_kind="exec"`` (there is
    no public SWMM container image — pyswmm is a pip dep, so we run the
    in-repo CLI shim ``services/workers/swmm/run_inp.py`` with the venv python),
    a ``swmm_args`` manifest key carrying the ``.inp`` filename, ``swmm.stdout`` /
    ``swmm.stderr`` artifacts, ``swmm_stdout_uri`` / ``swmm_stderr_uri``
    completion fields, and a ``classify_exit`` that reads the Flow Routing
    Continuity error from the ``.rpt`` (the mass-balance honesty gate, mirroring
    MODFLOW's mfsim.lst convergence guard).

    This is the symmetry path — the DEV PRIMARY path (``run_swmm_local``) runs
    pyswmm in-process and never touches this. It exists so ``'swmm'`` is a
    first-class entry in ``SOLVER_WORKFLOW_REGISTRY`` and ``run_solver(
    solver='swmm', model_setup_uri=...)`` dispatches correctly under
    ``TRID3NT_SOLVER_BACKEND=local-docker`` if a sandboxed worker lane is wired.
    """
    import sys

    from ..tools.simulation.solver import LOCAL_EXEC_WORKFLOW_NAME, LocalSolverSpec

    def build_argv(run_id: str, rundir: Path, args: list[str]) -> list[str]:
        # python services/workers/swmm/run_inp.py <inp_filename...>. The .inp is
        # staged into the rundir by launch_local_solver, so the args carry the
        # bare filename(s) resolved against CWD (== rundir).
        return [sys.executable, str(_SWMM_WORKER_RUN_INP), *args]

    def classify_exit(
        rundir: Path, exit_code: int
    ) -> tuple[str, int, str | None, dict[str, Any]]:
        # Mass-balance honesty gate (mirrors MODFLOW's convergence guard). The
        # worker writes mesh.rpt alongside the .inp; read its Flow Routing
        # Continuity error. A tolerance of 5% matches the SWMMRunArgs default.
        tol = 5.0
        try:
            tol = float(os.environ.get("TRID3NT_SWMM_MASS_BALANCE_TOL_PCT", "5.0"))
        except (TypeError, ValueError):
            tol = 5.0
        rpt = next(iter(sorted(rundir.glob("*.rpt"))), None)
        continuity = read_flow_routing_continuity(str(rpt)) if rpt else None
        extra: dict[str, Any] = {"continuity_error_pct": continuity}
        if exit_code != 0:
            return "error", exit_code, f"swmm worker exited with code {exit_code}", extra
        if continuity is None:
            return (
                "error",
                2,
                "no Flow Routing Continuity error in the .rpt (run did not complete)",
                extra,
            )
        if abs(continuity) > tol:
            return (
                "error",
                3,
                f"Flow Routing Continuity error {continuity:+.3f}% exceeds "
                f"tolerance {tol:.1f}% (SWMM_MASS_BALANCE_EXCEEDED)",
                extra,
            )
        return "ok", 0, None, extra

    # Prepend the repo root to PYTHONPATH so the shim can import
    # ``services.workers.swmm.*`` when the agent is installed in an isolated venv.
    existing_pypath = os.environ.get("PYTHONPATH", "")
    new_pypath = (
        f"{_SWMM_TRID3NT_REPO_ROOT}:{existing_pypath}"
        if existing_pypath
        else _SWMM_TRID3NT_REPO_ROOT
    )

    return LocalSolverSpec(
        solver=SWMM_SOLVER_NAME,
        workflow_name=LOCAL_EXEC_WORKFLOW_NAME,
        args_key="swmm_args",
        build_argv=build_argv,
        stdout_name="swmm.stdout",
        stderr_name="swmm.stderr",
        stdout_uri_field="swmm_stdout_uri",
        stderr_uri_field="swmm_stderr_uri",
        exec_kind="exec",
        classify_exit=classify_exit,
        env_overrides={"PYTHONPATH": new_pypath},
    )


def register_swmm_solver() -> None:
    """Register ``'swmm'`` in SOLVER_WORKFLOW_REGISTRY and LOCAL_SOLVER_SPEC_REGISTRY.

    Mirrors the SFINCS registration. The workflow registry maps the solver name
    to its workflow/dispatch name; for the local-exec lane that name is the
    ``LOCAL_EXEC_WORKFLOW_NAME`` sentinel (``run_solver`` only requires the key
    to be PRESENT to dispatch). The spec registry maps the solver name to its
    ``LocalSolverSpec`` factory so ``_run_solver_local_docker`` dispatches to the
    correct shim instead of the default SFINCS spec. Idempotent -- safe to call
    at import.
    """
    from ..tools.simulation.solver import (
        LOCAL_EXEC_WORKFLOW_NAME,
        SOLVER_WORKFLOW_REGISTRY,
        register_local_solver_spec,
    )

    SOLVER_WORKFLOW_REGISTRY.setdefault(SWMM_SOLVER_NAME, LOCAL_EXEC_WORKFLOW_NAME)
    register_local_solver_spec(SWMM_SOLVER_NAME, swmm_local_spec)


# Register at import so ``run_solver(solver='swmm')`` is wired wherever this
# module is imported (the composer + the tool wrapper both import it).
register_swmm_solver()
