"""Engine template ``hecras_riverine_flood`` -- HEC-RAS 6.x 1D/2D riverine-flood
engine.

The LLM-facing exposure of the HEC-RAS refinement-grade riverine solver. The tool
is named for the CAPABILITY (1D/2D riverine flood), not the place. Its V1 SHIPPED
GEOMETRY is a labeled fact: HEC's own Muncie project (White River, Muncie IN).
Headless 2D mesh authoring is the frontier (RASMapper's terrain subgrid tables need
Windows DLLs), so v1 reparameterizes that shipped project rather than building
geometry for an arbitrary AOI. The GEOMETRY is FROZEN; only the unsteady FLOW
forcing varies (the inflow hydrograph, scaled by a plain multiplier or to a target
peak discharge). Future geometry sources widen the same capability tool.

``hecras_riverine_flood(...)`` takes flow knobs (all defaulted so a bare "run the
HEC-RAS riverine flood model" runs the published Muncie-v1 baseline), runs the deterministic
stage -> flow-scale -> RasGeomPreprocess -> RasUnsteady -> postprocess chain
(``model_hecras_riverine_flood`` below), and returns a ``HecrasDepthLayerURI`` the
emitter loads onto the map (peak overland-depth COG) beside the 2D mesh preview.

DEMONSTRATION-GEOMETRY honesty is LOUD (NATE no-hand-wave doctrine): this template
answers "what-if flow on the Muncie White River demonstration model", NOT "flood
any user AOI" -- said in the docstring AND stamped on the result envelope's
``fallback_note``. Off-scope arbitrary-AOI riverine/coastal flooding ->
``sfincs_flood``.

HEC-RAS is LOCAL-DOCKER ONLY (the Linux engines live in the worker image), so the
composer always dispatches through the generic run_solver seam. Determinism
boundary (invariant 1): every depth number the agent narrates comes from the typed
``HecrasDepthLayerURI`` fields the postprocess computed -- never free-generated.
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
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.hecras_contracts import (
    HECRAS_INPUT_INVALID,
    HECRAS_SOLVE_FAILED,
    HecrasDepthLayerURI,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.gates.input_review import gate_input_review
from trid3nt_server.workflows.hecras._template_card import TemplateCard

logger = logging.getLogger(
    "trid3nt_server.workflows.hecras.riverine_flood.riverine_flood"
)

__all__ = [
    "hecras_riverine_flood",
    "model_hecras_riverine_flood",
    "HecrasScenarioError",
    "TEMPLATE_CARD",
]

#: The baked demonstration deck's baseline PEAK inflow (cfs) -- the Muncie White
#: River unsteady hydrograph peaks at 21000 cfs (verified in-container). Used to
#: narrate the forcing when the user passes a plain multiplier; the worker recomputes
#: the true baseline from the deck so this is a narration aid, never the source of
#: truth for the deck edit.
_MUNCIE_BASELINE_PEAK_CFS: float = 21000.0

#: The LOUD demonstration-geometry honesty floor stamped on every result envelope.
_DEMO_GEOMETRY_NOTE: str = (
    "DEMONSTRATION GEOMETRY: this is HEC's shipped Muncie White River (Muncie, IN) "
    "1D/2D unsteady model with FROZEN terrain/geometry -- only the inflow flow "
    "forcing was reparameterized. It answers a what-if flow question on THIS "
    "demonstration reach, NOT flooding at an arbitrary user AOI (real-AOI HEC-RAS "
    "awaits headless 2D geometry authoring). For arbitrary-AOI flooding use "
    "sfincs_flood."
)


class HecrasScenarioError(RuntimeError):
    """Raised when the HEC-RAS chain fails fatally before producing a layer.

    Carries an open-set ``error_code`` propagated to the agent emitter so the
    failure renders a typed error frame (never a silent dead-end)."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


#: Curated door-listing card. One-line question + the real required input (none --
#: the demonstration deck is self-contained) + a knobs summary.
TEMPLATE_CARD = TemplateCard(
    question=(
        "peak 2D inundation depth + water surface for a WHAT-IF FLOW on the HEC-RAS "
        "1D/2D riverine model (refinement-grade riverine flood; scale the inflow "
        "hydrograph up/down). V1 geometry: HEC's shipped Muncie White River (Indiana) "
        "project, FROZEN demonstration geometry"
    ),
    required_inputs=[],  # self-contained demonstration deck
    knobs="flow_scale, target_peak_cfs, input_mode",
)


_HECRAS_MUNCIE_METADATA = AtomicToolMetadata(
    name="hecras_riverine_flood",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="hecras",
    tier="template",
)


@register_tool(
    _HECRAS_MUNCIE_METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def hecras_riverine_flood(
    flow_scale: float = 1.0,
    target_peak_cfs: float | None = None,
    run_demo_geometry: bool = False,
    input_mode: str | None = None,
    # absorb LLM-invented kwargs (centralized at server.py via
    # tool_arg_normalizer, but kept as belt-and-suspenders).
    **_extra_ignored: Any,
) -> HecrasDepthLayerURI | dict[str, Any]:
    """A REFINEMENT-GRADE 1D/2D RIVERINE FLOOD what-if on the HEC-RAS solver (V1 geometry: Muncie White River).

    Fidelity: HEC-RAS 6.x full-physics 1D/2D unsteady hydraulics (the FEMA/USACE
    refinement-grade riverine solver), Linux-verified bit-identical to the GUI. The
    V1 SHIPPED GEOMETRY is HEC's OWN Muncie test project (White River, Muncie IN):
    terrain + 2D mesh are FROZEN (demonstration geometry -- RASMapper's subgrid
    tables are prebuilt and cannot be rebuilt headless); this answers a WHAT-IF FLOW
    question on THIS reach, NOT flooding at an arbitrary user AOI.

    THE tool for "run the HEC-RAS riverine flood model", "run HEC-RAS 1D/2D unsteady
    flow", "what does the White River at Muncie flood look like at a higher flow / a
    bigger event", "scale up the inflow and show the deeper/wider 2D inundation".
    Reparameterizes the
    unsteady inflow hydrograph (a plain multiplier OR a target peak discharge),
    runs RasGeomPreprocess + RasUnsteady headless, and returns a peak overland-DEPTH
    map layer (the max water surface minus the bed at each 2D cell) plus the 2D
    flow-area computational mesh preview.

    Do NOT use this for:
        - Flooding at an ARBITRARY user AOI / any place the user names (this is a
          FROZEN Muncie demonstration reach) -- use ``sfincs_flood`` (SFINCS) for
          arbitrary-AOI riverine/coastal/pluvial flooding, or ``swmm_urban_flood``
          for urban drainage.
        - A dye/contaminant plume traveling downstream (``telemac_river_dye``),
          groundwater (``modflow_*``), or tsunami/dam-break run-up
          (``geoclaw_inundation``).

    Params:
        flow_scale: multiply the baseline Muncie inflow hydrograph (peaks ~21000
            cfs) by this factor. ``1.0`` (default) runs the published baseline;
            ``> 1`` a higher-flow "what-if" (deeper + wider inundation), ``< 1`` a
            lower-flow scenario. Set from user intent ("a 30% bigger flood" ->
            1.3, "double the flow" -> 2.0). Clamped [0.25, 4.0] (the frozen
            demonstration geometry is only faithful within a modelable band).
        target_peak_cfs: OPTIONAL alternative to ``flow_scale`` -- a target PEAK
            inflow discharge in cfs. The worker derives the multiplier from the
            baseline peak, so a user (or a USGS-gauge / NWM peak) can pin the
            forcing to a real discharge. Overrides ``flow_scale``.
        run_demo_geometry: EXPLICIT opt-in (default ``False``) to run HEC's baked
            Muncie White River demonstration geometry. This template models ONLY
            that frozen reach -- it is NOT a model of any AOI you name. Left False
            the run REFUSES with a typed ``HECRAS_DEMO_GEOMETRY_REQUIRED`` error
            (law 9: foreign geometry is never a silent answer to a place-named
            request). Set True to run the demonstration explicitly; outputs are
            banner-labeled DEMONSTRATION GEOMETRY.
        input_mode: run-mode lever. ``"user_gated"`` presents the
            resolved flow forcing + the frozen-geometry note for review before the
            solve; ``"auto"`` (default) proceeds with them labeled.

    Returns:
        On success: ``HecrasDepthLayerURI`` (``LayerURI`` subtype) -- the emitter
        loads the peak-depth COG onto the map beside the 2D mesh preview. Carries
        ``depth_max_ft`` / ``depth_mean_ft`` / ``wet_cell_count`` / ``wse_max_ft``
        / ``flow_scale`` / ``peak_inflow_cfs`` / ``volume_error_pct`` (narrate
        these typed numbers only -- invariant 1) + a LOUD demonstration-geometry
        ``fallback_note``.
        On failure: dict with ``status="error"`` + ``error_code`` + ``error_message``.

    ``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="workflow_dispatch"`` -- cache shim not invoked.
    """
    # --- arg hardening (defensive; mirrors the other engine templates) -------- #
    try:
        flow_scale = float(flow_scale)
    except (TypeError, ValueError):
        flow_scale = 1.0
    if flow_scale != flow_scale or not (flow_scale > 0.0):  # NaN / non-positive
        return {
            "status": "error",
            "error_code": HECRAS_INPUT_INVALID,
            "error_message": f"flow_scale must be a positive finite number, got {flow_scale!r}",
        }
    if not (0.25 <= flow_scale <= 4.0):
        logger.warning("hecras_riverine_flood: flow_scale %r outside [0.25, 4.0] - clamped", flow_scale)
        flow_scale = min(max(flow_scale, 0.25), 4.0)

    tp: float | None = None
    if target_peak_cfs is not None:
        try:
            tp = float(target_peak_cfs)
        except (TypeError, ValueError):
            tp = None
        else:
            if not (tp > 0.0):
                tp = None

    logger.info(
        "hecras_riverine_flood flow_scale=%.4g target_peak_cfs=%s input_mode=%s",
        flow_scale, tp, input_mode,
    )

    try:
        depth = await model_hecras_riverine_flood(
            flow_scale=flow_scale,
            target_peak_cfs=tp,
            run_demo_geometry=bool(run_demo_geometry),
            input_mode=input_mode,
        )
        if isinstance(depth, dict):  # a gate cancel returns a typed dict
            return depth
        logger.info(
            "hecras_riverine_flood complete layer_id=%s depth_max_ft=%.3g wet_cells=%s "
            "peak_cfs=%s uri=%s",
            depth.layer_id, depth.depth_max_ft, depth.wet_cell_count,
            depth.peak_inflow_cfs, depth.uri,
        )
        return depth
    except asyncio.CancelledError:
        raise
    except HecrasScenarioError as exc:
        logger.warning("hecras_riverine_flood failed: %s (%s)", exc.error_code, exc)
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("hecras_riverine_flood unexpected failure")
        return {
            "status": "error",
            "error_code": "HECRAS_INTERNAL_ERROR",
            "error_message": str(exc),
        }


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)
from trid3nt_server.emission.publish import (
    PublishLayerError,
    publish_layer,
)
from trid3nt_server.emission.layer_uri_emit import publish_input_layer
from trid3nt_server.workflows.hecras._frame_emit import read_and_emit_hecras_frames
from trid3nt_server.workflows.hecras.postprocess_hecras import (
    PostprocessHecrasError,
    postprocess_hecras,
)
from trid3nt_server.workflows.hecras.run_hecras import HECRAS_SOLVER_NAME


