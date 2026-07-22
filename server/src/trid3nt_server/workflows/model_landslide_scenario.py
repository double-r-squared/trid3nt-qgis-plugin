"""Landlab landslide-susceptibility composer (sprint-17 — NEW engine).

The Landlab analogue of ``model_urban_flood_swmm`` (SWMM) /
``model_groundwater_contamination_scenario`` (MODFLOW). A deterministic
orchestrator-style workflow (Invariant 2 — no LLM in the chain) that composes the
Landlab surface-process engine end-to-end:

    fetch DEM (fetch_3dep_extra 1 m -> fetch_dem 10 m fallback)
      -> stage_landlab_manifest (DEM COG + build_spec -> S3)
      -> run_solver('landlab')  (AWS Batch — the scale-to-zero island)
      -> wait_for_completion    (the shared S3 completion poll)
      -> download the field COG + read the worker's typed `result` block
      -> postprocess_landlab    (field COG -> EPSG:4326 susceptibility COG)
      -> publish the primary COG through publish_layer (the render chokepoint).

Returns the primary ``LandlabSusceptibilityLayerURI`` directly (a ``LayerURI``
subtype) so the ``emit_tool_call`` ``add_loaded_layer`` gate fires on it —
exactly like ``run_modflow_job`` returns a ``PlumeLayerURI`` and
``run_swmm_urban_flood`` returns a ``SWMMDepthLayerURI``.

Determinism boundary (Invariant 1): every number the agent narrates comes from
the typed ``LandlabSusceptibilityLayerURI.unstable_area_fraction`` /
``.min_factor_of_safety`` / ``.mean_probability_of_failure`` fields the worker /
postprocess computed — never free-generated.

Landlab runs OFF-BOX ONLY (the scale-to-zero island norm) — there is no
in-process lane, so this composer always dispatches through the generic
``run_solver`` / ``wait_for_completion`` Batch seam (the SAME seam SFINCS uses).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts.landlab_contracts import (
    LandlabRunArgs,
    LandlabSusceptibilityLayerURI,
)

from ..pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from ..tools.publish_layer import PublishLayerError, publish_layer
from .postprocess_landlab import (
    LANDSLIDE_STYLE_PRESET,
    postprocess_landlab,
)
from .run_landlab import (
    LANDLAB_SOLVER_NAME,
    LandlabStaging,
    LandlabWorkflowError,
    stage_landlab_manifest,
)

logger = logging.getLogger("trid3nt_server.workflows.model_landslide_scenario")

__all__ = [
    "model_landslide_scenario",
    "LandslideWorkflowError",
]

#: Minimum landslide AOI side length (m). A geocoded single-feature bbox can be a
#: few metres across; a landslide-susceptibility scenario needs at least a
#: hillslope. Below this the bbox is EXPANDED (centred) to this side length. A
#: normal hillslope/catchment AOI is far above this, so this is a no-op except on
#: a collapsed bbox. Floor only; never shrinks. (Mirrors the SWMM AOI floor.)
_MIN_LANDSLIDE_AOI_SIDE_M: float = 500.0


class LandslideWorkflowError(RuntimeError):
    """Raised on a fatal composer failure (carries an open-set ``error_code``)."""

    error_code: str = "LANDSLIDE_WORKFLOW_FAILED"

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _enforce_min_landslide_aoi(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Expand a too-small AOI bbox to a sensible landslide minimum, centred.

    Floors BOTH side lengths to ``_MIN_LANDSLIDE_AOI_SIDE_M`` metres about the
    bbox centroid (lon scaled by cos(lat)). Returns the bbox UNCHANGED when both
    sides already meet the floor. Never shrinks. Mirrors
    ``model_urban_flood_swmm._enforce_min_urban_aoi``.
    """
    import math

    min_lon, min_lat, max_lon, max_lat = bbox
    cen_lat = 0.5 * (min_lat + max_lat)
    cen_lon = 0.5 * (min_lon + max_lon)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(math.cos(math.radians(cen_lat)), 1e-6)
    width_m = (max_lon - min_lon) * m_per_deg_lon
    height_m = (max_lat - min_lat) * m_per_deg_lat
    if width_m >= _MIN_LANDSLIDE_AOI_SIDE_M and height_m >= _MIN_LANDSLIDE_AOI_SIDE_M:
        return bbox
    half_lon = 0.5 * max(width_m, _MIN_LANDSLIDE_AOI_SIDE_M) / m_per_deg_lon
    half_lat = 0.5 * max(height_m, _MIN_LANDSLIDE_AOI_SIDE_M) / m_per_deg_lat
    expanded = (
        cen_lon - half_lon,
        cen_lat - half_lat,
        cen_lon + half_lon,
        cen_lat + half_lat,
    )
    logger.info(
        "model_landslide_scenario: AOI floor applied - input bbox %s was "
        "%.0fm x %.0fm (below the %.0fm minimum); expanded to %s",
        bbox,
        width_m,
        height_m,
        _MIN_LANDSLIDE_AOI_SIDE_M,
        expanded,
    )
    return expanded


