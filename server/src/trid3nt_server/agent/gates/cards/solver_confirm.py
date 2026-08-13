"""Solver-confirm / granularity / fetch-resolution confirm-card builders.

Pure (no websocket, no session state) envelope + suggestion builders for the
solver and heavy-fetch confirm gates. The transport-coupled gate orchestration
(``_gate_on_solver_confirm`` etc.) stays in ``server``.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Any

from trid3nt_contracts import new_ulid

logger = logging.getLogger("trid3nt_server.agent.gates.cards.solver_confirm")


#: hard px-grid ceiling for the fetch-resolution gate. A fine
#: rung on a huge AOI would materialize an enormous raster; finest_allowed_m is
#: floored at max(ladder_floor, max(width_m, height_m) / MAX_FETCH_PX) so the
#: finest selectable rung keeps the grid bounded to ~8192 px on the long axis.
MAX_FETCH_PX: int = 8192


# Per-fetcher resolution ladders for the fetch-resolution gate. Finer =
# smaller metres. fetch_dem can go to 1 m (3DEP); fetch_topobathy floors at
# 3 m (CUDEM tiles). Both default to 10 m (the tools' resolution_m default).
# fetch_landcover: NLCD native is 30 m; for large bboxes the gate coarsens
# to 60/120/300/600 m so the MRLC WCS GetCoverage stays under 4000 px per
# axis. fetch_dem's 90/300/900 m rungs exist because a state-scale AOI (e.g.
# WA-state) needs ~150 m to stay under the tool's own 4000 px/axis budget
# (data_fetch.py's _DEM_PIXEL_BUDGET_PX) -- without them the ladder-filtered
# choices collapse to just the computed finest_allowed_m with no coarser
# alternative, same as fetch_landcover's coarse rungs give for NLCD.
_FETCH_RES_LADDERS: dict[str, list[float]] = {
    "fetch_dem": [1.0, 3.0, 10.0, 30.0, 90.0, 300.0, 900.0],
    "fetch_topobathy": [3.0, 10.0, 30.0],
    "fetch_landcover": [30.0, 60.0, 120.0, 300.0, 600.0],
}
_FETCH_DEFAULT_RES_M: float = 10.0
# fetch_landcover: native NLCD resolution doubles as the tool's resolution_m
# default (there is no finer rung, so the coarse default IS the native grid).
_LANDCOVER_DEFAULT_RES_M: float = 30.0
# Per-tool px-grid ceiling override for the fetch-resolution gate. The MRLC WCS
# server rejects/times-out GetCoverage beyond ~4096 px per axis, so the
# fetch_landcover card must bound its finest selectable rung to 4000 px (margin)
# rather than the generic MAX_FETCH_PX -- otherwise the card would offer a rung
# the tool cannot deliver (it clamps to 4000 px and would silently coarsen).
# fetch_dem (2026-07-10): the tool itself now auto-coarsens against a 4000
# px/axis budget (data_fetch.py's _DEM_PIXEL_BUDGET_PX) -- kept identical here
# so the card's suggested rung matches what fetch_dem will actually deliver
# (an honest suggestion instead of a stale 30 m that the tool would silently
# coarsen past).
_FETCH_MAX_PX_BY_TOOL: dict[str, int] = {
    "fetch_landcover": 4000,
    "fetch_dem": 4000,
}


def _local_compute_lane() -> bool:
    """True when solves run on the LOCAL machine (the TRID3NT local build).

    Local-cloud fingerprint seam: the canonical deployment
    signal is the solver dispatch backend -- ``TRID3NT_SOLVER_BACKEND=
    local-docker`` (``tools.simulation.solver.solver_backend()``), which the local build
    pins and the cloud stack never sets. Used ONLY to localize user-visible
    confirm-card wording (compute labels / "cloud solve" prose); it never
    changes dispatch. Cloud wording stays byte-identical when this is False.
    """
    from ...tools.simulation.solver.solver import SOLVER_BACKEND_LOCAL_DOCKER, solver_backend

    return solver_backend() == SOLVER_BACKEND_LOCAL_DOCKER


def _clamp_fetch_resolution(chosen_m: float, finest_allowed_m: float) -> float:
    """Floor a user-chosen fetch resolution UP to the finest allowed cell size.

    Finer = SMALLER metres, so the px-grid bound is a LOWER bound on the rung: a
    request finer than ``finest_allowed_m`` (e.g. 1 m on a continent-scale AOI)
    is clamped UP to ``finest_allowed_m`` so the materialized grid stays under
    ``MAX_FETCH_PX`` on the long axis. A coarser request is honoured exactly.
    """
    return max(float(chosen_m), float(finest_allowed_m))


async def _build_fetch_resolution_envelope(
    tool_name: str, params: dict
) -> tuple[Any, Any]:
    """Build the fetch-resolution confirm card for ``fetch_dem`` / ``fetch_topobathy``.

    the granularity gate widened to the two heavy raster
    fetchers so the user controls the download/merge resolution before the big
    fetch (memory: feedback_user_controlled_granularity). Modeled on
    :func:`_build_swmm_granularity_envelope` but PURE arithmetic (no DEM read /
    network): coerce the bbox, compute the bbox extent in metres, build the
    per-fetcher ladder, and floor the finest selectable rung so a fine rung on a
    huge AOI stays bounded to ``MAX_FETCH_PX`` px on the long axis.

    Returns ``(envelope, fetch_suggestion)`` where ``fetch_suggestion`` is a
    small namespace the decision tail reads (``coarse_default_m`` for proceed,
    ``finest_allowed_m`` for the narrow_scope clamp, ``cap``). Raises on a
    missing/invalid bbox so the caller's try/except fails OPEN (the fetch runs
    with its own resolution_m default rather than being blocked by a gate error).
    """
    from trid3nt_contracts.payload_warning import (
        GranularitySuggestion,
        PayloadWarningEnvelopePayload,
    )
    from types import SimpleNamespace

    from ...tool_arg_normalizer import coerce_bbox_value
    from ...tools.fetchers.imagery._pc_stac import bbox_pixel_dims

    coerced = coerce_bbox_value(params.get("bbox"))
    if coerced is None or len(coerced) != 4:
        # No usable bbox: let the fetcher raise its own typed params error.
        raise ValueError(f"{tool_name} gate: bbox missing/invalid")
    bbox = (float(coerced[0]), float(coerced[1]),
            float(coerced[2]), float(coerced[3]))

    ladder = _FETCH_RES_LADDERS.get(tool_name, [3.0, 10.0, 30.0])
    ladder_floor = min(ladder)
    coarse_default = (
        _LANDCOVER_DEFAULT_RES_M
        if tool_name == "fetch_landcover"
        else _FETCH_DEFAULT_RES_M
    )
    # Per-tool px ceiling (MRLC WCS caps ~4096/axis for fetch_landcover).
    max_fetch_px = _FETCH_MAX_PX_BY_TOOL.get(tool_name, MAX_FETCH_PX)

    # The user's requested rung (the base the readout describes). Defaults to the
    # fetcher's resolution_m default so an absent value matches the fetch.
    try:
        requested = float(params.get("resolution_m", coarse_default))
    except (TypeError, ValueError):
        requested = coarse_default

    # bbox extent in metres (approx, mid-latitude) -> the finest selectable rung.
    # A fine rung on a huge AOI would materialize an enormous raster; floor the
    # finest allowed cell size at the long-axis extent / MAX_FETCH_PX so the grid
    # stays bounded. Coarser than the ladder floor never gets finer than allowed.
    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    m_per_deg_lon = 111_320.0 * max(0.05, math.cos(math.radians(mid_lat)))
    width_m = max(0.0, max_lon - min_lon) * m_per_deg_lon
    height_m = max(0.0, max_lat - min_lat) * 111_320.0
    long_axis_m = max(width_m, height_m)
    finest_allowed_m = max(ladder_floor, long_axis_m / float(max_fetch_px))

    # The default-selected rung: the coarse default when it clears the bound,
    # else the finest allowed (so the card never pre-selects an unselectable rung
    # on a continent-scale AOI where even the ladder floor would blow the grid).
    suggested = (
        coarse_default
        if coarse_default >= finest_allowed_m - 1e-9
        else finest_allowed_m
    )

    # The selectable ladder = rungs at/above finest_allowed_m (a fine rung on a
    # huge AOI is dropped), plus the user's requested rung (if it clears the
    # bound) AND the suggested rung (always selectable), ascending. Always keep
    # at least one rung (the suggested fallback) so the card is never empty.
    candidate = sorted(
        {r for r in ladder if r >= finest_allowed_m - 1e-9}
        | ({requested} if requested >= finest_allowed_m - 1e-9 else set())
        | {suggested}
    )
    resolution_choices = [float(r) for r in candidate if r > 0]

    # px-grid estimate at the SUGGESTED rung (pure arithmetic, no read). px_max
    # raised to the tool's px ceiling so a large-AOI estimate is not clamped at
    # the default 4096; estimated_active_cells = width_px * height_px.
    width_px, height_px = bbox_pixel_dims(
        bbox, suggested, px_min=1, px_max=max_fetch_px
    )
    px_estimate = int(width_px) * int(height_px)

    finest_bounded = finest_allowed_m > ladder_floor + 1e-9
    reason = (
        f"{tool_name} at ~{suggested:.0f} m over a "
        f"{long_axis_m / 1000.0:.1f} km AOI (~{px_estimate} px grid). "
        + (
            f"A finer rung is bounded to {finest_allowed_m:.0f} m to keep the "
            f"grid under {max_fetch_px} px. "
            if finest_bounded
            else ""
        )
        + "Pick a finer or coarser resolution, or confirm."
    )[:512]

    _ENGINE_BY_TOOL = {
        "fetch_dem": "dem",
        "fetch_topobathy": "topobathy",
        "fetch_landcover": "landcover",
    }
    engine = _ENGINE_BY_TOOL.get(tool_name, "topobathy")
    # Local-cloud fingerprint fix: the local build must not
    # surface the cloud "fetch (1 vCPU)" compute label on the confirm card --
    # the fetch runs in-process on the local machine, so the compute label is
    # "local" (the QGIS-plugin/web cards render local wording off it). The
    # cloud lane keeps the exact prior values byte-for-byte.
    fetch_compute_class = "local" if _local_compute_lane() else "fetch"
    granularity = GranularitySuggestion(
        engine=engine,
        resolution_param="resolution_m",
        suggested_resolution_m=float(suggested),
        resolution_choices=resolution_choices,
        estimated_active_cells=int(px_estimate),
        estimated_solve_seconds=0.0,
        vcpus=1,
        compute_class=fetch_compute_class,
        cell_cap=int(max_fetch_px) ** 2,
        coarsened=False,
        reason=reason,
        spot_label=None,
    )

    envelope = PayloadWarningEnvelopePayload(
        warning_id=new_ulid(),
        tool_name=tool_name,
        tool_args={
            "bbox": list(bbox),
            "resolution_m": float(suggested),
        },
        estimated_mb=0.0,
        threshold_mb=0.0,
        recommendation=reason,
        options=["proceed", "cancel", "narrow_scope"],
        granularity=granularity,
    )
    fetch_suggestion = SimpleNamespace(
        coarse_default_m=float(suggested),
        finest_allowed_m=float(finest_allowed_m),
        cap=int(max_fetch_px) ** 2,
    )
    return envelope, fetch_suggestion


async def _build_flood_run_settings_envelope(
    tool_name: str, params: dict
) -> tuple[Any, Any, float | None, float]:
    """Build the COMBINED run-settings confirm card for the flood solvers.

    The combined run-settings gate extends the flood solver-confirm
    gate into ONE card the user reviews + overrides before the heavy SFINCS run,
    carrying BOTH:

    * a ``GranularitySuggestion`` (SPATIAL resolution -- the SFINCS
      ``grid_resolution_m`` ladder + estimated cells / solve time / compute
      class), built from the bbox via
      :func:`suggest_sfincs_resolution_from_bbox` (no DEM read -- loop-safe; the
      real cell count comes from ``build_sfincs_model``'s DEM autoscale at run
      time, so the card numbers are labelled ESTIMATES), and
    * a ``TimeScaleSuggestion`` (TEMPORAL cadence + window -- the resolved
      animation ``output_interval_min`` + ``duration_hr`` + a frame-count
      estimate) for a COASTAL/wave run (the "looks like rain" fix). PLUVIAL
      runs animate hourly with a fixed cadence, so ``time_scale`` is None and
      the card degrades to the granularity-only resolution gate.

    Returns ``(envelope, granularity_suggestion, resolved_interval_min,
    duration_hr)`` -- the granularity result is the raw ``GridAutoscaleResult``
    so the decision tail can pin the suggested resolution on ``proceed``;
    ``resolved_interval_min`` is the resolved coastal cadence (None for pluvial)
    pinned on ``proceed``; ``duration_hr`` is the simulation window.

    Raises on ANY failure -- the caller's try/except fails OPEN (proceeds with
    the original params) so a gate problem never blocks or orphans a solve.
    """
    from trid3nt_contracts.payload_warning import (
        GranularitySuggestion,
        PayloadWarningEnvelopePayload,
        TimeScaleSuggestion,
    )
    from ...tool_arg_normalizer import coerce_bbox_value
    from ...workflows.sfincs.flood.flood import (
        _estimate_frame_count,
        _resolve_output_interval_min,
    )
    from ...workflows.shared.frames import MAX_FLOOD_FRAMES
    from ...workflows.sfincs.sfincs_builder import (
        SFINCS_RES_LADDER,
        suggest_sfincs_resolution_from_bbox,
    )

    where = params.get("location_query") or params.get("bbox") or "?"

    # is_coastal mirrors the workflow signal (coastal/quadtree/surge -> fine
    # minute-scale animation; pluvial -> hourly).
    flood_is_coastal = bool(
        params.get("coastal")
        or params.get("quadtree")
        or params.get("surge_forcing")
    )
    try:
        flood_duration_hr = float(
            params.get("duration_hr")
            if params.get("duration_hr") is not None
            else params.get("duration_hours", 24)
        )
    except (TypeError, ValueError):
        flood_duration_hr = 24.0
    if flood_duration_hr <= 0:
        flood_duration_hr = 24.0

    # --- TIME-SCALE suggestion (coastal only) -----------------------------
    resolved_interval_min = _resolve_output_interval_min(
        is_coastal=flood_is_coastal,
        output_interval_min=params.get("output_interval_min"),
        duration_hr=flood_duration_hr,
    )
    frame_count = _estimate_frame_count(
        output_interval_min=resolved_interval_min,
        duration_hr=flood_duration_hr,
    )
    time_scale: Any = None
    if resolved_interval_min is not None:
        # Coastal: a fine minute-scale cadence the user can override. The chip
        # ladder is a small set of sensible strides; free-edit is also allowed.
        interval_choices = sorted(
            {1.0, 2.0, 5.0, 10.0, 15.0, 30.0, 60.0, float(resolved_interval_min)}
        )
        time_scale = TimeScaleSuggestion(
            cadence_param="output_interval_min",
            suggested_interval_min=float(resolved_interval_min),
            interval_choices=[float(c) for c in interval_choices if c > 0],
            duration_param="duration_hr",
            suggested_duration_hr=float(flood_duration_hr),
            estimated_frame_count=int(frame_count),
            max_frames=int(MAX_FLOOD_FRAMES),
            min_interval_min=1.0,
            is_coastal=True,
            reason=(
                f"Coastal/wave: ~{resolved_interval_min:g}-min frames over a "
                f"{flood_duration_hr:g} h window animate the water roll-in "
                f"(~{frame_count} frames)."
            )[:512],
        )

    # --- GRANULARITY (spatial resolution) suggestion ----------------------
    granularity: Any = None
    auto: Any = None
    coerced = coerce_bbox_value(params.get("bbox"))
    if coerced is not None:
        # suggest is PURE arithmetic (no DEM read) -> safe on the loop, but keep
        # it off-thread for symmetry with the SWMM path / no-blocking norm.
        auto = await asyncio.to_thread(
            suggest_sfincs_resolution_from_bbox,
            tuple(coerced),  # type: ignore[arg-type]
        )
        compute_class = params.get("compute_class", "standard") or "standard"
        card_vcpus = int(auto.vcpus)
        if _local_compute_lane():
            # Local-cloud fingerprint fix: the local build
            # solves on the local machine, so the card's compute descriptors
            # are the local lane's ("local" + host CPU count -- mirrors the
            # SWMM builder's local lane). Cloud keeps the exact prior values.
            compute_class = "local"
            card_vcpus = os.cpu_count() or 1
        rungs = sorted(
            {r for r in SFINCS_RES_LADDER if r > 0}
            | {float(auto.grid_resolution_m)}
        )
        granularity = GranularitySuggestion(
            engine="sfincs",
            resolution_param="grid_resolution_m",
            suggested_resolution_m=float(auto.grid_resolution_m),
            resolution_choices=[float(r) for r in rungs if r > 0],
            estimated_active_cells=int(auto.estimated_active_cells),
            estimated_solve_seconds=float(auto.estimated_solve_seconds),
            vcpus=card_vcpus,
            compute_class=str(compute_class),
            cell_cap=int(auto.cell_cap),
            coarsened=bool(auto.coarsened),
            reason=str(auto.reason)[:512],
            spot_label=None,
        )

    # --- recommendation prose (the card's caption) ------------------------
    if resolved_interval_min is not None:
        cadence_phrase = (
            f" Animation: ~{frame_count} frames every "
            f"{resolved_interval_min:g} min (fine wave cadence)."
        )
    else:
        cadence_phrase = f" Animation: ~{frame_count} hourly frames."
    res_phrase = ""
    if auto is not None:
        res_phrase = (
            f" Grid ~{auto.grid_resolution_m:.0f} m "
            f"(~{auto.estimated_active_cells} active cells est)."
        )

    envelope = PayloadWarningEnvelopePayload(
        warning_id=new_ulid(),
        tool_name=tool_name,
        tool_args={
            "location": str(where),
            "return_period_yr": params.get("return_period_yr"),
            "duration_hr": flood_duration_hr,
            "forcing_raster_uri": params.get("forcing_raster_uri"),
            "compute_class": params.get("compute_class", "standard"),
            "grid_resolution_m": (
                float(auto.grid_resolution_m) if auto is not None else None
            ),
            # cadence lever  -  visible + overridable in the card.
            "output_interval_min": resolved_interval_min,
            "animation_frames": frame_count,
        },
        estimated_mb=0.0,
        threshold_mb=0.0,
        recommendation=(
            f"Run a SFINCS flood simulation for {where} "
            # Local-cloud fingerprint fix: local builds run
            # the solve on this machine -- never say "cloud solve" there. The
            # cloud phrase is byte-identical to the prior wording.
            + (
                "(local solve)."
                if _local_compute_lane()
                else "(cloud solve, typically 5-20 minutes)."
            )
            + res_phrase
            + cadence_phrase
            + " Review the run settings, then confirm to start."
        )[:512],
        # narrow_scope is meaningful here whenever ANY override (resolution or
        # cadence/window) is offered, i.e. whenever the card carries a
        # granularity OR a time_scale block.
        options=(
            ["proceed", "cancel", "narrow_scope"]
            if (granularity is not None or time_scale is not None)
            else ["proceed", "cancel"]
        ),
        granularity=granularity,
        time_scale=time_scale,
    )
    return envelope, auto, resolved_interval_min, flood_duration_hr


def _clamp_swmm_resolution_to_cap(
    chosen_res_m: float, auto: Any, requested_res_m: float
) -> tuple[float, bool]:
    """HARD-CLAMP a user-chosen SWMM resolution so the cell count cannot exceed
    ``auto.cell_cap`` (narrow_scope path).

    A finer (smaller) resolution multiplies the active-cell count by
    ``(base/chosen)**2``; the cell count must stay <= ``cell_cap``. We invert the
    estimate to find the FINEST resolution whose cell count fits the cap and clamp
    the chosen value UP to it when the user picked finer. A coarser-than-suggested
    choice is always honoured (fewer cells). Returns ``(clamped_res_m, clamped)``.
    """
    chosen = float(chosen_res_m)
    if chosen <= 0:
        chosen = float(requested_res_m) if requested_res_m > 0 else float(auto.resolution_m)
    base_cells = int(auto.estimated_active_cells_at_base)
    base_res = float(auto.base_resolution_m)
    cap = int(auto.cell_cap)
    if base_cells <= 0 or base_res <= 0 or cap <= 0:
        return chosen, False
    # cells(res) = base_cells * (base_res/res)**2 <= cap
    #   => res >= base_res * sqrt(base_cells / cap)
    import math

    min_res = base_res * math.sqrt(base_cells / float(cap))
    if chosen < min_res:
        return float(min_res), True
    return chosen, False


def _build_psha_confirm_envelope(params: dict) -> Any:
    """build the OpenQuake classical-PSHA solver-confirm card.

    A simple proceed/cancel confirmation (no granularity/resolution picker): the
    deck lays a single area source over the WHOLE bbox/AOI, so there is no
    rupture/incident-area user input to gate (that is scenario mode, not built).
    The card summarizes the run (approximate AOI area, IMT, PoE -> return period)
    so the user confirms the consequential Batch solve (Invariant 9). Built inline
    from the tool args (no composer extraction -- the run args ARE the args).
    """
    import math

    from trid3nt_contracts.payload_warning import PayloadWarningEnvelopePayload
    from ...tool_arg_normalizer import coerce_bbox_value

    imt = str(params.get("imt", "PGA"))
    try:
        poe = float(params.get("poe", 0.10))
    except (TypeError, ValueError):
        poe = 0.10
    try:
        inv_time = float(params.get("investigation_time_years", 50.0))
    except (TypeError, ValueError):
        inv_time = 50.0

    # Return period implied by the PoE over the investigation time:
    # RP = -investigation_time / ln(1 - poe). 10%/50yr -> ~475 yr.
    return_period_years: float | None = None
    if 0.0 < poe < 1.0 and inv_time > 0.0:
        try:
            return_period_years = -inv_time / math.log(1.0 - poe)
        except (ValueError, ZeroDivisionError):
            return_period_years = None

    # Approximate AOI area (km^2) from the bbox via a cosine-latitude correction
    # (~111.32 km/deg). Best-effort: None when the bbox is missing/malformed.
    bbox_area_km2: float | None = None
    coerced = coerce_bbox_value(params.get("bbox"))
    if coerced is not None and len(coerced) == 4:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in coerced)
        mid_lat = (min_lat + max_lat) / 2.0
        width_km = abs(max_lon - min_lon) * 111.32 * math.cos(math.radians(mid_lat))
        height_km = abs(max_lat - min_lat) * 111.32
        bbox_area_km2 = max(0.0, width_km * height_km)

    area_phrase = (
        f"~{bbox_area_km2:,.0f} km^2 AOI" if bbox_area_km2 is not None
        else "the requested AOI"
    )
    rp_phrase = (
        f" (~{return_period_years:,.0f}-year return period)"
        if return_period_years is not None
        else ""
    )
    # Local-cloud fingerprint fix: the local build runs the
    # OpenQuake engine on this machine -- never say "AWS Batch"/"cloud solve"
    # there. The cloud phrase is byte-identical to the prior wording.
    dispatch_phrase = (
        "This runs the OpenQuake engine locally (typically several minutes)."
        if _local_compute_lane()
        else (
            "This dispatches the OpenQuake engine to AWS Batch (a cloud "
            "solve, typically several minutes)."
        )
    )
    recommendation = (
        f"Run a classical probabilistic seismic-hazard (PSHA) calculation over "
        f"{area_phrase}: intensity measure {imt} at a {poe:g} probability of "
        f"exceedance in {inv_time:g} years{rp_phrase}. {dispatch_phrase} "
        f"Confirm to start."
    )[:512]

    return PayloadWarningEnvelopePayload(
        warning_id=new_ulid(),
        tool_name="openquake_psha",
        tool_args={
            "bbox": list(coerced) if coerced is not None else params.get("bbox"),
            "imt": imt,
            "poe": poe,
            "investigation_time_years": inv_time,
            "return_period_years": (
                round(return_period_years) if return_period_years is not None
                else None
            ),
            "aoi_area_km2": (
                round(bbox_area_km2) if bbox_area_km2 is not None else None
            ),
            "gmpe": params.get("gmpe", "BooreAtkinson2008"),
        },
        estimated_mb=0.0,
        threshold_mb=0.0,
        recommendation=recommendation,
        options=["proceed", "cancel"],
    )


def _build_scenario_confirm_envelope(params: dict, tool_name: str) -> Any:
    """build the OpenQuake scenario-GMF / secondary-perils solver-confirm card.

    A simple proceed/cancel confirmation (Invariant 9 - a consequential engine
    run). Summarizes the scenario magnitude + approximate AOI area; the
    secondary-perils variant names the two ground-failure screens it produces.
    Built inline from the tool args (the run args ARE the args).
    """
    import math

    from trid3nt_contracts.payload_warning import PayloadWarningEnvelopePayload
    from ...tool_arg_normalizer import coerce_bbox_value

    try:
        magnitude = float(params.get("magnitude", 6.7))
    except (TypeError, ValueError):
        magnitude = 6.7

    bbox_area_km2: float | None = None
    coerced = coerce_bbox_value(params.get("bbox"))
    if coerced is not None and len(coerced) == 4:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in coerced)
        mid_lat = (min_lat + max_lat) / 2.0
        width_km = abs(max_lon - min_lon) * 111.32 * math.cos(math.radians(mid_lat))
        height_km = abs(max_lat - min_lat) * 111.32
        bbox_area_km2 = max(0.0, width_km * height_km)
    area_phrase = (
        f"~{bbox_area_km2:,.0f} km^2 AOI" if bbox_area_km2 is not None
        else "the requested AOI"
    )
    lane_phrase = (
        "This runs the OpenQuake engine locally (typically seconds)."
        if _local_compute_lane()
        else "This dispatches the OpenQuake engine to AWS Batch (typically minutes)."
    )
    if tool_name == "openquake_secondary_perils":
        recommendation = (
            f"Screen earthquake-triggered LIQUEFACTION + LANDSLIDE ground failure "
            f"for a magnitude {magnitude:g} scenario rupture over {area_phrase}: "
            f"runs the scenario ground-motion field then the openquake.sep "
            f"liquefaction + Newmark-landslide models over fetched terrain. "
            f"{lane_phrase} Confirm to start."
        )[:512]
    else:
        recommendation = (
            f"Run a magnitude {magnitude:g} scenario ground-motion field over "
            f"{area_phrase}: correlated realizations of one rupture -> the mean "
            f"shaking map + across-realization spread. {lane_phrase} Confirm to "
            f"start."
        )[:512]

    return PayloadWarningEnvelopePayload(
        warning_id=new_ulid(),
        tool_name=tool_name,
        tool_args={
            "bbox": list(coerced) if coerced is not None else params.get("bbox"),
            "magnitude": magnitude,
            "aoi_area_km2": (
                round(bbox_area_km2) if bbox_area_km2 is not None else None
            ),
            "gsim": params.get("gsim", "BooreAtkinson2008"),
        },
        estimated_mb=0.0,
        threshold_mb=0.0,
        recommendation=recommendation,
        options=["proceed", "cancel"],
    )


def _build_fire_confirm_envelope(params: dict) -> Any:
    """build the ELMFIRE fire-spread solver-confirm card.

    A simple proceed/cancel confirmation (mirrors ``openquake_psha``):
    PURE arithmetic from the call args -- the approximate computational grid
    (``estimate_elmfire_grid``, cosine-latitude cell count) + the
    -calibrated runtime heuristic (``estimate_elmfire_runtime_s``) + the
    scenario weather (wind, fuel-moisture preset expanded to its m1/m10/m100
    percentages, duration). No fetch, no rasterio -- safe to build inline. The
    ignition-required rule is NOT enforced here: a missing ignition falls
    through to the tool's own typed ``FIRE_IGNITION_REQUIRED`` error (the gate
    must never mask parameter problems, matching the extraction-failure
    fall-through below).
    """
    from trid3nt_contracts.elmfire_contracts import FUEL_MOISTURE_PRESETS
    from trid3nt_contracts.payload_warning import PayloadWarningEnvelopePayload
    from ...tool_arg_normalizer import coerce_bbox_value
    from ...workflows.elmfire.run_elmfire import (
        estimate_elmfire_grid,
        estimate_elmfire_runtime_s,
    )

    coerced = coerce_bbox_value(params.get("bbox"))
    try:
        cellsize_m = float(params.get("cellsize_m", 30.0))
    except (TypeError, ValueError):
        cellsize_m = 30.0
    try:
        duration_hours = float(params.get("duration_hours", 6.0))
    except (TypeError, ValueError):
        duration_hours = 6.0
    try:
        wind_speed_mph = float(params.get("wind_speed_mph", 15.0))
    except (TypeError, ValueError):
        wind_speed_mph = 15.0
    try:
        wind_dir_deg = float(params.get("wind_dir_deg", 0.0))
    except (TypeError, ValueError):
        wind_dir_deg = 0.0
    preset = str(params.get("fuel_moisture", "dry")).strip().lower()
    moisture = FUEL_MOISTURE_PRESETS.get(preset)

    n_cells: int | None = None
    est_runtime_s: float | None = None
    if coerced is not None and len(coerced) == 4:
        _nx, _ny, n_cells = estimate_elmfire_grid(coerced, cellsize_m)
        est_runtime_s = estimate_elmfire_runtime_s(
            n_cells, duration_hours * 3600.0
        )

    cells_phrase = (
        f"~{n_cells:,} cells at {cellsize_m:g} m" if n_cells is not None
        else "the requested AOI"
    )
    runtime_phrase = (
        f"; estimated solver time ~{est_runtime_s:,.0f} s"
        if est_runtime_s is not None
        else ""
    )
    moisture_phrase = (
        f"{preset} fuels"
        + (
            f" (1h/10h/100h = {moisture['m1_pct']:g}/{moisture['m10_pct']:g}/"
            f"{moisture['m100_pct']:g}%)"
            if moisture
            else ""
        )
    )
    recommendation = (
        f"Run an ELMFIRE wildfire-spread simulation over {cells_phrase}: "
        f"{duration_hours:g} h burn, wind {wind_speed_mph:g} mph from "
        f"{wind_dir_deg:g} deg, {moisture_phrase}{runtime_phrase}. This "
        f"fetches LANDFIRE fuels + terrain and dispatches the ELMFIRE solver. "
        f"Confirm to start."
    )[:512]

    return PayloadWarningEnvelopePayload(
        warning_id=new_ulid(),
        tool_name="elmfire_fire_spread",
        tool_args={
            "bbox": list(coerced) if coerced is not None else params.get("bbox"),
            "ignition_lonlat": params.get("ignition_lonlat"),
            "wind_speed_mph": wind_speed_mph,
            "wind_dir_deg": wind_dir_deg,
            "fuel_moisture": preset,
            "fuel_moisture_pct": moisture,
            "duration_hours": duration_hours,
            "cellsize_m": cellsize_m,
            "estimated_cells": n_cells,
            "estimated_runtime_s": (
                round(est_runtime_s) if est_runtime_s is not None else None
            ),
        },
        estimated_mb=0.0,
        threshold_mb=0.0,
        recommendation=recommendation,
        options=["proceed", "cancel"],
    )


def _build_geoclaw_confirm_envelope(params: dict) -> Any:
    """build the GeoClaw inundation solver-confirm card.

    A simple proceed/cancel confirmation (mirrors ``openquake_psha`` /
    ``elmfire_fire_spread``): PURE arithmetic from the call args -- the
    approximate AOI area (cosine-latitude correction) + the scenario
    (dam_break / tsunami / surge) + the simulated window + AMR levels. No
    fetch, no rasterio, so it is safe to build inline. GeoClaw's adaptive mesh
    means there is no single fixed cell count to advertise (unlike the SWMM /
    TELEMAC granularity gates), so this is a plain proceed/cancel: ``amr_levels``
    / ``sim_duration_s`` are explicit tool args the LLM can restate. A missing
    bbox is NOT enforced here -- it falls through to the tool's own typed
    ``GEOCLAW_PARAMS_INCOMPLETE`` error after approval (the gate must never mask
    parameter problems, matching the fire/psha fall-through).
    """
    import math

    from trid3nt_contracts.payload_warning import PayloadWarningEnvelopePayload
    from ...tool_arg_normalizer import coerce_bbox_value

    scenario = str(params.get("scenario", "dam_break")).strip().lower() or "dam_break"
    try:
        sim_duration_s = float(params.get("sim_duration_s", 3600.0))
    except (TypeError, ValueError):
        sim_duration_s = 3600.0
    try:
        amr_levels = int(params.get("amr_levels", 2))
    except (TypeError, ValueError):
        amr_levels = 2

    # Approximate AOI area (km^2) from the bbox via a cosine-latitude correction
    # (~111.32 km/deg). Best-effort: None when the bbox is missing/malformed.
    coerced = coerce_bbox_value(params.get("bbox"))
    bbox_area_km2: float | None = None
    if coerced is not None and len(coerced) == 4:
        min_lon, min_lat, max_lon, max_lat = (float(v) for v in coerced)
        mid_lat = (min_lat + max_lat) / 2.0
        width_km = abs(max_lon - min_lon) * 111.32 * math.cos(math.radians(mid_lat))
        height_km = abs(max_lat - min_lat) * 111.32
        bbox_area_km2 = max(0.0, width_km * height_km)

    area_phrase = (
        f"~{bbox_area_km2:,.0f} km^2 AOI" if bbox_area_km2 is not None
        else "the requested AOI"
    )
    sim_minutes = sim_duration_s / 60.0
    window_phrase = (
        f"~{sim_minutes:,.0f} min simulated" if sim_minutes >= 1.0
        else f"{sim_duration_s:g} s simulated"
    )
    # Local-cloud fingerprint fix (mirrors the psha card): the local build runs
    # the GeoClaw engine on this machine -- never say "AWS Batch"/"cloud solve"
    # there.
    dispatch_phrase = (
        "This runs the GeoClaw engine locally (typically several minutes)."
        if _local_compute_lane()
        else (
            "This dispatches the GeoClaw engine to AWS Batch (a cloud solve, "
            "typically several minutes)."
        )
    )
    recommendation = (
        f"Run a GeoClaw {scenario} shallow-water inundation simulation over "
        f"{area_phrase}: {window_phrase}, {amr_levels} AMR level(s). "
        f"{dispatch_phrase} Confirm to start."
    )[:512]

    return PayloadWarningEnvelopePayload(
        warning_id=new_ulid(),
        tool_name="geoclaw_inundation",
        tool_args={
            "bbox": list(coerced) if coerced is not None else params.get("bbox"),
            "scenario": scenario,
            "sim_duration_s": sim_duration_s,
            "amr_levels": amr_levels,
            "aoi_area_km2": (
                round(bbox_area_km2) if bbox_area_km2 is not None else None
            ),
        },
        estimated_mb=0.0,
        threshold_mb=0.0,
        recommendation=recommendation,
        options=["proceed", "cancel"],
    )


def _gate_memory_key(tool_name: str, params: dict[str, Any]) -> tuple[str, str]:
    """Turn-memory key for the solver-confirm / fetch-resolution gate.

    Fix (bbox-gate-retry-loop, 2026-07-09): keys on ``(tool_name, bbox)`` -
    a bbox rounded to ~6 decimal degrees (~0.1 m; matches the quantization
    granularity the fetch tools already use for cache-key stability) - so a
    retry of the SAME tool over the SAME AOI with a corrected non-bbox arg
    (e.g. a typed-error retry that fixes ``dataset``) reuses the earlier
    proceed/narrow_scope decision instead of re-gating. When the call
    carries no ``bbox`` arg, falls back to keying on the FULL normalized
    args dict (order-independent JSON), so any arg change still gates
    fresh - this is the conservative default for gated tools without a
    bbox-shaped AOI (e.g. the groundwater-contamination composers).
    """
    bbox = params.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        try:
            bbox_key = tuple(round(float(v), 6) for v in bbox)
            return (tool_name, repr(bbox_key))
        except (TypeError, ValueError):
            pass
    import json as _json

    try:
        normalized = _json.dumps(params, sort_keys=True, default=str)
    except TypeError:
        normalized = repr(sorted(params.items(), key=lambda kv: kv[0]))
    return (tool_name, normalized)


async def _build_telemac_mesh_envelope(
    params: dict, emitter: Any = None
) -> tuple[Any, dict]:
    """Build the approve-mesh confirm card for ``telemac_river_dye``.

    The heavy-work-in-builder license comes from the SWMM builder below
    (it fetches + reads the actual DEM there): this builder goes one further
    and runs the FAST mesh-only worker (``preview_telemac_mesh``: gmsh mesh,
    no DEM, no solve, ~10-25 s) so the card shows the REAL mesh - the
    triangle wireframe lands on the map as a role="input" vector layer with a
    zoom-to BEFORE the card appears, and every number on the card (node /
    element count, CFL-coupled dt, conservative solve-time estimate) comes
    from that actual mesh, not an area model.

    Returns ``(envelope, preview_stats)`` - the stats dict is what the
    decision tail pins into the approved params so the solve reproduces the
    previewed mesh exactly.

    Raises on ANY failure (geocode, river fetch, worker, metrics) - the
    caller's try/except fails OPEN so a preview problem never blocks the tool
    (which then raises its own typed errors on the same underlying fault).
    """
    from trid3nt_contracts.payload_warning import (
        GranularitySuggestion,
        PayloadWarningEnvelopePayload,
    )
    from ...workflows.telemac.river_dye.river_dye import (
        MESH_H_FLOOR_M,
        MESH_NODE_CAP,
        plausible_release_coords,
        preview_telemac_mesh,
    )

    stats = await preview_telemac_mesh(params, emitter=emitter)
    h = float(stats["mesh_size_m"])
    npoin = int(stats["npoin"])
    nelem = int(stats["nelem"])
    dt = float(stats["time_step_s"])
    est_s = float(stats["est_solve_seconds"])
    where = stats.get("location_name") or params.get("location") or "?"
    # bank-source provenance (leg 1) shown on the approve-mesh card so the user
    # sees whether the previewed banks are real (nhd_area) or an assumed ribbon.
    _bank_source = str(stats.get("bank_source") or "constant_ribbon")
    _bank_note = (
        " Banks: real NHDArea geometry."
        if _bank_source == "nhd_area"
        else " Banks: assumed constant-width ribbon."
    )

    # Release-coverage guard: when the CALL carried plausible release coords
    # the preview seeds the reach from them, so the built mesh should
    # contain the release point. If it STILL does not (coords off any
    # flowline, snap landed on a different water body), say so ON THE CARD -
    # never silently mesh elsewhere. The tri-state pin below lets the
    # decision tail tell call-provided coords (seed the reach) apart from
    # the gate-picked click (source only).
    _rel = plausible_release_coords(
        params.get("release_lon"), params.get("release_lat")
    )
    stats["release_seeds_reach"] = _rel is not None
    # decouple: keep the EXACT pair the preview seeded the reach from so
    # the decision tail can thread it as separate seed keys - the click
    # overwrites release_lon/release_lat, and re-seeding from the click would
    # silently mesh a DIFFERENT reach than the one the user approved.
    stats["release_seed_pair"] = list(_rel) if _rel is not None else None
    release_note = ""
    _mesh_bbox = list(stats.get("bbox") or [])
    if _rel is not None and len(_mesh_bbox) == 4:
        _rlon, _rlat = _rel
        if not (
            float(_mesh_bbox[0]) <= _rlon <= float(_mesh_bbox[2])
            and float(_mesh_bbox[1]) <= _rlat <= float(_mesh_bbox[3])
        ):
            release_note = (
                f" WARNING: the requested release point ({_rlon:.4f}, "
                f"{_rlat:.4f}) is OUTSIDE this mesh - the meshed reach may be "
                "the wrong water body. Cancel and name the river / a closer "
                "place, or click a release point inside this mesh."
            )
            logger.warning(
                "telemac approve-mesh: requested release point (%.5f, %.5f) "
                "is outside the previewed mesh bbox %s",
                _rlon, _rlat, _mesh_bbox,
            )

    # Resolution ladder around the suggested edge length (finer halves, coarser
    # doubles), floored at the gmsh-quality minimum. The client recomputes the
    # cell/ETA readout per rung from the suggested-ratio model; the REAL numbers
    # for the suggested rung are the ones below.
    rungs = sorted({
        max(round(h * f, 1), MESH_H_FLOOR_M) for f in (0.5, 0.75, 1.0, 1.5, 2.0)
    })
    granularity = GranularitySuggestion(
        engine="telemac",
        resolution_param="mesh_resolution_m",
        suggested_resolution_m=h,
        resolution_choices=[float(r) for r in rungs],
        estimated_active_cells=npoin,
        estimated_solve_seconds=est_s,
        vcpus=os.cpu_count() or 1,
        compute_class="local",
        cell_cap=int(MESH_NODE_CAP),
        coarsened="clamped" in str(stats.get("resolution_label") or ""),
        reason=(
            f"Mesh previewed on the map: {npoin:,} nodes / {nelem:,} triangles "
            f"at {h:g} m edges; CFL-coupled timestep {dt:g} s." + _bank_note
        )[:512],
        spot_label=None,
    )
    envelope = PayloadWarningEnvelopePayload(
        warning_id=new_ulid(),
        tool_name="telemac_river_dye",
        tool_args={
            "location": str(where),
            "mesh_resolution_m": h,
            "nodes": npoin,
            "elements": nelem,
            "time_step_s": dt,
            "compute_class": "local",
            # the client card enters "Select release point" mode - the
            # user clicks the map inside the mesh; Continue stays greyed until
            # a point exists; the point rides back in revised_args.
            "release_point_required": True,
            "mesh_bbox": list(stats.get("bbox") or []),
            "bank_source": _bank_source,
        },
        estimated_mb=0.0,
        threshold_mb=0.0,
        # The release-coverage warning is APPENDED and must survive the 512
        # cap - the base prose is trimmed first, never the warning.
        recommendation=(
            (
                f"The river mesh for {where} is previewed on the map "
                f"({npoin:,} nodes at {h:g} m edges, dt {dt:g} s; est solve "
                f"~{est_s / 60.0:.0f} min). Click the map INSIDE the mesh to "
                "place the spill release point (click again to move it), then "
                "Continue. You can also pick a finer/coarser mesh, or cancel."
            )[: 512 - len(release_note)]
            + release_note
        ),
        options=["proceed", "cancel", "narrow_scope"],
        granularity=granularity,
    )
    return envelope, stats


async def _build_swmm_granularity_envelope(params: dict) -> tuple[Any, Any, str]:
    """Build the granularity confirm card for ``swmm_urban_flood``.

    Mirrors the SWMM tool / composer DEM-resolution path EXACTLY so the suggested
    resolution + active-cell count the user SEES is what the build would compute:
    coerce + floor the bbox (``_enforce_min_urban_aoi``), fetch the DEM
    (``_fetch_dem_for_urban``: 3DEP 1m -> fetch_dem 10m), then call
    ``suggest_swmm_resolution`` (DEM read + active-cell count + autoscale only).
    The synchronous DEM read + suggest is OFFLOADED via ``asyncio.to_thread`` so
    the WS heartbeat is never starved (memory: no-sync-blocking-on-asyncio-loop).

    Returns ``(envelope, autoscale_result, local_dem_path)`` -- the
    ``PayloadWarningEnvelopePayload`` carrying the ``GranularitySuggestion``
    block, the raw ``SWMMAutoscaleResult``, and the localized DEM path the
    decision tail needs for the REAL-grid cap-clamp on a ``narrow_scope``
    override (``clamp_swmm_resolution_to_real_cap`` re-probes this same DEM so the
    clamp matches the count :func:`build_swmm_mesh` will produce).

    Raises on ANY failure (DEM fetch, read, suggest) -- the caller's try/except
    fails OPEN (proceeds with the original params) so a gate problem never blocks
    or orphans a solve.
    """
    from trid3nt_contracts.payload_warning import (
        GranularitySuggestion,
        PayloadWarningEnvelopePayload,
    )
    from trid3nt_contracts.swmm_contracts import SWMMRunArgs
    from ...tool_arg_normalizer import coerce_bbox_value
    from ...tools.simulation.solver.solver import (
        AWS_BATCH_COMPUTE_CLASS_SIZING,
        select_compute_class,
    )
    from ...workflows.swmm.urban_flood.urban_flood import (
        _enforce_min_urban_aoi,
        _fetch_dem_for_urban,
    )
    from ...workflows.swmm.run_swmm import is_local_mode
    from ...mesh.raster_cell_mesh import (
        SWMM_RES_LADDER,
        estimate_swmm_solve_seconds,
        suggest_swmm_resolution,
    )

    # The user's requested resolution rung (the base the ladder snaps UP from).
    # Defaults to the SWMMRunArgs / tool default so an absent value matches build.
    try:
        requested_res = float(
            params.get("target_resolution_m")
            if params.get("target_resolution_m") is not None
            else SWMMRunArgs.model_fields["target_resolution_m"].default
        )
    except (TypeError, ValueError):
        requested_res = 10.0

    coerced = coerce_bbox_value(params.get("bbox"))
    if coerced is None:
        # No usable bbox: let the tool raise its own typed SWMM_PARAMS error.
        raise ValueError("swmm_urban_flood gate: bbox missing/invalid")
    bbox = _enforce_min_urban_aoi(tuple(coerced))  # type: ignore[arg-type]

    # DEM fetch + read + suggest are SYNCHRONOUS compute (network + rasterio +
    # numpy); offload the whole thing off the event loop. The resolved local DEM
    # path is surfaced so a narrow_scope override can re-probe the SAME DEM for
    # the REAL-grid cap-clamp (the build re-reads this DEM at the chosen rung).
    def _resolve_and_suggest() -> tuple[Any, str]:
        local_dem_path, _src = _fetch_dem_for_urban(bbox)
        return suggest_swmm_resolution(local_dem_path, requested_res), local_dem_path

    auto, dem_path = await asyncio.to_thread(_resolve_and_suggest)

    # The selectable ladder = the SWMM rungs at/above the floor of the ladder,
    # plus the user's requested rung, ascending. Keep every rung > 0.
    rungs = sorted({r for r in SWMM_RES_LADDER if r > 0} | {requested_res})
    resolution_choices = [float(r) for r in rungs if r > 0]

    # Off-box (Batch) lane sizes a Spot compute_class from the active-cell count;
    # the in-process LOCAL lane has no Spot label. is_local_mode() default = True.
    local_lane = is_local_mode()
    if local_lane:
        compute_class = "local"
        sizing = {"vcpus": os.cpu_count() or 1}
        spot_label = None
    else:
        compute_class = select_compute_class(auto.estimated_active_cells)
        sizing = AWS_BATCH_COMPUTE_CLASS_SIZING.get(
            compute_class, AWS_BATCH_COMPUTE_CLASS_SIZING["standard"]
        )
        spot_label = f"Spot-eligible ({compute_class})"
    vcpus = int(sizing.get("vcpus", 1)) or 1

    granularity = GranularitySuggestion(
        engine="swmm",
        resolution_param="target_resolution_m",
        suggested_resolution_m=float(auto.resolution_m),
        resolution_choices=resolution_choices,
        estimated_active_cells=int(auto.estimated_active_cells),
        estimated_solve_seconds=float(auto.estimated_solve_seconds),
        vcpus=vcpus,
        compute_class=compute_class,
        cell_cap=int(auto.cell_cap),
        coarsened=bool(auto.coarsened),
        reason=auto.reason[:512],
        spot_label=spot_label,
    )

    where = params.get("location_query") or params.get("bbox") or "?"
    envelope = PayloadWarningEnvelopePayload(
        warning_id=new_ulid(),
        tool_name="swmm_urban_flood",
        tool_args={
            "location": str(where),
            "return_period_yr": params.get("return_period_yr"),
            "storm_duration_hr": params.get("storm_duration_hr"),
            "building_representation": params.get("building_representation"),
            "target_resolution_m": float(auto.resolution_m),
            "compute_class": compute_class,
        },
        estimated_mb=0.0,
        threshold_mb=0.0,
        recommendation=(
            f"Run a SWMM urban-flood simulation for {where} at "
            f"~{auto.resolution_m:.0f} m (~{auto.estimated_active_cells} active "
            f"cells, est ~{auto.estimated_solve_seconds:.0f} s). Pick a finer or "
            "coarser resolution, or confirm to start."
        )[:512],
        options=["proceed", "cancel", "narrow_scope"],
        granularity=granularity,
    )
    return envelope, auto, dem_path
