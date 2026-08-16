"""Atomic tool ``run_modflow_multi_species_job`` - MODFLOW N-species engine.

The internal engine surface for the multi_species archetype: ONE
shared GWF flow field driving N ModflowGwt solute-transport models (one per
species) + N ModflowGwfgwt flow<->transport exchanges, authored in ONE mf6 run by
``workers/modflow/gwt_adapter.build_modflow_deck(archetype="multi_species",
species=[...])``. It is the multi_species analogue
of ``run_modflow_archetype_job`` (the archetype surface) and
``modflow_contaminant_plume`` (the single-species spill surface), differing only in:

  * it threads the per-species ``species`` list into the adapter's multi_species
    branch (the staging seam ``build_and_stage_modflow_deck`` does NOT forward
    ``species``, so this tool builds the deck itself with the species list), and
  * its postprocess is ``postprocess_multi_species`` -> a LIST of one
    ``PlumeLayerURI`` per species (each carrying ``max_concentration_mgl`` +
    ``plume_area_km2`` + the species name in the layer label), returned inside a
    ``MultiSpeciesPlumeResult``.

Chain (mirrors ``modflow_contaminant_plume`` with the multi_species branch):

  1. Build the multi_species deck (``build_modflow_deck(write=True,
     archetype="multi_species", species=[...])``)  -  ONE shared GWF + N GWT models,
     each writing its own ``gwt_<species>.ucn``.
  2. Run mf6 (local ``mf6`` when ``TRID3NT_MODFLOW_LOCAL=1``; the local-exec
     supervisor / Batch path otherwise)  -  the SAME submit/wait/cancel seam.
  3. Postprocess EVERY per-species ``gwt_<species>.ucn`` -> N ``PlumeLayerURI``.
  4. Return the ``MultiSpeciesPlumeResult`` so the composer loads each plume layer.

Honesty floor (Invariant 9): the USER-INPUT gate (a non-empty, valid species
list with at least one positive release rate) lives in the COMPOSER. As a
backstop, the adapter raises a typed ``ValueError`` for an empty / malformed
species list, surfaced here as a typed error envelope; a run whose every species
plume is empty (max concentration <= floor) returns a typed empty-result error
rather than reading as a successful modeled layer.

Determinism boundary (Invariant 1): every narrated number is a typed
``PlumeLayerURI`` field the postprocess computed  -  never free-generated.

``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.execution import RunResult
from trid3nt_contracts.modflow_contracts import (
    MODFLOWRunArgs,
    MultiSpeciesPlumeResult,
    SpeciesSpec,
)

from trid3nt_server.emission.pipeline_emitter import current_emitter
from trid3nt_server.workflows.modflow.postprocess_modflow import (
    PLUME_DETECTION_FLOOR_MGL,
    PostprocessMODFLOWError,
    postprocess_multi_species,
)
from trid3nt_server.workflows.modflow.run_modflow import (
    MODFLOWWorkflowError,
    build_and_stage_modflow_deck,
    is_local_mode,
    run_modflow_local,
    submit_modflow_run,
)
from trid3nt_server.workflows.shared.solve_progress import drive_live_solve_progress

logger = logging.getLogger("trid3nt_server.data.simulation.modflow.run_modflow_multi_species_tool")

__all__ = [
    "run_modflow_multi_species_job",
    "RunMODFLOWMultiSpeciesError",
]


class RunMODFLOWMultiSpeciesError(RuntimeError):
    """Raised when the multi_species chain fails fatally before producing layers."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _runs_prefix() -> str:
    """Default runs bucket name for composing a fallback output prefix."""
    return os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")


