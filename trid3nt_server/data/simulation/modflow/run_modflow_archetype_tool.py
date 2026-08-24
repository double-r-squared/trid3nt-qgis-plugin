"""Atomic tool ``run_modflow_archetype_job`` - MODFLOW GWF-only engines.

The shared LLM-facing exposure of all MODFLOW archetypes:
  ``sustainable_yield`` / ``mine_dewatering`` / ``regional_water_budget``
  ``MAR`` / ``ASR`` / ``wetland_hydroperiod``
  ``multi_species``
  ``capture_zone`` / ``wellhead_protection`` (PRT backward particle tracking)

All archetypes REUSE the live MODFLOW solver path (``workflows/run_modflow.py``
deck-build -> submit/local-run, the ``modflow`` Batch job-def, the GWF-only
branch of ``workers/modflow/gwt_adapter.build_modflow_deck``)  -  there
is NO new worker, container, or Batch job-def. They differ ONLY in the
per-archetype forcing they thread into ``MODFLOWRunArgs`` and the postprocess
reader they pick:

  * ``sustainable_yield``     -> ``postprocess_drawdown``        -> DrawdownLayerURI
  * ``mine_dewatering``       -> ``postprocess_dewatering``      -> DewaterLayerURI
  * ``regional_water_budget`` -> ``postprocess_budget_partition``-> BudgetPartitionLayerURI
  * ``MAR``                   -> ``postprocess_mounding``        -> MoundingLayerURI
  * ``ASR``                   -> ``postprocess_asr``             -> ASRLayerURI
  * ``wetland_hydroperiod``   -> ``postprocess_wetland_hydroperiod``-> HydroperiodLayerURI
  * ``capture_zone``          -> ``postprocess_capture_zone``    -> CaptureZoneLayerURI
  * ``wellhead_protection``   -> ``postprocess_capture_zone``    -> CaptureZoneLayerURI

Chain (mirrors ``modflow_contaminant_plume`` with the archetype branch):

  1. Build + stage a GWF-only archetype deck (``build_and_stage_modflow_deck``
     threads ``run_args.archetype`` + the per-archetype fields into the adapter's
     GWF-only branch). The deck writes head (``gwf_model.hds``) + budget
     (``gwf_model.cbc``) and NO UCN concentration.
  2. Run mf6 (AWS Batch ``modflow`` job-def, or local ``mf6`` when
     ``TRID3NT_MODFLOW_LOCAL=1``)  -  the SAME submit/wait/cancel seam as
     ``modflow_contaminant_plume``.
  3. Postprocess the head / cbc into the archetype's headline LayerURI.
  4. Return it so the emitter's ``add_loaded_layer`` gate loads it onto the map.

 PRT archetypes (``capture_zone`` / ``wellhead_protection``) run a
two-simulation sequence: a GWF flow solve followed by an MF6 PRT
backward-particle-tracking solve. These archetypes are LOCAL-ONLY (PRT track
files are small and fast; the Batch path is never used). After the GWF run,
``gwt_adapter.build_and_run_prt_from_gwf`` reverses the GWF outputs, builds and
runs the PRT sim, and returns the PRT working directory whose
``prtmodel.trk.csv`` feeds ``postprocess_capture_zone``.

This tool is the engine surface the composers dispatch to. The USER-INPUT
honesty gate (no fabricated well) lives in the COMPOSERS  -  by the time a
request reaches this tool the contract args carry the real user-supplied
geometry. As a backstop, an absent required field raises a typed ValueError in
the adapter (surfaced here as a typed error envelope).

Determinism boundary (Invariant 1): every narrated number comes from the typed
LayerURI fields the postprocess computed  -  never free-generated.

``cacheable=False`` + ``ttl_class="live-no-cache"`` +
``source_class="workflow_dispatch"``  -  the cache shim is NOT invoked.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from trid3nt_contracts.execution import LayerURI, RunResult
from trid3nt_contracts.modflow_contracts import MODFLOWRunArgs

from trid3nt_server.emission.pipeline_emitter import current_emitter
from trid3nt_server.workflows.modflow.postprocess_modflow import (
    PostprocessMODFLOWError,
    postprocess_asr,
    postprocess_budget_partition,
    postprocess_capture_zone,
    postprocess_dewatering,
    postprocess_drawdown,
    postprocess_gwe_thermal,
    postprocess_mounding,
    postprocess_saltwater_intrusion,
    postprocess_stream_reaches,
    postprocess_subsidence,
    postprocess_vadose,
    postprocess_wetland_hydroperiod,
)
from trid3nt_server.workflows.modflow.run_modflow import (
    MODFLOWWorkflowError,
    build_and_stage_modflow_deck,
    is_local_mode,
    run_modflow_local,
    submit_modflow_run,
)
from trid3nt_server.workflows.shared.solve_progress import drive_live_solve_progress

logger = logging.getLogger("trid3nt_server.data.simulation.modflow.run_modflow_archetype_tool")

__all__ = [
    "run_modflow_archetype_job",
    "RunMODFLOWArchetypeError",
    "ARCHETYPE_POSTPROCESS",
]


class RunMODFLOWArchetypeError(RuntimeError):
    """Raised when the archetype chain fails fatally before producing a layer."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: archetype -> (postprocess callable, headline-scalar attr the logger reads).