def _localize_to_dem_path(uri: str) -> str:
    """Resolve a DEM ``LayerURI.uri`` (s3:// / file:// / local) to an on-disk
    GeoTIFF path the staging upload can read.

    Mirrors ``model_urban_flood_swmm._localize_to_dem_path``: ``s3://`` objects
    are staged down to a temp file via boto3; ``file://`` + bare local paths pass
    through. On a synthetic / test path the URI is already local.
    """
    if uri.startswith("file://"):
        return uri[len("file://"):]
    if not uri.startswith("s3://"):
        return uri

    import hashlib

    cache_dir = Path(tempfile.gettempdir()) / "trid3nt-landlab-dem-stage"
    cache_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(uri).suffix or ".tif"
    local = cache_dir / (hashlib.sha256(uri.encode()).hexdigest()[:24] + suffix)
    if local.exists() and local.stat().st_size > 0:
        return str(local)
    tmp = local.with_suffix(local.suffix + ".part")
    from ..tools.solver import _get_s3_client

    bucket_name, _, obj_key = uri[len("s3://"):].partition("/")
    resp = _get_s3_client().get_object(Bucket=bucket_name, Key=obj_key)
    with tmp.open("wb") as fh:
        shutil.copyfileobj(resp["Body"], fh)
    os.replace(tmp, local)
    logger.info("staged DEM %s -> %s (%d bytes)", uri, local, local.stat().st_size)
    return str(local)


def _fetch_dem_for_landslide(
    bbox: tuple[float, float, float, float],
) -> tuple[str, str]:
    """Fetch a DEM for the AOI: ``fetch_3dep_extra`` 1 m primary -> ``fetch_dem``
    10 m fallback (the data-source fallback norm). Returns ``(local_dem_path,
    source_label)``; raises ``LandslideWorkflowError("LANDLAB_DEM_FETCH_FAILED")``
    only when BOTH fail."""
    from ..tools.data_fetch import fetch_dem
    from ..tools.fetch_3dep_extra import fetch_3dep_extra

    try:
        layer = fetch_3dep_extra(bbox, resolution="1 meter")
        return _localize_to_dem_path(layer.uri), "USGS 3DEP 1m LiDAR"
    except Exception as exc:  # noqa: BLE001 — fall through to the 10 m fallback
        logger.info(
            "fetch_3dep_extra(1m) failed (%s); falling back to fetch_dem(10m)", exc
        )

    try:
        layer = fetch_dem(bbox, resolution_m=10)
        return _localize_to_dem_path(layer.uri), "USGS 3DEP 10m"
    except Exception as exc:  # noqa: BLE001
        raise LandslideWorkflowError(
            "LANDLAB_DEM_FETCH_FAILED",
            f"both DEM sources failed for bbox {bbox}: 3DEP-1m + fetch_dem-10m: {exc}",
        ) from exc