def _stage_manifest(flow_scale: float, target_peak_cfs: float | None, run_tag: str) -> str:
    """Write the ``hecras_riverine_flood`` worker manifest to the cache bucket and
    return its ``s3://`` URI (``run_solver`` downloads it to the rundir).

    The Muncie deck is BAKED in the worker image (frozen demonstration geometry),
    so ``inputs`` is empty -- the manifest carries only the archetype + the flow
    knobs. ``outputs`` globs the solved plan HDF (Results appended in place) + the
    metrics so the supervisor uploads them and the postprocess can read them."""
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    cache_bucket = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not cache_bucket:
        raise HecrasScenarioError(
            HECRAS_SOLVE_FAILED,
            "TRID3NT_CACHE_BUCKET must be set to stage the HEC-RAS manifest.",
        )
    manifest: dict[str, Any] = {
        "archetype": "muncie_riverine_flood",
        "run_id": run_tag,
        "flow_scale": float(flow_scale),
        "inputs": [],  # the deck is baked in the image
        "hecras_args": [],  # the image ENTRYPOINT drives
        "outputs": ["*.p04.tmp.hdf", "hecras_metrics.json"],
    }
    if target_peak_cfs is not None:
        manifest["target_peak_cfs"] = float(target_peak_cfs)
    key = f"hecras/{run_tag}/manifest.json"
    _get_s3_client().put_object(
        Bucket=cache_bucket,
        Key=key,
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{cache_bucket}/{key}"


def _read_run_metrics(run_id: str) -> dict[str, Any]:
    """Best-effort read of ``<run_id>/hecras_metrics.json`` from the runs bucket.

    Returns ``{}`` on any miss. The worker uploads this file even on a failed run,
    so it is the channel through which the flow-forcing provenance + a worker-side
    volume-accounting figure reach the composer."""
    from trid3nt_server.workflows.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
    )

    try:
        s3 = _get_s3_client()
        obj = s3.get_object(Bucket=_get_runs_bucket(), Key=f"{run_id}/hecras_metrics.json")
        loaded = json.loads(obj["Body"].read().decode("utf-8"))
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logger.info("hecras: run metrics read miss for %s: %s", run_id, exc)
        return {}


