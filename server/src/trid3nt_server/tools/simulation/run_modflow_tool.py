"""Atomic tool ``run_modflow_job`` — MODFLOW groundwater-plume engine (job-0227).

The LLM-facing exposure of the MODFLOW 6 + MF6-GWT groundwater-contamination
engine. ``run_modflow_job(...)`` takes the ``MODFLOWRunArgs`` forcing-parameter
fields, runs the deterministic deck-build → submit → wait → postprocess chain
(``workflows/run_modflow.py`` + ``workflows/postprocess_modflow.py``), and
returns a ``PlumeLayerURI`` the emitter loads onto the map (it subclasses
``LayerURI`` so the ``emit_tool_call`` ``add_loaded_layer`` gate fires).

This is the MODFLOW analogue of ``run_model_flood_scenario`` (SFINCS). Like that
wrapper it declares ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"`` (FR-DC-6 — workflow exposure surface;
never touches the cache shim).

Two execution paths, selected by ``TRID3NT_MODFLOW_LOCAL``:

  * **Cloud (default).** Stage the deck to the cache bucket, submit a
    legacy Cloud Workflows execution
    (``submit_modflow_run``), poll it with the SFINCS-shared
    ``wait_for_completion`` (tools/solver.py — its ``ExecutionHandle`` cancel
    seam is solver-agnostic), then postprocess the run's UCN output. Confirmation
    before consequence (Invariant 9 — a solver run) is enforced by the server
    confirmation hook around this tool, not re-implemented here.

  * **Local (``TRID3NT_MODFLOW_LOCAL=1``).** Run the staged deck against a local
    ``mf6`` binary (``run_modflow_local``), then postprocess. This is the
    dev/test seam AND the live-evidence path on a box with no docker / gcloud.

Determinism boundary (Invariant 1): every plume number the agent narrates comes
from the typed ``PlumeLayerURI.max_concentration_mgl`` / ``.plume_area_km2``
fields the postprocess computed — never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from trid3nt_contracts.execution import RunResult
from trid3nt_contracts.modflow_contracts import MODFLOWRunArgs, PlumeLayerURI
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.pipeline_emitter import current_emitter
from trid3nt_server.tool_arg_normalizer import LatLonCoercionError, coerce_latlon
from trid3nt_server.workflows.postprocess_modflow import (
    PostprocessMODFLOWError,
    postprocess_modflow,
    publish_modflow_quantities,
)
from trid3nt_server.workflows.solve_progress import drive_live_solve_progress
from trid3nt_server.workflows.run_modflow import (
    MODFLOWWorkflowError,
    build_and_stage_modflow_deck,
    is_local_mode,
    run_modflow_local,
    submit_modflow_run,
)

logger = logging.getLogger("trid3nt_server.tools.simulation.run_modflow_tool")

__all__ = ["run_modflow_job", "RunMODFLOWError"]


class RunMODFLOWError(RuntimeError):
    """Raised when the MODFLOW chain fails fatally before producing a layer.

    Carries the open-set ``error_code`` propagated from the failing stage so
    the agent emitter renders a typed error frame (the emitter's
    ``_classify_exception`` reads ``error_code`` off the exception)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


_RUN_MODFLOW_JOB_METADATA = AtomicToolMetadata(
    name="run_modflow_job",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
)