async def run_modflow_multi_species_job(
    run_args: MODFLOWRunArgs,
    *,
    compute_class: str = "standard",
) -> MultiSpeciesPlumeResult | dict[str, Any]:
    """Run a multi_species MODFLOW transport run and postprocess N per-species plumes.

    Internal engine surface (the composer calls this with a fully-assembled
    ``MODFLOWRunArgs`` carrying ``archetype="multi_species"`` + a non-empty
    ``species`` list). Builds the N-GWT deck, runs mf6, postprocesses every
    per-species ``gwt_<species>.ucn`` into one ``PlumeLayerURI`` each, and returns
    them inside a ``MultiSpeciesPlumeResult``.

    Args:
        run_args: the assembled run args (``archetype="multi_species"`` +
            ``species``).
        compute_class: compute class.

    Returns:
        On success: ``MultiSpeciesPlumeResult`` (one ``PlumeLayerURI`` per species).
        On failure: a dict with ``status="error"`` + ``error_code`` +
        ``error_message`` so the composer narrates the failure honestly (no
        layers, never a fabricated plume).
    """
    species = list(run_args.species or [])
    if not species:
        return {
            "status": "error",
            "error_code": "MODFLOW_MULTISPECIES_NO_SPECIES",
            "error_message": (
                "run_modflow_multi_species_job requires a non-empty species list "
                "(archetype='multi_species'); none was supplied."
            ),
        }

    logger.info(
        "run_modflow_multi_species_job aoi=%s n_species=%d local=%s",
        run_args.spill_location_latlon,
        len(species),
        is_local_mode(),
    )

    staging = None
    try:
        # --- Step 1: build the multi_species deck (off-loop) -----------------
        # FOLD (engine-door refactor): build_and_stage_modflow_deck now forwards
        # species (its multi_species branch builds the FLAT N-GWT deck the deleted
        # build_multi_species_staging used to build) - ONE build/stage seam.
        staging = await asyncio.to_thread(build_and_stage_modflow_deck, run_args)

        # --- Step 2: run the solver (local or local-exec/Batch) --------------
        if is_local_mode():
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
                _progress_task.cancel()
                try:
                    await _progress_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        else:
            handle = await asyncio.to_thread(
                submit_modflow_run, staging, compute_class=compute_class
            )
            from trid3nt_server.data.simulation.solver.solver import wait_for_completion

            try:
                run_result: RunResult = await wait_for_completion(handle)
            except asyncio.CancelledError:
                logger.info("run_modflow_multi_species_job cancelled awaiting solver")
                raise
            if run_result.status != "complete":
                return {
                    "status": "error",
                    "error_code": run_result.error_code or run_result.status.upper(),
                    "error_message": (
                        run_result.error_message
                        or run_result.cancellation_reason
                        or "multi_species MODFLOW solver did not complete"
                    ),
                }
            run_outputs_uri = (
                run_result.output_uri or f"s3://{_runs_prefix()}/{run_result.run_id}/"
            )

        # --- Step 3: postprocess N per-species UCN -> N PlumeLayerURI --------
        species_names = [
            (sp.name if isinstance(sp, SpeciesSpec) else sp.get("name"))
            for sp in species
        ]
        result: MultiSpeciesPlumeResult = await asyncio.to_thread(
            lambda: postprocess_multi_species(
                run_outputs_uri,
                run_id=staging.run_id,
                model_crs=staging.model_crs,
                deck_dir=staging.local_deck_dir,
                species_names=[str(n) for n in species_names if n],
            )
        )

        # Honesty floor: a "modeled" multi_species envelope whose EVERY species
        # plume is empty (peak concentration at/below the detection floor) must not
        # read as a successful layer set. At least one species must show a plume.
        any_plume = any(
            float(getattr(p, "max_concentration_mgl", 0.0)) > PLUME_DETECTION_FLOOR_MGL
            for p in result.plumes
        )
        if not any_plume:
            return {
                "status": "error",
                "error_code": "MODFLOW_MULTISPECIES_EMPTY_RESULT",
                "error_message": (
                    "the multi_species run produced no non-trivial plume for any "
                    "species (all peak concentrations at/below the detection floor); "
                    "check the per-species release rates. No layers were loaded."
                ),
            }

        logger.info(
            "run_modflow_multi_species_job complete run_id=%s n_plumes=%d",
            staging.run_id,
            len(result.plumes),
        )
        return result

    except asyncio.CancelledError:
        raise
    except (MODFLOWWorkflowError, PostprocessMODFLOWError) as exc:
        logger.warning(
            "run_modflow_multi_species_job failed: %s (%s)", exc.error_code, exc
        )
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except ValueError as exc:
        # The adapter raises a ValueError for an invalid/empty species list (the
        # engine-side backstop to the composer honesty gate).
        logger.warning("run_modflow_multi_species_job input error: %s", exc)
        return {
            "status": "error",
            "error_code": "MODFLOW_MULTISPECIES_INPUT_INVALID",
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001  -  defensive catch-all
        logger.exception("run_modflow_multi_species_job unexpected failure")
        return {
            "status": "error",
            "error_code": "MODFLOW_MULTISPECIES_INTERNAL_ERROR",
            "error_message": str(exc),
        }
    finally:
        if staging is not None:
            try:
                deck_base = Path(staging.local_deck_dir).parent
                if deck_base.name.startswith("modflow-"):
                    shutil.rmtree(deck_base, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass
