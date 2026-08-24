"""Engine template ``geoclaw_thacker_validation`` -- GeoClaw wet-dry SWE+AMR
verification against Thacker's (1981) exact paraboloid-basin solution.

A DISTINCT question CLASS from ``geoclaw_inundation`` (per the capability-naming
rule): rather than a geographic hazard run-up MAP, this asks *"does the GeoClaw
wet-dry shallow-water + AMR solver reproduce the closed-form Thacker oscillation
(period / amplitude / moving shoreline) and conserve mass in a frictionless closed
basin?"* -- a numerical-verification question, NOT a hazard target. It is a
SYNTHETIC, NON-GEOGRAPHIC, idealized V&V (a lab-scale paraboloid bowl in planar
Cartesian metres), so it emits CHARTS + SCALARS only, never a basemap layer.

The run is entirely worker-generated (no fetched DEM): the worker writes the
paraboloid bed + the analytic still-surface qinit from ``bowl_a_m`` / ``bowl_h0_m``
/ ``bowl_eta_amp`` (the DEM-free composer branch), solves the frictionless
closed-wall bowl, and this module grades the numerical center-gauge + shoreline
against ``trid3nt_contracts.geoclaw_thacker`` (the shared closed form the deck is
built from, so the two agree by construction).

Determinism boundary (Invariant 1): every V&V number the agent narrates
(period / amplitude / shoreline / mass-drift errors) is computed with plain
arithmetic from the gauge + fort.q output vs the closed form -- never free-generated.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.geoclaw_contracts import GeoClawRunArgs
from trid3nt_contracts.geoclaw_thacker import thacker_reference
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.geoclaw._template_card import TemplateCard
from trid3nt_server.workflows.geoclaw.postprocess_geoclaw import (
    build_thacker_validation_chart_spec,
    compute_thacker_vandv,
)
from trid3nt_server.workflows.geoclaw.run_geoclaw import (
    GEOCLAW_SOLVER_NAME,
    GeoClawWorkflowError,
    stage_geoclaw_manifest,
)
from trid3nt_server.workflows.geoclaw.inundation.inundation import (
    GeoClawComposerError,
    _cleanup_dir,
    _download_batch_geoclaw_outputs,
)
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.geoclaw.thacker_validation.thacker_validation"
)

__all__ = ["geoclaw_thacker_validation", "model_geoclaw_thacker_validation"]


TEMPLATE_CARD = TemplateCard(
    question=(
        "VERIFY the GeoClaw wet-dry shallow-water + AMR solver against Thacker's "
        "1981 exact paraboloid-basin solution -- period, central amplitude, moving "
        "shoreline, and closed-basin mass conservation (a synthetic, non-geographic "
        "V&V, NOT a hazard target)"
    ),
    required_inputs=[],
    knobs=(
        "bowl_a_m (basin radius), bowl_h0_m (central depth), bowl_eta_amp "
        "(oscillation amplitude A in (0,1)), n_periods, amr_levels, base_cells, "
        "output_frames"
    ),
)


_METADATA = AtomicToolMetadata(
    name="geoclaw_thacker_validation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="geoclaw",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def geoclaw_thacker_validation(
    bowl_a_m: float = 1.0,
    bowl_h0_m: float = 0.1,
    bowl_eta_amp: float = 0.5,
    n_periods: float = 2.5,
    amr_levels: int = 3,
    base_cells: int = 60,
    output_frames: int = 24,
    compute_class: str = "small",
    # absorb LLM-invented kwargs (centralized at server.py; belt-and-suspenders).
    **_extra_ignored: Any,
) -> dict[str, Any]:
    """Verify GeoClaw's wet-dry SWE+AMR solver against Thacker's exact bowl solution.

    Fidelity: an idealized, frictionless, closed-wall paraboloid-basin V&V (Thacker
    1981) in PLANAR Cartesian metres -- a numerical-verification benchmark, NOT a
    geographic hazard scenario. The worker GENERATES the bowl topography + the
    analytic still-surface initial condition (NO fetched DEM); this grades the
    numerical run against the closed form.
    Off-scope: any real geographic run-up (tsunami / dam-break / surge ->
    geoclaw_inundation); a coastal gauge waveform (geoclaw_tsunami_gauge_timeseries).

    Use this when: the user wants to VERIFY / VALIDATE the shallow-water solver, sees
    a wet-dry / mass-conservation / convergence benchmark, or asks for the Thacker
    (parabolic bowl / oscillating-basin) analytic test case. Do NOT use for a real
    place -- it has no AOI.

    Params:
        bowl_a_m: basin radius a (m) -- the still-water shoreline radius (default 1.0).
        bowl_h0_m: central still-water depth h0 (m) at r=0 (default 0.1).
        bowl_eta_amp: Thacker dimensionless oscillation amplitude A in (0, 1)
            (default 0.5); larger A = a stronger slosh + wider shoreline excursion.
        n_periods: number of analytic periods to simulate (default 2.5).
        amr_levels: AMR refinement levels (default 3).
        base_cells: base computational grid cells per side (default 60).
        output_frames: fort.q output frames for the mass integral (default 24).
        compute_class: compute class (default "small" -- a lab-scale bowl).

    Returns:
        A dict with ``status="ok"`` and the V&V scalars: ``period_s_numerical`` vs
        ``period_s_analytic`` (+ ``period_error_pct``), the central-amplitude,
        moving-shoreline (``r_shore_min/max``), and ``mass_drift_pct`` errors, plus
        ``rms_eta_m``. A numerical-vs-analytic center-elevation overlay chart is
        emitted to the charts window. On failure:
        ``{"status": "error", "error_code", "error_message"}``.
    """
    if not (0.0 < float(bowl_eta_amp) < 1.0):
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INVALID",
            "error_message": (
                f"bowl_eta_amp must be in (0, 1), got {bowl_eta_amp}"
            ),
        }
    if float(bowl_a_m) <= 0.0 or float(bowl_h0_m) <= 0.0:
        return {
            "status": "error",
            "error_code": "GEOCLAW_PARAMS_INVALID",
            "error_message": "bowl_a_m and bowl_h0_m must be > 0",
        }

    try:
        result = await model_geoclaw_thacker_validation(
            bowl_a_m=float(bowl_a_m),
            bowl_h0_m=float(bowl_h0_m),
            bowl_eta_amp=float(bowl_eta_amp),
            n_periods=float(n_periods),
            amr_levels=int(amr_levels),
            base_cells=int(base_cells),
            output_frames=int(output_frames),
            compute_class=compute_class,
        )
        logger.info(
            "geoclaw_thacker_validation complete period_err=%.2f%% amp_err=%.2f%% "
            "shoreline_err=%.2f%%/%.2f%% mass_drift=%.2f%% rms=%.4g",
            result.get("period_error_pct", float("nan")),
            result.get("eta_amplitude_error_pct", float("nan")),
            result.get("r_shore_max_error_pct", float("nan")),
            result.get("r_shore_min_error_pct", float("nan")),
            result.get("mass_drift_pct", float("nan")),
            result.get("rms_eta_m", float("nan")),
        )
        return result
    except asyncio.CancelledError:
        raise
    except (GeoClawWorkflowError, GeoClawComposerError) as exc:
        logger.warning("geoclaw_thacker_validation failed: %s", exc)
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "GEOCLAW_INTERNAL_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 -- defensive catch-all
        logger.exception("geoclaw_thacker_validation unexpected failure")
        return {
            "status": "error",
            "error_code": "GEOCLAW_INTERNAL_ERROR",
            "error_message": str(exc),
        }


#: Domain half-width margin (fraction of a) beyond the analytic max shoreline so
#: the closed walls never clip the slosh.
_DOMAIN_MARGIN_A: float = 0.35


async def model_geoclaw_thacker_validation(
    *,
    bowl_a_m: float,
    bowl_h0_m: float,
    bowl_eta_amp: float,
    n_periods: float,
    amr_levels: int,
    base_cells: int,
    output_frames: int,
    compute_class: str = "small",
) -> dict[str, Any]:
    """Compose the DEM-free Thacker V&V chain: stage -> solve -> grade -> chart.

    The DEM-free composer branch: it SKIPS the fetch / reproject / offshore-source
    / flat-ocean chain the geographic composer runs (there is no AOI DEM) and
    stages ONLY the build_spec (the worker generates the bowl topo + analytic
    qinit). The domain is a planar-metres box sized to enclose the analytic
    shoreline excursion with a margin.
    """
    ref = thacker_reference(bowl_a_m, bowl_h0_m, bowl_eta_amp)
    k = (1.0 + bowl_eta_amp) / (1.0 - bowl_eta_amp)
    half = bowl_a_m * (k ** 0.25 + _DOMAIN_MARGIN_A)  # metres domain half-width
    sim_s = float(n_periods) * ref["period_s"]

    emitter = current_emitter()
    begin_substeps(emitter, 3)

    run_args = GeoClawRunArgs(
        bbox=(-half, -half, half, half),  # PLANAR METRES (coordinate_system=1)
        scenario="thacker",
        bowl_a_m=bowl_a_m,
        bowl_h0_m=bowl_h0_m,
        bowl_eta_amp=bowl_eta_amp,
        sim_duration_s=sim_s,
        output_frames=int(output_frames),
        amr_levels=int(amr_levels),
    )

    # --- Stage the DEM-free manifest (worker generates topo + qinit) ---------
    async with substep(emitter, "stage_geoclaw_manifest"):
        staging = await asyncio.to_thread(
            stage_geoclaw_manifest,
            run_args,
            dem_uri="",  # DEM-free: no topo staged
            base_num_cells=(int(base_cells), int(base_cells)),
        )

    # --- Dispatch the local-docker solve -------------------------------------
    from trid3nt_server.workflows.solver.solver import (
        run_solver,
        wait_for_completion,
    )

    handle = run_solver(
        solver=GEOCLAW_SOLVER_NAME,
        model_setup_uri=staging.manifest_uri,
        compute_class=compute_class,
    )
    async with substep(emitter, "run_solver"):
        run_result = await wait_for_completion(handle)
    if run_result.status != "complete":
        raise GeoClawWorkflowError(
            "GEOCLAW_RUN_FAILED",
            message=(
                f"Thacker V&V solve did not complete (status={run_result.status}): "
                f"{getattr(run_result, 'error_message', '') or ''}"
            ),
            details={"run_id": staging.run_id},
        )

    # --- Download outputs + grade against the closed form --------------------
    batch_run_id = getattr(run_result, "run_id", None) or staging.run_id
    out_dir = await asyncio.to_thread(_download_batch_geoclaw_outputs, batch_run_id)
    try:
        async with substep(emitter, "compute_thacker_vandv"):
            vandv = await asyncio.to_thread(
                compute_thacker_vandv, out_dir, bowl_a_m, bowl_h0_m, bowl_eta_amp
            )
    finally:
        _cleanup_dir(out_dir)

    # --- Emit the numerical-vs-analytic overlay chart ------------------------
    await _maybe_emit_thacker_chart(emitter, vandv)

    # Trim the raw series off the narrated result (the chart carries it) + add the
    # synthetic-fixture provenance so the agent never presents this as a real place.
    series = vandv.pop("series", None)
    vandv["status"] = "ok"
    vandv["synthetic_inputs"] = [
        SyntheticInput(
            param="thacker_bowl",
            value=f"a={bowl_a_m} m, h0={bowl_h0_m} m, A={bowl_eta_amp}",
            basis="default_demo", consequence="scenario",
            note=(
                "idealized non-geographic paraboloid-basin V&V (Thacker 1981), NOT a "
                "hazard target -- a solver-verification fixture in planar metres"
            ),
        ).model_dump()
    ]
    vandv["chart_points"] = len((series or {}).get("t", []))
    logger.info(
        "model_geoclaw_thacker_validation run_id=%s a=%.3g h0=%.3g A=%.3g "
        "period_num=%.4g period_ana=%.4g mass_drift=%.3g%%",
        staging.run_id, bowl_a_m, bowl_h0_m, bowl_eta_amp,
        vandv["period_s_numerical"], vandv["period_s_analytic"],
        vandv["mass_drift_pct"],
    )
    return vandv


async def _maybe_emit_thacker_chart(emitter: Any, vandv: dict[str, Any]) -> None:
    """Emit the numerical-vs-analytic center-elevation overlay to the charts window."""
    if emitter is None or not hasattr(emitter, "emit_chart"):
        return
    spec = build_thacker_validation_chart_spec(vandv)
    if spec is None:
        return
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    caption = (
        "Center surface elevation eta(0,t): GeoClaw (numerical) vs Thacker 1981 "
        f"(analytic). period {vandv['period_s_numerical']:.3f}s vs "
        f"{vandv['period_s_analytic']:.3f}s ({vandv['period_error_pct']:.1f}%); "
        f"amplitude {vandv['eta_center_amplitude_numerical_m']:.4f} vs "
        f"{vandv['eta_center_amplitude_analytic_m']:.4f} m "
        f"({vandv['eta_amplitude_error_pct']:.1f}%); shoreline "
        f"{vandv['r_shore_min_numerical_m']:.3f}-{vandv['r_shore_max_numerical_m']:.3f} m "
        f"vs {vandv['r_shore_min_analytic_m']:.3f}-{vandv['r_shore_max_analytic_m']:.3f} m; "
        f"mass drift {vandv['mass_drift_pct']:.1f}% (closed frictionless basin)."
    )
    payload = build_chart_payload(
        vega_lite_spec=spec,
        title="Thacker paraboloid-basin V&V (center gauge)",
        caption=caption,
    )
    try:
        await emitter.emit_chart(payload)
    except Exception as exc:  # noqa: BLE001 -- non-fatal
        logger.warning("thacker V&V chart emit failed: %s", exc)