@register_tool(
    _RUN_MODFLOW_JOB_METADATA,
    # readOnlyHint=False (submits a solver run writing output artifacts),
    # openWorldHint=False (intra-GCP Cloud Workflows + Cloud Run, or local mf6),
    # destructiveHint=False (writes go to a new runs/ prefix), idempotentHint=False
    # (each call mints a new run_id + Workflow execution).
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def run_modflow_job(
    spill_location_latlon: tuple[float, float] | list[float] | None = None,
    contaminant: str | None = None,
    release_rate_kg_s: float | None = None,
    duration_days: float | None = None,
    aquifer_k_ms: float | None = None,
    porosity: float | None = None,
    compute_class: str = "standard",
    # job-0164: absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> PlumeLayerURI | dict[str, Any]:
    """Run a MODFLOW 6 groundwater-contamination (spill) plume simulation.

    Builds a MODFLOW 6 GWF (steady-state flow) + MF6-GWT (transient solute
    transport) deck from the spill parameters, runs the ``mf6`` solver (Cloud
    Run Job via Cloud Workflows, or a local binary when
    ``TRID3NT_MODFLOW_LOCAL=1``), reads the final-timestep contaminant
    concentration field, reprojects it to an EPSG:4326 plume COG, and returns a
    ``PlumeLayerURI`` carrying the peak concentration + plume footprint the
    agent narrates.

    Use this when:
        - The user asks to model a groundwater contamination spill, simulate a
          contaminant plume, estimate how far a chemical spill spreads in an
          aquifer, or run a MODFLOW groundwater-transport scenario.
        - A spill event (location + contaminant + release rate + duration) needs
          a plume extent + peak concentration.

    Do NOT use this for:
        - Surface-water / inundation flooding (use ``run_model_flood_scenario``
          — that is SFINCS).
        - Reactive transport with sorption or biodegradation (v0.1 models a
          conservative tracer only).
        - Cancelling a running plume simulation (use the WS ``cancel`` envelope;
          cancellation propagates through ``wait_for_completion``).

    Params:
        spill_location_latlon: ``(lat, lon)`` of the spill in EPSG:4326 degrees
            (lat-first — a point, not a bbox).
        contaminant: contaminant name (e.g. ``"benzene"``, ``"TCE"``). The
            transport math treats it as a conservative tracer.
        release_rate_kg_s: contaminant mass-release rate in kg/s (> 0).
        duration_days: release + transport duration in days (> 0).
        aquifer_k_ms: aquifer hydraulic conductivity in m/s (> 0). Optional;
            defaults to the demo value (1e-4 m/s) — narrate as a demo default.
        porosity: aquifer effective porosity in (0, 1]. Optional; defaults to
            the demo value (0.3) — narrate as a demo default.
        compute_class: FR-CE-3 compute class. Default ``"standard"``.

    Returns:
        On success: a ``PlumeLayerURI`` (a ``LayerURI`` subtype) — the emitter
        appends it to ``session-state.loaded_layers`` and the map renders it.
        It carries ``max_concentration_mgl`` + ``plume_area_km2`` (Invariant 1 —
        the agent narrates these typed numbers, never invents them).

        On failure: a dict with ``status="error"`` + ``error_code`` +
        ``error_message`` so the LLM narrates the failure honestly (no layer).

    FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
    ``source_class="workflow_dispatch"`` — the cache shim is NOT invoked.
    """
    # --- Validate + coerce into the MODFLOWRunArgs contract -----------------
    # The contract owns range validation (lat/lon, positivity, porosity bound).
    # Defaults for aquifer_k_ms / porosity live in the contract, so only pass
    # them through when supplied.
    if spill_location_latlon is None or contaminant is None or (
        release_rate_kg_s is None
    ) or duration_days is None:
        return {
            "status": "error",
            "error_code": "MODFLOW_PARAMS_INCOMPLETE",
            "error_message": (
                "run_modflow_job requires spill_location_latlon, contaminant, "
                "release_rate_kg_s, and duration_days."
            ),
        }
    # job-0317: Bedrock Claude passes spill_location_latlon as a STRING
    # ("40.81,-96.71", "[40.81, -96.71]", "(40.81, -96.71)", "40.81 -96.71"),
    # not a JSON array. The previous ``tuple(float(v) for v in ...)`` iterated
    # the STRING'S CHARACTERS -> float('.') -> MODFLOW_PARAMS_INVALID. Coerce
    # robustly here BEFORE the float loop so the direct solver path works.
    try:
        loc = tuple(coerce_latlon(spill_location_latlon))  # -> (lat, lon)
    except LatLonCoercionError as exc:
        return {
            "status": "error",
            "error_code": "MODFLOW_PARAMS_INVALID",
            "error_message": (
                f"invalid spill_location_latlon (expected lat,lon): {exc}"
            ),
        }
    try:
        kwargs: dict[str, Any] = dict(
            spill_location_latlon=loc,  # type: ignore[arg-type]
            contaminant=contaminant,
            release_rate_kg_s=float(release_rate_kg_s),
            duration_days=float(duration_days),
        )
        if aquifer_k_ms is not None:
            kwargs["aquifer_k_ms"] = float(aquifer_k_ms)
        if porosity is not None:
            kwargs["porosity"] = float(porosity)
        run_args = MODFLOWRunArgs(**kwargs)
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError or coercion
        return {
            "status": "error",
            "error_code": "MODFLOW_PARAMS_INVALID",
            "error_message": f"invalid MODFLOW run arguments: {exc}",
        }

    logger.info(
        "run_modflow_job spill=%s contaminant=%r rate=%s kg/s duration=%s d local=%s",
        run_args.spill_location_latlon,
        run_args.contaminant,
        run_args.release_rate_kg_s,
        run_args.duration_days,
        is_local_mode(),
    )

    staging = None
    try:
        # --- Step 1: build + stage the deck ---------------------------------
        # audit #4: deck build (FloPy) + subdir reorg + S3/GCS upload are
        # synchronous and CPU/IO-bound. Offload to a worker thread so they do
        # NOT stall the asyncio event loop (WS keepalive). build_and_stage is
        # emit-free (no current_emitter / add_loaded_layer / emit_*) - verified
        # safe to run off-loop; the bracketing emit_tool_call still runs on the
        # loop around this call.
        staging = await asyncio.to_thread(build_and_stage_modflow_deck, run_args)

        # --- Step 2: run the solver (local or cloud) ------------------------
        if is_local_mode():
            # audit #4: a local mf6 binary solve can run for MINUTES - the same
            # long-blocking-solve class that killed the WS for SWMM. Offload to
            # a worker thread. run_modflow_local is a foreground subprocess run
            # + mfsim.lst parse + completion.json write; emit-free.
            #
            # LIVE solve-progress heartbeat (NATE 2026-06-17): the off-loop mf6
            # solve emits nothing for minutes, so the running card is a silent
            # spinner. Drive the shared solve-progress envelope ON the loop (the
            # emitter is loop-bound) alongside the off-loop solve - identical to
            # the proven SFINCS pattern. DeckStaging carries no cell-count /
            # resolution / perf-model ETA, so those pass None - the heartbeat
            # still ticks elapsed wall-clock (the point). Best-effort: emitter
            # None -> no-op; cancelled + awaited in the finally regardless.
            _progress_task = asyncio.ensure_future(
                drive_live_solve_progress(
                    emitter=current_emitter(),
                    run_id=staging.run_id,
                    solver="modflow",
                    grid_resolution_m=None,
                    active_cell_count=None,
                    vcpus=None,
                    eta_seconds=None,
                )
            )
            try:
                run_outputs_uri = await asyncio.to_thread(run_modflow_local, staging)
            finally:
                # Tear down the heartbeat (success, failure, OR cancel).
                _progress_task.cancel()
                try:
                    await _progress_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        else:
            # audit #4: submit_modflow_run is a synchronous boto3-backed
            # local-exec dispatch (launch_local_solver is non-blocking but does
            # synchronous manifest read + S3 input staging before returning the
            # handle). Offload so the submit IO does not stall the loop;
            # emit-free. The async wait_for_completion below stays on the loop.
            handle = await asyncio.to_thread(
                submit_modflow_run, staging, compute_class=compute_class
            )
            # Reuse the SFINCS-shared poller — its ExecutionHandle cancel seam
            # is solver-agnostic (Invariant 8 cancel chain propagates here).
            from trid3nt_server.tools.simulation.solver import wait_for_completion

            try:
                run_result: RunResult = await wait_for_completion(handle)
            except asyncio.CancelledError:
                logger.info("run_modflow_job cancelled while awaiting solver")
                raise
            if run_result.status != "complete":
                return {
                    "status": "error",
                    "error_code": run_result.error_code or run_result.status.upper(),
                    "error_message": (
                        run_result.error_message
                        or run_result.cancellation_reason
                        or "MODFLOW solver did not complete"
                    ),
                }
            run_outputs_uri = (
                run_result.output_uri
                or f"s3://{_runs_prefix()}/{run_result.run_id}/"
            )

        # --- Step 3: postprocess UCN → plume COG → PlumeLayerURI ------------
        # audit #4: postprocess reads the UCN, reprojects to a COG, uploads it,
        # and calls publish_layer - all synchronous and IO/CPU-bound. Offload to
        # a worker thread. EMITTER SUBTLETY: postprocess_modflow calls
        # publish_layer, but publish_layer is purely synchronous and does NOT
        # touch the loop-bound emitter (no current_emitter / add_loaded_layer /
        # emit_*; it only bridges the COG to a tile/WMS URL). The actual
        # add_loaded_layer happens later, on the loop, in the bracketing
        # emit_tool_call when the returned PlumeLayerURI is processed - so the
        # whole call is emit-free and safe off-loop (no compute/emit split
        # needed here).
        plume = await asyncio.to_thread(
            lambda: postprocess_modflow(
                run_outputs_uri,
                run_id=staging.run_id,
                model_crs=staging.model_crs,
                deck_dir=staging.local_deck_dir,
            )
        )
        logger.info(
            "run_modflow_job complete run_id=%s max_concentration_mgl=%.6g "
            "plume_area_km2=%.6g uri=%s",
            staging.run_id,
            plume.max_concentration_mgl,
            plume.plume_area_km2,
            plume.uri,
        )

        # --- levers STEP 3: ADDITIVE registry quantities (gated) ------------
        # The plume above is the byte-identical headline. When the registry
        # quantities flag is on, ALSO publish the concentration ANIMATION (all
        # saved UCN steps) + the GWF head / water-table as context layers via
        # the shared executor. Non-fatal: a failure here never sinks the plume.
        import os as _os

        if _os.environ.get("TRID3NT_MODFLOW_REGISTRY_QUANTITIES", "").lower() in (
            "1", "true", "on", "yes"
        ):
            try:
                from trid3nt_server.workflows.register_published_manifest import (
                    register_manifest_layers,
                )

                reg = await asyncio.to_thread(
                    lambda: publish_modflow_quantities(
                        run_outputs_uri,
                        run_id=staging.run_id,
                        model_crs=staging.model_crs,
                        register_manifest_layers=register_manifest_layers,
                        deck_dir=staging.local_deck_dir,
                        bbox=plume.bbox,
                    )
                )
                emitter = current_emitter()
                if emitter is not None and reg is not None:
                    for extra_layer in getattr(reg, "layers", []) or []:
                        try:
                            await emitter.add_loaded_layer(extra_layer)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("could not add registry layer: %s", exc)
            except Exception as exc:  # noqa: BLE001 - additive layers are best-effort
                logger.warning(
                    "run_modflow_job registry-quantity publish failed (non-fatal): %s",
                    exc,
                )

        return plume

    except asyncio.CancelledError:
        raise
    except (MODFLOWWorkflowError, PostprocessMODFLOWError) as exc:
        logger.warning("run_modflow_job failed: %s (%s)", exc.error_code, exc)
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 — defensive catch-all
        logger.exception("run_modflow_job unexpected failure")
        return {
            "status": "error",
            "error_code": "MODFLOW_INTERNAL_ERROR",
            "error_message": str(exc),
        }
    finally:
        # Best-effort cleanup of the local deck dir (the COG was already
        # uploaded / surfaced). In local mode the postprocess read the deck
        # for georegistration BEFORE this runs, so cleanup is safe here.
        if staging is not None:
            try:
                deck_base = Path(staging.local_deck_dir).parent
                if deck_base.name.startswith("modflow-"):
                    shutil.rmtree(deck_base, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass


def _runs_prefix() -> str:
    """Default runs bucket name for composing a fallback output prefix."""
    import os

    return os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")