def _download_plan_hdf(run_id: str) -> str:
    """Download the solved plan HDF (``*.p04.tmp.hdf``) for a completed run.

    Returns the local path. Raises ``HecrasScenarioError`` when the plan HDF is
    missing (a completed run with no result is a failure, not an empty success)."""
    from trid3nt_server.workflows.solver.solver import (
        _get_runs_bucket,
        _get_s3_client,
    )

    runs_bucket = _get_runs_bucket()
    s3 = _get_s3_client()
    # The solved plan HDF filename is the baked deck's (Muncie.p04.tmp.hdf); read
    # it from the metrics if present, else the known Muncie filename.
    metrics = _read_run_metrics(run_id)
    plan_name = str(metrics.get("plan_hdf") or "Muncie.p04.tmp.hdf")
    key = f"{run_id}/{plan_name}"
    tmp_dir = tempfile.mkdtemp(prefix=f"hecras-{run_id}-")
    local = str(Path(tmp_dir) / plan_name)
    try:
        resp = s3.get_object(Bucket=runs_bucket, Key=key)
        with open(local, "wb") as fh:
            fh.write(resp["Body"].read())
    except Exception as exc:  # noqa: BLE001
        raise HecrasScenarioError(
            HECRAS_SOLVE_FAILED,
            f"HEC-RAS run {run_id} completed but s3://{runs_bucket}/{key} was not "
            f"downloadable: {exc}",
        ) from exc
    return local


