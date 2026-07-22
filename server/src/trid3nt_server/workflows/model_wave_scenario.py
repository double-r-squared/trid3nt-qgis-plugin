"""SWAN standalone nearshore wave-field composer (Phase 1).

The SWAN analogue of ``model_dambreak_geoclaw_scenario`` (GeoClaw) /
``model_flood_scenario`` (SFINCS). A deterministic orchestrator-style workflow
(Invariant 2 -- no LLM in the chain) that composes the STANDALONE SWAN wave-field
engine end-to-end:

    fetch topo/bathy DEM (fetch_topobathy seamless land+bathy)
      -> resolve a parametric offshore wave boundary (demo synthesis, or the
         caller-supplied SwanRunArgs.boundary)
      -> stage build_spec manifest + DEM reference to S3 (run_swan)
      -> run_solver('swan', ...) -> wait_for_completion (AWS Batch, the SAME
         generic dispatch seam SFINCS/GeoClaw use)
      -> download the SWAN swan_out.mat output from the Batch run
      -> postprocess_swan (rasterize the Hs field -> peak primary Hs COG +
         per-frame Hs COGs)
      -> publish the peak primary + emit the frames out-of-band (the Phase-1
         scrubber animation group).

SWAN is the ADDITIVE comparison engine: it runs STANDALONE and produces its OWN
wave-field layers (Hs / Tp / Dir) so a user can compare SWAN against the existing
SFINCS+SnapWave output on the SAME case. This is NOT a pivot away from SFINCS and
NOT a coupling job in v0.1 (no ``wave`` member is added to the surge-forcing seam;
the SWAN->SFINCS wave-setup coupling is a clearly-commented LATER step in
``run_swan.py``).

SWAN is BATCH-ONLY (the GPL Fortran lives in the worker image, never in the agent
venv), so there is no in-process lane -- like GeoClaw this composer ALWAYS
dispatches to AWS Batch. It mirrors the GeoClaw off-box Batch lane verbatim
(two-card sim observability, live solve-progress heartbeat, telemetry, Batch
output download).

Returns the PEAK ``WaveFieldLayerURI`` directly (a ``LayerURI`` subtype) so the
``emit_tool_call`` ``add_loaded_layer`` gate fires on it. Per-frame Hs COGs are
emitted OUT-OF-BAND through ``emitter.add_loaded_layer`` so the web
``detectSequentialGroups`` LayerPanel scrubber group forms.

Determinism boundary (Invariant 1): every wave number the agent narrates comes
from the typed ``WaveFieldLayerURI`` fields the postprocess computed with plain
arithmetic -- never free-generated. Honesty floor: a SWAN run that produced no
wave field raises ``SWAN_OUTPUT_EMPTY`` (it NEVER reports status ok).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts.execution import LayerURI
from trid3nt_contracts.swan_contracts import (
    SWAN_WAVE_HEIGHT_STYLE_PRESET,
    SwanRunArgs,
    WaveFieldLayerURI,
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
from .postprocess_swan import PostprocessSwanError, postprocess_swan
from .register_published_manifest import (
    read_publish_manifest,
    register_swan_wave_layers,
)
from .run_swan import (
    SWAN_SOLVER_NAME,
    SwanWorkflowError,
    stage_swan_manifest,
)
from .solve_progress import drive_live_solve_progress

logger = logging.getLogger("trid3nt_server.workflows.model_wave_scenario")

__all__ = [
    "model_wave_scenario",
    "SwanComposerError",
]

#: SWAN solve ETA heuristic (s) per mesh cell -- a coarse perf hint for the live
#: progress heartbeat (Invariant 1: a hint, never a narrated number). A full
#: spectral solve is pricier per cell than a shallow-water step.
_SWAN_SEC_PER_CELL: float = 0.08


class SwanComposerError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    error_code: str = "SWAN_COMPOSER_FAILED"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


# --------------------------------------------------------------------------- #
# Bathy DEM acquisition (topobathy seamless -> fetch_dem fallback).
# --------------------------------------------------------------------------- #
def _fetch_bathy_for_swan(
    bbox: tuple[float, float, float, float],
) -> str:
    """Fetch a topo/bathy DEM for the AOI and return its ``s3://`` URI.

    SWAN needs a SEAMLESS land+bathymetry DEM (the bed for depth-induced shoaling /
    breaking): try ``fetch_topobathy`` first (the seamless coastal DEM -- the right
    substrate for a nearshore wave field).

    REQUIRES REAL BATHYMETRY: a coastal wave model run on a LAND-ONLY DEM (all
    positive NAVD88 elevations) renders an ALL-DRY SWAN bottom grid (every cell
    above the still-water level -> depth < DEPMIN -> inactive), so SWAN "prepares
    computation", runs zero sweeps, and writes no swan_out.mat -- the live
    2026-06-23 Mexico Beach 33 ms no-op. ``fetch_topobathy`` degrades to a
    land-only 3DEP fallback and signals it via ``bathymetry_present=False``; the
    older code only checked ``.uri`` and so SILENTLY fed that all-dry DEM to the
    worker. We now REJECT a bathymetry-absent result up front with an honest typed
    error rather than launch a guaranteed no-op solve. The old direct ``fetch_dem``
    (3DEP, land-only) fallback is REMOVED: it can never carry below-sea-level
    depths, so it would always produce an all-dry deck for a coastal AOI.

    Returns the DEM cache/runs ``s3://`` URI (staged BY REFERENCE -- the worker
    downloads it directly). Raises ``SwanComposerError`` when the fetch fails OR
    when the result carries no bathymetry (the data-source fallback norm: primary
    -> honest typed error, never a silent all-dry dead-end).
    """
    from ..tools.fetchers.ocean.fetch_topobathy import fetch_topobathy

    def _attr(layer: Any, name: str) -> Any:
        if isinstance(layer, dict):
            return layer.get(name)
        return getattr(layer, name, None)

    try:
        layer = fetch_topobathy(bbox)
    except Exception as exc:  # noqa: BLE001
        raise SwanComposerError(
            "SWAN_DEM_FETCH_FAILED",
            f"fetch_topobathy failed for bbox {bbox}: {exc}",
        ) from exc

    uri = _attr(layer, "uri")
    if not uri:
        raise SwanComposerError(
            "SWAN_DEM_FETCH_FAILED",
            f"fetch_topobathy returned no uri for bbox {bbox}",
        )

    # REQUIRE real bathymetry. ``bathymetry_present`` is False when CUDEM had no
    # coverage and fetch_topobathy degraded to a LAND-ONLY 3DEP surface -- which
    # has NO below-datum sea cells, so the SWAN bottom grid would be entirely dry
    # and SWAN would no-op silently. Default True so a plain ``LayerURI`` (no flag,
    # e.g. a test stub) is accepted.
    bathy_present = _attr(layer, "bathymetry_present")
    if bathy_present is False:
        warning = _attr(layer, "fallback_warning")
        raise SwanComposerError(
            "SWAN_NO_BATHYMETRY",
            f"fetch_topobathy returned a LAND-ONLY DEM for bbox {bbox} "
            f"(bathymetry_present=False); a coastal SWAN run needs real "
            f"below-datum bathymetry or the computational grid is all-dry and "
            f"SWAN no-ops (empty solve). "
            + (f"({warning})" if warning else "No CUDEM coastal coverage for this AOI."),
        )

    return str(uri)


def _record_swan_batch_solve_telemetry(
    *,
    run_result: Any,
    handle: Any,
    staging: Any,
    compute_class: str,
    session_id: str | None = None,
    case_id: str | None = None,
) -> dict | None:
    """Record ONE SOLVE row for the SWAN Batch lane (mirrors the GeoClaw/SFINCS
    telemetry sibling). Best-effort; returns the recorded row or ``None``."""
    from ..telemetry import record_solve_telemetry

    meta = getattr(run_result, "batch_compute_meta", None) or {}
    if not isinstance(meta, dict):
        meta = {}

    row: dict = {
        "run_id": getattr(run_result, "run_id", None) or staging.run_id,
        "solver": SWAN_SOLVER_NAME,
        "status": getattr(run_result, "status", None),
        "backend": str(getattr(handle, "workflow_name", "") or "unknown"),
        "compute_class": compute_class,
        "case_id": case_id,
        "session_id": session_id,
        "active_cell_count": int(getattr(staging, "n_active_cells", 0) or 0),
        "mode": staging.run_args.mode,
    }
    row.update(meta)
    return record_solve_telemetry(row)


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
async def model_wave_scenario(
    run_args: SwanRunArgs,
    *,
    dem_uri: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
    cleanup_outputs: bool = True,
) -> WaveFieldLayerURI:
    """Compose the full standalone SWAN nearshore wave-field chain (Batch).

    Args:
        run_args: the validated ``SwanRunArgs`` (bbox + mode + boundary forcing).
        dem_uri: optional topo/bathy DEM ``s3://`` URI. When ``None`` the composer
            fetches it (``fetch_topobathy`` -> ``fetch_dem`` fallback). Tests pass
            a synthetic URI to skip the fetch.
        run_id: optional ULID; minted by the staging step if absent.
        compute_class: FR-CE-3 compute class for the Batch sizing.
        cleanup_outputs: when True, the downloaded output dir is removed after
            postprocess (the COGs were already uploaded).

    Returns:
        The PEAK ``WaveFieldLayerURI`` (role ``"primary"``, name ``"Peak wave
        height"``) carrying the four narration scalars + the echoed mode. Per-frame
        Hs layers are emitted out-of-band via the emitter.

    Raises:
        SwanComposerError / SwanWorkflowError / PostprocessSwanError on a fatal
        stage failure (the tool wrapper catches these and returns a typed error
        dict so the agent narrates honestly).
    """
    bbox = tuple(run_args.bbox)
    emitter = current_emitter()

    # --- Zoom-on-area-first: the map zooms before the solve runs. ---
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 -- non-fatal UX hint
            logger.warning("model_wave_scenario: zoom-to emit failed: %s", exc)

    # --- Sub-step plan: fetch DEM -> stage -> solve -> postprocess -> publish. --
    begin_substeps(emitter, 5)

    # --- Step 1: topo/bathy DEM (off-loop blocking I/O) ---------------------
    if dem_uri is None:
        async with substep(emitter, "fetch_topobathy"):
            resolved_dem_uri = await asyncio.to_thread(_fetch_bathy_for_swan, bbox)
    else:
        resolved_dem_uri = dem_uri
    logger.info("model_wave_scenario: DEM=%s", resolved_dem_uri)

    # --- Step 2: stage the build_spec manifest + DEM reference --------------
    async with substep(emitter, "stage_swan_manifest"):
        staging = await asyncio.to_thread(
            stage_swan_manifest,
            run_args,
            dem_uri=resolved_dem_uri,
            run_id=run_id,
            wind_uri=run_args.wind_uri,
        )

    # --- Auto vertical scaling from the mesh cell count ---------------------
    from ..tools.simulation.solver import (
        select_compute_class,
        solve_progress_vcpus,
    )

    n_active = int(getattr(staging, "n_active_cells", 0) or 0)
    if n_active > 0:
        effective_compute_class = select_compute_class(n_active)
    else:
        effective_compute_class = compute_class
    # Deployment-aware CPU count (fingerprint audit A6): local-docker reports
    # the HOST cpu count; aws-batch keeps the tier lookup byte-identical.
    _vcpus = solve_progress_vcpus(effective_compute_class)

    # --- Step 3: dispatch to AWS Batch (the generic run_solver seam) --------
    from ..tools.simulation.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    handle = run_solver(
        solver=SWAN_SOLVER_NAME,
        model_setup_uri=staging.manifest_uri,
        compute_class=effective_compute_class,
    )

    # --- Two-card sim observability (dispatch card + Batch-bound sim card) --
    _sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter,
        solver=SWAN_SOLVER_NAME,
        handle=handle,
        compute_class=effective_compute_class,
    )
    if emitter is not None and _sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))

    _progress_task = asyncio.ensure_future(
        drive_live_solve_progress(
            emitter=current_emitter(),
            run_id=staging.run_id,
            solver=SWAN_SOLVER_NAME,
            grid_resolution_m=None,
            active_cell_count=n_active or None,
            vcpus=int(_vcpus) if _vcpus is not None else None,
            eta_seconds=(n_active * _SWAN_SEC_PER_CELL) if n_active else None,
        )
    )
    run_result = None

    class _SolveReturnedFailed(RuntimeError):
        pass

    try:
        async with substep(emitter, "run_solver"):
            try:
                run_result = await wait_for_completion(handle)
            except asyncio.CancelledError:
                # Invariant 8: propagate the cancel; route it to the SIM card.
                logger.info("model_wave_scenario cancelled while awaiting solver")
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
        # Child already marked red by the substep; fall through to the telemetry +
        # typed-error path (which records + raises SwanWorkflowError).
        pass

    await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    # --- SOLVE telemetry (Batch instance + size + timing) ------------------
    try:
        _record_swan_batch_solve_telemetry(
            run_result=run_result,
            handle=handle,
            staging=staging,
            compute_class=effective_compute_class,
        )
    except Exception as exc:  # noqa: BLE001 -- never break the solve
        logger.warning("SWAN solve batch-compute telemetry failed (non-fatal): %s", exc)

    if run_result.status != "complete":
        raise SwanWorkflowError(
            "SWAN_RUN_FAILED",
            message=(
                "SWAN Batch solve did not complete "
                f"(status={run_result.status}, "
                f"error_code={getattr(run_result, 'error_code', None)}): "
                f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}"
            ),
            details={
                "run_id": staging.run_id,
                "output_uri": getattr(run_result, "output_uri", None),
            },
        )

    # --- Postprocess-offload branch (Phase 4): worker-written manifest -------
    # When the SWAN Batch worker rebuilds with the raster-postprocess offload it
    # runs the heavy .mat -> COG conversion ITSELF (display-ready overview-bearing
    # COGs) and writes a thin typed publish_manifest.json (pointed to by
    # completion.json.publish_manifest_uri). ``read_publish_manifest`` reads +
    # SCHEMA-GATEs it; a present, schema_version==1 manifest activates the
    # REGISTER-ONLY path below - SHORT-CIRCUITing the on-box heavy tail entirely
    # (NO _download_batch_swan_outputs, NO postprocess_swan, NO _ensure_raster_
    # has_overviews). The publish-or-honest-drop gate (TRID3NT_TILE_SERVER_BASE) +
    # the render-chokepoint registration are preserved per layer.
    #
    # ONE-RELEASE SAFETY: manifest absent OR unknown schema_version ->
    # ``read_publish_manifest`` returns None and the EXISTING on-box path below
    # runs unchanged (the raw swan_out.mat is still uploaded). Clean if/else.
    # (The SWAN worker does NOT emit a manifest yet, so today this always falls
    # back; the branch is forward-ready for when the SWAN worker side lands.)
    manifest = await asyncio.to_thread(read_publish_manifest, run_result)
    if manifest is not None:
        logger.info(
            "model_wave_scenario: REGISTER-ONLY path (worker postprocess offload) "
            "run_id=%s engine=%s layers=%d",
            staging.run_id, manifest.engine, len(manifest.layers),
        )
        async with substep(emitter, "publish_layer"):
            wave_layers, _top_metrics, _dropped = await asyncio.to_thread(
                register_swan_wave_layers,
                manifest,
                run_id=staging.run_id,
                mode=run_args.mode,
                bbox=bbox,
            )
        if not wave_layers:
            raise SwanComposerError(
                "SWAN_NO_LAYERS",
                "publish manifest produced no renderable wave layers "
                "(no tile server configured, or empty manifest).",
            )
        peak = wave_layers[0]
        frame_layers = wave_layers[1:]
        emitted_frames = await _emit_frame_layers(
            emitter, frame_layers, staging.run_id
        )
        logger.info(
            "model_wave_scenario complete (register-only) run_id=%s mode=%s "
            "max_hs_m=%.4g frames_emitted=%d/%d peak_uri=%s",
            staging.run_id, run_args.mode, peak.max_hs_m,
            emitted_frames, len(frame_layers), peak.uri,
        )
        if emitter is not None:
            try:
                await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "model_wave_scenario: register-only zoom-to failed: %s", exc
                )
        return peak

    # --- Step 4: download the Batch SWAN output (ON-BOX FALLBACK) ----------
    batch_run_id = getattr(run_result, "run_id", None) or staging.run_id
    out_dir = await asyncio.to_thread(_download_batch_swan_outputs, batch_run_id)

    try:
        # --- Step 5: postprocess (rasterize Hs -> peak + frames) -----------
        async with substep(emitter, "postprocess_swan"):
            layers, metrics = await asyncio.to_thread(
                postprocess_swan,
                out_dir,
                bbox,
                run_id=staging.run_id,
                mode=run_args.mode,
            )
    finally:
        if cleanup_outputs:
            _cleanup_dir(out_dir)

    if not layers:
        raise SwanComposerError(
            "SWAN_NO_LAYERS",
            "postprocess_swan produced no wave layers (empty solve?)",
        )

    raw_peak = layers[0]
    frame_layers = layers[1:]

    # --- Step 6: publish the PEAK COG through publish_layer (render chokepoint)
    async with substep(emitter, "publish_layer"):
        peak = await asyncio.to_thread(_publish_peak_layer, raw_peak, staging.run_id)

    # --- Step 6b: publish + emit the per-frame animation layers OUT-OF-BAND --
    emitted_frames = await _emit_frame_layers(emitter, frame_layers, staging.run_id)

    logger.info(
        "model_wave_scenario complete run_id=%s mode=%s max_hs_m=%.4g "
        "mean_tp_s=%.4g mean_dir_deg=%.1f wave_area_km2=%.6g "
        "frames_emitted=%d/%d peak_uri=%s",
        staging.run_id,
        run_args.mode,
        peak.max_hs_m,
        peak.mean_tp_s,
        peak.mean_dir_deg,
        peak.wave_area_km2,
        emitted_frames,
        len(frame_layers),
        peak.uri,
    )

    # --- AUTHORITATIVE LAST zoom-to ----------------------------------------
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("model_wave_scenario: authoritative zoom-to failed: %s", exc)

    return peak


def _publish_peak_layer(
    raw_peak: WaveFieldLayerURI, run_id: str
) -> WaveFieldLayerURI:
    """Publish the PEAK Hs COG through publish_layer (render chokepoint).

    Routes the raw s3:// peak COG through ``publish_layer`` and returns a NEW
    ``WaveFieldLayerURI`` carrying the published /tiles or WMS URL plus the
    narration scalars. On publish failure the raw peak is returned UNCHANGED: the
    dispatch-level ``emit_layer_uri`` guardrail then drops the dead raw-s3:// raster
    from the map (honest) while the typed metrics still narrate. Mirrors the
    GeoClaw/SFINCS primary-publish path.
    """
    if raw_peak.layer_type != "raster" or not (
        raw_peak.uri.startswith("gs://") or raw_peak.uri.startswith("s3://")
    ):
        return raw_peak
    layer_id_for_pub = f"swan-wave-height-peak-{run_id}"
    try:
        published_uri = publish_layer(
            layer_uri=raw_peak.uri,
            layer_id=layer_id_for_pub,
            style_preset=raw_peak.style_preset or SWAN_WAVE_HEIGHT_STYLE_PRESET,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_wave_scenario: publish_layer FAILED for the peak layer_id=%s "
            "error_code=%s (%s) - returning the unpublished peak.",
            layer_id_for_pub,
            exc.error_code,
            exc,
        )
        return raw_peak
    return WaveFieldLayerURI(
        layer_id=layer_id_for_pub,
        name=raw_peak.name,
        layer_type=raw_peak.layer_type,
        uri=published_uri,
        style_preset=raw_peak.style_preset or SWAN_WAVE_HEIGHT_STYLE_PRESET,
        role=raw_peak.role,
        units=raw_peak.units,
        bbox=raw_peak.bbox,
        max_hs_m=raw_peak.max_hs_m,
        mean_tp_s=raw_peak.mean_tp_s,
        mean_dir_deg=raw_peak.mean_dir_deg,
        wave_area_km2=raw_peak.wave_area_km2,
        mode=raw_peak.mode,
    )


async def _emit_frame_layers(
    emitter: Any, frame_layers: list[WaveFieldLayerURI], run_id: str
) -> int:
    """Publish + emit per-frame Hs COGs out-of-band so the web scrubber forms.

    Each frame COG is routed through ``publish_layer`` (render chokepoint) so it
    carries a renderable URL before ``add_loaded_layer``. The "Wave height step N"
    name token is preserved so the web ``detectSequentialGroups`` groups them. A
    frame that fails to publish is HONESTLY DROPPED. Returns the number emitted
    (0 when no emitter is bound). Never raises (mirrors GeoClaw).
    """
    if not frame_layers or emitter is None:
        if frame_layers:
            logger.info(
                "model_wave_scenario: %d animation frames available but no emitter "
                "bound - frames not emitted.",
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
                    style_preset=lyr.style_preset or SWAN_WAVE_HEIGHT_STYLE_PRESET,
                )
            except PublishLayerError as exc:
                logger.warning(
                    "model_wave_scenario: publish_layer FAILED for frame "
                    "layer_id=%s error_code=%s (%s) - dropping this frame.",
                    lyr.layer_id,
                    exc.error_code,
                    exc,
                )
                continue
            emit_layer = WaveFieldLayerURI(
                layer_id=lyr.layer_id,
                name=lyr.name,
                layer_type=lyr.layer_type,
                uri=frame_uri,
                style_preset=lyr.style_preset or SWAN_WAVE_HEIGHT_STYLE_PRESET,
                role=lyr.role,
                units=lyr.units,
                bbox=lyr.bbox,
                max_hs_m=lyr.max_hs_m,
                mean_tp_s=lyr.mean_tp_s,
                mean_dir_deg=lyr.mean_dir_deg,
                wave_area_km2=lyr.wave_area_km2,
                mode=lyr.mode,
            )
        try:
            safe = emit_layer_uri(emit_layer)
            if safe is not None:
                await emitter.add_loaded_layer(safe)
                emitted += 1
        except Exception as exc:  # noqa: BLE001 -- never break the solve
            logger.warning(
                "model_wave_scenario: frame add_loaded_layer failed for %s: %s",
                emit_layer.layer_id,
                exc,
            )
    if emitted:
        logger.info(
            "model_wave_scenario: emitted %d/%d animation frames as a sequential "
            "group (run_id=%s)",
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


def _download_batch_swan_outputs(run_id: str) -> str:
    """Download the Batch SWAN output to a tmp dir for postprocess.

    The SWAN Batch worker uploads its ``swan_out.mat`` (+ PRINT / Errfile
    diagnostics) under ``s3://<runs_bucket>/<run_id>/`` and records their URIs in
    completion.json ``output_uris``. We re-read completion.json (small, already on
    S3) to find the output keys, download them via the SAME boto3 client the solver
    dispatch uses (no new client), and return the local dir holding the output.

    Raises:
        SwanWorkflowError("SWAN_BATCH_OUTPUT_MISSING"): the completed run produced
            no downloadable swan_out.mat (a 'complete' solve with no wave output is
            a real failure -- never a silent dead-end).
    """
    from ..tools.simulation.solver import (
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
            if base.endswith(".mat") or base in {"PRINT", "Errfile", "deck_manifest.json"}:
                keys.append(key)
    if not keys:
        # Defensive fallback: list the runs prefix for the .mat output.
        try:
            resp = s3.list_objects_v2(Bucket=runs_bucket, Prefix=f"{run_id}/")
            for obj in resp.get("Contents", []) or []:
                k = obj.get("Key", "")
                base = k.rsplit("/", 1)[-1]
                if base.endswith(".mat") or base in {"PRINT", "Errfile"}:
                    keys.append(k)
        except Exception as exc:  # noqa: BLE001
            logger.warning("SWAN output list fallback failed: %s", exc)

    tmp_dir = tempfile.mkdtemp(prefix=f"swan-batch-out-{run_id}-")
    out_sub = Path(tmp_dir)

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
                "SWAN Batch output download failed s3://%s/%s: %s",
                runs_bucket,
                key,
                exc,
            )

    has_mat = any(
        p.suffix.lower() == ".mat" for p in out_sub.iterdir() if p.is_file()
    )
    if not has_mat:
        _cleanup_dir(tmp_dir)
        raise SwanWorkflowError(
            "SWAN_BATCH_OUTPUT_MISSING",
            message=(
                f"SWAN Batch run {run_id} completed but produced no downloadable "
                f"swan_out.mat under s3://{runs_bucket}/{run_id}/ "
                f"(downloaded {downloaded} output objects)"
            ),
            details={"run_id": run_id, "runs_bucket": runs_bucket},
        )

    return tmp_dir
