"""Engine template ``elmfire_transient_wind_schedule_spread`` - how a mid-run wind
shift redirects a fire, versus a constant wind holding the same initial direction.

A distinct question CLASS from a single constant-wind spread (per the capability-
naming rule): ELMFIRE consumes MULTI-BAND weather rasters with NUM_METEOROLOGY_TIMES
> 1 and interpolates them every DT_METEOROLOGY. This template authors a SYNTHETIC
transient weather schedule whose wind DIRECTION shifts partway through the run, then
contrasts the resulting fire against an otherwise identical constant-wind run - the
shift visibly bends the fire's spread axis.

Fidelity: a controlled ALL-CONSTANT flat grass deck (single fuel model, flat
terrain), the ONLY time-varying input being the synthetic wind schedule - NOT a
real reanalysis forcing (gridMET/HRRR ingestion is a later front). Data: NO
LANDFIRE/DEM fetch. The synthetic wind schedule is a model-invented input and
rides the ADR 0107 input-review gate (basis default_demo) - never silently
mistaken for observed weather.

Determinism boundary (Invariant 1): the constant / transient burned areas and the
heading shift come from the typed ``ElmfireSensitivityLayerURI`` fields the two
solved runs measured - never free-generated.
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
    SweepCaseResult,
    build_sweep_chart_spec,
    cleanup_cases,
    publish_primary_from_out_dir,
    solve_constant_case,
)
from trid3nt_server.emission.pipeline_emitter import begin_substeps, current_emitter

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.elmfire.transient.wind_schedule.wind_schedule"
)

__all__ = [
    "elmfire_transient_wind_schedule_spread",
    "model_elmfire_transient_wind_schedule",
]

#: Neutral mid-CONUS deck centre (geography immaterial on a constant flat deck).
_CENTER_LON: float = -98.5
_CENTER_LAT: float = 38.5


def _burned_centroid_shift_deg(
    constant: SweepCaseResult, transient: SweepCaseResult
) -> float:
    """Angular heading shift (deg) of the burned-area centroid, constant->transient.

    Both runs share the ignition (domain centre) and grid; the centroid azimuth
    encodes the mean spread direction, so the change is a scalar proxy for how far
    the wind shift redirected the fire. Read off the two decks' domain centroids
    stored in ``extras`` (``cx``/``cy`` in projected metres from ignition)."""
    cx0 = constant.extras.get("cx"); cy0 = constant.extras.get("cy")
    cx1 = transient.extras.get("cx"); cy1 = transient.extras.get("cy")
    if None in (cx0, cy0, cx1, cy1):
        return 0.0
    a0 = math.degrees(math.atan2(float(cy0), float(cx0)))
    a1 = math.degrees(math.atan2(float(cy1), float(cx1)))
    d = (a1 - a0 + 180.0) % 360.0 - 180.0
    return abs(d)


TEMPLATE_CARD = TemplateCard(
    question=(
        "how a mid-run wind shift redirects a wildfire versus a constant wind - a "
        "synthetic transient (multi-band, time-interpolated) weather schedule whose "
        "wind direction shifts partway through the burn"
    ),
    required_inputs=[],
    knobs=(
        "wind_speed_mph, wind_dir_initial_deg, wind_dir_shifted_deg, shift_fraction, "
        "duration_hours, cellsize_m, domain_km, fuel_model, fuel_moisture, input_mode"
    ),
)

_METADATA = AtomicToolMetadata(
    name="elmfire_transient_wind_schedule_spread",
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
async def elmfire_transient_wind_schedule_spread(
    wind_speed_mph: float = 22.0,
    wind_dir_initial_deg: float = 270.0,
    wind_dir_shifted_deg: float = 180.0,
    shift_fraction: float = 0.5,
    duration_hours: float = 0.5,
    cellsize_m: float = 60.0,
    domain_km: float = 8.0,
    fuel_model: int = VERIFICATION_FUEL_MODEL_GR2,
    fuel_moisture: str = "dry",
    input_mode: str | None = None,
    compute_class: str = "small",
    **_extra_ignored: Any,
) -> ElmfireSensitivityLayerURI | dict[str, Any]:
    """Contrast a mid-run wind SHIFT against a constant wind on the same fire deck.

    Fidelity: a controlled ALL-CONSTANT flat grass deck (single fuel model, flat
    terrain); the ONLY time-varying input is a SYNTHETIC transient weather schedule
    whose wind direction ramps from ``wind_dir_initial_deg`` to
    ``wind_dir_shifted_deg`` (both meteorological deg, the direction wind blows
    FROM), completing the shift at ``shift_fraction`` of the run and holding after.
    ELMFIRE reads it as multi-band weather (NUM_METEOROLOGY_TIMES>1) and
    interpolates it every DT_METEOROLOGY. A constant-wind run (holding the initial
    direction) is run alongside; the shift visibly bends the fire's spread axis and
    changes the burned area. NOT a real reanalysis forcing (that is a later front).
    Data: NO LANDFIRE/DEM fetch. The synthetic wind schedule rides the input-review
    gate (basis default_demo).
    Off-scope: real wildfire spread over LANDFIRE fuels -> elmfire_fire_spread.

    Use this when: the user asks how a wind shift / changing wind / a mid-run wind
    direction change redirects a fire, or for a transient / time-varying wind
    what-if versus a steady wind.

    Params:
        wind_speed_mph: constant wind speed held across both runs (ELMFIRE 20 ft
            convention, default 22).
        wind_dir_initial_deg: the initial wind direction, met deg (default 270 = a
            westerly pushing the fire east).
        wind_dir_shifted_deg: the post-shift direction, met deg (default 180 = a
            southerly pushing the fire north).
        shift_fraction: fraction of the run at which the shift completes (0..1,
            default 0.5 = a mid-run shift, then held).
        duration_hours: burn duration for both runs (default 0.5).
        cellsize_m: computational cell size (default 60).
        domain_km: square domain side length, km (default 8).
        fuel_model: the uniform FBFM fuel-model code (default 102 = GR2 grass).
        fuel_moisture: "dry" (default), "moderate", or "moist".
        input_mode: input-review gate lever ("auto"|"user_gated"; None -> session
            default) for the synthetic wind schedule.
        compute_class: compute class (default "small").

    Returns:
        On success: ``ElmfireSensitivityLayerURI`` - the TRANSIENT run's
        time-of-arrival COG (the redirected fire), a two-point ``sweep``
        (constant vs transient burned area), and a ``summary``
        (``constant_burned_area_km2``, ``transient_burned_area_km2``,
        ``burned_area_delta_km2``, ``heading_shift_deg``, the wind directions).
        A constant-vs-transient burned-area chart is emitted.
        On failure / a cancelled review: ``{"status": "error", ...}``.
        Not cached (``cacheable=False``).
    """
    if not (0.0 < float(shift_fraction) < 1.0):
        return {
            "status": "error",
            "error_code": "FIRE_PARAMS_INVALID",
            "error_message": "shift_fraction must be strictly between 0 and 1",
        }

    # --- ADR 0107 input-review gate: the synthetic wind schedule is the
    # consequential, model-invented input (it decides WHEN and HOW the wind
    # redirects the fire), so it rides the review gate labeled default_demo.
    entries = [
        SyntheticInput(
            param="transient_wind_schedule",
            value=(
                f"wind FROM {float(wind_dir_initial_deg):.0f} deg -> "
                f"{float(wind_dir_shifted_deg):.0f} deg, shift completes at "
                f"{float(shift_fraction) * 100:.0f}% of the {float(duration_hours):.2g} h run "
                f"(constant {float(wind_speed_mph):.0f} mph)"
            ),
            units="meteorological degrees",
            basis="default_demo",
            note="synthetic transient weather schedule (multi-band, interpolated); "
            "NOT a real reanalysis forcing",
        )
    ]
    review = await gate_input_review(
        tool_name="elmfire_transient_wind_schedule_spread",
        mode=input_mode,
        entries=entries,
        params={},
    )
    if review.cancelled:
        return {
            "status": "error",
            "error_code": "USER_INPUT_CANCELLED",
            "error_message": f"elmfire_transient_wind_schedule_spread {review.cancel_reason}",
        }

    try:
        primary = await model_elmfire_transient_wind_schedule(
            wind_speed_mph=float(wind_speed_mph),
            wind_dir_initial_deg=float(wind_dir_initial_deg),
            wind_dir_shifted_deg=float(wind_dir_shifted_deg),
            shift_fraction=float(shift_fraction),
            duration_hours=float(duration_hours),
            cellsize_m=float(cellsize_m),
            domain_km=float(domain_km),
            fuel_model=int(fuel_model),
            fuel_moisture=str(fuel_moisture),
            compute_class=compute_class,
        )
        logger.info(
            "elmfire_transient_wind_schedule_spread complete layer_id=%s "
            "const=%.4g trans=%.4g heading_shift=%.1f uri=%s",
            primary.layer_id,
            primary.summary.get("constant_burned_area_km2", float("nan")),
            primary.summary.get("transient_burned_area_km2", float("nan")),
            primary.summary.get("heading_shift_deg", float("nan")),
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (ElmfireWorkflowError, PostprocessElmfireError, FireSpreadComposerError) as exc:
        logger.warning(
            "elmfire_transient_wind_schedule_spread failed: %s (%s)",
            getattr(exc, "error_code", "?"), exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "FIRE_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("elmfire_transient_wind_schedule_spread unexpected failure")
        return {
            "status": "error",
            "error_code": "FIRE_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_elmfire_transient_wind_schedule(
    *,
    wind_speed_mph: float,
    wind_dir_initial_deg: float,
    wind_dir_shifted_deg: float,
    shift_fraction: float,
    duration_hours: float,
    cellsize_m: float,
    domain_km: float,
    fuel_model: int,
    fuel_moisture: str,
    compute_class: str = "small",
) -> ElmfireSensitivityLayerURI:
    """Compose the constant-vs-transient wind-shift contrast end-to-end."""
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
            logger.warning("transient_wind zoom-to failed: %s", exc)

    begin_substeps(emitter, 3)

    # Two bands: initial direction at t=0, shifted direction reached at
    # shift_fraction*duration; the solver ramps linearly between and holds the
    # shifted band after (ITLO clamps to the last band). Wind SPEED is constant.
    schedule = [
        {"wd": float(wind_dir_initial_deg)},
        {"wd": float(wind_dir_shifted_deg)},
    ]
    dt_meteorology_s = duration_s * float(shift_fraction)

    def _mk_run_args(direction_deg: float) -> ElmfireRunArgs:
        return ElmfireRunArgs(
            bbox=bbox,  # type: ignore[arg-type]
            ignition_lonlat=ignition,  # type: ignore[arg-type]
            wind_speed_mph=float(wind_speed_mph),
            wind_dir_deg=float(direction_deg),
            fuel_moisture=fuel_moisture,  # type: ignore[arg-type]
            duration_hours=float(duration_hours),
            cellsize_m=float(cellsize_m),
        )

    cases: list[SweepCaseResult] = []
    try:
        # Constant run: hold the initial direction, no schedule.
        constant = await solve_constant_case(
            _mk_run_args(wind_dir_initial_deg),
            knob_value=0.0,
            fuel_model=int(fuel_model),
            compute_class=compute_class,
            emitter=emitter,
        )
        _stamp_centroid(constant)
        cases.append(constant)

        # Transient run: the wind-shift schedule (base direction = initial).
        transient = await solve_constant_case(
            _mk_run_args(wind_dir_initial_deg),
            knob_value=1.0,
            fuel_model=int(fuel_model),
            weather_schedule=schedule,
            dt_meteorology_s=dt_meteorology_s,
            compute_class=compute_class,
            emitter=emitter,
        )
        _stamp_centroid(transient)
        cases.append(transient)

        base = await asyncio.to_thread(
            publish_primary_from_out_dir,
            transient, bbox=bbox, duration_s=duration_s, ignition_lonlat=ignition,
        )
    finally:
        cleanup_cases(cases, keep_out_dir=None)

    const_area = float(constant.burned_area_km2)
    trans_area = float(transient.burned_area_km2)
    heading_shift = _burned_centroid_shift_deg(constant, transient)
    sweep = [
        {"x": 0.0, "y": const_area},  # 0 = constant wind
        {"x": 1.0, "y": trans_area},  # 1 = transient wind-shift
    ]
    summary = {
        "constant_burned_area_km2": const_area,
        "transient_burned_area_km2": trans_area,
        "burned_area_delta_km2": trans_area - const_area,
        "heading_shift_deg": float(heading_shift),
        "wind_dir_initial_deg": float(wind_dir_initial_deg),
        "wind_dir_shifted_deg": float(wind_dir_shifted_deg),
        "shift_fraction": float(shift_fraction),
        "fixed_wind_mph": float(wind_speed_mph),
    }

    primary = ElmfireSensitivityLayerURI(
        layer_id=base.layer_id,
        name="Fire arrival time (mid-run wind shift, transient schedule)",
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
        swept_param="wind_regime",
        swept_units="constant|transient",
        response_metric="burned_area_km2",
        response_units="km2",
        sweep=sweep,
        summary=summary,
    )

    await _maybe_emit_chart(emitter, const_area, trans_area, primary.uri)
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("transient_wind authoritative zoom-to failed: %s", exc)

    logger.info(
        "model_elmfire_transient_wind_schedule complete const=%.5f trans=%.5f "
        "heading_shift=%.1f uri=%s",
        const_area, trans_area, heading_shift, primary.uri,
    )
    return primary


def _stamp_centroid(case: SweepCaseResult) -> None:
    """Read the burned-cell centroid (projected metres from ignition) into extras.

    Re-reads the case's time-of-arrival raster; the centroid azimuth is the mean
    spread heading used for the constant-vs-transient redirection metric."""
    import glob

    import numpy as np
    import rasterio

    tifs = sorted(glob.glob(f"{case.out_dir}/outputs/time_of_arrival_*.bil"))
    tifs += sorted(glob.glob(f"{case.out_dir}/time_of_arrival_*.bil"))
    if not tifs:
        return
    with rasterio.open(tifs[-1]) as ds:
        arr = ds.read(1).astype("float64")
        arr[arr == -9999.0] = np.nan
        transform = ds.transform
    ys, xs = np.where(np.isfinite(arr))
    if ys.size == 0:
        return
    ign = case.extras
    # Ignition is at the domain centre; centroid relative to grid centre in metres.
    ny, nx = arr.shape
    dcol = float(xs.mean()) - (nx - 1) / 2.0
    drow = float(ys.mean()) - (ny - 1) / 2.0
    case.extras["cx"] = dcol * float(abs(transform.a))   # east +
    case.extras["cy"] = -drow * float(abs(transform.e))  # north + (row increases south)
    _ = ign


async def _maybe_emit_chart(
    emitter: Any, const_area: float, trans_area: float, source_uri: str
) -> None:
    """Emit the constant-vs-transient burned-area comparison chart."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    spec = build_sweep_chart_spec(
        [{"x": 0.0, "y": float(const_area)}, {"x": 1.0, "y": float(trans_area)}],
        x_title="wind regime (0 = constant, 1 = mid-run shift)",
        y_title="burned area (km2)",
    )
    if spec is None:
        return
    from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Burned area: constant wind vs a mid-run wind shift",
        caption=(
            "Burned area under a constant wind (0) versus a synthetic mid-run "
            "wind-direction shift (1) on the same fire deck; the shift redirects "
            "the spread axis and changes the area the fire reaches."
        ),
        source_layer_uri=source_uri,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("transient_wind chart emit failed: %s", exc)
