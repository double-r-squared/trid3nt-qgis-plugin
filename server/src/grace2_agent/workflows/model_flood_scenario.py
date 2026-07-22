"""model_flood_scenario workflow (M5 capstone — job-0042).

This module implements the **M5 capstone composition**:

    geocode_location (if location_query)
      → fetch_dem
      → fetch_landcover (with NLCD vintage_year sidecar per OQ-4 §4)
      → fetch_river_geometry
      → lookup_precip_return_period
      → build_sfincs_model        ← OQ-4 §4 NLCD validation gate fires here
      → run_solver(sfincs, model_setup_uri)
      → wait_for_completion(handle)
      → postprocess_flood
      → AssessmentEnvelope (Flood subtype, Appendix B.4)

Per Decision G + FR-TA-1, this workflow is **deterministic Python composition**
— there is no LLM in the chain. The workflow returns a typed
``AssessmentEnvelope`` whose ``flood: FloodPayload`` subtype carries the
narration metrics.

LLM exposure (workflow-as-atomic-tool-wrapper pattern):

    @register_tool(AtomicToolMetadata(name="run_model_flood_scenario",
                                       ttl_class="live-no-cache",
                                       source_class="workflow_dispatch",
                                       cacheable=False))
    def run_model_flood_scenario(bbox?, location_query?, ...) -> dict: ...

The wrapper forwards verbatim to ``model_flood_scenario`` and returns the
envelope's ``model_dump(mode="json")`` (a dict — the LLM tool surface doesn't
need the pydantic instance). The wrapper carries the FR-DC-6 ``cacheable=False``
flag because workflows are uncacheable (the whole point is the dispatch +
solver run + envelope build, never the cached return).

Partial-failure envelope shape (TENTATIVE per kickoff Open Questions):
    On any internal failure (fetcher exception, NLCD validation gate firing,
    SFINCS dispatch error, solver SOLVER_FAILED, postprocess error), the
    workflow still returns a typed ``AssessmentEnvelope`` — but with
    ``envelope_type="modeled"``, an empty layers list, and a
    ``FloodPayload`` carrying zero-valued metrics + the error code threaded
    into the ``solver_version`` field (a documented seam — see
    OQ-42-PARTIAL-FAILURE-ENVELOPE-SHAPE). The agent surface narrates the
    envelope honestly ("scenario could not be modeled because …") rather than
    fabricating depth values.

Cross-cutting principles in force:
- **Invariant 1 (Determinism boundary): preserves.** No LLM in the chain.
- **Invariant 2 (Deterministic workflows): preserves.** Straight-line
  composition; each step's failure surfaces as a typed exception caught at
  the workflow boundary.
- **Invariant 7 (no silent wrong answers): EXTENDS — the headline.** The
  ``build_sfincs_model`` NLCD validation gate is the load-bearing mitigation
  for OQ-4. ``LULC_MAPPING_MISMATCH`` is surfaced as a failed envelope, not a
  dispatched-broken-model SFINCS run.
- **Invariant 8 (Cancellation is first-class): preserves.** The workflow
  awaits ``wait_for_completion`` — any ``asyncio.CancelledError`` propagates
  through the workflow as-is, triggering the 850ms cancel chain from
  job-0041.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from grace2_contracts import new_ulid
from grace2_contracts.envelope import (
    AssessmentEnvelope,
    CriticalFacility,
    DataSource,
    FloodMetrics,
    FloodPayload,
    ForcingSummary,
    Provenance,
    ResultLayer,
)
from grace2_contracts.execution import ExecutionHandle, LayerURI, ModelSetup, RunResult
from grace2_contracts.tool_registry import AtomicToolMetadata

from ..layer_uri_emit import emit_layer_uri, publish_input_layer
from ..pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from ..tools import register_tool
from ..tools.data_fetch import (
    fetch_dem,
    fetch_landcover,
    fetch_river_geometry,
    geocode_location,
    lookup_precip_return_period,
)
from ..tools.fetch_topobathy import TopobathyError, fetch_topobathy
from ..tools.publish_layer import PublishLayerError, publish_layer
from ..tools.solver import (
    run_solver,
    select_compute_class,
    wait_for_completion,
)
from .mesh_layer import make_sfincs_mesh_layer_uri
from .postprocess_flood import (
    FLOOD_DEPTH_STYLE_PRESET,
    PostprocessError,
    postprocess_flood,
)
from .postprocess_waves import (
    WAVE_HEIGHT_STYLE_PRESET,
    postprocess_waves,
)
from .register_published_manifest import (
    read_publish_manifest,
    register_manifest_layers,
)
from .sfincs_builder import (
    BuildOptions,
    DischargeForcing,
    ForcingSpec,
    # NATE 2026-06-26: infiltration-loss member (scenario coverage).
    InfiltrationForcing,
    PressureForcing,
    SFINCSSetupError,
    # SPIDERWEB (2026-07-19): parametric-hurricane wind+pressure member.
    SpiderwebForcing,
    WaterlevelForcing,
    WindForcing,
    # Heavy-compute offload: the NLCD validation gate stays a light PRE-SUBMIT
    # check on the fetched landcover (so LULC_MAPPING_MISMATCH still surfaces as
    # the SAME failed envelope), and the bbox-only autoscale sizes the Batch job
    # + telemetry (the worker re-does the DEM-active autoscale for real).
    _extract_unique_nlcd_classes,
    _to_vsigs,
    build_sfincs_model,
    load_manning_mapping,
    suggest_sfincs_resolution_from_bbox,
    validate_nlcd_vintage_against_mapping,
)
from .physics_registry import PhysicsRegistryError, validate_and_resolve_physics
from .sfincs_forcing_adapter import SFINCSForcingAdapterError

__all__ = [
    "model_flood_scenario",
    "run_model_flood_scenario",
    "WorkflowError",
    "PrecipForcingError",
    "compute_precip_area_mean_mm_per_hr",
    "_resolve_surge_forcing_from_fetchers",
]

logger = logging.getLogger("grace2_agent.workflows.model_flood_scenario")


# Default project/session identifiers for ULID-bearing envelope fields. The
# agent runtime threads real IDs through when WS state is present; the
# workflow itself accepts None and falls back to fresh ULIDs so a direct call
# (smoke harness, integration test) still produces a valid envelope.
_FALLBACK_PROJECT_ID = None
_FALLBACK_SESSION_ID = None


# --- Pre-solver phase timeouts (terminal-pipeline-card hardening) -----------
# The fetcher chain (Steps 1-4) + ``build_sfincs_model`` (Step 5) run BEFORE
# ``wait_for_completion``, which is the only phase that previously emitted
# progress. If any pre-solver step hangs (a wedged data endpoint, a GDAL VSI
# read with no overall timeout, a py3dep stall) the card sat ``running`` with
# NO progress and NO timeout — indistinguishable from the spin-after-cancel
# bug and consistent with NATE's "120 min, never finished" symptom. Each phase
# is now wrapped in ``asyncio.wait_for`` (the sync calls go through
# ``asyncio.to_thread`` so the timeout is enforceable) and bounded by a
# GENEROUS budget — large enough that a healthy fetch/build never trips it, but
# finite so a true hang surfaces as a typed ``*_TIMEOUT`` failed envelope
# instead of an infinite await. Overridable via env for ops tuning.
_FETCHER_PHASE_TIMEOUT_S = float(
    os.environ.get("GRACE2_FLOOD_FETCHER_TIMEOUT_S", "900")  # 15 min
)
_BUILD_PHASE_TIMEOUT_S = float(
    os.environ.get("GRACE2_FLOOD_BUILD_TIMEOUT_S", "900")  # 15 min
)


async def _emit_presolver_progress(
    emitter: Any, progress_percent: int
) -> None:
    """Best-effort pre-solver progress bump on the current pipeline card.

    Keeps the card from sitting SILENTLY during the multi-second pre-solver
    chain. ``emitter`` is the ``current_emitter()`` handle (may be ``None``
    outside a WS dispatch — direct call / smoke / unit test); failure is
    swallowed because progress is a UX hint, never a correctness gate.
    """
    if emitter is None:
        return
    try:
        await emitter.update_current_progress(progress_percent)
    except Exception as exc:  # noqa: BLE001 — progress is non-fatal
        logger.debug(
            "model_flood_scenario: pre-solver progress emit failed (non-fatal): %s",
            exc,
        )


#: Cadence (seconds) for the LIVE solve-progress envelope during the long solve.
#: Independent of the solver poll cadence — this is a UX tick on the running
#: card; conservative so a 10-20-min solve emits a steady (not chatty) stream.
_LIVE_SOLVE_PROGRESS_INTERVAL_S = 10.0


# --------------------------------------------------------------------------- #
# COASTAL/WAVE animation cadence ("looks like rain" fix)
#
# A coastal surge+SnapWave animation rendered at HOURLY frames (the legacy
# dtout = duration/24 + the 24-frame cap) reads like a slowly-filling bathtub:
# waves move in seconds-to-minutes, so an hourly snapshot of a rising surge
# hides the wave motion entirely regardless of the wave model. For coastal /
# quadtree / wave runs we therefore output map frames at a FINE minute-scale
# interval. Cadence and duration are COUPLED  -  a fine interval over the full 24h
# is hundreds of frames (huge payload), so a watchable wave animation is a
# fine interval over a FOCUSED window (default a few hours, frame count bounded).
# The PLUVIAL path keeps ``output_interval_min=None`` -> the legacy hourly
# cadence (byte-identical).
# --------------------------------------------------------------------------- #

#: Default fine map-output interval (minutes) for a coastal/wave run when the
#: caller/LLM did not pin one. 5 min over a focused window gives a smooth
#: water-rolling-in animation without ballooning the frame count.
_COASTAL_OUTPUT_INTERVAL_MIN_DEFAULT: float = float(
    os.environ.get("GRACE2_COASTAL_OUTPUT_INTERVAL_MIN", "5")
)
#: Physical floor for the requested interval (minutes)  -  mirrors the 60 s deck
#: floor so the resolved frame count never explodes past what the deck emits.
_OUTPUT_INTERVAL_MIN_FLOOR: float = 1.0


def _resolve_output_interval_min(
    *,
    is_coastal: bool,
    output_interval_min: int | float | None,
    duration_hr: float,
) -> float | None:
    """Resolve the SFINCS map-output interval (minutes) by sim type.

    Returns the FINE minute-scale interval for a coastal/wave run (so the
    animation reads as water rolling in, not a filling bathtub) and ``None`` for
    the pluvial path (the legacy hourly cadence, byte-identical).

    Precedence:
    - PLUVIAL (``is_coastal`` False): ALWAYS ``None``  -  the pluvial deck is never
      touched (regression-critical), even if a stray ``output_interval_min`` was
      passed; rain animates fine at hourly stride.
    - COASTAL with an explicit ``output_interval_min``: honor it, floored at
      ``_OUTPUT_INTERVAL_MIN_FLOOR`` minutes (the deck re-floors at 60 s).
    - COASTAL with no explicit value: the
      ``_COASTAL_OUTPUT_INTERVAL_MIN_DEFAULT`` (LLM-default-by-sim-type).

    ``duration_hr`` is accepted so a future window-narrowing default can ride
    here; v0.1 keeps the full ``duration_hr`` window and bounds the frame count
    via ``MAX_FLOOD_FRAMES`` (postprocess) + the deck dtout floor.
    """
    if not is_coastal:
        return None
    if output_interval_min is not None:
        try:
            return max(_OUTPUT_INTERVAL_MIN_FLOOR, float(output_interval_min))
        except (TypeError, ValueError):
            pass
    return max(_OUTPUT_INTERVAL_MIN_FLOOR, _COASTAL_OUTPUT_INTERVAL_MIN_DEFAULT)


def _estimate_frame_count(
    *, output_interval_min: float | None, duration_hr: float
) -> int:
    """Estimate the number of animation frames a cadence yields over the window.

    Used by the user gate to surface "N frames every M min" before the run. The
    real frame count is bounded by ``MAX_FLOOD_FRAMES`` in postprocess; this is
    the pre-cap raw snapshot count = ``duration_hr*60 / interval`` (clamped to
    [1, MAX_FLOOD_FRAMES]). ``None`` interval -> the legacy ~24 hourly frames.
    """
    from .postprocess_flood import MAX_FLOOD_FRAMES

    if output_interval_min is None or output_interval_min <= 0:
        raw = max(1, int(round(float(duration_hr))))  # ~1 frame/hour
    else:
        raw = int(round(float(duration_hr) * 60.0 / float(output_interval_min)))
    return max(1, min(int(MAX_FLOOD_FRAMES), raw))


def _extract_solve_autoscale(model_setup: Any) -> dict[str, Any]:
    """Pull the autoscale provenance (active cells / vCPU / est-solve) off the
    built ``ModelSetup`` for the live solve-progress envelope + telemetry.

    Mirrors ``_emit_flood_solve_telemetry``'s read of
    ``model_setup.parameters['autoscale']`` so the live card and the
    at-completion telemetry agree on cells/vCPU. Returns ``{}`` when absent.
    """
    params = getattr(model_setup, "parameters", {}) or {}
    autoscale = params.get("autoscale") if isinstance(params, dict) else None
    return autoscale if isinstance(autoscale, dict) else {}


async def _drive_live_solve_progress(
    *,
    emitter: Any,
    run_id: str,
    solver: str,
    grid_resolution_m: float | None,
    active_cell_count: int | None,
    vcpus: int | None,
    eta_seconds: float | None,
) -> None:
    """Background loop: emit the LIVE solve-progress envelope every N seconds.

    Runs alongside ``wait_for_completion`` so the running tool/pipeline card
    shows grid/cells/vCPU/elapsed/ETA ticking during the long solve (rather than
    a silent multi-minute spinner). ``elapsed_seconds`` is wall-clock from this
    coroutine's start (Invariant 1: never an LLM estimate); ``eta_seconds`` is
    the perf-model ``estimated_solve_seconds`` when available, else ``None``.

    Best-effort + cancellation-safe: the caller cancels this task when the solve
    returns; any emit failure is swallowed (live telemetry is a UX hint, never a
    correctness gate). No-op when ``emitter`` is ``None`` (direct/smoke/test
    call without a WS emitter)."""
    if emitter is None:
        return
    from ..telemetry import build_live_solve_progress

    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        while True:
            elapsed = max(0.0, loop.time() - started)
            payload = build_live_solve_progress(
                run_id=run_id,
                solver=solver,
                grid_resolution_m=grid_resolution_m,
                active_cell_count=active_cell_count,
                vcpus=vcpus,
                elapsed_seconds=elapsed,
                eta_seconds=eta_seconds,
            )
            try:
                await emitter.emit_solve_progress(payload)
            except Exception as exc:  # noqa: BLE001 — UX hint, never fatal
                logger.debug(
                    "model_flood_scenario: live solve-progress emit failed "
                    "(non-fatal): %s",
                    exc,
                )
            await asyncio.sleep(_LIVE_SOLVE_PROGRESS_INTERVAL_S)
    except asyncio.CancelledError:
        # Normal teardown when the solve completes — re-raise so the task
        # finalizes cleanly.
        raise


#: Cadence (seconds) for the LIVE pre-solver progress ticks during the long
#: fetcher chain + SFINCS build. Deliberately WELL UNDER the browser WS
#: data-frame watchdog window (~25-30 s) so a real ``pipeline-state`` DATA frame
#: lands on the active connection several times per phase -- this is what keeps
#: the client from force-reconnecting ("run goes dark / hangs") during the ~70 s
#: pre-solver phase when the work is off-loop in a worker thread and the turn is
#: otherwise SILENT. Tunable via env for ops.
_PRESOLVER_PROGRESS_TICK_S: float = float(
    os.environ.get("GRACE2_PRESOLVER_PROGRESS_TICK_S", "7")
)


async def _drive_presolver_phase_progress(
    emitter: Any,
    *,
    start_pct: int,
    end_pct: int,
    expected_seconds: float,
) -> None:
    """Background loop: tick a ``pipeline-state`` DATA frame on the CURRENT
    running pre-solver step every ``_PRESOLVER_PROGRESS_TICK_S`` seconds.

    THE FIX for the demo-breaking "run hangs / goes dark" symptom: during the
    long pre-solver phases (the fetcher chain pulling DEM/topobathy/landcover,
    then ``build_sfincs_model``) the heavy work runs OFF the event loop in a
    worker thread (Invariant: no sync-blocking on the loop) and the turn emits
    NOTHING for tens of seconds. With no data frame on the wire, the browser's
    WS inbound-activity watchdog (~25-30 s window -- the WS-30s-storm class) trips
    and the client force-reconnects mid-build, so the user sees the run freeze
    even though it is healthy and proceeds to dispatch server-side. This driver
    emits a real ``pipeline-state`` frame (via ``update_current_progress``)
    several times per phase, which (a) resets the client watchdog -> NO reconnect,
    and (b) creeps the card progress so the user sees it is working.

    The percent CREEPS from ``start_pct`` toward ``end_pct`` on an asymptotic
    ``elapsed/expected`` curve clamped to 95% of the band, so a slower-than-
    expected phase never visually "completes" early or stalls at a flat number.
    ``update_current_progress`` targets the most-recently-added RUNNING step
    (the active ``substep`` child), so this must run INSIDE the phase's
    ``substep`` context.

    Best-effort + cancellation-safe: a no-op when ``emitter`` is ``None``
    (direct/smoke/test call); any emit failure is swallowed (progress is a UX +
    liveness hint, never a correctness gate); the caller cancels it in a
    ``finally`` the instant the phase returns/raises.
    """
    if emitter is None:
        return
    loop = asyncio.get_running_loop()
    started = loop.time()
    band = max(0, int(end_pct) - int(start_pct))
    try:
        while True:
            await asyncio.sleep(_PRESOLVER_PROGRESS_TICK_S)
            elapsed = max(0.0, loop.time() - started)
            frac = min(0.95, elapsed / max(float(expected_seconds), 1.0))
            pct = int(start_pct) + int(round(band * frac))
            try:
                await emitter.update_current_progress(pct)
            except Exception as exc:  # noqa: BLE001 — liveness hint, never fatal
                logger.debug(
                    "model_flood_scenario: pre-solver progress tick failed "
                    "(non-fatal): %s",
                    exc,
                )
    except asyncio.CancelledError:
        # Normal teardown when the phase completes — re-raise so the task
        # finalizes cleanly.
        raise


class WorkflowError(RuntimeError):
    """Raised by the workflow when composition fails fatally (rare).

    Most failure modes inside the workflow are surfaced as a typed
    AssessmentEnvelope with zero-valued metrics + the error code threaded
    through (per the partial-failure shape). ``WorkflowError`` is reserved
    for the case where even building a failed envelope isn't possible (e.g.
    geocoder returns no bbox AND no bbox was supplied).
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# --------------------------------------------------------------------------- #
# Helpers — bbox resolution + zero-metrics envelope builder
# --------------------------------------------------------------------------- #


def _resolve_bbox(
    *,
    bbox: tuple[float, float, float, float] | None,
    location_query: str | None,
) -> tuple[tuple[float, float, float, float], dict[str, Any] | None]:
    """Resolve the bbox via direct param or via ``geocode_location``.

    Precedence per the kickoff TENTATIVE: bbox-direct wins when both are
    given (matches the "intent + irreducible inputs" Decision K — bbox IS
    the irreducible input; geocode is a convenience).

    Returns:
        Tuple ``(bbox, geocode_result)``; ``geocode_result`` is the geocoder's
        return dict (carries canonical name + provenance) when geocoding was
        run, ``None`` when bbox was supplied directly.
    """
    if bbox is not None:
        if location_query is not None:
            logger.info(
                "model_flood_scenario: both bbox and location_query given; "
                "bbox-direct wins (decision K precedence)"
            )
        return bbox, None
    if location_query is None:
        raise WorkflowError(
            "BBOX_UNRESOLVABLE",
            "model_flood_scenario requires either bbox or location_query",
        )
    geo = geocode_location(location_query)
    bb = geo.get("bbox")
    if not bb or len(bb) != 4:
        raise WorkflowError(
            "GEOCODE_NO_BBOX",
            f"geocode_location({location_query!r}) returned no usable bbox: {geo!r}",
        )
    return (float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])), geo


def _build_failed_envelope(
    *,
    bbox: tuple[float, float, float, float],
    project_id: str,
    session_id: str,
    error_code: str,
    error_detail: str,
    workflow_name: str,
    data_sources: list[DataSource],
    forcing: ForcingSummary | None,
    solver_run_ids: list[str],
    return_period_years: int,
    duration_hours: float,
    grid_resolution_m: float,
) -> AssessmentEnvelope:
    """Construct a typed failed-flood AssessmentEnvelope.

    Per OQ-42-PARTIAL-FAILURE-ENVELOPE-SHAPE (TENTATIVE): zero-valued
    FloodMetrics + error_code threaded into ``solver_version`` (a documented
    out-of-band seam — the schema-side ``solver_version`` is a string field
    so we can carry ``"failed:LULC_MAPPING_MISMATCH"`` etc. The agent surface
    parses this and emits a meaningful failure narration.)

    All required envelope fields are populated with safe defaults so the
    pydantic validator doesn't reject the failed envelope.
    """
    now = datetime.now(timezone.utc)
    # job-0327 (HONESTY FLOOR, B2): promote the error code onto the depth-0
    # ``workflow_name`` string ("<name>:FAILED:<CODE>") so it survives the
    # adapter's ``_coerce_to_summary_value`` depth>=2 dict-collapse (the
    # ``flood.metrics.solver_version`` threading sits at depth 2 and is reduced
    # to bare key names before the LLM sees it). This gives the adapter's
    # failed-modeled-envelope classifier (summarize_tool_result, job-0327 B1) a
    # depth-0 corroborating signal AND keeps the code human-legible in the
    # function_response even if the classifier were ever bypassed. The
    # ``:FAILED:`` infix is the parse anchor (``workflow_name`` never otherwise
    # contains it). Guard against double-tagging when this envelope is re-built.
    failed_workflow_name = (
        workflow_name
        if ":FAILED:" in workflow_name
        else f"{workflow_name}:FAILED:{error_code}"
    )
    return AssessmentEnvelope(
        envelope_id=new_ulid(),
        project_id=project_id,
        session_id=session_id,
        envelope_type="modeled",
        hazard_type="flood",
        workflow_name=failed_workflow_name,
        bbox=bbox,
        crs="EPSG:4326",
        forcing=forcing,
        layers=[],
        provenance=Provenance(data_sources=data_sources),
        created_at=now,
        completed_at=now,
        solver_run_ids=solver_run_ids,
        flood=FloodPayload(
            metrics=FloodMetrics(
                flooded_area_km2=0.0,
                max_depth_m=0.0,
                mean_depth_m=0.0,
                p95_depth_m=0.0,
                solver_version=f"failed:{error_code}",
                grid_resolution_m=grid_resolution_m,
                simulation_duration_hours=int(duration_hours),
            )
        ),
    )


def _bbox_area_km2(bbox: tuple[float, float, float, float]) -> float:
    """Approximate WGS84 bbox area in km^2 (matches data_fetch helper)."""
    import math

    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lat = 0.5 * (min_lat + max_lat)
    dlat_km = (max_lat - min_lat) * 111.320
    dlon_km = (max_lon - min_lon) * 111.320 * math.cos(math.radians(mid_lat))
    return abs(dlat_km * dlon_km)


def _emit_flood_solve_telemetry(
    *,
    run_result: "RunResult",
    handle: Any,
    model_setup: Any,
    bbox: tuple[float, float, float, float],
    grid_resolution_m: float,
) -> dict | None:
    """Emit a solve-completion telemetry record (sprint-16 autoscale).

    Pulls the autoscale provenance (estimated active cells, chosen resolution,
    vCPU) off ``model_setup.parameters`` and the wall-clock from the
    ``RunResult`` (``duration_seconds``), and folds in the backend
    (``handle.workflow_name`` — ``local-docker`` / ``local-exec`` /
    ``grace-2-sfincs-orchestrator``) + aoi_km2. Best-effort; returns the record
    (or ``None`` on any failure) so the caller's try/except stays simple.
    """
    from ..telemetry import emit_solve_telemetry

    params = getattr(model_setup, "parameters", {}) or {}
    autoscale = params.get("autoscale") if isinstance(params, dict) else None
    autoscale = autoscale if isinstance(autoscale, dict) else {}

    active_cells = autoscale.get("estimated_active_cells")
    vcpus = autoscale.get("vcpus")
    est_solve_s = autoscale.get("estimated_solve_seconds")
    coarsened = autoscale.get("coarsened")
    # Prefer the actually-built resolution off the ModelSetup; fall back to the
    # workflow's resolution variable.
    built_res = getattr(model_setup, "grid_resolution_m", None) or grid_resolution_m

    return emit_solve_telemetry(
        run_id=run_result.run_id,
        backend=str(getattr(handle, "workflow_name", "") or "unknown"),
        active_cell_count=int(active_cells) if active_cells is not None else None,
        grid_resolution_m=float(built_res) if built_res is not None else None,
        vcpus=int(vcpus) if vcpus is not None else None,
        wall_clock_seconds=run_result.duration_seconds,
        aoi_km2=_bbox_area_km2(bbox),
        solver=getattr(handle, "solver", "sfincs") or "sfincs",
        estimated_solve_seconds=float(est_solve_s) if est_solve_s is not None else None,
        coarsened=bool(coarsened) if coarsened is not None else None,
    )


def _record_flood_batch_solve_telemetry(
    *,
    run_result: "RunResult",
    handle: Any,
    model_setup: Any,
    grid_resolution_m: float,
    session_id: str | None,
    case_id: str | None,
) -> dict | None:
    """Record ONE SOLVE row merging the Batch compute meta + the mesh descriptor.

    task-153: the regular-grid SFINCS Batch path exposes both a ``handle`` and a
    terminal ``RunResult``; the wait-loop captured the Spot instance + timing
    breakdown onto ``run_result.batch_compute_meta`` (best-effort, may be
    ``None``). This folds that together with the active-cell count + the built
    grid resolution + the solver + the terminal status + the run/case/session ids
    into the SOLVE telemetry sink (``telemetry.record_solve_telemetry``). Mirrors
    ``_emit_flood_solve_telemetry`` (the autoscale row) — they are siblings: the
    autoscale row drives cap re-tuning, this row drives completion-time
    inference. Best-effort; returns the recorded row (or ``None`` on any failure)
    so the caller's try/except stays trivial. Only the regular-grid path calls
    this (the quadtree submit+wait path is left uninstrumented, consistent with
    the two-card work)."""
    from ..telemetry import record_solve_telemetry

    meta = getattr(run_result, "batch_compute_meta", None) or {}
    if not isinstance(meta, dict):
        meta = {}

    params = getattr(model_setup, "parameters", {}) or {}
    autoscale = params.get("autoscale") if isinstance(params, dict) else None
    autoscale = autoscale if isinstance(autoscale, dict) else {}
    active_cells = autoscale.get("estimated_active_cells")
    built_res = getattr(model_setup, "grid_resolution_m", None) or grid_resolution_m

    row: dict = {
        "run_id": run_result.run_id,
        "solver": getattr(handle, "solver", "sfincs") or "sfincs",
        "status": run_result.status,
        "backend": str(getattr(handle, "workflow_name", "") or "unknown"),
        "case_id": case_id,
        "session_id": session_id,
        "active_cell_count": int(active_cells) if active_cells is not None else None,
        "resolution_m": float(built_res) if built_res is not None else None,
    }
    # Merge the Batch instance + timing fields (instance_type / lifecycle / az /
    # vcpus / memory_mib / *_at_ms / *_secs) — present only on the aws-batch
    # terminal paths; empty dict otherwise (local/in-process).
    row.update(meta)
    return record_solve_telemetry(row)


def _default_runs_prefix(run_id: str) -> str:
    """Fallback runs prefix when ``RunResult.output_uri`` is None.

    Mints the same ``s3://<runs_bucket>/<run_id>/`` shape the local-docker
    solver writes outputs under (``GRACE2_RUNS_BUCKET``, default
    ``trid3nt-runs`` -- the local MinIO runs bucket). GCP is gone: no
    gs:// fabrication.
    """
    import os

    bucket = (os.environ.get("GRACE2_RUNS_BUCKET") or "").strip() or "trid3nt-runs"
    return f"s3://{bucket}/{run_id}/"


# --------------------------------------------------------------------------- #
# cht_sfincs quadtree+SnapWave deck-build (coastal North Star) — agent side
#
# The agent composes a build_spec JSON from the already-fetched topobathy +
# forcing + the grid params build_sfincs_model already computed, uploads it to
# S3, and hands its URI to ``build_sfincs_quadtree_deck`` (solver.py) which
# SUBMITS the GPL-isolated deck-builder Batch job and returns the deck manifest
# URI. The agent NEVER imports cht_sfincs — this is pure JSON + S3 + batch.submit.
# --------------------------------------------------------------------------- #


# Surge/discharge/wave forcing sub-dict keys that carry a FILE URI the worker
# downloads via ``_download`` (entrypoint._split_object_uri rejects anything that
# is not s3:// or gs://). On the QUADTREE path the deck is built REMOTELY on
# Batch, so any of these that point at a LOCAL agent-box path (e.g. the bzs CSV /
# bnd FGB the parametric/CO-OPS surge wrote under /tmp/grace2-sfincs-forcing/)
# must be uploaded to S3 first and the URI rewritten to s3://  -  else the worker
# crashes "unsupported object URI scheme: '/tmp/...'" (run 01KVRJK7333NP2XC64...).
_FORCING_FILE_URI_KEYS = (
    "timeseries_uri",
    "locations_uri",
    "geodataset_uri",
    "rivers_uri",
    "hydrography_uri",
)


def _is_remote_object_uri(uri: Any) -> bool:
    """True when ``uri`` is already an object-store URI the worker can download.

    The deck-builder worker's ``_split_object_uri`` accepts ONLY ``s3://`` /
    ``gs://``. Anything else (a bare ``/tmp/...`` path, ``file://``, a relative
    path) is a LOCAL agent-box path the remote Batch worker cannot read.
    """
    return isinstance(uri, str) and (
        uri.startswith("s3://") or uri.startswith("gs://")
    )


def _upload_local_forcing_files_to_s3(
    surge_forcing: dict[str, Any] | None,
    *,
    cache_bucket: str,
    scheme: str,
    key_prefix: str,
) -> dict[str, Any] | None:
    """Upload any LOCAL forcing file URIs to S3 + return a rewritten surge dict.

    QUADTREE-PATH FIX (run 01KVRJK7333NP2XC64PBHABZ11 crash): the auto-wired /
    parametric surge (and any CO-OPS/GTSM/NWM adapter output) materialises its
    bzs/bnd (and dis/src) files to LOCAL paths on the AGENT box
    (``/tmp/grace2-sfincs-forcing/bzs-*.csv``, ``bnd-*.fgb``). The SIMPLE-SFINCS
    path builds the deck ON the box so those local paths resolve; the QUADTREE
    path builds the deck on a REMOTE Batch worker that can only ``_download``
    ``s3://`` / ``gs://`` URIs. This walks every forcing sub-dict, uploads each
    file URI that is NOT already a remote object URI to
    ``{scheme}://{cache_bucket}/{key_prefix}<filename>``, and returns a DEEP-COPIED
    surge_forcing dict with those URIs rewritten to s3://. Already-remote URIs and
    non-file fields (offset/buffer_m/value_unit/_prov*) pass through untouched.

    Raises ``DeckBuildError`` when a referenced local file is missing or the
    upload fails  -  honest typed failure (the worker would otherwise crash later
    on the unscheme'd URI; surface it here where it is actionable).
    """
    if not surge_forcing:
        return surge_forcing

    from ..tools.solver import SolverDispatchError as _DeckBuildError

    s3 = None  # lazy: only create the client if there is a local file to upload
    out: dict[str, Any] = {}
    for member_name, member in surge_forcing.items():
        if not isinstance(member, dict):
            out[member_name] = member
            continue
        new_member = dict(member)
        for key in _FORCING_FILE_URI_KEYS:
            uri = new_member.get(key)
            if not uri or _is_remote_object_uri(uri):
                continue  # absent, or already an s3:///gs:// URI -> leave as-is
            local_path = uri[len("file://"):] if str(uri).startswith("file://") else str(uri)
            if not os.path.isfile(local_path):
                raise _DeckBuildError(
                    f"quadtree forcing file for {member_name}.{key} is a LOCAL "
                    f"path the remote Batch deck-builder cannot read and it does "
                    f"not exist on the agent box: {uri!r}"
                )
            filename = os.path.basename(local_path)
            s3_key = f"{key_prefix}{member_name}/{filename}"
            s3_uri = f"{scheme}://{cache_bucket}/{s3_key}"
            try:
                if s3 is None:
                    from ..tools.solver import _get_s3_client

                    s3 = _get_s3_client()
                with open(local_path, "rb") as fh:
                    body = fh.read()
                s3.put_object(
                    Bucket=cache_bucket,
                    Key=s3_key,
                    Body=body,
                    ContentType="application/octet-stream",
                )
            except _DeckBuildError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise _DeckBuildError(
                    f"failed to upload quadtree forcing file "
                    f"{member_name}.{key} ({local_path}) to {s3_uri}: {exc}"
                ) from exc
            logger.info(
                "quadtree forcing: uploaded LOCAL %s.%s %s -> %s",
                member_name,
                key,
                local_path,
                s3_uri,
            )
            new_member[key] = s3_uri
        out[member_name] = new_member
    return out


# Parametric offshore design-storm WAVE boundary. SnapWave needs an incident
# significant-wave-height / peak-period / direction boundary to produce a wave
# field; without boundary POINTS the worker logs "no SnapWave boundary points in
# spec - deck has no wave forcing" and hm0 stays flat 0 (observed run
# 01KVRJK7333NP2XC64PBHABZ11). The peak Hs scales with the design-storm return
# period (mirrors ``_parametric_surge_peak_m``): a major-hurricane ARI -> a real
# multi-metre offshore sea state, a frequent event -> a modest swell. Anchored to
# Gulf-coast hurricane offshore conditions (Michael offshore Hs ~ 8-10 m; a
# nearshore/shelf incident boundary an order below that). Tunable via env.
_WAVE_HS_M_AT_100YR = float(os.getenv("GRACE2_WAVE_HS_M_AT_100YR", "4.0"))
_WAVE_HS_M_FLOOR = float(os.getenv("GRACE2_WAVE_HS_M_FLOOR", "0.5"))
_WAVE_HS_M_CEIL = float(os.getenv("GRACE2_WAVE_HS_M_CEIL", "9.0"))

# --------------------------------------------------------------------------- #
# Depth-aware offshore wave-boundary placement (Defect 1 fix)
# --------------------------------------------------------------------------- #
# The parametric SnapWave boundary MUST land in genuine offshore water, not on a
# shallow / intertidal / land bbox edge. SnapWave dissipates the incident wave AT
# a boundary point in < 5 m of water ("depth at boundary input point ... dropped
# below 5 m ... specify input in deeper water") so ~98% of the active mask gets
# zero wave energy (run 01KVSTC80F: boundary stranded in 0.10 m / 1.79 m).
#
# We sample the topobathy DEM (positive-up NAVD88, seabed < 0) and place the
# offshore point(s) on the genuinely-seaward side: the edge with the deepest mean
# bed, pushing the candidate seaward along the offshore bearing until the bed is
# at least ``_WAVE_BND_TARGET_DEPTH_M`` deep. A point that never clears the HARD
# floor (``_WAVE_BND_MIN_DEPTH_M``) is dropped; if NO edge clears it we raise a
# typed error rather than running a flat-zero wave field.

#: Hard minimum water depth (m, positive-down) a wave-boundary point must clear.
#: Mirrors SnapWave's stdout gate (it warns + clamps below 5 m); below this the
#: incident wave breaks AT the boundary and the field is born empty.
_WAVE_BND_MIN_DEPTH_M = float(os.getenv("GRACE2_WAVE_BND_MIN_DEPTH_M", "5.0"))
#: Preferred ("deep enough to not break at the boundary") depth (m). The seaward
#: search keeps pushing out until it reaches this; a point at >= this is ideal.
_WAVE_BND_TARGET_DEPTH_M = float(os.getenv("GRACE2_WAVE_BND_TARGET_DEPTH_M", "10.0"))
#: How far (as a fraction of the bbox span on that axis) to step the candidate
#: seaward per iteration when searching for deep water, and the max steps.
_WAVE_BND_SEAWARD_STEP_FRAC = float(
    os.getenv("GRACE2_WAVE_BND_SEAWARD_STEP_FRAC", "0.04")
)
_WAVE_BND_SEAWARD_MAX_STEPS = int(os.getenv("GRACE2_WAVE_BND_SEAWARD_MAX_STEPS", "10"))


def _sample_dem_depth_m(
    topobathy_uri: str | None,
    points_xy: list[tuple[float, float]],
    target_epsg: int,
) -> list[float] | None:
    """Point-sample positive-DOWN water depth (m) from the topobathy DEM.

    ``points_xy`` are in ``target_epsg`` (the deck CRS). Returns a depth per point
    where depth = ``-elevation`` (DEM is positive-up NAVD88: seabed < 0 -> depth
    > 0; land > 0 -> depth < 0). A NaN/nodata/off-tile sample maps to ``nan``.

    Best-effort + NON-fatal: returns ``None`` (caller falls back to the prior
    bathy-unaware placement) when rasterio/numpy is missing, the DEM cannot be
    read, or ``topobathy_uri`` is falsy. Never raises - the typed deep-water gate
    is the caller's decision, made only when a real DEM WAS sampled.
    """
    if not topobathy_uri or not points_xy:
        return None
    try:
        import numpy as np  # type: ignore[import-not-found]
        import rasterio  # type: ignore[import-not-found]
        from rasterio.warp import (  # type: ignore[import-not-found]
            transform as _warp_transform,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "wave boundary: rasterio/numpy unavailable for DEM depth sampling "
            "(%s) - falling back to bathy-unaware edge placement",
            exc,
        )
        return None

    memfile = None
    try:
        if str(topobathy_uri).startswith("s3://"):
            from rasterio.io import MemoryFile  # type: ignore[import-not-found]

            from ..tools.cache import read_object_bytes_s3

            memfile = MemoryFile(read_object_bytes_s3(topobathy_uri))
            ds_ctx = memfile.open()
        else:
            ds_ctx = rasterio.open(_to_vsigs(topobathy_uri))
        with ds_ctx as ds:
            src_crs = ds.crs
            band_nodata = ds.nodata
            xs = [float(x) for (x, _y) in points_xy]
            ys = [float(y) for (_x, y) in points_xy]
            if src_crs is not None and src_crs.to_epsg() not in (
                int(target_epsg),
                None,
            ):
                xs, ys = _warp_transform(
                    f"EPSG:{int(target_epsg)}", src_crs, xs, ys
                )
            elev = np.fromiter(
                (v[0] for v in ds.sample(zip(xs, ys))),
                dtype="float64",
                count=len(xs),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "wave boundary: DEM depth sampling of %s failed (%s) - falling back "
            "to bathy-unaware edge placement",
            topobathy_uri,
            exc,
        )
        return None
    finally:
        if memfile is not None:
            try:
                memfile.close()
            except Exception:  # noqa: BLE001
                pass

    # Mask declared nodata + common fill sentinels -> NaN (mirrors the worker's
    # ``_mask_topobathy_sentinels``: 9999 / -9999 / 1e20 / |z|>=9000).
    bad = ~np.isfinite(elev)
    if band_nodata is not None:
        bad |= elev == band_nodata
    bad |= np.abs(elev) >= 9000.0
    elev = np.where(bad, np.nan, elev)
    # depth (positive-down) = -elevation. Seabed (elev<0) -> depth>0.
    depth = -elev
    return [float(d) for d in depth]


def _wave_storm_envelope_factor(t_s: float, win_s: float) -> float:
    """Raised-cosine storm envelope factor in [0, 1] at time ``t_s``.

    Mirrors the surge forcing's ``_synthesize_parametric_surge_forcing`` bump: 0 at
    the window ends, 1 at the centre, a smooth rise-and-recede so the incident wave
    grows into the storm peak and recedes. ``win_s`` is the full window in seconds.
    """
    import math

    if win_s <= 0:
        return 1.0
    t_peak = 0.5 * win_s
    span = 0.5 * win_s
    frac = max(-1.0, min(1.0, (float(t_s) - t_peak) / span))
    return 0.5 * (1.0 + math.cos(math.pi * frac))


def _parametric_wave_hs_m(return_period_yr: int | float | None) -> float:
    """Peak incident significant wave height (m) for a design-storm ARI.

    Same monotone log-scaling shape as ``_parametric_surge_peak_m``, anchored at
    the 100-yr offshore Hs (``_WAVE_HS_M_AT_100YR``): +1 decade -> +1.0 m, clamped
    to a sane [floor, ceil] so a degenerate / huge ARI cannot drive a negative or
    runaway sea state. 10-yr near ~3 m, 500-yr near ~4.7 m.
    """
    import math

    rp = float(return_period_yr) if return_period_yr else 100.0
    rp = max(rp, 1.0)
    hs = _WAVE_HS_M_AT_100YR + 1.0 * math.log10(rp / 100.0)
    return max(_WAVE_HS_M_FLOOR, min(_WAVE_HS_M_CEIL, hs))


