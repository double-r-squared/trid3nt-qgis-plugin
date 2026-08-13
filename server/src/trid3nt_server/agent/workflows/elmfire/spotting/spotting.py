"""Engine template ``elmfire_spot_fire_barrier_crossing`` - does wind-driven ember
spotting carry a wildfire ACROSS a fuel break the contiguous front cannot cross?

A distinct question CLASS from surface spread and crown fire (per the capability-
naming rule): the headline is a barrier-JUMP - the cleanest spotting discriminant.
The template runs the SAME controlled deck TWICE on an all-constant flat grass bed
carrying a single NON-BURNABLE strip (a fuel break, NB1/FBFM 91) downwind of the
ignition, spanning the full cross-wind extent so the contiguous surface fire has NO
path around it:

  - spotting OFF (baseline): the head fire slams into the break and STOPS - zero
    burned area on the far (downwind) side.
  - spotting ON: lofted embers clear the break and ignite spot fires in the grass
    beyond it, so the far side burns - the fire JUMPED the break.

The physics-asserted showcase: far-side burned area is ~0 with spotting OFF and
strictly > 0 with spotting ON, both measured off the time-of-arrival raster
(Invariant 1) - the fire crosses the break ONLY because of ember spotting.

ELMFIRE SPOTTING KNOB TRAP (verified against the baked binary,
``elmfire_spotting.f90::SET_SPOTTING_PARAMETERS``): whenever ``ENABLE_SPOTTING`` the
solver OVERWRITES the scalar spotting knobs from their ``_MIN/_MAX/_LO/_HI`` bounds,
and surface-fire spotting stays OFF unless ``GLOBAL_SURFACE_FIRE_SPOTTING_PERCENT``
(default 0) is raised. So this template sets the BOUNDS (MIN==MAX for a deterministic
run), never the bare scalars. The folded spotting parameters - mean spotting distance
(the lognormal-distance model), critical spotting fireline intensity (the generation
gate), ember count and landing ignition probability - ride as this template's knobs.

Fidelity: an ALL-CONSTANT flat grass deck with a synthetic fuel break + uniform wind
- NOT a real-landscape spotting event. Data: NO LANDFIRE/DEM fetch - the deck is
authored agent-side as constants.
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

from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.elmfire._template_card import TemplateCard
from trid3nt_server.agent.workflows.elmfire.fire_spread.fire_spread import (
    FireSpreadComposerError,
)
from trid3nt_server.agent.workflows.elmfire.postprocess_elmfire import (
    PostprocessElmfireError,
)
from trid3nt_server.agent.workflows.elmfire.run_elmfire import (
    ElmfireWorkflowError,
)
from trid3nt_server.agent.workflows.elmfire.sensitivity._sensitivity_common import (
    cleanup_cases,
    publish_primary_from_out_dir,
    solve_constant_case,
)
from trid3nt_server.emission.pipeline_emitter import begin_substeps, current_emitter

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.elmfire.spotting.spotting"
)

__all__ = [
    "elmfire_spot_fire_barrier_crossing",
    "model_elmfire_spot_fire_barrier_crossing",
]

#: Deck centre - CA Sierra-foothill WUI fire country (geography immaterial on a
#: constant deck; a natural US fire-prone locale per the US-only-cases norm).
_CENTER_LON: float = -120.5
_CENTER_LAT: float = 38.8

#: A dry-grass surface fuel bed: GR2 (FBFM 102) - a fast, high-intensity head fire
#: that reliably launches embers on this deck.
_GRASS_FUEL_MODEL_GR2: int = 102

#: Fuel-break geometry (FIDELITY facts, LOUD labelled defaults - NOT user knobs):
#: a vertical NON-BURNABLE strip spanning the full N-S extent, placed ~1/3 across
#: the E-W domain so a strong head fire builds before hitting it. Width ~8 cells so
#: the contiguous level-set front cannot leak across, but embers loft over it. The
#: ignition sits ~10% in from the west so the head fire runs downwind into the break.
_BREAK_LO_FRAC: float = 0.33
_BREAK_HI_FRAC: float = 0.35
_IGN_FRAC_X: float = 0.10

#: A rectangular domain: long E-W (down-wind spotting runway) x short N-S.
_DOMAIN_KM_X: float = 12.0
_DOMAIN_KM_Y: float = 1.5

#: Lognormal spotting-distance shape defaults (baked-binary parametrization): the
#: normalized distance variance + the wind/intensity power-law exponents. LOUD
#: labelled defaults the caller may override are the DISTANCE and generation knobs.
_NORMALIZED_SPOTTING_DIST_VARIANCE: float = 250.0
_SPOT_FLIN_EXP: float = 0.3
_SPOT_WS_EXP: float = 0.7

#: The 0:303 fuel-model index span of ELMFIRE's per-fuel spotting arrays (used to
#: broadcast a scalar critical-intensity gate across every fuel model via the
#: namelist ``N*value`` repeat form).
_FBFM_ARRAY_LEN: int = 304

#: Far-side burned area (km2) below which the barrier counts as NOT jumped - a small
#: floor absorbing at most a couple of edge cells, well under any real spot-fire.
_JUMP_FLOOR_KM2: float = 1.0e-3


def _spotting_namelist(
    *,
    mean_spotting_distance_m: float,
    nembers: int,
    pign_pct: float,
    critical_spotting_intensity_kwm: float,
) -> dict[str, str]:
    """Build the deterministic ``&SPOTTING`` namelist dict (MIN==MAX bounds).

    The baked binary's ``SET_SPOTTING_PARAMETERS`` resolves each knob to
    ``_MIN + R1*(_MAX - _MIN)``, so MIN==MAX pins a deterministic value regardless of
    the internal draw; ``GLOBAL_SURFACE_FIRE_SPOTTING_PERCENT`` must be raised or
    surface-fire spotting never launches. NEMBERS rides ``NEMBERS_MIN`` +
    ``NEMBERS_MAX_LO/HI``; PIGN the ``PIGN_MIN/MAX`` bounds."""
    extra: dict[str, str] = {
        "ENABLE_SPOTTING": ".TRUE.",
        "ENABLE_SURFACE_FIRE_SPOTTING": ".TRUE.",
        "USE_SUPERSEDED_SPOTTING": ".TRUE.",
        "SPOTTING_DISTRIBUTION_TYPE": "'LOGNORMAL'",
        "GLOBAL_SURFACE_FIRE_SPOTTING_PERCENT_MIN": "100.0000",
        "GLOBAL_SURFACE_FIRE_SPOTTING_PERCENT_MAX": "100.0000",
        "MEAN_SPOTTING_DIST_MIN": f"{float(mean_spotting_distance_m):.4f}",
        "MEAN_SPOTTING_DIST_MAX": f"{float(mean_spotting_distance_m):.4f}",
        "NORMALIZED_SPOTTING_DIST_VARIANCE_MIN": f"{_NORMALIZED_SPOTTING_DIST_VARIANCE:.4f}",
        "NORMALIZED_SPOTTING_DIST_VARIANCE_MAX": f"{_NORMALIZED_SPOTTING_DIST_VARIANCE:.4f}",
        "SPOT_FLIN_EXP_LO": f"{_SPOT_FLIN_EXP:.4f}",
        "SPOT_FLIN_EXP_HI": f"{_SPOT_FLIN_EXP:.4f}",
        "SPOT_WS_EXP_LO": f"{_SPOT_WS_EXP:.4f}",
        "SPOT_WS_EXP_HI": f"{_SPOT_WS_EXP:.4f}",
        "NEMBERS_MIN": f"{int(nembers):d}",
        "NEMBERS_MAX_LO": f"{int(nembers):d}",
        "NEMBERS_MAX_HI": f"{int(nembers):d}",
        "PIGN_MIN": f"{float(pign_pct):.4f}",
        "PIGN_MAX": f"{float(pign_pct):.4f}",
    }
    # CRITICAL_SPOTTING_FIRELINE_INTENSITY is a per-fuel (0:303) array; broadcast a
    # scalar gate across every fuel model with the namelist repeat form when > 0
    # (default 0 stays absent -> the binary default of 0, spotting always generates).
    if float(critical_spotting_intensity_kwm) > 0.0:
        extra["CRITICAL_SPOTTING_FIRELINE_INTENSITY"] = (
            f"{_FBFM_ARRAY_LEN}*{float(critical_spotting_intensity_kwm):.4f}"
        )
    return extra


TEMPLATE_CARD = TemplateCard(
    question=(
        "whether wind-driven ember spotting carries a wildfire across a fuel break "
        "the contiguous front cannot cross (spotting OFF vs ON on a grass deck with a "
        "non-burnable strip), and how far downwind spot fires ignite"
    ),
    required_inputs=[],
    knobs=(
        "mean_spotting_distance_m, critical_spotting_intensity_kwm, nembers, pign_pct, "
        "wind_speed_mph, wind_dir_deg, duration_hours, cellsize_m, fuel_model"
    ),
)

_METADATA = AtomicToolMetadata(
    name="elmfire_spot_fire_barrier_crossing",
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
async def elmfire_spot_fire_barrier_crossing(
    mean_spotting_distance_m: float = 25.0,
    critical_spotting_intensity_kwm: float = 0.0,
    nembers: int = 20,
    pign_pct: float = 100.0,
    wind_speed_mph: float = 25.0,
    wind_dir_deg: float = 270.0,
    duration_hours: float = 4.0,
    cellsize_m: float = 30.0,
    fuel_model: int = _GRASS_FUEL_MODEL_GR2,
    fuel_moisture: str = "dry",
    compute_class: str = "small",
    **_extra_ignored: Any,
) -> ElmfireSensitivityLayerURI | dict[str, Any]:
    """Does ember spotting make a wildfire JUMP a fuel break? (ELMFIRE, controlled deck)

    Fidelity: an ALL-CONSTANT flat dry-grass deck (single fuel model, flat terrain,
    uniform wind) carrying ONE synthetic NON-BURNABLE fuel break (NB1) across the
    full cross-wind extent, run TWICE - spotting OFF then ON. NOT a real-landscape
    spotting event. Data: NO LANDFIRE/DEM fetch - the deck is authored as constants.
    Off-scope: real wildfire spread over LANDFIRE fuels -> elmfire_fire_spread; the
    surface-to-crown transition -> elmfire_crown_fire_initiation_threshold_sweep.

    Use this when: the user asks whether a fire can jump a road / river / fuel break
    / firebreak via embers, how ember spotting spreads fire past a barrier, how far
    ahead of the front spot fires ignite, or how the spotting distance / ember count /
    ignition probability / generation-intensity threshold change spot-fire crossing.

    Params:
        mean_spotting_distance_m: mean lognormal spotting distance knob (the
            MEAN_SPOTTING_DIST distance-model scale; default 25 - larger throws embers
            farther downwind).
        critical_spotting_intensity_kwm: fireline-intensity gate below which no embers
            generate (kW/m; default 0 = generate from every burning cell; raise to
            suppress spotting from low-intensity backing fire).
        nembers: embers cast per torching cell (default 20).
        pign_pct: probability (percent) a landed ember ignites a spot fire (default
            100 - deterministic ignition; lower thins spot-fire proliferation).
        wind_speed_mph: constant wind, ELMFIRE 20 ft mph convention (default 25).
        wind_dir_deg: direction the wind blows FROM, meteorological deg (default 270 =
            from the west, driving the head fire east into the break).
        duration_hours: burn duration (default 4).
        cellsize_m: computational cell size (default 30 = LANDFIRE native).
        fuel_model: the uniform burnable FBFM code (default 102 = GR2 grass).
        fuel_moisture: "dry" (default), "moderate", or "moist".
        compute_class: solver compute class (default "small").

    Returns:
        On success: ``ElmfireSensitivityLayerURI`` - the spotting-ON time-of-arrival
        COG (showing the far-side spot fire beyond the break), a two-point ``sweep``
        (spotting OFF vs ON far-side burned area), and a ``summary`` carrying the
        physics-asserted barrier-jump scalars. An OFF-vs-ON comparison chart is
        emitted. On failure: ``{"status": "error", "error_code", "error_message"}``.
        Not cached (``cacheable=False``).
    """
    if int(nembers) < 1:
        return _err("FIRE_PARAMS_INVALID", "nembers must be >= 1")
    if not (0.0 < float(pign_pct) <= 100.0):
        return _err("FIRE_PARAMS_INVALID", "pign_pct must be in (0, 100]")
    if float(mean_spotting_distance_m) <= 0.0:
        return _err("FIRE_PARAMS_INVALID", "mean_spotting_distance_m must be > 0")
    if float(critical_spotting_intensity_kwm) < 0.0:
        return _err("FIRE_PARAMS_INVALID", "critical_spotting_intensity_kwm must be >= 0")

    try:
        primary = await model_elmfire_spot_fire_barrier_crossing(
            mean_spotting_distance_m=float(mean_spotting_distance_m),
            critical_spotting_intensity_kwm=float(critical_spotting_intensity_kwm),
            nembers=int(nembers),
            pign_pct=float(pign_pct),
            wind_speed_mph=float(wind_speed_mph),
            wind_dir_deg=float(wind_dir_deg),
            duration_hours=float(duration_hours),
            cellsize_m=float(cellsize_m),
            fuel_model=int(fuel_model),
            fuel_moisture=str(fuel_moisture),
            compute_class=compute_class,
        )
        logger.info(
            "elmfire_spot_fire_barrier_crossing complete layer_id=%s summary=%s uri=%s",
            primary.layer_id, primary.summary, primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (ElmfireWorkflowError, PostprocessElmfireError, FireSpreadComposerError) as exc:
        logger.warning(
            "elmfire_spot_fire_barrier_crossing failed: %s (%s)",
            getattr(exc, "error_code", "?"), exc,
        )
        return _err(getattr(exc, "error_code", "FIRE_INTERNAL_ERROR"), str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("elmfire_spot_fire_barrier_crossing unexpected failure")
        return _err("FIRE_INTERNAL_ERROR", str(exc))


def _err(code: str, msg: str) -> dict[str, Any]:
    return {"status": "error", "error_code": code, "error_message": msg}


async def model_elmfire_spot_fire_barrier_crossing(
    *,
    mean_spotting_distance_m: float,
    critical_spotting_intensity_kwm: float,
    nembers: int,
    pign_pct: float,
    wind_speed_mph: float,
    wind_dir_deg: float,
    duration_hours: float,
    cellsize_m: float,
    fuel_model: int,
    fuel_moisture: str,
    compute_class: str = "small",
) -> ElmfireSensitivityLayerURI:
    """Compose the spotting barrier-jump OFF-vs-ON discriminant end-to-end."""
    emitter = current_emitter()
    duration_s = float(duration_hours) * 3600.0

    half_deg_lat = (_DOMAIN_KM_Y * 1000.0 / 2.0) / 111_320.0
    half_deg_lon = (_DOMAIN_KM_X * 1000.0 / 2.0) / (
        111_320.0 * max(math.cos(math.radians(_CENTER_LAT)), 1e-6)
    )
    bbox = (
        _CENTER_LON - half_deg_lon, _CENTER_LAT - half_deg_lat,
        _CENTER_LON + half_deg_lon, _CENTER_LAT + half_deg_lat,
    )
    # Ignition ~10% in from the west edge, on the centre-line.
    ign_lon = _CENTER_LON - half_deg_lon + _IGN_FRAC_X * (2.0 * half_deg_lon)
    ignition = (ign_lon, _CENTER_LAT)
    fuel_break = {
        "axis": "x",
        "lo_frac": _BREAK_LO_FRAC,
        "hi_frac": _BREAK_HI_FRAC,
        "fuel_model": 91,
    }

    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("spotting zoom-to failed: %s", exc)

    begin_substeps(emitter, 3)

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

    spotting_extra = _spotting_namelist(
        mean_spotting_distance_m=mean_spotting_distance_m,
        nembers=nembers,
        pign_pct=pign_pct,
        critical_spotting_intensity_kwm=critical_spotting_intensity_kwm,
    )

    cases = []
    try:
        # Case 0: spotting OFF (baseline) - the break stops the contiguous front.
        off = await solve_constant_case(
            _mk_run_args(), knob_value=0.0, fuel_model=int(fuel_model),
            fuel_break=fuel_break, spotting_extra=None,
            compute_class=compute_class, emitter=emitter,
            step_label="build_elmfire_deck_off",
        )
        cases.append(off)
        # Case 1: spotting ON - embers loft over the break and ignite the far side.
        on = await solve_constant_case(
            _mk_run_args(), knob_value=1.0, fuel_model=int(fuel_model),
            fuel_break=fuel_break, spotting_extra=spotting_extra,
            compute_class=compute_class, emitter=emitter,
            step_label="build_elmfire_deck_on",
        )
        cases.append(on)

        off_east = float(off.extras.get("east_of_break_km2", 0.0))
        on_east = float(on.extras.get("east_of_break_km2", 0.0))
        logger.info(
            "spotting barrier-jump: OFF east=%.4f km2  ON east=%.4f km2",
            off_east, on_east,
        )
        # PHYSICS ASSERTION (honesty floor): the fire must cross ONLY with spotting.
        if not (off_east <= _JUMP_FLOOR_KM2 < on_east):
            raise FireSpreadComposerError(
                "ELMFIRE_SPOTTING_NO_DISCRIMINANT",
                "spotting barrier-jump not demonstrated: expected far-side burned "
                f"area ~0 with spotting OFF (got {off_east:.4f} km2) and > "
                f"{_JUMP_FLOOR_KM2} km2 with spotting ON (got {on_east:.4f} km2). "
                "The break may be leaking, or the embers not clearing it.",
            )

        base = await asyncio.to_thread(
            publish_primary_from_out_dir,
            on, bbox=bbox, duration_s=duration_s, ignition_lonlat=ignition,
        )
    finally:
        cleanup_cases(cases, keep_out_dir=None)

    sweep = [
        {"x": 0.0, "y": off_east},  # spotting OFF
        {"x": 1.0, "y": on_east},   # spotting ON
    ]
    summary = {
        "far_side_area_spotting_off_km2": off_east,
        "far_side_area_spotting_on_km2": on_east,
        "head_fire_area_km2": float(on.extras.get("west_of_break_km2", 0.0)),
        "far_side_spot_cells_on": float(on.extras.get("east_of_break_cells", 0.0)),
        "mean_spotting_distance_m": float(mean_spotting_distance_m),
        "nembers": float(int(nembers)),
        "pign_pct": float(pign_pct),
        "critical_spotting_intensity_kwm": float(critical_spotting_intensity_kwm),
        "fixed_wind_mph": float(wind_speed_mph),
        "break_jumped": 1.0,  # asserted above
    }

    primary = ElmfireSensitivityLayerURI(
        layer_id=base.layer_id,
        name="Fire arrival time (ember spotting across a fuel break)",
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
        swept_param="spotting_enabled",
        swept_units="off(0)/on(1)",
        response_metric="far_side_burned_area_km2",
        response_units="km2",
        sweep=sweep,
        summary=summary,
    )

    await _maybe_emit_chart(emitter, off_east, on_east, primary.uri)
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("spotting authoritative zoom-to failed: %s", exc)

    logger.info(
        "model_elmfire_spot_fire_barrier_crossing complete summary=%s uri=%s",
        summary, primary.uri,
    )
    return primary


async def _maybe_emit_chart(
    emitter: Any, off_east: float, on_east: float, source_uri: str
) -> None:
    """Emit the OFF-vs-ON far-side-burned-area comparison bar chart."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "data": {"values": [
            {"case": "spotting OFF", "area": off_east},
            {"case": "spotting ON", "area": on_east},
        ]},
        "mark": {"type": "bar", "color": "#d1495b"},
        "encoding": {
            "x": {"field": "case", "type": "nominal", "title": None,
                  "sort": ["spotting OFF", "spotting ON"]},
            "y": {"field": "area", "type": "quantitative",
                  "title": "burned area beyond the break (km2)"},
            "tooltip": [
                {"field": "case", "type": "nominal"},
                {"field": "area", "type": "quantitative", "format": ".3g"},
            ],
        },
        "title": "Does the fire jump the break? Far-side burned area, spotting OFF vs ON",
    }
    from trid3nt_server.agent.tools.processing.charts_common import build_chart_payload

    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Ember spotting across a fuel break",
        caption=(
            "Burned area on the FAR (downwind) side of a non-burnable fuel break. "
            "With spotting OFF the contiguous head fire stops at the break (~0 far-side "
            "area); with spotting ON lofted embers clear the break and ignite spot "
            "fires beyond it - the fire jumps the break ONLY because of ember spotting."
        ),
        source_layer_uri=source_uri,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("spotting chart emit failed: %s", exc)
