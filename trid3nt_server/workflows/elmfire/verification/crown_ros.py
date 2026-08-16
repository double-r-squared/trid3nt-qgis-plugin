"""Engine template ``elmfire_crown_fire_active_ros_verification`` - the ELMFIRE
ACTIVE crown-fire rate-of-spread VERIFICATION against the Cruz et al. (2005)
closed form (the crown-fire calibration anchor / exact-solution regression gate).

A distinct question CLASS from the crown-fire INITIATION sweep
(``elmfire_crown_fire_initiation_threshold_sweep``) and from surface-fire
elliptical verification: not "where does the surface-to-crown boundary sit" nor
"does the flat-fuel perimeter match the Richards ellipse", but "does the numerical
level-set HEAD spread rate on an UNCAPPED, fully-active crown deck reproduce the
Cruz (2005) active crown-fire rate of spread within tolerance". It is its OWN
registered engine TEMPLATE (engine="elmfire", tier="template"), the twin of
``elmfire_verification_elliptical_replication`` for the crown-fire regime.

Cruz, M.G., Alexander, M.E., Wagner, R.H. (2005), Can. J. For. Res.
35:1626-1639, active crown-fire ROS (see ``cruz_crown_fire.py`` for the exact
equation + the ELMFIRE elmfire_spread_rate.f90 implementation cite):

    R_active = 11.02 * U10^0.90 * CBD^0.19 * exp(-0.17 * EFFM)      [m/min]

The deck is authored ALL-CONSTANT agent-side (SH7 shrub fuel, a uniform canopy,
flat terrain, uniform wind -- no LANDFIRE/DEM fetch) with the crown model on, the
Cruz rate ceiling LIFTED (so the closed-form rate carries the front, never the
MIN() cap) and the critical canopy cover set below the deck cover (so the burn is
active-crown, CROWN_FIRE=2). The composer runs the SAME container solver, reads
the time-of-arrival raster, measures the numerical HEAD spread rate (head extent /
duration), evaluates the Cruz closed form at the deck's own inputs, and returns
the verification triple (numerical ROS / Cruz ROS / relative error) gated to a
stated coarse-grid tolerance.

Determinism boundary (Invariant 1): every narrated number
(numerical_ros_m_min / cruz_ros_m_min / rel_error / passed) comes from the typed
``ElmfireCrownRosVerificationLayerURI`` fields the composer measured/computed --
never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from trid3nt_contracts.elmfire_contracts import (
    ELMFIRE_TOA_STYLE_PRESET,
    ElmfireCrownRosVerificationLayerURI,
    ElmfireRunArgs,
    FUEL_MOISTURE_PRESETS,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.data import register_tool
from trid3nt_server.workflows.elmfire._template_card import TemplateCard
from trid3nt_server.workflows.elmfire.cruz_crown_fire import (
    cruz_active_crown_ros_m_min,
)
from trid3nt_server.workflows.elmfire.crown.crown_fire import (
    _CROWN_CANOPY,
    _CROWN_FUEL_MODEL_SH7,
    _CROWN_BANDTHICKNESS,
    _CROWN_SIMULATION_DT_S,
    _CROWN_TARGET_CFL,
    _UNCAPPED_RATE_FTMIN,
)
from trid3nt_server.workflows.elmfire.fire_spread.fire_spread import (
    FireSpreadComposerError,
)
from trid3nt_server.workflows.elmfire.postprocess_elmfire import (
    PostprocessElmfireError,
    discover_elmfire_rasters,
    read_fire_raster,
    verify_elliptical_replication,
)
from trid3nt_server.workflows.elmfire.run_elmfire import (
    ElmfireWorkflowError,
)
from trid3nt_server.workflows.elmfire.sensitivity._sensitivity_common import (
    cleanup_cases,
    publish_primary_from_out_dir,
    solve_constant_case,
)
from trid3nt_server.emission.pipeline_emitter import begin_substeps, current_emitter

logger = logging.getLogger(
    "trid3nt_server.workflows.elmfire.verification.crown_ros"
)

__all__ = [
    "elmfire_crown_fire_active_ros_verification",
    "model_elmfire_crown_ros_verification",
]

#: Neutral mid-CONUS deck centre (geography immaterial on a constant flat deck).
_CENTER_LON: float = -98.5
_CENTER_LAT: float = 38.5

#: Critical canopy cover set BELOW the deck's 0.60 canopy cover so the burn
#: registers as ACTIVE crown (CROWN_FIRE=2) and the head spreads at the Cruz rate.
_ACTIVE_CRITICAL_CANOPY_COVER: float = 0.20

#: The pass tolerance on the relative ROS error. The numerical head ROS is
#: head-extent / duration off a point ignition on the level-set grid; a 5 % band
#: brackets the discretization + point-ignition-transient error while still
#: catching a broken crown-fire ROS (a wrong coefficient / wind-height conversion
#: blows well past it). Live in-image V&V (2026-08-14, trid3nt-local elmfire
#: image, 20 mph / cbd 0.18 / EFFM 3 % / 30 m / 0.4 h): numerical 123.75 vs Cruz
#: 123.16 m/min -> rel. error 0.48 %, comfortably inside the 5 % gate.
_CROWN_ROS_TOLERANCE: float = 0.05


TEMPLATE_CARD = TemplateCard(
    question=(
        "ELMFIRE crown-fire verification: does the numerical active-crown HEAD "
        "spread rate on an uncapped canopied deck match the Cruz (2005) "
        "closed-form active crown-fire rate of spread within tolerance -- the "
        "crown-fire calibration anchor / exact-solution regression"
    ),
    required_inputs=[],
    knobs=(
        "wind_speed_mph, duration_hours, cellsize_m, domain_km, fuel_model"
    ),
)


_METADATA = AtomicToolMetadata(
    name="elmfire_crown_fire_active_ros_verification",
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
async def elmfire_crown_fire_active_ros_verification(
    wind_speed_mph: float = 20.0,
    duration_hours: float = 0.4,
    cellsize_m: float = 30.0,
    domain_km: float = 12.0,
    fuel_model: int = _CROWN_FUEL_MODEL_SH7,
    fuel_moisture: str = "dry",
    compute_class: str = "small",
    # absorb LLM-invented kwargs + the server confirm gate's injected confirmed=True.
    **_extra_ignored: Any,
) -> ElmfireCrownRosVerificationLayerURI | dict[str, Any]:
    """Verify ELMFIRE's active-crown HEAD spread rate against the Cruz (2005) closed form.

    Fidelity: a CALIBRATION/VERIFICATION run on a controlled ALL-CONSTANT flat
    CANOPIED deck (single SH7 shrub fuel, a uniform canopy -- cc=60%, ch=37.5 m,
    cbh=1.0 m, cbd=0.18 kg/m3, flat terrain, uniform wind) with the crown model
    on, the Cruz rate ceiling LIFTED (uncapped), and the critical canopy cover set
    below the deck cover so the burn is fully ACTIVE crown. Under these conditions
    the numerical level-set head fire spreads at the Cruz (2005) active crown-fire
    rate of spread; this checks the solver reproduces that closed form.
    Data: NO LANDFIRE/DEM fetch -- the deck (incl. the canopy) is authored as
    constants. NOT a real-landscape crown-fire forecast.
    Off-scope: where crown fire initiates -> elmfire_crown_fire_initiation_
    threshold_sweep; real wildfire spread over LANDFIRE fuels -> elmfire_fire_spread.

    Use this when: the user wants to VERIFY / VALIDATE the CROWN-fire spread rate,
    run the Cruz (2005) crown-fire regression / exact-solution check, or confirm
    the numerical active-crown head rate matches the analytical crown ROS.

    Params:
        wind_speed_mph: constant 20-ft wind speed (default 20; drives the Cruz
            rate -- kept modest so the active-crown head stays inside the domain).
        duration_hours: burn duration (default 0.4; short so the head does not
            reach the domain edge, which would truncate the measured rate).
        cellsize_m: computational cell size (default 30).
        domain_km: square domain side length, km (default 12).
        fuel_model: the uniform FBFM fuel-model code (default 147 = SH7 shrub).
        fuel_moisture: "dry" (default), "moderate", or "moist".
        compute_class: compute class (default "small").

    Returns:
        On success: ``ElmfireCrownRosVerificationLayerURI`` -- the time-of-arrival
        COG plus ``numerical_ros_m_min`` / ``cruz_ros_m_min`` / ``rel_error`` /
        ``tolerance`` / ``passed`` and the echoed closed-form inputs.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    try:
        primary = await model_elmfire_crown_ros_verification(
            wind_speed_mph=float(wind_speed_mph),
            duration_hours=float(duration_hours),
            cellsize_m=float(cellsize_m),
            domain_km=float(domain_km),
            fuel_model=int(fuel_model),
            fuel_moisture=str(fuel_moisture),
            compute_class=compute_class,
        )
        logger.info(
            "elmfire_crown_fire_active_ros_verification complete layer_id=%s "
            "numerical=%.2f cruz=%.2f rel_err=%.4f passed=%s uri=%s",
            primary.layer_id,
            primary.numerical_ros_m_min,
            primary.cruz_ros_m_min,
            primary.rel_error,
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
            "elmfire_crown_fire_active_ros_verification failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "FIRE_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("elmfire_crown_fire_active_ros_verification unexpected failure")
        return {
            "status": "error",
            "error_code": "FIRE_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_elmfire_crown_ros_verification(
    *,
    wind_speed_mph: float,
    duration_hours: float,
    cellsize_m: float,
    domain_km: float,
    fuel_model: int = _CROWN_FUEL_MODEL_SH7,
    fuel_moisture: str = "dry",
    compute_class: str = "small",
) -> ElmfireCrownRosVerificationLayerURI:
    """Compose the crown-ROS verification end-to-end (uncapped active-crown deck).

    build uncapped active-crown constant deck -> solve (container) -> read ToA ->
    measure numerical HEAD spread rate (head extent / duration) -> evaluate the
    Cruz (2005) closed form at the deck's own inputs -> gate the relative error.
    """
    emitter = current_emitter()
    duration_s = float(duration_hours) * 3600.0
    duration_min = float(duration_hours) * 60.0

    half_deg_lat = (float(domain_km) * 1000.0 / 2.0) / 111_320.0
    half_deg_lon = half_deg_lat / max(math.cos(math.radians(_CENTER_LAT)), 1e-6)
    bbox = (
        _CENTER_LON - half_deg_lon, _CENTER_LAT - half_deg_lat,
        _CENTER_LON + half_deg_lon, _CENTER_LAT + half_deg_lat,
    )
    ignition = (_CENTER_LON, _CENTER_LAT)

    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 -- non-fatal UX hint
            logger.warning("crown_ros verification zoom-to failed: %s", exc)

    begin_substeps(emitter, 2)

    run_args = ElmfireRunArgs(
        bbox=bbox,  # type: ignore[arg-type]
        ignition_lonlat=ignition,  # type: ignore[arg-type]
        wind_speed_mph=float(wind_speed_mph),
        wind_dir_deg=270.0,
        fuel_moisture=fuel_moisture,  # type: ignore[arg-type]
        duration_hours=float(duration_hours),
        cellsize_m=float(cellsize_m),
    )

    # UNCAPPED active-crown config: crown model on, Cruz rate ceiling lifted so
    # the closed-form rate (not the MIN() cap) carries the front, critical canopy
    # cover below the deck cover so the burn is ACTIVE (CROWN_FIRE=2).
    sim_extra = {
        "CROWN_FIRE_MODEL": "1",
        "BANDTHICKNESS": str(_CROWN_BANDTHICKNESS),
        "CROWN_FIRE_SPREAD_RATE_LIMIT": f"{_UNCAPPED_RATE_FTMIN:.1f}",
        "CRITICAL_CANOPY_COVER": f"{_ACTIVE_CRITICAL_CANOPY_COVER:.4f}",
    }

    cases = []
    try:
        case = await solve_constant_case(
            run_args,
            knob_value=float(wind_speed_mph),
            fuel_model=int(fuel_model),
            canopy=dict(_CROWN_CANOPY),
            target_cfl=_CROWN_TARGET_CFL,
            simulator_extra=sim_extra,
            outputs_extra={"DUMP_CROWN_FIRE": ".TRUE."},
            dt_s=_CROWN_SIMULATION_DT_S,
            compute_class=compute_class,
            emitter=emitter,
            measure_crown=True,
        )
        cases.append(case)

        # Head-spread measurement: reuse the elliptical perimeter extractor to get
        # the along-wind HEAD extent (m); numerical head ROS = head extent /
        # duration. The verifier also flags an edge-touching perimeter (which
        # truncates the head extent -> the verification is invalid).
        toa_s, _transform, _crs, cell_m = await asyncio.to_thread(
            read_fire_raster,
            discover_elmfire_rasters(case.out_dir)["time_of_arrival"],
            epsg=case.epsg,
        )
        ny, nx = toa_s.shape
        # Ignition sits at the domain centre (constant deck) -> centre cell.
        verification, _overlay = verify_elliptical_replication(
            toa_s,
            cellsize_m=float(cell_m),
            ignition_rowcol=(ny // 2, nx // 2),
            wind_from_deg=float(run_args.wind_dir_deg),
        )
        head_m = float(verification.get("head_m", 0.0))
        touches_edge = bool(verification.get("touches_domain_edge", False))
        crown_active_km2 = float(case.crown_active_area_km2 or 0.0)

        base = await asyncio.to_thread(
            publish_primary_from_out_dir,
            case, bbox=bbox, duration_s=duration_s, ignition_lonlat=ignition,
        )
    finally:
        cleanup_cases(cases, keep_out_dir=None)

    if crown_active_km2 <= 1e-6:
        raise FireSpreadComposerError(
            "ELMFIRE_NO_ACTIVE_CROWN",
            "the verification deck did not produce an ACTIVE crown fire "
            f"(active-crown area {crown_active_km2:.4f} km2); cannot verify the "
            "Cruz active-crown rate of spread. Check wind / canopy / moisture.",
        )
    if head_m <= 0.0:
        raise FireSpreadComposerError(
            "ELMFIRE_NO_SPREAD",
            "the verification deck produced no measurable head spread extent.",
        )

    numerical_ros = head_m / max(duration_min, 1e-6)

    effm_pct = float(FUEL_MOISTURE_PRESETS[fuel_moisture]["m1_pct"])
    # cbd stored units are CBD_TIMES_100 -> kg/m3 = stored / 100.
    cbd_kg_m3 = float(_CROWN_CANOPY["cbd"]) / 100.0
    cruz_ros = cruz_active_crown_ros_m_min(
        wind_speed_mph_20ft=float(wind_speed_mph),
        cbd_kg_m3=cbd_kg_m3,
        effm_pct=effm_pct,
    )

    rel_error = abs(numerical_ros - cruz_ros) / max(cruz_ros, 1e-6)
    passed = bool(rel_error <= _CROWN_ROS_TOLERANCE and not touches_edge)

    primary = ElmfireCrownRosVerificationLayerURI(
        layer_id=base.layer_id,
        name="Fire arrival time (active crown-fire ROS verification)",
        layer_type=base.layer_type,
        uri=base.uri,
        style_preset=base.style_preset or ELMFIRE_TOA_STYLE_PRESET,
        role=base.role,
        bbox=base.bbox,
        burned_area_km2=base.burned_area_km2,
        fire_arrival_max_hr=base.fire_arrival_max_hr,
        max_flame_length_m=base.max_flame_length_m,
        max_spread_rate_m_min=base.max_spread_rate_m_min,
        duration_hours=base.duration_hours,
        ignition_lonlat=base.ignition_lonlat,
        numerical_ros_m_min=float(numerical_ros),
        cruz_ros_m_min=float(cruz_ros),
        rel_error=float(rel_error),
        tolerance=float(_CROWN_ROS_TOLERANCE),
        passed=passed,
        wind_speed_mph=float(wind_speed_mph),
        cbd_kg_m3=float(cbd_kg_m3),
        effm_pct=float(effm_pct),
    )

    await _maybe_emit_crown_ros_chart(emitter, primary)
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 -- non-fatal
            logger.warning("crown_ros verification authoritative zoom-to failed: %s", exc)

    logger.info(
        "model_elmfire_crown_ros_verification complete numerical=%.3f cruz=%.3f "
        "rel_err=%.4f edge=%s active_km2=%.3f passed=%s uri=%s",
        numerical_ros, cruz_ros, rel_error, touches_edge, crown_active_km2,
        passed, primary.uri,
    )
    return primary


async def _maybe_emit_crown_ros_chart(
    emitter: Any, primary: ElmfireCrownRosVerificationLayerURI
) -> None:
    """Emit a numerical-vs-Cruz bar chart (the two rates side by side)."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {
            "values": [
                {"source": "numerical (level-set head)",
                 "ros_m_min": primary.numerical_ros_m_min},
                {"source": "Cruz 2005 (closed form)",
                 "ros_m_min": primary.cruz_ros_m_min},
            ]
        },
        "mark": "bar",
        "encoding": {
            "x": {"field": "source", "type": "nominal", "title": None},
            "y": {"field": "ros_m_min", "type": "quantitative",
                  "title": "active crown-fire ROS (m/min)"},
            "color": {"field": "source", "type": "nominal", "legend": None},
        },
    }
    from trid3nt_server.data.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Active crown-fire ROS: numerical vs Cruz (2005) closed form",
        caption=(
            "The numerical level-set head spread rate vs the Cruz (2005) "
            "active crown-fire rate of spread evaluated at the deck's own inputs "
            f"(rel. error {primary.rel_error * 100:.1f} %, tolerance "
            f"{primary.tolerance * 100:.0f} %)."
        ),
        source_layer_uri=primary.uri,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001 -- non-fatal
        logger.warning("crown_ros verification chart emit failed: %s", exc)