class WaveBoundaryError(RuntimeError):
    """Raised when a depth-aware offshore wave boundary cannot be placed.

    Carries an A.6 open-set ``error_code`` so the deck-build compose surfaces it
    as a typed failed envelope (honest failure) rather than running a flat-zero
    wave field. The only code is ``WAVE_BOUNDARY_NO_DEEP_WATER`` (no bbox edge
    reaches >= ``_WAVE_BND_MIN_DEPTH_M`` of water even after the seaward search).
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _depth_aware_offshore_points(
    edges: list[dict[str, Any]],
    *,
    cx: float,
    cy: float,
    sample_depths: Any,
) -> list[dict[str, Any]] | None:
    """Pick the genuinely-seaward edge(s) by sampling the DEM at each edge midpoint.

    ``edges`` is the four candidate bbox-edge midpoints in the deck CRS, each
    ``{"name","x","y","ox","oy"}`` where ``(ox, oy)`` is a UNIT seaward step vector
    (outward, away from the AOI centre) for that edge. ``sample_depths`` is a
    callable ``list[(x,y)] -> list[float] | None`` giving positive-down water
    depth (m) at each point (``None`` -> DEM unavailable).

    Algorithm: sample each edge midpoint; for any edge below the target depth,
    push the candidate seaward along ``(ox, oy)`` (steps of
    ``_WAVE_BND_SEAWARD_STEP_FRAC`` of the bbox span) until it clears the target
    depth or runs out of steps. Keep edges whose best candidate clears the HARD
    floor ``_WAVE_BND_MIN_DEPTH_M``, deepest first. Returns the kept points
    (``{"name","x","y","depth_m"}``) or:
      * ``None`` when ``sample_depths`` returns ``None`` (DEM unavailable) -> the
        caller falls back to the prior bathy-unaware placement.
      * raises ``WaveBoundaryError`` when the DEM WAS sampled but NO edge clears
        the hard floor (honest dead-end, never a flat-zero field).
    """
    import math

    # Step size in projected units = a fraction of the bbox span on each axis.
    xs = [e["x"] for e in edges]
    ys = [e["y"] for e in edges]
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)
    step = _WAVE_BND_SEAWARD_STEP_FRAC * max(span_x, span_y, 1.0)

    # Build the full candidate set: each edge midpoint + its seaward pushes.
    probe_pts: list[tuple[float, float]] = []
    probe_owner: list[int] = []  # index into edges
    for i, e in enumerate(edges):
        for k in range(_WAVE_BND_SEAWARD_MAX_STEPS + 1):
            probe_pts.append(
                (e["x"] + k * step * e["ox"], e["y"] + k * step * e["oy"])
            )
            probe_owner.append(i)

    depths = sample_depths(probe_pts)
    if depths is None:
        return None  # DEM unavailable -> caller falls back

    # For each edge, pick the SHALLOWEST seaward step that already clears the
    # target depth (prefer the closest-to-shore deep point); else the DEEPEST
    # candidate it reached. Track each edge's best clearing depth.
    best_by_edge: dict[int, tuple[float, float, float]] = {}  # i -> (depth,x,y)
    for owner, (px, py), d in zip(probe_owner, probe_pts, depths):
        if not math.isfinite(d):
            continue
        prev = best_by_edge.get(owner)
        # Prefer the FIRST candidate that clears the target depth; otherwise keep
        # the deepest seen so far for this edge.
        if d >= _WAVE_BND_TARGET_DEPTH_M:
            if prev is None or prev[0] < _WAVE_BND_TARGET_DEPTH_M:
                best_by_edge[owner] = (d, px, py)
        elif prev is None or d > prev[0]:
            best_by_edge[owner] = (d, px, py)

    kept: list[dict[str, Any]] = []
    for i, e in enumerate(edges):
        best = best_by_edge.get(i)
        if best is None:
            continue
        depth, px, py = best
        if depth < _WAVE_BND_MIN_DEPTH_M:
            continue
        kept.append(
            {"name": e["name"], "x": float(px), "y": float(py), "depth_m": depth}
        )

    if not kept:
        deepest = max(
            (best_by_edge.get(i, (float("nan"),))[0] for i in range(len(edges))),
            default=float("nan"),
        )
        raise WaveBoundaryError(
            "WAVE_BOUNDARY_NO_DEEP_WATER",
            "no AOI edge reaches deep enough water for a SnapWave offshore wave "
            f"boundary: the deepest sampled candidate was {deepest:.2f} m, below "
            f"the {_WAVE_BND_MIN_DEPTH_M:.0f} m floor (SnapWave dissipates the "
            "incident wave at a shallow boundary -> a flat-zero wave field). The "
            "AOI may be fully inland / enclosed, or the topobathy lacks offshore "
            "bathymetry. Extend the AOI seaward into deeper water.",
        )

    # Deepest edge first (the most-seaward forcing the worker should prefer).
    kept.sort(key=lambda p: p["depth_m"], reverse=True)
    return kept


def _synthesize_parametric_wave_boundary(
    bbox: tuple[float, float, float, float],
    *,
    target_epsg: int,
    return_period_yr: int | float | None,
    duration_hr: float = 24.0,
    topobathy_uri: str | None = None,
) -> dict[str, Any]:
    """Build a parametric offshore SnapWave boundary block (incident waves).

    Mirrors ``_synthesize_parametric_surge_forcing`` for the WAVE side, with two
    fixes for the live "empty + static" wave animation (run 01KVSTC80F):

    * DEPTH-AWARE placement (Defect 1): instead of laying one point per 2%-inset
      bbox edge midpoint with NO bathymetric awareness (which stranded the live
      boundary in 0.10 m / 1.79 m of water on the shallow N/E edges so SnapWave
      dissipated the wave AT the boundary), we sample the topobathy DEM and place
      the offshore point(s) on the genuinely-seaward edge -- the one(s) reaching
      genuine deep water (>= ~10 m, hard floor 5 m), pushing each candidate
      seaward along its outward bearing until it clears the gate. If NO edge
      clears the floor we raise ``WaveBoundaryError`` (honest dead-end). When the
      DEM is unavailable we fall back to the prior all-four-edges placement (the
      worker still derives the seaward edge).
    * TIME-VARYING forcing (Defect 2 realism): each point carries a per-time Hs/Tp
      series ramped on the SAME raised-cosine storm envelope the surge forcing
      uses, so hm0 grows into the storm peak and recedes (vs the prior single
      constant Hs/Tp per point). ``wd``/``ds`` stay constant.

    ``hs`` is the PEAK significant wave height (scaled to ``return_period_yr``);
    ``tp`` the peak period (a deep-water period-vs-height relation); ``wd`` the
    direction in SnapWave's nautical "coming FROM" convention (the SEAWARD bearing
    from the AOI centre toward the boundary point -- the prior shoreward wd was
    180 deg wrong, already fixed). Points are emitted in the deck's PROJECTED CRS
    (``target_epsg``) because the worker feeds them straight into
    ``snapwave.boundary_conditions.add_point(x, y, ...)`` (grid coordinates) and
    ``derive_seaward_open_boundary_polygon`` reasons in ``target_epsg``.

    Returns ``{"points": [{"x","y","hs","tp","wd","ds","time_s","hs_series",
    "tp_series"}, ...], "_prov_*": ...}`` -- the exact shape the worker's
    ``resolve_forcing_blocks(...)["snapwave_boundary"]`` consumes (``hs``/``tp``
    are the peak scalars the worker seeds ``add_point`` with; the ``*_series`` +
    shared ``time_s`` are the time-varying override the worker applies). The agent
    does NO cht/GIS work; it only declares the boundary.

    Raises ``WaveBoundaryError("WAVE_BOUNDARY_NO_DEEP_WATER")`` when the DEM was
    sampled but no edge reaches deep water.
    """
    import math

    hs = _parametric_wave_hs_m(return_period_yr)
    # Peak period from a deep-water steepness relation (Tp ~ 3.86*sqrt(Hs), the
    # fully-developed-sea approximation) clamped to a realistic storm window.
    tp = max(4.0, min(16.0, 3.86 * math.sqrt(max(hs, 0.1))))
    # Directional spread (deg)  -  a moderately spread storm sea, not a clean swell.
    ds = 30.0

    min_lon, min_lat, max_lon, max_lat = bbox
    mid_lon = 0.5 * (min_lon + max_lon)
    mid_lat = 0.5 * (min_lat + max_lat)
    inset_lon = 0.02 * (max_lon - min_lon)
    inset_lat = 0.02 * (max_lat - min_lat)
    # One candidate per edge midpoint (lon/lat); the depth-aware selector keeps
    # only the genuinely-seaward (deep) one(s).
    edge_pts_ll: dict[str, tuple[float, float]] = {
        "west": (min_lon + inset_lon, mid_lat),
        "east": (max_lon - inset_lon, mid_lat),
        "south": (mid_lon, min_lat + inset_lat),
        "north": (mid_lon, max_lat - inset_lat),
    }

    # Reproject the boundary points to the deck CRS (the worker's add_point wants
    # grid coordinates). Fall back to lon/lat if pyproj is unavailable (the worker
    # treats them as grid coords either way; a metric grid is the live path).
    try:
        from pyproj import Transformer  # type: ignore[import-not-found]

        tf = Transformer.from_crs(4326, int(target_epsg), always_xy=True)
        cx, cy = tf.transform(mid_lon, mid_lat)
        edge_xy = {n: tf.transform(lon, lat) for n, (lon, lat) in edge_pts_ll.items()}
        projected = True
    except Exception as exc:  # noqa: BLE001  -  degrade to lon/lat (worker re-snaps)
        logger.warning(
            "parametric wave boundary: pyproj reproject to EPSG:%s failed (%s)  -  "
            "emitting lon/lat points",
            target_epsg,
            exc,
        )
        cx, cy = mid_lon, mid_lat
        edge_xy = dict(edge_pts_ll)
        projected = False

    # Build the edge candidate records with an outward (seaward) UNIT step vector.
    edges: list[dict[str, Any]] = []
    for name, (x, y) in edge_xy.items():
        ox, oy = float(x) - cx, float(y) - cy
        norm = math.hypot(ox, oy) or 1.0
        edges.append(
            {"name": name, "x": float(x), "y": float(y), "ox": ox / norm, "oy": oy / norm}
        )

    # --- DEPTH-AWARE selection (Defect 1) ---------------------------------- #
    # Sample the DEM only on the projected (metric) path; lon/lat probes are not
    # meaningful for a CONUS topobathy COG, so degrade to the prior placement.
    selected: list[dict[str, Any]] | None = None
    if projected and topobathy_uri:
        def _sampler(pts: list[tuple[float, float]]) -> list[float] | None:
            return _sample_dem_depth_m(topobathy_uri, pts, int(target_epsg))

        selected = _depth_aware_offshore_points(
            edges, cx=cx, cy=cy, sample_depths=_sampler
        )

    if selected is not None:
        chosen = selected
        logger.info(
            "model_flood_scenario: DEPTH-AWARE SnapWave wave boundary picked "
            "%d offshore edge(s) %s (depths %s m) for bbox=%s",
            len(chosen),
            [c["name"] for c in chosen],
            [round(c["depth_m"], 1) for c in chosen],
            bbox,
        )
    else:
        # Bathy-unaware fallback: keep all four edge midpoints (the worker derives
        # the seaward edge). No depth annotation.
        chosen = [{"name": e["name"], "x": e["x"], "y": e["y"]} for e in edges]

    # --- TIME-VARYING storm envelope (Defect 2 realism) -------------------- #
    win_hr = float(duration_hr) if duration_hr and duration_hr > 0 else 24.0
    win_s = win_hr * 3600.0
    n_steps = max(int(round(win_hr)), 2)
    time_s = [round(float(i) * win_s / float(n_steps), 1) for i in range(n_steps + 1)]
    # Hs ramps 0 -> peak -> 0 on the raised-cosine bump; Tp scales with the
    # instantaneous Hs (steeper, longer-period seas at the storm peak), clamped.
    hs_floor = max(0.25, 0.15 * hs)  # never a fully-dead boundary off-peak
    hs_series_template: list[float] = []
    tp_series_template: list[float] = []
    for t in time_s:
        f = _wave_storm_envelope_factor(t, win_s)
        hs_t = round(hs_floor + (hs - hs_floor) * f, 3)
        tp_t = round(max(4.0, min(16.0, 3.86 * math.sqrt(max(hs_t, 0.1)))), 3)
        hs_series_template.append(hs_t)
        tp_series_template.append(tp_t)

    points: list[dict[str, Any]] = []
    for c in chosen:
        x, y = c["x"], c["y"]
        # SnapWave wd is nautical "coming FROM" (degrees clockwise from north),
        # confirmed against the SnapWave/SFINCS Fortran (cht_sfincs passes wd
        # through verbatim; the solver does `theta = 270 - wd`) and the Roelvink
        # 18th-Waves-Workshop-2025 decks (a west point peaks at 270 oN = waves
        # coming from the west). The boundary direction is the SEAWARD bearing
        # FROM the AOI centre TOWARD the boundary point.
        dx = float(x) - cx
        dy = float(y) - cy
        wd = (math.degrees(math.atan2(dx, dy))) % 360.0
        pt: dict[str, Any] = {
            "x": float(x),
            "y": float(y),
            "hs": round(hs, 3),  # PEAK scalar (worker seeds add_point with this)
            "tp": round(tp, 3),
            "wd": round(wd, 2),
            "ds": ds,
            # Time-varying override (shared time vector + per-point series). The
            # worker replaces the point's constant timeseries with these.
            "time_s": list(time_s),
            "hs_series": list(hs_series_template),
            "tp_series": list(tp_series_template),
        }
        if "depth_m" in c:
            pt["_prov_depth_m"] = round(float(c["depth_m"]), 2)
        points.append(pt)

    logger.info(
        "model_flood_scenario: synthesised PARAMETRIC SnapWave wave boundary for "
        "bbox=%s (return_period_yr=%s -> peak hs=%.2f m, tp=%.2f s, %d offshore "
        "points in EPSG:%s, %d time steps over %.0f hr, depth_aware=%s)",
        bbox,
        return_period_yr,
        hs,
        tp,
        len(points),
        target_epsg,
        len(time_s),
        win_hr,
        selected is not None,
    )
    return {
        "points": points,
        "_prov_synthetic_parametric": True,
        "_prov_hs_m": hs,
        "_prov_tp_s": tp,
        "_prov_return_period_yr": return_period_yr,
        "_prov_depth_aware": selected is not None,
        "_prov_time_varying": True,
    }


def _compose_and_upload_deckbuild_spec(
    *,
    bbox: tuple[float, float, float, float],
    topobathy_uri: str,
    bathymetry_present: bool,
    model_setup: Any,
    forcing_spec: Any,
    surge_forcing: dict[str, Any] | None,
    grid_resolution_m: float,
    duration_hr: float,
    buildings_uri: str | None = None,
    building_obstacle_mode: str = "thin_dams",
    rivers_uri: str | None = None,
    refinement_levels: int = 2,
    max_cells: int = 2_000_000,
    output_dt_s: float = 600.0,
    return_period_yr: int | float | None = None,
    is_coastal: bool = False,
) -> str:
    """Build the deck-build worker's input spec JSON + upload it; return its URI.

    The build_spec is the cht_sfincs deck-builder worker's ONLY input (the worker
    downloads it, authors the quadtree+SnapWave deck via cht_sfincs, uploads the
    deck + a deck manifest.json, and writes completion.json). It is composed
    ENTIRELY from artifacts the regular ``build_sfincs_model`` already produced
    (topobathy COG URI, grid params on ``model_setup.parameters``, materialised
    surge/wave forcing URIs) so the deck-build mirrors the regular build's inputs
    — only the authoring engine (refined quadtree + SnapWave) differs.

    The two known cht_sfincs caveats are baked in here so the worker reproduces
    the deck exactly:
      * CAVEAT 2 — ``snapwave.use_herbers = 1`` (the Herbers infragravity-wave
        run-up path; matches the deck-builder worker's validated default). Passed
        EXPLICITLY so agent + worker agree on the contract value.
      * CAVEAT 1 (SnapWave bnd time column) is owned by cht/the worker (the
        boundary time column is written as ``(time - tref).total_seconds()``
        there; do NOT post-correct it agent-side) — recorded here as
        ``snapwave.time_column_owned_by_cht = True`` so the worker honors it.

    The deck dir + manifest output URIs are co-located with the build_spec under
    the cache bucket's ``sfincs_deck/<id>/`` prefix; the manifest URI is the
    EXACT input the existing ``run_solver('sfincs', model_setup_uri=...)`` solve
    consumes. Returns the build_spec ``s3://.../build_spec.json`` URI.

    Raises ``DeckBuildError`` when the build_spec cannot be uploaded (no S3
    backend / upload failure) — honest typed failure, never a silent local
    fallback the GPL worker on Batch could not reach.
    """
    import json as _json
    import math

    from ..tools.cache import storage_scheme
    from ..tools.solver import SolverDispatchError as _DeckBuildError

    params = getattr(model_setup, "parameters", {}) or {}
    if not isinstance(params, dict):
        params = {}
    explicit_grid = params.get("grid")
    explicit_grid = explicit_grid if isinstance(explicit_grid, dict) else {}

    # --- target_epsg: the projected CRS the quadtree grid is authored in ---
    # Prefer an explicit epsg in params; else parse the EPSG int from the builder's
    # ``crs`` param (e.g. "EPSG:3857"); else derive the UTM zone from the bbox
    # centroid (matching ``fetch_topobathy``'s UTM default — a coastal AOI gets a
    # metric grid, not Web-Mercator). The deck-build worker REQUIRES an int.
    target_epsg = (
        explicit_grid.get("epsg")
        or params.get("epsg")
        or params.get("target_epsg")
    )
    if target_epsg is None:
        crs_str = str(params.get("crs") or "")
        if crs_str.upper().startswith("EPSG:"):
            try:
                target_epsg = int(crs_str.split(":", 1)[1])
            except (ValueError, IndexError):
                target_epsg = None
    if not target_epsg or int(target_epsg) in (4326, 3857):
        # Web-Mercator / geographic is unsuitable for a metric quadtree grid;
        # snap to the bbox-centroid UTM zone (northern hemisphere assumed for the
        # CONUS coastal North Star — matches fetch_topobathy).
        lon_c = (float(bbox[0]) + float(bbox[2])) / 2.0
        lat_c = (float(bbox[1]) + float(bbox[3])) / 2.0
        zone = int((lon_c + 180.0) // 6.0) + 1
        zone = max(1, min(60, zone))
        target_epsg = (32600 if lat_c >= 0 else 32700) + zone
    target_epsg = int(target_epsg)

    # --- base (coarsest) grid params x0/y0/nmax/mmax/dx/dy ---
    # The deck-build worker REQUIRES these (cht refines x2 per level off this
    # base). build_sfincs_model does NOT carry them on ModelSetup.parameters
    # (HydroMT computes the grid into the deck files), so derive a deterministic
    # base grid from the AOI bbox reprojected to target_epsg + grid_resolution_m.
    # The worker re-snaps/validates; this gives it a complete, valid base.
    dx = dy = float(grid_resolution_m)
    base_grid: dict[str, Any] = dict(explicit_grid)
    if not all(k in base_grid for k in ("x0", "y0", "nmax", "mmax", "dx", "dy")):
        try:
            from pyproj import Transformer  # type: ignore[import-not-found]

            tf = Transformer.from_crs(4326, target_epsg, always_xy=True)
            x_min, y_min = tf.transform(float(bbox[0]), float(bbox[1]))
            x_max, y_max = tf.transform(float(bbox[2]), float(bbox[3]))
        except Exception as exc:  # noqa: BLE001
            raise _DeckBuildError(
                f"could not reproject bbox {bbox} to EPSG:{target_epsg} for the "
                f"deck-build base grid: {exc}"
            ) from exc
        x0 = min(x_min, x_max)
        y0 = min(y_min, y_max)
        nmax = max(1, int(math.ceil(abs(x_max - x_min) / dx)))
        mmax = max(1, int(math.ceil(abs(y_max - y_min) / dy)))
        base_grid = {
            "x0": float(x0),
            "y0": float(y0),
            "nmax": int(nmax),
            "mmax": int(mmax),
            "dx": dx,
            "dy": dy,
            "rotation": float(base_grid.get("rotation", 0.0)),
        }
    base_grid["grid_resolution_m"] = float(grid_resolution_m)

    # --- tref/tstart/tstop (SFINCS "YYYYMMDD HHMMSS" strings) ---
    # Prefer the forcing provenance; else a deterministic window anchored at a
    # fixed reference spanning ``duration_hr`` (the worker parses these; tstop
    # must be after tstart). The worker owns the SnapWave bnd time column
    # (CAVEAT 1) relative to tref.
    forcing_provenance = dict(getattr(forcing_spec, "provenance", {}) or {})
    builder_provenance = params.get("forcing_provenance")
    builder_provenance = builder_provenance if isinstance(builder_provenance, dict) else {}

    def _pick_time(key: str) -> Any:
        return (
            forcing_provenance.get(key)
            or builder_provenance.get(key)
            or params.get(key)
        )

    tref = _pick_time("tref")
    tstart = _pick_time("tstart")
    tstop = _pick_time("tstop")
    if not (tref and tstart and tstop):
        # Deterministic fallback window (matches the builder's sfincs.inp anchor).
        span_h = max(1, int(round(float(duration_hr))))
        ref = "20260101 000000"
        end_day = 1 + (span_h // 24)
        end_hh = span_h % 24
        tref = tref or ref
        tstart = tstart or ref
        tstop = tstop or f"202601{end_day:02d} {end_hh:02d}0000"

    scheme = storage_scheme()
    cache_bucket = os.environ.get("GRACE2_CACHE_BUCKET") or CACHE_BUCKET
    deck_id = new_ulid()
    base_prefix = f"cache/static-30d/sfincs_deck/{deck_id}/"
    deck_dir_uri = f"{scheme}://{cache_bucket}/{base_prefix}deck/"
    deck_manifest_uri = f"{scheme}://{cache_bucket}/{base_prefix}manifest.json"
    build_spec_uri = f"{scheme}://{cache_bucket}/{base_prefix}build_spec.json"

    # --- QUADTREE FIX (issue 1): upload LOCAL forcing files to S3 -------------
    # The deck is built on a REMOTE Batch worker that can only download s3:// /
    # gs:// URIs. The auto-wired / parametric surge (and the CO-OPS/GTSM/NWM
    # adapter) wrote its bzs/bnd (+ dis/src) files to LOCAL agent-box paths
    # (/tmp/grace2-sfincs-forcing/...). Upload any LOCAL forcing file URIs under
    # this deck's prefix and rewrite the surge_forcing block to carry s3:// URIs
    # BEFORE it goes into the build_spec  -  else the worker crashes
    # "unsupported object URI scheme: '/tmp/...'" (run 01KVRJK7333NP2XC64...).
    surge_forcing = _upload_local_forcing_files_to_s3(
        surge_forcing,
        cache_bucket=cache_bucket,
        scheme=scheme,
        key_prefix=f"{base_prefix}forcing/",
    )

    # --- WAVES FIX (issue 2): parametric SnapWave wave boundary --------------
    # SnapWave needs an offshore incident wave boundary (Hs/Tp/dir) to produce a
    # wave field; without boundary POINTS the worker logs "no SnapWave boundary
    # points in spec - deck has no wave forcing" + "wavebnd=0" and hm0 stays flat
    # (run 01KVRJK7333NP2XC64PBHABZ11). For a COASTAL "surge with waves" run we
    # synthesise a parametric offshore wave boundary (mirrors the parametric surge
    # path: peak Hs scales with return_period_yr, points along the bbox edges in
    # the deck CRS). The worker derives the seaward open-boundary polygon from
    # these points so wavebnd>0 and the incident wave injects. Gated on
    # ``is_coastal`` and only when no wave boundary was already supplied  -  the
    # inland / pluvial path emits NO wave boundary (unchanged).
    if is_coastal:
        existing = surge_forcing.get("snapwave_boundary") if surge_forcing else None
        has_points = (
            isinstance(existing, dict) and bool(existing.get("points"))
        )
        if not has_points:
            # Depth-aware placement samples the SAME topobathy COG the deck uses;
            # the time-varying envelope spans ``duration_hr``. A
            # ``WaveBoundaryError`` (no edge reaches deep water) is raised as a
            # DeckBuildError below so the workflow surfaces an honest typed failed
            # envelope rather than a flat-zero wave field.
            try:
                wave_bc = _synthesize_parametric_wave_boundary(
                    bbox,
                    target_epsg=target_epsg,
                    return_period_yr=return_period_yr,
                    duration_hr=float(duration_hr),
                    topobathy_uri=topobathy_uri,
                )
            except WaveBoundaryError as exc:
                dberr = _DeckBuildError(str(exc))
                dberr.error_code = exc.error_code  # WAVE_BOUNDARY_NO_DEEP_WATER
                raise dberr from exc
            surge_forcing = dict(surge_forcing or {})
            surge_forcing["snapwave_boundary"] = wave_bc

    # --- auto-refinement + cell budget (combined-worker v2) ---
    # The agent does NOT do heavy GIS or import cht — it only declares the
    # refinement DEPTH (max levels, x2 each) + the cell budget; the combined
    # worker DERIVES the actual refinement polygons (e.g. a nearshore/AOI-interior
    # band carrying the 'refinement_level' int column cht.grid.build requires) and
    # enforces ``nr_cells <= max_cells`` after the build, erroring honestly if the
    # refined quadtree blows the budget rather than launching an oversized solve.
    refine_levels = max(0, int(refinement_levels))
    cell_budget = max(1, int(max_cells))
    base_grid["refinement_levels"] = refine_levels
    base_grid["max_cells"] = cell_budget

    build_spec: dict[str, Any] = {
        "schema_version": "v2",
        "deck_id": deck_id,
        "aoi": {
            "bbox": [float(b) for b in bbox],  # EPSG:4326
            "target_epsg": target_epsg,
        },
        "topobathy": {
            "cog_uri": topobathy_uri,
            "bathymetry_present": bool(bathymetry_present),
        },
        # Base (coarsest) grid x0/y0/nmax/mmax/dx/dy in target_epsg; cht refines
        # x2 per level off this base (up to ``refinement_levels``). The combined
        # worker derives the refinement polygons + enforces ``max_cells``.
        # Worker-required + complete.
        "grid": base_grid,
        "mask": {
            # Active + waterlevel-boundary mask window (domain-adaptive bounds
            # build_sfincs_model used; the worker mirrors them).
            "zmin": params.get("mask_zmin") if isinstance(params, dict) else None,
            "zmax": params.get("mask_zmax") if isinstance(params, dict) else None,
        },
        "snapwave": {
            # CAVEAT 2 — snapwave_use_herbers = 1 (the Herbers infragravity-wave
            # run-up path; the deck-builder worker's validated default is also 1).
            # The agent passes it EXPLICITLY so agent + worker agree on the
            # contract value rather than the agent silently overriding the
            # worker's default.
            "use_herbers": 1,
            # CAVEAT 1 — let cht own the SnapWave boundary time column; do NOT
            # post-correct to tref-relative (the boundary time column is written
            # as (time - tref).total_seconds() inside cht/the worker, NOT a raw
            # epoch column — the worker owns it).
            "time_column_owned_by_cht": True,
            "gamma": 0.8,
            "gammaig": 1.0,
            "gammax": 1.0,
            "dtheta": 15.0,
            "hmin": 0.1,
            "fw0": 0.01,
            "crit": 0.01,
            "igwaves": 1,
            "nrsweeps": 1,
            # DEFECT 2 FIX - SnapWave coupling cadence (dtwave). Without it SFINCS
            # defaults dtwave=3600 s (SnapWave re-solves HOURLY) while map output
            # is every output_dt -> ~12 byte-identical hm0 frames per re-solve, so
            # the wave animation is static ("literally nothing happening"). Pin it
            # to the FINE output cadence (capped at 600 s) so SnapWave re-solves
            # every output frame and the wave field actually evolves. The worker
            # threads this into v.dtwave; the agent can override via this knob.
            "dtwave": min(float(output_dt_s), 600.0),
        },
        "forcing": {
            "tref": tref,
            "tstart": tstart,
            "tstop": tstop,
            "duration_hours": float(duration_hr),
            # Materialised surge/wave/discharge forcing URIs (timeseries CSV +
            # locations geofile). The deck-builder reads these to write the
            # bzs/dis + snapwave.bnd/bhs/btp/bwd/bds files. ``None`` → that block
            # absent (a pure quadtree run with no surge boundary still valid).
            "surge_forcing": surge_forcing or {},
        },
        "output": {
            "deck_dir_uri": deck_dir_uri,
            "manifest_uri": deck_manifest_uri,
            # SFINCS map-output cadence (seconds). The combined worker writes the
            # flood field at this dt; the agent does not author the deck, just
            # declares the desired output stride.
            "output_dt": float(output_dt_s),
        },
    }

    # --- buildings-as-obstacles (combined-worker v2, OPTIONAL) ---
    # OSM building footprints (FlatGeobuf from fetch_buildings(bbox, source=osm)).
    # The agent only POINTS at the footprint geofile + names the burn MODE; the
    # combined worker reprojects + burns them (thin_dams = blocked uv-faces along
    # building edges; raise_subgrid = lift bed elevation under footprint cells;
    # exclude = mask footprint cells inactive). Absent when no footprints fetched.
    if buildings_uri:
        mode = (building_obstacle_mode or "thin_dams").strip().lower()
        if mode not in ("thin_dams", "raise_subgrid", "exclude"):
            mode = "thin_dams"
        build_spec["buildings"] = {
            "footprints_uri": buildings_uri,
            "mode": mode,
        }

    # --- rivers (combined-worker v2, OPTIONAL) ---
    # OSM waterway LineStrings (FlatGeobuf — same Overpass pattern fetch_roads_osm
    # uses). The combined worker may burn them as flow paths / refinement seeds.
    # Absent when no waterways fetched.
    if rivers_uri:
        build_spec["rivers"] = {"lines_uri": rivers_uri}

    payload = _json.dumps(build_spec, indent=2, default=str).encode("utf-8")

    if not build_spec_uri.startswith("s3://"):
        raise _DeckBuildError(
            "The cht_sfincs deck-build requires an S3 storage backend so the "
            "GPL deck-builder Batch worker can read the build_spec "
            "(GRACE2_STORAGE_BACKEND=s3). Composed build_spec URI was "
            f"{build_spec_uri!r} — staying inert."
        )
    try:
        from ..tools.solver import _get_s3_client

        s3 = _get_s3_client()
        s3_bucket, _, key = build_spec_uri[len("s3://"):].partition("/")
        s3.put_object(
            Bucket=s3_bucket,
            Key=key,
            Body=payload,
            ContentType="application/json",
        )
    except _DeckBuildError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _DeckBuildError(
            f"failed to upload the cht_sfincs deck-build build_spec to "
            f"{build_spec_uri}: {exc}"
        ) from exc

    logger.info(
        "composed + uploaded cht_sfincs combined-quadtree build_spec -> %s "
        "(deck_manifest=%s, use_herbers=1, refinement_levels=%d, max_cells=%d, "
        "buildings=%s, rivers=%s)",
        build_spec_uri,
        deck_manifest_uri,
        refine_levels,
        cell_budget,
        bool(buildings_uri),
        bool(rivers_uri),
    )
    return build_spec_uri


# --------------------------------------------------------------------------- #
# REGULAR-GRID (pluvial) BUILD offload — compose a job_spec, submit ONE combined
# hydromt-BUILD + SFINCS-solve Batch job (heavy-compute offload, the reference
# implementation). Mirrors the quadtree ``_compose_and_upload_deckbuild_spec`` +
# ``run_sfincs_quadtree`` pattern, but for the regular-grid ``build_sfincs_model``
# path: the agent stops running hydromt in-process (the 16 GB driver) and instead
# hands the worker the already-fetched input COG URIs + the serialized forcing /
# options. See reports/design/heavy-compute-offload-2026-07-02.md.
# --------------------------------------------------------------------------- #


#: Forcing-member file-URI keys the worker downloads (superset of the quadtree
#: helper's — also covers wind/pressure ``grid_uri`` + infiltration rasters). A
#: LOCAL value (a /tmp adapter/parametric file) must be uploaded to S3 before it
#: enters the job_spec, else the remote worker cannot read it.
_FLOOD_BUILD_FORCING_FILE_KEYS = (
    "timeseries_uri",
    "locations_uri",
    "geodataset_uri",
    "rivers_uri",
    "hydrography_uri",
    "grid_uri",
    "cn_uri",
    "lulc_uri",
    "reclass_table_uri",
)


def _sfincs_build_offload_enabled() -> bool:
    """True when the pluvial SFINCS build should run on the Batch worker.

    Gated OFF by default (``GRACE2_SFINCS_BUILD_OFFLOAD`` unset) so live behavior
    is byte-identical to the legacy in-agent build until NATE rebuilds+deploys the
    ``grace2-sfincs`` image (which now bundles hydromt) and flips the flag — the
    same inert-until-provisioned discipline the quadtree path uses. A truthy value
    (``1``/``on``/``true``/``yes``) activates it."""
    return (os.environ.get("GRACE2_SFINCS_BUILD_OFFLOAD") or "").strip().lower() in {
        "1",
        "on",
        "true",
        "yes",
    }


def _forcing_member_to_dict(member: Any) -> dict[str, Any] | None:
    """Serialize a ForcingSpec sub-member dataclass (or None) to a plain dict."""
    if member is None:
        return None
    from dataclasses import asdict, is_dataclass

    if is_dataclass(member):
        return asdict(member)
    if isinstance(member, dict):
        return dict(member)
    return None


def _forcing_spec_to_dict(forcing: "ForcingSpec") -> dict[str, Any]:
    """Serialize a ``ForcingSpec`` to the flat job_spec ``forcing`` dict.

    Scalar fields verbatim; each surge/discharge/wind/pressure/infiltration member
    is a nested dict (``None`` -> absent). Round-trips through
    ``_sfincs_build.deck.forcing_spec_from_dict`` in the worker.
    """
    return {
        "forcing_type": forcing.forcing_type,
        "precip_inches": forcing.precip_inches,
        "duration_hours": forcing.duration_hours,
        "return_period_years": forcing.return_period_years,
        "precip_magnitude_mm_per_hr": forcing.precip_magnitude_mm_per_hr,
        "waterlevel": _forcing_member_to_dict(forcing.waterlevel),
        "discharge": _forcing_member_to_dict(forcing.discharge),
        "breach": _forcing_member_to_dict(forcing.breach),
        "wind": _forcing_member_to_dict(forcing.wind),
        "pressure": _forcing_member_to_dict(forcing.pressure),
        # SPIDERWEB (2026-07-19): cloud-offload mirror. The worker localizes
        # ``spw_uri`` into the deck; a local-only ``spw_path`` is dropped (the
        # offload path stages the .spw to the object store as ``spw_uri``).
        "wind_spiderweb": _forcing_member_to_dict(forcing.wind_spiderweb),
        "infiltration": _forcing_member_to_dict(forcing.infiltration),
        "provenance": dict(forcing.provenance or {}),
    }


def _build_options_to_dict(options: "BuildOptions") -> dict[str, Any]:
    """Serialize ``BuildOptions`` to the job_spec ``options`` dict (worker
    reconstructs via ``_sfincs_build.deck.build_options_from_dict``)."""
    return {
        "grid_resolution_m": options.grid_resolution_m,
        "simulation_hours": options.simulation_hours,
        "crs": options.crs,
        "compute_class": options.compute_class,
        "autoscale_grid": options.autoscale_grid,
        "output_interval_min": options.output_interval_min,
        "enable_subgrid": options.enable_subgrid,
        "subgrid_nr_subgrid_pixels": options.subgrid_nr_subgrid_pixels,
        "building_obstacle_uri": options.building_obstacle_uri,
        "building_obstacle_mode": options.building_obstacle_mode,
        "advanced_physics": options.advanced_physics,
    }


def _stage_local_forcing_files_full(
    forcing_dict: dict[str, Any],
    *,
    cache_bucket: str,
    scheme: str,
    key_prefix: str,
) -> dict[str, Any]:
    """Upload any LOCAL forcing FILE URIs to S3 + rewrite in place (full key set).

    Like ``_upload_local_forcing_files_to_s3`` but over the serialized
    ``forcing`` dict (scalars + member sub-dicts) and the SUPERSET key list (wind/
    pressure grids + infiltration rasters). Already-remote (s3:///gs://) and
    non-file fields pass through. Raises ``DeckBuildError`` on a missing local file
    / upload failure (honest typed failure)."""
    from ..tools.solver import SolverDispatchError as _DeckBuildError

    s3 = None
    out: dict[str, Any] = {}
    for member_name, member in forcing_dict.items():
        if not isinstance(member, dict):
            out[member_name] = member
            continue
        new_member = dict(member)
        for key in _FLOOD_BUILD_FORCING_FILE_KEYS:
            uri = new_member.get(key)
            if not uri or not isinstance(uri, str) or _is_remote_object_uri(uri):
                continue
            local_path = uri[len("file://"):] if uri.startswith("file://") else uri
            if not os.path.isfile(local_path):
                raise _DeckBuildError(
                    f"forcing file for {member_name}.{key} is a LOCAL path the "
                    f"remote build worker cannot read and it does not exist: {uri!r}"
                )
            filename = os.path.basename(local_path)
            s3_key = f"{key_prefix}{member_name}/{filename}"
            s3_uri = f"{scheme}://{cache_bucket}/{s3_key}"
            try:
                if s3 is None:
                    from ..tools.solver import _get_s3_client

                    s3 = _get_s3_client()
                with open(local_path, "rb") as fh:
                    s3.put_object(
                        Bucket=cache_bucket, Key=s3_key, Body=fh.read(),
                        ContentType="application/octet-stream",
                    )
            except _DeckBuildError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise _DeckBuildError(
                    f"failed to upload forcing file {member_name}.{key} "
                    f"({local_path}) to {s3_uri}: {exc}"
                ) from exc
            new_member[key] = s3_uri
        out[member_name] = new_member
    return out


def _compose_and_upload_flood_build_spec(
    *,
    bbox: tuple[float, float, float, float],
    dem_uri: str,
    landcover_uri: str,
    river_uri: str | None,
    forcing_spec: "ForcingSpec",
    options: "BuildOptions",
    nlcd_vintage_year: int | None,
) -> ModelSetup:
    """Compose + upload the SFINCS BUILD job_spec; return a build-spec ModelSetup.

    Replaces the in-agent ``build_sfincs_model`` on the pluvial (regular-grid)
    path when the offload is enabled. Runs the OQ-4 §4 NLCD validation gate
    PRE-SUBMIT (light read of the already-fetched landcover -> a
    ``LULC_MAPPING_MISMATCH`` here surfaces as the SAME failed envelope the
    in-agent build produced), computes the bbox-only autoscale estimate (for Batch
    sizing + telemetry; the worker re-does the DEM-active autoscale for real),
    uploads any LOCAL forcing files to S3, serializes forcing + options + input
    URIs into the ``job_spec``, uploads it, and returns a ``ModelSetup`` whose
    ``setup_uri`` is the job_spec URI and whose ``parameters['sfincs_build_spec']``
    marks the combined build+solve dispatch. The DEM/NetCDF are NEVER loaded here.

    Raises ``SFINCSSetupError`` (NLCD gate / mapping load) — caught by the
    composer's existing handler — and ``DeckBuildError`` (storage backend not S3,
    forcing upload, spec upload).
    """
    from ..tools.cache import storage_scheme
    from ..tools.solver import SolverDispatchError as _DeckBuildError

    # --- PRE-SUBMIT NLCD validation gate (Invariant 7; light landcover read) ---
    mapping = load_manning_mapping()
    fetched_classes = _extract_unique_nlcd_classes(landcover_uri)
    if nlcd_vintage_year is not None:
        validate_nlcd_vintage_against_mapping(
            fetched_classes=fetched_classes,
            nlcd_vintage_year=int(nlcd_vintage_year),
            mapping=mapping,
        )

    # --- bbox-only autoscale estimate (sizing + telemetry) ---
    autoscale = suggest_sfincs_resolution_from_bbox(
        bbox,
        base_resolution_m=options.grid_resolution_m,
        compute_class=options.compute_class,
    )

    scheme = storage_scheme()
    if scheme != "s3":
        raise _DeckBuildError(
            "The SFINCS build offload requires an S3 storage backend so the Batch "
            "worker can read the job_spec (GRACE2_STORAGE_BACKEND=s3). Staying inert."
        )
    cache_bucket = os.environ.get("GRACE2_CACHE_BUCKET") or CACHE_BUCKET
    spec_id = new_ulid()
    base_prefix = f"cache/static-30d/sfincs_build/{spec_id}/"
    job_spec_uri = f"{scheme}://{cache_bucket}/{base_prefix}sfincs_build_spec.json"

    forcing_dict = _stage_local_forcing_files_full(
        _forcing_spec_to_dict(forcing_spec),
        cache_bucket=cache_bucket,
        scheme=scheme,
        key_prefix=f"{base_prefix}forcing/",
    )

    job_spec: dict[str, Any] = {
        "schema_version": 1,
        "engine": "sfincs",
        "spec_id": spec_id,
        "bbox": [float(b) for b in bbox],
        "nlcd_vintage_year": nlcd_vintage_year,
        "inputs": {
            "dem_uri": dem_uri,
            "landcover_uri": landcover_uri,
            "river_uri": river_uri,
        },
        "forcing": forcing_dict,
        "options": _build_options_to_dict(options),
    }
    payload = json.dumps(job_spec, indent=2).encode("utf-8")
    try:
        from ..tools.solver import _get_s3_client

        s3 = _get_s3_client()
        s3_bucket, _, key = job_spec_uri[len("s3://"):].partition("/")
        s3.put_object(
            Bucket=s3_bucket, Key=key, Body=payload, ContentType="application/json"
        )
    except _DeckBuildError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _DeckBuildError(
            f"failed to upload the SFINCS build job_spec to {job_spec_uri}: {exc}"
        ) from exc

    logger.info(
        "SFINCS build offload: composed job_spec -> %s (est_res=%.1fm est_cells=%s "
        "est_solve=%ss forcing_type=%s)",
        job_spec_uri,
        autoscale.grid_resolution_m,
        autoscale.estimated_active_cells,
        autoscale.estimated_solve_seconds,
        forcing_spec.forcing_type,
    )
    return ModelSetup(
        setup_id=new_ulid(),
        solver="sfincs",
        setup_uri=job_spec_uri,
        grid_resolution_m=autoscale.grid_resolution_m,
        bbox=bbox,
        parameters={
            # The build+solve dispatch marker Step 6 branches on.
            "sfincs_build_spec": True,
            "crs": options.crs,
            "simulation_hours": options.simulation_hours,
            "output_interval_min": options.output_interval_min,
            "nlcd_vintage_year": nlcd_vintage_year,
            "fetched_classes": sorted(fetched_classes),
            "forcing_type": forcing_spec.forcing_type,
            "forcing_provenance": dict(forcing_spec.provenance),
            "compute_class": options.compute_class,
            # bbox-only estimate (the worker's manifest carries the real
            # DEM-active autoscale); shape matches build_sfincs_model's block so
            # _extract_solve_autoscale + the solve-telemetry read it uniformly.
            "autoscale": {
                "grid_resolution_m": autoscale.grid_resolution_m,
                "estimated_active_cells": autoscale.estimated_active_cells,
                "estimated_active_cells_at_base": autoscale.estimated_active_cells_at_base,
                "cell_cap": autoscale.cell_cap,
                "vcpus": autoscale.vcpus,
                "base_resolution_m": autoscale.base_resolution_m,
                "estimated_solve_seconds": autoscale.estimated_solve_seconds,
                "coarsened": autoscale.coarsened,
                "reason": autoscale.reason,
                "estimate_only": True,
            },
        },
        created_at=datetime.now(timezone.utc),
    )


# --------------------------------------------------------------------------- #
# job-0225 v2 — real-precip forcing branch (area-mean netamt)
# --------------------------------------------------------------------------- #


class PrecipForcingError(RuntimeError):
    """Raised when the observed-precip-raster forcing path cannot be computed.

    Carries an A.6 open-set ``error_code`` so the workflow surface lifts it
    into a failed AssessmentEnvelope (same pattern as ``SFINCSSetupError``).
    Codes:
    - ``PRECIP_RASTER_READ_FAILED`` — the raster bytes were unreadable.
    - ``PRECIP_RASTER_EMPTY`` — the raster had no valid (non-nodata) cells in
      the domain → no area-mean is computable.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def compute_precip_area_mean_mm_per_hr(
    forcing_raster_uri: str,
    bbox: tuple[float, float, float, float],
    accumulation_hours: float,
    *,
    raster_units: str = "mm",
) -> tuple[float, float]:
    """Compute the AREA-MEAN accumulated precip over the model domain → mm/hr.

    job-0225 v2 (OQ-6 netamt fallback). Reads the precipitation raster at
    ``forcing_raster_uri`` (an accumulated-precip COG — MRMS QPE, ERA5,
    gridMET, …), computes the mean over all valid cells, and converts that
    single domain-mean accumulated depth into a uniform SFINCS ``netamt``
    rate in **mm/hr** by dividing by the ``accumulation_hours`` window.

    This collapses the raster's spatial structure to one number — the v0.1
    netamt fallback locked by manifest OQ-6. The spw spatially-varying-precip
    upgrade path (ingest the raster as a 2D time grid) is documented in
    ``sfincs_builder._generate_hydromt_yaml_config`` + this job's report.md.

    Domain handling (v0.1): we average over EVERY valid cell in the raster.
    The fetchers that produce the precip raster (e.g. ``fetch_mrms_qpe``) clip
    to roughly the requested bbox already, so the raster footprint ≈ the model
    domain. A future refinement would window-read the raster to the exact bbox
    before averaging (captured as OQ-225-EXACT-DOMAIN-WINDOW); for v0.1 the
    whole-raster mean is the documented behavior.

    Args:
        forcing_raster_uri: ``gs://...`` (or local path / ``/vsigs/...``) URI
            of the accumulated-precip COG.
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` — the model domain.
            Carried for provenance + future exact-window cropping; v0.1 uses
            the whole-raster mean.
        accumulation_hours: the precip accumulation window in hours (e.g. 24
            for a 24h QPE product). The area-mean accumulated depth is divided
            by this to yield mm/hr. Must be positive.
        raster_units: declared units of the raster values. Default ``"mm"``
            (the MRMS/ERA5/gridMET convention used by our fetchers). If
            ``"inches"`` the mean is multiplied by 25.4 to reach mm before the
            per-hour conversion.

    Returns:
        ``(magnitude_mm_per_hr, area_mean_mm)`` — the uniform SFINCS netamt
        rate AND the area-mean accumulated depth in mm (echoed into forcing
        provenance for narration).

    Raises:
        PrecipForcingError("PRECIP_RASTER_READ_FAILED"): the read failed.
        PrecipForcingError("PRECIP_RASTER_EMPTY"): no valid cells.
        ValueError: ``accumulation_hours <= 0``.
    """
    if accumulation_hours <= 0:
        raise ValueError(
            f"accumulation_hours must be positive; got {accumulation_hours!r}"
        )
    try:
        import numpy as np  # type: ignore[import-not-found]
        import rasterio  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise PrecipForcingError(
            "PRECIP_RASTER_READ_FAILED",
            f"rasterio/numpy not available for precip area-mean: {exc}",
        ) from exc

    # Scheme dispatch for the forcing-raster read:
    #   s3://  — boto3 stage-then-open (sprint-14-aws / job-0293c). GDAL's
    #            /vsis3/ credential chain does NOT resolve the EC2 instance role
    #            in this env (boto3 does) — observed live: "does not exist" on an
    #            existing object. Stage the bytes via the shared boto3 reader and
    #            open in-memory (MemoryFile frees with the dataset; no temp-file
    #            leak — mirrors extract_landcover_class._open_source). The MRMS
    #            COG is bbox-clipped/small, so a whole-file fetch is safe.
    #   gs:// / /vsigs/ / file:// / local — keep the GDAL /vsigs/ path (job-0170
    #            — keeps the fragile gcsfs path out of the read; local pass-through).
    try:
        if forcing_raster_uri.startswith("s3://"):
            from rasterio.io import MemoryFile  # type: ignore[import-not-found]

            from ..tools.cache import read_object_bytes_s3

            with MemoryFile(read_object_bytes_s3(forcing_raster_uri)) as mf:
                with mf.open() as src:
                    arr = src.read(1).astype("float64")
                    nodata = src.nodata
        else:
            read_path = _to_vsigs(forcing_raster_uri)
            with rasterio.open(read_path) as src:
                arr = src.read(1).astype("float64")
                nodata = src.nodata
    except Exception as exc:  # noqa: BLE001
        raise PrecipForcingError(
            "PRECIP_RASTER_READ_FAILED",
            f"rasterio.open({forcing_raster_uri}) failed: {exc}",
        ) from exc

    # Mask nodata + common sentinels + non-finite values. Negative precip is
    # physically invalid (some products use negatives as fill) — mask those
    # too so they don't drag the mean.
    mask = np.isfinite(arr)
    if nodata is not None:
        mask &= arr != nodata
    mask &= arr != -9999.0
    mask &= arr >= 0.0
    valid = arr[mask]
    if valid.size == 0:
        raise PrecipForcingError(
            "PRECIP_RASTER_EMPTY",
            f"precip raster {forcing_raster_uri} has no valid cells over the "
            f"domain {bbox} — no area-mean computable",
        )

    area_mean = float(valid.mean())
    if raster_units == "inches":
        area_mean_mm = area_mean * 25.4
    else:
        area_mean_mm = area_mean
    magnitude_mm_per_hr = area_mean_mm / accumulation_hours
    logger.info(
        "precip area-mean: raster=%s valid_cells=%d mean=%.4f %s "
        "(%.4f mm) / %.2f hr → %.6f mm/hr",
        forcing_raster_uri,
        int(valid.size),
        area_mean,
        raster_units,
        area_mean_mm,
        accumulation_hours,
        magnitude_mm_per_hr,
    )
    return magnitude_mm_per_hr, area_mean_mm


# --------------------------------------------------------------------------- #
# COASTAL SFINCS — surge-forcing member construction (engine plumbing)
# --------------------------------------------------------------------------- #


def _build_surge_forcing_members(
    surge_forcing: dict[str, Any] | None,
) -> tuple[
    WaterlevelForcing | None,
    DischargeForcing | None,
    WindForcing | None,
    PressureForcing | None,
]:
    """Translate the workflow ``surge_forcing`` dict into typed ``ForcingSpec`` members.

    The COASTAL SFINCS North Star couples surge / tide / discharge / wind /
    pressure forcing into the SFINCS deck. The workflow caller (or a future
    fetcher-plumbing step that materialises ``fetch_gtsm_tide_surge`` /
    ``fetch_noaa_coops_tides`` / ``fetch_noaa_nwm_streamflow`` /
    ``fetch_cama_flood_discharge`` hydrographs to CSV + locations) supplies a
    nested dict::

        {
          "waterlevel": {"timeseries_uri": ..., "locations_uri": ...,
                          "geodataset_uri": ..., "offset": ..., "buffer_m": ...},
          "discharge":  {"timeseries_uri": ..., "locations_uri": ...,
                          "rivers_uri": ..., "hydrography_uri": ...,
                          "river_upa_km2": ...},
          "wind":       {"magnitude": ..., "direction": ...} | {"grid_uri": ...},
          "pressure":   {"grid_uri": ..., "fill_value": ...},
        }

    Any subset of keys may be present; an absent / empty sub-dict yields ``None``
    for that member (no block emitted). Unknown keys inside a sub-dict are
    ignored so a forward-compatible caller can't crash the build. Returns the
    four typed members ready to drop onto ``ForcingSpec``.
    """
    if not surge_forcing:
        return None, None, None, None

    def _sub(name: str) -> dict[str, Any]:
        v = surge_forcing.get(name)
        return dict(v) if isinstance(v, dict) else {}

    wl_raw = _sub("waterlevel")
    waterlevel = (
        WaterlevelForcing(
            timeseries_uri=wl_raw.get("timeseries_uri"),
            locations_uri=wl_raw.get("locations_uri"),
            geodataset_uri=wl_raw.get("geodataset_uri"),
            offset=wl_raw.get("offset"),
            buffer_m=wl_raw.get("buffer_m"),
            provenance={k: v for k, v in wl_raw.items() if k.startswith("_prov")},
        )
        if wl_raw and (
            wl_raw.get("timeseries_uri") or wl_raw.get("geodataset_uri")
        )
        else None
    )

    dq_raw = _sub("discharge")
    discharge = (
        DischargeForcing(
            timeseries_uri=dq_raw.get("timeseries_uri"),
            locations_uri=dq_raw.get("locations_uri"),
            rivers_uri=dq_raw.get("rivers_uri"),
            hydrography_uri=dq_raw.get("hydrography_uri"),
            river_upa_km2=dq_raw.get("river_upa_km2"),
        )
        if dq_raw and (
            dq_raw.get("timeseries_uri")
            or dq_raw.get("rivers_uri")
            or dq_raw.get("hydrography_uri")
        )
        else None
    )

    wd_raw = _sub("wind")
    wind = (
        WindForcing(
            magnitude=wd_raw.get("magnitude"),
            direction=wd_raw.get("direction"),
            grid_uri=wd_raw.get("grid_uri"),
        )
        if wd_raw
        and (
            wd_raw.get("grid_uri")
            or (wd_raw.get("magnitude") is not None and wd_raw.get("direction") is not None)
        )
        else None
    )

    pr_raw = _sub("pressure")
    pressure = (
        PressureForcing(
            grid_uri=pr_raw["grid_uri"],
            fill_value=pr_raw.get("fill_value"),
        )
        if pr_raw and pr_raw.get("grid_uri")
        else None
    )

    return waterlevel, discharge, wind, pressure


def _resolve_surge_forcing_from_fetchers(
    surge_forcing: dict[str, Any] | None,
    bbox: tuple[float, float, float, float],
    *,
    window_hours: float | None = None,
    data_sources: list[DataSource] | None = None,
) -> dict[str, Any] | None:
    """Materialise RAW fetcher outputs in ``surge_forcing`` into deck-ready URIs.

    This is the fetcher → ADAPTER bridge: it lets a caller hand
    ``model_flood_scenario`` the RAW surge/discharge fetcher outputs (a GTSM /
    CO-OPS / NWM ``LayerURI`` or FlatGeobuf URI, or a CaMa-Flood COG) instead of
    pre-materialised bzs/dis CSV + locations files. The adapter
    (``sfincs_forcing_adapter``) converts each hydrograph into the SFINCS
    ``timeseries_uri`` + ``locations_uri`` pair that
    ``_build_surge_forcing_members`` → the deck-emission seam expects.

    Recognised RAW keys inside a sub-dict (in addition to the already-materialised
    ``timeseries_uri`` / ``locations_uri`` / ``geodataset_uri`` the deck consumes
    verbatim):

    - ``waterlevel.fetch_uri`` (or ``fgb_uri``) — a GTSM / CO-OPS FlatGeobuf to
      adapt into bzs files. Optional ``offset`` / ``buffer_m`` pass through.
    - ``discharge.fetch_uri`` (or ``fgb_uri``) — an NWM FlatGeobuf to adapt into
      dis files. Optional ``cama_cog_uri`` instead → sample the CaMa COG.
      ``rivers_uri`` / ``hydrography_uri`` / ``river_upa_km2`` pass through.

    A sub-dict that ALREADY carries ``timeseries_uri`` / ``geodataset_uri`` is
    left untouched (the pre-materialised path — backward compatible). Returns the
    surge_forcing dict with raw inputs replaced by materialised URIs, or the
    input unchanged when there is nothing to adapt. ``None`` → ``None``.

    Adapter failures are NOT swallowed here: a surge event the user explicitly
    requested that cannot be materialised must surface as a typed failed envelope
    (the workflow's Step-5 try/except catches ``SFINCSForcingAdapterError`` and
    threads its ``error_code``), NOT silently degrade to a pluvial-only deck
    (Invariant 7 — never a silent wrong answer for an explicit surge request).
    """
    if not surge_forcing:
        return surge_forcing
    from .sfincs_forcing_adapter import (
        discharge_forcing_from_cama_cog,
        discharge_forcing_from_fgb,
        waterlevel_forcing_from_fgb,
    )

    out = dict(surge_forcing)

    wl = surge_forcing.get("waterlevel")
    if isinstance(wl, dict):
        wl_fetch = wl.get("fetch_uri") or wl.get("fgb_uri")
        already = wl.get("timeseries_uri") or wl.get("geodataset_uri")
        if wl_fetch and not already:
            materialised = waterlevel_forcing_from_fgb(
                wl_fetch,
                window_hours=window_hours,
                offset=wl.get("offset"),
                buffer_m=wl.get("buffer_m"),
            )
            out["waterlevel"] = materialised
            if data_sources is not None:
                data_sources.append(
                    DataSource(
                        name="Water-level forcing (GTSM/CO-OPS → SFINCS bzs)",
                        uri=str(wl_fetch),
                        accessed_at=datetime.now(timezone.utc),
                    )
                )

    dq = surge_forcing.get("discharge")
    if isinstance(dq, dict):
        dq_fetch = dq.get("fetch_uri") or dq.get("fgb_uri")
        cama_uri = dq.get("cama_cog_uri")
        already = dq.get("timeseries_uri") or dq.get("geodataset_uri")
        if dq_fetch and not already:
            # UNIT WIRING (Invariant-7 silent-wrong-physics guard): SFINCS dis is
            # m^3/s. A USGS NWIS hydrograph FGB carries discharge in ft^3/s (cfs);
            # NWM's streamflow_cms is already metric. discharge_forcing_from_fgb
            # defaults value_unit="cms", so a USGS hydrograph routed through here
            # WITHOUT a unit would be fed ~35.3x too large. Thread an explicit
            # value_unit, inferring cfs for USGS/NWIS sources when not supplied.
            dq_unit = dq.get("value_unit")
            if not dq_unit:
                _src = str(dq_fetch).lower()
                dq_unit = "cfs" if ("usgs" in _src or "nwis" in _src) else "cms"
            out["discharge"] = discharge_forcing_from_fgb(
                dq_fetch,
                window_hours=window_hours,
                rivers_uri=dq.get("rivers_uri"),
                hydrography_uri=dq.get("hydrography_uri"),
                river_upa_km2=dq.get("river_upa_km2"),
                value_unit=dq_unit,
            )
            if data_sources is not None:
                data_sources.append(
                    DataSource(
                        name="River-discharge forcing (USGS/NWM → SFINCS dis)",
                        uri=str(dq_fetch),
                        accessed_at=datetime.now(timezone.utc),
                    )
                )
        elif cama_uri and not already:
            out["discharge"] = discharge_forcing_from_cama_cog(
                cama_uri,
                bbox,
                window_hours=window_hours,
            )
            if data_sources is not None:
                data_sources.append(
                    DataSource(
                        name="River-discharge forcing (CaMa-Flood → SFINCS dis)",
                        uri=str(cama_uri),
                        accessed_at=datetime.now(timezone.utc),
                    )
                )

    return out


# --------------------------------------------------------------------------- #
# COASTAL SFINCS  -  auto-wire a time-varying sea-surge water-level boundary
# --------------------------------------------------------------------------- #

# Parametric design-storm surge scaling. The peak surge above the tidal datum
# (metres) is a smooth, monotone function of the design-storm return period so a
# "major hurricane / 100-yr" event shows a real, visually-meaningful multi-metre
# surge marching inland, while a frequent (2-yr) event shows only a modest rise.
# Anchored to published Gulf-coast storm-tide observations (Hurricane Michael at
# Mexico Beach peaked near 4 m NAVD88)  -  the 100-yr anchor sits at ~3.5 m, with a
# gentle log-scaling above/below so the curve never goes negative or runaway.
# Tunable via env for ops without a code change.
_SURGE_PEAK_M_AT_100YR = float(os.getenv("GRACE2_SURGE_PEAK_M_AT_100YR", "3.5"))
_SURGE_PEAK_M_FLOOR = float(os.getenv("GRACE2_SURGE_PEAK_M_FLOOR", "0.6"))
_SURGE_PEAK_M_CEIL = float(os.getenv("GRACE2_SURGE_PEAK_M_CEIL", "7.5"))


def _parametric_surge_peak_m(return_period_yr: int | float | None) -> float:
    """Peak design-storm surge height (m above datum) for a return period.

    Monotone log-scaling anchored at the 100-yr peak (``_SURGE_PEAK_M_AT_100YR``):
    a larger ARI -> a higher peak, a smaller ARI -> a lower peak, clamped to a
    sane [floor, ceil] window so a degenerate / huge ARI can't drive a negative
    or runaway surge. ``log10(rp/100)`` gives 0 at 100-yr, +1 decade -> +scale,
    -1 decade -> -scale; a 0.9 m/decade slope puts 10-yr near ~2.6 m, 500-yr near
    ~4.1 m, 1000-yr near ~4.4 m  -  a realistic Gulf-coast spread.
    """
    import math

    rp = float(return_period_yr) if return_period_yr else 100.0
    rp = max(rp, 1.0)
    peak = _SURGE_PEAK_M_AT_100YR + 0.9 * math.log10(rp / 100.0)
    return max(_SURGE_PEAK_M_FLOOR, min(_SURGE_PEAK_M_CEIL, peak))


def _synthesize_parametric_surge_forcing(
    bbox: tuple[float, float, float, float],
    *,
    duration_hr: float,
    return_period_yr: int | float | None,
) -> dict[str, Any]:
    """LAST-RESORT parametric design-storm surge -> materialised bzs files dict.

    With no CO-OPS station and no CDS key (the only fully offline / key-free
    deterministic path), synthesise a smooth surge hydrograph: a base ramp that
    rises to a single peak near mid-event then recedes (a raised-cosine bump on a
    small tidal-mean offset), driven onto a handful of offshore boundary points
    laid along the SEAWARD edge of the bbox. The peak scales with
    ``return_period_yr`` via ``_parametric_surge_peak_m`` so a major-hurricane ARI
    yields a real multi-metre surge.

    Returns the SAME materialised dict shape ``waterlevel_forcing_from_fgb``
    produces (``{"timeseries_uri": <bzs.csv>, "locations_uri": <bnd.fgb>}``), so it
    flows verbatim through ``_build_surge_forcing_members`` -> a NON-None
    ``WaterlevelForcing`` (``timeseries_uri`` is set, which is the gate). The files
    are written via the SAME ``write_bzs_timeseries_csv`` / ``write_locations_fgb``
    seam the fetcher adapter uses, so the deck consumes them unchanged.
    """
    import math

    from .sfincs_forcing_adapter import (
        SFINCS_TREF,
        ReanchoredSeries,
        StationHydrograph,
        write_bzs_timeseries_csv,
        write_locations_fgb,
    )

    min_lon, min_lat, max_lon, max_lat = bbox
    peak_m = _parametric_surge_peak_m(return_period_yr)
    # Small tidal-mean offset the surge rides on (a modest high-tide baseline so
    # the boundary water level is never below the datum even off-peak).
    base_m = 0.3
    win_hr = float(duration_hr) if duration_hr and duration_hr > 0 else 24.0

    # --- RISING-LIMB ramp-and-hold hydrograph -------------------------------
    # A SYMMETRIC raised-cosine bump (peak at mid-event, 0 at both ends) makes the
    # surge crest at win_hr/2 and then DRAIN back to base by the window end -- the
    # peak map captures a transient that has already pushed the front fully inland
    # at the FIRST output frame, so the wet-front-advance test reads ratio ~1.0
    # (no march). Instead drive a clear RISING LIMB: hold at the tidal base for a
    # short pre-storm lead, ramp smoothly (raised half-cosine S-curve) up to the
    # full peak over the first ~40% of the window, then HOLD near the peak for the
    # remainder (a gentle final taper avoids a hard boundary discontinuity at
    # tstop). The flood therefore MARCHES inland across frames as the boundary
    # climbs, and the sustained hold lets the inundation reach its full connected
    # extent + berm runup rather than a transient crest.
    lead_hr = min(0.5, 0.05 * win_hr)            # brief pre-storm tidal lead
    rise_hr = max(1.0, 0.40 * win_hr)            # ramp base -> peak over ~40%
    rise_end_hr = lead_hr + rise_hr
    taper_hr = min(0.5, 0.05 * win_hr)           # soft easing into tstop
    taper_start_hr = max(rise_end_hr, win_hr - taper_hr)
    taper_floor = 0.92                           # never drop below 92% of peak

    # FINE sampling (~6 min) so the rising limb resolves across the minute-scale
    # output cadence (>= 2 samples so set_forcing_1d accepts it).
    _sample_s = 360.0
    n_steps = max(int(round(win_hr * 3600.0 / _sample_s)), 2)
    secs = [float(i) * (win_hr * 3600.0) / float(n_steps) for i in range(n_steps + 1)]
    values: list[float] = []
    for s in secs:
        hr = s / 3600.0
        if hr <= lead_hr:
            frac = 0.0
        elif hr < rise_end_hr:
            # raised half-cosine S-curve from 0 -> 1 across the rise window.
            x = (hr - lead_hr) / rise_hr
            frac = 0.5 * (1.0 - math.cos(math.pi * x))
        elif hr < taper_start_hr:
            frac = 1.0
        else:
            # gentle ease-down to taper_floor over the last taper window.
            x = (hr - taper_start_hr) / max(taper_hr, 1e-6)
            x = max(0.0, min(1.0, x))
            frac = 1.0 - (1.0 - taper_floor) * 0.5 * (1.0 - math.cos(math.pi * x))
        values.append(round(base_m + peak_m * frac, 4))

    # Offshore boundary points along the SEAWARD edge of the bbox. Without a
    # coastline lookup we cannot know which edge faces the sea, so we seed points
    # along ALL FOUR edges (a thin ring just inside the bbox)  -  HydroMT selects
    # the boundary cells nearest the actual water-level boundary via ``buffer_m``,
    # and the deck ignores points that don't fall on a boundary cell. A few points
    # per edge is enough to drive a coherent surge boundary.
    inset_lon = 0.02 * (max_lon - min_lon)
    inset_lat = 0.02 * (max_lat - min_lat)
    mid_lon = 0.5 * (min_lon + max_lon)
    mid_lat = 0.5 * (min_lat + max_lat)
    edge_pts: list[tuple[float, float]] = [
        (min_lon + inset_lon, mid_lat),  # west edge
        (max_lon - inset_lon, mid_lat),  # east edge
        (mid_lon, min_lat + inset_lat),  # south edge
        (mid_lon, max_lat - inset_lat),  # north edge
    ]

    times = [SFINCS_TREF + _timedelta_s(s) for s in secs]
    stations: list[StationHydrograph] = []
    series_by_id: dict[int, ReanchoredSeries] = {}
    for i, (lon, lat) in enumerate(edge_pts, start=1):
        stations.append(
            StationHydrograph(
                point_id=i,
                lon=float(lon),
                lat=float(lat),
                times=times,
                values=list(values),
                source_id=f"parametric-surge-{i}",
                provenance={"_prov_synthetic": True},
            )
        )
        series_by_id[i] = ReanchoredSeries(
            seconds=list(secs),
            datetimes=list(times),
            values=list(values),
        )

    from .sfincs_forcing_adapter import _staging_dir, _unique  # local: lean top

    stage = _staging_dir(None)
    csv_path = write_bzs_timeseries_csv(series_by_id, _unique(stage, "bzs", "csv"))
    loc_path = write_locations_fgb(stations, _unique(stage, "bnd", "fgb"))
    logger.info(
        "model_flood_scenario: synthesised PARAMETRIC RISING-LIMB surge hydrograph "
        "for bbox=%s (return_period_yr=%s -> peak=%.2f m on base=%.2f m, ramp "
        "%.1f->%.1f hr then hold, %d steps over %.0f hr, %d boundary points) "
        "-> bzs=%s bnd=%s",
        bbox,
        return_period_yr,
        peak_m,
        base_m,
        lead_hr,
        rise_end_hr,
        len(secs),
        win_hr,
        len(stations),
        csv_path,
        loc_path,
    )
    return {
        "timeseries_uri": csv_path,
        "locations_uri": loc_path,
        "_prov_synthetic_parametric": True,
        "_prov_peak_m": peak_m,
        "_prov_return_period_yr": return_period_yr,
    }


def _timedelta_s(seconds: float):
    """Local helper: a ``timedelta`` of ``seconds`` (avoids a top-level import)."""
    from datetime import timedelta

    return timedelta(seconds=float(seconds))


def _autowire_coastal_surge_forcing(
    bbox: tuple[float, float, float, float],
    *,
    duration_hr: float,
    return_period_yr: int | float | None,
    data_sources: list[DataSource] | None = None,
) -> dict[str, Any]:
    """Auto-wire a time-varying SEA surge water-level boundary for a coastal run.

    The COASTAL fix: a ``coastal=True`` run with NO explicit ``surge_forcing``
    used to silently degrade to a pure-RAINFALL deck (``fetch_topobathy`` only
    deepened the bed, no sea water was added). This builds a water-level boundary
    so the flood animation shows water rising from the sea and marching inland.

    Degrade ladder (data-source fallback norm: primary -> fallback -> honest
    last-resort, never a silent dead-end):

    1. PRIMARY  -  NOAA CO-OPS tides (``fetch_noaa_coops_tides``): KEY-FREE, CONUS.
       Pull the observed tide+surge timeseries over the event window for the
       AOI's stations. Returns a FlatGeobuf carrying per-station ``time_series_csv``
        -  handed back as ``{"waterlevel": {"fetch_uri": <uri>}}`` so the EXISTING
       ``_resolve_surge_forcing_from_fetchers`` adapter materialises the bzs files.
    2. FALLBACK  -  GTSM tide+surge (``fetch_gtsm_tide_surge``): global, needs a CDS
       key. Attempted only if CO-OPS yields no usable station; degrades on a
       missing key / no data.
    3. LAST-RESORT  -  a PARAMETRIC design-storm surge hydrograph (key-free, offline,
       deterministic) materialised directly to bzs files. The peak scales with
       ``return_period_yr`` (a major-hurricane ARI -> a real multi-metre surge).

    Returns a ``surge_forcing`` dict whose ``waterlevel`` sub-dict yields a
    NON-None ``WaterlevelForcing`` after the resolve+build seam  -  guaranteed,
    because the last-resort parametric path is always available. Never raises (a
    fetcher exception logs + falls through to the next rung).
    """
    # Event window: anchor on "today" and run forward over the deck window. The
    # exact calendar dates do NOT matter  -  the adapter re-anchors the series onto
    # the deck's synthetic ``tref`` window (reanchor_to_tref), so we just need a
    # window long enough to carry the surge shape.
    win_hr = float(duration_hr) if duration_hr and duration_hr > 0 else 24.0
    end_dt = datetime.now(timezone.utc)
    span_days = max(int((win_hr + 23) // 24), 1)
    from datetime import timedelta as _td

    start_dt = end_dt - _td(days=span_days)
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = end_dt.strftime("%Y-%m-%d")

    # --- 1) PRIMARY: NOAA CO-OPS tides (key-free, CONUS) -------------------- #
    try:
        from ..tools.fetch_noaa_coops_tides import fetch_noaa_coops_tides

        layer = fetch_noaa_coops_tides(
            bbox, start_date=start_date, end_date=end_date, product="water_level"
        )
        uri = getattr(layer, "uri", None)
        if uri:
            if data_sources is not None:
                data_sources.append(
                    DataSource(
                        name="NOAA CO-OPS tides (auto-wired surge boundary)",
                        uri=str(uri),
                        accessed_at=datetime.now(timezone.utc),
                    )
                )
            logger.info(
                "model_flood_scenario: auto-wired coastal surge via NOAA CO-OPS "
                "tides for bbox=%s -> %s",
                bbox,
                uri,
            )
            return {"waterlevel": {"fetch_uri": str(uri)}}
    except Exception as exc:  # noqa: BLE001  -  degrade to the next rung
        logger.warning(
            "model_flood_scenario: NOAA CO-OPS auto-wire failed for bbox=%s "
            "(%s)  -  trying GTSM fallback.",
            bbox,
            exc,
        )

    # --- 2) FALLBACK: GTSM tide+surge (global, needs a CDS key) ------------- #
    try:
        from ..tools.fetch_gtsm_tide_surge import fetch_gtsm_tide_surge

        layer = fetch_gtsm_tide_surge(
            bbox, start_date=start_date, end_date=end_date
        )
        uri = getattr(layer, "uri", None)
        if uri:
            if data_sources is not None:
                data_sources.append(
                    DataSource(
                        name="GTSM tide+surge (auto-wired surge boundary)",
                        uri=str(uri),
                        accessed_at=datetime.now(timezone.utc),
                    )
                )
            logger.info(
                "model_flood_scenario: auto-wired coastal surge via GTSM for "
                "bbox=%s -> %s",
                bbox,
                uri,
            )
            return {"waterlevel": {"fetch_uri": str(uri)}}
    except Exception as exc:  # noqa: BLE001  -  degrade to the parametric path
        logger.warning(
            "model_flood_scenario: GTSM auto-wire failed for bbox=%s (%s)  -  "
            "falling back to the PARAMETRIC design-storm surge.",
            bbox,
            exc,
        )

    # --- 3) LAST-RESORT: parametric design-storm surge (always available) --- #
    wl = _synthesize_parametric_surge_forcing(
        bbox, duration_hr=win_hr, return_period_yr=return_period_yr
    )
    if data_sources is not None:
        data_sources.append(
            DataSource(
                name=(
                    "Parametric design-storm surge (auto-wired; "
                    f"{return_period_yr}-yr, peak {wl.get('_prov_peak_m')} m)"
                ),
                uri="synthetic:parametric-surge",
                accessed_at=datetime.now(timezone.utc),
            )
        )
    return {"waterlevel": wl}


def _synthesize_tide_base_forcing(
    bbox: tuple[float, float, float, float],
    *,
    duration_hr: float,
    base_m: float = 0.3,
) -> dict[str, Any]:
    """A FLAT constant tide-base bzs boundary (default 0.3 m) for the spiderweb path.

    SPIDERWEB (2026-07-19): when a parametric hurricane spiderweb drives the
    wind+pressure, the parametric surge synthesis is SUPPRESSED (it would
    double-count the surge). But the deck MUST still carry msk=2 water-level
    boundary cells or it cannot drain/feed (the setup_mask_bounds emit is gated
    on a waterlevel member). So we emit a low CONSTANT tide-base bzs: the
    offshore boundary sits at a modest high-tide level and the surge is then
    GENERATED by the spw wind+pressure over the shelf. Same materialised
    ``{timeseries_uri, locations_uri}`` shape the fetchers produce, so it flows
    through ``_build_surge_forcing_members`` -> a non-None WaterlevelForcing.
    """
    from .sfincs_forcing_adapter import (
        SFINCS_TREF,
        ReanchoredSeries,
        StationHydrograph,
        _staging_dir,
        _unique,
        write_bzs_timeseries_csv,
        write_locations_fgb,
    )

    min_lon, min_lat, max_lon, max_lat = bbox
    win_hr = float(duration_hr) if duration_hr and duration_hr > 0 else 24.0
    # 2 samples spanning the window (set_forcing_1d needs >= 2) at the flat base.
    secs = [0.0, win_hr * 3600.0]
    values = [round(base_m, 4), round(base_m, 4)]
    times = [SFINCS_TREF + _timedelta_s(s) for s in secs]
    inset_lon = 0.02 * (max_lon - min_lon)
    inset_lat = 0.02 * (max_lat - min_lat)
    mid_lon = 0.5 * (min_lon + max_lon)
    mid_lat = 0.5 * (min_lat + max_lat)
    edge_pts = [
        (min_lon + inset_lon, mid_lat),
        (max_lon - inset_lon, mid_lat),
        (mid_lon, min_lat + inset_lat),
        (mid_lon, max_lat - inset_lat),
    ]
    stations: list[StationHydrograph] = []
    series_by_id: dict[int, ReanchoredSeries] = {}
    for i, (lon, lat) in enumerate(edge_pts, start=1):
        stations.append(
            StationHydrograph(
                point_id=i, lon=float(lon), lat=float(lat), times=times,
                values=list(values), source_id=f"tide-base-{i}",
                provenance={"_prov_tide_base": True},
            )
        )
        series_by_id[i] = ReanchoredSeries(
            seconds=list(secs), datetimes=list(times), values=list(values)
        )
    stage = _staging_dir(None)
    csv_path = write_bzs_timeseries_csv(series_by_id, _unique(stage, "bzs", "csv"))
    loc_path = write_locations_fgb(stations, _unique(stage, "bnd", "fgb"))
    logger.info(
        "model_flood_scenario: synthesised FLAT %.2f m tide-base bzs boundary "
        "(spiderweb path; surge is generated by the spw wind+pressure) -> "
        "bzs=%s bnd=%s",
        base_m, csv_path, loc_path,
    )
    return {
        "timeseries_uri": csv_path,
        "locations_uri": loc_path,
        "_prov_tide_base_m": base_m,
    }


def _resolve_spiderweb_forcing(
    bbox: tuple[float, float, float, float],
    *,
    duration_hr: float,
    storm_name: str | None,
    storm_season: int | None,
    storm_track_uri: str | None,
    data_sources: list[DataSource] | None = None,
) -> tuple["SpiderwebForcing", dict[str, Any]]:
    """Resolve the IBTrACS track -> build the Holland spiderweb -> SpiderwebForcing.

    SPIDERWEB (2026-07-19). Two track sources:
    - ``storm_track_uri`` verbatim (a prior fetch_storm_tracks POINTS-FGB), OR
    - resolve via ``fetch_storm_tracks(bbox, start_year=storm_season,
      end_year=storm_season, storm_name=..., geometry="points")``.

    The FGB is staged to a local path (s3://gs:// via the sfincs_builder cache
    stager; local / file:// used directly), read into fix dicts, and handed to
    ``sfincs_spiderweb.build_spiderweb_from_fixes`` which writes the .spw and
    returns the utm zone + provenance (incl. which values were fallback).

    Runs SYNC (fetch_storm_tracks network I/O + geopandas read + Holland build)
    -> the caller MUST invoke it via ``asyncio.to_thread`` (no-loop-block norm).
    """
    import os as _os

    from ..tools.fetch_storm_tracks import fetch_storm_tracks
    from .sfincs_builder import _stage_gcs_local
    from . import sfincs_spiderweb as _spw

    # --- 1. resolve the track FGB uri ----------------------------------------
    if storm_track_uri:
        track_uri = storm_track_uri
    else:
        layer = fetch_storm_tracks(
            bbox=bbox,
            start_year=storm_season,
            end_year=storm_season,
            storm_name=storm_name,
            geometry="points",
        )
        track_uri = layer.uri
        if data_sources is not None:
            data_sources.append(
                DataSource(
                    name=(
                        f"IBTrACS best track ({storm_name or 'storm'} "
                        f"{storm_season or ''})".strip()
                    ),
                    uri=track_uri,
                    accessed_at=datetime.now(timezone.utc),
                )
            )

    # --- 2. stage to a readable local path -----------------------------------
    local_fgb = track_uri
    if local_fgb.startswith("file://"):
        local_fgb = local_fgb[len("file://"):]
    if local_fgb.startswith(("gs://", "s3://")):
        local_fgb = _stage_gcs_local(local_fgb)

    # --- 3. build the spiderweb ----------------------------------------------
    out_dir = _os.path.join(
        _staging_dir_local(), f"spw_{new_ulid()}"
    )
    result = _spw.build_spiderweb_from_fixes(
        _spw.read_ibtracs_fixes_from_fgb(local_fgb),
        bbox,
        out_dir=out_dir,
        deck_sim_hours=float(duration_hr),
        storm_name=storm_name,
    )
    member = SpiderwebForcing(
        spw_path=result.spw_path,
        utmzone=result.utmzone,
        spw_filename=result.spw_filename,
        provenance=dict(result.provenance),
    )
    prov = dict(result.provenance)
    prov["track_uri"] = track_uri
    prov["utm_epsg"] = result.utm_epsg
    return member, prov


def _staging_dir_local() -> str:
    """A local scratch dir for generated spw files (temp, per-process)."""
    import tempfile as _tf

    d = _os_path_join(_tf.gettempdir(), "grace2_spw")
    import os as _os

    _os.makedirs(d, exist_ok=True)
    return d


def _os_path_join(*parts: str) -> str:
    import os as _os

    return _os.path.join(*parts)


def _resolve_building_obstacle_uri(
    building_obstacles: bool | str,
    bbox: tuple[float, float, float, float],
    data_sources: list[DataSource],
) -> str | None:
    """Resolve the building-obstacle geofile URI for the SFINCS deck (best-effort).

    COASTAL SFINCS — burn building footprints into the deck so the rough 2D
    flood routes around buildings. Three forms of ``building_obstacles``:

    - ``False`` / falsy → no obstacles (``None``).
    - a ``str`` → used verbatim as the footprint geofile URI (caller already has
      a FlatGeobuf / GeoJSON; e.g. a prior ``fetch_buildings`` output).
    - ``True`` → fetch OSM building footprints for ``bbox`` via the
      ``fetch_buildings`` atomic tool (OSM Overpass primary, job-0331). This is
      BEST-EFFORT: any fetch failure logs + returns ``None`` (the flood proceeds
      WITHOUT obstacles, never aborts) — same degrade policy as river geometry
      (job-0307). A successful fetch is recorded as a ``DataSource``.

    Returns the obstacle geofile URI, or ``None`` when there is nothing to burn.
    """
    if not building_obstacles:
        return None
    if isinstance(building_obstacles, str):
        return building_obstacles
    # building_obstacles is True → fetch OSM footprints (best-effort).
    try:
        from ..tools.data_fetch import fetch_buildings  # local: keep top imports lean

        layer = fetch_buildings(bbox, source="osm")
        uri = getattr(layer, "uri", None)
        if uri:
            data_sources.append(
                DataSource(
                    name="OSM building footprints (Overpass — SFINCS obstacles)",
                    uri=uri,
                    accessed_at=datetime.now(timezone.utc),
                )
            )
        return uri
    except Exception as exc:  # noqa: BLE001 — obstacles are optional for the flood
        logger.warning(
            "model_flood_scenario: fetch_buildings failed for bbox=%s (%s) — "
            "proceeding WITHOUT building obstacles (the flood still runs, just "
            "without footprint masking).",
            bbox,
            exc,
        )
        return None


# --------------------------------------------------------------------------- #
# NATE 2026-06-26 -- SFINCS scenario-coverage composer auto-wiring
# (fluvial / compound / wind / infiltration / levee-breach / tsunami).
# Each helper mirrors the existing _autowire_coastal_surge_forcing /
# _resolve_building_obstacle_uri patterns: best-effort fetch + honest degrade
# per the data-source fallback norm, EXCEPT the breach + tsunami magnitude
# gates, which HARD-FAIL (never fabricate model inputs).
# --------------------------------------------------------------------------- #


def _autowire_river_discharge_forcing(
    bbox: tuple[float, float, float, float],
    *,
    duration_hr: float,
    data_sources: list[DataSource] | None = None,
    river_layer_uri: str | None = None,
) -> dict[str, Any] | None:
    """Auto-wire a FLUVIAL river-discharge boundary for a fluvial / compound run.

    NATE 2026-06-26: the fluvial archetype. A ``river=True`` (or ``compound``)
    run needs a domain-EDGE river-inflow hydrograph driving the SFINCS ``dis``
    boundary. Unlike the coastal surge there is NO parametric last-resort synth
    (a fabricated discharge would violate Invariant 7), so the ladder degrades to
    SKIP -- the run proceeds pluvial-only when no real discharge is available.

    Degrade ladder (data-source fallback norm: primary -> fallback -> honest skip):

    1. PRIMARY  -  NOAA National Water Model (``fetch_noaa_nwm_streamflow``):
       CONUS, KEY-FREE, the canonical operational streamflow. Returns a point
       FlatGeobuf carrying ``streamflow_cms`` (m^3/s) -> handed back as
       ``{"discharge": {"fetch_uri": <uri>, "value_unit": "cms"}}`` so the
       EXISTING ``_resolve_surge_forcing_from_fetchers`` adapter materialises the
       dis files. ``rivers_uri`` (the already-fetched NHDPlus river layer) is
       threaded so ``setup_river_inflow`` gets inflow points.
    2. FALLBACK  -  USGS NWIS gauges (``fetch_usgs_nwis_gauges``): observed
       instrument-record hydrograph. NWIS discharge is in cfs (ft^3/s), so
       ``value_unit`` is set to ``"cfs"`` (the resolve converts; without it the
       series would be ~35.3x too large -- silent-wrong-physics).
    3. LAST-RESORT  -  SKIP: return ``None`` (the run proceeds pluvial-only). The
       composer logs the honest degrade; NO fabricated hydrograph.

    Returns a partial ``{"discharge": {...}}`` dict to merge into ``surge_forcing``
    BEFORE ``_resolve_surge_forcing_from_fetchers``, or ``None`` when neither
    source yields a hydrograph. Never raises (a fetcher exception logs + falls
    through to the next rung).
    """
    win_hr = float(duration_hr) if duration_hr and duration_hr > 0 else 24.0

    # --- 1) PRIMARY: NOAA NWM streamflow (key-free, CONUS) ------------------ #
    try:
        from ..tools.fetch_noaa_nwm_streamflow import fetch_noaa_nwm_streamflow

        layer = fetch_noaa_nwm_streamflow(bbox)
        uri = getattr(layer, "uri", None)
        if uri:
            if data_sources is not None:
                data_sources.append(
                    DataSource(
                        name="NOAA NWM streamflow (auto-wired fluvial boundary)",
                        uri=str(uri),
                        accessed_at=datetime.now(timezone.utc),
                    )
                )
            logger.info(
                "model_flood_scenario: auto-wired fluvial discharge via NOAA NWM "
                "streamflow for bbox=%s -> %s",
                bbox,
                uri,
            )
            return {
                "discharge": {
                    "fetch_uri": str(uri),
                    "value_unit": "cms",  # NWM streamflow_cms is m^3/s
                    "rivers_uri": river_layer_uri,
                }
            }
    except Exception as exc:  # noqa: BLE001  -  degrade to the next rung
        logger.warning(
            "model_flood_scenario: NOAA NWM auto-wire failed for bbox=%s (%s)  -  "
            "trying USGS NWIS fallback.",
            bbox,
            exc,
        )

    # --- 2) FALLBACK: USGS NWIS gauges (observed hydrograph, cfs) ----------- #
    try:
        import math as _math

        from ..tools.fetch_usgs_nwis_gauges import fetch_usgs_nwis_gauges

        period_days = max(1, int(_math.ceil(win_hr / 24.0)))
        layer = fetch_usgs_nwis_gauges(bbox=bbox, period=f"P{period_days}D")
        uri = getattr(layer, "uri", None)
        if uri:
            if data_sources is not None:
                data_sources.append(
                    DataSource(
                        name="USGS NWIS gauges (auto-wired fluvial boundary)",
                        uri=str(uri),
                        accessed_at=datetime.now(timezone.utc),
                    )
                )
            logger.info(
                "model_flood_scenario: auto-wired fluvial discharge via USGS NWIS "
                "gauges for bbox=%s -> %s",
                bbox,
                uri,
            )
            return {
                "discharge": {
                    "fetch_uri": str(uri),
                    "value_unit": "cfs",  # NWIS discharge is ft^3/s
                    "rivers_uri": river_layer_uri,
                }
            }
    except Exception as exc:  # noqa: BLE001  -  degrade to the honest skip
        logger.warning(
            "model_flood_scenario: USGS NWIS auto-wire failed for bbox=%s (%s)  -  "
            "no fluvial discharge available; the run proceeds PLUVIAL-only.",
            bbox,
            exc,
        )

    # --- 3) LAST-RESORT: honest skip (no fabricated discharge) -------------- #
    logger.info(
        "model_flood_scenario: no fluvial discharge source for bbox=%s (NWM + "
        "NWIS both unavailable)  -  skipping the discharge boundary (pluvial-only).",
        bbox,
    )
    return None


def _resolve_infiltration_uri(
    infiltration: bool | str,
    bbox: tuple[float, float, float, float],
    data_sources: list[DataSource],
) -> str | None:
    """Resolve the GCN250 curve-number raster URI for the SFINCS infiltration loss.

    NATE 2026-06-26: the infiltration archetype. Tri-state (mirrors
    ``building_obstacles``):

    - ``False`` / falsy -> no infiltration loss (``None``).
    - a ``str`` -> used verbatim as the CN raster URI (caller already has a
      single-band GCN250 GeoTIFF).
    - ``True`` -> BEST-EFFORT fetch of the GCN250 global SCS curve-number raster
      for ``bbox`` via ``fetch_gcn250_curve_numbers`` (key-free, global). A fetch
      failure logs + returns ``None`` (the flood proceeds WITHOUT an infiltration
      loss, never aborts -- same degrade policy as building obstacles).

    Returns the CN raster URI, or ``None`` when there is nothing to wire.
    """
    if not infiltration:
        return None
    if isinstance(infiltration, str):
        return infiltration
    # infiltration is True -> fetch the GCN250 CN raster (best-effort).
    try:
        from ..tools.fetch_gcn250_curve_numbers import fetch_gcn250_curve_numbers

        layer = fetch_gcn250_curve_numbers(bbox, antecedent_moisture="average")
        uri = getattr(layer, "uri", None)
        if uri:
            data_sources.append(
                DataSource(
                    name="GCN250 SCS curve numbers (SFINCS infiltration loss)",
                    uri=str(uri),
                    accessed_at=datetime.now(timezone.utc),
                )
            )
        return uri
    except Exception as exc:  # noqa: BLE001 — infiltration is optional for the flood
        logger.warning(
            "model_flood_scenario: fetch_gcn250_curve_numbers failed for bbox=%s "
            "(%s) — proceeding WITHOUT an infiltration loss (the flood still runs).",
            bbox,
            exc,
        )
        return None


def _synthesize_breach_discharge_forcing(
    breach_point: tuple[float, float],
    *,
    peak_m3s: float,
    arrival_hr: float | None,
    duration_hr: float,
) -> dict[str, Any]:
    """Synthesize an INTERIOR levee-breach discharge hydrograph -> dis files dict.

    NATE 2026-06-26: the levee-breach archetype. The breach is an interior
    point-source ``dis`` jet (NOT a domain-edge river inflow), so it reuses the
    discharge seam with explicit ``locations`` at the drawn breach point and NO
    ``rivers_uri``/``hydrography_uri`` (the deck emits a SECOND
    ``setup_discharge_forcing(merge: true)`` with no ``setup_river_inflow``).

    HONESTY GATE (caller-enforced): the breach PEAK + LOCATION are USER inputs --
    the composer NEVER fabricates them. This synth only runs when the caller has
    already validated both are present (the magnitude gate fires upstream).

    Hydrograph: a triangular pulse rising from 0 to ``peak_m3s`` at
    ``arrival_hr`` (defaults to ~25% of the window) then receding linearly to a
    small residual by the window end. Materialised to a dis CSV + a 1-point
    locations FGB at ``breach_point`` via the SAME writers the fetcher adapter
    uses, so the deck consumes them unchanged.

    Returns ``{"timeseries_uri": <dis.csv>, "locations_uri": <src.fgb>, ...}`` --
    the pre-materialised discharge shape (carried onto ``ForcingSpec.breach``).
    """
    from .sfincs_forcing_adapter import (
        SFINCS_TREF,
        ReanchoredSeries,
        StationHydrograph,
        _staging_dir,
        _unique,
        write_dis_timeseries_csv,
        write_locations_fgb,
    )

    lon, lat = float(breach_point[0]), float(breach_point[1])
    peak = float(peak_m3s)
    win_hr = float(duration_hr) if duration_hr and duration_hr > 0 else 24.0
    # Time-to-peak (breach arrival). Default ~25% of the window so the jet builds
    # then drains within the run; clamp into (0, win_hr) so the triangle is valid.
    if arrival_hr is not None and arrival_hr > 0:
        t_peak_hr = min(float(arrival_hr), 0.95 * win_hr)
    else:
        t_peak_hr = 0.25 * win_hr
    t_peak_hr = max(t_peak_hr, 0.05 * win_hr)
    residual = max(0.0, 0.02 * peak)  # small non-zero tail (avoids a hard zero)

    # FINE sampling (~6 min) so the triangular rise/recession resolves on the
    # minute-scale output cadence (>= 2 samples for set_forcing_1d).
    _sample_s = 360.0
    n_steps = max(int(round(win_hr * 3600.0 / _sample_s)), 2)
    secs = [float(i) * (win_hr * 3600.0) / float(n_steps) for i in range(n_steps + 1)]
    values: list[float] = []
    for s in secs:
        hr = s / 3600.0
        if hr <= t_peak_hr:
            frac = hr / t_peak_hr if t_peak_hr > 0 else 1.0
            q = peak * frac
        else:
            # Linear recession from the peak to the residual by the window end.
            denom = max(win_hr - t_peak_hr, 1e-6)
            frac = (win_hr - hr) / denom
            frac = max(0.0, min(1.0, frac))
            q = residual + (peak - residual) * frac
        values.append(round(q, 4))

    times = [SFINCS_TREF + _timedelta_s(s) for s in secs]
    stations = [
        StationHydrograph(
            point_id=1,
            lon=lon,
            lat=lat,
            times=times,
            values=list(values),
            source_id="levee-breach-1",
            provenance={"_prov_breach": True},
        )
    ]
    series_by_id = {
        1: ReanchoredSeries(
            seconds=list(secs),
            datetimes=list(times),
            values=list(values),
        )
    }

    stage = _staging_dir(None)
    csv_path = write_dis_timeseries_csv(series_by_id, _unique(stage, "breach_dis", "csv"))
    loc_path = write_locations_fgb(stations, _unique(stage, "breach_src", "fgb"))
    logger.info(
        "model_flood_scenario: synthesised LEVEE-BREACH discharge hydrograph at "
        "(%.5f, %.5f): peak=%.1f m^3/s at %.1f hr, %d steps over %.0f hr "
        "-> dis=%s src=%s",
        lon,
        lat,
        peak,
        t_peak_hr,
        len(secs),
        win_hr,
        csv_path,
        loc_path,
    )
    return {
        "timeseries_uri": csv_path,
        "locations_uri": loc_path,
        "_prov_breach": True,
        "_prov_peak_m3s": peak,
        "_prov_arrival_hr": t_peak_hr,
    }


def _synthesize_tsunami_waterlevel_forcing(
    bbox: tuple[float, float, float, float],
    *,
    wave_height_m: float,
    period_min: float | None,
    duration_hr: float,
) -> dict[str, Any]:
    """Synthesize a TSUNAMI water-level boundary -> materialised bzs files dict.

    NATE 2026-06-26: the tsunami archetype. Delegates the waveform GENERATION to
    the forcing adapter's ``synthesize_tsunami_bzs`` (a leading-depression N-wave
    -- trough THEN crest -- NOT the storm raised-cosine), driven onto the SAME
    seaward boundary points the surge synth uses. Reuses the ENTIRE existing
    waterlevel ``bzs`` deck seam (``setup_mask_bounds`` + ``setup_waterlevel_forcing``)
    with zero new deck code.

    HONESTY GATE (caller-enforced): the wave HEIGHT is a USER input -- the
    composer NEVER fabricates it. This synth only runs when the caller has already
    validated ``wave_height_m`` is present (the magnitude gate fires upstream).

    ``period_min`` defaults to ~15 min (a representative tsunami period) when the
    user did not supply it -- a SHAPE default, not a magnitude fabrication.

    Returns the materialised ``{"timeseries_uri": <bzs.csv>, "locations_uri":
    <bnd.fgb>, ...}`` dict (carried onto ``ForcingSpec.waterlevel``).
    """
    from .sfincs_forcing_adapter import synthesize_tsunami_bzs

    period_s = float(period_min) * 60.0 if period_min and period_min > 0 else 15.0 * 60.0
    win_hr = float(duration_hr) if duration_hr and duration_hr > 0 else 24.0
    out = synthesize_tsunami_bzs(
        bbox,
        eta_max_m=float(wave_height_m),
        period_s=period_s,
        wave_type="ldn",
        lead_depression=True,
        window_hours=win_hr,
    )
    logger.info(
        "model_flood_scenario: synthesised TSUNAMI N-wave bzs boundary for "
        "bbox=%s (height=%.2f m, period=%.0f s over %.0f hr) -> bzs=%s bnd=%s",
        bbox,
        float(wave_height_m),
        period_s,
        win_hr,
        out.get("timeseries_uri"),
        out.get("locations_uri"),
    )
    return out


def _resolve_quadtree_rivers_uri(
    *,
    bbox: tuple[float, float, float, float],
    data_sources: list[DataSource],
) -> str | None:
    """Resolve the river-geometry geofile URI for the combined quadtree deck.

    BEST-EFFORT: fetches river/waterway LineStrings for ``bbox`` so the combined
    worker can burn them into the deck as flow paths / refinement seeds. The agent
    only POINTS at the geofile — it does NO GIS. Any fetch failure logs + returns
    ``None`` (the combined run proceeds WITHOUT rivers; same degrade policy as the
    building footprints + the land/pluvial river branch, job-0307). A successful
    fetch is recorded as a ``DataSource``. Returns the river geofile URI, or
    ``None`` when nothing was fetched.
    """
    try:
        from ..tools.data_fetch import fetch_river_geometry

        layer = fetch_river_geometry(bbox, source="nhdplus_hr")
        uri = getattr(layer, "uri", None)
        if uri:
            data_sources.append(
                DataSource(
                    name="NHDPlus HR river geometry (combined quadtree deck)",
                    uri=uri,
                    accessed_at=datetime.now(timezone.utc),
                )
            )
        return uri
    except Exception as exc:  # noqa: BLE001 — rivers are optional for the flood
        logger.warning(
            "model_flood_scenario: fetch_river_geometry failed for bbox=%s (%s) — "
            "proceeding WITHOUT rivers in the combined quadtree deck (the flood "
            "still runs).",
            bbox,
            exc,
        )
        return None


# --------------------------------------------------------------------------- #
# The workflow itself
# --------------------------------------------------------------------------- #


async def model_flood_scenario(
    bbox: tuple[float, float, float, float] | None = None,
    location_query: str | None = None,
    event_id: str | None = None,
    return_period_yr: int = 100,
    duration_hr: int = 24,
    compute_class: str = "medium",
    forcing_raster_uri: str | None = None,
    surge_forcing: dict[str, Any] | None = None,
    enable_subgrid: bool = False,
    building_obstacles: bool | str = False,
    building_obstacle_mode: str = "exclude",
    coastal: bool = False,
    quadtree: bool = False,
    output_interval_min: float | None = None,
    # NATE 2026-06-26: SFINCS scenario-coverage intents (fluvial / compound /
    # wind / infiltration / levee-breach / tsunami). All default to today's
    # behaviour so a pluvial run is byte-identical (Invariant 7).
    river: bool = False,
    compound: bool = False,
    wind: dict[str, Any] | None = None,
    advanced_physics: dict[str, Any] | None = None,
    infiltration: bool | str = False,
    breach_point: tuple[float, float] | None = None,
    breach_peak_discharge_m3s: float | None = None,
    breach_arrival_hr: float | None = None,
    tsunami: bool = False,
    tsunami_wave_height_m: float | None = None,
    tsunami_period_min: float | None = None,
    # SPIDERWEB (2026-07-19): parametric hurricane wind+pressure via a Delft3D
    # .spw. Any of these implies coastal + the spiderweb wind path; mutually
    # exclusive with the ``wind`` param (typed input error, never silent).
    storm_name: str | None = None,
    storm_season: int | None = None,
    storm_track_uri: str | None = None,
    *,
    project_id: str | None = None,
    session_id: str | None = None,
) -> AssessmentEnvelope:
    """Compose the full M5 flood-modeling chain.

    Resolves the location (geocode if ``bbox`` not given), fetches DEM (3DEP)
    + landcover (NLCD) + river geometry (NHDPlus HR) + design-storm
    precipitation depth (NOAA Atlas 14), builds an SFINCS model via HydroMT
    (the OQ-4 §4 NLCD validation gate fires here — raises
    ``SFINCSSetupError("LULC_MAPPING_MISMATCH")`` on vintage mismatch),
    dispatches ``run_solver(sfincs, ...)``, awaits ``wait_for_completion``,
    postprocesses the run's NetCDF to a flood-depth COG, and returns a
    typed ``AssessmentEnvelope`` Flood subtype (Appendix B.4).

    On internal failure (fetch error, NLCD gate firing, SFINCS dispatch
    failure, SOLVER_FAILED, postprocess error), returns a typed
    AssessmentEnvelope with zero-valued ``FloodMetrics`` and the error code
    threaded into ``solver_version`` — never raises (caller-friendly).
    The agent surface narrates the failed envelope honestly.

    Args:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. When
            ``None``, ``location_query`` is used to geocode.
        location_query: free-text place name (e.g. ``"Fort Myers, FL"``)
            geocoded via Nominatim. Ignored if ``bbox`` is supplied.
        event_id: optional event ID for provenance (HEP integration future
            hook; v0.1 carries it on the envelope's provenance dict).
        return_period_yr: design-storm ARI. Atlas 14 publishes
            ``{1, 2, 5, 10, 25, 50, 100, 200, 500, 1000}``. Default 100.
        duration_hr: design-storm duration in hours. Atlas 14 publishes a
            fixed row set; 24 hr is the v0.1 default.
        compute_class: FR-CE-3 compute class. Default ``"medium"``.
        forcing_raster_uri: optional ``gs://...`` (or local) URI of an
            OBSERVED accumulated-precip raster (job-0225 v2, Case 3). When
            set, the workflow SKIPS the ``lookup_precip_return_period`` Atlas
            14 design-storm lookup and instead computes the AREA-MEAN
            accumulated precip over the model domain, converting it to a
            uniform SFINCS ``netamt`` rate (mm/hr) — the OQ-6 area-mean
            fallback (spw spatial upgrade path documented in
            ``sfincs_builder``). ``duration_hr`` is reused as the precip
            accumulation window for the depth→rate conversion. When ``None``
            (the default) the Atlas 14 design-storm path runs unchanged —
            behavior is **identical** to the v1 workflow (regression-critical).
        surge_forcing: optional nested dict wiring the COASTAL SFINCS surge /
            tide / discharge / wind / pressure boundary forcing into the deck —
            ``{"waterlevel": {...}, "discharge": {...}, "wind": {...},
            "pressure": {...}}``. Each sub-dict carries the forcing-file URIs
            (timeseries CSV + locations geofile, or a geodataset / grid netCDF)
            materialised from the forcing fetchers (``fetch_gtsm_tide_surge`` /
            ``fetch_noaa_coops_tides`` / ``fetch_noaa_nwm_streamflow`` /
            ``fetch_cama_flood_discharge`` / ERA5). See
            ``_build_surge_forcing_members``. ``None`` (default) → pure-pluvial
            deck (NO surge blocks emitted; byte-identical to the v0.1 deck).
        enable_subgrid: emit a SFINCS ``setup_subgrid`` block so the solve runs
            on a coarse grid while resolving sub-cell topography + roughness (the
            cheap urban-flood estimate). Auto-enabled when ``building_obstacles``
            is set. Default ``False``.
        building_obstacles: burn building footprints into the deck so the rough
            2D flood routes around buildings (the COASTAL "urban flood" ask).
            ``True`` → BEST-EFFORT OSM-footprint fetch (a fetch failure degrades
            to no obstacles, never aborts the flood); a ``str`` is used verbatim
            as the footprint geofile URI; ``False`` (default) → no obstacles.
        building_obstacle_mode: ``"exclude"`` (default) makes footprint cells
            INACTIVE no-flow holes; ``"raise"`` keeps them active but lifts their
            bed elevation via the subgrid (requires subgrid; auto-enabled).
        coastal: COASTAL-AOI flag (SFINCS North Star P1). When ``True`` — OR
            implicitly when ``surge_forcing`` is supplied (a water-level / surge
            boundary is physically incoherent without a nearshore bed) — the DEM
            fetch is routed through ``fetch_topobathy`` instead of ``fetch_dem``.
            ``fetch_topobathy`` produces ONE seamless topo-bathymetry surface
            (USGS 3DEP land + NOAA NCEI CUDEM bathymetry, CUDEM winning on the
            coast) in the SAME contract as ``fetch_dem`` (single-band float32
            NAVD88-metres COG, positive-up, EPSG:32616), so ``build_sfincs_model``
            / ``setup_dep`` consume it UNCHANGED — the coastal DEM is a drop-in.
            ``False`` (default) AND no ``surge_forcing`` → the LAND/pluvial path
            is byte-identical to the v0.1 workflow (``fetch_dem``,
            regression-critical). If ``fetch_topobathy`` cannot find CUDEM
            bathymetry for the AOI it degrades INTERNALLY to a 3DEP-land-only
            surface (honest ``fallback_warning``, never a silent dead-end); a
            hard topobathy failure (no CUDEM AND no 3DEP, bad bbox) surfaces as a
            typed failed envelope. The land DEM behaviour for a non-coastal run
            is never touched.
            COASTAL AUTO-WIRE (job: coastal surge-with-waves): when ``is_coastal``
            (this flag OR ``surge_forcing`` OR ``quadtree``) AND no explicit
            ``surge_forcing`` was supplied, the workflow auto-wires a time-varying
            SEA surge water-level boundary via ``_autowire_coastal_surge_forcing``
            (CO-OPS tides -> GTSM -> a parametric design-storm surge scaling with
            ``return_period_yr``) AND forces ``quadtree`` on so the SnapWave wave
            deck + ``postprocess_waves`` run. This makes a coastal run show water
            coming IN from the sea + a wave-height field, instead of the old
            silent rainfall-only degrade. ALL gated on ``is_coastal``  -  the
            inland/pluvial path is byte-identical (no boundary, quadtree unchanged).
        quadtree: COASTAL SFINCS North Star — build the deck with a multi-level
            REFINED QUADTREE grid + SnapWave wave coupling (incident + infragravity
            waves) instead of a regular grid. This authoring requires cht_sfincs
            (GPL-3.0), so it runs in a DEDICATED GPL-isolated Batch worker the
            agent only SUBMITS: the workflow composes a build_spec from the
            already-fetched topobathy + forcing, submits the deck-build Batch job
            (``build_sfincs_quadtree_deck``), and feeds the resulting deck
            manifest URI into the SAME ``run_solver('sfincs', ...)`` solve — the
            solve half is unchanged. INERT until NATE provisions + flips
            ``GRACE2_SOLVER_BACKEND=aws-batch`` + the deck-builder job-def
            (``GRACE2_AWS_BATCH_JOB_DEF_SFINCS_DECKBUILDER``); when unset the
            quadtree request surfaces as a typed ``DECK_BUILD_FAILED`` failed
            envelope (honest degrade, never silent). Implies ``coastal=True``.
            ``False`` (default) → the regular-grid build_sfincs_model path,
            byte-identical to today. The agent NEVER imports cht_sfincs.
        output_interval_min: animation map-output cadence in MINUTES (the SFINCS
            ``dtout``/``dtmaxout`` stride). Drives how often the solve writes a
            depth snapshot, hence how fast the animation reads. Resolved BY SIM
            TYPE via ``_resolve_output_interval_min``: a COASTAL / quadtree / wave
            run defaults to a FINE ~5-min stride (so the animation shows water
            rolling in  -  waves move in seconds-to-minutes; an hourly surge
            snapshot looks like a slowly-filling bathtub), while the PLUVIAL path
            ALWAYS resolves to ``None`` -> the legacy HOURLY cadence
            (byte-identical, regression-critical). An explicit value overrides the
            coastal default (floored at 1 min; the deck re-floors at 60 s). Frame
            count is bounded by ``MAX_FLOOD_FRAMES`` in postprocess so a fine
            cadence over the full window can't balloon the payload. ``None``
            (default) lets the sim-type default apply.
        river: FLUVIAL run -- auto-wire a river-discharge boundary (NOAA NWM ->
            USGS NWIS -> honest skip). Does NOT imply coastal (stays on
            ``fetch_dem``). ``False`` (default) -> no discharge boundary.
        compound: COMPOUND flood -- auto-wire waterlevel AND discharge AND precip
            together (implies ``coastal`` + ``river``). ``False`` (default).
        wind: optional uniform/gridded WIND forcing -- ``{"magnitude": <m/s>,
            "direction": <deg-from>}`` OR ``{"grid_uri": <nc>}`` (user/ERA5
            supplied, never fabricated). When set, defaults ``advanced_physics`` to
            ``{"advection": 1}`` (the registry exposes ``coriolis_latitude`` +
            ``wind_drag`` for the user to lift). ``None`` (default) -> no wind.
        advanced_physics: optional SFINCS physics overrides validated via
            ``physics_registry`` (keys subset of ``{advection, theta, alpha,
            huthresh, coriolis_latitude, wind_drag}``) and threaded onto
            ``BuildOptions.advanced_physics`` -> the deck ``setup_config`` block.
            ``None`` (default) -> deck physics byte-identical to today.
        infiltration: SOIL-INFILTRATION loss (GCN250 curve numbers). ``True`` ->
            auto-fetch GCN250; a ``str`` -> verbatim CN raster URI; ``False``
            (default) -> no infiltration loss. Best-effort (a fetch failure
            degrades to no loss, never aborts).
        breach_point: ``(lon, lat)`` of a DRAWN levee-breach point. USER-GATED:
            if given WITHOUT ``breach_peak_discharge_m3s`` the run returns a typed
            ``USER_INPUT_REQUIRED`` failed envelope (the composer NEVER fabricates
            a breach hydrograph). ``None`` (default) -> no breach.
        breach_peak_discharge_m3s: peak breach discharge (m^3/s, USER-supplied).
            Paired with ``breach_point`` to synthesize a triangular interior
            point-source jet. ``None`` (default).
        breach_arrival_hr: optional time-to-peak (hr) for the breach hydrograph;
            defaults to ~25% of the window. ``None`` (default).
        tsunami: TSUNAMI run (implies ``coastal``). USER-GATED: if ``True``
            WITHOUT ``tsunami_wave_height_m`` the run returns a typed
            ``USER_INPUT_REQUIRED`` failed envelope (the composer NEVER fabricates
            a wave height). ``False`` (default).
        tsunami_wave_height_m: peak tsunami wave amplitude (m, USER-supplied) ->
            a leading-depression N-wave waterlevel boundary. ``None`` (default).
        tsunami_period_min: tsunami characteristic period (min); defaults to ~15
            min (a SHAPE default, not a magnitude). ``None`` (default).
        project_id / session_id: ULID identifiers from the WS session. When
            ``None``, fresh ULIDs are minted (for direct-call / smoke).

    Returns:
        ``AssessmentEnvelope`` with ``envelope_type="modeled"``,
        ``hazard_type="flood"``, ``workflow_name="model_flood_scenario"``,
        and a populated ``flood: FloodPayload``. On success, ``layers``
        contains the flood-depth COG ``ResultLayer``; on failure the layer
        list is empty and ``FloodMetrics.solver_version`` carries the
        error code.
    """
    workflow_name = "model_flood_scenario"
    now = datetime.now(timezone.utc)
    proj_id = project_id or new_ulid()
    sess_id = session_id or new_ulid()
    data_sources: list[DataSource] = []
    solver_run_ids: list[str] = []
    grid_resolution_m = 30.0  # NFR-P-4 default; OQ-4 §4 immediate

    logger.info(
        "model_flood_scenario start bbox=%s location_query=%r event_id=%r "
        "return_period_yr=%s duration_hr=%s compute_class=%s "
        "forcing_raster_uri=%r",
        bbox,
        location_query,
        event_id,
        return_period_yr,
        duration_hr,
        compute_class,
        forcing_raster_uri,
    )

    # --- Coastal-AOI detection (SFINCS North Star P1) ---
    # Signal = explicit ``coastal`` flag OR ``surge_forcing`` present. A surge /
    # water-level boundary is physically incoherent on a land-only DEM (there is
    # no nearshore bed to route run-up over), so a surge request implies a
    # coastal AOI that needs the merged topo-bathymetry surface. This is a clean,
    # testable signal off the existing workflow inputs — no geometry/coastline
    # lookup needed. When False, the DEM fetch stays on ``fetch_dem`` exactly as
    # the v0.1 land/pluvial path (regression-critical).
    # ``quadtree`` (the cht_sfincs quadtree+SnapWave deck-build North Star) is a
    # coastal-only path — a wave-coupled run needs the merged topo-bathymetry
    # surface — so it implies coastal regardless of the explicit flag.
    # NATE 2026-06-26: scenario-coverage couplings. A ``compound`` run is BOTH a
    # coastal-surge AND a fluvial-discharge driver (plus the always-present
    # precip), so it lifts both ``coastal`` and ``river``. A ``tsunami`` run needs
    # the seaward bed + msk==2 boundary, so it implies ``coastal`` too. These are
    # additive — a pluvial run (all flags off) is byte-identical.
    # SPIDERWEB (2026-07-19): a storm (name+season, or a verbatim track URI)
    # implies coastal + the parametric hurricane wind path. The mutual-exclusion
    # with the ``wind`` param is validated as a typed USER_INPUT error below
    # (after bbox resolution, alongside the other input guards).
    storm_requested = bool(storm_name) or bool(storm_track_uri)
    coastal = bool(coastal) or bool(compound) or bool(tsunami) or storm_requested
    river = bool(river) or bool(compound)
    is_coastal = bool(coastal) or bool(surge_forcing) or bool(quadtree)
    logger.info(
        "model_flood_scenario coastal=%s (explicit=%s, surge_forcing=%s, "
        "quadtree=%s) — DEM fetch routes through %s",
        is_coastal,
        bool(coastal),
        bool(surge_forcing),
        bool(quadtree),
        "fetch_topobathy" if is_coastal else "fetch_dem",
    )

    # --- Animation cadence by sim type ("looks like rain" fix) ---
    # Coastal/wave -> a FINE minute-scale map-output stride so the animation
    # shows water rolling in; pluvial -> None (legacy hourly, byte-identical).
    resolved_output_interval_min = _resolve_output_interval_min(
        is_coastal=is_coastal,
        output_interval_min=output_interval_min,
        duration_hr=float(duration_hr),
    )
    logger.info(
        "model_flood_scenario output cadence: is_coastal=%s requested=%s -> "
        "resolved_interval_min=%s (~%d frames over %s h; pluvial=hourly)",
        is_coastal,
        output_interval_min,
        resolved_output_interval_min,
        _estimate_frame_count(
            output_interval_min=resolved_output_interval_min,
            duration_hr=float(duration_hr),
        ),
        duration_hr,
    )

    # --- Step 0: bbox resolution (Decision K; bbox-direct wins precedence) ---
    # audit #5: ``_resolve_bbox`` calls ``geocode_location`` -> a SYNC
    # ``requests.get`` to Nominatim (up to ~15s) plus a sync S3 cache read.
    # Run it off the loop so it cannot stall the WS keepalive while geocoding.
    # ``_resolve_bbox`` is EMIT-FREE (no current_emitter()/emit_*/
    # add_loaded_layer): it geocodes + does dict work then returns, so it is
    # safe to move to a worker thread. The async frame still emits around it
    # (the zoom-on-area-first emit below runs back on the loop).
    try:
        resolved_bbox, geocode_result = await asyncio.to_thread(
            _resolve_bbox, bbox=bbox, location_query=location_query
        )
    except WorkflowError as exc:
        # No bbox to anchor a failed envelope on; this is the rare fatal case.
        # Bubble up so the agent surface emits a top-level error frame.
        raise
    if geocode_result is not None:
        data_sources.append(
            DataSource(
                name="OpenStreetMap Nominatim",
                uri=f"nominatim:{geocode_result.get('osm_type','')}/{geocode_result.get('osm_id','')}",
                accessed_at=datetime.now(timezone.utc),
            )
        )

    # --- NATE 2026-06-26: USER-INPUT honesty gates (never fabricate magnitudes) ---
    # feedback_never_fabricate_model_inputs_user_gate: the levee-breach peak +
    # the tsunami wave height are PHYSICAL magnitudes the user MUST supply. If a
    # breach/tsunami intent is detected WITHOUT its magnitude, return a typed
    # USER_INPUT_REQUIRED failed envelope (honest gate) rather than inventing a
    # hydrograph / wave height. The drawn breach POINT alone is not enough — the
    # peak discharge governs the flood, so it must be explicit.
    if breach_point is not None and breach_peak_discharge_m3s is None:
        logger.info(
            "model_flood_scenario: breach_point given without "
            "breach_peak_discharge_m3s — returning USER_INPUT_REQUIRED (no "
            "fabricated breach hydrograph)."
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code="USER_INPUT_REQUIRED",
            error_detail=(
                "A levee-breach scenario needs the peak breach discharge "
                "(breach_peak_discharge_m3s, m^3/s) — please supply it; the "
                "breach hydrograph is not fabricated."
            ),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=None,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )
    if tsunami and tsunami_wave_height_m is None:
        logger.info(
            "model_flood_scenario: tsunami=True without tsunami_wave_height_m — "
            "returning USER_INPUT_REQUIRED (no fabricated wave height)."
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code="USER_INPUT_REQUIRED",
            error_detail=(
                "A tsunami scenario needs the peak wave height "
                "(tsunami_wave_height_m, m) — please supply it; the wave form "
                "is not fabricated."
            ),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=None,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )
    # SPIDERWEB (2026-07-19): storm (parametric hurricane) XOR the wind param.
    # Both would double-count the wind driver -> typed input error, never silent
    # precedence. Placed here (post-bbox) so the failed envelope carries a valid
    # resolved bbox for the pydantic validator.
    if storm_requested and wind:
        logger.info(
            "model_flood_scenario: storm_name/storm_track_uri given WITH wind "
            "param — returning STORM_WIND_CONFLICT (no silent precedence)."
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code="STORM_WIND_CONFLICT",
            error_detail=(
                "storm_name / storm_track_uri (parametric hurricane spiderweb) is "
                "mutually exclusive with the wind param (uniform/gridded wind) -- "
                "both would double-count the wind driver. Pass exactly one."
            ),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=None,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )

    # --- Zoom-on-area-first (job-0160): emit ``map-command(zoom-to)`` BEFORE
    # any compute starts. As soon as we have a bbox, the map zooms — the
    # user sees immediate response while the multi-minute SFINCS chain runs.
    # The emitter binding is set by ``PipelineEmitter.emit_tool_call`` via
    # the ``_CURRENT_EMITTER`` ContextVar; outside that scope (direct call,
    # smoke harness, unit test without an emitter) ``current_emitter()``
    # returns ``None`` and we skip silently — emitting a transient verb is
    # a UX nice-to-have, not a correctness gate.
    emitter = current_emitter()
    if emitter is not None:
        try:
            await emitter.emit_map_command(
                "zoom-to",
                {"bbox": list(resolved_bbox)},
            )
            logger.info(
                "model_flood_scenario: zoom-on-area-first emitted bbox=%s",
                resolved_bbox,
            )
        except Exception as exc:  # noqa: BLE001 — non-fatal UX hint
            logger.warning(
                "model_flood_scenario: zoom-on-area-first emit failed (non-fatal): %s",
                exc,
            )

    # --- Sub-step timeline plan (task-168) ----------------------------------
    # Declare the planned internal-operation count so the parent workflow card's
    # live breadcrumb can show "k/total" while it runs. The fused fetcher phase
    # counts as ONE substep (it runs as a single off-loop ``_fetcher_chain`` under
    # one timeout budget -- see below), then build + solve + postprocess + publish.
    # The quadtree (coastal North Star) path swaps the regular run_solver for the
    # combined deck-build+solve substep and adds a wave-postprocess substep. The
    # plan is best-effort + re-declarable; ``begin_substeps`` no-ops when no
    # emitter is bound (the verify/CI direct-call path) and degrades to label-only
    # if the real count diverges. Surfacing fewer is fine -- the breadcrumb just
    # shows the running index.
    _planned_substeps = (
        1  # fetcher phase (fetch_topobathy/fetch_dem + landcover + river + precip)
        + 1  # build_sfincs_model
        + 1  # solve (run_solver/wait_for_completion OR the combined quadtree run)
        + 1  # postprocess_flood
        + 1  # publish_layer (peak depth)
        + (1 if quadtree else 0)  # postprocess_waves (quadtree+SnapWave only)
    )
    begin_substeps(emitter, _planned_substeps)

    # --- Step 1-4: atomic-tool fetcher chain ---
    forcing_summary: ForcingSummary | None = None
    # job-0225 v2: ``precip_inches`` is the Atlas 14 design-storm depth (None
    # on the observed-raster path); ``precip_magnitude_mm_per_hr`` is the
    # pre-computed uniform netamt rate (None on the design-storm path).
    precip_inches: float | None = None
    precip_magnitude_mm_per_hr: float | None = None
    # Pre-solver progress (terminal-pipeline-card hardening): nudge the card so
    # it is never SILENT during the multi-second fetcher chain.
    await _emit_presolver_progress(emitter, 5)
    # The fetcher chain + ForcingSummary build is SYNCHRONOUS, blocking I/O
    # (HTTP fetches, GDAL VSI reads with no overall timeout). Run it off the
    # event loop in a worker thread and bound it with ``asyncio.wait_for`` so a
    # wedged endpoint surfaces as a typed PRESOLVER_TIMEOUT failed envelope
    # instead of an INFINITE silent await (NATE's "120 min, never finished").
    # The closure mutates ``data_sources`` / ``forcing_summary`` etc. via a
    # results container; single worker thread, sequential, no concurrent reader.
    _fetch_out: dict[str, Any] = {}

    def _fetcher_chain() -> None:
        nonlocal precip_inches, precip_magnitude_mm_per_hr, forcing_summary
        # --- DEM fetch: COASTAL branch (fetch_topobathy) vs LAND/pluvial branch
        # (fetch_dem). Both return a LayerURI with .uri pointing at a single-band
        # float32 NAVD88-metres COG (positive-up, bathymetry NEGATIVE on the
        # coastal path, NO sign flip) in the SAME contract, so the downstream
        # build_sfincs_model(dem_uri=...) seam is identical for both. The
        # non-coastal branch is byte-identical to the v0.1 workflow.
        if is_coastal:
            # ``fetch_topobathy`` REUSES fetch_dem internally for the 3DEP land
            # DEM and merges NOAA NCEI CUDEM bathymetry on top (CUDEM wins on the
            # coast). It DEGRADES internally to 3DEP-land-only with an honest
            # fallback_warning if CUDEM is missing for the AOI (never a silent
            # dead-end); a hard failure (no CUDEM AND no 3DEP, bad bbox, datum
            # mismatch) raises a TopobathyError carrying an error_code that the
            # outer handler threads into the failed envelope.
            topobathy_layer = fetch_topobathy(
                resolved_bbox, resolution_m=int(grid_resolution_m)
            )
            dem_layer = topobathy_layer
            _bathy_present = bool(getattr(topobathy_layer, "bathymetry_present", True))
            _tile_count = int(getattr(topobathy_layer, "cudem_tile_count", 0))
            _fallback_warning = getattr(topobathy_layer, "fallback_warning", None)
            data_sources.append(
                DataSource(
                    name=(
                        "NOAA NCEI CUDEM + USGS 3DEP (merged topo-bathymetry)"
                        if _bathy_present
                        else "USGS 3DEP (topobathy fallback: bathymetry ABSENT)"
                    ),
                    uri=dem_layer.uri,
                    accessed_at=datetime.now(timezone.utc),
                )
            )
            if not _bathy_present:
                logger.warning(
                    "model_flood_scenario: coastal AOI but fetch_topobathy "
                    "degraded to 3DEP-land-only (cudem_tile_count=%s) — %s",
                    _tile_count,
                    _fallback_warning
                    or "bathymetry absent; coastal inundation under-represented",
                )
        else:
            dem_layer = fetch_dem(resolved_bbox, resolution_m=int(grid_resolution_m))
            data_sources.append(
                DataSource(
                    name="USGS 3DEP",
                    uri=dem_layer.uri,
                    accessed_at=datetime.now(timezone.utc),
                )
            )
        landcover_result = fetch_landcover(resolved_bbox, dataset="nlcd_2021")
        landcover_layer: LayerURI = landcover_result["layer"]
        nlcd_vintage_year = int(landcover_result.get("nlcd_vintage_year"))
        data_sources.append(
            DataSource(
                name=f"NLCD {nlcd_vintage_year} (MRLC WMS)",
                uri=landcover_layer.uri,
                accessed_at=datetime.now(timezone.utc),
            )
        )
        # job-0307: river geometry is BEST-EFFORT for the v0.1 pluvial deck.
        # ``build_sfincs_model`` does NOT emit ``setup_river_inflow`` for v0.1
        # pluvial (job-0055) — ``river_geometry_uri`` is accepted but unused, and
        # documented as ``may be None``. So a river-fetch failure must NOT kill an
        # otherwise-valid pluvial flood. Live Case 3 (2026-06-16): Victoria, TX
        # failed with "could not route bbox … to a HUC4 region" (the OQ-39 v0.1
        # HUC4 heuristic only covers a few demo areas), needlessly aborting a
        # flood that needs no river inflow. Degrade to None + narrate; re-enable
        # the hard dependency when v0.2 river-inflow (real ATCF surge) lands.
        river_layer: LayerURI | None
        try:
            river_layer = fetch_river_geometry(resolved_bbox, source="nhdplus_hr")
            data_sources.append(
                DataSource(
                    name="NHDPlus HR (USGS)",
                    uri=river_layer.uri,
                    accessed_at=datetime.now(timezone.utc),
                )
            )
        except Exception as exc:  # noqa: BLE001 — river is optional for pluvial
            logger.warning(
                "model_flood_scenario: fetch_river_geometry failed for bbox=%s "
                "(%s) — proceeding WITHOUT river geometry (pluvial deck does not "
                "use river inflow; job-0055/job-0307).",
                resolved_bbox,
                exc,
            )
            river_layer = None
        if forcing_raster_uri is not None:
            # --- job-0225 v2: OBSERVED-precip forcing branch (Case 3) ---
            # Compute the AREA-MEAN accumulated precip over the model domain
            # and convert to a uniform SFINCS netamt rate (mm/hr). ``duration_hr``
            # is reused as the accumulation window. The Atlas 14 design-storm
            # lookup is SKIPPED entirely on this path.
            precip_magnitude_mm_per_hr, area_mean_mm = (
                compute_precip_area_mean_mm_per_hr(
                    forcing_raster_uri=forcing_raster_uri,
                    bbox=resolved_bbox,
                    accumulation_hours=float(duration_hr),
                )
            )
            data_sources.append(
                DataSource(
                    name="Observed precipitation raster (area-mean netamt)",
                    uri=forcing_raster_uri,
                    accessed_at=datetime.now(timezone.utc),
                )
            )
            # Envelope-side ``ForcingSummary.forcing_type`` is a contract-owned
            # Literal that does NOT (yet) include ``"pluvial_observed"`` — the
            # observed precip raster IS a pluvial-precip forcing on the same
            # SFINCS netamt path, so we summarise it as ``"pluvial_synthetic"``
            # and carry the observed/area-mean distinction in the free-form
            # ``parameters`` dict (``forcing_mode="area_mean_netamt"`` +
            # ``forcing_raster_uri``) + the human-readable ``source``. The
            # ENGINE-internal ``ForcingSpec.forcing_type`` (below) is
            # ``"pluvial_observed"`` — that drives the deck-builder branch and
            # is engine-owned. A future schema amendment could add a dedicated
            # ``"pluvial_observed"`` envelope literal (OQ-225-OBSERVED-FORCING-
            # LITERAL — propose to the schema specialist).
            forcing_summary = ForcingSummary(
                forcing_type="pluvial_synthetic",
                source=(
                    f"Observed precip raster {forcing_raster_uri} — "
                    f"area-mean {area_mean_mm:.2f} mm over {duration_hr}-hr "
                    "accumulation → uniform netamt (OQ-6 area-mean fallback)"
                ),
                parameters={
                    "forcing_raster_uri": forcing_raster_uri,
                    "area_mean_mm": area_mean_mm,
                    "precip_magnitude_mm_per_hr": precip_magnitude_mm_per_hr,
                    "accumulation_hours": float(duration_hr),
                    "forcing_mode": "area_mean_netamt",
                },
                inputs_uri=forcing_raster_uri,
            )
        else:
            # --- Atlas 14 design-storm path (v1 behavior, unchanged) ---
            mid_lon = 0.5 * (resolved_bbox[0] + resolved_bbox[2])
            mid_lat = 0.5 * (resolved_bbox[1] + resolved_bbox[3])
            precip_result = lookup_precip_return_period(
                location=(mid_lat, mid_lon),
                return_period_years=return_period_yr,
                duration_hours=float(duration_hr),
            )
            precip_inches = float(precip_result["precip_inches"])
            data_sources.append(
                DataSource(
                    name=precip_result.get("vintage_volume", "NOAA Atlas 14"),
                    uri="noaa-atlas14-pfds",
                    accessed_at=datetime.now(timezone.utc),
                )
            )
            forcing_summary = ForcingSummary(
                forcing_type="pluvial_synthetic",
                source=(
                    f"{precip_result.get('vintage_volume', 'NOAA Atlas 14')} — "
                    f"{return_period_yr}-yr / {duration_hr}-hr design storm"
                ),
                parameters={
                    "precip_inches": precip_inches,
                    "duration_hours": float(duration_hr),
                    "return_period_years": return_period_yr,
                    "vintage_volume": precip_result.get("vintage_volume"),
                    "project_area": precip_result.get("project_area"),
                },
            )
        # Hand the downstream-needed locals back to the async frame.
        _fetch_out["dem_layer"] = dem_layer
        _fetch_out["landcover_layer"] = landcover_layer
        _fetch_out["nlcd_vintage_year"] = nlcd_vintage_year
        _fetch_out["river_layer"] = river_layer
        # ``_bathy_present`` is only assigned on the coastal branch; default True
        # for the land/pluvial path (no bathymetry concept there). The quadtree
        # deck-build (coastal) reads it to flag a wave-coupled run.
        _fetch_out["bathymetry_present"] = bool(locals().get("_bathy_present", True))

    try:
        # task-168: surface the fused data-fetch phase as ONE nested child row
        # under the parent workflow card. The chain runs ALL fetchers
        # (fetch_topobathy/fetch_dem + fetch_landcover + fetch_river_geometry +
        # lookup_precip_return_period/compute_precip_area_mean) inside a SINGLE
        # off-loop ``_fetcher_chain`` under ONE timeout budget (the hardened
        # terminal-card block), so it cannot be split into per-fetcher async
        # substeps without unwinding that budget - it is wrapped as one substep
        # labelled by the dominant DEM pull (the web humanizes it). ``substep`` is
        # a no-op when no emitter is bound (verify/CI direct-call path), so the
        # ``wait_for``/``to_thread`` body below is byte-identical there. A timeout
        # raises ``asyncio.TimeoutError`` INSIDE the substep -> the child reads red
        # (honesty floor) and the error re-raises to the existing except cascade,
        # which returns the PRESOLVER_TIMEOUT failed envelope unchanged.
        async with substep(
            emitter, "fetch_topobathy" if is_coastal else "fetch_dem"
        ):
            # NO-RECONNECT (NATE 2026-06-29): the fetcher chain pulls DEM /
            # topobathy / landcover OFF the loop in a worker thread and is SILENT
            # on the wire for tens of seconds (a novel-AOI CUDEM/3DEP merge is the
            # long pole). Drive a periodic pipeline-state DATA frame so the browser
            # WS watchdog stays reset (no ~30 s force-reconnect) + the user sees the
            # fetch is alive. Cancelled the instant the chain returns/raises.
            _fetch_progress_task = asyncio.ensure_future(
                _drive_presolver_phase_progress(
                    emitter, start_pct=5, end_pct=24, expected_seconds=60.0
                )
            )
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(_fetcher_chain),
                    timeout=_FETCHER_PHASE_TIMEOUT_S,
                )
            finally:
                _fetch_progress_task.cancel()
                try:
                    await _fetch_progress_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
    except asyncio.CancelledError:
        # Invariant 8: a true cancel propagates (mark_cancelled fires upstream).
        raise
    except asyncio.TimeoutError:
        logger.warning(
            "model_flood_scenario: fetcher chain exceeded %.0fs budget for "
            "bbox=%s — returning PRESOLVER_TIMEOUT failed envelope (a hang is "
            "now bounded + visible, not an infinite silent await).",
            _FETCHER_PHASE_TIMEOUT_S,
            resolved_bbox,
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code="PRESOLVER_TIMEOUT",
            error_detail=(
                f"data-fetch phase exceeded {_FETCHER_PHASE_TIMEOUT_S:.0f}s "
                "(a data endpoint or terrain/landcover read stalled)"
            ),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )
    except TopobathyError as exc:
        # COASTAL DEM hard failure (no CUDEM AND no 3DEP, bad bbox, datum
        # mismatch). The soft "CUDEM missing, 3DEP present" case does NOT reach
        # here — fetch_topobathy degrades internally and returns a result. This
        # is the honest dead-end: thread the typed error_code into the failed
        # envelope (Invariant 7 — never a fabricated topobathy success).
        logger.warning(
            "model_flood_scenario: fetch_topobathy hard-failed for coastal "
            "bbox=%s (%s / %s) — returning failed envelope.",
            resolved_bbox,
            exc.error_code,
            exc,
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code=exc.error_code,
            error_detail=str(exc),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("fetcher chain failed: %s", exc)
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code=getattr(exc, "error_code", "FETCHER_FAILED"),
            error_detail=str(exc),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )

    dem_layer = _fetch_out["dem_layer"]
    landcover_layer = _fetch_out["landcover_layer"]
    nlcd_vintage_year = _fetch_out["nlcd_vintage_year"]
    river_layer = _fetch_out["river_layer"]
    bathymetry_present = bool(_fetch_out.get("bathymetry_present", True))

    # --- task #207: surface the SFINCS INPUT data as renderable layers --------
    # The engine consumes renderable inputs (DEM/topobathy, NLCD landcover,
    # NHDPlus rivers) but historically only the RESULT (flood-depth) was
    # published. Surface them now as role="input" so the user sees the terrain /
    # landcover / river network the model actually ran on. ALL best-effort
    # (publish_input_layer never raises): a failure to surface an input can NEVER
    # fail the solve. Gated on ``emitter is not None`` (no-op on the verify/CI
    # direct-call path).
    if emitter is not None:
        # Stable per-turn id base for the surfaced input layer_ids (the solver
        # run_id is not minted until AFTER the solve, below; an input is surfaced
        # PRE-solve so the user sees the terrain/landcover/rivers immediately).
        _input_id_base = new_ulid()
        # (a) RIVERS — a VECTOR already carrying role="input"; no publish_layer
        #     round-trip (the s3:// FlatGeobuf inlines server-side, job-0175).
        if river_layer is not None:
            await publish_input_layer(emitter, river_layer)

        # (b) DEM + LANDCOVER — RASTERs carrying a raw s3:// COG, which MapLibre
        #     cannot fetch; each needs a publish_layer round-trip to mint a
        #     renderable tile/WMS URL FIRST, then emit as role="input" with its
        #     existing preset (continuous_dem / categorical_landcover resolve in
        #     the TiTiler registry). publish_layer runs a sync worker-poll loop ->
        #     OFFLOADED off the loop. On AWS publish_layer fails until QGIS-on-AWS
        #     lands (job-0308); the input is then simply absent (honest no-surface,
        #     never fatal) — exactly like the result-layer publish-or-drop gate.
        for _raster_in, _fallback_preset, _kind in (
            (dem_layer, "continuous_dem", "DEM"),
            (landcover_layer, "categorical_landcover", "landcover"),
        ):
            if _raster_in is None:
                continue
            try:
                _layer_id = f"input-{_kind.lower()}-{_input_id_base}"
                _wms_url = await asyncio.to_thread(
                    publish_layer,
                    layer_uri=_raster_in.uri,
                    layer_id=_layer_id,
                    style_preset=_raster_in.style_preset or _fallback_preset,
                )
                _renderable = _raster_in.model_copy(
                    update={
                        "layer_id": _layer_id,
                        "uri": _wms_url,
                        "role": "input",
                        "bbox": None,
                        "style_preset": _raster_in.style_preset or _fallback_preset,
                    }
                )
                await publish_input_layer(emitter, _renderable)
            except PublishLayerError as exc:
                logger.warning(
                    "model_flood_scenario: %s input publish failed (non-fatal, "
                    "input absent until QGIS-on-AWS) error_code=%s: %s",
                    _kind,
                    getattr(exc, "error_code", "?"),
                    exc,
                )
            except Exception as exc:  # noqa: BLE001 - input surfacing is NEVER fatal
                logger.warning(
                    "model_flood_scenario: %s input surface failed (non-fatal): %s",
                    _kind,
                    exc,
                )

    await _emit_presolver_progress(emitter, 25)

    # --- Step 5: build_sfincs_model with NLCD validation gate ---
    try:
        # COASTAL SFINCS — surge / tide / discharge / wind / pressure members.
        # ``surge_forcing`` is a nested dict of forcing URIs. Two shapes are
        # accepted: (a) PRE-MATERIALISED — sub-dicts already carry
        # ``timeseries_uri`` / ``locations_uri`` / ``geodataset_uri`` (consumed
        # verbatim); (b) RAW FETCHER — sub-dicts carry ``fetch_uri`` (a GTSM /
        # CO-OPS / NWM FlatGeobuf) or ``cama_cog_uri``, which the forcing ADAPTER
        # (sfincs_forcing_adapter) converts into the bzs/dis CSV + locations files
        # the deck-emission seam expects. The resolver materialises (b) in place;
        # an adapter failure for an EXPLICIT surge request raises (caught below as
        # a typed failed envelope — never a silent pluvial degrade). Empty/absent
        # → pure-pluvial deck (no surge blocks emitted).
        # NO-SYNC-BLOCKING-ON-LOOP: the forcing adapter does heavy synchronous
        # geopandas/rasterio/pandas work (reads the GTSM/CO-OPS/NWM FlatGeobuf,
        # samples the CaMa COG, writes the bzs/dis CSV + locations files). Run it
        # off the event loop so the WS heartbeat + keepalive stay responsive on
        # the coastal surge path (otherwise the loop stalls -> the client sees
        # ~30s of silence -> force-reconnect -> the turn's socket dies 1005).
        #
        # COASTAL AUTO-WIRE (the fix): a coastal run with NO explicit
        # ``surge_forcing`` used to silently degrade to a pure-RAINFALL deck
        # (``fetch_topobathy`` only deepened the bed  -  no sea water entered). For a
        # coastal AOI we now AUTO-WIRE a time-varying sea-surge water-level
        # boundary (CO-OPS primary -> GTSM -> parametric last-resort) so water
        # rises from the sea and marches inland across the frames. Gated strictly
        # on ``is_coastal`` so the inland / pluvial path is byte-identical (no
        # surge boundary, branch never taken). The fetcher fan-out does sync
        # network I/O, so it runs off the loop alongside the resolve.
        # NATE 2026-06-26: scenario-coverage auto-wire precedence ladder. Each
        # branch fills the SAME ``surge_forcing`` dict so compound combinations
        # compose; all run BEFORE ``_resolve_surge_forcing_from_fetchers`` so any
        # ``fetch_uri`` gets materialised. Order: tsunami -> coastal storm-surge
        # (only if no waterlevel yet) -> fluvial/breach discharge -> wind merge.
        surge_forcing = dict(surge_forcing) if surge_forcing else {}

        # 1) TSUNAMI waterlevel (pre-materialised N-wave) — the magnitude gate
        #    already fired above, so a height is present here. Sets a sentinel so
        #    the coastal storm-surge synth below does NOT also fire (a tsunami is
        #    NOT a storm surge).
        if tsunami and tsunami_wave_height_m is not None and not surge_forcing.get(
            "waterlevel"
        ):
            _tsu = await asyncio.to_thread(
                _synthesize_tsunami_waterlevel_forcing,
                resolved_bbox,
                wave_height_m=float(tsunami_wave_height_m),
                period_min=tsunami_period_min,
                duration_hr=float(duration_hr),
            )
            surge_forcing["waterlevel"] = _tsu
            data_sources.append(
                DataSource(
                    name=(
                        "Tsunami N-wave water level (auto-wired; "
                        f"height {tsunami_wave_height_m} m)"
                    ),
                    uri="synthetic:tsunami-nwave",
                    accessed_at=datetime.now(timezone.utc),
                )
            )

        # 1.5) SPIDERWEB (2026-07-19): parametric hurricane wind+pressure. When a
        #      storm is requested, resolve the IBTrACS track + build the Holland
        #      .spw here and SUPPRESS the parametric surge synthesis below (the
        #      spw wind+pressure GENERATES the surge; a parametric bzs would
        #      double-count it). We still emit a FLAT low tide-base bzs so the
        #      deck keeps msk=2 boundary cells (setup_mask_bounds is gated on a
        #      waterlevel member) and the offshore boundary sits at tide level.
        #      Runs off the loop (fetch + geopandas + Holland build are sync).
        spiderweb_member: "SpiderwebForcing | None" = None
        spiderweb_prov: dict[str, Any] = {}
        spiderweb_utm_epsg: int | None = None
        if storm_requested:
            spiderweb_member, spiderweb_prov = await asyncio.to_thread(
                _resolve_spiderweb_forcing,
                resolved_bbox,
                duration_hr=float(duration_hr),
                storm_name=storm_name,
                storm_season=storm_season,
                storm_track_uri=storm_track_uri,
                data_sources=data_sources,
            )
            spiderweb_utm_epsg = spiderweb_prov.get("utm_epsg")
            # tide-base bzs (msk=2 cells) in place of the parametric surge.
            if not surge_forcing.get("waterlevel"):
                _tide = await asyncio.to_thread(
                    _synthesize_tide_base_forcing,
                    resolved_bbox,
                    duration_hr=float(duration_hr),
                )
                surge_forcing = {**surge_forcing, "waterlevel": _tide}
            data_sources.append(
                DataSource(
                    name=(
                        f"Hurricane {storm_name or 'storm'} parametric spiderweb "
                        f"(Holland; landfall {spiderweb_prov.get('landfall_iso','?')}, "
                        f"RMW {spiderweb_prov.get('rmw_source','?')})"
                    ),
                    uri=str(spiderweb_prov.get("track_uri") or "synthetic:spiderweb"),
                    accessed_at=datetime.now(timezone.utc),
                )
            )

        # 2) COASTAL storm-surge auto-wire — only when no waterlevel is present
        #    yet (a tsunami / explicit surge / spiderweb tide-base already wins).
        #    SUPPRESSED for the spiderweb path (storm_requested) so the surge is
        #    generated by the spw wind+pressure, never double-counted.
        if is_coastal and not storm_requested and not surge_forcing.get("waterlevel"):
            _surge = await asyncio.to_thread(
                _autowire_coastal_surge_forcing,
                resolved_bbox,
                duration_hr=float(duration_hr),
                return_period_yr=return_period_yr,
                data_sources=data_sources,
            )
            if _surge:
                surge_forcing = {**surge_forcing, **_surge}

        # 3a) LEVEE-BREACH discharge (pre-materialised interior jet) — distinct
        #     from a domain-edge river discharge; carried onto ForcingSpec.breach
        #     so a compound run can have BOTH. The magnitude gate already fired.
        breach_member = None
        if breach_point is not None and breach_peak_discharge_m3s is not None:
            _br = await asyncio.to_thread(
                _synthesize_breach_discharge_forcing,
                (float(breach_point[0]), float(breach_point[1])),
                peak_m3s=float(breach_peak_discharge_m3s),
                arrival_hr=breach_arrival_hr,
                duration_hr=float(duration_hr),
            )
            breach_member = DischargeForcing(
                timeseries_uri=_br.get("timeseries_uri"),
                locations_uri=_br.get("locations_uri"),
            )
            data_sources.append(
                DataSource(
                    name=(
                        "Levee-breach discharge (auto-wired; "
                        f"peak {breach_peak_discharge_m3s} m^3/s)"
                    ),
                    uri="synthetic:levee-breach",
                    accessed_at=datetime.now(timezone.utc),
                )
            )

        # 3b) FLUVIAL river discharge auto-wire (NWM -> NWIS -> honest skip).
        #     Gated on ``river`` (lifted by ``compound``); does NOT force
        #     is_coastal. Skipped when a discharge boundary is already present.
        if river and not surge_forcing.get("discharge"):
            _dq_wire = await asyncio.to_thread(
                _autowire_river_discharge_forcing,
                resolved_bbox,
                duration_hr=float(duration_hr),
                data_sources=data_sources,
                river_layer_uri=(river_layer.uri if river_layer is not None else None),
            )
            if _dq_wire:
                surge_forcing = {**surge_forcing, **_dq_wire}

        # 4) WIND merge (user/ERA5-supplied; never fabricated).
        if wind:
            surge_forcing = {**surge_forcing, "wind": dict(wind)}

        surge_forcing = await asyncio.to_thread(
            _resolve_surge_forcing_from_fetchers,
            surge_forcing or None,
            resolved_bbox,
            window_hours=float(duration_hr),
            data_sources=data_sources,
        )
        _wl, _dq, _wind, _press = _build_surge_forcing_members(surge_forcing)

        # NATE 2026-06-26: INFILTRATION loss + ADVANCED-PHYSICS resolution.
        # Infiltration auto-fetches the GCN250 CN raster (best-effort) into an
        # InfiltrationForcing member. advanced_physics defaults to {"advection":1}
        # when WIND forcing is present (so a wind run flips the momentum scheme
        # rather than emitting wind with the deck default); validated via
        # physics_registry and threaded onto BuildOptions below.
        infiltration_member = None
        _inf_uri = await asyncio.to_thread(
            _resolve_infiltration_uri,
            infiltration,
            resolved_bbox,
            data_sources,
        )
        if _inf_uri:
            # Single-band GCN250 raster -> antecedent_moisture None (the deck
            # emits YAML null; the default 'avg' ValueErrors on a bare band).
            infiltration_member = InfiltrationForcing(
                cn_uri=_inf_uri,
                antecedent_moisture=None,
                provenance={"_prov_source": "gcn250"},
            )

        resolved_advanced_physics = advanced_physics
        if resolved_advanced_physics is None and (wind or spiderweb_member is not None):
            # NATE 2026-06-26 (doc-grounding): SFINCS coriolis is on-by-default but
            # INERT while latitude==0.0 on a projected CRS, so a wind deck that omits
            # latitude silently runs WITHOUT Coriolis (parameters.html). Pin the
            # AOI-centre latitude alongside advection=1 so a wind run flips the
            # momentum scheme AND activates Coriolis. Never overrides an explicit
            # advanced_physics dict (that path leaves the user fully in control).
            _aoi_centre_lat = 0.5 * (float(resolved_bbox[1]) + float(resolved_bbox[3]))
            resolved_advanced_physics = {
                "advection": 1,
                "coriolis_latitude": _aoi_centre_lat,
            }
        if forcing_raster_uri is not None:
            # Observed-precip netamt path: carry the pre-computed magnitude.
            forcing_spec = ForcingSpec(
                forcing_type="pluvial_observed",
                duration_hours=float(duration_hr),
                precip_magnitude_mm_per_hr=precip_magnitude_mm_per_hr,
                waterlevel=_wl,
                discharge=_dq,
                # NATE 2026-06-26: scenario-coverage members (breach jet +
                # infiltration loss). None on a pluvial run (byte-identical).
                breach=breach_member,
                wind=_wind,
                pressure=_press,
                # SPIDERWEB (2026-07-19): parametric hurricane wind+pressure. None
                # on every non-storm run (byte-identical). XOR wind/pressure is
                # enforced in the emitter (storm_requested already suppressed the
                # wind param via STORM_WIND_CONFLICT).
                wind_spiderweb=spiderweb_member,
                infiltration=infiltration_member,
                provenance=dict(forcing_summary.parameters if forcing_summary else {}),
            )
        else:
            forcing_spec = ForcingSpec(
                forcing_type="pluvial_synthetic",
                precip_inches=precip_inches,
                duration_hours=float(duration_hr),
                return_period_years=return_period_yr,
                waterlevel=_wl,
                discharge=_dq,
                # NATE 2026-06-26: scenario-coverage members (breach jet +
                # infiltration loss). None on a pluvial run (byte-identical).
                breach=breach_member,
                wind=_wind,
                pressure=_press,
                # SPIDERWEB (2026-07-19): None on every non-storm run (byte-identical).
                wind_spiderweb=spiderweb_member,
                infiltration=infiltration_member,
                provenance=dict(forcing_summary.parameters if forcing_summary else {}),
            )
        # COASTAL SFINCS — building-obstacle URI. ``building_obstacles=True``
        # triggers a BEST-EFFORT OSM-footprint fetch (so a footprint-fetch
        # failure NEVER kills the flood — same degrade policy as river geometry,
        # job-0307); a string is used verbatim as the obstacle geofile URI.
        # NO-SYNC-BLOCKING-ON-LOOP: a True building_obstacles triggers a
        # synchronous OSM Overpass footprint fetch (network I/O). Off-load it so
        # the loop keeps servicing the WS heartbeat.
        building_obstacle_uri = await asyncio.to_thread(
            _resolve_building_obstacle_uri,
            building_obstacles,
            resolved_bbox,
            data_sources,
        )
        # NATE 2026-06-26: resolve the advanced-physics overrides ONCE (a single
        # resolve point) via the registry so an unknown key / out-of-range value
        # raises a typed error here (caught below as a failed envelope) rather
        # than emitting a silently-wrong deck. None -> {} (deck byte-identical).
        _resolved_physics = validate_and_resolve_physics(
            "sfincs", resolved_advanced_physics
        )
        # SPIDERWEB (2026-07-19): the spw eye coords are lon/lat and SFINCS
        # converts them to the GRID's UTM (utmzone). So the grid MUST be built in
        # the AOI UTM CRS (e.g. EPSG:32616 for Mexico Beach), overriding the
        # EPSG:3857 BuildOptions default. Proven byte-for-byte in the docker smoke
        # (utmzone=16n grid in EPSG:32616). None-guarded so a non-storm run keeps
        # the default crs.
        _spw_crs = (
            f"EPSG:{spiderweb_utm_epsg}"
            if (spiderweb_member is not None and spiderweb_utm_epsg)
            else None
        )
        options = BuildOptions(
            grid_resolution_m=grid_resolution_m,
            simulation_hours=float(duration_hr),
            # SPIDERWEB: UTM crs override (else BuildOptions default EPSG:3857).
            **({"crs": _spw_crs} if _spw_crs else {}),
            # sprint-16: feed the compute_class through so the adaptive-grid cap
            # is sized against the right instance vCPU (the cap derives from the
            # solve budget + vCPU via the perf model). build_sfincs_model snaps
            # grid_resolution_m UP if the estimated active-cell count overruns.
            compute_class=compute_class,
            # COASTAL SFINCS — subgrid + building-obstacle mask (urban flood).
            # Subgrid is auto-enabled when buildings are present (the obstacle
            # "raise" mode needs it; "exclude" benefits from sub-cell topography).
            enable_subgrid=bool(enable_subgrid or building_obstacle_uri),
            building_obstacle_uri=building_obstacle_uri,
            building_obstacle_mode=building_obstacle_mode,
            # COASTAL/WAVE animation cadence: a fine minute-scale map-output
            # stride for a coastal/wave run, None (legacy hourly) for pluvial.
            # Drives dtout/dtmaxout in the regular-grid deck (the quadtree path
            # threads the same value into the remote deck-build output_dt below).
            output_interval_min=resolved_output_interval_min,
            # NATE 2026-06-26: resolved advanced-physics dict (advection / theta /
            # alpha / huthresh / coriolis_latitude / wind_drag) -> setup_config
            # block. None/{} -> no physics override (byte-identical pluvial deck).
            advanced_physics=(_resolved_physics or None),
        )
        # ``build_sfincs_model`` is SYNCHRONOUS with no overall timeout
        # (sfincs_builder GDAL VSI cache/timeout is per-read only). Run it off
        # the loop + bound it so a wedged build surfaces as PRESOLVER_TIMEOUT
        # rather than an infinite silent await.
        # task-168: surface the deck build as a nested child row. A build timeout
        # (TimeoutError), the NLCD validation gate (SFINCSSetupError), or a forcing
        # adapter failure raises INSIDE the substep -> the child reads red (honesty
        # floor) and re-raises to the existing except cascade below, which returns
        # the corresponding failed envelope unchanged. No-op when no emitter bound.
        async with substep(emitter, "build_sfincs_model"):
            # NO-RECONNECT (NATE 2026-06-29): build_sfincs_model (hydromt: DEM
            # reproject + active-mask + manning rasterize + deck write + S3
            # upload) is the longest pre-solver phase (~70 s for a city AOI) and
            # runs OFF the loop in a worker thread -- SILENT on the wire. Without a
            # periodic frame the browser WS watchdog trips and force-reconnects
            # mid-build, so the run appears to hang/go dark even though it is
            # healthy and dispatches to Batch. Drive a pipeline-state tick so the
            # connection stays up + the card visibly advances. Cancelled the
            # instant the build returns/raises (the child is still ``running``
            # here, so update_current_progress targets THIS step).
            _build_progress_task = asyncio.ensure_future(
                _drive_presolver_phase_progress(
                    emitter, start_pct=30, end_pct=88, expected_seconds=90.0
                )
            )
            try:
                model_setup = await asyncio.wait_for(
                    asyncio.to_thread(
                        build_sfincs_model,
                        dem_uri=dem_layer.uri,
                        landcover_uri=landcover_layer.uri,
                        # job-0307: None when the best-effort river fetch failed
                        # (pluvial deck ignores it; build_sfincs_model documents
                        # river_geometry_uri as "may be None").
                        river_geometry_uri=(
                            river_layer.uri if river_layer is not None else None
                        ),
                        forcing=forcing_spec,
                        bbox=resolved_bbox,
                        options=options,
                        nlcd_vintage_year=nlcd_vintage_year,
                    ),
                    timeout=_BUILD_PHASE_TIMEOUT_S,
                )
            finally:
                _build_progress_task.cancel()
                try:
                    await _build_progress_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        # build_sfincs_model may snap grid_resolution_m UP (coarsen) if the
        # estimated active-cell count overruns the per-job cell cap. Refresh the
        # workflow-local resolution from the ACTUALLY-BUILT value so downstream
        # consumers — the solve-telemetry record (cells/resolution/vCPU/wall) and
        # any envelope metrics — report the resolution the solver really ran at,
        # not the pre-coarsen 30 m request.
        _built_res = getattr(model_setup, "grid_resolution_m", None)
        if _built_res:
            grid_resolution_m = float(_built_res)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning(
            "model_flood_scenario: build_sfincs_model exceeded %.0fs budget for "
            "bbox=%s — returning PRESOLVER_TIMEOUT failed envelope.",
            _BUILD_PHASE_TIMEOUT_S,
            resolved_bbox,
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code="PRESOLVER_TIMEOUT",
            error_detail=(
                f"SFINCS model build exceeded {_BUILD_PHASE_TIMEOUT_S:.0f}s"
            ),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )
    except SFINCSSetupError as exc:
        # The headline failure path — LULC_MAPPING_MISMATCH and friends
        # surface here. Invariant 7: the failed envelope carries the error
        # code instead of a fabricated FloodPayload.
        logger.warning(
            "build_sfincs_model raised %s (details=%s) — returning failed envelope",
            exc.error_code,
            exc.details,
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code=exc.error_code,
            error_detail=str(exc),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )
    except SFINCSForcingAdapterError as exc:
        # COASTAL SFINCS — the surge/discharge FETCHER → ADAPTER bridge failed
        # (unreadable fetcher FGB/COG, no usable stations, all-NaN hydrographs).
        # Invariant 7: an EXPLICIT surge request that cannot be materialised
        # surfaces as a typed failed envelope carrying the adapter error code —
        # NOT a silent degrade to a pluvial-only deck.
        logger.warning(
            "surge forcing adapter raised %s (details=%s) — returning failed envelope",
            exc.error_code,
            exc.details,
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code=exc.error_code,
            error_detail=str(exc),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )
    except PhysicsRegistryError as exc:
        # NATE 2026-06-26: an invalid ``advanced_physics`` override (unknown key
        # or out-of-range value) surfaces as a typed failed envelope rather than
        # an uncaught exception or a silently-wrong deck (Invariant 7).
        logger.warning(
            "model_flood_scenario: invalid advanced_physics override (%s) — "
            "returning ADVANCED_PHYSICS_INVALID failed envelope.",
            exc,
        )
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code="ADVANCED_PHYSICS_INVALID",
            error_detail=str(exc),
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )

    # Pre-solver phases done — the long solve takes over progress emission from
    # here (wait_for_completion drives the binding). Stamp the hand-off so the
    # card shows clear forward motion into Step 7.
    await _emit_presolver_progress(emitter, 40)

    # --- Step 5.5: solve dispatch prep (quadtree combined-Batch path removed) ---
    # The quadtree+SnapWave combined deck-build+solve was a Batch-only, GPL-
    # isolated worker job; with the AWS Batch arm removed (local-only slim) the
    # regular-grid in-agent build + local-docker solve is the only path. The
    # ``quadtree_run_result`` / ``build_solve_run_result`` sentinels stay wired
    # (always None here) so the downstream result-selection + context-layer
    # gates are unchanged.
    solve_model_setup_uri = model_setup.setup_uri
    quadtree_run_result: RunResult | None = None
    build_solve_run_result: RunResult | None = None

    # --- Step 6: run_solver (Invariant 9 confirmation seam owned by agent) ---
    # Auto vertical scaling per case (NATE 2026-06-17): size the Batch
    # compute_class from the AOI/mesh element count the adaptive-grid autoscale
    # already estimated (model_setup.parameters['autoscale']['estimated_active_
    # cells']) instead of always dispatching at the default "standard" (8 vCPU).
    # A big domain grabs more compute (up to the new xlarge 48-vCPU tier); a
    # small one stays cheap. When the estimate is unavailable we fall back to the
    # caller's compute_class (default "medium" == standard) — select_compute_class
    # never raises, so a missing/zero estimate can never crash the dispatch.
    # The combined quadtree job ALREADY solved (Step 5.5 returned its solve
    # RunResult); skip the second run_solver + wait_for_completion entirely and
    # carry that result straight into telemetry + postprocess. The non-quadtree
    # (regular-grid) path is UNCHANGED.
    handle: ExecutionHandle | None = None
    if quadtree_run_result is not None:
        run_result: RunResult = quadtree_run_result
    elif build_solve_run_result is not None:
        # HEAVY-COMPUTE OFFLOAD: the combined build+solve job (Step 5.6) already
        # built + solved + postprocessed; carry its RunResult straight into the
        # telemetry + register-only manifest tail (no second run_solver).
        run_result = build_solve_run_result
    else:
        _autoscale_for_sizing = _extract_solve_autoscale(model_setup)
        _estimated_elements = _autoscale_for_sizing.get("estimated_active_cells")
        if _estimated_elements:
            effective_compute_class = select_compute_class(_estimated_elements)
            logger.info(
                "model_flood_scenario: auto vertical scaling "
                "estimated_active_cells=%s → compute_class=%s (caller requested %s)",
                _estimated_elements,
                effective_compute_class,
                compute_class,
            )
        else:
            effective_compute_class = compute_class
            logger.info(
                "model_flood_scenario: no element estimate available; using caller "
                "compute_class=%s for the solve dispatch",
                compute_class,
            )
        try:
            # task-168: surface the solver DISPATCH (the Batch submit) as a nested
            # child row. This is a fast submit, so the child lands green quickly;
            # the LIVE Batch readout (status ticks + terminal) stays owned by the
            # two-card Sim card (mint_dispatch_and_sim_cards) below - the substep
            # does NOT touch that machinery (HARD INVARIANT). A dispatch failure
            # raises INSIDE the substep -> the child reads red (honesty floor) and
            # re-raises to the existing except handler, which returns the
            # SOLVER_DISPATCH_FAILED failed envelope unchanged. No-op when no
            # emitter is bound.
            async with substep(emitter, "run_solver"):
                # NO-SYNC-BLOCKING-ON-LOOP (NATE 2026-06-29): ``run_solver`` does a
                # SYNCHRONOUS boto3 Batch ``submit_job`` (TLS + AWS API I/O, with
                # botocore retry/backoff that can stall for many seconds under
                # throttling / a slow control plane). It was the LAST un-offloaded
                # sync call on the flood hot path -- every other heavy step (the
                # fetcher chain, build_sfincs_model, postprocess_flood, publish_layer)
                # already runs via ``asyncio.to_thread``. Offload the submit too so a
                # slow/throttled Batch API call can never stall the 12 s WS
                # heartbeat. ``run_solver`` is EMIT-FREE (it returns an
                # ``ExecutionHandle``; this workflow does all the emitting), so a
                # worker thread is safe -- it mirrors the awaited async
                # ``run_sfincs_quadtree`` on the coastal (quadtree) path.
                handle = await asyncio.to_thread(
                    run_solver,
                    solver="sfincs",
                    # The regular-grid model_setup.setup_uri (the quadtree path no
                    # longer reaches here -- it solved inside the combined job).
                    model_setup_uri=solve_model_setup_uri,
                    compute_class=effective_compute_class,
                )
                solver_run_ids.append(handle.run_id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("run_solver dispatch failed: %s", exc)
            return _build_failed_envelope(
                bbox=resolved_bbox,
                project_id=proj_id,
                session_id=sess_id,
                error_code=getattr(exc, "error_code", "SOLVER_DISPATCH_FAILED"),
                error_detail=str(exc),
                workflow_name=workflow_name,
                data_sources=data_sources,
                forcing=forcing_summary,
                solver_run_ids=solver_run_ids,
                return_period_years=return_period_yr,
                duration_hours=float(duration_hr),
                grid_resolution_m=grid_resolution_m,
            )

        # --- Two-card sim observability (task-149) ----------------------------
        # Mint the Dispatch (tool, lands complete) + Sim (compute, bound to the
        # Batch jobId) cards and point the solver emitter binding at the SIM step
        # so wait_for_completion's poller feeds its live batch_status. The
        # ephemeral SFINCS Batch worker has NO inbound WS; status flows agent-side
        # over the EXISTING WS via the poller. Best-effort: emitter None / emit
        # failure -> no cards, solve proceeds unchanged.
        from ..tools.solver import EmitterBinding, set_emitter_binding

        _sim_step_id = await mint_dispatch_and_sim_cards(
            emitter=emitter,
            solver=getattr(handle, "solver", "sfincs") or "sfincs",
            handle=handle,
            compute_class=effective_compute_class,
        )
        if emitter is not None and _sim_step_id is not None:
            set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))

        # --- Step 7: wait_for_completion (Invariant 8 cancel chain propagates) ---
        # LIVE big-sim telemetry (NATE 2026-06-17): drive a solve-progress envelope
        # on the running card every few seconds for the duration of the solve so
        # the user sees grid/cells/vCPU/elapsed/ETA tick rather than a silent
        # spinner. The ETA comes from the perf model (autoscale
        # estimated_solve_seconds) when available, else None (no fabricated ETA).
        # The driver is a side task that we cancel as soon as the solve
        # returns/raises — it never affects the outcome.
        _autoscale = _extract_solve_autoscale(model_setup)
        _live_active_cells = _autoscale.get("estimated_active_cells")
        _live_vcpus = _autoscale.get("vcpus")
        _live_eta = _autoscale.get("estimated_solve_seconds")
        # Deployment-aware CPU count (fingerprint audit A6): local-docker
        # reports the HOST cpu count (never the perf model's cloud vCPU
        # anchor); aws-batch keeps the autoscale-provenance value
        # byte-identical.
        from ..tools.solver import solve_progress_vcpus

        _progress_task = asyncio.ensure_future(
            _drive_live_solve_progress(
                emitter=emitter,
                run_id=handle.run_id,
                solver=getattr(handle, "solver", "sfincs") or "sfincs",
                grid_resolution_m=grid_resolution_m,
                active_cell_count=(
                    int(_live_active_cells)
                    if _live_active_cells is not None
                    else None
                ),
                vcpus=solve_progress_vcpus(
                    cloud_vcpus=(
                        int(_live_vcpus) if _live_vcpus is not None else None
                    )
                ),
                eta_seconds=float(_live_eta) if _live_eta is not None else None,
            )
        )
        try:
            run_result = await wait_for_completion(handle)
        except asyncio.CancelledError:
            # Invariant 8: the cancel chain is owned by wait_for_completion;
            # propagate immediately so the WS handler emits
            # pipeline-state(cancelled). Route the cancel to the SIM card
            # (best-effort terminal send, J-B-i).
            logger.info("model_flood_scenario cancelled while awaiting solver")
            await route_sim_terminal(emitter, _sim_step_id, run_result=None)
            raise
        finally:
            # Tear down the live-progress driver (success, failure, OR cancel)
            # + clear the compute-card emitter binding.
            _progress_task.cancel()
            try:
                await _progress_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            set_emitter_binding(None)

        # task-149: route the SIM compute card to its terminal state from the
        # RunResult (complete -> green, non-complete -> red) before the
        # solve-time telemetry + non-complete guard below.
        await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    # --- Solve-time telemetry (sprint-16 SFINCS per-job autoscale) ---
    # Accumulate real (active_cells, vCPU, wall_clock) data so the adaptive-grid
    # cell cap can be re-tuned from logged measurements. Emitted on the CURRENT
    # path (every solve), for BOTH success and failure/timeout — a censored
    # timeout is itself a data point about a too-big AOI. Best-effort; never
    # breaks the solve loop.
    try:
        _emit_flood_solve_telemetry(
            run_result=run_result,
            handle=handle,
            model_setup=model_setup,
            bbox=resolved_bbox,
            grid_resolution_m=grid_resolution_m,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the solve
        logger.warning("solve telemetry emission failed (non-fatal): %s", exc)

    # --- SOLVE telemetry (task-153): Batch instance + size + timing breakdown ---
    # Record ONE solve row merging run_result.batch_compute_meta (Spot instance +
    # queue/compute/total timing the wait-loop captured) with the mesh size
    # descriptor (active_cell_count + resolution_m) so a perf model can later infer
    # completion time. ONLY the regular-grid path (handle is not None) records this
    # — the quadtree submit+wait path is left uninstrumented (consistent with the
    # two-card work). Best-effort; a telemetry failure never affects the solve.
    if handle is not None:
        try:
            _record_flood_batch_solve_telemetry(
                run_result=run_result,
                handle=handle,
                model_setup=model_setup,
                grid_resolution_m=grid_resolution_m,
                session_id=sess_id,
                case_id=None,
            )
        except Exception as exc:  # noqa: BLE001 — telemetry must never break the solve
            logger.warning(
                "solve batch-compute telemetry failed (non-fatal): %s", exc
            )

    if run_result.status != "complete":
        # SOLVER_FAILED, SOLVER_TIMEOUT, cancelled — surface as failed envelope.
        return _build_failed_envelope(
            bbox=resolved_bbox,
            project_id=proj_id,
            session_id=sess_id,
            error_code=run_result.error_code or run_result.status.upper(),
            error_detail=run_result.error_message or run_result.cancellation_reason or "",
            workflow_name=workflow_name,
            data_sources=data_sources,
            forcing=forcing_summary,
            solver_run_ids=solver_run_ids,
            return_period_years=return_period_yr,
            duration_hours=float(duration_hr),
            grid_resolution_m=grid_resolution_m,
        )

    # --- Step 7.5: emit the cht_sfincs quadtree mesh as a context layer ----
    # NATE task #160 (coastal North Star): when the COMBINED quadtree job ran,
    # the cht_sfincs worker authored the VARIABLE-SIZE quadtree mesh and wrote an
    # ALREADY-EPSG:4326 ``mesh.geojson`` next to its outputs. We construct a THIN
    # LayerURI over it (no geometry build / no reproject / no file write - the
    # worker did all of that; the regular-grid SFINCS path has NO quadtree mesh,
    # so the emit is GATED on ``quadtree_run_result is not None``) and let the
    # emitter inline the s3:// .geojson via _read_vector_uri_as_geojson (the
    # boto3 GET runs inside add_loaded_layer, already offloaded off the loop).
    # It is a DEFAULT-VISIBLE CONTEXT backdrop (role="context", bbox=None so it
    # does not emit a competing zoom-to that would fight the AOI camera).
    # BEST-EFFORT: a mesh-emit failure must NEVER break the solve (log + go on).
    if quadtree_run_result is not None:
        mesh_uri = (
            run_result.output_uri or _default_runs_prefix(run_result.run_id)
        ).rstrip("/") + "/mesh.geojson"
        try:
            mesh_layer = make_sfincs_mesh_layer_uri(
                mesh_uri, run_id=run_result.run_id
            )
            if mesh_layer is not None and emitter is not None:
                safe = emit_layer_uri(mesh_layer)
                if safe is not None:
                    await emitter.add_loaded_layer(safe)
        except Exception as exc:  # noqa: BLE001 — mesh emit is non-fatal
            logger.warning(
                "model_flood_scenario: sfincs mesh emit failed (non-fatal): %s",
                exc,
            )

    # --- Postprocess-offload branch (SFINCS Phase 4): worker-written manifest ---
    # When the Batch worker rebuilt with the raster-postprocess offload, it ran
    # the heavy NetCDF -> COG conversion ITSELF (display-ready overview-bearing
    # COGs at deterministic keys) and wrote a thin typed publish_manifest.json
    # (pointed to by completion.json.publish_manifest_uri). ``read_publish_manifest``
    # reads + SCHEMA-GATEs it; a present, schema_version==1 manifest activates the
    # REGISTER-ONLY path below - SHORT-CIRCUITing the on-box heavy tail entirely
    # (NO _resolve_run_output_to_local, NO postprocess_flood/_waves, NO
    # _ensure_raster_has_overviews - has_overviews is true). The agent-side
    # publish-or-honest-drop gate (GRACE2_TILE_SERVER_BASE) is preserved per layer.
    #
    # ONE-RELEASE SAFETY: manifest absent OR unknown schema_version ->
    # ``read_publish_manifest`` returns None and we run the EXISTING on-box path
    # below unchanged (the raw sfincs_map.nc is still uploaded). Clean if/else.
    published_layers: list[LayerURI] = []
    depth_metrics: dict[str, Any] = {}
    manifest = await asyncio.to_thread(read_publish_manifest, run_result)
    register_only = manifest is not None
    if register_only:
        logger.info(
            "model_flood_scenario: REGISTER-ONLY path (worker postprocess "
            "offload) run_id=%s engine=%s layers=%d",
            run_result.run_id, manifest.engine, len(manifest.layers),
        )
        async with substep(emitter, "publish_layer"):
            reg = register_manifest_layers(
                manifest, run_id=run_result.run_id, bbox=resolved_bbox
            )
        depth_metrics = reg.metrics
        # The merged manifest carries depth + wave layers. Primary layers (peak
        # depth + peak wave) ride into the success envelope's ResultLayer set;
        # context layers (the "... step N" frames) emit OUT-OF-BAND so the web
        # scrubber groups form, exactly as the on-box path does.
        published_layers = [lyr for lyr in reg.layers if lyr.role == "primary"]
        manifest_frames = [lyr for lyr in reg.layers if lyr.role != "primary"]
        if manifest_frames and emitter is not None:
            emitted = 0
            for lyr in manifest_frames:
                try:
                    await emitter.add_loaded_layer(lyr)
                    emitted += 1
                except Exception as exc:  # noqa: BLE001 — never break the solve
                    logger.warning(
                        "model_flood_scenario: manifest frame emit failed for "
                        "%s: %s", lyr.layer_id, exc,
                    )
            if emitted:
                logger.info(
                    "model_flood_scenario: emitted %d/%d manifest animation "
                    "frames as sequential group(s) (run_id=%s)",
                    emitted, len(manifest_frames), run_result.run_id,
                )
        elif manifest_frames:
            logger.info(
                "model_flood_scenario: %d manifest animation frames available "
                "but no emitter bound - frames not emitted.",
                len(manifest_frames),
            )

    # --- Step 8: postprocess_flood (ON-BOX FALLBACK) ---
    # audit #1: ``postprocess_flood`` downloads the full ``sfincs_map.nc`` via
    # SYNC boto3 and writes N COGs to object storage — tens of seconds to
    # minutes of blocking I/O right after the solve. Run it off the loop so it
    # cannot stall the WS keepalive. ``postprocess_flood`` is EMIT-FREE (no
    # current_emitter()/emit_*/add_loaded_layer): it produces the LayerURIs +
    # metrics then returns, and THIS workflow does all the emitting (the
    # publish + add_loaded_layer steps below run back on the loop), so it is
    # safe to move to a worker thread. SKIPPED on the register-only path.
    layers: list[LayerURI] = []
    if not register_only:
        try:
            # task-168: surface the depth postprocess as a nested child row. A
            # PostprocessError raises INSIDE the substep -> the child reads red
            # (honesty floor) and re-raises to the except handler, which returns
            # the failed envelope unchanged. No-op when no emitter is bound.
            async with substep(emitter, "postprocess_flood"):
                layers, depth_metrics = await asyncio.to_thread(
                    postprocess_flood,
                    run_result.output_uri
                    or _default_runs_prefix(run_result.run_id),
                    run_id=run_result.run_id,
                )
        except PostprocessError as exc:
            logger.warning(
                "postprocess_flood failed: %s (%s)", exc.error_code, exc
            )
            return _build_failed_envelope(
                bbox=resolved_bbox,
                project_id=proj_id,
                session_id=sess_id,
                error_code=exc.error_code,
                error_detail=str(exc),
                workflow_name=workflow_name,
                data_sources=data_sources,
                forcing=forcing_summary,
                solver_run_ids=solver_run_ids,
                return_period_years=return_period_yr,
                duration_hours=float(duration_hr),
                grid_resolution_m=grid_resolution_m,
            )

    # On-box publish (Steps 9/9b/9c) - SKIPPED on the register-only path
    # (the worker already produced display-ready COGs; the manifest branch
    # above did the registration + frame/wave emission).
    if not register_only:
        # --- Step 9: publish_layer (COG → QGIS Server WMS bridge, job-0062) ---
        # For the primary flood-depth layer, invoke the PyQGIS worker to add the COG
        # to the canonical .qgs project so QGIS Server can serve it as WMS.
        # The returned WMS URL replaces the gs:// uri in the LayerURI/ResultLayer so
        # the client gets a renderable URL directly (layer-emission-contract.md, 2026-06-07).
        #
        # Non-fatal: if publish_layer fails (e.g. OQ-62-WORKER-SA-RUNS-BUCKET-GRANT
        # is not yet landed), we DROP the primary raster layer from the emitted set
        # rather than fall back to the raw gs:// uri (job-0254 §1, Decision 11). A
        # gs:// uri never renders — MapLibre cannot fetch it; emitting it only paints
        # a dead, broken layer row in the LayerPanel. Dropping it keeps the map
        # honest while the rest of the envelope (metrics, provenance, narration)
        # stays intact, so the LLM narrates the publish failure truthfully and the
        # job-0177 retry-on-failure loop can act. The layer_uri_emit seam enforces
        # this same rule at the emission boundary as a belt-and-suspenders invariant.
        # postprocess_flood returns [peak_primary] + [frame_0..frame_k]. The PEAK
        # layer (role="primary") is the ONE returned by the wrapper + the
        # published/on_map summary source + the habitat/Pelicun hazard raster — it
        # takes the existing publish-or-honest-drop path UNCHANGED. The FRAME layers
        # (role="context", names "Flood depth step N") are the time-stepped animation
        # (flood North Star Phase 1): each is published + emitted OUT-OF-BAND via the
        # emitter so the web SequenceScrubber group forms, WITHOUT changing the tool's
        # single-LayerURI return shape (no re-publish trip in summarize_tool_result).
        primary_layers = [lyr for lyr in layers if lyr.role == "primary"]
        frame_layers = [lyr for lyr in layers if lyr.role != "primary"]

        published_layers: list[LayerURI] = []
        for lyr in primary_layers:
            # job-0291: s3:// COGs (AWS local-docker backend) take the same
            # publish-or-honest-drop gate as gs:// — a raw object-store URI never
            # renders in MapLibre (job-0254 §1), so it must never reach the map.
            # On AWS publish_layer fails until job-0290 lands QGIS-on-AWS; the
            # layer is dropped and the metrics/narration stay honest.
            if (
                lyr.role == "primary"
                and lyr.layer_type == "raster"
                and (lyr.uri.startswith("gs://") or lyr.uri.startswith("s3://"))
            ):
                layer_id_for_wms = f"flood-depth-peak-{run_result.run_id}"
                try:
                    # audit #1: ``publish_layer`` runs a ``time.sleep`` poll loop
                    # (worker job poll) that blocks the loop for tens of seconds.
                    # Run it off the loop so it cannot stall the WS keepalive.
                    # ``publish_layer`` is EMIT-FREE (no current_emitter()/emit_*/
                    # add_loaded_layer): it returns the WMS URL; this workflow does
                    # the emitting (the LayerURI it builds reaches the map via the
                    # wrapper return / out-of-band add_loaded_layer back on the
                    # loop), so it is safe to move to a worker thread.
                    # task-168: surface the peak-layer publish as a nested child row.
                    # A PublishLayerError raises INSIDE the substep -> the child reads
                    # red (honesty floor) and re-raises to the existing except handler
                    # below, which DROPS the layer (publish-or-honest-drop, job-0254
                    # §1) unchanged. No-op when no emitter is bound.
                    async with substep(emitter, "publish_layer"):
                        wms_url = await asyncio.to_thread(
                            publish_layer,
                            layer_uri=lyr.uri,
                            layer_id=layer_id_for_wms,
                            style_preset=lyr.style_preset or "continuous_flood_depth",
                        )
                    # Substitute the WMS URL into the LayerURI so the client renders
                    # directly (OQ-62-LAYERURI-URI-FIELD: LayerURI.uri is documented
                    # as gs:// but has no validator rejecting WMS URLs; we use it here
                    # as the renderable URL per the kickoff direction. A follow-up
                    # schema job should add a dedicated wms_url field.)
                    published_layers.append(
                        LayerURI(
                            layer_id=layer_id_for_wms,
                            name=lyr.name,
                            layer_type=lyr.layer_type,
                            uri=wms_url,
                            # job (flood-duplicate-layer fix): the published layer
                            # is the ONE styled (white->blue->green) peak-depth
                            # layer the user sees. Carry the canonical preset
                            # unconditionally — never emit a styleless flood-depth
                            # raster (a styleless COG falls through to TiTiler's
                            # default matplotlib viridis, the redundant unstyled
                            # duplicate this workflow must never produce).
                            style_preset=lyr.style_preset or FLOOD_DEPTH_STYLE_PRESET,
                            temporal=lyr.temporal,
                            role=lyr.role,
                            units=lyr.units,
                            bbox=resolved_bbox,
                        )
                    )
                    logger.info(
                        "publish_layer succeeded layer_id=%s wms_url=%s",
                        layer_id_for_wms,
                        wms_url,
                    )
                except PublishLayerError as exc:
                    logger.warning(
                        "publish_layer failed for layer_id=%s error_code=%s (%s) — "
                        "DROPPING the primary flood-depth layer from the emitted set "
                        "(job-0254 §1): a raw gs:// uri never renders in MapLibre, so "
                        "we do NOT fall back to it. The envelope's metrics/provenance "
                        "remain intact and the failure is narrated honestly; the "
                        "retry-on-failure loop (job-0177) can re-attempt publish.",
                        layer_id_for_wms,
                        exc.error_code,
                        exc,
                    )
                    # Intentionally do NOT append `lyr` — the gs:// uri stays off the
                    # map. (OQ-62-WORKER-SA-RUNS-BUCKET-GRANT resolution restores the
                    # success path; until then the depth metrics still surface.)
            else:
                published_layers.append(lyr)

        # --- Step 9b: publish + emit the time-step animation frames (Phase 1) ---
        # Each frame is a DISTINCT COG (distinct runs-bucket key → distinct TiTiler
        # url= → distinct pipeline_emitter._layer_identity_key → no dedup collapse).
        # We publish in ASCENDING step order and call emitter.add_loaded_layer for
        # each so all N frames arrive as one contiguous sequential group; the final
        # session-state snapshot carries peak + N frames. Frames are emitted ONLY
        # through the emitter (NOT added to published_layers / result_layers / the
        # wrapper return), so they never reach summarize_tool_result and can't trip a
        # re-publish, and the habitat/Pelicun consumers still see layers[0] = peak.
        # When current_emitter() is None (direct call / smoke / unit test) frame
        # emission is skipped — the frames still live in the returned `layers` from
        # postprocess_flood for tests to assert on.
        if frame_layers and emitter is not None:
            published_frame_count = 0
            for lyr in frame_layers:
                if not (lyr.uri.startswith("gs://") or lyr.uri.startswith("s3://")):
                    # Already a renderable URL (defensive) — emit as-is.
                    try:
                        await emitter.add_loaded_layer(lyr)
                        published_frame_count += 1
                    except Exception as exc:  # noqa: BLE001 — never break the solve
                        logger.warning("frame emit failed for %s: %s", lyr.layer_id, exc)
                    continue
                try:
                    # audit #1: same as the peak ``publish_layer`` above —
                    # ``time.sleep`` poll loop blocks the loop for tens of seconds
                    # per frame. Run it off the loop so it cannot stall the WS
                    # keepalive. EMIT-FREE: it returns the WMS URL; the
                    # ``add_loaded_layer`` emit for this frame runs back on the loop
                    # just below, so moving the publish to a worker thread is safe.
                    frame_wms_url = await asyncio.to_thread(
                        publish_layer,
                        layer_uri=lyr.uri,
                        layer_id=lyr.layer_id,
                        style_preset=lyr.style_preset or FLOOD_DEPTH_STYLE_PRESET,
                    )
                except PublishLayerError as exc:
                    # Honest drop: a frame that won't publish is dropped (its raw
                    # gs:// never renders). The remaining frames + the peak layer
                    # stay intact. If too many frames drop the group may fall below
                    # 2 members and simply not form — acceptable, never a fake row.
                    logger.warning(
                        "publish_layer failed for frame layer_id=%s error_code=%s "
                        "(%s) — dropping this frame from the animation group.",
                        lyr.layer_id, exc.error_code, exc,
                    )
                    continue
                frame_layer = LayerURI(
                    layer_id=lyr.layer_id,
                    name=lyr.name,  # "Flood depth step N" — the web grouping token
                    layer_type=lyr.layer_type,
                    uri=frame_wms_url,
                    style_preset=lyr.style_preset or FLOOD_DEPTH_STYLE_PRESET,
                    role=lyr.role,  # "context"
                    units=lyr.units,
                    bbox=resolved_bbox,
                )
                try:
                    await emitter.add_loaded_layer(frame_layer)
                    published_frame_count += 1
                except Exception as exc:  # noqa: BLE001 — never break the solve
                    logger.warning(
                        "frame add_loaded_layer failed for %s: %s", lyr.layer_id, exc
                    )
            if published_frame_count:
                logger.info(
                    "model_flood_scenario: emitted %d/%d animation frames as a "
                    "sequential group (run_id=%s)",
                    published_frame_count, len(frame_layers), run_result.run_id,
                )
        elif frame_layers:
            logger.info(
                "model_flood_scenario: %d animation frames available but no emitter "
                "bound (direct/smoke/test) — frames not emitted to the map.",
                len(frame_layers),
            )

        # --- Step 9c: postprocess + emit the SnapWave WAVE field (sprint-17) ---
        # GATED on a quadtree+SnapWave run: the time-resolved wave height field
        # (hm0 / hm0ig, dims (nmesh2d_face, time)) is written EVERY output step ONLY
        # on the quadtree+SnapWave solve, so the wave postprocess runs ONLY when the
        # combined quadtree job ran (``quadtree_run_result is not None`` — the
        # regular-grid SFINCS path has no wave field). This makes the SnapWave waves
        # visibly ANIMATE on the Mexico Beach (Hurricane Michael) North Star.
        #
        # DEGRADE-not-fail: the entire block is best-effort (mirrors the Step 7.5
        # mesh-emit pattern). A wave-postprocess failure (no SnapWave field, a COG
        # write error, a publish/emit error) MUST NEVER sink the depth layers OR the
        # envelope — it logs + degrades, leaving the flood-depth peak + frames intact.
        # The peak wave layer takes the SAME publish-or-honest-drop gate the depth
        # peak uses; the wave frames emit OUT-OF-BAND via the emitter (same as the
        # depth frames) so they form a SEPARATE web scrubber group ("Wave height
        # step N") without changing the tool's single-LayerURI return shape.
        if quadtree_run_result is not None:
            try:
                # task-168: surface the wave postprocess as a nested child row. A
                # PostprocessError (no SnapWave field / read / write failure) raises
                # INSIDE the substep -> the child reads red (honesty floor) and
                # re-raises to the existing except handlers below, which DEGRADE to the
                # depth layers (a non-SnapWave quadtree run is a legitimate state) with
                # the depth layers intact. No-op when no emitter is bound.
                async with substep(emitter, "postprocess_waves"):
                    wave_layers, _wave_metrics = await asyncio.to_thread(
                        postprocess_waves,
                        run_result.output_uri or _default_runs_prefix(run_result.run_id),
                        run_id=run_result.run_id,
                        bbox=resolved_bbox,
                    )
            except PostprocessError as exc:
                # No SnapWave field / read / write failure — degrade silently to the
                # depth layers (a non-SnapWave quadtree run is a legitimate state).
                logger.info(
                    "model_flood_scenario: wave postprocess degraded (%s: %s) — "
                    "depth layers intact, no wave animation.",
                    exc.error_code, exc,
                )
                wave_layers = []
            except Exception as exc:  # noqa: BLE001 — wave emit is non-fatal
                logger.warning(
                    "model_flood_scenario: wave postprocess raised unexpectedly "
                    "(non-fatal): %s — depth layers intact.",
                    exc,
                )
                wave_layers = []

            wave_peak = [lyr for lyr in wave_layers if lyr.role == "primary"]
            wave_frames = [lyr for lyr in wave_layers if lyr.role != "primary"]

            # Peak wave layer — publish-or-honest-drop gate (same as the depth peak).
            # The published peak wave LayerURI is appended to ``published_layers`` so
            # it rides into the success envelope's ResultLayer set alongside the
            # depth peak; a publish failure DROPS it (a raw s3:// never renders).
            for lyr in wave_peak:
                if lyr.uri.startswith("gs://") or lyr.uri.startswith("s3://"):
                    try:
                        wave_wms_url = await asyncio.to_thread(
                            publish_layer,
                            layer_uri=lyr.uri,
                            layer_id=lyr.layer_id,
                            style_preset=lyr.style_preset or WAVE_HEIGHT_STYLE_PRESET,
                        )
                    except PublishLayerError as exc:
                        logger.warning(
                            "model_flood_scenario: publish_layer failed for peak "
                            "wave layer_id=%s error_code=%s (%s) — dropping it "
                            "(depth layers intact).",
                            lyr.layer_id, exc.error_code, exc,
                        )
                        continue
                    except Exception as exc:  # noqa: BLE001 — never break the solve
                        logger.warning(
                            "model_flood_scenario: peak wave publish raised "
                            "unexpectedly (non-fatal): %s", exc,
                        )
                        continue
                    published_layers.append(
                        LayerURI(
                            layer_id=lyr.layer_id,
                            name=lyr.name,
                            layer_type=lyr.layer_type,
                            uri=wave_wms_url,
                            style_preset=lyr.style_preset or WAVE_HEIGHT_STYLE_PRESET,
                            role=lyr.role,
                            units=lyr.units,
                            bbox=resolved_bbox,
                        )
                    )
                    logger.info(
                        "model_flood_scenario: published peak wave layer_id=%s",
                        lyr.layer_id,
                    )
                else:
                    published_layers.append(lyr)

            # Wave frames — publish + emit OUT-OF-BAND (same as the depth frames) so
            # they form a SEPARATE "Wave height step N" scrubber group. Emitted only
            # through the emitter (NOT added to published_layers / result_layers), so
            # they never reach summarize_tool_result. Skipped when no emitter bound.
            if wave_frames and emitter is not None:
                published_wave_frames = 0
                for lyr in wave_frames:
                    if not (lyr.uri.startswith("gs://") or lyr.uri.startswith("s3://")):
                        try:
                            await emitter.add_loaded_layer(lyr)
                            published_wave_frames += 1
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "wave frame emit failed for %s: %s", lyr.layer_id, exc
                            )
                        continue
                    try:
                        wf_wms_url = await asyncio.to_thread(
                            publish_layer,
                            layer_uri=lyr.uri,
                            layer_id=lyr.layer_id,
                            style_preset=lyr.style_preset or WAVE_HEIGHT_STYLE_PRESET,
                        )
                    except PublishLayerError as exc:
                        logger.warning(
                            "publish_layer failed for wave frame layer_id=%s "
                            "error_code=%s (%s) — dropping this wave frame.",
                            lyr.layer_id, exc.error_code, exc,
                        )
                        continue
                    except Exception as exc:  # noqa: BLE001 — never break the solve
                        logger.warning(
                            "wave frame publish raised unexpectedly (non-fatal): %s",
                            exc,
                        )
                        continue
                    wave_frame_layer = LayerURI(
                        layer_id=lyr.layer_id,
                        name=lyr.name,  # "Wave height step N" — the web grouping token
                        layer_type=lyr.layer_type,
                        uri=wf_wms_url,
                        style_preset=lyr.style_preset or WAVE_HEIGHT_STYLE_PRESET,
                        role=lyr.role,  # "context"
                        units=lyr.units,
                        bbox=resolved_bbox,
                    )
                    try:
                        await emitter.add_loaded_layer(wave_frame_layer)
                        published_wave_frames += 1
                    except Exception as exc:  # noqa: BLE001 — never break the solve
                        logger.warning(
                            "wave frame add_loaded_layer failed for %s: %s",
                            lyr.layer_id, exc,
                        )
                if published_wave_frames:
                    logger.info(
                        "model_flood_scenario: emitted %d/%d wave-animation frames "
                        "as a separate sequential group (run_id=%s)",
                        published_wave_frames, len(wave_frames), run_result.run_id,
                    )

    # --- Step 10: build success envelope ---
    bbox_area_km2 = _bbox_area_km2(resolved_bbox)
    result_layers: list[ResultLayer] = [
        ResultLayer(
            layer_id=lyr.layer_id,
            name=lyr.name,
            layer_type=lyr.layer_type,
            uri=lyr.uri,
            style_preset=lyr.style_preset,
            temporal=lyr.temporal,
            role=lyr.role,
            units=lyr.units,
        )
        for lyr in published_layers
    ]
    metrics = FloodMetrics(
        flooded_area_km2=min(
            bbox_area_km2,
            float(depth_metrics.get("flooded_cell_count", 0))
            * (grid_resolution_m * grid_resolution_m / 1_000_000.0),
        ),
        max_depth_m=float(depth_metrics.get("max_depth_m", 0.0)),
        mean_depth_m=float(depth_metrics.get("mean_depth_m", 0.0)),
        p95_depth_m=float(depth_metrics.get("p95_depth_m", 0.0)),
        solver_version="sfincs-v2.3.3",
        grid_resolution_m=grid_resolution_m,
        simulation_duration_hours=int(duration_hr),
    )
    envelope = AssessmentEnvelope(
        envelope_id=new_ulid(),
        project_id=proj_id,
        session_id=sess_id,
        envelope_type="modeled",
        hazard_type="flood",
        workflow_name=workflow_name,
        bbox=resolved_bbox,
        crs="EPSG:4326",
        forcing=forcing_summary,
        layers=result_layers,
        provenance=Provenance(data_sources=data_sources),
        created_at=now,
        completed_at=datetime.now(timezone.utc),
        solver_run_ids=solver_run_ids,
        flood=FloodPayload(metrics=metrics),
    )
    logger.info(
        "model_flood_scenario complete envelope_id=%s run_ids=%s layers=%d",
        envelope.envelope_id,
        solver_run_ids,
        len(result_layers),
    )
    return envelope


# --------------------------------------------------------------------------- #
# LLM-exposed thin atomic-tool wrapper (workflow_dispatch source class)
# --------------------------------------------------------------------------- #


_RUN_MODEL_FLOOD_SCENARIO_METADATA = AtomicToolMetadata(
    name="run_model_flood_scenario",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(_RUN_MODEL_FLOOD_SCENARIO_METADATA)
async def run_model_flood_scenario(
    bbox: tuple[float, float, float, float] | None = None,
    location_query: str | None = None,
    event_id: str | None = None,
    return_period_yr: int = 100,
    duration_hr: int = 24,
    compute_class: str = "medium",
    forcing_raster_uri: str | None = None,
    surge_forcing: dict[str, Any] | None = None,
    enable_subgrid: bool = False,
    coastal: bool = False,
    quadtree: bool = False,
    building_obstacles: bool | str = False,
    building_obstacle_mode: str = "exclude",
    output_interval_min: float | None = None,
    # NATE 2026-06-26: SFINCS scenario-coverage intents (fluvial / compound /
    # wind / infiltration / levee-breach / tsunami). All default to today's
    # behaviour so a pluvial run is byte-identical.
    river: bool = False,
    compound: bool = False,
    wind: dict[str, Any] | None = None,
    advanced_physics: dict[str, Any] | None = None,
    infiltration: bool | str = False,
    breach_point: tuple[float, float] | None = None,
    breach_peak_discharge_m3s: float | None = None,
    breach_arrival_hr: float | None = None,
    tsunami: bool = False,
    tsunami_wave_height_m: float | None = None,
    tsunami_period_min: float | None = None,
    # SPIDERWEB (2026-07-19): parametric hurricane wind+pressure via a Delft3D
    # .spw. Any of these implies coastal + the spiderweb wind path; mutually
    # exclusive with ``wind`` (typed STORM_WIND_CONFLICT, never silent).
    storm_name: str | None = None,
    storm_season: int | None = None,
    storm_track_uri: str | None = None,
    project_id: str | None = None,
    session_id: str | None = None,
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> LayerURI | dict[str, Any]:
    """Run the full deterministic SFINCS flood-modeling workflow end-to-end.

    Nine-step composition chain (all deterministic Python, zero LLM calls):
    1. ``geocode_location(location_query)`` — optional; derives bbox from
       a free-text place name when ``bbox`` is not provided.
    2. ``fetch_dem(bbox)`` — downloads USGS 3DEP or CoastalDEM to a COG.
    3. ``fetch_landcover(bbox)`` — downloads NLCD landcover for Manning's
       roughness parameterization.
    4. ``fetch_river_geometry(bbox)`` — downloads NHD river geometry for
       channel routing.
    5. ``lookup_precip_return_period(bbox, return_period_years, duration_hours)``
       — looks up NOAA Atlas 14 design-storm precipitation depth.
    6. ``build_sfincs_model(dem_uri, landcover_uri, river_uri, forcing, bbox)``
       — assembles the HydroMT-SFINCS deck in GCS with NLCD validation gate.
    7. ``run_solver(model_setup)`` — submits the SFINCS Cloud Run Job.
    8. ``wait_for_completion(run_id)`` — polls until SUCCEEDED or FAILED;
       emits progress events per FR-WC-12.
    9. ``postprocess_flood(run_outputs_uri)`` → ``publish_layer(flood_depth_cog)``
       — extracts peak depth COG, uploads to the runs bucket, and publishes
       to QGIS Server WMS.

    When to use:
        - User asks to model a flood scenario, simulate flood inundation,
          compute peak flood depth, run a flood simulation, or estimate flood
          extent for a named location.
        - Any request mentioning "return period", "design storm", "ARI",
          "flood risk", "inundation depth", or "flood extent" for a named
          location or bounding box.

    When NOT to use:
        - Custom solver dispatch (use ``run_solver`` + ``wait_for_completion``
          directly).
        - Non-flood hazards (separate workflow milestones).
        - Cancelling a running flood scenario (use the WS ``cancel`` envelope;
          cancellation propagates through ``wait_for_completion``).

    Examples:
        - "model the flood from a 100-year storm in Fort Myers, FL"
          → location_query: Fort Myers, FL ; return_period_years: 100
        - "peak flood depth from a 25-year design storm in Houston"
          → location_query: Houston ; return_period_years: 25
        - "simulate flood inundation for Hurricane Ian near Fort Myers"
          → location_query: Fort Myers ; return_period_years: 100 (default)
        - "500-year flood for New Orleans, 48-hour duration"
          → location_query: New Orleans ; return_period_years: 500 ; duration_hours: 48

    Params:
        bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. When
            ``None``, ``location_query`` is used to geocode. Direct bbox
            wins when both are supplied.
        location_query: free-text place name (geocoded via Nominatim).
        event_id: optional event ID for HEP-side provenance (v0.1: carried
            on the envelope's provenance hook; HEP integration M5.5+).
        return_period_years: design-storm ARI in years. Atlas 14 publishes
            {1, 2, 5, 10, 25, 50, 100, 200, 500, 1000}. Default 100.
            (Alias ``return_period_yr`` is accepted for backward compat.)
        duration_hours: design-storm duration in hours. Atlas 14 publishes
            durations 5-min through 60-day. Default 24.
            (Alias ``duration_hr`` is accepted for backward compat.)
        compute_class: FR-CE-3 compute class. Default ``"medium"``.
        forcing_raster_uri: optional ``gs://...`` URI of an OBSERVED
            accumulated-precipitation raster (e.g. an MRMS QPE COG from
            ``fetch_mrms_qpe``). When provided, the workflow forces SFINCS
            with the AREA-MEAN of this raster over the model domain (converted
            to a uniform rain rate) INSTEAD of the Atlas 14 design storm — this
            is the Case 3 real-data forcing path. ``duration_hours`` is reused
            as the accumulation window. Leave unset (``None``) for the standard
            return-period design-storm scenario.
        surge_forcing: optional COMPOUND-FLOOD forcing spec — a nested dict that
            wires the coastal water-level (surge / tide) boundary AND/OR the
            fluvial river-discharge boundary (plus optional wind / pressure)
            into the SFINCS deck, so the run combines coastal surge + river
            discharge + the pluvial design storm. Shape (each sub-dict optional;
            mirror the internal ``model_flood_scenario`` contract exactly):
            ``{"waterlevel": {"timeseries_uri": ..., "locations_uri": ...,
            "offset": ..., "buffer_m": ...} | {"geodataset_uri": ...},
            "discharge": {"timeseries_uri": ..., "rivers_uri": ...,
            "river_upa_km2": ...}, "wind": {"magnitude": ..., "direction": ...},
            "pressure": {"grid_uri": ..., "fill_value": ...}}``. The forcing-file
            URIs come from the forcing fetchers (``fetch_gtsm_tide_surge`` /
            ``fetch_noaa_coops_tides`` / ``fetch_noaa_nwm_streamflow`` /
            ``fetch_cama_flood_discharge`` / ERA5). Supplying a water-level
            boundary IMPLIES ``coastal=True`` (the surge needs a nearshore bed),
            so the DEM fetch auto-routes through ``fetch_topobathy``. ``None``
            (the default) → pure-pluvial deck, BYTE-IDENTICAL to today (no surge /
            discharge blocks emitted; regression-critical).
        enable_subgrid: emit a SFINCS ``setup_subgrid`` block so the solve runs on
            a coarse grid while resolving sub-cell topography + roughness (the
            cheap higher-fidelity urban-flood estimate). Auto-enabled when
            ``building_obstacles`` is set. Default ``False`` (no subgrid block;
            byte-identical to today).
        coastal: set ``True`` for a COASTAL flood / surge / run-up scenario near
            the ocean shoreline. This routes the terrain fetch through
            ``fetch_topobathy`` (a SEAMLESS land-plus-seafloor DEM merging USGS
            3DEP with NOAA NCEI CUDEM bathymetry) instead of the land-only
            ``fetch_dem`` — so the model has a real nearshore bed to route
            inundation over. Default ``False`` for an inland / pluvial flood
            (land-only DEM, unchanged). Auto-enabled when a surge water-level
            boundary is supplied. Use for prompts mentioning the coast, storm
            surge, hurricane inundation at the shoreline, tide, or "include the
            sea floor / bathymetry".
            ``coastal=True`` also auto-wires a sea water-level boundary + waves
            (no ``surge_forcing`` needed); set it only when the sea is involved.
        quadtree: set ``True`` for storm waves / wave run-up (implies
            ``coastal=True``; auto-enabled for any coastal run). Default ``False``.
        output_interval_min: optional animation frame spacing in minutes (coastal
            runs default fine; pluvial stays hourly). Leave unset unless asked.
        building_obstacles: OPTIONAL, default ``False`` (OFF). When truthy, the
            workflow burns building footprints into the SFINCS grid so the flood
            routes AROUND buildings — a more realistic (but slightly slower)
            urban-flood estimate. Three forms:
              * ``True`` → best-effort fetch of OSM building footprints (OSM
                Overpass) for the AOI; burned as no-flow ``exclude_mask`` cells.
                A footprint-fetch failure NEVER aborts the flood — it logs a
                warning and proceeds WITHOUT obstacles (honest degrade, same
                policy as river geometry).
              * a ``str`` → used verbatim as a footprint geofile URI (e.g. a
                prior ``fetch_buildings`` output FlatGeobuf / GeoJSON).
              * ``False`` → no obstacles (terrain-only; the default, unchanged
                plain DEM + Manning deck).
            ASK-WHEN-URBAN: for an URBAN / developed AOI (a named city core,
            downtown / midtown, a dense built-up bbox), if the user has NOT said
            whether to include buildings, ASK before running — e.g. "Model
            buildings as obstacles so water routes around them — more realistic
            but a bit slower — or just terrain?" — and set ``building_obstacles``
            from the answer. If the user PRE-specified ("include buildings" /
            "route around buildings" → ``True``; "terrain only" → ``False``),
            honor it without asking. RURAL / non-urban AOIs default to no
            buildings WITHOUT asking. Obstacles are OFF by default everywhere.
        building_obstacle_mode: how footprints are burned, default ``"exclude"``.
            ``"exclude"`` makes footprint cells INACTIVE no-flow holes on the
            plain regular grid (fast/rough, no subgrid). ``"raise"`` instead
            lifts the footprint bed elevation via the SFINCS subgrid so flow is
            impeded without disconnecting the domain (higher fidelity; auto-uses
            subgrid). Leave ``"exclude"`` unless higher fidelity is requested.
        river: set ``True`` for a FLUVIAL / river-flooding run -- auto-wires a
            river-discharge boundary (NOAA NWM -> USGS NWIS -> honest skip).
            Stays inland (``fetch_dem``). Default ``False``.
        compound: set ``True`` for a COMPOUND flood (coastal surge AND river
            discharge AND rain together; implies ``coastal`` + ``river``).
            Default ``False``.
        wind: optional WIND forcing ``{"magnitude": <m/s>, "direction":
            <deg-from>}`` or ``{"grid_uri": <nc>}`` (user/ERA5 supplied, never
            invented). Default ``None``.
        advanced_physics: optional SFINCS physics overrides (keys: advection,
            theta, alpha, huthresh, coriolis_latitude, wind_drag), validated +
            threaded into the deck. Default ``None`` (deck unchanged).
        infiltration: ``True`` -> auto-fetch GCN250 curve numbers; a ``str`` ->
            verbatim CN raster URI; ``False`` (default) -> no infiltration loss.
        breach_point: ``(lon, lat)`` of a drawn levee breach. USER-GATED: needs
            ``breach_peak_discharge_m3s`` or the run returns a typed input gate
            (never fabricated). Default ``None``.
        breach_peak_discharge_m3s: peak breach discharge (m^3/s, user-supplied).
            Default ``None``.
        breach_arrival_hr: optional breach time-to-peak (hr). Default ``None``.
        tsunami: ``True`` for a TSUNAMI run (implies ``coastal``). USER-GATED:
            needs ``tsunami_wave_height_m`` or the run returns a typed input gate.
            Default ``False``.
        tsunami_wave_height_m: tsunami peak wave height (m, user-supplied).
            Default ``None``.
        tsunami_period_min: tsunami period (min); defaults to ~15 min. ``None``.
        storm_name: NAMED historical hurricane / tropical cyclone (e.g.
            ``"Michael"``). With ``storm_season`` it resolves the IBTrACS best
            track via ``fetch_storm_tracks`` and builds a parametric Holland
            wind+pressure SPIDERWEB (.spw) that GENERATES the surge over a
            shelf-scale domain — the asymmetry uniform wind cannot produce
            (inundation concentrated RIGHT of the eye). Implies ``coastal``.
            MUTUALLY EXCLUSIVE with ``wind`` (typed STORM_WIND_CONFLICT).
            Example: "Simulate Hurricane Michael at landfall at Mexico Beach" ->
            ``storm_name="Michael", storm_season=2018,
            location_query="Mexico Beach, FL"``.
        storm_season: the storm's IBTrACS SEASON (calendar year, e.g. ``2018``).
            Names are reused across years, so pair it with ``storm_name``.
        storm_track_uri: a prior ``fetch_storm_tracks`` POINTS-FGB output used
            verbatim as the track (skips the fetch). Also implies coastal +
            spiderweb. Example: reuse a track already shown on the map.
        project_id / session_id: ULID identifiers from the WS session, forwarded
            for provenance / artifact namespacing. When ``None`` (default), the
            internal workflow mints fresh ULIDs (direct-call / smoke path).

    Returns:
        On success: the primary flood-depth COG as a ``LayerURI`` — the
        ``PipelineEmitter.emit_tool_call`` gate at
        ``pipeline_emitter.py:517`` fires ``add_loaded_layer`` when it sees
        a ``LayerURI`` return, which appends to ``session-state.loaded_layers``
        and emits a fresh ``session-state`` envelope (A.7 replace-not-reconcile).
        See ``docs/decisions/layer-emission-contract.md`` (ADOPTED 2026-06-07).

        On failure (partial-failure envelope with empty layers): the
        AssessmentEnvelope serialized as a dict so the LLM can narrate the
        error. The dict carries the Appendix B.4 Flood subtype shape with the
        error code threaded into ``flood.metrics.solver_version`` as
        ``"failed:<ERROR_CODE>"``.

    FR-DC-6: This wrapper declares ``cacheable=False`` +
    ``ttl_class="live-no-cache"`` + ``source_class="workflow_dispatch"`` (a new
    FR-DC-6 source class for the workflow exposure surface — same shape as
    job-0041's ``solver_dispatch``).

    Cross-tool dependencies:
        Upstream (consumes) — the 9-step fetch + solve chain above:
        - ``geocode_location`` (optional) → ``fetch_dem`` → ``fetch_landcover``
          → ``fetch_river_geometry`` → ``lookup_precip_return_period``
          → ``build_sfincs_model`` → ``run_solver`` → ``wait_for_completion``
          → ``postprocess_flood`` → ``publish_layer``
        Downstream (feeds):
        - ``run_model_flood_habitat_scenario`` — calls this sub-workflow as
          step 3 to generate the flood layer for Case 1 habitat analysis.
        - ``run_pelicun_damage_assessment`` / ``run_pelicun_with_buildings`` —
          consume the returned flood-depth COG ``LayerURI.uri`` as
          ``hazard_raster_uri`` for building-damage assessment.
        - ``compute_zonal_statistics`` — flood-depth COG as ``value_raster_uri``
          for population-in-flood-zone or habitat-impact metrics.
    """
    envelope = await model_flood_scenario(
        bbox=bbox,
        location_query=location_query,
        event_id=event_id,
        return_period_yr=return_period_yr,
        duration_hr=duration_hr,
        compute_class=compute_class,
        forcing_raster_uri=forcing_raster_uri,
        surge_forcing=surge_forcing,
        enable_subgrid=enable_subgrid,
        coastal=coastal,
        quadtree=quadtree,
        building_obstacles=building_obstacles,
        building_obstacle_mode=building_obstacle_mode,
        output_interval_min=output_interval_min,
        # NATE 2026-06-26: SFINCS scenario-coverage intents threaded through.
        river=river,
        compound=compound,
        wind=wind,
        advanced_physics=advanced_physics,
        infiltration=infiltration,
        breach_point=breach_point,
        breach_peak_discharge_m3s=breach_peak_discharge_m3s,
        breach_arrival_hr=breach_arrival_hr,
        tsunami=tsunami,
        tsunami_wave_height_m=tsunami_wave_height_m,
        tsunami_period_min=tsunami_period_min,
        # SPIDERWEB (2026-07-19): parametric hurricane wind+pressure.
        storm_name=storm_name,
        storm_season=storm_season,
        storm_track_uri=storm_track_uri,
        project_id=project_id,
        session_id=session_id,
    )
    # --- Layer-emission contract pin (docs/decisions/layer-emission-contract.md, 2026-06-07) ---
    # Return the primary flood-depth COG as a LayerURI so PipelineEmitter's
    # isinstance(result, LayerURI) gate at pipeline_emitter.py:517 fires
    # add_loaded_layer → session-state.loaded_layers (declarative, A.7
    # replace-not-reconcile).  On failure the envelope has no layers; fall
    # back to the dict so the LLM can narrate the error honestly.
    #
    # job-0160 bbox fix: include ``envelope.bbox`` on the returned LayerURI so
    # ``PipelineEmitter.add_loaded_layer`` fires the post-publish
    # ``emit_map_command("zoom-to")`` (pipeline_emitter.py:443-447). Prior to
    # this fix the wrapper dropped bbox (``envelope.layers[0]`` is a
    # ``ResultLayer`` with no bbox field) → silent no-zoom after layer landed.
    if envelope.layers:
        primary = envelope.layers[0]
        return LayerURI(
            layer_id=primary.layer_id,
            name=primary.name,
            layer_type=primary.layer_type,
            uri=primary.uri,
            style_preset=primary.style_preset,
            temporal=primary.temporal,
            role=primary.role,
            units=primary.units,
            bbox=envelope.bbox,
        )
    return envelope.model_dump(mode="json")
