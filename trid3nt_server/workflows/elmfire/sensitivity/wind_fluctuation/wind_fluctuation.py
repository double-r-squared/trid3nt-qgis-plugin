"""Engine template ``elmfire_wind_fluctuation_randomization`` - how per-member
randomized sub-hourly wind fluctuation spreads fire outcomes vs a deterministic run.

A distinct question CLASS (per the capability-naming rule): not one fixed-wind
spread, but how much the burned outcome VARIES when ELMFIRE's stochastic wind
fluctuation (WIND_FLUCTUATIONS) perturbs the wind each DT_WIND_FLUCTUATIONS with
a fresh random seed per member -- the single-run building block of a Monte-Carlo
wind ensemble. It is its OWN registered engine TEMPLATE (engine="elmfire",
tier="template").

The composer runs ONE deterministic member (WIND_FLUCTUATIONS off) plus a small
number of randomized members (WIND_FLUCTUATIONS on, RANDOMIZE_RANDOM_SEED on) on
an ALL-CONSTANT flat grass deck (no LANDFIRE/DEM fetch), and reports the burned
area of each member so the ensemble spread around the deterministic baseline is
visible. A full production burn-probability ensemble (County-Fire
NUM_ENSEMBLE_MEMBERS=100) is the ctl-file-driven Monte-Carlo run mode -- this
template exercises the per-member wind-fluctuation knob that underlies it.

Determinism boundary (Invariant 1): every narrated number comes from the typed
``ElmfireSensitivityLayerURI`` fields the members measured -- never free-generated.
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
    "trid3nt_server.workflows.elmfire.sensitivity.wind_fluctuation.wind_fluctuation"
)

__all__ = [
    "elmfire_wind_fluctuation_randomization",
    "model_elmfire_wind_fluctuation",
]

_CENTER_LON: float = -98.5
_CENTER_LAT: float = 38.5


TEMPLATE_CARD = TemplateCard(
    question=(
        "how much wildfire burned-area outcomes spread when sub-hourly wind is "
        "randomly perturbed per ensemble member vs a deterministic run -- the "
        "per-member wind-fluctuation building block of a Monte-Carlo wind ensemble"
    ),
    required_inputs=[],
    knobs=(
        "n_members, ws_fluctuation_intensity, wd_fluctuation_intensity, "
        "dt_wind_fluctuations_s, wind_speed_mph, wind_dir_deg, duration_hours, "
        "cellsize_m, domain_km"
    ),
)

_METADATA = AtomicToolMetadata(
    name="elmfire_wind_fluctuation_randomization",
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
async def elmfire_wind_fluctuation_randomization(
    n_members: int = 4,
    ws_fluctuation_intensity: float = 0.3,
    wd_fluctuation_intensity: float = 0.1,
    dt_wind_fluctuations_s: float = 15.0,
    wind_speed_mph: float = 25.0,
    wind_dir_deg: float = 270.0,
    duration_hours: float = 0.75,
    cellsize_m: float = 45.0,
    domain_km: float = 10.0,
    fuel_model: int = VERIFICATION_FUEL_MODEL_GR2,
    fuel_moisture: str = "dry",
    compute_class: str = "small",
    **_extra_ignored: Any,
) -> ElmfireSensitivityLayerURI | dict[str, Any]:
    """Measure how randomized wind fluctuation spreads fire outcomes vs deterministic.

    Fidelity: a Monte-Carlo wind-fluctuation sweep on a controlled ALL-CONSTANT
    flat grass deck (single fuel model, flat terrain, uniform base wind) -- NOT a
    real-landscape fire. One deterministic member (fluctuation off) plus
    ``n_members`` randomized members (WIND_FLUCTUATIONS on, a fresh random seed
    each) are run; the burned area of each is reported so the ensemble spread
    around the deterministic baseline is visible.
    Data: NO LANDFIRE/DEM fetch -- the deck is authored agent-side as constants.
    A full burn-probability ensemble (NUM_ENSEMBLE_MEMBERS=100) is the ctl-file
    Monte-Carlo run mode; this exercises the per-member wind-fluctuation knob.
    Off-scope: real wildfire spread over LANDFIRE fuels -> elmfire_fire_spread.

    Use this when: the user asks how sub-hourly wind randomization / stochastic
    wind fluctuation spreads fire outcomes, for a per-member wind-ensemble
    what-if, or deterministic-vs-randomized wind fire variability.

    Params:
        n_members: number of RANDOMIZED members (default 4; each is one solve, on
            top of the single deterministic baseline).
        ws_fluctuation_intensity: WIND_SPEED_FLUCTUATION_INTENSITY, the fractional
            wind-speed perturbation amplitude (default 0.3 -> about +/-15%).
        wd_fluctuation_intensity: WIND_DIRECTION_FLUCTUATION_INTENSITY, the
            direction-perturbation amplitude (default 0.1 -> about +/-18 deg).
        dt_wind_fluctuations_s: how often the wind is re-perturbed, s (default 15).
        wind_speed_mph: base wind speed (ELMFIRE 20 ft convention, default 25).
        wind_dir_deg: base direction wind blows FROM, meteorological deg (270).
        duration_hours: burn duration per member (short; default 0.75).
        cellsize_m: computational cell size (default 45).
        domain_km: square domain side length, km (default 10).
        fuel_model: the uniform FBFM fuel-model code (default 102 = GR2 grass).
        fuel_moisture: "dry" (default), "moderate", or "moist".
        compute_class: compute class (default "small").

    Returns:
        On success: ``ElmfireSensitivityLayerURI`` -- the deterministic member's
        time-of-arrival COG, plus a per-member burned-area ``sweep`` (member 0 =
        deterministic) and a ``summary`` (``deterministic_burned_area_km2``,
        ``member_mean``/``member_min``/``member_max``/``member_std``,
        ``spread_fraction``, ``ws_fluctuation_intensity``). A burned-area-per-member
        chart with the deterministic baseline line is emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if int(n_members) < 1:
        return {
            "status": "error",
            "error_code": "FIRE_PARAMS_INVALID",
            "error_message": "n_members must be >= 1",
        }
    try:
        primary = await model_elmfire_wind_fluctuation(
            n_members=int(n_members),
            ws_fluctuation_intensity=float(ws_fluctuation_intensity),
            wd_fluctuation_intensity=float(wd_fluctuation_intensity),
            dt_wind_fluctuations_s=float(dt_wind_fluctuations_s),
            wind_speed_mph=float(wind_speed_mph),
            wind_dir_deg=float(wind_dir_deg),
            duration_hours=float(duration_hours),
            cellsize_m=float(cellsize_m),
            domain_km=float(domain_km),
            fuel_model=int(fuel_model),
            fuel_moisture=str(fuel_moisture),
            compute_class=compute_class,
        )
        logger.info(
            "elmfire_wind_fluctuation_randomization complete layer_id=%s "
            "det=%.4g mean=%.4g spread_frac=%.4g uri=%s",
            primary.layer_id,
            primary.summary.get("deterministic_burned_area_km2", float("nan")),
            primary.summary.get("member_mean", float("nan")),
            primary.summary.get("spread_fraction", float("nan")),
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (ElmfireWorkflowError, PostprocessElmfireError, FireSpreadComposerError) as exc:
        logger.warning(
            "elmfire_wind_fluctuation_randomization failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "FIRE_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("elmfire_wind_fluctuation_randomization unexpected failure")
        return {
            "status": "error",
            "error_code": "FIRE_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_elmfire_wind_fluctuation(
    *,
    n_members: int,
    ws_fluctuation_intensity: float,
    wd_fluctuation_intensity: float,
    dt_wind_fluctuations_s: float,
    wind_speed_mph: float,
    wind_dir_deg: float,
    duration_hours: float,
    cellsize_m: float,
    domain_km: float,
    fuel_model: int,
    fuel_moisture: str,
    compute_class: str = "small",
) -> ElmfireSensitivityLayerURI:
    """Compose the deterministic + randomized-member wind-fluctuation sweep."""
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
            logger.warning("wind_fluctuation zoom-to failed: %s", exc)

    randomized_extra = {
        "WIND_FLUCTUATIONS": ".TRUE.",
        "WIND_SPEED_FLUCTUATION_INTENSITY": f"{float(ws_fluctuation_intensity):.4f}",
        "WIND_DIRECTION_FLUCTUATION_INTENSITY": f"{float(wd_fluctuation_intensity):.4f}",
        "DT_WIND_FLUCTUATIONS": f"{float(dt_wind_fluctuations_s):.4f}",
        "RANDOMIZE_RANDOM_SEED": ".TRUE.",
    }
    # member 0 = deterministic (no extra), members 1..n = randomized.
    plan: list[tuple[int, dict[str, str] | None]] = [(0, None)]
    plan += [(m, randomized_extra) for m in range(1, int(n_members) + 1)]

    begin_substeps(emitter, len(plan) + 1)

    def _mk_run_args() -> ElmfireRunArgs:
        return ElmfireRunArgs(
            bbox=bbox,  # type: ignore[arg-type]
            ignition_lonlat=ignition,  # type: ignore[arg-type]
            wind_speed_mph=float(wind_speed_mph),
            wind_dir_deg=float(wind_dir_deg),
            fuel_moisture=fuel_moisture,  # type: ignore[arg-type]
            duration_hours=float(duration_hours),
            cellsize_m=float(cellsize_m),
        )

    cases = []
    det_case = None
    try:
        for member, extra in plan:
            case = await solve_constant_case(
                _mk_run_args(),
                knob_value=float(member),
                fuel_model=int(fuel_model),
                simulator_extra=extra,
                compute_class=compute_class,
                emitter=emitter,
                measure_ltw=False,
            )
            case.extras["member"] = float(member)
            case.extras["randomized"] = 0.0 if extra is None else 1.0
            cases.append(case)
            if member == 0:
                det_case = case
            logger.info(
                "wind_fluctuation member=%d randomized=%s burned_km2=%.5f",
                member, extra is not None, case.burned_area_km2,
            )

        if det_case is None:
            raise FireSpreadComposerError(
                "ELMFIRE_NO_LAYERS", "no deterministic member solved"
            )
        base = await asyncio.to_thread(
            publish_primary_from_out_dir,
            det_case,
            bbox=bbox,
            duration_s=duration_s,
            ignition_lonlat=ignition,
        )
    finally:
        cleanup_cases(cases, keep_out_dir=None)

    sweep = [
        {"x": float(c.extras.get("member", 0.0)), "y": float(c.burned_area_km2)}
        for c in sorted(cases, key=lambda c: c.extras.get("member", 0.0))
    ]
    det_area = float(det_case.burned_area_km2)
    randomized_areas = [
        float(c.burned_area_km2) for c in cases if c.extras.get("randomized", 0.0) > 0.5
    ]
    n_rand = len(randomized_areas)
    mean = sum(randomized_areas) / n_rand if n_rand else det_area
    amin = min(randomized_areas) if randomized_areas else det_area
    amax = max(randomized_areas) if randomized_areas else det_area
    var = (
        sum((a - mean) ** 2 for a in randomized_areas) / n_rand if n_rand else 0.0
    )
    std = var ** 0.5
    spread_fraction = (amax - amin) / det_area if det_area > 0 else 0.0
    summary = {
        "deterministic_burned_area_km2": det_area,
        "member_mean": float(mean),
        "member_min": float(amin),
        "member_max": float(amax),
        "member_std": float(std),
        "spread_fraction": float(spread_fraction),
        "n_members": float(n_rand),
        "ws_fluctuation_intensity": float(ws_fluctuation_intensity),
        "wd_fluctuation_intensity": float(wd_fluctuation_intensity),
    }

    primary = ElmfireSensitivityLayerURI(
        layer_id=base.layer_id,
        name="Fire arrival time (wind-fluctuation ensemble, deterministic member)",
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
        swept_param="wind_fluctuation_member",
        swept_units="member",
        response_metric="burned_area_km2",
        response_units="km2",
        sweep=sweep,
        summary=summary,
    )

    await _maybe_emit_chart(emitter, sweep, det_area, primary.uri)
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("wind_fluctuation authoritative zoom-to failed: %s", exc)

    logger.info(
        "model_elmfire_wind_fluctuation complete det=%.5f mean=%.5f min=%.5f "
        "max=%.5f spread_frac=%.4f uri=%s",
        det_area, mean, amin, amax, spread_fraction, primary.uri,
    )
    return primary


async def _maybe_emit_chart(
    emitter: Any, sweep: list[dict[str, float]], det_area: float, source_uri: str
) -> None:
    """Emit the burned-area-per-member chart with the deterministic baseline line."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    spec = build_sweep_chart_spec(
        sweep,
        x_title="member (0 = deterministic)",
        y_title="burned area (km2)",
        reference_y=float(det_area),
        reference_label="Burned area per member (deterministic dashed)",
    )
    if spec is None:
        return
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Wind-fluctuation ensemble spread vs deterministic",
        caption=(
            "Burned area of each member: member 0 is the deterministic run "
            "(dashed baseline); the randomized members scatter around it, "
            "showing how much sub-hourly wind randomization spreads the outcome."
        ),
        source_layer_uri=source_uri,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("wind_fluctuation chart emit failed: %s", exc)
