"""Engine template ``elmfire_fire_spread`` - ELMFIRE wildfire-spread engine
(engine-door refactor - ELMFIRE slice; was ``model_fire_spread``).

The LLM-facing exposure of the ELMFIRE level-set fire-spread engine.
``elmfire_fire_spread(...)`` takes the AOI + a REQUIRED ignition point + the
scenario weather dial, runs the deterministic fetch -> deck-build -> solve ->
postprocess chain (``model_elmfire_fire_spread`` below, in this module), and
returns a ``FireSpreadLayerURI`` the emitter loads onto the map (it subclasses
``LayerURI`` so the ``emit_tool_call`` ``add_loaded_layer`` gate fires).

This is the fire analogue of ``geoclaw_inundation`` (GeoClaw) /
``sfincs_flood`` (SFINCS) / ``openquake_psha`` (OpenQuake). It is a registered
engine TEMPLATE tagged ``engine="elmfire", tier="template"`` - EXCLUDED from the
default retrieval pool and surfaced only by the ``run_elmfire`` door's gate
expansion (SELECT-THEN-CALL). Like the other templates it declares
``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` (workflow exposure surface;
never touches the cache shim). Confirmation before consequence (Invariant 9 -
a solver run) is enforced by the server solver-confirm gate around this template
(``SOLVER_CONFIRM_TOOLS`` keys on ``elmfire_fire_spread``): the user sees the
cell count + estimated runtime before the solve dispatches.

Determinism boundary (Invariant 1): every number the agent narrates
(``burned_area_km2`` / ``fire_arrival_max_hr`` / flame length / spread rate)
comes from the typed ``FireSpreadLayerURI`` fields the postprocess computed -
never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.elmfire_contracts import (
    DEFAULT_FIRE_WIND_DIR_DEG,
    DEFAULT_FIRE_WIND_SPEED_MPH,
    ELMFIRE_TOA_STYLE_PRESET,
    ElmfireRunArgs,
    FireSpreadLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata, GateSpec

from trid3nt_server.data import register_tool
from trid3nt_server.data.publish_layer.publish_layer import PublishLayerError, publish_layer
from trid3nt_server.workflows.elmfire._frame_emit import (
    read_and_emit_elmfire_frames,
)
from trid3nt_server.workflows.elmfire._template_card import TemplateCard
from trid3nt_server.workflows.elmfire.postprocess_elmfire import (
    PostprocessElmfireError,
    postprocess_elmfire,
)
from trid3nt_server.workflows.elmfire.run_elmfire import (
    ELMFIRE_SOLVER_NAME,
    ElmfireWorkflowError,
    build_elmfire_deck,
    estimate_elmfire_runtime_s,
    fetch_elmfire_inputs,
    stage_elmfire_manifest,
)
from trid3nt_server.workflows.shared.solve_progress import drive_live_solve_progress
from trid3nt_server.emission.layer_uri_emit import emit_layer_uri
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)

logger = logging.getLogger("trid3nt_server.workflows.elmfire.fire_spread.fire_spread")

__all__ = ["elmfire_fire_spread", "model_elmfire_fire_spread", "FireSpreadComposerError"]


#: Curated door-listing card (the run_elmfire door prefers this over signature
#: derivation). One-line question + the real required inputs + a knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "wildfire spread from a point ignition - where a fire burns in the next "
        "hours (fire-arrival time + burned extent + flame length) over LANDFIRE "
        "fuels/terrain, and wind/fuel-moisture what-ifs"
    ),
    required_inputs=["bbox", "ignition_lonlat"],
    knobs=(
        "wind_speed_mph, wind_dir_deg, fuel_moisture (dry/moderate/moist), "
        "duration_hours, cellsize_m"
    ),
)


_ELMFIRE_FIRE_SPREAD_METADATA = AtomicToolMetadata(
    name="elmfire_fire_spread",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="elmfire",
    gate_spec=GateSpec(
        kind="solver",
        estimate_provider="trid3nt_server.gates.cards.solver_confirm:estimate_fire",
        title="ELMFIRE fire spread",
        rationale="A consequential ELMFIRE solve: confirm before the run.",
    ),
    tier="template",
)


@register_tool(
    _ELMFIRE_FIRE_SPREAD_METADATA,
    # readOnlyHint=False (runs a solver writing output COG artifacts),
    # openWorldHint=False (LANDFIRE/3DEP fetches go through the cache shim
    # tools; the solve itself is a local container / intra-cloud Batch task),
    # destructiveHint=False (writes go to a new runs/ prefix),
    # idempotentHint=False (each call mints a new run_id + COG keys).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def elmfire_fire_spread(
    bbox: tuple[float, float, float, float] | list[float] | str | None = None,
    ignition_lonlat: tuple[float, float] | list[float] | None = None,
    wind_speed_mph: float = 15.0,
    wind_dir_deg: float = 0.0,
    fuel_moisture: str = "dry",
    duration_hours: float = 6.0,
    cellsize_m: float = 30.0,
    compute_class: str = "standard",
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders). Also absorbs the
    # server confirm gate's injected ``confirmed=True``.
    **_extra_ignored: Any,
) -> FireSpreadLayerURI | dict[str, Any]:
    """Run an ELMFIRE wildfire-spread simulation from a point ignition.

    Fidelity: ELMFIRE level-set wildfire-spread over LANDFIRE 30 m fuels;
    CONUS-only, county-scale; the ignition point must come from the user;
    planning-grade perimeter-growth envelope, not an operational fire forecast.
    Off-scope: post-fire debris-flow hazard -> model_debris_flow; observed
    satellite fire animation (not a spread solve) -> fetch_goes_animation /
    fetch_viirs_day_fire.

    Use this when: the user wants to MODEL/SIMULATE/FORECAST wildfire
    spread from a specific ignition ("if a fire started here, where does
    it spread in 6 hours?") or explore wind/fuel-moisture what-ifs over
    LANDFIRE 30m fuels+terrain. Do NOT use for: observed fire
    perimeters/detections (``fetch_nifc_fire_perimeters``/
    ``fetch_firms_active_fire``/``fetch_goes_active_fire``); satellite
    animations of a real event (``fetch_goes_animation``/
    ``fetch_viirs_day_fire``); post-fire debris-flow (``model_debris_flow``);
    past burn severity (``fetch_mtbs_burn_severity``).

    IGNITION POINT IS REQUIRED -- NEVER GUESS IT. If not given, ask the
    user or call ``request_spatial_input(mode="point")`` and pass the
    returned coordinates as ``ignition_lonlat``.

    Params:
        bbox: simulation AOI, EPSG:4326. CONUS-only (LANDFIRE coverage);
            county-scale or smaller (fetch capped ~123km).
        ignition_lonlat: REQUIRED (lon, lat) inside bbox.
        wind_speed_mph: constant wind speed (ELMFIRE 20ft convention,
            default 15).
        wind_dir_deg: direction wind blows FROM, meteorological deg
            [0,360] (default 0).
        fuel_moisture: ``"dry"`` (default, critical fire weather),
            ``"moderate"``, or ``"moist"`` (marginal burning).
        duration_hours: burn duration (>0, <=48, default 6); also
            animation frame count.
        cellsize_m: computational cell size (default 30, LANDFIRE native).
        compute_class: compute class (default "standard").

    Returns:
        On success: ``FireSpreadLayerURI`` -- fire-arrival-time COG,
        hourly burned-extent scrubber animation, flame-length/spread-rate
        layers, with ``burned_area_km2``, ``fire_arrival_max_hr``,
        ``max_flame_length_m``, ``max_spread_rate_m_min``.
        On failure: ``{"status": "error", "error_code", "error_message"}``
        -- notably ``FIRE_IGNITION_REQUIRED`` or ``ELMFIRE_NO_SPREAD``
        (nonburnable fuels at ignition). Not cached
        (``cacheable=False``).
    """
    # --- ignition: REQUIRED, never fabricated --------------------------------
    # All SHAPE handling (string "lon,lat" / dict ignition, string / reordered
    # / point-collapsed / missing bbox deriving a ~5 km domain) lives in
    # ElmfireRunArgs' before-validators - the wrapper no longer pre-validates
    # (its old manual checks rejected the very shapes the contract coerces;
    # observed live 2026-07-08).
    if ignition_lonlat is None:
        return {
            "status": "error",
            "error_code": "FIRE_IGNITION_REQUIRED",
            "error_message": (
                "elmfire_fire_spread requires an ignition point "
                "(ignition_lonlat=[lon, lat]) and it must come from the USER. "
                "Do NOT invent one: ask the user where the fire starts, or "
                "call request_spatial_input(mode='point') so they click the "
                "ignition point on the map, then pass the returned "
                "coordinates as ignition_lonlat."
            ),
        }

    try:
        run_args = ElmfireRunArgs(
            bbox=bbox,  # type: ignore[arg-type]
            ignition_lonlat=ignition_lonlat,  # type: ignore[arg-type]
            wind_speed_mph=float(wind_speed_mph),
            wind_dir_deg=float(wind_dir_deg),
            fuel_moisture=fuel_moisture,  # type: ignore[arg-type]
            duration_hours=float(duration_hours),
            cellsize_m=float(cellsize_m),
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError / coercion
        return {
            "status": "error",
            "error_code": "FIRE_PARAMS_INVALID",
            "error_message": f"invalid fire-spread run arguments: {exc}",
        }

    logger.info(
        "elmfire_fire_spread bbox=%s ignition=%s wind=%.1fmph@%.0fdeg "
        "moisture=%s duration=%.1fh cellsize=%.0fm",
        run_args.bbox,
        run_args.ignition_lonlat,
        run_args.wind_speed_mph,
        run_args.wind_dir_deg,
        run_args.fuel_moisture,
        run_args.duration_hours,
        run_args.cellsize_m,
    )

    try:
        primary = await model_elmfire_fire_spread(
            run_args, compute_class=compute_class
        )
        logger.info(
            "elmfire_fire_spread complete layer_id=%s burned_area_km2=%.4g "
            "arrival_max_hr=%.3g uri=%s",
            primary.layer_id,
            primary.burned_area_km2,
            primary.fire_arrival_max_hr,
            primary.uri,
        )
        return primary
    except asyncio.CancelledError:
        raise
    except (
        ElmfireWorkflowError,
        PostprocessElmfireError,
        FireSpreadComposerError,
    ) as exc:
        logger.warning("elmfire_fire_spread failed: %s (%s)", exc.error_code, exc)
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("elmfire_fire_spread unexpected failure")
        return {
            "status": "error",
            "error_code": "FIRE_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# The composer.
# A deterministic orchestrator-style chain (Invariant 2 - no LLM in the chain):
#   fetch LANDFIRE fuels (fbfm40/cbh/cbd/cc/ch) + DEM + derived slope/aspect
#     -> deck builder (same-grid EPSG:5070 deck + elmfire.data)
#     -> stage manifest -> run_solver('elmfire') -> wait_for_completion
#     -> download the solver's .bil outputs
#     -> postprocess_elmfire (CRS stamp -> ToA COG + hourly burned-extent
#        animation frames + flame-length/spread-rate COGs)
#     -> publish the primary through publish_layer (render chokepoint) + emit
#        the frames/aux layers out-of-band (the Phase-1 scrubber group).
# Determinism boundary (Invariant 1): every narrated number comes from the
# typed postprocess fields - never free-generated.
# --------------------------------------------------------------------------- #
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
    from trid3nt_server.telemetry import record_solve_telemetry

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
    rundir (``TRID3NT_RUNS_DIR/<run_id>``) still holds ``outputs/`` on this
    machine - postprocess reads it in place (``is_temp=False``: never deleted
    here; the rundir is the run's artifact dir).

    Otherwise (Batch, or a foreign rundir) the completed run's outputs are
    downloaded from the runs bucket (completion.json ``output_uris``, the same
    client the dispatch used) into a temp dir (``is_temp=True``).

    Raises ``ElmfireWorkflowError("ELMFIRE_OUTPUT_MISSING")`` when a
    'complete' run yields no downloadable raster (never a silent dead-end).
    """
    from trid3nt_server.data.simulation.solver.solver import (
        DEFAULT_LOCAL_RUNS_DIR,
        _get_runs_bucket,
        _get_s3_client,
        _split_object_uri,
        _try_get_completion_s3,
    )

    runs_dir = Path(os.environ.get("TRID3NT_RUNS_DIR") or DEFAULT_LOCAL_RUNS_DIR)
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
            "model_elmfire_fire_spread: publish_layer FAILED for the primary "
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
                "model_elmfire_fire_spread: %d secondary layers available "
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
                    "model_elmfire_fire_spread: publish_layer FAILED for "
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
                "model_elmfire_fire_spread: add_loaded_layer failed for %s: %s",
                emit_layer.layer_id, exc,
            )
    if emitted:
        logger.info(
            "model_elmfire_fire_spread: emitted %d/%d secondary layers "
            "(run_id=%s)",
            emitted, len(layers), run_id,
        )
    return emitted