async def model_hecras_riverine_flood(
    *,
    flow_scale: float = 1.0,
    target_peak_cfs: float | None = None,
    run_demo_geometry: bool = False,
    input_mode: str | None = None,
) -> HecrasDepthLayerURI | dict[str, Any]:
    """Stage -> flow-scale -> solve -> postprocess -> publish the Muncie flood.

    Returns the ``HecrasDepthLayerURI`` on success, or a typed cancel dict when the
    input-review gate declines. Raises ``HecrasScenarioError`` on a fatal solve /
    postprocess fault."""
    emitter = current_emitter()
    begin_substeps(emitter, 3)  # run_solver + postprocess + publish

    # --- demo-geometry opt-in (law 9, audit row 14): the baked Muncie reach is
    # foreign to any AOI the user names, so it never answers a place-named request
    # silently. Running it is an EXPLICIT choice (the pahm_surge allow_synthetic_
    # domain precedent); left un-opted-in the run REFUSES before touching the solver.
    if not run_demo_geometry:
        return {
            "status": "error",
            "error_code": "HECRAS_DEMO_GEOMETRY_REQUIRED",
            "error_message": (
                "hecras_riverine_flood solves ONLY HEC's baked Muncie White River "
                "(Muncie, IN) demonstration geometry -- it is NOT a model of any AOI "
                "you name. Pass run_demo_geometry=True to explicitly run the Muncie "
                "demonstration (outputs are banner-labeled DEMONSTRATION GEOMETRY), "
                "or use sfincs_flood for a real place-named flood."
            ),
        }

    # --- Stage 1: the input-review gate ---------------------------- #
    # The physically dominant, otherwise-buried input is the FLOW forcing (the
    # multiplier or the derived peak) + the frozen-geometry basis. Surface both as
    # structured provenance so a demo default is never mistaken for a site study.
    peak_est = (target_peak_cfs if target_peak_cfs is not None
                else _MUNCIE_BASELINE_PEAK_CFS * flow_scale)
    review_entries: list[SyntheticInput] = [
        SyntheticInput(
            param="flow_scale", value=round(float(flow_scale), 4), units="x",
            basis="user" if (flow_scale != 1.0 or target_peak_cfs is not None) else "default_demo", consequence="scenario",
            note="inflow-hydrograph multiplier on the baseline Muncie event (~21000 cfs peak)",
        ),
        SyntheticInput(
            param="peak_inflow_cfs", value=round(float(peak_est), 1), units="cfs",
            basis="user" if target_peak_cfs is not None else "derived",
            real_source_if_any=None,
            note="peak inflow the scaled hydrograph forces the run with",
        ),
        SyntheticInput(
            param="geometry", value="Muncie White River (IN) demonstration model",
            basis="default_demo", consequence="scenario",
            note="DEMONSTRATION GEOMETRY (opt-in via run_demo_geometry=True): HEC's "
                 "FROZEN shipped Muncie White River 1D/2D reach -- terrain + mesh "
                 "unchanged, NOT a user AOI",
        ),
    ]
    review = await gate_input_review(
        tool_name="hecras_riverine_flood",
        mode=input_mode,
        entries=review_entries,
        params={"flow_scale": flow_scale, "target_peak_cfs": target_peak_cfs},
    )
    if not review.proceed:
        return {
            "status": "error",
            "error_code": "HECRAS_INPUT_REVIEW_CANCELLED",
            "error_message": review.cancel_reason or "input review not approved; the solver did not run",
        }
    # A revision may have adjusted the flow knobs.
    flow_scale = float(review.params.get("flow_scale", flow_scale) or flow_scale)
    _tp = review.params.get("target_peak_cfs", target_peak_cfs)
    target_peak_cfs = float(_tp) if _tp is not None else None

    # --- Stage 2: stage the worker manifest ----------------------------------- #
    run_tag = new_ulid()
    manifest_uri = await asyncio.to_thread(_stage_manifest, flow_scale, target_peak_cfs, run_tag)
    logger.info(
        "model_hecras_riverine_flood staged manifest run_tag=%s flow_scale=%.4g target_peak=%s uri=%s",
        run_tag, flow_scale, target_peak_cfs, manifest_uri,
    )

    # --- Stage 3: dispatch to the solver (generic run_solver seam) ------------- #
    from trid3nt_server.workflows.solver.solver import (
        run_solver,
        wait_for_completion,
    )

    handle = run_solver(
        solver=HECRAS_SOLVER_NAME,
        model_setup_uri=manifest_uri,
        compute_class="medium",
    )
    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=HECRAS_SOLVER_NAME, handle=handle, compute_class="medium",
    )
    run_result = None
    try:
        async with substep(emitter, "run_solver"):
            run_result = await wait_for_completion(handle)
    except asyncio.CancelledError:
        await route_sim_terminal(emitter, sim_step_id, run_result=None)
        raise
    await route_sim_terminal(emitter, sim_step_id, run_result=run_result)

    if run_result is None or run_result.status != "complete":
        raise HecrasScenarioError(
            HECRAS_SOLVE_FAILED,
            "HEC-RAS solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)}): "
            f"{getattr(run_result, 'error_message', '') or getattr(run_result, 'cancellation_reason', '') or ''}",
        )

    batch_run_id = getattr(run_result, "run_id", None) or run_tag
    metrics = await asyncio.to_thread(_read_run_metrics, batch_run_id)
    eff_scale = float(metrics.get("flow_scale", flow_scale) or flow_scale)
    peak_cfs = metrics.get("peak_inflow_cfs")
    va = metrics.get("volume_accounting") or {}
    try:
        vol_err = float(va.get("Error Percent")) if va.get("Error Percent") is not None else None
    except (TypeError, ValueError):
        vol_err = None

    # --- Stage 4: download the solved plan HDF + postprocess ------------------ #
    plan_path = await asyncio.to_thread(_download_plan_hdf, batch_run_id)
    try:
        async with substep(emitter, "postprocess_hecras"):
            layers, pp_metrics = await asyncio.to_thread(
                postprocess_hecras,
                plan_path,
                run_id=batch_run_id,
                flow_scale=eff_scale,
                peak_inflow_cfs=(float(peak_cfs) if peak_cfs is not None else None),
                volume_error_pct=vol_err,
                fallback_note=_DEMO_GEOMETRY_NOTE,
            )
    except PostprocessHecrasError as exc:
        raise HecrasScenarioError(exc.error_code, str(exc)) from exc
    finally:
        try:
            Path(plan_path).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    if not layers:
        raise HecrasScenarioError(
            HECRAS_SOLVE_FAILED, "postprocess_hecras produced no depth layer"
        )
    depth = layers[0]
    assert isinstance(depth, HecrasDepthLayerURI)
    mesh_layer = layers[1] if len(layers) > 1 else None

    # --- Stage 5: publish the peak-depth COG (render chokepoint) -------------- #
    async with substep(emitter, "publish_layer"):
        depth = await asyncio.to_thread(_publish_depth_layer, depth, review_entries)

    # --- Best-effort: surface the 2D mesh preview beside the result ----------- #
    if mesh_layer is not None:
        try:
            await publish_input_layer(emitter, mesh_layer, role="context")
        except Exception as exc:  # noqa: BLE001
            logger.warning("hecras mesh preview emit skipped: %s", exc)

    # --- Best-effort: the inflow hydrograph forcing chart --------------------- #
    if emitter is not None:
        try:
            await _maybe_emit_inflow_chart(emitter, pp_metrics, eff_scale)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hecras inflow chart skipped: %s", exc)

    # --- Best-effort: the per-step depth animation (ADR 0287 seam) ------------ #
    # postprocess_hecras wrote outputs.json host-side; read it back frames_only and
    # emit the flood_depth group beside the peak. A miss degrades to peak-only.
    await read_and_emit_hecras_frames(
        emitter, run_id=batch_run_id,
        bbox=tuple(depth.bbox) if depth.bbox else None,
    )

    # --- AUTHORITATIVE LAST zoom-to ------------------------------------------- #
    if emitter is not None and depth.bbox:
        try:
            await emitter.emit_map_command("zoom-to", {"bbox": list(depth.bbox)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("hecras zoom-to failed: %s", exc)

    logger.info(
        "model_hecras_riverine_flood complete run_id=%s depth_max_ft=%.3g wet_cells=%s "
        "flow_scale=%.3g peak_cfs=%s vol_err=%s uri=%s",
        batch_run_id, depth.depth_max_ft, depth.wet_cell_count, eff_scale,
        depth.peak_inflow_cfs, depth.volume_error_pct, depth.uri,
    )
    return depth


def _publish_depth_layer(
    depth: HecrasDepthLayerURI, synthetic_inputs: list[SyntheticInput]
) -> HecrasDepthLayerURI:
    """Publish the peak-depth COG through publish_layer (render chokepoint) and
    stamp the structured provenance. On publish failure the raw layer is returned
    UNCHANGED (the raw s3:// COG still renders via the dispatch guardrail)."""
    out = depth
    if synthetic_inputs:
        try:
            out = out.model_copy(update={"synthetic_inputs": list(synthetic_inputs)})
        except Exception:  # noqa: BLE001
            pass
    try:
        published_uri = publish_layer(
            layer_uri=out.uri,
            layer_id=out.layer_id,
            style_preset=out.style_preset,
        )
        return out.model_copy(update={"uri": published_uri})
    except PublishLayerError as exc:
        logger.warning(
            "model_hecras_riverine_flood: publish_layer FAILED layer_id=%s (%s) - "
            "returning the raw COG", out.layer_id, exc,
        )
        return out


async def _maybe_emit_inflow_chart(
    emitter: Any, metrics: dict[str, Any], flow_scale: float
) -> None:
    """Best-effort inflow-hydrograph forcing chart (time vs the scaled flow).

    Every point is the real Event Conditions hydrograph time scaled by the run's
    flow multiplier (invariant 1 -- never synthesized). Non-blocking: a chart
    failure never voids the map deliverable. The chart is HEC's own output-plot
    analogue (the inflow forcing the solve ran)."""
    if not hasattr(emitter, "emit_chart"):
        return
    series = metrics.get("inflow_hydrograph") or []
    if not series:
        return
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    values = [{"time_hr": p["t_hr"], "inflow_cfs": p["q_cfs"]} for p in series]
    spec = {
        "data": {"values": values},
        "mark": {"type": "line", "point": True, "color": "#1f5fbf"},
        "encoding": {
            "x": {"field": "time_hr", "type": "quantitative", "title": "time (hours)"},
            "y": {"field": "inflow_cfs", "type": "quantitative", "title": "inflow (cfs)"},
        },
    }
    payload = build_chart_payload(
        vega_lite_spec=spec,
        title=f"Muncie White River inflow hydrograph (flow_scale {flow_scale:.2g})",
        caption=(
            "The unsteady inflow forcing the HEC-RAS solve ran with -- the baseline "
            "Muncie event hydrograph scaled by the flow multiplier."
        ),
    )
    await emitter.emit_chart(payload)
