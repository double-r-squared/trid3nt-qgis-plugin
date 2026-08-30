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

from .estimate import CardEstimate

logger = logging.getLogger("trid3nt_server.gates.cards.solver_confirm")


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
    fetch (memory: feedback_user_controlled_granularity). PURE arithmetic (no DEM
    read / network): coerce the bbox, compute the bbox extent in metres, build the
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

    from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
    from trid3nt_server.tools.fetchers.imagery._pc_stac import bbox_pixel_dims

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
    # The fetch runs in-process on this machine, so the compute label the
    # QGIS-plugin and web cards render is "local".
    fetch_compute_class = "local"
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
    from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
    from trid3nt_server.workflows.sfincs.flood.flood import (
        _estimate_frame_count,
        _resolve_output_interval_min,
    )
    from trid3nt_server.workflows.shared.frames import MAX_FLOOD_FRAMES
    from trid3nt_server.workflows.sfincs.sfincs_builder import (
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
        # it off-thread per the no-sync-blocking-on-the-loop norm.
        auto = await asyncio.to_thread(
            suggest_sfincs_resolution_from_bbox,
            tuple(coerced),  # type: ignore[arg-type]
        )
        # The solve runs on this machine, so the card's compute descriptors are
        # this host's rather than a sizing class.
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
            "(local solve)."
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


async def estimate_fetch_resolution(
    params: dict, *, tool_name: str, **_: Any
) -> CardEstimate:
    """Estimate provider for the heavy raster fetchers (fetch_dem/topobathy/landcover).

    Wraps :func:`_build_fetch_resolution_envelope`. Honours the fetch_landcover
    no-coarsening skip by returning a ``CardEstimate(envelope=None)`` (dispatch as-is)
    when the suggested rung IS the native 30 m grid and no finer was requested.
    """
    envelope, suggestion = await _build_fetch_resolution_envelope(tool_name, params)
    if (
        tool_name == "fetch_landcover"
        and envelope.granularity is not None
        and envelope.granularity.suggested_resolution_m
        <= _LANDCOVER_DEFAULT_RES_M + 1e-9
    ):
        return CardEstimate(envelope=None)
    return CardEstimate(
        envelope=envelope, tail_state={"fetch_suggestion": suggestion}
    )


def pin_fetch_resolution(
    decision: str, revised_args: dict | None, params: dict, tail_state: dict
) -> dict | None:
    """Pin provider for the fetch-resolution gate. No ``confirmed`` (fetchers ignore it).

    proceed -> pin the SUGGESTED resolution_m the card showed. narrow_scope -> honour the
    chosen resolution_m floored UP to finest_allowed_m so a fine rung on a huge AOI stays
    bounded.
    """
    suggestion = tail_state["fetch_suggestion"]
    if decision == "narrow_scope":
        revised = revised_args or {}
        try:
            chosen = float(revised.get("resolution_m", suggestion.coarse_default_m))
        except (TypeError, ValueError):
            chosen = float(suggestion.coarse_default_m)
        clamped = _clamp_fetch_resolution(chosen, suggestion.finest_allowed_m)
        return {"resolution_m": int(clamped)}
    return {"resolution_m": int(suggestion.coarse_default_m)}


async def estimate_flood_run_settings(
    params: dict, *, tool_name: str, **_: Any
) -> CardEstimate:
    """Estimate provider for the SFINCS flood combined run-settings gate.

    Wraps :func:`_build_flood_run_settings_envelope` and records the autoscale result,
    resolved cadence, window, and whether an override (narrow_scope) was advertised, so
    the pin provider can honour a resolution/cadence/window override or fail closed on a
    pluvial (proceed/cancel-only) card.
    """
    envelope, auto, interval, dur = await _build_flood_run_settings_envelope(
        tool_name, params
    )
    return CardEstimate(
        envelope=envelope,
        tail_state={
            "flood_grid_autoscale": auto,
            "flood_output_interval_min": interval,
            "flood_duration_hr": dur,
            "flood_override_offered": "narrow_scope" in envelope.options,
        },
    )


def pin_flood_run_settings(
    decision: str, revised_args: dict | None, params: dict, tail_state: dict
) -> dict | None:
    """Pin provider for the flood gate (grid resolution + cadence + window levers).

    Returns ``None`` on a narrow_scope to a pluvial card that offered only proceed/cancel
    (fail-closed). Otherwise pins ``confirmed`` + the SUGGESTED grid_resolution_m /
    output_interval_min on proceed, or the chosen overrides on narrow_scope.
    """
    auto = tail_state["flood_grid_autoscale"]
    interval = tail_state["flood_output_interval_min"]
    if decision == "narrow_scope":
        if not tail_state["flood_override_offered"]:
            return None
        revised = revised_args or {}
        delta: dict[str, Any] = {"confirmed": True}
        if auto is not None:
            try:
                chosen_grid_res = float(
                    revised.get("grid_resolution_m", auto.grid_resolution_m)
                )
            except (TypeError, ValueError):
                chosen_grid_res = float(auto.grid_resolution_m)
            if chosen_grid_res > 0:
                delta["grid_resolution_m"] = chosen_grid_res
                delta["enable_autoscale"] = False
        chosen_interval = interval
        if (
            "output_interval_min" in revised
            and revised["output_interval_min"] is not None
        ):
            try:
                chosen_interval = max(1.0, float(revised["output_interval_min"]))
            except (TypeError, ValueError):
                chosen_interval = interval
        if chosen_interval is not None:
            delta["output_interval_min"] = float(chosen_interval)
        if "duration_hr" in revised and revised["duration_hr"] is not None:
            try:
                chosen_duration = float(revised["duration_hr"])
                if chosen_duration > 0:
                    delta["duration_hr"] = chosen_duration
            except (TypeError, ValueError):
                pass
        return delta
    delta = {"confirmed": True}
    if auto is not None:
        delta["grid_resolution_m"] = float(auto.grid_resolution_m)
    if interval is not None:
        delta["output_interval_min"] = float(interval)
    return delta
