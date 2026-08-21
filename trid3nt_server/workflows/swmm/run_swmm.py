"""PySWMM quasi-2D urban-flood deck build + run orchestration.

One module owns the urban-flood engine's solve surface. SWMM runs HOST-EXEC:
pyswmm is a pip dep in the agent venv and SWMM5 is fully headless, so the engine
needs no container, no worker image, and no solver-dispatch substrate.

  1. **Deck build** (``build_and_stage_swmm_deck``). Calls the engine core's
     ``raster_cell_mesh.build_swmm_mesh`` (a DEM -> quasi-2D node/link SWMM
     ``.inp`` deck) and returns a ``SWMMStaging`` carrying the local ``.inp``
     path + the ``BuildResult`` provenance the run + postprocess paths read.

  2. **In-process solve** (``run_swmm_local``). Runs the built deck headless via
     pyswmm right where it was built. The mass-balance honesty gate (Flow
     Routing Continuity error) fires inside ``run_swmm_deck`` before this
     returns.

Determinism boundary (Invariant 1 / 2): no LLM call anywhere in this module.
The deck build is deterministic; the run is an in-process pyswmm solve. Every
number the agent narrates comes from the typed ``SWMMDepthLayerURI`` fields the
postprocess computed -- never free-generated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.swmm_contracts import SWMMRunArgs

from trid3nt_server.mesh.raster_cell_mesh import (
    BuildResult,
    RunResult,
    SWMMMeshError,
    build_swmm_mesh,
    run_swmm_deck,
)

logger = logging.getLogger("trid3nt_server.workflows.swmm.run_swmm")

__all__ = [
    "SWMMWorkflowError",
    "SWMMStaging",
    "build_and_stage_swmm_deck",
    "run_swmm_local",
    "SWMM_SOLVER_NAME",
]


#: The engine identifier carried on solve-progress + telemetry rows.
SWMM_SOLVER_NAME: str = "swmm"


# --------------------------------------------------------------------------- #
# Errors (mirrors MODFLOWWorkflowError / SWMMMeshError shape)
# --------------------------------------------------------------------------- #
class SWMMWorkflowError(RuntimeError):
    """Raised on any deck-build or in-process-solve failure.

    Carries an open-set A.6 ``error_code`` so the agent emitter renders a typed
    error frame (the emitter's ``_classify_exception`` reads ``error_code`` off
    the exception). Codes:

    - ``SWMM_PARAMS_INVALID`` -- the run args could not be coerced.
    - ``SWMM_DECK_BUILD_FAILED`` -- ``build_swmm_mesh`` raised (wraps the typed
      ``SWMMMeshError`` codes: SWMM_EMPTY_MESH / SWMM_DEM_UNREADABLE /
      SWMM_DEPENDENCY_MISSING).
    - ``SWMM_LOCAL_RUN_FAILED`` -- the in-process pyswmm solve raised (wraps
      SWMM_RUN_FAILED / SWMM_CONTINUITY_UNREADABLE).
    - ``SWMM_MASS_BALANCE_EXCEEDED`` -- the honesty gate: Flow Routing Continuity
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
# Build result -- the deck-build to solve handoff.
# --------------------------------------------------------------------------- #
@dataclass
class SWMMStaging:
    """The result of building a quasi-2D SWMM deck.

    Carries the on-disk ``.inp`` path + the full ``BuildResult`` provenance
    (grid_shape / crs / transform / resolution_m / barriers / dropped-building
    count) the run + postprocess paths read. Nothing is uploaded -- the deck is
    solved in-process where it was built, and the ``BuildResult`` IS the handoff.

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
# Deck build.
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
    through (return-period / storm depth handling is the COMPOSER's job -- by the
    time this is called ``run_args.total_rain_depth_mm`` is populated, or the
    builder's hyetograph default is used). The deck + its run outputs live in a
    scratch dir the caller cleans up after postprocess.

    Args:
        run_args: the validated ``SWMMRunArgs`` (forcing + structure params).
        dem_path: an on-disk DEM (GeoTIFF) path the mesh builder reads. The
            composer resolves ``fetch_3dep_extra`` / ``fetch_dem`` cache URIs to
            a local path before calling this; tests pass a synthetic GeoTIFF.
        building_footprints: optional GeoJSON FeatureCollection of building
            footprints (``fetch_buildings(source=osm)`` shape) -- drives the
            building obstruction mode AND the postprocess ``n_buildings_affected``
            count. ``None`` for a plain run.
        run_id: optional ULID; minted if absent.
        workdir: optional scratch base; a temp dir is used otherwise.
        enable_autoscale: when True (default) the mesh builder runs its adaptive
            budget and may COARSEN ``run_args.target_resolution_m`` to fit the
            cell cap. When False (the gate's ``narrow_scope`` path) the
            builder honours ``target_resolution_m`` EXACTLY -- the gate already
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
    from trid3nt_server.workflows.shared.physics_registry import (
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
        barriers=run_args.barriers,
        enable_autoscale=bool(enable_autoscale),
        # Universal emit-on-solve cadence lever (ADR 0282): None -> the legacy
        # 5-min REPORT_STEP (byte-identical deck).
        output_interval_min=getattr(run_args, "output_interval_min", None),
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
    # manning_overland is Optional on SWMMRunArgs (law 9, ADR 0285 P4): the COMPOSER
    # resolves it (NLCD-derived or user) or REFUSES before ever reaching here, so a
    # populated value is threaded through. A None here means a direct-call caller
    # (test / advanced) that skipped the composer's resolution -> defer to the mesh
    # builder's mechanical default (DEFAULT_OVERLAND_N); this primitive cannot fetch
    # NLCD (sync, no AOI fetch seam), so it never invents a friction for a USER run.
    if run_args.manning_overland is not None:
        build_kwargs["manning_overland"] = float(run_args.manning_overland)

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
# Solve -- pyswmm in-process.
# --------------------------------------------------------------------------- #
def run_swmm_local(staging: SWMMStaging) -> RunResult:
    """Run the built deck headless via pyswmm IN-PROCESS.

    pyswmm is in the agent venv and SWMM5 is fully headless, so the deck built
    by ``build_and_stage_swmm_deck`` is solved right here. Delegates to the engine core ``run_swmm_deck`` (which
    owns the mass-balance honesty gate: it raises ``SWMM_MASS_BALANCE_EXCEEDED``
    if the Flow Routing Continuity error exceeds the tolerance rather than
    publishing a silently-wrong layer).

    Returns:
        The ``swmm_mesh_builder.RunResult`` (``out_path`` + ``rpt_path`` +
        ``continuity_error_pct`` + ``peak_depth_grid`` + ``n_steps``) -- the
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
            "SWMM_SOLVE_TIMEOUT",
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
