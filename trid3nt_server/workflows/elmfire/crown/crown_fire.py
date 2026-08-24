"""Engine template ``elmfire_crown_fire_initiation_threshold_sweep`` - where the
surface-to-crown transition boundary sits, and how the Cruz active-crown spread
rate ceiling bounds fire extent.

A distinct question CLASS from surface fire spread (per the capability-naming
rule): on a canopied deck, ELMFIRE's crown-fire model (CROWN_FIRE_MODEL) escalates
a surface fire into passive (torching) or active crown fire. Two folded knobs
select which crown boundary is swept (0142 fold decision - one template, a
``sweep_variable`` check):

  - ``critical_canopy_cover`` (the INITIATION boundary): sweep CRITICAL_CANOPY_COVER,
    the canopy-cover fraction a cell must exceed for the crown fire to register as
    ACTIVE. While the threshold sits below the deck's canopy cover the burn crowns
    actively; once the threshold rises past it the active crown vanishes and the
    fire drops to a surface fire - the initiation boundary.
  - ``spread_rate_limit`` (the Cruz RATE CEILING): sweep CROWN_FIRE_SPREAD_RATE_LIMIT,
    the cap on the Cruz (2005) active-crown spread rate. A low cap throttles the
    crown run to a small extent; lifting the cap lets the Cruz rate carry the front
    far - the capped-vs-uncapped extent contrast.

Fidelity: a controlled ALL-CONSTANT flat CANOPIED deck (single fuel model, uniform
canopy in ELMFIRE stored units, flat terrain, uniform wind) - NOT a real-landscape
crown fire. Data: NO LANDFIRE/DEM fetch - the deck (incl. the canopy stack) is
authored agent-side as constants.

Determinism boundary (Invariant 1): every narrated number comes from the typed
``ElmfireSensitivityLayerURI`` fields the sweep measured (active-crown area read
off the per-cell DUMP_CROWN_FIRE raster) - never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import math
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
    "trid3nt_server.workflows.elmfire.crown.crown_fire"
)

__all__ = [
    "elmfire_crown_fire_initiation_threshold_sweep",
    "model_elmfire_crown_fire",
]

#: Neutral mid-CONUS deck centre (geography immaterial on a constant flat deck).
_CENTER_LON: float = -98.5
_CENTER_LAT: float = 38.5

#: A crowning fuel bed: SH7 (FBFM code 147), a very-high-load dry-climate shrub
#: whose surface fireline intensity reliably exceeds the canopy critical intensity
#: on this deck, so the crown-fire escalation is exercised (verified live).
_CROWN_FUEL_MODEL_SH7: int = 147

#: The uniform canopy stack in ELMFIRE STORED units (the &INPUTS unit-flag
#: defaults CC_IN_PERCENT / CH_TIMES_10 / CBH_TIMES_10 / CBD_TIMES_100, all TRUE):
#:   cc = 60      -> 60 % canopy cover
#:   ch = 375     -> 37.5 m canopy height   (CH_TIMES_10)
#:   cbh = 10     -> 1.0 m canopy base height (CBH_TIMES_10; low -> easy initiation)
#:   cbd = 18     -> 0.18 kg/m3 canopy bulk density (CBD_TIMES_100)
_CROWN_CANOPY: dict[str, int] = {"cc": 60, "ch": 375, "cbh": 10, "cbd": 18}

#: The canopy-cover FRACTION the stored ``cc`` percent maps to (CC_IN_PERCENT).
_CANOPY_COVER_FRACTION: float = _CROWN_CANOPY["cc"] / 100.0

#: Verification-02 time-control values (the crown-fire verification deck): a tight
#: CFL + fine base dt + a 3-cell level-set band so the crown front is resolved.
_CROWN_TARGET_CFL: float = 0.3
_CROWN_SIMULATION_DT_S: float = 1.0
_CROWN_BANDTHICKNESS: int = 3

#: An "uncapped" Cruz rate ceiling (ft/min) - far above any physical crown rate,
#: so the capped-vs-uncapped contrast isolates the CROWN_FIRE_SPREAD_RATE_LIMIT.
_UNCAPPED_RATE_FTMIN: float = 99999.0


def _validate_canopy_stored_units(canopy: dict[str, int]) -> None:
    """Assert the canopy stack is in sane ELMFIRE stored-unit ranges.

    Raises ``ElmfireWorkflowError`` (never a silently skewed canopy) when cc is
    outside 0..100 percent, or ch/cbh/cbd are non-positive - the canopy-constants
    validation loop (the crown fire needs a real canopy to escalate into)."""
    cc = canopy.get("cc")
    if cc is None or not (0 <= int(cc) <= 100):
        raise ElmfireWorkflowError(
            "ELMFIRE_PARAMS_INVALID",
            f"canopy cc must be 0..100 percent (CC_IN_PERCENT); got {cc!r}",
        )
    for key in ("ch", "cbh", "cbd"):
        v = canopy.get(key)
        if v is None or int(v) <= 0:
            raise ElmfireWorkflowError(
                "ELMFIRE_PARAMS_INVALID",
                f"canopy {key} must be > 0 in ELMFIRE stored units; got {v!r}",
            )


TEMPLATE_CARD = TemplateCard(
    question=(
        "where the surface-to-crown fire transition boundary sits (sweep the "
        "critical canopy cover) and how the Cruz active-crown spread-rate ceiling "
        "bounds fire extent (capped vs uncapped) on a canopied deck"
    ),
    required_inputs=[],
    knobs=(
        "sweep_variable, sweep_min, sweep_max, n_steps, wind_speed_mph, "
        "wind_dir_deg, duration_hours, cellsize_m, domain_km, fuel_model"
    ),
)

_METADATA = AtomicToolMetadata(
    name="elmfire_crown_fire_initiation_threshold_sweep",
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
async def elmfire_crown_fire_initiation_threshold_sweep(
    sweep_variable: str = "critical_canopy_cover",
    sweep_min: float | None = None,
    sweep_max: float | None = None,
    n_steps: int = 3,
    wind_speed_mph: float = 25.0,
    wind_dir_deg: float = 270.0,
    duration_hours: float = 0.5,
    cellsize_m: float = 60.0,
    domain_km: float = 8.0,
    fuel_model: int = _CROWN_FUEL_MODEL_SH7,
    fuel_moisture: str = "dry",
    compute_class: str = "small",
    **_extra_ignored: Any,
) -> ElmfireSensitivityLayerURI | dict[str, Any]:
    """Sweep an ELMFIRE crown-fire boundary on a controlled canopied deck.

    Fidelity: a SENSITIVITY sweep on an ALL-CONSTANT flat CANOPIED deck (single
    shrub/timber fuel model, a uniform canopy in ELMFIRE stored units - cc=60%,
    ch=37.5 m, cbh=1.0 m, cbd=0.18 kg/m3, flat terrain, uniform wind) - NOT a
    real-landscape crown fire. Two folded boundaries, selected by ``sweep_variable``:
    "critical_canopy_cover" sweeps CRITICAL_CANOPY_COVER (the active-crown
    INITIATION threshold - active crown vanishes once it rises past the deck's
    0.60 canopy cover); "spread_rate_limit" sweeps CROWN_FIRE_SPREAD_RATE_LIMIT
    (the Cruz active-crown rate CEILING - a low cap throttles extent, an uncapped
    rate carries the front far).
    Data: NO LANDFIRE/DEM fetch - the deck (incl. canopy) is authored as constants.
    Off-scope: real wildfire spread over LANDFIRE fuels -> elmfire_fire_spread.

    Use this when: the user asks where crown fire initiates / the surface-to-crown
    transition, how sensitive active crown is to canopy cover, or how the crown
    (Cruz) spread-rate cap bounds fire size.

    Params:
        sweep_variable: "critical_canopy_cover" (default, the initiation boundary)
            or "spread_rate_limit" (the Cruz rate ceiling).
        sweep_min: lowest knob value (default 0.30 for critical_canopy_cover,
            80 ft/min for spread_rate_limit).
        sweep_max: highest knob value (default 0.75 for critical_canopy_cover -
            above the deck's 0.60 cover so the boundary is crossed; the uncapped
            sentinel for spread_rate_limit).
        n_steps: number of sweep points (default 3; each is one solve).
        wind_speed_mph: constant wind (ELMFIRE 20 ft convention, default 25).
        wind_dir_deg: direction wind blows FROM, meteorological deg (default 270).
        duration_hours: burn duration per point (default 0.5).
        cellsize_m: computational cell size (default 60).
        domain_km: square domain side length, km (default 8).
        fuel_model: the uniform FBFM fuel-model code (default 147 = SH7 shrub).
        fuel_moisture: "dry" (default), "moderate", or "moist".
        compute_class: compute class (default "small").

    Returns:
        On success: ``ElmfireSensitivityLayerURI`` - the representative run's
        time-of-arrival COG (max-crown for the initiation sweep / uncapped for the
        ceiling sweep), the response ``sweep`` (active-crown-area or burned-area
        vs the knob), and a ``summary``. A response-vs-knob chart is emitted.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    variable = str(sweep_variable).strip().lower()
    if variable not in ("critical_canopy_cover", "spread_rate_limit"):
        return {
            "status": "error",
            "error_code": "FIRE_PARAMS_INVALID",
            "error_message": (
                "sweep_variable must be 'critical_canopy_cover' or "
                f"'spread_rate_limit'; got {sweep_variable!r}"
            ),
        }
    if int(n_steps) < 1:
        return {
            "status": "error",
            "error_code": "FIRE_PARAMS_INVALID",
            "error_message": "n_steps must be >= 1",
        }

    if variable == "critical_canopy_cover":
        lo = 0.30 if sweep_min is None else float(sweep_min)
        hi = 0.75 if sweep_max is None else float(sweep_max)
    else:
        lo = 80.0 if sweep_min is None else float(sweep_min)
        hi = _UNCAPPED_RATE_FTMIN if sweep_max is None else float(sweep_max)
    n = int(n_steps)
    knobs = [hi] if n == 1 else [lo + (hi - lo) * i / (n - 1) for i in range(n)]

    try:
        primary = await model_elmfire_crown_fire(
            sweep_variable=variable,
            knobs=knobs,
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
            "elmfire_crown_fire_initiation_threshold_sweep complete var=%s "
            "layer_id=%s summary=%s uri=%s",
            variable, primary.layer_id, primary.summary, primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (ElmfireWorkflowError, PostprocessElmfireError, FireSpreadComposerError) as exc:
        logger.warning(
            "elmfire_crown_fire_initiation_threshold_sweep failed: %s (%s)",
            getattr(exc, "error_code", "?"), exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "FIRE_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("elmfire_crown_fire_initiation_threshold_sweep unexpected failure")
        return {
            "status": "error",
            "error_code": "FIRE_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_elmfire_crown_fire(
    *,
    sweep_variable: str,
    knobs: list[float],
    wind_speed_mph: float,
    wind_dir_deg: float,
    duration_hours: float,
    cellsize_m: float,
    domain_km: float,
    fuel_model: int,
    fuel_moisture: str,
    compute_class: str = "small",
) -> ElmfireSensitivityLayerURI:
    """Compose the crown-fire boundary sweep end-to-end (canopied constant decks)."""
    _validate_canopy_stored_units(_CROWN_CANOPY)
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
            logger.warning("crown_fire zoom-to failed: %s", exc)

    begin_substeps(emitter, len(knobs) + 1)

    is_initiation = sweep_variable == "critical_canopy_cover"

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
    try:
        for knob in knobs:
            sim_extra = {
                "CROWN_FIRE_MODEL": "1",
                "BANDTHICKNESS": str(_CROWN_BANDTHICKNESS),
            }
            if is_initiation:
                sim_extra["CRITICAL_CANOPY_COVER"] = f"{float(knob):.4f}"
            else:
                sim_extra["CROWN_FIRE_SPREAD_RATE_LIMIT"] = f"{float(knob):.1f}"
            case = await solve_constant_case(
                _mk_run_args(),
                knob_value=float(knob),
                fuel_model=int(fuel_model),
                canopy=dict(_CROWN_CANOPY),
                target_cfl=_CROWN_TARGET_CFL,
                simulator_extra=sim_extra,
                # DUMP_CROWN_FIRE only: the per-cell crown-fire type raster. NOTE
                # DUMP_CROWN_FIRE_AREA segfaults this ELMFIRE build's fire-size-stats
                # postprocess (sub-STOP); the active-crown area is derived
                # from the per-cell raster instead.
                outputs_extra={"DUMP_CROWN_FIRE": ".TRUE."},
                dt_s=_CROWN_SIMULATION_DT_S,
                compute_class=compute_class,
                emitter=emitter,
                measure_crown=True,
            )
            cases.append(case)
            logger.info(
                "crown_fire point %s=%.4g burned_km2=%.4f active_crown_km2=%s",
                sweep_variable, knob, case.burned_area_km2, case.crown_active_area_km2,
            )

        # Representative run: the one showing the MOST crown (initiation: the lowest
        # threshold; ceiling: the highest/uncapped rate). Both = the largest extent.
        rep = max(cases, key=lambda c: c.burned_area_km2)
        base = await asyncio.to_thread(
            publish_primary_from_out_dir,
            rep, bbox=bbox, duration_s=duration_s, ignition_lonlat=ignition,
        )
    finally:
        cleanup_cases(cases, keep_out_dir=None)

    if is_initiation:
        swept_param, swept_units = "critical_canopy_cover", "fraction"
        response_metric, response_units = "active_crown_area_km2", "km2"
        sweep = [
            {"x": c.knob_value, "y": float(c.crown_active_area_km2 or 0.0)}
            for c in sorted(cases, key=lambda c: c.knob_value)
        ]
    else:
        swept_param, swept_units = "crown_fire_spread_rate_limit", "ft/min"
        response_metric, response_units = "burned_area_km2", "km2"
        sweep = [
            {"x": c.knob_value, "y": float(c.burned_area_km2)}
            for c in sorted(cases, key=lambda c: c.knob_value)
        ]

    summary = _crown_summary(is_initiation, cases, wind_speed_mph)

    primary = ElmfireSensitivityLayerURI(
        layer_id=base.layer_id,
        name=(
            "Fire arrival time (crown-fire initiation sweep)"
            if is_initiation
            else "Fire arrival time (crown Cruz-rate ceiling sweep, uncapped)"
        ),
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
        swept_param=swept_param,
        swept_units=swept_units,
        response_metric=response_metric,
        response_units=response_units,
        sweep=sweep,
        summary=summary,
    )

    await _maybe_emit_chart(emitter, is_initiation, sweep, primary.uri)
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("crown_fire authoritative zoom-to failed: %s", exc)

    logger.info(
        "model_elmfire_crown_fire complete var=%s summary=%s uri=%s",
        sweep_variable, summary, primary.uri,
    )
    return primary


def _crown_summary(
    is_initiation: bool, cases: list[Any], wind_speed_mph: float
) -> dict[str, float]:
    """Derive the narratable summary scalars for the crown sweep (pure)."""
    if is_initiation:
        ordered = sorted(cases, key=lambda c: c.knob_value)
        # The initiation boundary = the largest threshold that still crowns
        # actively (crown area above a small floor); above it the crown drops out.
        crowning = [
            c.knob_value for c in ordered
            if (c.crown_active_area_km2 or 0.0) > 1e-6
        ]
        max_crowning_threshold = max(crowning) if crowning else float("nan")
        return {
            "canopy_cover_fraction": _CANOPY_COVER_FRACTION,
            "max_crowning_critical_cover": float(max_crowning_threshold),
            "min_threshold": float(ordered[0].knob_value),
            "max_threshold": float(ordered[-1].knob_value),
            "max_active_crown_area_km2": float(
                max((c.crown_active_area_km2 or 0.0) for c in ordered)
            ),
            "fixed_wind_mph": float(wind_speed_mph),
        }
    ordered = sorted(cases, key=lambda c: c.knob_value)
    capped = ordered[0]
    uncapped = ordered[-1]
    capped_area = float(capped.burned_area_km2)
    uncapped_area = float(uncapped.burned_area_km2)
    return {
        "capped_rate_ftmin": float(capped.knob_value),
        "uncapped_rate_ftmin": float(uncapped.knob_value),
        "capped_burned_area_km2": capped_area,
        "uncapped_burned_area_km2": uncapped_area,
        "extent_ratio_uncapped_to_capped": (
            uncapped_area / capped_area if capped_area > 0 else float("nan")
        ),
        "fixed_wind_mph": float(wind_speed_mph),
    }


async def _maybe_emit_chart(
    emitter: Any, is_initiation: bool, sweep: list[dict[str, float]], source_uri: str
) -> None:
    """Emit the crown response-vs-knob chart."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    if is_initiation:
        spec = build_sweep_chart_spec(
            sweep,
            x_title="critical canopy cover threshold (fraction)",
            y_title="active-crown area (km2)",
            reference_y=_CANOPY_COVER_FRACTION,
            reference_label="Active-crown area vs critical canopy cover",
        )
        title = "Crown-fire initiation boundary vs critical canopy cover"
        caption = (
            "Active-crown area at each critical-canopy-cover threshold: while the "
            "threshold sits below the deck's canopy cover (dotted line) the burn "
            "crowns actively; once it rises past, active crown collapses to zero - "
            "the surface-to-crown initiation boundary."
        )
    else:
        spec = build_sweep_chart_spec(
            sweep,
            x_title="crown (Cruz) spread-rate ceiling (ft/min)",
            y_title="burned area (km2)",
            reference_label="Burned area vs the Cruz active-crown rate ceiling",
        )
        title = "Crown-fire extent vs the Cruz active-crown rate ceiling"
        caption = (
            "Burned area at each CROWN_FIRE_SPREAD_RATE_LIMIT: a low cap throttles "
            "the Cruz active-crown run to a small extent; lifting the cap lets the "
            "Cruz rate carry the front far - the capped-vs-uncapped contrast."
        )
    if spec is None:
        return
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec, title=title, caption=caption, source_layer_uri=source_uri
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("crown_fire chart emit failed: %s", exc)
