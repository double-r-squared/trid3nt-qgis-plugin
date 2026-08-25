"""The OPEN-WATER front of the TELEMAC AOI templates: stage, solve, read, surface.

The reach family (``steps/reach.py`` + ``steps/deck.py`` + ``steps/solve.py``)
meshes a corridor along a flowline. The other TELEMAC domains - a coastal strip,
a lake fetch, a harbour basin - are all the same shape instead: a regular grid
over an AOI, real topobathy at the nodes, one worker section in the manifest, one
result SELAFIN, one peak field. Four templates were each carrying their own copy
of that: their own ``_stage_<x>_manifest``, ``_download_<x>_result``, their own
run_solver + progress + terminal-card dance, their own bed-input surfacing.

This module is that one copy. What varies between the four - which manifest
section, which solver, which result file, which outputs the supervisor uploads -
is DATA the deck writer returns, not code paths here: a deck says what solves it.

Everything past the primary layer is best-effort by contract, exactly as in the
reach family: a missing bed COG or an unpublished mesh never voids a solve.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid

from trid3nt_server.workflows.lib import DeclarativeError, Step

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.open_water")

__all__ = [
    "OpenWaterError",
    "SolveOpenWater",
    "download_open_water_result",
    "publish_peak_layer",
    "solve_open_water",
    "stage_open_water_manifest",
    "surface_in_worker_bed_input",
]

_STEPS = "trid3nt_server.workflows.telemac.steps"

#: Wall-clock ceiling on one open-water solve. These domains are a few thousand
#: grid nodes over hours of simulated time - the reach family's node-budget
#: estimator has no corridor geometry to work from here, so the bound is a flat
#: one rather than a fake calculation.
_SOLVE_TIMEOUT_S = 3600.0


class OpenWaterError(DeclarativeError):
    """An open-water TELEMAC domain could not be staged, solved or read."""

    error_code = "TELEMAC_OPEN_WATER_FAILED"


def stage_open_water_manifest(*, section: str, config: dict[str, Any],
                              run_tag: str, outputs: list[str],
                              prefix: str | None = None) -> str:
    """Write the worker manifest to the cache bucket and return its ``s3://`` URI.

    ``section`` is the key the worker's ENTRYPOINT dispatches on (``coastal``,
    ``wave``, ``agitation``, ``stratified``). ``prefix`` is where the manifest is
    STAGED, and it is not always the same word - the wave module answers to
    ``wave`` inside the document while its manifests live under ``tomawac/``, and
    the harbour module to ``agitation`` under ``artemis/``. Collapsing the two
    into one name is how a manifest lands somewhere the worker looks and carries a
    key it does not read, which is a silent fall-through to a different pipeline
    rather than an error. The rest of the envelope - ``run_id``, an empty
    ``inputs`` because these pipelines self-fetch, an empty ``telemac_args``
    because the image CMD drives the entrypoint - is the same for all of them.
    """
    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise OpenWaterError(
            "TRID3NT_CACHE_BUCKET must be set to stage the TELEMAC manifest.",
            error_code="TELEMAC_STAGING_FAILED")
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    manifest = {section: config, "run_id": run_tag, "inputs": [],
                "telemac_args": [], "outputs": list(outputs)}
    key = f"{prefix or section}/{run_tag}/manifest.json"
    _get_s3_client().put_object(
        Bucket=cache_bucket, Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json")
    return f"s3://{cache_bucket}/{key}"


async def solve_open_water(*, deck: dict[str, Any],
                           compute_class: str = "medium") -> dict[str, Any]:
    """Stage the deck's manifest, dispatch it, wait, and return the run handle.

    The deck carries its own solver, section, outputs and result file, so this is
    the ONE dispatch for every open-water TELEMAC domain. The returned ``uri`` is
    the result SELAFIN under the run prefix - what a ledger replay probes, so a
    resumed rerun can only skip the solve while the solved artifact is still there.
    """
    from trid3nt_server.emission.pipeline_emitter import (
        current_emitter,
        mint_dispatch_and_sim_cards,
        route_sim_terminal,
    )
    from trid3nt_server.workflows.shared.solve_progress import drive_live_solve_progress
    from trid3nt_server.workflows.solver.solver import (
        EmitterBinding,
        _get_runs_bucket,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

    solver, section = deck["solver"], deck["section"]
    run_tag = deck["run_tag"]
    manifest_uri = await asyncio.to_thread(
        stage_open_water_manifest, section=section, config=deck["config"],
        run_tag=run_tag, outputs=deck["outputs"], prefix=deck.get("prefix"))
    logger.info("telemac %s staged manifest run_tag=%s name=%s -> %s",
                section, run_tag, deck["config"].get("name"), manifest_uri)

    emitter = current_emitter()
    handle = run_solver(solver=solver, model_setup_uri=manifest_uri,
                        compute_class=compute_class)
    run_id = handle.run_id
    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=solver, handle=handle, compute_class=compute_class)
    if emitter is not None and sim_step_id is not None:
        set_emitter_binding(EmitterBinding(emitter=emitter, step_id=sim_step_id))
    progress = asyncio.ensure_future(drive_live_solve_progress(
        emitter=emitter, run_id=run_id, solver=solver,
        grid_resolution_m=deck.get("mesh_size_m"), active_cell_count=None,
        vcpus=None, eta_seconds=None))

    run_result = None
    try:
        run_result = await wait_for_completion(handle, timeout_s=_SOLVE_TIMEOUT_S)
    except asyncio.CancelledError:
        logger.info("telemac %s solve cancelled awaiting solver", section)
        await route_sim_terminal(emitter, sim_step_id, run_result=None)
        raise
    finally:
        progress.cancel()
        try:
            await progress
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        set_emitter_binding(None)
    await route_sim_terminal(emitter, sim_step_id, run_result=run_result)

    batch_run_id = getattr(run_result, "run_id", None) or run_id
    if run_result is None or run_result.status != "complete":
        raise OpenWaterError(
            f"the TELEMAC {section} solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or ''}",
            error_code=deck.get("run_failed_code") or "TELEMAC_OPEN_WATER_FAILED")

    metrics = await asyncio.to_thread(_read_run_metrics, batch_run_id)
    if metrics.get("utm_epsg") is None:
        # A SELAFIN carries no CRS of its own, so without the worker's UTM zone
        # the result cannot be georeferenced at all - a typed refusal, never a
        # guessed zone.
        raise OpenWaterError(
            f"TELEMAC {section} run {batch_run_id} produced no utm_epsg; "
            "the result cannot be georeferenced.",
            error_code=deck.get("output_missing_code") or "TELEMAC_OUTPUT_MISSING")
    return {
        "run_id": batch_run_id,
        "uri": f"s3://{_get_runs_bucket()}/{batch_run_id}/{deck['result_basename']}",
        "utm_epsg": int(metrics["utm_epsg"]),
        "metrics": metrics,
    }


def _read_run_metrics(run_id: str) -> dict[str, Any]:
    """Best-effort read of ``<run_id>/telemac_metrics.json``; ``{}`` on any miss."""
    from trid3nt_server.workflows.solver.solver import _get_runs_bucket, _get_s3_client

    try:
        obj = _get_s3_client().get_object(
            Bucket=_get_runs_bucket(), Key=f"{run_id}/telemac_metrics.json")
        loaded = json.loads(obj["Body"].read().decode("utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:  # noqa: BLE001 - absence is a fact, not a crash
        logger.info("telemac: run metrics read miss for %s: %s", run_id, exc)
        return {}


def download_open_water_result(run_id: str, basename: str,
                               *, error_code: str = "TELEMAC_OUTPUT_MISSING") -> str:
    """Download the run's result SELAFIN to a local path the postprocess can read."""
    from trid3nt_server.workflows.solver.solver import _get_runs_bucket, _get_s3_client

    runs_bucket = _get_runs_bucket()
    local = str(Path(tempfile.mkdtemp(prefix=f"telemac-{run_id}-")) / basename)
    try:
        body = _get_s3_client().get_object(
            Bucket=runs_bucket, Key=f"{run_id}/{basename}")["Body"].read()
        with open(local, "wb") as fh:
            fh.write(body)
    except Exception as exc:  # noqa: BLE001
        raise OpenWaterError(
            f"TELEMAC run {run_id} completed but s3://{runs_bucket}/{run_id}/"
            f"{basename} was not downloadable: {exc}", error_code=error_code) from exc
    return local


