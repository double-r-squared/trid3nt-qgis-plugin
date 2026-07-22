"""Atomic tool ``run_modflow_multi_species_job``  -  MODFLOW Wave-3 N-species engine.

The internal engine surface for the sprint-18 Wave-3 multi_species archetype: ONE
shared GWF flow field driving N ModflowGwt solute-transport models (one per
species) + N ModflowGwfgwt flow<->transport exchanges, authored in ONE mf6 run by
``services/workers/modflow/gwt_adapter.build_modflow_deck(archetype="multi_species",
species=[...])`` (the Wave-3 DECK-AUTHOR landing). It is the multi_species analogue
of ``run_modflow_archetype_job`` (the Wave-1/2 archetype surface) and
``run_modflow_job`` (the single-species spill surface), differing only in:

  * it threads the per-species ``species`` list into the adapter's multi_species
    branch (the staging seam ``build_and_stage_modflow_deck`` does NOT forward
    ``species``, so this tool builds the deck itself with the species list), and
  * its postprocess is ``postprocess_multi_species`` -> a LIST of one
    ``PlumeLayerURI`` per species (each carrying ``max_concentration_mgl`` +
    ``plume_area_km2`` + the species name in the layer label), returned inside a
    ``MultiSpeciesPlumeResult``.

Chain (mirrors ``run_modflow_job`` with the multi_species branch):

  1. Build the multi_species deck (``build_modflow_deck(write=True,
     archetype="multi_species", species=[...])``)  -  ONE shared GWF + N GWT models,
     each writing its own ``gwt_<species>.ucn``.
  2. Run mf6 (local ``mf6`` when ``GRACE2_MODFLOW_LOCAL=1``; the local-exec
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

FR-DC-6: ``cacheable=False`` + ``ttl_class="live-no-cache"`` +
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

from grace2_contracts import new_ulid
from grace2_contracts.execution import RunResult
from grace2_contracts.modflow_contracts import (
    MODFLOWRunArgs,
    MultiSpeciesPlumeResult,
    SpeciesSpec,
)

from ..pipeline_emitter import current_emitter
from ..workflows.postprocess_modflow import (
    PLUME_DETECTION_FLOOR_MGL,
    PostprocessMODFLOWError,
    postprocess_multi_species,
)
from ..workflows.run_modflow import (
    MODFLOWWorkflowError,
    DeckStaging,
    build_modflow_deck,
    is_local_mode,
    run_modflow_local,
    submit_modflow_run,
)
from ..workflows.solve_progress import drive_live_solve_progress

logger = logging.getLogger("grace2_agent.tools.run_modflow_multi_species_tool")

__all__ = [
    "run_modflow_multi_species_job",
    "RunMODFLOWMultiSpeciesError",
    "build_multi_species_staging",
]


class RunMODFLOWMultiSpeciesError(RuntimeError):
    """Raised when the multi_species chain fails fatally before producing layers."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _species_payload(species: list[SpeciesSpec | dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize each species spec into a plain dict the adapter accepts.

    The adapter's ``_normalize_species`` accepts either objects with ``.name`` /
    ``.release_rate_kg_s`` / ``.sorption_kd`` / ``.decay_per_day`` / ``.parent``
    attributes OR plain dicts; we hand it dicts so the agent does not depend on
    the adapter's object-vs-dict acceptance.
    """
    out: list[dict[str, Any]] = []
    for sp in species:
        if isinstance(sp, SpeciesSpec):
            out.append(sp.model_dump())
        elif isinstance(sp, dict):
            out.append(dict(sp))
        else:  # defensive: attribute-style object
            out.append(
                {
                    "name": getattr(sp, "name", None),
                    "release_rate_kg_s": getattr(sp, "release_rate_kg_s", None),
                    "sorption_kd": getattr(sp, "sorption_kd", None),
                    "decay_per_day": getattr(sp, "decay_per_day", None),
                    "parent": getattr(sp, "parent", None),
                }
            )
    return out


def build_multi_species_staging(
    run_args: MODFLOWRunArgs,
    *,
    run_id: str | None = None,
    workdir: str | Path | None = None,
) -> DeckStaging:
    """Build a multi_species deck (ONE shared GWF + N GWT) and wrap it for the run.

    Unlike ``build_and_stage_modflow_deck`` (which does not forward ``species``),
    this threads ``run_args.species`` + ``archetype="multi_species"`` into the
    adapter's multi_species branch. The deck is written FLAT (mf6 reads
    ``mfsim.nam`` from the deck dir CWD in local mode); the per-species
    ``gwt_<species>.ucn`` files land beside it for the postprocess glob. Returns a
    ``DeckStaging`` whose ``local_deck_dir`` is the flat deck dir + ``model_crs``
    is the adapter's projected grid CRS (the postprocess reprojection key).

    Raises:
        MODFLOWWorkflowError("MODFLOW_DECK_BUILD_FAILED"): the adapter build failed.
        ValueError: re-raised from the adapter for an invalid species list.
    """
    rid = run_id or new_ulid()
    base = Path(workdir) if workdir is not None else Path(
        tempfile.mkdtemp(prefix=f"modflow-{rid}-")
    )
    deck_dir = base / "deck"
    deck_dir.mkdir(parents=True, exist_ok=True)

    species_payload = _species_payload(run_args.species or [])
    try:
        manifest_obj = build_modflow_deck(
            spill_location_latlon=run_args.spill_location_latlon,
            contaminant=run_args.contaminant,
            release_rate_kg_s=run_args.release_rate_kg_s,
            duration_days=run_args.duration_days,
            aquifer_k_ms=run_args.aquifer_k_ms,
            porosity=run_args.porosity,
            workdir=str(deck_dir),
            write=True,
            archetype="multi_species",
            species=species_payload,
        )
    except (MODFLOWWorkflowError, ValueError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise MODFLOWWorkflowError(
            "MODFLOW_DECK_BUILD_FAILED",
            message=f"multi_species build_modflow_deck failed: {exc}",
            details={"run_id": rid},
        ) from exc

    deck_base_uri = f"file://{deck_dir}/"
    return DeckStaging(
        run_id=rid,
        manifest_uri=deck_base_uri + "manifest.json",
        deck_base_uri=deck_base_uri,
        local_deck_dir=str(deck_dir),
        model_crs=manifest_obj.model_crs,
        gwf_name=manifest_obj.gwf_name,
        gwt_name=manifest_obj.gwt_name,
        spill_lat=float(manifest_obj.spill_lat),
        spill_lon=float(manifest_obj.spill_lon),
        output_globs=[
            "gwt_*.ucn",
            f"{manifest_obj.gwf_name}.hds",
            f"{manifest_obj.gwf_name}.cbc",
            "*.lst",
            "mfsim.lst",
        ],
        archetype="multi_species",
        gwt_present=True,
    )


def _runs_prefix() -> str:
    """Default runs bucket name for composing a fallback output prefix."""
    return os.environ.get("GRACE2_RUNS_BUCKET", "grace-2-hazard-prod-runs")


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
        compute_class: FR-CE-3 compute class.

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
        staging = await asyncio.to_thread(build_multi_species_staging, run_args)

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
            from .solver import wait_for_completion

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
                run_result.output_uri or f"gs://{_runs_prefix()}/{run_result.run_id}/"
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
