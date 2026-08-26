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
from typing import Any, Mapping

from trid3nt_contracts import new_ulid

from trid3nt_server.workflows.lib import DeclarativeError, Step

from .solve import read_run_metrics

logger = logging.getLogger("trid3nt_server.workflows.telemac.steps.open_water")

__all__ = [
    "mesh_resolution_label",
    "OpenWaterError",
    "dispatch_and_wait",
    "mesh_sizing_provenance",
    "SolveOpenWater",
    "download_open_water_result",
    "solve_open_water",
    "solved_domain_bbox",
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


async def dispatch_and_wait(*, solver: str, manifest_uri: str, compute_class: str,
                           label: str, timeout_s: float,
                           grid_resolution_m: float | None = None,
                           active_cell_count: int | None = None) -> tuple[Any, str]:
    """Dispatch a staged manifest, drive the cards, wait, and hand back the result.

    The supervision dance every TELEMAC front performs identically: mint the
    dispatch and sim cards, bind the emitter so the worker's own progress reaches
    them, poll to completion, and route the terminal card whichever way the run
    ends - CANCELLED included, which is the clause a hand-copied version drops.
    Returns ``(run_result, batch_run_id)`` and judges nothing: what a non-complete
    status MEANS is the caller's typed error to raise, because the code it carries
    is the caller's contract.
    """
    from trid3nt_server.emission.pipeline_emitter import (
        current_emitter,
        mint_dispatch_and_sim_cards,
        route_sim_terminal,
    )
    from trid3nt_server.workflows.shared.solve_progress import drive_live_solve_progress
    from trid3nt_server.workflows.solver.solver import (
        EmitterBinding,
        run_solver,
        set_emitter_binding,
        wait_for_completion,
    )

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
        grid_resolution_m=grid_resolution_m, active_cell_count=active_cell_count,
        vcpus=None, eta_seconds=None))

    run_result = None
    try:
        run_result = await wait_for_completion(handle, timeout_s=timeout_s)
    except asyncio.CancelledError:
        logger.info("telemac %s solve cancelled awaiting solver", label)
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
    return run_result, (getattr(run_result, "run_id", None) or run_id)


async def solve_open_water(*, deck: dict[str, Any],
                           compute_class: str = "medium") -> dict[str, Any]:
    """Stage the deck's manifest, dispatch it, wait, and return the run handle.

    The deck carries its own solver, section, outputs and result file, so this is
    the ONE dispatch for every open-water TELEMAC domain. The returned ``uri`` is
    the result SELAFIN under the run prefix - what a ledger replay probes, so a
    resumed rerun can only skip the solve while the solved artifact is still there.

    ``deck["requires_utm"]`` says whether a missing ``utm_epsg`` is a FAILURE.
    A domain built over real geography is ungeoreferenceable without the worker's
    zone, so its absence is a typed refusal. An IDEALIZED domain - the analytic
    harbour basin, the Berkhoff shoal, the lock-exchange channel - has no
    geographic footprint at all and legitimately reports no zone; its reader
    rasterizes the local metres in a placeholder frame instead. Refusing there
    refuses a run that is working exactly as designed.
    """
    from trid3nt_server.workflows.solver.solver import _get_runs_bucket

    solver, section = deck["solver"], deck["section"]
    run_tag = deck["run_tag"]
    manifest_uri = await asyncio.to_thread(
        stage_open_water_manifest, section=section, config=deck["config"],
        run_tag=run_tag, outputs=deck["outputs"], prefix=deck.get("prefix"))
    logger.info("telemac %s staged manifest run_tag=%s name=%s -> %s",
                section, run_tag, deck["config"].get("name"), manifest_uri)

    run_result, batch_run_id = await dispatch_and_wait(
        solver=solver, manifest_uri=manifest_uri, compute_class=compute_class,
        label=section, timeout_s=_SOLVE_TIMEOUT_S,
        grid_resolution_m=deck.get("mesh_size_m"))
    if run_result is None or run_result.status != "complete":
        raise OpenWaterError(
            f"the TELEMAC {section} solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or ''}",
            error_code=deck.get("run_failed_code") or "TELEMAC_OPEN_WATER_FAILED")

    metrics = await asyncio.to_thread(read_run_metrics, batch_run_id)
    utm_epsg = metrics.get("utm_epsg")
    if utm_epsg is None and deck.get("requires_utm", True):
        # A SELAFIN carries no CRS of its own, so a domain built over real
        # geography cannot be georeferenced at all without the worker's UTM zone -
        # a typed refusal, never a guessed zone.
        raise OpenWaterError(
            f"TELEMAC {section} run {batch_run_id} produced no utm_epsg; "
            "the result cannot be georeferenced.",
            error_code=deck.get("output_missing_code") or "TELEMAC_OUTPUT_MISSING")
    return {
        "run_id": batch_run_id,
        "uri": f"s3://{_get_runs_bucket()}/{batch_run_id}/{deck['result_basename']}",
        "utm_epsg": int(utm_epsg) if utm_epsg is not None else None,
        "metrics": metrics,
    }