#: The callable signature is uniform (run_outputs_uri, *, run_id, model_crs,
#: deck_dir) so the dispatch below is one branchless lookup. The trio
#: (MAR / ASR / wetland_hydroperiod) extends it additively. adds the
#: two PRT capture-zone archetypes; both share ``postprocess_capture_zone``
#: with ``capture_zone_area_km2`` as the headline scalar.
ARCHETYPE_POSTPROCESS: dict[str, Any] = {
    "sustainable_yield": (postprocess_drawdown, "max_drawdown_m"),
    "mine_dewatering": (postprocess_dewatering, "dewatering_rate_m3_day"),
    "regional_water_budget": (postprocess_budget_partition, "budget_partition_m3_day"),
    "MAR": (postprocess_mounding, "max_mounding_m"),
    "ASR": (postprocess_asr, "head_timeseries"),
    "wetland_hydroperiod": (
        postprocess_wetland_hydroperiod,
        "seasonal_head_range_m",
    ),
    # PRT backward particle tracking (LOCAL-ONLY; see PRT_ARCHETYPES below).
    # The headline is the outer envelope area; a zero-area zone is an empty result.
    "capture_zone": (postprocess_capture_zone, "capture_zone_area_km2"),
    "wellhead_protection": (postprocess_capture_zone, "capture_zone_area_km2"),
    # Henry-style variable-density GWF+GWT saltwater intrusion (LOCAL-ONLY:
    # the Henry demo grid is small + fast; Batch is never used). The headline is the
    # bottom-layer 50%-isochlor toe penetration in metres (a positive scalar; the
    # > 0 floor applies). NOT in PRT_ARCHETYPES (no PRT sim; standard GWF+GWT run).
    "saltwater_intrusion": (postprocess_saltwater_intrusion, "intrusion_length_m"),
    # module wave: SFR routed stream-depletion (LOCAL-ONLY v0.1; kept OFF the
    # offload table alongside the PRT + saltwater archetypes). The headline is the
    # net streamflow captured from the stream by the pumping (a positive scalar;
    # the > 0 empty-result floor applies).
    "stream_depletion": (postprocess_stream_reaches, "total_depletion_m3_day"),
    # module wave: CSUB land subsidence (LOCAL-ONLY v0.1; kept OFF the offload
    # table alongside stream_depletion + the PRT + saltwater archetypes). Standard
    # single GWF run (no PRT, no two-sim sequence). The headline is the peak
    # ground subsidence in cm (a positive scalar; the > 0 empty-result floor
    # applies).
    "land_subsidence": (postprocess_subsidence, "max_subsidence_cm"),
    # UZF+UZT vadose-zone breakthrough (LOCAL-ONLY; FLAT dual-model
    # deck, kept OFF the offload table). The deliverable is the base-of-column
    # breakthrough SERIES (chart-primary), so the headline is the
    # concentration_series presence, not a positive scalar (in _NON_SCALAR_HEADLINES
    # below): a valid run always writes >= 1 obs step even when the tracer has not
    # yet crossed the half-source threshold (honest non-arrival narrated by the
    # composer), so the > 0 scalar floor would wrongly reject a slow column.
    "vadose_transport": (postprocess_vadose, "concentration_series"),
    # GWE heat transport (both injection_plume + ates modes ride this
    # one postprocess). The honesty headline is the peak temperature EXCESS above
    # ambient (a positive scalar; the > 0 floor applies to BOTH modes -- an ates
    # run's charged footprint always heats before recovery draws it down). The
    # per-cycle recovery series is an ADDITIONAL ates deliverable, not the floor.
    # LOCAL-ONLY (GWF+GWE dual model, kept off the Batch offload table).
    "gwe_thermal": (postprocess_gwe_thermal, "peak_excess_temperature_c"),
}