def _download_batch_landlab_outputs(
    run_result: Any, run_id: str
) -> tuple[str, dict[str, Any], str, dict[str, str]]:
    """Download the Batch field COG + read the worker's typed ``result`` block.

    The Landlab worker uploads ``landlab_field.tif`` under
    ``s3://<runs_bucket>/<run_id>/`` and records the field URI + the typed
    ``result`` block in completion.json. We re-read completion.json (small,
    already on S3) to find the field key + the result block, download the COG via
    the SAME boto3 client the solver dispatch uses, and return ``(local_cog,
    result_block, tmp_dir, secondary_local_by_token)``.

    levers STEP 3: the worker also writes per-secondary-field COGs
    (``landlab_secondary_<token>.tif``) and records them in
    ``result.secondary_field_files`` (token -> filename). We download each that
    is present and return ``secondary_local_by_token`` (token -> local path) so
    the composer can publish the additional quantities. A missing/undownloadable
    secondary COG is skipped (never sinks the primary).

    Raises ``LandlabWorkflowError("LANDLAB_BATCH_OUTPUT_MISSING")`` when a
    'complete' run produced no downloadable field COG (a real failure, never a
    silent dead-end).
    """
    from ..tools.solver import (
        _get_runs_bucket,
        _get_s3_client,
        _split_object_uri,
        _try_get_completion_s3,
    )

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()

    field_keys: list[str] = []
    result_block: dict[str, Any] = {}
    manifest = _try_get_completion_s3(runs_bucket, run_id)
    if isinstance(manifest, dict):
        res = manifest.get("result")
        if isinstance(res, dict):
            result_block = res
        for raw in manifest.get("output_uris") or []:
            uri = str(raw)
            try:
                _scheme, _bucket, key = _split_object_uri(uri)
            except Exception:  # noqa: BLE001 — skip an unparseable entry
                continue
            if key.endswith(".tif"):
                field_keys.append(key)
    if not field_keys:
        field_keys = [f"{run_id}/landlab_field.tif"]

    tmp_dir = tempfile.mkdtemp(prefix=f"landlab-batch-out-{run_id}-")

    def _download(key: str) -> str | None:
        dest = Path(tmp_dir) / Path(key).name
        try:
            resp = s3.get_object(Bucket=runs_bucket, Key=key)
            with dest.open("wb") as fh:
                shutil.copyfileobj(resp["Body"], fh)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Landlab Batch output download failed s3://%s/%s: %s",
                runs_bucket,
                key,
                exc,
            )
            return None
        return str(dest)

    local_field = next((p for p in (_download(k) for k in field_keys) if p), None)
    if local_field is None:
        _cleanup_dir(tmp_dir)
        raise LandlabWorkflowError(
            "LANDLAB_BATCH_OUTPUT_MISSING",
            message=(
                f"Landlab Batch run {run_id} completed but produced no "
                f"downloadable field COG under s3://{runs_bucket}/{run_id}/ "
                f"(looked for {field_keys!r})"
            ),
            details={"run_id": run_id, "runs_bucket": runs_bucket},
        )

    # levers STEP 3: download the secondary-field COGs the worker recorded.
    secondary_local_by_token: dict[str, str] = {}
    sec_files = result_block.get("secondary_field_files")
    if isinstance(sec_files, dict):
        for token, fname in sec_files.items():
            key = f"{run_id}/{fname}"
            local = _download(key)
            if local is not None:
                secondary_local_by_token[str(token)] = local

    return local_field, result_block, tmp_dir, secondary_local_by_token


def _cleanup_dir(path: str) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


