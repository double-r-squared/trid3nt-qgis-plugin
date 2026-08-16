"""Engine template ``elmfire_length_to_width_ceiling_sensitivity`` - how the
MAX_LOW length:width cap bounds fire-shape elongation at a fixed wind.

A distinct question CLASS from the elliptical verification (per the capability-
naming rule): not "does the perimeter match the analytical ellipse" but "how
sensitive is the fire's ELONGATION to ELMFIRE's MAX_LOW length:width cap". It is
its OWN registered engine TEMPLATE (engine="elmfire", tier="template").

Holding a fixed wind, the composer sweeps the ``MAX_LOW`` cap across a small
ladder on an ALL-CONSTANT flat grass deck (no LANDFIRE/DEM fetch) and measures the
observed ellipse length:width ratio at each cap from the Richards-ellipse fit.
While the cap sits below the natural (uncapped) elongation the observed ratio
TRACKS the cap; once the cap exceeds the natural elongation the observed ratio
plateaus at the natural value -- where the cap stops binding.

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
    "trid3nt_server.agent.workflows.elmfire.sensitivity.ltw_ceiling.ltw_ceiling"
)

__all__ = [
    "elmfire_length_to_width_ceiling_sensitivity",
    "model_elmfire_ltw_ceiling",
]

#: Neutral mid-CONUS deck centre (geography immaterial on a constant flat grid).
_CENTER_LON: float = -98.5
_CENTER_LAT: float = 38.5

#: The MAX_LOW cap is BINDING at a point when it SUPPRESSES the observed
#: length:width ratio below the natural (largest-cap) elongation by more than
#: this margin -- i.e. the cap, not the fuel/wind, governs the shape there.
_BIND_MARGIN: float = 0.02


TEMPLATE_CARD = TemplateCard(
    question=(
        "how sensitive fire-shape elongation is to ELMFIRE's MAX_LOW length:width "
        "cap at a fixed wind -- sweep the cap and watch the observed "
        "length:width track it until the natural elongation takes over"
    ),
    required_inputs=[],
    knobs=(
        "max_low_min, max_low_max, n_max_low_steps, wind_speed_mph, wind_dir_deg, "
        "duration_hours, cellsize_m, domain_km"
    ),
)

_METADATA = AtomicToolMetadata(
    name="elmfire_length_to_width_ceiling_sensitivity",
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
async def elmfire_length_to_width_ceiling_sensitivity(
    max_low_min: float = 3.0,
    max_low_max: float = 12.0,
    n_max_low_steps: int = 3,
    wind_speed_mph: float = 20.0,
    wind_dir_deg: float = 270.0,
    duration_hours: float = 0.75,
    cellsize_m: float = 45.0,
    domain_km: float = 12.0,
    fuel_model: int = VERIFICATION_FUEL_MODEL_GR2,
    fuel_moisture: str = "dry",
    compute_class: str = "small",
    **_extra_ignored: Any,
) -> ElmfireSensitivityLayerURI | dict[str, Any]:
    """Measure how sensitive fire-shape elongation is to the MAX_LOW length:width cap.

    Fidelity: a SENSITIVITY sweep on a controlled ALL-CONSTANT flat grass deck
    (single fuel model, flat terrain, uniform wind) -- NOT a real-landscape
    fire. ELMFIRE's MAX_LOW (&SIMULATOR, default 8.0) caps the wind-driven
    length:width ratio of the surface spread ellipse. Holding the wind fixed, MAX_LOW is swept: at each cap the observed ellipse length:width ratio is
    measured from the Richards-ellipse fit. While the cap sits below the natural
    (uncapped) elongation the observed ratio TRACKS the cap; once the cap exceeds
    the natural elongation the observed ratio plateaus at the natural value -- the
    cap stops binding.
    Data: NO LANDFIRE/DEM fetch -- the deck is authored agent-side as constants.
    Off-scope: real wildfire spread over LANDFIRE fuels -> elmfire_fire_spread;
    perimeter-vs-ellipse shape verification -> elmfire_verification_elliptical_replication.

    Use this when: the user asks how the MAX_LOW length:width cap affects fire
    elongation, how sensitive fire shape is to the elongation ceiling, or
    where the cap stops binding.

    Params:
        max_low_min: lowest MAX_LOW cap in the ladder (default 3.0).
        max_low_max: highest MAX_LOW cap (default 12.0 -- above the natural
            elongation so the plateau is visible).
        n_max_low_steps: number of caps swept (default 3; each is one solve).
        wind_speed_mph: the FIXED wind held across the cap sweep (ELMFIRE 20 ft convention, default 20).
        wind_dir_deg: direction wind blows FROM, meteorological deg (default 270).
        duration_hours: burn duration per point (short so the ellipse stays inside
            the domain).
        cellsize_m: computational cell size (default 45).
        domain_km: square domain side length, km (default 12).
        fuel_model: the uniform FBFM fuel-model code (default 102 = GR2 grass).
        fuel_moisture: "dry" (default), "moderate", or "moist".
        compute_class: compute class (default "small").

    Returns:
        On success: ``ElmfireSensitivityLayerURI`` -- the time-of-arrival COG of
        the largest-cap (most elongated / natural) run, plus the length:width-vs-
        MAX_LOW ``sweep`` and a ``summary`` (``fixed_wind_mph``, ``natural_ltw``,
        ``max_binding_cap`` -- the largest cap still binding, ``min_cap``,
        ``max_cap``). A length:width-vs-cap chart with the cap-identity diagonal
        is emitted alongside.
        On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if int(n_max_low_steps) < 1:
        return {
            "status": "error",
            "error_code": "FIRE_PARAMS_INVALID",
            "error_message": "n_max_low_steps must be >= 1",
        }
    lo, hi = float(max_low_min), float(max_low_max)
    n = int(n_max_low_steps)
    if n == 1:
        caps = [hi]
    else:
        caps = [lo + (hi - lo) * i / (n - 1) for i in range(n)]

    try:
        primary = await model_elmfire_ltw_ceiling(
            caps=caps,
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
            "elmfire_length_to_width_ceiling_sensitivity complete layer_id=%s "
            "natural_ltw=%.4g max_binding_cap=%.4g uri=%s",
            primary.layer_id,
            primary.summary.get("natural_ltw", float("nan")),
            primary.summary.get("max_binding_cap", float("nan")),
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (ElmfireWorkflowError, PostprocessElmfireError, FireSpreadComposerError) as exc:
        logger.warning(
            "elmfire_length_to_width_ceiling_sensitivity failed: %s (%s)",
            getattr(exc, "error_code", "?"),
            exc,
        )
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "FIRE_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("elmfire_length_to_width_ceiling_sensitivity unexpected failure")
        return {
            "status": "error",
            "error_code": "FIRE_INTERNAL_ERROR",
            "error_message": str(exc),
        }


async def model_elmfire_ltw_ceiling(
    *,
    caps: list[float],
    wind_speed_mph: float,
    wind_dir_deg: float,
    duration_hours: float,
    cellsize_m: float,
    domain_km: float,
    fuel_model: int,
    fuel_moisture: str,
    compute_class: str = "small",
) -> ElmfireSensitivityLayerURI:
    """Compose the MAX_LOW length:width ceiling sweep end-to-end (constant decks)."""
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
            logger.warning("ltw_ceiling zoom-to failed: %s", exc)

    begin_substeps(emitter, len(caps) + 1)

    cases = []
    try:
        for cap in caps:
            run_args = ElmfireRunArgs(
                bbox=bbox,  # type: ignore[arg-type]
                ignition_lonlat=ignition,  # type: ignore[arg-type]
                wind_speed_mph=float(wind_speed_mph),
                wind_dir_deg=float(wind_dir_deg),
                fuel_moisture=fuel_moisture,  # type: ignore[arg-type]
                duration_hours=float(duration_hours),
                cellsize_m=float(cellsize_m),
            )
            case = await solve_constant_case(
                run_args,
                knob_value=float(cap),
                fuel_model=int(fuel_model),
                simulator_extra={"MAX_LOW": f"{float(cap):.4f}"},
                compute_class=compute_class,
                emitter=emitter,
                measure_ltw=True,
                step_label="build_elmfire_deck",
            )
            cases.append(case)
            logger.info(
                "ltw_ceiling point MAX_LOW=%.2f ltw=%.3f corr_class=%s",
                cap, case.length_to_width_ratio or 0.0, case.corr_class,
            )

        # Representative = the largest cap (natural, most elongated) run.
        rep = max(cases, key=lambda c: c.knob_value)
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
        {"x": c.knob_value, "y": float(c.length_to_width_ratio or 0.0)}
        for c in sorted(cases, key=lambda c: c.knob_value)
    ]
    # Natural elongation = the observed L/W at the largest cap (cap not binding).
    natural_ltw = sweep[-1]["y"] if sweep else 0.0
    # A cap binds when it SUPPRESSES the observed L/W below the natural value.
    binding_caps = [
        p["x"] for p in sweep if p["y"] < natural_ltw * (1.0 - _BIND_MARGIN)
    ]
    max_binding_cap = max(binding_caps) if binding_caps else float("nan")
    summary = {
        "fixed_wind_mph": float(wind_speed_mph),
        "natural_ltw": float(natural_ltw),
        "max_binding_cap": float(max_binding_cap),
        "min_cap": float(sweep[0]["x"]) if sweep else float("nan"),
        "max_cap": float(sweep[-1]["x"]) if sweep else float("nan"),
        "n_max_low_steps": float(len(sweep)),
    }

    primary = ElmfireSensitivityLayerURI(
        layer_id=base.layer_id,
        name="Fire arrival time (length:width ceiling sweep, natural cap)",
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
        swept_param="max_low_cap",
        swept_units="ratio",
        response_metric="length_to_width_ratio",
        response_units="ratio",
        sweep=sweep,
        summary=summary,
    )

    await _maybe_emit_chart(emitter, sweep, natural_ltw, primary.uri)
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("ltw_ceiling authoritative zoom-to failed: %s", exc)

    logger.info(
        "model_elmfire_ltw_ceiling complete wind=%.1f natural_ltw=%.3f "
        "max_binding_cap=%.4g uri=%s",
        wind_speed_mph, natural_ltw, max_binding_cap, primary.uri,
    )
    return primary


async def _maybe_emit_chart(
    emitter: Any, sweep: list[dict[str, float]], natural_ltw: float, source_uri: str
) -> None:
    """Emit the length:width-vs-cap chart with the cap-identity diagonal."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    spec = build_sweep_chart_spec(
        sweep,
        x_title="MAX_LOW length:width cap",
        y_title="observed length:width ratio",
        reference_y=float(natural_ltw),
        reference_label="Observed length:width vs MAX_LOW cap",
        identity_diagonal=True,
    )
    if spec is None:
        return
    from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Fire elongation sensitivity to the MAX_LOW length:width cap",
        caption=(
            "The observed ellipse length:width ratio tracks the MAX_LOW cap "
            "(dashed diagonal) while the cap sits below the natural elongation "
            "(dotted line), then plateaus once the cap exceeds it -- where the "
            "cap stops binding."
        ),
        source_layer_uri=source_uri,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ltw_ceiling chart emit failed: %s", exc)