async def model_elmfire_fire_spread(
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
        compute_class: compute class; auto-scaled UP from the deck
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
                "model_elmfire_fire_spread: zoom-to emit failed: %s", exc
            )

    # Sub-step plan: fetch inputs -> build deck -> stage -> solve -> postprocess.
    begin_substeps(emitter, 5)

    # --- Step 1: the 8 fuels/topography rasters (off-loop blocking I/O). -----
    async with substep(emitter, "fetch_elmfire_inputs"):
        inputs = await asyncio.to_thread(fetch_elmfire_inputs, bbox)

    # --- Step 2: the same-grid deck (off-loop warping + writes). ------
    deck_dir = tempfile.mkdtemp(prefix="elmfire-deck-")
    try:
        async with substep(emitter, "build_elmfire_deck"):
            deck_manifest = await asyncio.to_thread(
                build_elmfire_deck, run_args, inputs, deck_dir
            )

        grid = deck_manifest.get("grid") or {}
        logger.info(
            "model_elmfire_fire_spread: deck ready grid=EPSG:%s %sx%s @%sm "
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

        # ONE local compute environment: the solve runs on the host CPUs, so the
        # caller's compute_class flows through unchanged (no auto-scaling).
        n_cells = int(staging.n_cells or 0)
        effective_compute_class = compute_class

        # --- Step 4: dispatch via the generic run_solver seam. ----------------
        from trid3nt_server.data.simulation.solver.solver import (
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
                vcpus=os.cpu_count(),
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
                        "model_elmfire_fire_spread cancelled awaiting solver"
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
                    write_frames_manifest=True,
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

    # --- fire-weather provenance (law 9, audit row 34): wind + fuel moisture
    # DRIVE the entire spread; they rode SILENT. Surface them as the scenario
    # what-if levers they are (the audit's borderline resolution -> scenario, no
    # refuse: a fire-weather regime is the user's question). A RAWS / gridMET /
    # HRRR fire-weather fetch is the queued real-source upgrade.
    _fire_weather = [
        SyntheticInput(
            param="wind_speed_mph", value=round(float(run_args.wind_speed_mph), 1),
            units="mph",
            basis="default_demo" if float(run_args.wind_speed_mph) == DEFAULT_FIRE_WIND_SPEED_MPH else "user",
            consequence="scenario",
            note="sustained 20-ft driving wind (fire-weather scenario lever, not a "
                 "forecast; RAWS/gridMET/HRRR fetch queued)",
        ),
        SyntheticInput(
            param="wind_dir_deg", value=round(float(run_args.wind_dir_deg), 1),
            units="deg",
            basis="default_demo" if float(run_args.wind_dir_deg) == DEFAULT_FIRE_WIND_DIR_DEG else "user",
            consequence="scenario",
            note="direction the wind blows FROM (meteorological); scenario lever",
        ),
        SyntheticInput(
            param="fuel_moisture", value=str(run_args.fuel_moisture),
            basis="default_demo" if str(run_args.fuel_moisture) == "dry" else "user",
            consequence="scenario",
            note="dead/live fuel-moisture regime (dry = critical fire weather); scenario lever",
        ),
    ]
    primary = primary.model_copy(update={"synthetic_inputs": _fire_weather})

    # --- Publish + emit aux context COGs out-of-band. ----------------------
    emitted = await _emit_secondary_layers(emitter, secondary, staging.run_id)

    # --- Burned-extent animation: read the seam frames + emit (ADR 0288). ---
    # postprocess wrote the hourly ToA-threshold frames to outputs.json under the
    # SOLVE run prefix (write_frames_manifest=True); the seam owns the temporal
    # group, the typed peak above stays composer-built. Best-effort: a frame
    # miss never sinks the peak.
    frames_emitted = await read_and_emit_elmfire_frames(
        emitter, run_id=solve_run_id, bbox=bbox
    )

    logger.info(
        "model_elmfire_fire_spread complete run_id=%s burned_area_km2=%.4g "
        "arrival_max_hr=%.3g flame_max_m=%s spread_max_m_min=%s "
        "aux_emitted=%d/%d frames_emitted=%d primary_uri=%s",
        staging.run_id,
        primary.burned_area_km2,
        primary.fire_arrival_max_hr,
        primary.max_flame_length_m,
        primary.max_spread_rate_m_min,
        emitted,
        len(secondary),
        frames_emitted,
        primary.uri,
    )

    # --- AUTHORITATIVE LAST zoom-to. -----------------------------------------
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "model_elmfire_fire_spread: authoritative zoom-to failed: %s",
                exc,
            )

    return primary
