"""Solver-confirm, granularity and fetch-resolution confirm-card builders.

Pure envelope and suggestion builders: no websocket, no session state.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from trid3nt_contracts import new_ulid

from .estimate import CardEstimate

logger = logging.getLogger("trid3nt_server.gates.cards.solver_confirm")


#: hard px-grid ceiling for the fetch-resolution gate. A fine
#: rung on a huge AOI would materialize an enormous raster; finest_allowed_m is
#: floored at max(ladder_floor, max(width_m, height_m) / MAX_FETCH_PX) so the
#: finest selectable rung keeps the grid bounded to ~8192 px on the long axis.
MAX_FETCH_PX: int = 8192


# Per-fetcher resolution ladders for the fetch-resolution gate. Finer = smaller
# metres. fetch_dem reaches 1 m (3DEP); fetch_topobathy floors at 3 m (CUDEM
# tiles); both default to 10 m. fetch_landcover's native NLCD grid is 30 m and
# the gate coarsens to 60/120/300/600 m on a large bbox so the MRLC WCS
# GetCoverage stays under 4000 px per axis. fetch_dem's 90/300/900 m rungs exist
# because a state-scale AOI needs roughly 150 m to stay under the tool's own
# 4000 px/axis budget; without them the ladder-filtered choices collapse to the
# computed finest_allowed_m alone, with no coarser alternative.
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
# server rejects or times out a GetCoverage beyond roughly 4096 px per axis, so
# the fetch_landcover card bounds its finest selectable rung to 4000 px rather
# than the generic MAX_FETCH_PX; otherwise the card would offer a rung the tool
# cannot deliver and would silently coarsen past. fetch_dem auto-coarsens
# against its own 4000 px/axis budget, and the ceiling is identical here so the
# card's suggested rung matches what the fetch will actually deliver.
_FETCH_MAX_PX_BY_TOOL: dict[str, int] = {
    "fetch_landcover": 4000,
    "fetch_dem": 4000,
}


def _clamp_fetch_resolution(chosen_m: float, finest_allowed_m: float) -> float:
    """Floor a user-chosen fetch resolution UP to the finest allowed cell size.

    Finer = SMALLER metres, so the px-grid bound is a LOWER bound on the rung;
    a coarser request than ``finest_allowed_m`` is honoured exactly."""
    # A request finer than the bound would materialize a grid past MAX_FETCH_PX
    # on the long axis, so it is clamped up to the bound instead.
    return max(float(chosen_m), float(finest_allowed_m))


async def _build_fetch_resolution_envelope(
    tool_name: str, params: dict
) -> tuple[Any, Any]:
    """Build the fetch-resolution confirm card for a heavy raster fetcher.

    PURE arithmetic: no DEM read and no network. Raises on a missing or invalid
    bbox so the caller fails OPEN rather than blocking the fetch on a gate error."""
    from trid3nt_contracts.payload_warning import (
        GranularitySuggestion,
        PayloadWarningEnvelopePayload,
    )
    from types import SimpleNamespace

    from trid3nt_server.tools.tool_arg_normalizer import coerce_bbox_value
    from trid3nt_server.tools.fetchers._fetch_common import bbox_pixel_dims

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
    # What the decision tail reads: ``coarse_default_m`` on a proceed,
    # ``finest_allowed_m`` for the narrow_scope clamp, and the cell ``cap``.
    fetch_suggestion = SimpleNamespace(
        coarse_default_m=float(suggested),
        finest_allowed_m=float(finest_allowed_m),
        cap=int(max_fetch_px) ** 2,
    )
    return envelope, fetch_suggestion


def _gate_memory_key(tool_name: str, params: dict[str, Any]) -> tuple[str, str]:
    """Turn-memory key for the solver-confirm / fetch-resolution gate.

    Keyed on ``(tool_name, rounded bbox)``, or on the FULL normalized args when
    the call carries no bbox."""
    # The bbox rounds to ~6 decimal degrees, the same quantization the fetch
    # tools use for cache-key stability, so a retry of the SAME tool over the
    # SAME AOI with a corrected non-bbox arg reuses the earlier decision instead
    # of re-gating. Without a bbox-shaped AOI the full-args key is the
    # conservative default: any arg change gates fresh.
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
    """Estimate provider for the heavy raster fetchers.

    Returns ``CardEstimate(envelope=None)`` -- no gate -- when the suggested rung
    IS the native grid and nothing finer was asked for."""
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
    """Pin provider for the fetch-resolution gate; no ``confirmed`` is injected.

    A proceed pins the SUGGESTED rung the card showed; a narrow_scope honours the
    chosen rung floored UP to ``finest_allowed_m``."""
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