#: Archetypes whose headline deliverable is a SERIES / dict (truthy-when-present)
#: rather than a positive scalar. The empty-result honesty floor checks these for
#: presence (a non-empty list/dict) instead of ``float(headline) > 0``: the ASR
#: deliverable is the well-head sawtooth series (recovery_efficiency may legitimately
#: be None on a single cycle), and the budget partition is a dict.
#: capture_zone / wellhead_protection are NOT in this set: the headline scalar
#: (capture_zone_area_km2) is a non-negative float and the ``> 0`` floor applies.
_NON_SCALAR_HEADLINES: frozenset[str] = frozenset(
    {"regional_water_budget", "ASR", "vadose_transport"}
)

#: PRT archetypes run a two-simulation sequence (GWF + PRT backward
#: tracking). They are LOCAL-ONLY: PRT track files are small and fast; the
#: Batch submit/wait path is deliberately bypassed for these archetypes. The
#: ``run_modflow_archetype_job`` function has a contained branch for them that
#: calls ``gwt_adapter.build_and_run_prt_from_gwf`` after the GWF run and then
#: feeds the PRT directory to ``postprocess_capture_zone``.
PRT_ARCHETYPES: frozenset[str] = frozenset({"capture_zone", "wellhead_protection"})


def _runs_prefix() -> str:
    """Default runs bucket name for composing a fallback output prefix."""
    return os.environ.get("TRID3NT_RUNS_BUCKET", "trid3nt-runs")


