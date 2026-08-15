"""Engine template ``elmfire_dead_fuel_moisture_interpolation_frequency_control`` -
how coarsening the dead-fuel-moisture interpolation cadence trades accuracy for
cost on a transient moisture-recovery deck.

A distinct question CLASS (per the capability-naming rule), and the STOP
that was a no-op on constant decks: on a TRANSIENT deck whose dead fuel moisture
recovers (rises) over the run, ELMFIRE re-interpolates the 1/10/100-hour dead-fuel
moisture rasters from the bracketing meteorology bands every DT_INTERPOLATE_M1 /
_M10 / _M100 seconds, holding them stair-step between refreshes. Coarsening that
cadence is cheaper (fewer interpolation passes) but lags the moisture the fire
sees - an accuracy-vs-cost knob only meaningful once the weather actually varies
in time.

The composer sweeps the DT_INTERPOLATE_M1 cadence (M10/M100 scaled by the standard
10x/100x ratio) across a small ladder on an ALL-CONSTANT flat grass deck forced by
a synthetic moisture-recovery schedule, and reports each cadence's burned area; the
deviation from the finest (reference) cadence is the interpolation-cadence error.

Fidelity: a controlled flat grass deck; the only time-varying input is the
synthetic dead-fuel moisture-recovery schedule (multi-band, interpolated) - NOT a
real reanalysis forcing. Data: NO LANDFIRE/DEM fetch. The synthetic schedule rides
the input-review gate (basis default_demo).

Determinism boundary (Invariant 1): every burned area comes from the typed
``ElmfireSensitivityLayerURI`` fields the sweep measured - never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.elmfire_contracts import (
    ELMFIRE_TOA_STYLE_PRESET,
    ElmfireRunArgs,
    ElmfireSensitivityLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.gates.input_review import gate_input_review
from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.elmfire._template_card import TemplateCard
from trid3nt_server.agent.workflows.elmfire.fire_spread.fire_spread import (
    FireSpreadComposerError,
)
from trid3nt_server.agent.workflows.elmfire.postprocess_elmfire import (
    PostprocessElmfireError,
)
from trid3nt_server.agent.workflows.elmfire.run_elmfire import (
    VERIFICATION_FUEL_MODEL_GR2,
    ElmfireWorkflowError,
)
from trid3nt_server.agent.workflows.elmfire.sensitivity._sensitivity_common import (
    build_sweep_chart_spec,
    cleanup_cases,
    publish_primary_from_out_dir,
    solve_constant_case,
)
from trid3nt_server.emission.pipeline_emitter import begin_substeps, current_emitter

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.elmfire.transient.dead_fuel_interp.dead_fuel_interp"
)

__all__ = [
    "elmfire_dead_fuel_moisture_interpolation_frequency_control",
    "model_elmfire_dead_fuel_interp",
]

#: Neutral mid-CONUS deck centre (geography immaterial on a constant flat deck).
_CENTER_LON: float = -98.5
_CENTER_LAT: float = 38.5

#: The M10 / M100 interpolation cadences track the swept M1 cadence at the standard
#: ELMFIRE ratio (defaults 300 : 3000 : 30000 s) so a single knob moves the whole
#: dead-fuel interpolation frequency coherently.
_M10_RATIO: float = 10.0
_M100_RATIO: float = 100.0


def _recovery_schedule(
    n_bands: int, m1_start: float, m1_end: float
) -> list[dict[str, float]]:
    """A monotonic dead-fuel moisture-RECOVERY schedule (m1 rises m1_start->m1_end).

    m10 = m1 + 1, m100 = m1 + 2 at each band (the ascending NFDRS snapshot ladder).
    ``n_bands`` >= 2 evenly-spaced meteorology times."""
    n = max(int(n_bands), 2)
    bands: list[dict[str, float]] = []
    for k in range(n):
        f = k / (n - 1)
        m1 = m1_start + (m1_end - m1_start) * f
        bands.append({"m1": m1, "m10": m1 + 1.0, "m100": m1 + 2.0})
    return bands


TEMPLATE_CARD = TemplateCard(
    question=(
        "how coarsening the dead-fuel-moisture interpolation cadence "
        "(DT_INTERPOLATE_M1/M10/M100) trades accuracy for cost on a transient "
        "moisture-recovery fire deck"
    ),
    required_inputs=[],
    knobs=(
        "cadence_min_s, cadence_max_s, n_steps, n_bands, m1_start_pct, m1_end_pct, "
        "wind_speed_mph, duration_hours, cellsize_m, domain_km, input_mode"
    ),
)

_METADATA = AtomicToolMetadata(
    name="elmfire_dead_fuel_moisture_interpolation_frequency_control",
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
async def elmfire_dead_fuel_moisture_interpolation_frequency_control(
    cadence_min_s: float = 60.0,
    cadence_max_s: float = 1800.0,
    n_steps: int = 4,
    n_bands: int = 4,
    m1_start_pct: float = 3.0,
    m1_end_pct: float = 10.0,
    wind_speed_mph: float = 18.0,
    duration_hours: float = 1.0,
    cellsize_m: float = 60.0,
    domain_km: float = 8.0,
    fuel_model: int = VERIFICATION_FUEL_MODEL_GR2,
    input_mode: str | None = None,
    compute_class: str = "small",
    **_extra_ignored: Any,
) -> ElmfireSensitivityLayerURI | dict[str, Any]:
    """Sweep the dead-fuel-moisture interpolation cadence on a transient deck.

    Fidelity: a controlled ALL-CONSTANT flat grass deck forced by a SYNTHETIC
    dead-fuel moisture-RECOVERY schedule (the 1/10/100-hour dead-fuel moisture
    rises over the run, multi-band + time-interpolated) - NOT a real reanalysis
    forcing. DT_INTERPOLATE_M1 (with M10/M100 scaled 10x/100x) is swept from fine
    to coarse; each cadence's burned area is measured. A coarse cadence refreshes
    the moisture rasters less often (cheaper) but lags the recovering moisture the
    fire sees, so the burned area drifts from the fine-cadence reference - the
    accuracy-vs-cost tradeoff that is a no-op on a constant deck.
    Data: NO LANDFIRE/DEM fetch. The synthetic schedule rides the input-review gate.
    Off-scope: real wildfire spread over LANDFIRE fuels -> elmfire_fire_spread.

    Use this when: the user asks how the fuel-moisture (or weather) interpolation
    frequency / update cadence affects a fire result, or the accuracy-vs-cost of
    coarsening the dead-fuel moisture interpolation.

    Params:
        cadence_min_s: finest DT_INTERPOLATE_M1 cadence, s (default 60 = reference).
        cadence_max_s: coarsest cadence, s (default 1800).
        n_steps: number of cadences swept (default 4; each is one solve).
        n_bands: meteorology times in the recovery schedule (default 4).
        m1_start_pct: 1-hr dead-fuel moisture at ignition, percent (default 3).
        m1_end_pct: 1-hr dead-fuel moisture at the end of the run, percent
            (default 10 = a wetting/recovery trend).
        wind_speed_mph: constant wind (ELMFIRE 20 ft convention, default 18).
        duration_hours: burn duration per point (default 1.0).
        cellsize_m: computational cell size (default 60).
        domain_km: square domain side length, km (default 8).
        fuel_model: the uniform FBFM fuel-model code (default 102 = GR2 grass).
        input_mode: input-review gate lever ("auto"|"user_gated"; None -> session
            default) for the synthetic moisture schedule.
        compute_class: compute class (default "small").

    Returns:
        On success: ``ElmfireSensitivityLayerURI`` - the finest-cadence (reference)
        run's time-of-arrival COG, the burned-area-vs-cadence ``sweep``, and a
        ``summary`` (``reference_cadence_s``, ``reference_burned_area_km2``,
        ``max_burned_area_deviation_km2`` / ``_fraction``, the coarsest cadence).
        A burned-area-vs-cadence chart (reference baseline) is emitted.
        On failure / a cancelled review: ``{"status": "error", ...}``.
        Not cached (``cacheable=False``).
    """
    if int(n_steps) < 1:
        return {
            "status": "error",
            "error_code": "FIRE_PARAMS_INVALID",
            "error_message": "n_steps must be >= 1",
        }
    if float(cadence_min_s) <= 0 or float(cadence_max_s) <= 0:
        return {
            "status": "error",
            "error_code": "FIRE_PARAMS_INVALID",
            "error_message": "cadences must be > 0",
        }

    entries = [
        SyntheticInput(
            param="dead_fuel_moisture_recovery_schedule",
            value=(
                f"1-hr dead-fuel moisture {float(m1_start_pct):.1f}% -> "
                f"{float(m1_end_pct):.1f}% over the {float(duration_hours):.2g} h run "
                f"({int(n_bands)} meteorology bands)"
            ),
            units="percent",
            basis="default_demo",
            note="synthetic dead-fuel moisture-recovery schedule; NOT a real "
            "reanalysis forcing",
        )
    ]
    review = await gate_input_review(
        tool_name="elmfire_dead_fuel_moisture_interpolation_frequency_control",
        mode=input_mode,
        entries=entries,
        params={},
    )
    if review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": (
                f"elmfire_dead_fuel_moisture_interpolation_frequency_control "
                f"{review.cancel_reason}"
            ),
        }

    lo, hi = float(cadence_min_s), float(cadence_max_s)
    n = int(n_steps)
    cadences = [lo] if n == 1 else [lo + (hi - lo) * i / (n - 1) for i in range(n)]

    try:
        primary = await model_elmfire_dead_fuel_interp(
            cadences=cadences,
            n_bands=int(n_bands),
            m1_start_pct=float(m1_start_pct),
            m1_end_pct=float(m1_end_pct),
            wind_speed_mph=float(wind_speed_mph),
            duration_hours=float(duration_hours),
            cellsize_m=float(cellsize_m),
            domain_km=float(domain_km),
            fuel_model=int(fuel_model),
            compute_class=compute_class,
        )
        logger.info(
            "elmfire_dead_fuel_moisture_interpolation_frequency_control complete "
            "layer_id=%s ref=%.4g max_dev_frac=%.4g uri=%s",
            primary.layer_id,
            primary.summary.get("reference_burned_area_km2", float("nan")),
            primary.summary.get("max_burned_area_deviation_fraction", float("nan")),
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (ElmfireWorkflowError, PostprocessElmfireError, FireSpreadComposerError) as exc:
        logger.warning(
            "elmfire_dead_fuel_moisture_interpolation_frequency_control failed: %s (%s)",
            getattr(exc, "error_code", "?"), exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "FIRE_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "elmfire_dead_fuel_moisture_interpolation_frequency_control unexpected failure"
        )
        return {
            "status": "error",
            "error_code": "FIRE_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_elmfire_dead_fuel_interp(
    *,
    cadences: list[float],
    n_bands: int,
    m1_start_pct: float,
    m1_end_pct: float,
    wind_speed_mph: float,
    duration_hours: float,
    cellsize_m: float,
    domain_km: float,
    fuel_model: int,
    compute_class: str = "small",
) -> ElmfireSensitivityLayerURI:
    """Compose the dead-fuel interpolation-cadence sweep end-to-end (transient deck)."""
    emitter = current_emitter()
    duration_s = float(duration_hours) * 3600.0

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
        except Exception as exc:  # noqa: BLE001
            logger.warning("dead_fuel_interp zoom-to failed: %s", exc)

    schedule = _recovery_schedule(int(n_bands), float(m1_start_pct), float(m1_end_pct))
    dt_meteorology_s = duration_s / (len(schedule) - 1)

    begin_substeps(emitter, len(cadences) + 1)

    def _mk_run_args() -> ElmfireRunArgs:
        return ElmfireRunArgs(
            bbox=bbox,  # type: ignore[arg-type]
            ignition_lonlat=ignition,  # type: ignore[arg-type]
            wind_speed_mph=float(wind_speed_mph),
            wind_dir_deg=270.0,
            fuel_moisture="dry",  # type: ignore[arg-type]
            duration_hours=float(duration_hours),
            cellsize_m=float(cellsize_m),
        )

    cases = []
    try:
        for cad in cadences:
            case = await solve_constant_case(
                _mk_run_args(),
                knob_value=float(cad),
                fuel_model=int(fuel_model),
                weather_schedule=schedule,
                dt_meteorology_s=dt_meteorology_s,
                time_control_extra={
                    "DT_INTERPOLATE_M1": f"{float(cad):.1f}",
                    "DT_INTERPOLATE_M10": f"{float(cad) * _M10_RATIO:.1f}",
                    "DT_INTERPOLATE_M100": f"{float(cad) * _M100_RATIO:.1f}",
                },
                compute_class=compute_class,
                emitter=emitter,
            )
            cases.append(case)
            logger.info(
                "dead_fuel_interp point cadence=%.0fs burned_km2=%.5f",
                cad, case.burned_area_km2,
            )

        # Representative = the FINEST cadence (the reference / most-accurate run).
        rep = min(cases, key=lambda c: c.knob_value)
        base = await asyncio.to_thread(
            publish_primary_from_out_dir,
            rep, bbox=bbox, duration_s=duration_s, ignition_lonlat=ignition,
        )
    finally:
        cleanup_cases(cases, keep_out_dir=None)

    ordered = sorted(cases, key=lambda c: c.knob_value)
    sweep = [{"x": c.knob_value, "y": float(c.burned_area_km2)} for c in ordered]
    reference = ordered[0]
    ref_area = float(reference.burned_area_km2)
    deviations = [abs(float(c.burned_area_km2) - ref_area) for c in ordered]
    max_dev = max(deviations) if deviations else 0.0
    summary = {
        "reference_cadence_s": float(reference.knob_value),
        "reference_burned_area_km2": ref_area,
        "coarsest_cadence_s": float(ordered[-1].knob_value),
        "coarsest_burned_area_km2": float(ordered[-1].burned_area_km2),
        "max_burned_area_deviation_km2": float(max_dev),
        "max_burned_area_deviation_fraction": float(max_dev / ref_area) if ref_area > 0 else 0.0,
        "n_steps": float(len(ordered)),
    }

    primary = ElmfireSensitivityLayerURI(
        layer_id=base.layer_id,
        name="Fire arrival time (dead-fuel interpolation cadence sweep, reference)",
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
        swept_param="dt_interpolate_m1_s",
        swept_units="seconds",
        response_metric="burned_area_km2",
        response_units="km2",
        sweep=sweep,
        summary=summary,
    )

    await _maybe_emit_chart(emitter, sweep, ref_area, primary.uri)
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("dead_fuel_interp authoritative zoom-to failed: %s", exc)

    logger.info(
        "model_elmfire_dead_fuel_interp complete ref=%.5f max_dev=%.5f uri=%s",
        ref_area, max_dev, primary.uri,
    )
    return primary


async def _maybe_emit_chart(
    emitter: Any, sweep: list[dict[str, float]], ref_area: float, source_uri: str
) -> None:
    """Emit the burned-area-vs-interpolation-cadence chart (reference baseline)."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    spec = build_sweep_chart_spec(
        sweep,
        x_title="dead-fuel moisture interpolation cadence (s)",
        y_title="burned area (km2)",
        reference_y=float(ref_area),
        reference_label="Burned area vs dead-fuel interpolation cadence",
    )
    if spec is None:
        return
    from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Accuracy vs cost: dead-fuel moisture interpolation cadence",
        caption=(
            "Burned area at each dead-fuel-moisture interpolation cadence on a "
            "transient moisture-recovery deck; the finest cadence (dotted baseline) "
            "is the reference, and coarser (cheaper) cadences lag the recovering "
            "moisture and drift the burned area - the accuracy-vs-cost tradeoff."
        ),
        source_layer_uri=source_uri,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("dead_fuel_interp chart emit failed: %s", exc)
