"""Engine template ``schism_transport_validation`` -- SCHISM transport-scheme
numerical-mixing V&V.

Advects a temperature FRONT (a conservative scalar) across the idealized
QuarterAnnulus tidal channel TWICE on the hydro-core binary through the identical
M2 flow -- once with the TVD^2 limiter active (per-element ``tvd.prop=1``) and once
with first-order upwind everywhere (``tvd.prop=0``) -- so the difference isolates
the transport scheme's NUMERICAL MIXING. Answers TWO published verification
questions in one run pair:

  * Test_HeatConsv_TVD / Test_HeatConsv_Upwind ("Module needed: None"): how much
    does upwind vs TVD change numerical mixing of a scalar front? -> the variance
    the front retains (sharp TVD vs diffusive upwind).
  * Test_GEN_MassConsv ("Module needed: GEN"): does the domain-integrated tracer
    mass stay conserved? -> the mass-drift sanity gate. The GEN module SPECIFICALLY
    is a full-monty-only build (USE_GEN, every-namelist); the conservative
    temperature tracer demonstrates the SAME mass-conservation mechanism on the
    clean hydro-core binary -- so this template covers the mechanism honestly and
    documents the GEN-module path rather than building it.

The mesh is schematic (planar, non-georeferenced), so the product is the
scheme-contrast CHART + typed scalars, never a map. Every number is plain
arithmetic off the scribed ``temperature`` netCDF (invariant 1). SCHISM is
LOCAL-DOCKER ONLY: both solves dispatch through the generic run_solver seam on the
EXISTING ``trid3nt-local/schism:latest`` hydro-core binary (no image change).
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
from trid3nt_contracts.schism_contracts import (
    SCHISM_INPUT_INVALID,
    SCHISM_SOLVE_FAILED,
    SchismTransportValidationResult,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.schism import deck_authoring
from trid3nt_server.workflows.schism import postprocess_schism as pp
from trid3nt_server.workflows.schism._template_card import TemplateCard
from trid3nt_server.workflows.schism.run_schism import SCHISM_SOLVER_NAME
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    mint_dispatch_and_sim_cards,
    route_sim_terminal,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.schism.transport_validation.transport_validation"
)

__all__ = [
    "schism_transport_validation",
    "model_schism_transport_validation",
    "SchismTransportError",
    "TEMPLATE_CARD",
]

_DEMO_NOTE: str = (
    "VERIFICATION GEOMETRY: a temperature front advected across SCHISM's own "
    "Test_QuarterAnnulus M2 tidal channel (an IDEALIZED, NON-GEOGRAPHIC mesh). The "
    "product is the transport-scheme numerical-mixing CONTRAST (TVD vs upwind) + the "
    "conservative-tracer mass-conservation gate -- a solver-numerics benchmark, not "
    "tides at a real AOI. There is no map layer."
)

_GEN_MODULE_NOTE: str = (
    "The Test_GEN_MassConsv question names SCHISM's GEN module, which is a "
    "full-monty-binary-only build (USE_GEN, and that binary unconditionally "
    "initializes every compiled module -> every namelist required). This template "
    "demonstrates the identical domain-integrated mass-conservation mechanism with "
    "a conservative TEMPERATURE tracer on the clean hydro-core binary (no image "
    "change); the GEN-specific path is documented, not built here."
)


class SchismTransportError(RuntimeError):
    """Raised when the transport-validation chain fails fatally before a result."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


TEMPLATE_CARD = TemplateCard(
    question=(
        "how much does the transport SCHEME (first-order upwind vs the TVD^2 "
        "limiter) change SCHISM's numerical mixing of a scalar front, and does the "
        "conservative tracer conserve domain-integrated mass -- the published "
        "Test_HeatConsv / Test_GEN_MassConsv verification questions"
    ),
    required_inputs=[],  # bundled verification mesh is self-contained
    knobs="sim_days",
)

