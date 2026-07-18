"""GeoClaw (Clawpack) shallow-water inundation composer (sprint-17).

The GeoClaw analogue of ``model_urban_flood_swmm`` (SWMM) /
``model_flood_scenario`` (SFINCS) / ``model_groundwater_contamination_scenario``
(MODFLOW). A deterministic orchestrator-style workflow (Invariant 2 - no LLM in
the chain) that composes the GeoClaw shallow-water engine end-to-end:

    fetch topo/bathy DEM (fetch_topobathy seamless land+bathy -> fetch_dem fallback)
      -> stage build_spec manifest + DEM reference to S3 (run_geoclaw)
      -> run_solver('geoclaw', ...) -> wait_for_completion (AWS Batch, the SAME
         generic dispatch seam SFINCS/SWMM-off-box use)
      -> download the GeoClaw fort.q frames from the Batch output
      -> postprocess_geoclaw (rasterize fort.q AMR frames -> peak primary COG +
         per-frame COGs)
      -> publish the peak primary + emit the frames out-of-band (the Phase-1
         scrubber animation group).

GeoClaw is BATCH-ONLY (the Clawpack Fortran lives in the worker image, never in
the agent venv), so there is no in-process lane - unlike SWMM this composer
ALWAYS dispatches to AWS Batch. It mirrors the SWMM off-box Batch lane verbatim
(two-card sim observability, live solve-progress heartbeat, telemetry, Batch
output download).

Returns the PEAK ``GeoClawDepthLayerURI`` directly (a ``LayerURI`` subtype) so
the ``emit_tool_call`` ``add_loaded_layer`` gate fires on it. Per-frame depth
COGs are emitted OUT-OF-BAND through ``emitter.add_loaded_layer`` so the web
``detectSequentialGroups`` LayerPanel scrubber group forms.

Determinism boundary (Invariant 1): every depth number the agent narrates comes
from the typed ``GeoClawDepthLayerURI.max_depth_m`` / ``.flooded_area_km2`` /
``.max_inundation_m`` fields the postprocess computed with plain arithmetic -
never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from grace2_contracts.execution import LayerURI
from grace2_contracts.geoclaw_contracts import (
    GEOCLAW_DEPTH_STYLE_PRESET,
    GeoClawDepthLayerURI,
    GeoClawRunArgs,
)

from ..layer_uri_emit import emit_layer_uri
from ..pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from ..tools.publish_layer import PublishLayerError, publish_layer
from .postprocess_geoclaw import PostprocessGeoClawError, postprocess_geoclaw
from .run_geoclaw import (
    GEOCLAW_SOLVER_NAME,
    GeoClawWorkflowError,
    stage_geoclaw_manifest,
)
from .solve_progress import drive_live_solve_progress

logger = logging.getLogger(
    "grace2_agent.workflows.model_dambreak_geoclaw_scenario"
)

__all__ = [
    "model_dambreak_geoclaw_scenario",
    "GeoClawComposerError",
]

#: GeoClaw solve ETA heuristic (s) per base-grid cell - a coarse perf hint for
#: the live progress heartbeat (Invariant 1: a hint, never a narrated number).
_GEOCLAW_SEC_PER_CELL: float = 0.05


class GeoClawComposerError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    error_code: str = "GEOCLAW_COMPOSER_FAILED"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# --------------------------------------------------------------------------- #
# DEM acquisition (topobathy seamless -> fetch_dem fallback).
# --------------------------------------------------------------------------- #
def _fetch_topo_for_geoclaw(
    bbox: tuple[float, float, float, float],
) -> str:
    """Fetch a topo/bathy DEM for the AOI and return its ``s3://`` URI.

    GeoClaw needs a SEAMLESS land+bathymetry DEM (the shallow-water bed): try
    ``fetch_topobathy`` first (the seamless coastal DEM, the right substrate for
    tsunami / surge run-up), fall back to ``fetch_dem`` (3DEP land-only) for an
    inland dam-break where bathymetry is irrelevant (the data-source fallback
    norm: primary -> fallback -> honest typed error).

    Returns the DEM cache/runs ``s3://`` URI (staged BY REFERENCE - the worker
    downloads it directly). Raises ``GeoClawComposerError`` only when BOTH fail.
    """
    from ..tools.data_fetch import fetch_dem
    from ..tools.fetch_topobathy import fetch_topobathy

    try:
        layer = fetch_topobathy(bbox)
        uri = getattr(layer, "uri", None) or (
            layer.get("uri") if isinstance(layer, dict) else None
        )
        if uri:
            return str(uri)
    except Exception as exc:  # noqa: BLE001 - fall through to fetch_dem
        logger.info(
            "fetch_topobathy failed (%s); falling back to fetch_dem(10m)", exc
        )

    try:
        layer = fetch_dem(bbox, resolution_m=10)
        uri = getattr(layer, "uri", None) or (
            layer.get("uri") if isinstance(layer, dict) else None
        )
        if not uri:
            raise GeoClawComposerError(
                "GEOCLAW_DEM_FETCH_FAILED",
                f"fetch_dem returned no uri for bbox {bbox}",
            )
        return str(uri)
    except GeoClawComposerError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GeoClawComposerError(
            "GEOCLAW_DEM_FETCH_FAILED",
            f"both DEM sources failed for bbox {bbox}: topobathy + fetch_dem-10m: {exc}",
        ) from exc


def _record_geoclaw_batch_solve_telemetry(
    *,
    run_result: Any,
    handle: Any,
    staging: Any,
    compute_class: str,
    session_id: str | None = None,
    case_id: str | None = None,
) -> dict | None:
    """Record ONE SOLVE row for the GeoClaw Batch lane (mirrors the SWMM/SFINCS
    telemetry sibling). Best-effort; returns the recorded row or ``None``."""
    from ..telemetry import record_solve_telemetry

    meta = getattr(run_result, "batch_compute_meta", None) or {}
    if not isinstance(meta, dict):
        meta = {}

    row: dict = {
        "run_id": getattr(run_result, "run_id", None) or staging.run_id,
        "solver": GEOCLAW_SOLVER_NAME,
        "status": getattr(run_result, "status", None),
        "backend": str(getattr(handle, "workflow_name", "") or "unknown"),
        "compute_class": compute_class,
        "case_id": case_id,
        "session_id": session_id,
        "active_cell_count": int(getattr(staging, "n_active_cells", 0) or 0),
        "scenario": staging.run_args.scenario,
    }
    row.update(meta)
    return record_solve_telemetry(row)


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
async def model_dambreak_geoclaw_scenario(
    run_args: GeoClawRunArgs,
    *,
    dem_uri: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    cleanup_outputs: bool = True,
) -> GeoClawDepthLayerURI:
    """Compose the full GeoClaw shallow-water inundation chain end-to-end (Batch).

    Args:
        run_args: the validated ``GeoClawRunArgs`` (bbox + scenario + forcing).
        dem_uri: optional topo/bathy DEM ``s3://`` URI. When ``None`` the composer
            fetches it (``fetch_topobathy`` -> ``fetch_dem`` fallback). Tests pass
            a synthetic URI to skip the fetch.
        run_id: optional ULID; minted by the staging step if absent.
        compute_class: FR-CE-3 compute class for the Batch sizing.
        cleanup_outputs: when True, the downloaded fort.q output dir is removed
            after postprocess (the COGs were already uploaded).

    Returns:
        The PEAK ``GeoClawDepthLayerURI`` (role ``"primary"``, name ``"Peak flood
        depth"``) carrying the three narration scalars + the echoed scenario.
        Per-frame depth layers are emitted out-of-band via the emitter.

    Raises:
        GeoClawComposerError / GeoClawWorkflowError / PostprocessGeoClawError on a
        fatal stage failure (the tool wrapper catches these and returns a typed
        error dict so the agent narrates honestly).
    """
    bbox = tuple(run_args.bbox)
    emitter = current_emitter()

    # --- Zoom-on-area-first: the map zooms before the solve runs. ---
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
            logger.warning(
                "model_dambreak_geoclaw_scenario: zoom-to emit failed: %s", exc
            )

    # --- Sub-step plan (task-168): fetch DEM -> stage -> solve -> postprocess
    #     -> publish peak. begin_substeps stamps the parent breadcrumb cap; it is
    #     a no-op outside emit_tool_call (current_emitter() is None).
    begin_substeps(emitter, 5)

    # --- Step 1: topo/bathy DEM (off-loop blocking I/O) ---------------------
    if dem_uri is None:
        async with substep(emitter, "fetch_topobathy"):
            resolved_dem_uri = await asyncio.to_thread(_fetch_topo_for_geoclaw, bbox)
    else:
        resolved_dem_uri = dem_uri
    logger.info("model_dambreak_geoclaw_scenario: DEM=%s", resolved_dem_uri)

    # Optional staged tsunami dtopo / surge forcing (already-staged URIs on args).
    dtopo_uri = run_args.tsunami_dtopo_uri
    surge_uri = run_args.surge_forcing_uri
    # Optional additional topo/bathy tiles (ordered coarse -> fine on the args).
    extra_dem_uris = list(run_args.extra_topo_uris or [])

    # --- Step 2: stage the build_spec manifest + DEM reference --------------
    # The USER-GATED fault_* + coastal_gauge_lonlat + fgmax_arrival_tol_m live on
    # run_args and ride into the build_spec inside stage_geoclaw_manifest ->
    # build_geoclaw_build_spec (only the supplied fault_* are threaded).
    async with substep(emitter, "stage_geoclaw_manifest"):
        staging = await asyncio.to_thread(
            stage_geoclaw_manifest,
            run_args,
            dem_uri=resolved_dem_uri,
            run_id=run_id,
            dtopo_uri=dtopo_uri,
            surge_uri=surge_uri,
            extra_dem_uris=extra_dem_uris,
        )

    # --- Auto vertical scaling from the base grid cell count ----------------
    from ..tools.solver import (
        AWS_BATCH_COMPUTE_CLASS_SIZING,
        select_compute_class,
    )

    n_active = int(getattr(staging, "n_active_cells", 0) or 0)
    auto_class = select_compute_class(n_active) if n_active > 0 else compute_class
    # Honor an explicit HIGHER compute_class from the caller: take the larger of
    # the auto-sized tier and the requested tier so vertical auto-scaling still
    # scales UP for big domains, but a user asking for "large"/"xlarge" (quicker
    # turnaround) is never silently downgraded to the small/standard auto pick.
    _CLASS_RANK = {"small": 0, "standard": 1, "large": 2, "xlarge": 3}
    effective_compute_class = max(
        auto_class, compute_class, key=lambda c: _CLASS_RANK.get(c, 1)
    )
    _vcpus = AWS_BATCH_COMPUTE_CLASS_SIZING.get(effective_compute_class, {}).get(
        "vcpus"
    )

    # --- Step 3: dispatch to AWS Batch (the generic run_solver seam) --------
    from ..tools.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    handle = run_solver(
        solver=GEOCLAW_SOLVER_NAME,
        model_setup_uri=staging.manifest_uri,
        compute_class=effective_compute_class,
    )

    # --- Two-card sim observability (dispatch card + Batch-bound sim card) --
    _sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter,
        solver=GEOCLAW_SOLVER_NAME,
        handle=handle,
        compute_class=effective_compute_class,
    )
    if emitter is not None and _sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))

    _progress_task = asyncio.ensure_future(
        drive_live_solve_progress(
            emitter=current_emitter(),
            run_id=staging.run_id,
            solver=GEOCLAW_SOLVER_NAME,
            grid_resolution_m=None,
            active_cell_count=n_active or None,
            vcpus=int(_vcpus) if _vcpus is not None else None,
            eta_seconds=(n_active * _GEOCLAW_SEC_PER_CELL) if n_active else None,
        )
    )
    run_result = None
    # task-168: surface the solve as a child "run_solver" row in the parent
    # timeline. The Sim card (mint_dispatch_and_sim_cards) STILL owns the live
    # Batch readout (hard invariant); this child is the timeline entry that goes
    # green/red/yellow with the solve. No-op outside emit_tool_call. The original
    # cancel/cleanup flow + telemetry + typed-error raise are PRESERVED verbatim:
    # a returned non-"complete" RunResult raises a SENTINEL inside the substep so
    # the child row reads red (honesty floor), which we swallow right after the
    # context exits so control falls through to the UNCHANGED telemetry + typed-
    # error block below (the parent's own state is owned there, not by the child).
    class _SolveReturnedFailed(RuntimeError):
        pass

    try:
        async with substep(emitter, "run_solver"):
            try:
                run_result = await wait_for_completion(handle)
            except asyncio.CancelledError:
                # Invariant 8: propagate the cancel; route it to the SIM card.
                logger.info(
                    "model_dambreak_geoclaw_scenario cancelled while awaiting solver"
                )
                await route_sim_terminal(emitter, _sim_step_id, run_result=None)
                raise
            finally:
                _progress_task.cancel()
                try:
                    await _progress_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                set_emitter_binding(None)
            if run_result.status != "complete":
                raise _SolveReturnedFailed
    except _SolveReturnedFailed:
        # Child already marked red by the substep; fall through to the original
        # telemetry + typed-error path (which records + raises GeoClawWorkflowError).
        pass

    await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    # --- SOLVE telemetry (Batch instance + size + timing) ------------------
    try:
        _record_geoclaw_batch_solve_telemetry(
            run_result=run_result,
            handle=handle,
            staging=staging,
            compute_class=effective_compute_class,
        )
    except Exception as exc:  # noqa: BLE001 - never break the solve
        logger.warning(
            "GeoClaw solve batch-compute telemetry failed (non-fatal): %s", exc
        )

    if run_result.status != "complete":
        raise GeoClawWorkflowError(
            "GEOCLAW_RUN_FAILED",
            message=(
                "GeoClaw Batch solve did not complete "
                f"(status={run_result.status}, "
                f"error_code={getattr(run_result, 'error_code', None)}): "
                f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}"
            ),
            details={
                "run_id": staging.run_id,
                "output_uri": getattr(run_result, "output_uri", None),
            },
        )

    # --- Step 4: download the Batch fort.q outputs -------------------------
    batch_run_id = getattr(run_result, "run_id", None) or staging.run_id
    out_dir = await asyncio.to_thread(_download_batch_geoclaw_outputs, batch_run_id)

    try:
        # --- Step 5: postprocess (rasterize fort.q -> peak + frames) -------
        async with substep(emitter, "postprocess_geoclaw"):
            layers, metrics = await asyncio.to_thread(
                postprocess_geoclaw,
                out_dir,
                bbox,
                run_id=staging.run_id,
                scenario=run_args.scenario,
                fgmax_arrival_tol_m=run_args.fgmax_arrival_tol_m,
            )
    finally:
        if cleanup_outputs:
            _cleanup_dir(out_dir)

    if not layers:
        raise GeoClawComposerError(
            "GEOCLAW_NO_LAYERS",
            "postprocess_geoclaw produced no depth layers (empty solve?)",
        )

    raw_peak = layers[0]
    frame_layers = layers[1:]

    # --- Step 6: publish the PEAK COG through publish_layer (render chokepoint)
    async with substep(emitter, "publish_layer"):
        peak = await asyncio.to_thread(_publish_peak_layer, raw_peak, staging.run_id)

    # --- Step 6b: publish + emit the per-frame animation layers OUT-OF-BAND --
    emitted_frames = await _emit_frame_layers(emitter, frame_layers, staging.run_id)

    logger.info(
        "model_dambreak_geoclaw_scenario complete run_id=%s scenario=%s "
        "max_depth_m=%.4g flooded_area_km2=%.6g max_inundation_m=%.4g "
        "arrival_time_s=%s frames_emitted=%d/%d peak_uri=%s",
        staging.run_id,
        run_args.scenario,
        peak.max_depth_m,
        peak.flooded_area_km2,
        peak.max_inundation_m,
        peak.arrival_time_s,
        emitted_frames,
        len(frame_layers),
        peak.uri,
    )

    # --- AUTHORITATIVE LAST zoom-to ----------------------------------------
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "model_dambreak_geoclaw_scenario: authoritative zoom-to failed: %s",
                exc,
            )

    return peak


def _publish_peak_layer(
    raw_peak: GeoClawDepthLayerURI, run_id: str
) -> GeoClawDepthLayerURI:
    """Publish the PEAK depth COG through publish_layer (render chokepoint).

    Routes the raw s3:// peak COG through ``publish_layer`` and returns a NEW
    ``GeoClawDepthLayerURI`` carrying the published /tiles or WMS URL plus the
    narration scalars. On publish failure the raw peak is returned UNCHANGED: the
    dispatch-level ``emit_layer_uri`` guardrail then drops the dead raw-s3://
    raster from the map (honest) while the typed metrics still narrate.

    Mirrors the SWMM/SFINCS primary-publish path.
    """
    if raw_peak.layer_type != "raster" or not (
        raw_peak.uri.startswith("gs://") or raw_peak.uri.startswith("s3://")
    ):
        return raw_peak
    layer_id_for_pub = f"geoclaw-depth-peak-{run_id}"
    try:
        published_uri = publish_layer(
            layer_uri=raw_peak.uri,
            layer_id=layer_id_for_pub,
            style_preset=raw_peak.style_preset or GEOCLAW_DEPTH_STYLE_PRESET,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_dambreak_geoclaw_scenario: publish_layer FAILED for the peak "
            "layer_id=%s error_code=%s (%s) - returning the unpublished peak.",
            layer_id_for_pub,
            exc.error_code,
            exc,
        )
        return raw_peak
    return GeoClawDepthLayerURI(
        layer_id=layer_id_for_pub,
        name=raw_peak.name,
        layer_type=raw_peak.layer_type,
        uri=published_uri,
        style_preset=raw_peak.style_preset or GEOCLAW_DEPTH_STYLE_PRESET,
        role=raw_peak.role,
        units=raw_peak.units,
        bbox=raw_peak.bbox,
        max_depth_m=raw_peak.max_depth_m,
        flooded_area_km2=raw_peak.flooded_area_km2,
        max_inundation_m=raw_peak.max_inundation_m,
        arrival_time_s=raw_peak.arrival_time_s,
        scenario=raw_peak.scenario,
    )


async def _emit_frame_layers(
    emitter: Any, frame_layers: list[GeoClawDepthLayerURI], run_id: str
) -> int:
    """Publish + emit per-frame depth COGs out-of-band so the web scrubber forms.

    Each frame COG is routed through ``publish_layer`` (render chokepoint) so it
    carries a renderable URL before ``add_loaded_layer``. The "Flood depth step N"
    name token is preserved so the web ``detectSequentialGroups`` groups them. A
    frame that fails to publish is HONESTLY DROPPED. Returns the number emitted
    (0 when no emitter is bound). Never raises (mirrors SWMM).
    """
    if not frame_layers or emitter is None:
        if frame_layers:
            logger.info(
                "model_dambreak_geoclaw_scenario: %d animation frames available "
                "but no emitter bound - frames not emitted.",
                len(frame_layers),
            )
        return 0
    emitted = 0
    for lyr in frame_layers:
        if not (lyr.uri.startswith("gs://") or lyr.uri.startswith("s3://")):
            emit_layer: LayerURI = lyr
        else:
            try:
                frame_uri = await asyncio.to_thread(
                    publish_layer,
                    layer_uri=lyr.uri,
                    layer_id=lyr.layer_id,
                    style_preset=lyr.style_preset or GEOCLAW_DEPTH_STYLE_PRESET,
                )
            except PublishLayerError as exc:
                logger.warning(
                    "model_dambreak_geoclaw_scenario: publish_layer FAILED for "
                    "frame layer_id=%s error_code=%s (%s) - dropping this frame.",
                    lyr.layer_id,
                    exc.error_code,
                    exc,
                )
                continue
            emit_layer = GeoClawDepthLayerURI(
                layer_id=lyr.layer_id,
                name=lyr.name,
                layer_type=lyr.layer_type,
                uri=frame_uri,
                style_preset=lyr.style_preset or GEOCLAW_DEPTH_STYLE_PRESET,
                role=lyr.role,
                units=lyr.units,
                bbox=lyr.bbox,
                max_depth_m=lyr.max_depth_m,
                flooded_area_km2=lyr.flooded_area_km2,
                max_inundation_m=lyr.max_inundation_m,
                scenario=lyr.scenario,
            )
        try:
            safe = emit_layer_uri(emit_layer)
            if safe is not None:
                await emitter.add_loaded_layer(safe)
                emitted += 1
        except Exception as exc:  # noqa: BLE001 - never break the solve
            logger.warning(
                "model_dambreak_geoclaw_scenario: frame add_loaded_layer failed "
                "for %s: %s",
                emit_layer.layer_id,
                exc,
            )
    if emitted:
        logger.info(
            "model_dambreak_geoclaw_scenario: emitted %d/%d animation frames as a "
            "sequential group (run_id=%s)",
            emitted,
            len(frame_layers),
            run_id,
        )
    return emitted


def _cleanup_dir(d: str | Path) -> None:
    """Best-effort removal of a downloaded scratch dir."""
    try:
        shutil.rmtree(Path(d), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _download_batch_geoclaw_outputs(run_id: str) -> str:
    """Download the Batch fort.q outputs to a tmp ``_output/`` dir for postprocess.

    The GeoClaw Batch worker uploads its fort.q frames under
    ``s3://<runs_bucket>/<run_id>/_output/`` and records their URIs in
    completion.json ``output_uris``. We re-read completion.json (small, already on
    S3) to find the fort.* keys, download them via the SAME boto3 client the
    solver dispatch uses (no new client), and return the local dir holding an
    ``_output/`` subtree the postprocess discovers.

    Raises:
        GeoClawWorkflowError("GEOCLAW_BATCH_OUTPUT_MISSING"): the completed run
            produced no downloadable fort.q (a 'complete' solve with no output is
            a real failure - never a silent dead-end).
    """
    from ..tools.solver import (
        _get_runs_bucket,
        _get_s3_client,
        _split_object_uri,
        _try_get_completion_s3,
    )

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()

    keys: list[str] = []
    manifest = _try_get_completion_s3(runs_bucket, run_id)
    if isinstance(manifest, dict):
        for raw in manifest.get("output_uris") or []:
            uri = str(raw)
            try:
                _scheme, _bucket, key = _split_object_uri(uri)
            except Exception:  # noqa: BLE001
                continue
            base = key.rsplit("/", 1)[-1]
            if base.startswith("fort."):
                keys.append(key)
    if not keys:
        # Defensive fallback: list the runs prefix for fort.* objects.
        try:
            resp = s3.list_objects_v2(
                Bucket=runs_bucket, Prefix=f"{run_id}/_output/"
            )
            for obj in resp.get("Contents", []) or []:
                k = obj.get("Key", "")
                if k.rsplit("/", 1)[-1].startswith("fort."):
                    keys.append(k)
        except Exception as exc:  # noqa: BLE001
            logger.warning("GeoClaw output list fallback failed: %s", exc)

    tmp_dir = tempfile.mkdtemp(prefix=f"geoclaw-batch-out-{run_id}-")
    out_sub = Path(tmp_dir) / "_output"
    out_sub.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    for key in keys:
        base = key.rsplit("/", 1)[-1]
        dest = out_sub / base
        try:
            resp = s3.get_object(Bucket=runs_bucket, Key=key)
            with dest.open("wb") as fh:
                shutil.copyfileobj(resp["Body"], fh)
            downloaded += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "GeoClaw Batch output download failed s3://%s/%s: %s",
                runs_bucket,
                key,
                exc,
            )

    has_frame = any(
        p.name.startswith("fort.q") for p in out_sub.iterdir() if p.is_file()
    )
    if not has_frame:
        _cleanup_dir(tmp_dir)
        raise GeoClawWorkflowError(
            "GEOCLAW_BATCH_OUTPUT_MISSING",
            message=(
                f"GeoClaw Batch run {run_id} completed but produced no downloadable "
                f"fort.q frames under s3://{runs_bucket}/{run_id}/_output/ "
                f"(downloaded {downloaded} fort.* objects)"
            ),
            details={"run_id": run_id, "runs_bucket": runs_bucket},
        )

    return tmp_dir
