"""ELMFIRE wildfire-spread composer (FIRE-3).

The fire analogue of ``model_dambreak_geoclaw_scenario`` (GeoClaw) /
``model_flood_scenario`` (SFINCS). A deterministic orchestrator-style workflow
(Invariant 2 — no LLM in the chain) that composes the ELMFIRE engine
end-to-end:

    fetch LANDFIRE fuels (fbfm40/cbh/cbd/cc/ch) + DEM + derived slope/aspect
      -> FIRE-2 deck builder (same-grid EPSG:5070 deck + elmfire.data)
      -> stage manifest -> run_solver('elmfire') -> wait_for_completion
         (local-docker: the FIRE-1 proven trid3nt/elmfire:dev image;
          aws-batch: the FIRE-4 seam, inert until the job def exists)
      -> download the solver's .bil outputs
      -> postprocess_elmfire (CRS stamp -> ToA COG + hourly burned-extent
         animation frames + flame-length/spread-rate COGs)
      -> publish the primary through publish_layer (render chokepoint) + emit
         the frames/aux layers out-of-band (the Phase-1 scrubber group).

Returns the PRIMARY ``FireSpreadLayerURI`` directly (a ``LayerURI`` subtype)
so the ``emit_tool_call`` ``add_loaded_layer`` gate fires on it.

Determinism boundary (Invariant 1): every number the agent narrates
(``burned_area_km2`` / ``fire_arrival_max_hr`` / ``max_flame_length_m`` /
``max_spread_rate_m_min``) comes from the typed postprocess fields — never
free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from grace2_contracts.elmfire_contracts import (
    ELMFIRE_TOA_STYLE_PRESET,
    ElmfireRunArgs,
    FireSpreadLayerURI,
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
from .postprocess_elmfire import PostprocessElmfireError, postprocess_elmfire
from .run_elmfire import (
    ELMFIRE_SOLVER_NAME,
    ElmfireWorkflowError,
    build_elmfire_deck,
    estimate_elmfire_runtime_s,
    fetch_elmfire_inputs,
    stage_elmfire_manifest,
)
from .solve_progress import drive_live_solve_progress

logger = logging.getLogger("grace2_agent.workflows.model_fire_spread_scenario")

__all__ = ["model_fire_spread_scenario", "FireSpreadComposerError"]


class FireSpreadComposerError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _record_elmfire_solve_telemetry(
    *,
    run_result: Any,
    handle: Any,
    staging: Any,
    compute_class: str,
) -> dict | None:
    """Record ONE SOLVE row for the ELMFIRE lane (mirrors the GeoClaw sibling).

    Best-effort; returns the recorded row or ``None``.
    """
    from ..telemetry import record_solve_telemetry

    meta = getattr(run_result, "batch_compute_meta", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    row: dict = {
        "run_id": getattr(run_result, "run_id", None) or staging.run_id,
        "solver": ELMFIRE_SOLVER_NAME,
        "status": getattr(run_result, "status", None),
        "backend": str(getattr(handle, "workflow_name", "") or "unknown"),
        "compute_class": compute_class,
        "active_cell_count": int(getattr(staging, "n_cells", 0) or 0),
        "scenario": "fire_spread",
    }
    row.update(meta)
    return record_solve_telemetry(row)


def _cleanup_dir(d: str | Path) -> None:
    """Best-effort removal of a scratch dir."""
    try:
        shutil.rmtree(Path(d), ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _download_elmfire_outputs(run_id: str) -> tuple[str, bool]:
    """Materialize the solver's ``outputs/`` locally; return ``(dir, is_temp)``.

    LOCAL FAST-PATH: under the ``local-docker`` backend the supervisor's
    rundir (``GRACE2_RUNS_DIR/<run_id>``) still holds ``outputs/`` on this
    machine — postprocess reads it in place (``is_temp=False``: never deleted
    here; the rundir is the run's artifact dir).

    Otherwise (Batch, or a foreign rundir) the completed run's outputs are
    downloaded from the runs bucket (completion.json ``output_uris``, the same
    client the dispatch used) into a temp dir (``is_temp=True``).

    Raises ``ElmfireWorkflowError("ELMFIRE_OUTPUT_MISSING")`` when a
    'complete' run yields no downloadable raster (never a silent dead-end).
    """
    from ..tools.solver import (
        DEFAULT_LOCAL_RUNS_DIR,
        _get_runs_bucket,
        _get_s3_client,
        _split_object_uri,
        _try_get_completion_s3,
    )

    runs_dir = Path(os.environ.get("GRACE2_RUNS_DIR") or DEFAULT_LOCAL_RUNS_DIR)
    local_out = runs_dir / run_id / "outputs"
    if local_out.is_dir() and any(local_out.iterdir()):
        return str(runs_dir / run_id), False

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()
    keys: list[str] = []
    manifest = _try_get_completion_s3(runs_bucket, run_id)
    if isinstance(manifest, dict):
        for raw in manifest.get("output_uris") or []:
            try:
                _scheme, _bucket, key = _split_object_uri(str(raw))
            except Exception:  # noqa: BLE001
                continue
            if "/outputs/" in f"/{key}":
                keys.append(key)
    if not keys:
        try:
            resp = s3.list_objects_v2(
                Bucket=runs_bucket, Prefix=f"{run_id}/outputs/"
            )
            keys = [
                obj.get("Key", "")
                for obj in (resp.get("Contents") or [])
                if obj.get("Key")
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("ELMFIRE output list fallback failed: %s", exc)

    tmp_dir = tempfile.mkdtemp(prefix=f"elmfire-out-{run_id}-")
    out_sub = Path(tmp_dir) / "outputs"
    out_sub.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    for key in keys:
        dest = out_sub / key.rsplit("/", 1)[-1]
        try:
            resp = s3.get_object(Bucket=runs_bucket, Key=key)
            with dest.open("wb") as fh:
                shutil.copyfileobj(resp["Body"], fh)
            downloaded += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ELMFIRE output download failed s3://%s/%s: %s",
                runs_bucket, key, exc,
            )

    has_raster = any(
        p.suffix.lower() in (".bil", ".tif") for p in out_sub.iterdir()
    )
    if not has_raster:
        _cleanup_dir(tmp_dir)
        raise ElmfireWorkflowError(
            "ELMFIRE_OUTPUT_MISSING",
            f"ELMFIRE run {run_id} completed but produced no downloadable "
            f"raster under s3://{runs_bucket}/{run_id}/outputs/ "
            f"(downloaded {downloaded} objects)",
            details={"run_id": run_id, "runs_bucket": runs_bucket},
        )
    return tmp_dir, True


def _publish_primary_layer(
    raw: FireSpreadLayerURI, run_id: str
) -> FireSpreadLayerURI:
    """Publish the PRIMARY ToA COG through publish_layer (render chokepoint).

    On publish failure the raw layer is returned UNCHANGED: the dispatch-level
    ``emit_layer_uri`` guardrail drops a dead raw-s3:// raster from the map
    (honest) while the typed metrics still narrate. Mirrors the GeoClaw
    ``_publish_peak_layer``.
    """
    if raw.layer_type != "raster" or not (
        raw.uri.startswith("gs://") or raw.uri.startswith("s3://")
    ):
        return raw
    try:
        published_uri = publish_layer(
            layer_uri=raw.uri,
            layer_id=raw.layer_id,
            style_preset=raw.style_preset or ELMFIRE_TOA_STYLE_PRESET,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_fire_spread_scenario: publish_layer FAILED for the primary "
            "layer_id=%s error_code=%s (%s) - returning the unpublished layer.",
            raw.layer_id, exc.error_code, exc,
        )
        return raw
    return raw.model_copy(update={"uri": published_uri})


async def _emit_secondary_layers(
    emitter: Any, layers: list[FireSpreadLayerURI], run_id: str
) -> int:
    """Publish + emit the frame/aux layers out-of-band (scrubber group forms).

    Each COG routes through ``publish_layer`` so it carries a renderable URL
    before ``add_loaded_layer``; a layer that fails to publish is HONESTLY
    DROPPED. Returns the number emitted (0 when no emitter). Never raises.
    """
    if not layers or emitter is None:
        if layers:
            logger.info(
                "model_fire_spread_scenario: %d secondary layers available "
                "but no emitter bound - not emitted.",
                len(layers),
            )
        return 0
    emitted = 0
    for lyr in layers:
        if lyr.uri.startswith("gs://") or lyr.uri.startswith("s3://"):
            try:
                pub_uri = await asyncio.to_thread(
                    publish_layer,
                    layer_uri=lyr.uri,
                    layer_id=lyr.layer_id,
                    style_preset=lyr.style_preset or ELMFIRE_TOA_STYLE_PRESET,
                )
            except PublishLayerError as exc:
                logger.warning(
                    "model_fire_spread_scenario: publish_layer FAILED for "
                    "layer_id=%s error_code=%s (%s) - dropping this layer.",
                    lyr.layer_id, exc.error_code, exc,
                )
                continue
            emit_layer: FireSpreadLayerURI = lyr.model_copy(
                update={"uri": pub_uri}
            )
        else:
            emit_layer = lyr
        try:
            safe = emit_layer_uri(emit_layer)
            if safe is not None:
                await emitter.add_loaded_layer(safe)
                emitted += 1
        except Exception as exc:  # noqa: BLE001 - never break the solve
            logger.warning(
                "model_fire_spread_scenario: add_loaded_layer failed for %s: %s",
                emit_layer.layer_id, exc,
            )
    if emitted:
        logger.info(
            "model_fire_spread_scenario: emitted %d/%d secondary layers "
            "(run_id=%s)",
            emitted, len(layers), run_id,
        )
    return emitted


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
async def model_fire_spread_scenario(
    run_args: ElmfireRunArgs,
    *,
    run_id: str | None = None,
    compute_class: str = "standard",
    cleanup_outputs: bool = True,
) -> FireSpreadLayerURI:
    """Compose the full ELMFIRE fire-spread chain end-to-end.

    Args:
        run_args: the validated ``ElmfireRunArgs`` (AOI + ignition + scenario
            weather + duration). The ignition point is REQUIRED by contract.
        run_id: optional pre-minted ULID (minted at staging when absent).
        compute_class: FR-CE-3 compute class; auto-scaled UP from the deck
            cell count, never silently downgraded below the caller's choice.
        cleanup_outputs: when True the temp deck dir + any temp download dir
            are removed after postprocess (COGs already uploaded). A LOCAL
            rundir is never deleted (it is the run's artifact dir).

    Returns:
        The PRIMARY ``FireSpreadLayerURI`` (role ``"primary"``, name ``"Fire
        arrival time"``) carrying the typed narration scalars. Burned-extent
        frames + flame-length/spread-rate layers are emitted out-of-band.

    Raises:
        ElmfireWorkflowError / PostprocessElmfireError / FireSpreadComposerError
        on a fatal stage failure (the tool wrapper catches these and returns a
        typed error dict so the agent narrates honestly).
    """
    bbox = tuple(run_args.bbox)
    emitter = current_emitter()

    # --- Zoom-on-area-first: the map zooms before the solve runs. ---
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
            logger.warning(
                "model_fire_spread_scenario: zoom-to emit failed: %s", exc
            )

    # Sub-step plan: fetch inputs -> build deck -> stage -> solve -> postprocess.
    begin_substeps(emitter, 5)

    # --- Step 1: the 8 fuels/topography rasters (off-loop blocking I/O). -----
    async with substep(emitter, "fetch_elmfire_inputs"):
        inputs = await asyncio.to_thread(fetch_elmfire_inputs, bbox)

    # --- Step 2: the FIRE-2 same-grid deck (off-loop warping + writes). ------
    deck_dir = tempfile.mkdtemp(prefix="elmfire-deck-")
    try:
        async with substep(emitter, "build_elmfire_deck"):
            deck_manifest = await asyncio.to_thread(
                build_elmfire_deck, run_args, inputs, deck_dir
            )

        grid = deck_manifest.get("grid") or {}
        logger.info(
            "model_fire_spread_scenario: deck ready grid=EPSG:%s %sx%s @%sm "
            "ignition=%s wind=%.1fmph@%.0fdeg moisture=%s duration=%.1fh",
            grid.get("epsg"), grid.get("nx"), grid.get("ny"),
            grid.get("cellsize_m"),
            run_args.ignition_lonlat,
            run_args.wind_speed_mph,
            run_args.wind_dir_deg,
            run_args.fuel_moisture,
            run_args.duration_hours,
        )

        # --- Step 3: stage the run_solver manifest. --------------------------
        async with substep(emitter, "stage_elmfire_manifest"):
            staging = await asyncio.to_thread(
                stage_elmfire_manifest,
                deck_dir,
                deck_manifest,
                run_args,
                run_id=run_id,
            )

        # --- Vertical auto-scaling from the deck cell count. -----------------
        from ..tools.solver import (
            select_compute_class,
            solve_progress_vcpus,
        )

        n_cells = int(staging.n_cells or 0)
        auto_class = (
            select_compute_class(n_cells) if n_cells > 0 else compute_class
        )
        _CLASS_RANK = {"small": 0, "standard": 1, "large": 2, "xlarge": 3}
        effective_compute_class = max(
            auto_class, compute_class, key=lambda c: _CLASS_RANK.get(c, 1)
        )
        # Deployment-aware CPU count (fingerprint audit A6): local-docker
        # reports the HOST cpu count; aws-batch keeps the tier lookup
        # byte-identical.
        _vcpus = solve_progress_vcpus(effective_compute_class)

        # --- Step 4: dispatch via the generic run_solver seam. ----------------
        from ..tools.solver import (
            EmitterBinding,
            run_solver,
            set_emitter_binding,
            wait_for_completion,
        )

        handle = run_solver(
            solver=ELMFIRE_SOLVER_NAME,
            model_setup_uri=staging.manifest_uri,
            compute_class=effective_compute_class,
        )

        _sim_step_id = await mint_dispatch_and_sim_cards(
            emitter=emitter,
            solver=ELMFIRE_SOLVER_NAME,
            handle=handle,
            compute_class=effective_compute_class,
        )
        if emitter is not None and _sim_step_id is not None:
            set_emitter_binding(
                EmitterBinding(emitter=emitter, step_id=_sim_step_id)
            )

        duration_s = float(run_args.duration_hours) * 3600.0
        _progress_task = asyncio.ensure_future(
            drive_live_solve_progress(
                emitter=current_emitter(),
                run_id=staging.run_id,
                solver=ELMFIRE_SOLVER_NAME,
                grid_resolution_m=float(run_args.cellsize_m),
                active_cell_count=n_cells or None,
                vcpus=int(_vcpus) if _vcpus is not None else None,
                eta_seconds=estimate_elmfire_runtime_s(n_cells, duration_s),
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
                    # Invariant 8: propagate the cancel; route to the SIM card.
                    logger.info(
                        "model_fire_spread_scenario cancelled awaiting solver"
                    )
                    await route_sim_terminal(
                        emitter, _sim_step_id, run_result=None
                    )
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
            pass  # fall through to the typed-error block below (child is red)

        await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

        try:
            _record_elmfire_solve_telemetry(
                run_result=run_result,
                handle=handle,
                staging=staging,
                compute_class=effective_compute_class,
            )
        except Exception as exc:  # noqa: BLE001 - never break the solve
            logger.warning(
                "ELMFIRE solve telemetry failed (non-fatal): %s", exc
            )

        if run_result.status != "complete":
            raise ElmfireWorkflowError(
                "ELMFIRE_RUN_FAILED",
                "ELMFIRE solve did not complete "
                f"(status={run_result.status}, "
                f"error_code={getattr(run_result, 'error_code', None)}): "
                f"{getattr(run_result, 'error_message', '') or ''}",
                details={"run_id": staging.run_id},
            )

        # --- Step 5: download outputs + postprocess. --------------------------
        solve_run_id = getattr(run_result, "run_id", None) or staging.run_id
        out_dir, out_is_temp = await asyncio.to_thread(
            _download_elmfire_outputs, solve_run_id
        )
        try:
            async with substep(emitter, "postprocess_elmfire"):
                layers, metrics = await asyncio.to_thread(
                    postprocess_elmfire,
                    out_dir,
                    bbox,
                    run_id=solve_run_id,
                    duration_s=duration_s,
                    epsg=int(grid.get("epsg", 5070)),
                    ignition_lonlat=tuple(run_args.ignition_lonlat),
                )
        finally:
            if cleanup_outputs and out_is_temp:
                _cleanup_dir(out_dir)
    finally:
        if cleanup_outputs:
            _cleanup_dir(deck_dir)

    if not layers:
        raise FireSpreadComposerError(
            "ELMFIRE_NO_LAYERS",
            "postprocess_elmfire produced no layers (honesty floor: cannot "
            "narrate an empty solve)",
        )

    raw_primary = layers[0]
    secondary = layers[1:]

    # --- Publish the PRIMARY through publish_layer (render chokepoint). -----
    primary = await asyncio.to_thread(
        _publish_primary_layer, raw_primary, staging.run_id
    )

    # --- Publish + emit frames/aux out-of-band (scrubber group). ------------
    emitted = await _emit_secondary_layers(emitter, secondary, staging.run_id)

    logger.info(
        "model_fire_spread_scenario complete run_id=%s burned_area_km2=%.4g "
        "arrival_max_hr=%.3g flame_max_m=%s spread_max_m_min=%s "
        "secondary_emitted=%d/%d primary_uri=%s",
        staging.run_id,
        primary.burned_area_km2,
        primary.fire_arrival_max_hr,
        primary.max_flame_length_m,
        primary.max_spread_rate_m_min,
        emitted,
        len(secondary),
        primary.uri,
    )

    # --- AUTHORITATIVE LAST zoom-to. -----------------------------------------
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "model_fire_spread_scenario: authoritative zoom-to failed: %s",
                exc,
            )

    return primary
