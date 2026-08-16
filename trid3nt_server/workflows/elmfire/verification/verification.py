"""Engine template ``elmfire_verification_elliptical_replication`` - the ELMFIRE
constant-wind flat-terrain elliptical-spread VERIFICATION (calibration anchor).

A distinct question CLASS from ``elmfire_fire_spread`` (per the capability-naming
rule): not "where does a real fire spread over LANDFIRE fuels", but "does the
level-set solver reproduce the closed-form elliptical solution on a controlled
constant-fuel/uniform-wind/flat-terrain deck". It is its OWN registered engine
TEMPLATE (engine="elmfire", tier="template").

``elmfire_verification_elliptical_replication(...)`` authors an ALL-CONSTANT deck
agent-side (GR2 uniform grass fuel, flat terrain, uniform wind -- no LANDFIRE/DEM
fetch), runs the SAME container solver, reads the time-of-arrival raster, extracts
the numerical perimeter, and compares it to the Richards (1990) ellipse implied by
the observed head/back/flank rates -- returning the verification triple (RMSE /
fractional error / correlation) + the ellipse-overlay chart.

Determinism boundary (Invariant 1): every narrated number (rmse_m / err_fraction /
correlation / length_to_width_ratio + the fire-spread scalars) comes from the typed
``ElmfireEllipseVerificationLayerURI`` fields the postprocess/verifier computed --
never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import math
import tempfile
from typing import Any

from trid3nt_contracts.elmfire_contracts import (
    ELMFIRE_TOA_STYLE_PRESET,
    ElmfireEllipseVerificationLayerURI,
    ElmfireRunArgs,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool
from trid3nt_server.workflows.elmfire._template_card import TemplateCard
from trid3nt_server.workflows.elmfire.fire_spread.fire_spread import (
    FireSpreadComposerError,
    _cleanup_dir,
    _download_elmfire_outputs,
    _publish_primary_layer,
)
from trid3nt_server.workflows.elmfire.postprocess_elmfire import (
    PostprocessElmfireError,
    build_ellipse_overlay_chart_spec,
    discover_elmfire_rasters,
    postprocess_elmfire,
    read_fire_raster,
    verify_elliptical_replication,
)
from trid3nt_server.workflows.elmfire.run_elmfire import (
    ELMFIRE_SOLVER_NAME,
    VERIFICATION_FUEL_MODEL_GR2,
    ElmfireWorkflowError,
    build_constant_verification_deck,
    stage_elmfire_manifest,
)
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.elmfire.verification.verification"
)

__all__ = [
    "elmfire_verification_elliptical_replication",
    "model_elmfire_elliptical_verification",
]

#: The verification deck is placed at a neutral mid-CONUS point (the geography is
#: immaterial on a constant-fuel flat grid; only the grid + EPSG:5070 projection
#: matter). Kansas centroid.
_VERIFICATION_CENTER_LON: float = -98.5
_VERIFICATION_CENTER_LAT: float = 38.5


#: Curated door-listing card (the run_elmfire door prefers this over signature
#: derivation). One-line question + the real inputs + a knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "ELMFIRE verification: does the numerical fire perimeter under constant "
        "fuel + uniform wind + flat terrain match the closed-form elliptical "
        "solution (Richards ellipse) within tolerance -- the calibration anchor"
    ),
    required_inputs=[],
    knobs=(
        "wind_speed_mph, wind_dir_deg, duration_hours, cellsize_m, domain_km, "
        "fuel_model"
    ),
)


_METADATA = AtomicToolMetadata(
    name="elmfire_verification_elliptical_replication",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="elmfire",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def elmfire_verification_elliptical_replication(
    wind_speed_mph: float = 15.0,
    wind_dir_deg: float = 270.0,
    duration_hours: float = 1.5,
    cellsize_m: float = 30.0,
    domain_km: float = 10.0,
    fuel_model: int = VERIFICATION_FUEL_MODEL_GR2,
    fuel_moisture: str = "dry",
    compute_class: str = "standard",
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders). Also absorbs the
    # server confirm gate's injected ``confirmed=True``.
    **_extra_ignored: Any,
) -> ElmfireEllipseVerificationLayerURI | dict[str, Any]:
    """Verify ELMFIRE's numerical fire perimeter against the closed-form elliptical solution.

    Fidelity: a CALIBRATION/VERIFICATION run on a controlled ALL-CONSTANT deck
    (single GR2 grass fuel model, flat terrain, uniform constant wind) -- NOT a
    real-landscape fire forecast. Under these conditions Rothermel's spread rate
    is constant and the point-ignition perimeter is a closed-form ellipse
    (Richards 1990); this checks the level-set solver reproduces it.
    Data: NO LANDFIRE/DEM fetch -- the deck is authored agent-side as constants.
    The tolerance is the COARSE-grid shape-agreement tolerance, not the published
    fine-grid <0.5% Verification-01 gate.
    Off-scope: real wildfire spread over LANDFIRE fuels -> elmfire_fire_spread.

    Use this when: the user wants to VERIFY / VALIDATE the fire-spread solver, run
    the elliptical-spread regression/calibration check, or confirm the numerical
    perimeter matches the analytical ellipse under constant wind / flat terrain.

    Params:
        wind_speed_mph: constant wind speed (ELMFIRE 20 ft convention, default 15).
        wind_dir_deg: direction wind blows FROM, meteorological deg (default 270,
            i.e. the fire heads east).
        duration_hours: burn duration (default 1.5; kept short so the ellipse stays
            inside the domain).
        cellsize_m: computational cell size (default 30).
        domain_km: square domain side length, km (default 10).
        fuel_model: the uniform FBFM fuel-model code (default 102 = GR2 grass).
        fuel_moisture: "dry" (default), "moderate", or "moist".
        compute_class: compute class (default "standard").

    Returns:
        On success: ``ElmfireEllipseVerificationLayerURI`` -- the time-of-arrival
        COG plus the verification triple (``rmse_m``, ``err_fraction``,
        ``correlation``, ``corr_class``, ``length_to_width_ratio``, ``passed``).
        An ellipse-overlay chart is emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    # Build a square domain of domain_km at a neutral mid-CONUS point; ignition at
    # the centre. Geography is immaterial on a constant-fuel flat grid.
    half_deg_lat = (float(domain_km) * 1000.0 / 2.0) / 111_320.0
    half_deg_lon = half_deg_lat / max(
        math.cos(math.radians(_VERIFICATION_CENTER_LAT)), 1e-6
    )
    bbox = (
        _VERIFICATION_CENTER_LON - half_deg_lon,
        _VERIFICATION_CENTER_LAT - half_deg_lat,
        _VERIFICATION_CENTER_LON + half_deg_lon,
        _VERIFICATION_CENTER_LAT + half_deg_lat,
    )
    ignition = (_VERIFICATION_CENTER_LON, _VERIFICATION_CENTER_LAT)

    try:
        run_args = ElmfireRunArgs(
            bbox=bbox,  # type: ignore[arg-type]
            ignition_lonlat=ignition,  # type: ignore[arg-type]
            wind_speed_mph=float(wind_speed_mph),
            wind_dir_deg=float(wind_dir_deg),
            fuel_moisture=fuel_moisture,  # type: ignore[arg-type]
            duration_hours=float(duration_hours),
            cellsize_m=float(cellsize_m),
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError / coercion
        return {
            "status": "error",
            "error_code": "FIRE_PARAMS_INVALID",
            "error_message": f"invalid verification run arguments: {exc}",
        }

    logger.info(
        "elmfire_verification_elliptical_replication fuel=%d wind=%.1fmph@%.0fdeg "
        "duration=%.2fh domain=%.1fkm cell=%.0fm",
        fuel_model,
        run_args.wind_speed_mph,
        run_args.wind_dir_deg,
        run_args.duration_hours,
        domain_km,
        run_args.cellsize_m,
    )

    try:
        primary = await model_elmfire_elliptical_verification(
            run_args, fuel_model=int(fuel_model), compute_class=compute_class
        )
        logger.info(
            "elmfire_verification complete layer_id=%s err_fraction=%.4f corr=%.4f "
            "class=%s passed=%s uri=%s",
            primary.layer_id,
            primary.err_fraction,
            primary.correlation,
            primary.corr_class,
            primary.passed,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        ElmfireWorkflowError,
        PostprocessElmfireError,
        FireSpreadComposerError,
    ) as exc:
        logger.warning(
            "elmfire_verification failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "FIRE_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("elmfire_verification unexpected failure")
        return {
            "status": "error",
            "error_code": "FIRE_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# The composer (deterministic, no LLM in the chain -- Invariant 2):
#   build constant deck -> stage -> run_solver('elmfire') -> download outputs
#     -> read ToA raster -> verify against the Richards ellipse
#     -> postprocess ToA COG -> publish -> emit the ellipse-overlay chart.
# --------------------------------------------------------------------------- #
async def model_elmfire_elliptical_verification(
    run_args: ElmfireRunArgs,
    *,
    fuel_model: int = VERIFICATION_FUEL_MODEL_GR2,
    run_id: str | None = None,
    compute_class: str = "standard",
    cleanup_outputs: bool = True,
) -> ElmfireEllipseVerificationLayerURI:
    """Compose the ELMFIRE elliptical-verification chain end-to-end (constant deck)."""
    from trid3nt_server.data.simulation.solver.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    bbox = tuple(run_args.bbox)
    emitter = current_emitter()
    duration_s = float(run_args.duration_hours) * 3600.0

    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
            logger.warning("model_elmfire_elliptical_verification: zoom-to failed: %s", exc)

    begin_substeps(emitter, 4)

    deck_dir = tempfile.mkdtemp(prefix="elmfire-verify-deck-")
    try:
        # --- Step 1: author the ALL-CONSTANT flat-grid deck (no fetch). ------
        async with substep(emitter, "build_elmfire_deck"):
            deck_manifest = await asyncio.to_thread(
                build_constant_verification_deck, run_args, deck_dir, fuel_model=fuel_model
            )
        grid = deck_manifest.get("grid") or {}

        # --- Step 2: stage the run_solver manifest. --------------------------
        async with substep(emitter, "stage_elmfire_manifest"):
            staging = await asyncio.to_thread(
                stage_elmfire_manifest, deck_dir, deck_manifest, run_args, run_id=run_id
            )

        # --- Step 3: dispatch via the generic run_solver seam. ---------------
        handle = run_solver(
            solver=ELMFIRE_SOLVER_NAME,
            model_setup_uri=staging.manifest_uri,
            compute_class=compute_class,
        )
        _sim_step_id = await mint_dispatch_and_sim_cards(
            emitter=emitter,
            solver=ELMFIRE_SOLVER_NAME,
            handle=handle,
            compute_class=compute_class,
        )
        if emitter is not None and _sim_step_id is not None:
            set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))
        run_result = None
        try:
            async with substep(emitter, "run_solver"):
                run_result = await wait_for_completion(handle)
        except asyncio.CancelledError:
            await route_sim_terminal(emitter, _sim_step_id, run_result=None)
            raise
        finally:
            set_emitter_binding(None)
        await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

        if run_result.status != "complete":
            raise ElmfireWorkflowError(
                "ELMFIRE_RUN_FAILED",
                "ELMFIRE verification solve did not complete "
                f"(status={run_result.status}, "
                f"error_code={getattr(run_result, 'error_code', None)}): "
                f"{getattr(run_result, 'error_message', '') or ''}",
                details={"run_id": staging.run_id},
            )

        # --- Step 4: download + read ToA + verify + postprocess. -------------
        solve_run_id = getattr(run_result, "run_id", None) or staging.run_id
        out_dir, out_is_temp = await asyncio.to_thread(
            _download_elmfire_outputs, solve_run_id
        )
        epsg = int(grid.get("epsg", 5070))
        try:
            rasters = discover_elmfire_rasters(out_dir)
            toa_path = rasters.get("time_of_arrival")
            if toa_path is None:
                raise FireSpreadComposerError(
                    "ELMFIRE_NO_LAYERS",
                    "verification solve produced no time_of_arrival raster",
                )
            toa_s, transform, _crs, cellsize_m = await asyncio.to_thread(
                read_fire_raster, toa_path, epsg=epsg
            )
            ign_xy = (deck_manifest.get("ignitions_domain_xy") or [{}])[0]
            ign_x = float(ign_xy.get("x", 0.0))
            ign_y = float(ign_xy.get("y", 0.0))
            # (col, row) = ~transform * (x, y)
            inv = ~transform
            ign_col, ign_row = inv * (ign_x, ign_y)
            verification, overlay = verify_elliptical_replication(
                toa_s,
                cellsize_m=float(cellsize_m),
                ignition_rowcol=(int(round(ign_row)), int(round(ign_col))),
                wind_from_deg=float(run_args.wind_dir_deg),
            )

            async with substep(emitter, "postprocess_elmfire"):
                layers, _metrics = await asyncio.to_thread(
                    postprocess_elmfire,
                    out_dir,
                    bbox,
                    run_id=solve_run_id,
                    duration_s=duration_s,
                    epsg=epsg,
                    ignition_lonlat=tuple(run_args.ignition_lonlat),
                )
        finally:
            if cleanup_outputs and out_is_temp:
                _cleanup_dir(out_dir)
    finally:
        if cleanup_outputs:
            _cleanup_dir(deck_dir)

    if not layers:
        raise FireSpreadComposerError(
            "ELMFIRE_NO_LAYERS",
            "postprocess_elmfire produced no ToA layer (verification honesty floor)",
        )

    raw_toa = layers[0]
    published = await asyncio.to_thread(_publish_primary_layer, raw_toa, staging.run_id)

    # Build the verification result layer from the published ToA + the triple.
    primary = ElmfireEllipseVerificationLayerURI(
        layer_id=published.layer_id,
        name="Fire arrival time (elliptical verification)",
        layer_type=published.layer_type,
        uri=published.uri,
        style_preset=published.style_preset or ELMFIRE_TOA_STYLE_PRESET,
        role=published.role,
        bbox=published.bbox,
        burned_area_km2=published.burned_area_km2,
        fire_arrival_max_hr=published.fire_arrival_max_hr,
        max_flame_length_m=published.max_flame_length_m,
        max_spread_rate_m_min=published.max_spread_rate_m_min,
        duration_hours=published.duration_hours,
        ignition_lonlat=published.ignition_lonlat,
        rmse_m=float(verification.get("rmse_m", 0.0)),
        err_fraction=float(verification.get("err_fraction", 0.0)),
        correlation=float(verification.get("correlation", 0.0)),
        corr_class=str(verification.get("corr_class", "poor")),
        length_to_width_ratio=float(verification.get("length_to_width_ratio", 0.0)),
        tolerance=float(verification.get("tolerance", 0.0)),
        passed=bool(verification.get("passed", False)),
    )

    await _maybe_emit_ellipse_chart(emitter, overlay, primary.uri)

    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal
            logger.warning(
                "model_elmfire_elliptical_verification: authoritative zoom-to failed: %s",
                exc,
            )

    logger.info(
        "model_elmfire_elliptical_verification complete run_id=%s err_fraction=%.4f "
        "corr=%.4f class=%s lw=%.3f passed=%s uri=%s",
        staging.run_id,
        primary.err_fraction,
        primary.correlation,
        primary.corr_class,
        primary.length_to_width_ratio,
        primary.passed,
        primary.uri,
    )
    return primary


async def _maybe_emit_ellipse_chart(
    emitter: Any, overlay_points: list[dict[str, float]], source_uri: str
) -> None:
    """Emit the ellipse-overlay chart (numerical perimeter vs Richards ellipse)."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    spec = build_ellipse_overlay_chart_spec(overlay_points)
    if spec is None:
        return
    from trid3nt_server.data.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Elliptical verification (numerical perimeter vs Richards ellipse)",
        caption=(
            "The numerical ELMFIRE fire perimeter vs the closed-form Richards "
            "ellipse implied by its own head/flank/back rates, in the wind-aligned "
            "frame -- how closely the level-set solver reproduces the analytical "
            "elliptical solution."
        ),
        source_layer_uri=source_uri,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001 - non-fatal
        logger.warning("ellipse-overlay chart emit failed: %s", exc)