async def model_landslide_scenario(
    run_args: LandlabRunArgs,
    *,
    dem_path: str | None = None,
    run_id: str | None = None,
    compute_class: str = "standard",
) -> LandlabSusceptibilityLayerURI:
    """Compose the full Landlab surface-process chain end-to-end (OFF-BOX lane).

    Args:
        run_args: the validated ``LandlabRunArgs`` (bbox + analysis + soil /
            rainfall parameters).
        dem_path: optional on-disk DEM path. When ``None`` the composer fetches
            it (``fetch_3dep_extra`` 1 m -> ``fetch_dem`` 10 m fallback) from
            ``run_args.bbox``. Tests pass a synthetic GeoTIFF to skip the fetch.
        run_id: optional ULID; minted by ``new_ulid`` if absent.
        compute_class: FR-CE-3 compute class for the Batch dispatch.

    Returns:
        The primary ``LandlabSusceptibilityLayerURI`` (role ``"primary"``)
        carrying the three narration scalars.

    Raises:
        LandslideWorkflowError / LandlabWorkflowError / PostprocessLandlabError on
        a fatal stage failure (the tool wrapper catches these and returns a typed
        error dict so the agent narrates honestly).
    """
    from ..tools.solver import (
        EmitterBinding,
        new_ulid,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    bbox = _enforce_min_landslide_aoi(tuple(run_args.bbox))
    emitter = current_emitter()
    rid = run_id or new_ulid()

    # --- Zoom-on-area-first: the map zooms before the solve runs. ---
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
            logger.warning("model_landslide_scenario: zoom-to emit failed: %s", exc)

    # --- Declare the planned child count for the live breadcrumb (task-168) -
    # The composer's user-meaningful internal operations surfaced as nested
    # child rows: (fetch_dem if not supplied) -> stage_landlab_manifest ->
    # run_solver (the Batch solve) -> download_landlab_outputs ->
    # postprocess_landlab -> publish_layer. The DEM fetch only runs when no
    # dem_path was supplied, so the planned count adjusts. No-op when no emitter
    # is bound (verify/CI direct-call path).
    begin_substeps(current_emitter(), 6 if dem_path is None else 5)

    # --- Step 1: DEM (1 m 3DEP primary -> 10 m fallback) --------------------
    # Off the loop (sync blocking I/O) per the no-sync-blocking norm.
    if dem_path is None:
        async with substep(current_emitter(), "fetch_dem"):
            local_dem_path, dem_source = await asyncio.to_thread(
                _fetch_dem_for_landslide, bbox
            )
    else:
        local_dem_path, dem_source = dem_path, "supplied"
    logger.info("model_landslide_scenario: DEM=%s (%s)", local_dem_path, dem_source)

    # --- Step 2: stage the DEM + build_spec manifest to S3 ------------------
    async with substep(current_emitter(), "stage_landlab_manifest"):
        staging: LandlabStaging = await asyncio.to_thread(
            stage_landlab_manifest, run_args, dem_path=local_dem_path, run_id=rid
        )

    # --- Step 3: dispatch through the generic Batch seam --------------------
    # Surface the dispatch + Batch wait as a single "run_solver" child row; the
    # live Batch readout stays owned by the two-card Sim observability
    # (mint_dispatch_and_sim_cards) which is PRESERVED as-is.
    async with substep(current_emitter(), "run_solver"):
        handle = run_solver(
            solver=LANDLAB_SOLVER_NAME,
            model_setup_uri=staging.manifest_uri,
            compute_class=compute_class,
        )
        # --- Two-card sim observability (mirror the SWMM off-box lane) ------
        _sim_step_id = await mint_dispatch_and_sim_cards(
            emitter=emitter,
            solver=LANDLAB_SOLVER_NAME,
            handle=handle,
            compute_class=compute_class,
        )
        if emitter is not None and _sim_step_id is not None:
            set_emitter_binding(EmitterBinding(emitter=emitter, step_id=_sim_step_id))

        try:
            run_result = await wait_for_completion(handle)
        except asyncio.CancelledError:
            logger.info("model_landslide_scenario cancelled while awaiting solver")
            await route_sim_terminal(emitter, _sim_step_id, run_result=None)
            raise
        finally:
            set_emitter_binding(None)

        await route_sim_terminal(emitter, _sim_step_id, run_result=run_result)

    if run_result.status != "complete":
        raise LandlabWorkflowError(
            "LANDLAB_RUN_FAILED",
            message=(
                "Landlab Batch solve did not complete "
                f"(status={run_result.status}, "
                f"error_code={getattr(run_result, 'error_code', None)}): "
                f"{getattr(run_result, 'error_message', '') or ''}"
            ),
            details={
                "run_id": rid,
                "output_uri": getattr(run_result, "output_uri", None),
            },
        )

    # --- Register-only branch (worker postprocess offload) -------------------
    # If the worker wrote a publish_manifest.json (schema_version==1), read +
    # schema-gate it and SHORT-CIRCUIT the on-box heavy tail (no download, no
    # postprocess_landlab). Degrades cleanly to the legacy on-box path when
    # absent (pre-rebuild worker image) or schema unknown.
    from .register_published_manifest import (
        read_publish_manifest,
        register_manifest_layers,
    )

    batch_run_id = getattr(run_result, "run_id", None) or rid
    _manifest = await asyncio.to_thread(read_publish_manifest, run_result)
    if _manifest is not None:
        logger.info(
            "model_landslide_scenario: REGISTER-ONLY path (worker postprocess "
            "offload) run_id=%s engine=%s layers=%d",
            batch_run_id, _manifest.engine, len(_manifest.layers),
        )
        async with substep(current_emitter(), "publish_layer"):
            _reg = register_manifest_layers(
                _manifest, run_id=batch_run_id, bbox=tuple(bbox)
            )
        _primary_layers = [lyr for lyr in _reg.layers if lyr.role == "primary"]
        _frame_layers = [lyr for lyr in _reg.layers if lyr.role != "primary"]
        if _frame_layers and emitter is not None:
            for _lyr in _frame_layers:
                try:
                    await emitter.add_loaded_layer(_lyr)
                except Exception:  # noqa: BLE001
                    pass
        if not _primary_layers:
            raise LandslideWorkflowError(
                "LANDLAB_NO_LAYERS",
                "worker publish_manifest produced no primary layer (empty solve?)",
            )
        _prim = _primary_layers[0]
        _m = _reg.metrics
        _typed_primary = LandlabSusceptibilityLayerURI(
            uri=_prim.uri,
            layer_type=_prim.layer_type,
            layer_id=_prim.layer_id,
            name=_prim.name,
            style_preset=_prim.style_preset,
            bbox=tuple(bbox),
            role=_prim.role,
            unstable_area_fraction=float(_m.get("unstable_area_fraction", 0.0)),
            min_factor_of_safety=float(_m.get("min_factor_of_safety", 0.0)),
            mean_probability_of_failure=float(_m.get("mean_probability_of_failure", 0.0)),
        )
        if emitter is not None:
            try:
                await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "model_landslide_scenario: authoritative zoom-to emit failed: %s", exc
                )
        return _typed_primary

    # --- Step 4: download the field COG + read the worker result block ------
    async with substep(current_emitter(), "download_landlab_outputs"):
        (
            local_field,
            result_block,
            batch_out_dir,
            secondary_cogs,
        ) = await asyncio.to_thread(
            _download_batch_landlab_outputs, run_result, batch_run_id
        )

    # --- Step 5: postprocess (field COG -> EPSG:4326 susceptibility COG) ----
    try:
        async with substep(current_emitter(), "postprocess_landlab"):
            layers, metrics = await asyncio.to_thread(
                postprocess_landlab,
                local_field,
                run_id=rid,
                analysis=run_args.analysis,
                result=result_block,
            )

        # levers STEP 3 (gated): ALSO publish the secondary fields (drainage
        # area / slope / relative wetness / discharge / factor-of-safety) as
        # context layers. Non-fatal -- a failure never sinks the primary.
        import os as _os

        if secondary_cogs and _os.environ.get(
            "TRID3NT_LANDLAB_REGISTRY_QUANTITIES", ""
        ).lower() in ("1", "true", "on", "yes"):
            try:
                from .postprocess_landlab import publish_landlab_quantities
                from .register_published_manifest import register_manifest_layers

                reg = await asyncio.to_thread(
                    lambda: publish_landlab_quantities(
                        secondary_cogs,
                        run_id=rid,
                        register_manifest_layers=register_manifest_layers,
                        bbox=tuple(bbox),
                    )
                )
                emitter_now = current_emitter()
                if emitter_now is not None and reg is not None:
                    for extra_layer in getattr(reg, "layers", []) or []:
                        try:
                            await emitter_now.add_loaded_layer(extra_layer)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("could not add landlab registry layer: %s", exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "model_landslide_scenario registry-quantity publish failed "
                    "(non-fatal): %s",
                    exc,
                )
    finally:
        _cleanup_dir(batch_out_dir)

    if not layers:
        raise LandslideWorkflowError(
            "LANDLAB_NO_LAYERS",
            "postprocess_landlab produced no susceptibility layer (empty solve?)",
        )

    raw_primary = layers[0]

    # --- Step 6: publish the primary COG through publish_layer (render chokepoint)
    async with substep(current_emitter(), "publish_layer"):
        primary = await asyncio.to_thread(_publish_primary_layer, raw_primary, rid)

    # Stamp the returned layer's bbox to the floored AOI (the authoritative AOI).
    if tuple(primary.bbox or ()) != tuple(bbox):
        primary = primary.model_copy(update={"bbox": tuple(bbox)})

    logger.info(
        "model_landslide_scenario complete run_id=%s analysis=%s "
        "unstable_frac=%.4f min_fos=%.4f mean_pof=%.4f uri=%s",
        rid,
        run_args.analysis,
        primary.unstable_area_fraction,
        primary.min_factor_of_safety,
        primary.mean_probability_of_failure,
        primary.uri,
    )

    # --- Authoritative LAST zoom-to (supersede any early geocode snap) ------
    if emitter is not None:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(bbox)})
        except Exception as exc:  # noqa: BLE001 - non-fatal UX hint
            logger.warning(
                "model_landslide_scenario: authoritative zoom-to emit failed: %s",
                exc,
            )

    return primary


