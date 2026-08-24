"""Engine template ``elmfire_live_fuel_moisture_sensitivity`` - how a uniform
live herbaceous fuel-moisture scalar changes surface fire spread.

A distinct question CLASS (per the capability-naming rule): not the dead/live
preset dial of ``elmfire_fire_spread``, but a direct sweep of the UNIFORM live
herbaceous moisture scalar (LH_MOISTURE_CONTENT) -- overriding the preset -- to
show how curing/greenness of the live grass load changes burned area and spread
rate. It is its OWN registered engine TEMPLATE (engine="elmfire", tier="template").

The composer sweeps live herbaceous moisture across a small ladder on an
ALL-CONSTANT flat dynamic-grass deck (no LANDFIRE/DEM fetch), holding dead
moisture + wind fixed, and reports burned area at each moisture -- the
sensitivity of spread to the uniform live-moisture override. Live woody and
foliar moisture are set alongside as fixed uniform scalars.

Determinism boundary (Invariant 1): every narrated number comes from the typed
``ElmfireSensitivityLayerURI`` fields the sweep measured -- never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.elmfire_contracts import (
    ELMFIRE_TOA_STYLE_PRESET,
    ElmfireRunArgs,
    ElmfireSensitivityLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.elmfire._template_card import TemplateCard
from trid3nt_server.workflows.elmfire.fire_spread.fire_spread import (
    FireSpreadComposerError,
)
from trid3nt_server.workflows.elmfire.postprocess_elmfire import (
    PostprocessElmfireError,
)
from trid3nt_server.workflows.elmfire.run_elmfire import (
    VERIFICATION_FUEL_MODEL_GR2,
    ElmfireWorkflowError,
)
from trid3nt_server.workflows.elmfire.sensitivity._sensitivity_common import (
    build_sweep_chart_spec,
    cleanup_cases,
    publish_primary_from_out_dir,
    solve_constant_case,
)
from trid3nt_server.emission.pipeline_emitter import begin_substeps, current_emitter

logger = logging.getLogger(
    "trid3nt_server.workflows.elmfire.sensitivity.live_moisture.live_moisture"
)

__all__ = [
    "elmfire_live_fuel_moisture_sensitivity",
    "model_elmfire_live_moisture",
]

_CENTER_LON: float = -98.5
_CENTER_LAT: float = 38.5


TEMPLATE_CARD = TemplateCard(
    question=(
        "how uniform live herbaceous fuel moisture (grass curing vs greenness) "
        "changes wildfire burned area and spread rate -- a live-moisture override "
        "sweep on a constant flat dynamic-grass deck"
    ),
    required_inputs=[],
    knobs=(
        "lh_min_pct, lh_max_pct, n_moisture_steps, live_woody_pct, foliar_pct, "
        "wind_speed_mph, wind_dir_deg, duration_hours, cellsize_m, domain_km"
    ),
)

_METADATA = AtomicToolMetadata(
    name="elmfire_live_fuel_moisture_sensitivity",
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
async def elmfire_live_fuel_moisture_sensitivity(
    lh_min_pct: float = 30.0,
    lh_max_pct: float = 120.0,
    n_moisture_steps: int = 3,
    live_woody_pct: float = 90.0,
    foliar_pct: float = 90.0,
    wind_speed_mph: float = 20.0,
    wind_dir_deg: float = 270.0,
    duration_hours: float = 0.75,
    cellsize_m: float = 45.0,
    domain_km: float = 10.0,
    fuel_model: int = VERIFICATION_FUEL_MODEL_GR2,
    dead_fuel_moisture: str = "dry",
    compute_class: str = "small",
    **_extra_ignored: Any,
) -> ElmfireSensitivityLayerURI | dict[str, Any]:
    """Sweep uniform live herbaceous moisture and measure surface fire spread.

    Fidelity: a SENSITIVITY sweep on a controlled ALL-CONSTANT flat dynamic-grass
    deck (single fuel model, flat terrain, uniform wind) -- NOT a real-landscape
    fire. Live herbaceous moisture (LH_MOISTURE_CONTENT) is swept as a uniform
    scalar override with dead moisture + wind held fixed; burned area at each
    moisture shows how curing (dry, ~30%) vs greenness (~120%) of the live grass
    load changes spread.
    Data: NO LANDFIRE/DEM fetch, and NO live-fuel-moisture raster -- the live
    moisture is a UNIFORM scalar override (the raster-driven LFM path is not
    fetched here). Off-scope: real wildfire spread over LANDFIRE fuels ->
    elmfire_fire_spread.

    Use this when: the user asks how live/green fuel moisture, grass curing, or
    the live herbaceous moisture scalar changes fire spread or burned area.

    Params:
        lh_min_pct: lowest live herbaceous moisture in the ladder, percent (30 =
            fully cured grass).
        lh_max_pct: highest live herbaceous moisture, percent (120 = green).
        n_moisture_steps: number of moisture points swept (default 3; one solve each).
        live_woody_pct: uniform live woody moisture, percent (fixed; default 90).
        foliar_pct: uniform foliar (canopy) moisture, percent (fixed; default 90).
        wind_speed_mph: base wind speed (ELMFIRE 20 ft convention, default 20).
        wind_dir_deg: direction wind blows FROM, meteorological deg (default 270).
        duration_hours: burn duration per point (short; default 0.75).
        cellsize_m: computational cell size (default 45).
        domain_km: square domain side length, km (default 10).
        fuel_model: the uniform FBFM fuel-model code (default 102 = GR2 dynamic grass).
        dead_fuel_moisture: the FIXED dead-moisture preset ("dry"/"moderate"/"moist").
        compute_class: compute class (default "small").

    Returns:
        On success: ``ElmfireSensitivityLayerURI`` -- the driest-live-moisture
        (largest-burn) run's time-of-arrival COG, plus a burned-area-vs-live-
        moisture ``sweep`` and a ``summary`` (``lh_min_pct``, ``lh_max_pct``,
        ``burned_area_dry``, ``burned_area_green``, ``spread_reduction_fraction``).
        A burned-area-vs-live-moisture chart is emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if int(n_moisture_steps) < 1:
        return {
            "status": "error",
            "error_code": "FIRE_PARAMS_INVALID",
            "error_message": "n_moisture_steps must be >= 1",
        }
    lo, hi = float(lh_min_pct), float(lh_max_pct)
    n = int(n_moisture_steps)
    if n == 1:
        lhs = [lo]
    else:
        lhs = [lo + (hi - lo) * i / (n - 1) for i in range(n)]

    try:
        primary = await model_elmfire_live_moisture(
            lh_values=lhs,
            live_woody_pct=float(live_woody_pct),
            foliar_pct=float(foliar_pct),
            wind_speed_mph=float(wind_speed_mph),
            wind_dir_deg=float(wind_dir_deg),
            duration_hours=float(duration_hours),
            cellsize_m=float(cellsize_m),
            domain_km=float(domain_km),
            fuel_model=int(fuel_model),
            dead_fuel_moisture=str(dead_fuel_moisture),
            compute_class=compute_class,
        )
        logger.info(
            "elmfire_live_fuel_moisture_sensitivity complete layer_id=%s "
            "reduction=%.4g uri=%s",
            primary.layer_id,
            primary.summary.get("spread_reduction_fraction", float("nan")),
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (ElmfireWorkflowError, PostprocessElmfireError, FireSpreadComposerError) as exc:
        logger.warning(
            "elmfire_live_fuel_moisture_sensitivity failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "FIRE_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("elmfire_live_fuel_moisture_sensitivity unexpected failure")
        return {
            "status": "error",
            "error_code": "FIRE_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_elmfire_live_moisture(
    *,
    lh_values: list[float],
    live_woody_pct: float,
    foliar_pct: float,
    wind_speed_mph: float,
    wind_dir_deg: float,
    duration_hours: float,
    cellsize_m: float,
    domain_km: float,
    fuel_model: int,
    dead_fuel_moisture: str,
    compute_class: str = "small",
) -> ElmfireSensitivityLayerURI:
    """Compose the live herbaceous moisture sweep end-to-end (constant decks)."""
    import math

    emitter = current_emitter()
    duration_s = float(duration_hours) * 3600.0

    half_deg_lat = (float(domain_km) * 1000.0 / 2.0) / 111_320.0
    half_deg_lon = half_deg_lat / max(math.cos(math.radians(_CENTER_LAT)), 1e-6)
    bbox = (
        _CENTER_LON - half_deg_lon,
        _CENTER_LAT - half_deg_lat,
        _CENTER_LON + half_deg_lon,
        _CENTER_LAT + half_deg_lat,
    )
    ignition = (_CENTER_LON, _CENTER_LAT)

    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("live_moisture zoom-to failed: %s", exc)

    begin_substeps(emitter, len(lh_values) + 1)
    foliar_extra = {"FOLIAR_MOISTURE_CONTENT": f"{float(foliar_pct):.4f}"}

    cases = []
    try:
        for lh in lh_values:
            run_args = ElmfireRunArgs(
                bbox=bbox,  # type: ignore[arg-type]
                ignition_lonlat=ignition,  # type: ignore[arg-type]
                wind_speed_mph=float(wind_speed_mph),
                wind_dir_deg=float(wind_dir_deg),
                fuel_moisture=dead_fuel_moisture,  # type: ignore[arg-type]
                duration_hours=float(duration_hours),
                cellsize_m=float(cellsize_m),
            )
            case = await solve_constant_case(
                run_args,
                knob_value=float(lh),
                fuel_model=int(fuel_model),
                moisture_override={
                    "lh_pct": float(lh),
                    "lw_pct": float(live_woody_pct),
                },
                inputs_extra=foliar_extra,
                compute_class=compute_class,
                emitter=emitter,
                measure_ltw=False,
            )
            cases.append(case)
            logger.info(
                "live_moisture point lh=%.1f%% burned_km2=%.5f spread_m_min=%s",
                lh, case.burned_area_km2, case.max_spread_rate_m_min,
            )

        # Representative = the driest live moisture (largest burn).
        rep = min(cases, key=lambda c: c.knob_value)
        base = await asyncio.to_thread(
            publish_primary_from_out_dir,
            rep,
            bbox=bbox,
            duration_s=duration_s,
            ignition_lonlat=ignition,
        )
    finally:
        cleanup_cases(cases, keep_out_dir=None)

    sweep = [
        {"x": c.knob_value, "y": float(c.burned_area_km2)}
        for c in sorted(cases, key=lambda c: c.knob_value)
    ]
    area_dry = sweep[0]["y"] if sweep else 0.0
    area_green = sweep[-1]["y"] if sweep else 0.0
    reduction = (area_dry - area_green) / area_dry if area_dry > 0 else 0.0
    summary = {
        "lh_min_pct": float(lh_values[0]),
        "lh_max_pct": float(lh_values[-1]),
        "burned_area_dry": float(area_dry),
        "burned_area_green": float(area_green),
        "spread_reduction_fraction": float(reduction),
        "n_moisture_steps": float(len(sweep)),
    }

    primary = ElmfireSensitivityLayerURI(
        layer_id=base.layer_id,
        name="Fire arrival time (live-moisture sweep, driest live grass)",
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
        swept_param="live_herbaceous_moisture_pct",
        swept_units="percent",
        response_metric="burned_area_km2",
        response_units="km2",
        sweep=sweep,
        summary=summary,
    )

    await _maybe_emit_chart(emitter, sweep, primary.uri)
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("live_moisture authoritative zoom-to failed: %s", exc)

    logger.info(
        "model_elmfire_live_moisture complete dry=%.5f green=%.5f reduction=%.4f uri=%s",
        area_dry, area_green, reduction, primary.uri,
    )
    return primary


async def _maybe_emit_chart(
    emitter: Any, sweep: list[dict[str, float]], source_uri: str
) -> None:
    """Emit the burned-area-vs-live-moisture chart."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    spec = build_sweep_chart_spec(
        sweep,
        x_title="live herbaceous moisture (percent)",
        y_title="burned area (km2)",
    )
    if spec is None:
        return
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Burned area vs live herbaceous moisture",
        caption=(
            "How the burned area falls as live herbaceous grass moisture rises "
            "from cured (dry) to green -- the sensitivity of surface fire spread "
            "to the uniform live-moisture override."
        ),
        source_layer_uri=source_uri,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("live_moisture chart emit failed: %s", exc)