async def run_modflow_archetype_job(
    run_args: MODFLOWRunArgs,
    *,
    compute_class: str = "standard",
) -> LayerURI | dict[str, Any]:
    """Run one MODFLOW archetype and postprocess its headline layer.

    Internal engine surface (the composers call this with a fully-assembled
    ``MODFLOWRunArgs``; it is NOT registered as a thin LLM tool because the
    per-archetype composer dispatch tools are the LLM-facing surface). Selects
    the postprocess by ``run_args.archetype`` and returns the archetype's typed
    headline LayerURI.

     PRT archetypes (``capture_zone`` / ``wellhead_protection``) run a
    two-simulation sequence (GWF + PRT) and are LOCAL-ONLY: regardless of the
    ``TRID3NT_MODFLOW_LOCAL`` env var or ``compute_class``, these archetypes always
    execute locally via ``run_modflow_local`` + ``gwt_adapter.build_and_run_prt_from_gwf``.

    Args:
        run_args: the assembled MODFLOW run args with ``archetype`` set and the
            per-archetype geometry fields populated.
        compute_class: compute class (ignored for PRT archetypes).

    Returns:
        On success: the archetype's headline LayerURI subtype (a ``LayerURI`` so
        the emitter loads it onto the map). On failure: a dict with
        ``status="error"`` + ``error_code`` + ``error_message`` so the caller
        narrates the failure honestly (no layer, never a fabricated success).
    """
    archetype = getattr(run_args, "archetype", None)
    if archetype not in ARCHETYPE_POSTPROCESS:
        return {
            "status": "error",
            "error_code": "MODFLOW_ARCHETYPE_UNKNOWN",
            "error_message": (
                f"run_modflow_archetype_job requires a known archetype "
                f"(one of {sorted(ARCHETYPE_POSTPROCESS)}); got {archetype!r}."
            ),
        }
    postprocess_fn, headline_attr = ARCHETYPE_POSTPROCESS[archetype]

    is_prt = archetype in PRT_ARCHETYPES


    logger.info(
        "run_modflow_archetype_job archetype=%s aoi=%s compute=%s local=%s prt=%s",
        archetype,
        run_args.spill_location_latlon,
        compute_class,
        is_local_mode(),
        is_prt,
    )

    staging = None
    try:
        # --- Step 1: build + stage the GWF-only archetype deck (off-loop) ----
        staging = await asyncio.to_thread(build_and_stage_modflow_deck, run_args)

        # --- Step 2: run the GWF solver ----------------------------------------
        # PRT archetypes are LOCAL-ONLY (fast; Batch not used).  Other archetypes
        # use the normal local/Batch branch.
        if is_prt or is_local_mode():
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
            from trid3nt_server.workflows.solver.solver import wait_for_completion

            try:
                run_result: RunResult = await wait_for_completion(handle)
            except asyncio.CancelledError:
                logger.info("run_modflow_archetype_job cancelled while awaiting solver")
                raise
            if run_result.status != "complete":
                return {
                    "status": "error",
                    "error_code": run_result.error_code or run_result.status.upper(),
                    "error_message": (
                        run_result.error_message
                        or run_result.cancellation_reason
                        or "MODFLOW archetype solver did not complete"
                    ),
                }
            run_outputs_uri = (
                run_result.output_uri or f"s3://{_runs_prefix()}/{run_result.run_id}/"
            )

        # --- Step 2b (PRT only): run the backward-tracking PRT sim -----------
        # After the GWF run, ``build_and_run_prt_from_gwf`` reverses the GWF
        # outputs (head + cbc), writes + runs the PRT sim, and returns the PRT
        # working directory.  The capture-zone postprocess then reads
        # ``prtmodel.trk.csv`` from that directory.
        #
        # We need a ``DeckManifest`` (from the gwt_adapter) to feed to
        # ``build_and_run_prt_from_gwf``. ``build_and_stage_modflow_deck`` does
        # not carry it forward on ``DeckStaging``, so we reconstruct it via a
        # ``write=False`` call to ``build_modflow_deck``.  This is cheap: all
        # flopy computations run in-memory without touching disk (``write=False``
        # skips the file I/O; ``sim.write_simulation`` is never called).
        if is_prt:
            from trid3nt_server.workflows.modflow.run_modflow import (
                _import_gwt_adapter as _import_adapter,
                _mf6_binary,
                _wellfield_dicts,
                build_modflow_deck as _build_modflow_deck,
            )

            _adapter = _import_adapter()
            gwf_run_dir = Path(run_outputs_uri.replace("file://", ""))

            # Reconstruct the DeckManifest without writing anything to disk.
            # ``staging.local_deck_dir`` is the subdir-organised deck dir; we
            # point ``workdir`` at a sibling temp path so flopy builds the
            # in-memory sim under there (``write=False`` never writes).
            _deck = await asyncio.to_thread(
                _build_modflow_deck,
                spill_location_latlon=run_args.spill_location_latlon,
                contaminant=run_args.contaminant,
                release_rate_kg_s=run_args.release_rate_kg_s,
                duration_days=run_args.duration_days,
                aquifer_k_ms=run_args.aquifer_k_ms,
                porosity=run_args.porosity,
                workdir=Path(staging.local_deck_dir).parent / "prt_manifest",
                write=False,
                archetype=archetype,
                # rebuild the SAME grid (structured | disv_quadrefined)
                # the staged GWF used so the PRT DISV grid matches its GWF field.
                grid_type=getattr(run_args, "grid_type", "structured"),
                well_location_latlon=run_args.well_location_latlon,
                pumping_rate_m3_day=getattr(run_args, "pumping_rate_m3_day", None),
                aquifer_sy=getattr(run_args, "aquifer_sy", None),
                aquifer_ss=getattr(run_args, "aquifer_ss", None),
                sim_years=getattr(run_args, "sim_years", None),
                n_periods=getattr(run_args, "n_periods", None),
                n_particles=getattr(run_args, "n_particles", 16),
                capture_zone_travel_time_years=getattr(
                    run_args, "capture_zone_travel_time_years", None
                ),
                regional_gradient_x=getattr(run_args, "regional_gradient_x", None),
                regional_gradient_y=getattr(run_args, "regional_gradient_y", None),
                # the WELLFIELD + transient + NHD RIV + kriged IC MUST be
                # reconstructed identically here so the PRT phase reads the same
                # prt_well_cells (release rings) + reversed per-period budget the
                # staged GWF deck produced.
                wells=_wellfield_dicts(getattr(run_args, "wells", None)),
                capture_zone_transient=bool(
                    getattr(run_args, "capture_zone_transient", False)
                ),
                river_reaches=getattr(run_args, "river_reaches", None),
                starting_head_by_cell=getattr(run_args, "starting_head_by_cell", None),
            )
            mf6_bin = _mf6_binary()

            prt_track_dir: Path = await asyncio.to_thread(
                _adapter.build_and_run_prt_from_gwf,
                _deck,
                gwf_run_dir,
                mf6_bin,
            )
            prt_outputs_uri = str(prt_track_dir)
            logger.info(
                "run_modflow_archetype_job PRT complete archetype=%s prt_dir=%s",
                archetype,
                prt_outputs_uri,
            )
        else:
            prt_outputs_uri = None

        # --- Step 3: postprocess -> archetype headline layer ------------------
        # PRT archetypes: postprocess_fn receives the PRT track directory.
        # All other archetypes: postprocess_fn receives the GWF output directory.
        _postprocess_uri = prt_outputs_uri if is_prt else run_outputs_uri
        _pp_kwargs: dict[str, Any] = {
            "run_id": staging.run_id,
            "model_crs": staging.model_crs,
            "deck_dir": staging.local_deck_dir,
        }
        if is_prt:
            # The PRT GWF grid is built at LOCAL (0,0) origin (mf6 6.7.0 float-
            # precision workaround), so the true UTM origin is NOT recoverable
            # from any on-disk file -- thread it (and the user-requested isochrone
            # tiers) explicitly from the in-memory DeckManifest + run_args, or
            # postprocess would land the polygon at the equator / fabricate tiers.
            _pp_kwargs["xoffset_m"] = getattr(_deck, "xoffset_m", None)
            _pp_kwargs["yoffset_m"] = getattr(_deck, "yoffset_m", None)
            _pp_kwargs["model_utm_epsg"] = getattr(_deck, "model_utm_epsg", None)
            _pp_kwargs["tier_years"] = getattr(
                run_args, "capture_zone_travel_time_years", None
            ) or getattr(_deck, "capture_zone_travel_time_years", None)
            # Georeferenced-mode narration: the gradient provenance + the aquifer
            # params the Grubb analytic (capture width / stagnation distance) reads,
            # all from the authoritative in-memory manifest. K converts m/s -> m/day;
            # b is the confined PRT aquifer thickness (top 50 m - bottom 0 m); Q is
            # the WEL magnitude (stored negative on the manifest).
            _pp_kwargs["gradient_source"] = getattr(_deck, "gradient_source", None)
            _pp_kwargs["gradient_magnitude"] = getattr(_deck, "gradient_magnitude", None)
            _pp_kwargs["gradient_azimuth_deg"] = getattr(
                _deck, "gradient_azimuth_deg", None
            )
            _pp_kwargs["k_m_per_day"] = abs(
                float(getattr(_deck, "aquifer_k_ms", 0.0) or 0.0)
            ) * 86400.0
            _pp_kwargs["aquifer_thickness_m"] = 50.0
            _pp_kwargs["pumping_rate_m3_day"] = abs(
                float(getattr(_deck, "pumping_rate_m3_day", 0.0) or 0.0)
            )
            # per-well allocation + transient + NHD RIV narration. The
            # per-well specs (name/lat/lon/rate, deck index order) come from the
            # in-memory manifest so the postprocess can label which well captured
            # which particles from the "W{k}_" boundname prefix.
            _prt_cells = getattr(_deck, "prt_well_cells", None) or []
            _prt_names = getattr(_deck, "prt_well_names", None) or []
            _pp_kwargs["well_specs"] = [
                {
                    "name": (_prt_names[i] if i < len(_prt_names) else f"well_{i}"),
                    "lat": c[3], "lon": c[4], "rate_m3_day": c[2],
                }
                for i, c in enumerate(_prt_cells)
            ]
            _pp_kwargs["transient"] = bool(getattr(_deck, "transient", False))
            _pp_kwargs["river_cell_count"] = int(
                getattr(_deck, "prt_river_cell_count", 0) or 0
            )
        if archetype == "vadose_transport":
            # 1D-column deck: the postprocess reads the breakthrough obs csv but
            # has no plan-view grid to georeference the spill point, so thread the
            # spill (lat, lon) from the staging + the thickness/infiltration-conc
            # from run_args (the FLAT worker-contract manifest carries neither).
            _pp_kwargs["spill_lat"] = float(getattr(staging, "spill_lat", 0.0) or 0.0)
            _pp_kwargs["spill_lon"] = float(getattr(staging, "spill_lon", 0.0) or 0.0)
            _pp_kwargs["vadose_thickness_m"] = getattr(
                run_args, "vadose_thickness_m", None
            )
            _pp_kwargs["vadose_infiltration_conc"] = getattr(
                run_args, "vadose_infiltration_conc", None
            )
        layer: LayerURI = await asyncio.to_thread(
            lambda: postprocess_fn(_postprocess_uri, **_pp_kwargs)
        )

        # Honesty floor: a "modeled" archetype layer with an empty deliverable
        # must NOT read as a successful layer. The budget partition is empty when
        # the CBC had no non-trivial source/sink term; drawdown is zero when the
        # well drew nothing; dewatering is zero when the drains removed nothing.
        headline = getattr(layer, headline_attr, None)
        if archetype in _NON_SCALAR_HEADLINES:
            empty = not headline  # an empty partition dict / empty head series
        else:
            empty = not headline or float(headline) <= 0.0
        if empty:
            return {
                "status": "error",
                "error_code": "MODFLOW_ARCHETYPE_EMPTY_RESULT",
                "error_message": (
                    f"the {archetype} run produced no non-trivial result "
                    f"({headline_attr}={headline!r}); check the well / pit / "
                    "gradient forcing. No layer was loaded."
                ),
            }

        logger.info(
            "run_modflow_archetype_job complete archetype=%s run_id=%s %s=%s uri=%s",
            archetype,
            staging.run_id,
            headline_attr,
            headline,
            layer.uri,
        )
        return layer

    except asyncio.CancelledError:
        raise
    except (MODFLOWWorkflowError, PostprocessMODFLOWError) as exc:
        logger.warning(
            "run_modflow_archetype_job failed: %s (%s)", exc.error_code, exc
        )
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except ValueError as exc:
        # The adapter raises a ValueError when a required per-archetype field is
        # missing (the engine-side backstop to the composer honesty gate).
        logger.warning("run_modflow_archetype_job input error: %s", exc)
        return {
            "status": "error",
            "error_code": "MODFLOW_ARCHETYPE_INPUT_INVALID",
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001  -  defensive catch-all
        logger.exception("run_modflow_archetype_job unexpected failure")
        return {
            "status": "error",
            "error_code": "MODFLOW_ARCHETYPE_INTERNAL_ERROR",
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