def _publish_primary_layer(
    raw_primary: LandlabSusceptibilityLayerURI, run_id: str
) -> LandlabSusceptibilityLayerURI:
    """Publish the primary susceptibility COG through publish_layer.

    Routes the raw s3:// COG through ``publish_layer`` (the
    ``_resolve_titiler_style_params`` render seam) and returns a NEW
    ``LandlabSusceptibilityLayerURI`` carrying the published /tiles or WMS URL
    plus the narration scalars. On publish failure the raw layer is returned
    UNCHANGED: the dispatch-level ``emit_layer_uri`` guardrail then drops the
    dead raw-s3:// raster from the map (honest) while the typed metrics still
    narrate. Mirrors ``model_urban_flood_swmm._publish_peak_layer``.
    """
    if raw_primary.layer_type != "raster" or not (
        raw_primary.uri.startswith("gs://") or raw_primary.uri.startswith("s3://")
    ):
        return raw_primary
    layer_id_for_pub = f"landlab-susceptibility-{run_id}"
    style = raw_primary.style_preset or LANDSLIDE_STYLE_PRESET
    try:
        published_uri = publish_layer(
            layer_uri=raw_primary.uri,
            layer_id=layer_id_for_pub,
            style_preset=style,
        )
    except PublishLayerError as exc:
        logger.warning(
            "model_landslide_scenario: publish_layer FAILED for the primary "
            "layer_id=%s error_code=%s (%s) - returning the unpublished layer. "
            "The narration scalars still surface honestly.",
            layer_id_for_pub,
            exc.error_code,
            exc,
        )
        return raw_primary
    return LandlabSusceptibilityLayerURI(
        layer_id=layer_id_for_pub,
        name=raw_primary.name,
        layer_type=raw_primary.layer_type,
        uri=published_uri,
        style_preset=style,
        role=raw_primary.role,
        units=raw_primary.units,
        bbox=raw_primary.bbox,
        unstable_area_fraction=raw_primary.unstable_area_fraction,
        min_factor_of_safety=raw_primary.min_factor_of_safety,
        mean_probability_of_failure=raw_primary.mean_probability_of_failure,
    )
