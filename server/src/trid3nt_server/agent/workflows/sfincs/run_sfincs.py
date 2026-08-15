"""SFINCS solve dispatch: progress cadence, telemetry, envelope + error seams.

Engine-door conformance split (matches every other engine's ``run_<engine>.py``):
the solve/progress/telemetry/envelope-and-error layer that ``model_flood_scenario``
drives is factored out of the template body into this engine-support module.
``flood/flood.py`` re-imports these and calls them exactly as before -- pure
reorganization, no behavior change.

The module logs under the ``...sfincs.flood.flood`` logger name (unchanged) so the
observable log surface is byte-identical to the pre-split module.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.envelope import (
    AssessmentEnvelope,
    DataSource,
    FloodMetrics,
    FloodPayload,
    ForcingSummary,
    Provenance,
)
from trid3nt_contracts.execution import ExecutionHandle, ModelSetup, RunResult
from trid3nt_server.agent.tools.fetchers.socioeconomic.geocode_location.geocode_location import geocode_location

logger = logging.getLogger("trid3nt_server.agent.workflows.sfincs.flood.flood")


async def _emit_presolver_progress(
    emitter: Any, progress_percent: int
) -> None:
    """Best-effort pre-solver progress bump on the current pipeline card.

    Keeps the card from sitting SILENTLY during the multi-second pre-solver
    chain. ``emitter`` is the ``current_emitter()`` handle (may be ``None``
    outside a WS dispatch -- direct call / smoke / unit test); failure is
    swallowed because progress is a UX hint, never a correctness gate.
    """
    if emitter is None:
        return
    try:
        await emitter.update_current_progress(progress_percent)
    except Exception as exc:  # noqa: BLE001 -- progress is non-fatal
        logger.debug(
            "model_flood_scenario: pre-solver progress emit failed (non-fatal): %s",
            exc,
        )


#: Cadence (seconds) for the LIVE solve-progress envelope during the long solve.
#: Independent of the solver poll cadence -- this is a UX tick on the running
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
    os.environ.get("TRID3NT_COASTAL_OUTPUT_INTERVAL_MIN", "5")
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
    from trid3nt_server.agent.workflows.sfincs.postprocess_sfincs import MAX_FLOOD_FRAMES

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
    from trid3nt_server.telemetry import build_live_solve_progress

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
            except Exception as exc:  # noqa: BLE001 -- UX hint, never fatal
                logger.debug(
                    "model_flood_scenario: live solve-progress emit failed "
                    "(non-fatal): %s",
                    exc,
                )
            await asyncio.sleep(_LIVE_SOLVE_PROGRESS_INTERVAL_S)
    except asyncio.CancelledError:
        # Normal teardown when the solve completes -- re-raise so the task
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
    os.environ.get("TRID3NT_PRESOLVER_PROGRESS_TICK_S", "7")
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
            except Exception as exc:  # noqa: BLE001 -- liveness hint, never fatal
                logger.debug(
                    "model_flood_scenario: pre-solver progress tick failed "
                    "(non-fatal): %s",
                    exc,
                )
    except asyncio.CancelledError:
        # Normal teardown when the phase completes -- re-raise so the task
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
# Helpers -- bbox resolution + zero-metrics envelope builder
# --------------------------------------------------------------------------- #


def _resolve_bbox(
    *,
    bbox: tuple[float, float, float, float] | None,
    location_query: str | None,
) -> tuple[tuple[float, float, float, float], dict[str, Any] | None]:
    """Resolve the bbox via direct param or via ``geocode_location``.

    Precedence per the kickoff TENTATIVE: bbox-direct wins when both are
    given (matches the "intent + irreducible inputs" -- bbox IS
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
            "sfincs_flood requires either bbox or location_query",
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

    Per (TENTATIVE): zero-valued
    FloodMetrics + error_code threaded into ``solver_version`` (a documented
    out-of-band seam -- the schema-side ``solver_version`` is a string field
    so we can carry ``"failed:LULC_MAPPING_MISMATCH"`` etc. The agent surface
    parses this and emits a meaningful failure narration.)

    All required envelope fields are populated with safe defaults so the
    pydantic validator doesn't reject the failed envelope.
    """
    now = datetime.now(timezone.utc)
    # promote the error code onto the depth-0
    # ``workflow_name`` string ("<name>:FAILED:<CODE>") so it survives the
    # adapter's ``_coerce_to_summary_value`` depth>=2 dict-collapse (the
    # ``flood.metrics.solver_version`` threading sits at depth 2 and is reduced
    # to bare key names before the LLM sees it). This gives the adapter's
    # failed-modeled-envelope classifier (summarize_tool_result, B1) a
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
    """Emit a solve-completion telemetry record (autoscale).

    Pulls the autoscale provenance (estimated active cells, chosen resolution,
    vCPU) off ``model_setup.parameters`` and the wall-clock from the
    ``RunResult`` (``duration_seconds``), and folds in the backend
    (``handle.workflow_name`` -- ``local-docker`` / ``local-exec`` /
    ``model_flood_scenario``) + aoi_km2. Best-effort; returns the record
    (or ``None`` on any failure) so the caller's try/except stays simple.
    """
    from trid3nt_server.telemetry import emit_solve_telemetry

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

    the regular-grid SFINCS Batch path exposes both a ``handle`` and a
    terminal ``RunResult``; the wait-loop captured the Spot instance + timing
    breakdown onto ``run_result.batch_compute_meta`` (best-effort, may be
    ``None``). This folds that together with the active-cell count + the built
    grid resolution + the solver + the terminal status + the run/case/session ids
    into the SOLVE telemetry sink (``telemetry.record_solve_telemetry``). Mirrors
    ``_emit_flood_solve_telemetry`` (the autoscale row) -- they are siblings: the
    autoscale row drives cap re-tuning, this row drives completion-time
    inference. Best-effort; returns the recorded row (or ``None`` on any failure)
    so the caller's try/except stays trivial. Only the regular-grid path calls
    this (the quadtree submit+wait path is left uninstrumented, consistent with
    the two-card work)."""
    from trid3nt_server.telemetry import record_solve_telemetry

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
    # vcpus / memory_mib / *_at_ms / *_secs) -- present only on the aws-batch
    # terminal paths; empty dict otherwise (local/in-process).
    row.update(meta)
    return record_solve_telemetry(row)


def _default_runs_prefix(run_id: str) -> str:
    """Fallback runs prefix when ``RunResult.output_uri`` is None.

    Mints the same ``s3://<runs_bucket>/<run_id>/`` shape the local-docker
    solver writes outputs under (``TRID3NT_RUNS_BUCKET``, default
    ``trid3nt-runs`` -- the local MinIO runs bucket). GCP is gone: no
    gs:// fabrication.
    """
    import os

    bucket = (os.environ.get("TRID3NT_RUNS_BUCKET") or "").strip() or "trid3nt-runs"
    return f"s3://{bucket}/{run_id}/"