_METADATA = AtomicToolMetadata(
    name="schism_transport_validation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="schism",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def schism_transport_validation(
    sim_days: float = 2.0,
    **_extra_ignored: Any,
) -> SchismTransportValidationResult | dict[str, Any]:
    """SCHISM transport-scheme NUMERICAL-MIXING + mass-conservation verification.

    Advects a temperature FRONT across SCHISM's QuarterAnnulus M2 tidal channel
    TWICE through the identical flow -- once with the TVD^2 limiter, once with
    first-order upwind -- and reports how much MORE the upwind scheme numerically
    mixes the front (the variance it loses) plus the conservative-tracer
    domain-integrated MASS drift (the numerical-scheme sanity gate). Reproduces the
    published Test_HeatConsv_TVD / Test_HeatConsv_Upwind and Test_GEN_MassConsv
    verification questions on the hydro-core binary.

    THE tool for "compare SCHISM transport schemes", "upwind vs TVD numerical
    mixing", "SCHISM heat conservation test", "does the SCHISM tracer conserve
    mass", "transport scheme accuracy in SCHISM", "numerical diffusion of the
    transport solver". A solver-numerics V&V benchmark on an idealized verification
    mesh -- NOT a georeferenced site study and NOT a tidal-circulation map (use
    ``schism_tidal_hydro`` for tidal elevation at a real AOI).

    Params:
        sim_days: run length in days for BOTH schemes (default 2; ~4 M2 cycles so
            the cumulative numerical-mixing contrast is clear). Clamped [0.5, 5].

    Returns:
        On success: ``SchismTransportValidationResult`` -- carries
        ``tvd_variance_retained_pct`` / ``upwind_variance_retained_pct`` /
        ``excess_mixing_factor`` (the headline: upwind mixes this many times more) /
        ``tvd_mass_drift_pct`` / ``upwind_mass_drift_pct`` / ``validated`` (narrate
        these typed numbers only -- invariant 1). Emits a front-profile comparison
        chart + a variance/mass-over-time chart.
        On failure: dict with ``status="error"`` + ``error_code`` + ``error_message``.

    ``cacheable=False``, ``ttl_class="live-no-cache"``,
    ``source_class="workflow_dispatch"``.
    """
    try:
        sim_days = float(sim_days)
    except (TypeError, ValueError):
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": "sim_days must be a number"}
    if not (0.5 <= sim_days <= 5.0):
        return {"status": "error", "error_code": SCHISM_INPUT_INVALID,
                "error_message": "sim_days must be in [0.5, 5]"}

    logger.info("schism_transport_validation sim_days=%.3g", sim_days)
    try:
        result = await model_schism_transport_validation(sim_days=sim_days)
        if isinstance(result, dict):
            return result
        logger.info(
            "schism_transport_validation complete tvd_ret=%.2f%% upwind_ret=%.2f%% "
            "excess_mix=%s validated=%s",
            result.tvd_variance_retained_pct, result.upwind_variance_retained_pct,
            result.excess_mixing_factor, result.validated,
        )
        return result
    except asyncio.CancelledError:
        raise
    except SchismTransportError as exc:
        logger.warning("schism_transport_validation failed: %s (%s)", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("schism_transport_validation unexpected failure")
        return {"status": "error", "error_code": "SCHISM_INTERNAL_ERROR", "error_message": str(exc)}


# --------------------------------------------------------------------------- #
# The composer.
# --------------------------------------------------------------------------- #
def _cache_bucket() -> str:
    b = (os.environ.get("TRID3NT_CACHE_BUCKET") or "").strip()
    if not b:
        raise SchismTransportError(
            SCHISM_SOLVE_FAILED, "TRID3NT_CACHE_BUCKET must be set to stage the SCHISM manifest."
        )
    return b


def _stage_manifest(deck_files: list[Path], run_tag: str, *, nscribe: int) -> str:
    """Upload the deck files as manifest inputs[]; return the manifest s3 uri."""
    from trid3nt_server.workflows.solver.solver import _get_s3_client

    cache_bucket = _cache_bucket()
    s3 = _get_s3_client()
    inputs = []
    for f in deck_files:
        key = f"schism/{run_tag}/{f.name}"
        with open(f, "rb") as fh:
            s3.put_object(Bucket=cache_bucket, Key=key, Body=fh.read())
        inputs.append({"gs_uri": f"s3://{cache_bucket}/{key}", "dest": f.name})
    manifest = {
        "variant": "hydro",
        "ncompute": 2,
        "nscribe": int(nscribe),
        "run_id": run_tag,
        "inputs": inputs,
        "schism_args": [],
        "outputs": ["outputs/*.nc", "outputs/staout_*", "schism_metrics.json"],
    }
    key = f"schism/{run_tag}/manifest.json"
    s3.put_object(Bucket=cache_bucket, Key=key,
                  Body=json.dumps(manifest, indent=2).encode("utf-8"),
                  ContentType="application/json")
    return f"s3://{cache_bucket}/{key}"


def _download_run_output(run_id: str, rel_key: str) -> str | None:
    from trid3nt_server.workflows.solver.solver import (
        _get_runs_bucket, _get_s3_client,
    )
    try:
        s3 = _get_s3_client()
        obj = s3.get_object(Bucket=_get_runs_bucket(), Key=f"{run_id}/{rel_key}")
        tmp = tempfile.NamedTemporaryFile(suffix="_" + Path(rel_key).name, delete=False)
        tmp.write(obj["Body"].read())
        tmp.close()
        return tmp.name
    except Exception as exc:  # noqa: BLE001
        logger.info("schism transport: run output miss %s/%s: %s", run_id, rel_key, exc)
        return None


async def _run_one_scheme(
    scheme: str, sim_days: float, emitter: Any
) -> dict[str, Any]:
    """Stage + dispatch + download ONE scheme; return read_transport_temperature dict.

    The dict is augmented with the grid sizes so the caller can stamp the result.
    """
    workdir = Path(tempfile.mkdtemp(prefix=f"schism-transport-{scheme}-"))
    deck = deck_authoring.stage_transport_scheme_deck(
        workdir, scheme=scheme, sim_days=sim_days
    )
    run_tag = new_ulid()
    manifest_uri = await asyncio.to_thread(
        _stage_manifest, deck["files"], run_tag, nscribe=deck["nscribe"]
    )
    logger.info("transport scheme=%s staged manifest run_tag=%s uri=%s",
                scheme, run_tag, manifest_uri)

    from trid3nt_server.workflows.solver.solver import (
        run_solver, wait_for_completion,
    )
    handle = run_solver(solver=SCHISM_SOLVER_NAME, model_setup_uri=manifest_uri,
                        compute_class="medium")
    sim_step_id = await mint_dispatch_and_sim_cards(
        emitter=emitter, solver=SCHISM_SOLVER_NAME, handle=handle, compute_class="medium",
    )
    run_result = None
    try:
        async with substep(emitter, f"run_solver_{scheme}"):
            run_result = await wait_for_completion(handle)
    except asyncio.CancelledError:
        await route_sim_terminal(emitter, sim_step_id, run_result=None)
        raise
    await route_sim_terminal(emitter, sim_step_id, run_result=run_result)

    if run_result is None or run_result.status != "complete":
        raise SchismTransportError(
            SCHISM_SOLVE_FAILED,
            f"SCHISM {scheme} solve did not complete "
            f"(status={getattr(run_result, 'status', None)}, "
            f"error_code={getattr(run_result, 'error_code', None)})",
        )
    batch_run_id = getattr(run_result, "run_id", None) or run_tag
    temp_local = await asyncio.to_thread(
        _download_run_output, batch_run_id, "outputs/temperature_1.nc"
    )
    if temp_local is None:
        raise SchismTransportError(
            SCHISM_SOLVE_FAILED,
            f"SCHISM {scheme} completed but outputs/temperature_1.nc was not downloadable",
        )
    try:
        data = await asyncio.to_thread(pp.read_transport_temperature, temp_local)
    finally:
        try:
            Path(temp_local).unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    data.update({
        "n_elements": deck["n_elements"], "x_mid": deck["x_mid"],
        "t_hot": deck["t_hot"], "t_cold": deck["t_cold"],
    })
    return data


async def model_schism_transport_validation(
    *, sim_days: float,
) -> SchismTransportValidationResult | dict[str, Any]:
    """Dispatch both schemes -> contrast -> emit charts -> typed result."""
    emitter = current_emitter()
    begin_substeps(emitter, 3)  # tvd solve + upwind solve + contrast/charts

    tvd = await _run_one_scheme("tvd", sim_days, emitter)
    upwind = await _run_one_scheme("upwind", sim_days, emitter)

    t_hot = float(tvd["t_hot"])
    t_cold = float(tvd["t_cold"])
    contrast = pp.compare_transport_schemes(
        tvd, upwind, t_hot=t_hot, t_cold=t_cold
    )
    logger.info(
        "transport contrast: tvd_ret=%.2f%% upwind_ret=%.2f%% excess=%s "
        "tvd_drift=%.3f%% upwind_drift=%.3f%%",
        contrast["tvd_variance_retained_pct"], contrast["upwind_variance_retained_pct"],
        contrast["excess_mixing_factor"], contrast["tvd_mass_drift_pct"],
        contrast["upwind_mass_drift_pct"],
    )

    chart_titles: list[str] = []
    async with substep(emitter, "contrast_and_charts"):
        try:
            chart_titles = await _emit_transport_charts(tvd, upwind, contrast)
        except Exception as exc:  # noqa: BLE001 -- never break the result on an emit miss
            logger.warning("transport chart emit failed (non-fatal): %s", exc)

    provenance = [
        SyntheticInput(
            param="schism_transport_validation:heat_front",
            value=None, basis="default_demo", consequence="scenario",
            real_source_if_any="schism_verification_tests Test_HeatConsv_TVD / Test_HeatConsv_Upwind",
            note="a temperature front advected across the QuarterAnnulus M2 channel; TVD vs upwind numerical mixing",
        ),
        SyntheticInput(
            param="schism_transport_validation:mass_conservation",
            value=None, basis="default_demo", consequence="scenario",
            real_source_if_any="schism_verification_tests Test_GEN_MassConsv",
            note="domain-integrated conservative-tracer mass over the run (GEN-module path documented, temperature proxy run)",
        ),
    ]
    return SchismTransportValidationResult(
        question=(
            "How much does the transport scheme (first-order upwind vs the TVD^2 "
            "limiter) change SCHISM's numerical mixing of a scalar front, and does "
            "the conservative tracer conserve domain-integrated mass?"
        ),
        n_nodes=int(tvd["n_nodes"]),
        n_elements=int(tvd["n_elements"]),
        n_layers=int(tvd["n_layers"]),
        sim_days=float(sim_days),
        front_t_hot_c=t_hot,
        front_t_cold_c=t_cold,
        tvd_variance_retained_pct=contrast["tvd_variance_retained_pct"],
        upwind_variance_retained_pct=contrast["upwind_variance_retained_pct"],
        excess_mixing_factor=contrast["excess_mixing_factor"],
        tvd_mass_drift_pct=contrast["tvd_mass_drift_pct"],
        upwind_mass_drift_pct=contrast["upwind_mass_drift_pct"],
        tvd_overshoot_c=contrast["tvd_overshoot_c"],
        upwind_overshoot_c=contrast["upwind_overshoot_c"],
        validated=contrast["validated"],
        metrics={
            "mass_drift_tol_pct": contrast["mass_drift_tol_pct"],
            "tvd_variance_start": round(float(tvd["variance"][0]), 5),
            "tvd_variance_end": round(float(tvd["variance"][-1]), 5),
            "upwind_variance_start": round(float(upwind["variance"][0]), 5),
            "upwind_variance_end": round(float(upwind["variance"][-1]), 5),
            "n_times": int(tvd["n_times"]),
        },
        chart_titles=chart_titles,
        demonstration_note=_DEMO_NOTE,
        gen_module_note=_GEN_MODULE_NOTE,
        synthetic_inputs=provenance,
    )


async def _emit_transport_charts(
    tvd: dict[str, Any], upwind: dict[str, Any], contrast: dict[str, Any]
) -> list[str]:
    """Emit the two comparison charts through the dock; return their titles."""
    from trid3nt_server.tools.processing.charts_common import build_chart_payload

    titles: list[str] = []

    # Chart 1: variance-retention (mixing) over time -- the two schemes diverge.
    v0 = float(tvd["variance"][0])
    mix_values = []
    for tag, d in (("TVD^2 limiter", tvd), ("first-order upwind", upwind)):
        for th, v in zip(d["t_hr"], d["variance"]):
            mix_values.append({
                "hours": round(float(th), 3), "scheme": tag,
                "variance_retained_pct": round(100.0 * float(v) / v0, 4) if v0 > 0 else None,
            })
    mix_spec = {
        "data": {"values": mix_values},
        "mark": {"type": "line", "point": True},
        "encoding": {
            "x": {"field": "hours", "type": "quantitative", "title": "time (hours)"},
            "y": {"field": "variance_retained_pct", "type": "quantitative",
                  "title": "front variance retained (% of initial)"},
            "color": {"field": "scheme", "type": "nominal", "title": "transport scheme"},
        },
    }
    excess = contrast["excess_mixing_factor"]
    mix_title = "SCHISM transport scheme: front-variance retention (numerical mixing)"
    mix_caption = (
        f"Spatial variance of the advected temperature front vs time, as a percent "
        f"of the initial front variance. Both runs share the identical mesh, M2 "
        f"boundary and flow -- the transport scheme is the only difference. Upwind "
        f"decays the front faster (more numerical mixing): it retains "
        f"{contrast['upwind_variance_retained_pct']:.1f}% vs the TVD^2 limiter's "
        f"{contrast['tvd_variance_retained_pct']:.1f}%"
        + (f", losing {excess:.1f}x more variance." if excess else ".")
    )
    await emit_chart_payloads(
        build_chart_payload(vega_lite_spec=mix_spec, title=mix_title, caption=mix_caption)
    )
    titles.append(mix_title)

    # Chart 2: domain-integrated mass conservation over time (the sanity gate).
    m0_t = float(tvd["mass"][0])
    m0_u = float(upwind["mass"][0])
    mass_values = []
    for tag, d, m0 in (("TVD^2 limiter", tvd, m0_t), ("first-order upwind", upwind, m0_u)):
        for th, m in zip(d["t_hr"], d["mass"]):
            mass_values.append({
                "hours": round(float(th), 3), "scheme": tag,
                "mass_drift_pct": round(100.0 * (float(m) - m0) / m0, 5) if m0 else None,
            })
    mass_spec = {
        "data": {"values": mass_values},
        "mark": {"type": "line", "point": True},
        "encoding": {
            "x": {"field": "hours", "type": "quantitative", "title": "time (hours)"},
            "y": {"field": "mass_drift_pct", "type": "quantitative",
                  "title": "domain-mean tracer drift (% of initial)"},
            "color": {"field": "scheme", "type": "nominal", "title": "transport scheme"},
        },
    }
    mass_title = "SCHISM transport scheme: conservative-tracer mass conservation"
    mass_caption = (
        f"Domain-integrated tracer mass (domain-mean temperature) vs time, as a "
        f"percent of the initial mass. A conservative scalar with only open-boundary "
        f"exchange should drift only slightly -- the numerical-scheme sanity gate. "
        f"Final drift: TVD^2 {contrast['tvd_mass_drift_pct']:+.3f}%, upwind "
        f"{contrast['upwind_mass_drift_pct']:+.3f}% (bound "
        f"+/-{contrast['mass_drift_tol_pct']:g}%)."
    )
    await emit_chart_payloads(
        build_chart_payload(vega_lite_spec=mass_spec, title=mass_title, caption=mass_caption)
    )
    titles.append(mass_title)
    return titles