def solved_domain_bbox(deck: Mapping[str, Any],
                       metrics: Mapping[str, Any]) -> tuple[float, ...] | None:
    """The 4326 bbox the WORKER laid its local mesh frame over. ``None`` if none.

    The open-water builds put node 0 at the AOI's SW corner, so the reader has to
    add that exact corner back before reprojecting. "That exact corner" is the
    point: the deck rounds the AOI to 4 decimals on its way into the manifest, so
    the ORIGINAL AOI is a few metres away from the one the worker meshed and
    offsets the whole field by that much. The worker's own echo in
    ``telemac_metrics.json`` is the ground truth; the manifest's rounded bbox is
    what it was handed and is the fallback. The unrounded AOI is neither.

    An IDEALIZED domain has no geographic footprint and reports no bbox at all.
    """
    echoed = (metrics or {}).get("bbox")
    staged = (deck.get("config") or {}).get("bbox")
    for candidate in (echoed, staged):
        if candidate is None:
            continue
        values = tuple(candidate)
        if len(values) != 4:
            raise OpenWaterError(
                f"the TELEMAC domain bbox {candidate!r} is not 4 values "
                "(min_lon, min_lat, max_lon, max_lat); the local mesh frame "
                "cannot be placed.", error_code="TELEMAC_PARAMS_INVALID")
        try:
            return tuple(float(v) for v in values)
        except (TypeError, ValueError) as exc:
            raise OpenWaterError(
                f"the TELEMAC domain bbox {candidate!r} carries non-numeric "
                "corners; the local mesh frame cannot be placed.",
                error_code="TELEMAC_PARAMS_INVALID") from exc
    return None


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


#: The workers report ``dx_m`` rounded to 0.1 m, so any disagreement at or below
#: half of that last place is the REPORT's precision, not a move the builder made.
#: Without this an ask of 33.33 m built at 33.3 m raised a 3 cm "override" note.
_DX_REPORT_TOL_M = 0.05


def mesh_resolution_label(bed: str, deck: Mapping[str, Any],
                         metrics: Mapping[str, Any], *, suffix: str = "") -> str:
    """What grid the run was SOLVED on, in one sentence, for the layer to carry.

    Four open-water publishers were each writing this f-string, and the four
    agreed only by accident: one said "idealized analytic" where the others said
    "idealized", and any of them could have drifted on the coarsening clause
    without the others noticing. ``bed`` names what the bed IS (the one thing that
    genuinely differs per template); ``suffix`` carries an extra fact a domain has
    and the others do not, such as TELEMAC-3D's sigma-plane count.

    The spacing is the one the WORKER reports, falling back to the one the deck
    asked for - never the other way round, or a run the node budget coarsened
    would advertise the spacing it did not use.
    """
    return (f"{bed} grid {metrics.get('dx_m', deck['mesh_size_m']):g} m{suffix}"
            + (" (coarsened under node budget)" if metrics.get("coarsened") else ""))


def mesh_sizing_provenance(asked_m: Any, metrics: Mapping[str, Any]) -> list[Any]:
    """The user's grid-spacing ask, as a row, WHEN the worker MOVED it.

    A user lever the run quietly overrode is the silent-override class: the mesh
    was right and the label was a lie. The open-water builds move it two ways -
    the grid FLOOR raises an ask that is finer than the builder authors, and the
    node BUDGET raises one that is finer than the domain can afford - and neither
    said so, while the reach family has narrated exactly this since wave 2b.

    The row appears only when the ask and the built spacing differ by more than
    the report's own precision, so an honoured lever adds no noise, and it names
    the DIRECTION the value actually moved rather than assuming it was raised.
    ``basis="user"`` - the value came from the caller; the note says what happened.
    """
    from trid3nt_contracts.common import SyntheticInput

    built = metrics.get("dx_m")
    if asked_m is None or built is None:
        return []
    asked_f, built_f = float(asked_m), float(built)
    if abs(built_f - asked_f) <= _DX_REPORT_TOL_M:
        return []
    if built_f > asked_f:
        # Both coarsening paths RAISE the spacing. The node budget records itself;
        # anything else that raised a finer ask is the builder's own grid floor.
        direction = "RAISED"
        reason = ("the node budget for this domain" if metrics.get("coarsened")
                  else "the grid floor this builder authors")
    else:
        # Nothing in the open-water builds coarsens DOWNWARD, so a lowered spacing
        # came from the builder fitting the ask onto its own grid. Say that rather
        # than blaming a floor or a budget that did the opposite.
        direction = "LOWERED"
        reason = "the builder fitting the ask to this domain's grid"
    return [SyntheticInput(
        param="target_resolution_m", value=round(asked_f, 3), units="m",
        basis="user", consequence="numerical",
        note=(f"target_resolution_m {asked_f:g} {direction} to {built_f:g} m "
              f"by {reason}; the field was solved at {built_f:g} m"))]


class SolveOpenWater:
    """The open-water solve step. The plan's consequential node."""

    @staticmethod
    def telemac(*, deck: Any, compute_class: Any) -> Step:
        """Dispatch a staged open-water deck to its own TELEMAC worker."""
        return Step(runner=f"{_STEPS}.open_water.solve_open_water", stage="solve",
                    kwargs={"deck": deck, "compute_class": compute_class},
                    consequential=True)