async def surface_in_worker_bed_input(emitter: Any, *, run_metrics: dict[str, Any],
                                      run_id: str, name: str,
                                      layer_id_prefix: str) -> bool:
    """BEST-EFFORT: surface an in-worker-sampled bed COG as a role=context input.

    The emit-on-fetch seam surfaces every AGENT-SIDE router fetch of renderable
    data, but a bed sampled INSIDE a solver container never touches the router.
    The worker writes the bed it actually solved on beside the result and records
    the key; this rides that existing object (NO re-upload). NEVER raises - a
    missing bed COG must not void a solve.
    """
    bed_cog = (run_metrics or {}).get("bed_cog")
    if emitter is None or not bed_cog:
        return False
    try:
        from trid3nt_server.emission.layer_uri_emit import publish_raster_input_cog
        from trid3nt_server.workflows.solver.solver import _get_runs_bucket

        return await publish_raster_input_cog(
            emitter, cog_uri=f"s3://{_get_runs_bucket()}/{run_id}/{bed_cog}",
            layer_id=f"{layer_id_prefix}-{new_ulid()}", name=name,
            style_preset="continuous_dem", role="context")
    except Exception as exc:  # noqa: BLE001 - input surfacing is NEVER fatal
        logger.warning("telemac bed input absent (the solve is unaffected): %s", exc)
        return False


async def publish_peak_layer(raw: Any, *, style_preset: str,
                             update: dict[str, Any]) -> Any:
    """Style the peak COG through the ONE publish seam and fold the narration on.

    On a publish failure the RAW layer is returned enriched but unpublished: its
    object-store COG still lets the case find the SELAFIN sibling, and retracting
    a solved result over a styling miss would be the failure-retracts-something
    anti-pattern.
    """
    from trid3nt_server.tools.publish_layer.publish_layer import (
        PublishLayerError,
        publish_layer,
    )

    if not str(getattr(raw, "uri", "")).startswith(("s3://", "gs://")):
        return raw.model_copy(update=update)
    try:
        published_uri = await asyncio.to_thread(
            publish_layer, layer_uri=raw.uri, layer_id=raw.layer_id,
            style_preset=raw.style_preset or style_preset)
    except PublishLayerError as exc:
        logger.warning("telemac publish_layer failed (%s) - unpublished COG", exc)
        return raw.model_copy(update=update)
    return raw.model_copy(update={"uri": published_uri, **update})


class SolveOpenWater:
    """The open-water solve step. The plan's consequential node."""

    @staticmethod
    def telemac(*, deck: Any, compute_class: Any) -> Step:
        """Dispatch a staged open-water deck to its own TELEMAC worker."""
        return Step(runner=f"{_STEPS}.open_water.solve_open_water", stage="solve",
                    kwargs={"deck": deck, "compute_class": compute_class},
                    consequential=True)
