"""Engine template ``modflow_package_validation``.

A THIN composer over the shared package-validation engine core. Five synthetic
MF6 benchmark cases selected by ``case``, each exercising a package no archetype
composer exposes and checking the mf6 result against a published/analytical
reference:

- ``newton_dry_rewet`` (GWF-NPF Newton): the Zaidel staircase channel - does the
  Newton formulation dry/rewet the unconfined channel without failure? Reports
  the Newton-vs-standard dry-cell contrast.
- ``maw_crossaquifer`` (GWF-MAW): a non-pumping multi-aquifer well equilibrating
  to the Sokol (1963) transmissivity-weighted analytical level (a free V&V
  delta).
- ``hfb_barrier`` (GWF-HFB): a defined-thickness barrier whose cross-wall flux
  matches the HYDCHR analytical and is grid-refinement independent.
- ``prt_capture_zone`` (native mf6 PRT): a confined well in regional through-flow
  tracked forward (pathlines/travel times) and backward (capture zone) vs the
  Grubb (1993) uniform-flow stagnation + capture-width analytical.
- ``henry_saltwater`` (GWF-BUY + GWT): the classic Henry variable-density wedge -
  does BUY reproduce the 0.5-isochlor saltwater-intrusion shape?
- ``sfr_stream_depletion`` (GWF-SFR + WEL): a well-connected SFR stream + a pumping
  well - does the transient stream-depletion fraction q(t)/Q match the Glover
  (1954) erfc analytical (via a pump/no-pump superposition)?
- ``mvr_routing`` (GWF-MVR): does the Mover transfer rejected UZF infiltration +
  DRN discharge into SFR reaches within one timestep, conserving mass exactly?

The decks are SMALL SYNTHETIC benchmarks (schematic coordinates), so the product
is the computed-vs-reference CHART + typed scalars, never a georeferenced map.
Every number is a real parsed mf6 output (invariant 1).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.modflow_contracts import ModflowValidationResult
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.mesh.modflow_package_validation import (
    VALIDATION_CASES,
    ModflowValidationError,
    SolvedValidation,
    run_validation_case,
)
from trid3nt_server.data import register_tool
from trid3nt_server.data.processing.charts_common import build_chart_payload
from trid3nt_server.workflows.modflow._template_card import TemplateCard
from trid3nt_server.emission.pipeline_emitter import (
    begin_substeps,
    current_emitter,
    emit_chart_payloads,
    substep,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.modflow.package_validation.package_validation"
)

__all__ = ["modflow_package_validation", "run_package_validation"]

TEMPLATE_CARD = TemplateCard(
    question=(
        "does a MODFLOW package reproduce a published/analytical benchmark: the "
        "Newton formulation drying/rewetting a staircase channel, a multi-aquifer "
        "well equilibrating to the Sokol analytical level, a horizontal-flow "
        "barrier whose flux is grid-refinement independent, native PRT particle "
        "tracking delineating a well capture zone vs the Grubb analytical, the "
        "BUY package reproducing the Henry saltwater-intrusion wedge, an SFR-coupled "
        "well reproducing the Glover stream-depletion curve, or the MVR package "
        "routing rejected UZF infiltration and discharge into SFR reaches?"
    ),
    required_inputs=[],
    knobs=(
        "case=newton_dry_rewet|maw_crossaquifer|hfb_barrier|prt_capture_zone|"
        "henry_saltwater|sfr_stream_depletion|mvr_routing, "
        "direction=forward|backward (prt only), n_particles (prt only)"
    ),
)

_METADATA = AtomicToolMetadata(
    name="modflow_package_validation",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="modflow",
    tier="template",
)

_DEMO_NOTE = (
    "Synthetic MODFLOW 6 package benchmark on a schematic deck (local model "
    "units, not a georeferenced AOI): the computed-vs-reference chart + typed "
    "scalars are the product, there is no map layer."
)


async def run_package_validation(
    case: str, direction: str = "backward", n_particles: int = 40
) -> ModflowValidationResult:
    """Solve one validation case, emit the chart, return the typed result.

    ``direction`` / ``n_particles`` apply ONLY to ``prt_capture_zone``. Raises
    ``ModflowValidationError`` (unknown case / missing mf6) - the caller maps it
    to a typed error frame.
    """
    emitter = current_emitter()
    begin_substeps(emitter, 2)

    async with substep(emitter, "solve_case"):
        solved: SolvedValidation = await asyncio.to_thread(
            run_validation_case, case, direction=direction, n_particles=int(n_particles)
        )

    async with substep(emitter, "chart_and_publish"):
        chart_titles: list[str] = []
        if solved.chart_spec is not None:
            try:
                payload = build_chart_payload(
                    vega_lite_spec=solved.chart_spec,
                    title=solved.chart_title,
                    caption=solved.chart_caption,
                )
                await emit_chart_payloads(payload)
                chart_titles.append(solved.chart_title)
            except Exception as exc:  # noqa: BLE001 - never break the solve on an emit miss
                logger.warning("run_package_validation: chart emit failed (%s)", exc)

    meta = VALIDATION_CASES[case]
    provenance = [
        SyntheticInput(
            param=f"modflow_validation:{case}",
            value=None,
            basis="default_demo",
            real_source_if_any=meta.reference_source,
            note=meta.basis_note,
        )
    ]
    result = ModflowValidationResult(
        case=case,
        question=meta.question,
        package=meta.package,
        computed_value=solved.computed_value,
        reference_value=solved.reference_value,
        reference_label=meta.reference_label,
        reference_source=meta.reference_source,
        delta=solved.delta,
        relative_error=solved.relative_error,
        validated=solved.validated,
        tolerance=solved.tolerance,
        metrics=solved.metrics,
        chart_titles=chart_titles,
        demonstration_note=_DEMO_NOTE,
        schematic_only=True,
        basis="synthetic",
        synthetic_inputs=provenance,
    )
    logger.info(
        "modflow_package_validation case=%s validated=%s computed=%s reference=%s delta=%s",
        case, result.validated, result.computed_value, result.reference_value,
        result.delta,
    )
    return result


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
)
async def modflow_package_validation(
    case: Literal[
        "newton_dry_rewet", "maw_crossaquifer", "hfb_barrier",
        "prt_capture_zone", "henry_saltwater", "sfr_stream_depletion",
        "mvr_routing",
    ] = "maw_crossaquifer",
    direction: Literal["forward", "backward"] = "backward",
    n_particles: int = 40,
    **_extra_ignored: Any,
) -> ModflowValidationResult | dict[str, Any]:
    """Validate a MODFLOW 6 package against a published/analytical benchmark.

    Fidelity: MODFLOW 6 mf6 solve on a SMALL SYNTHETIC benchmark deck (schematic,
    NOT a georeferenced AOI) - the computed-vs-reference chart + typed scalars are
    the product, no map layer. Off-scope: a real site groundwater study ->
    modflow_contaminant_plume / modflow_sustainable_yield / modflow_capture_zone /
    modflow_saltwater_intrusion.

    Use this when the user asks whether a MODFLOW package/formulation reproduces a
    known benchmark, or to demonstrate a package's behavior:
        - ``newton_dry_rewet`` (GWF-NPF Newton): can the solver dry AND rewet an
          unconfined channel over a staircase impervious base without oscillation
          or failure? Reproduces the Zaidel (2013) benchmark and reports the
          Newton-vs-standard dry-cell contrast.
        - ``maw_crossaquifer`` (GWF-MAW): does a non-pumping multi-aquifer well
          equilibrate to the Sokol (1963) transmissivity-weighted analytical
          water level between two confined aquifers? Reports the computed-vs-
          analytical delta.
        - ``hfb_barrier`` (GWF-HFB): does a defined-thickness barrier reduce
          cross-wall flux to the HYDCHR analytical value, independent of grid
          refinement? Reports the flux at several grid resolutions vs analytical.
        - ``prt_capture_zone`` (native mf6 PRT): does particle tracking delineate
          a pumping well's capture zone (backward from the well) + pathlines and
          travel times (forward from the regional inflow), matching the Grubb
          (1993) uniform-flow capture-zone analytical (stagnation distance +
          capture width)? Native PRT ships in mf6 6.7.0 - no MODPATH 7 needed.
        - ``henry_saltwater`` (GWF-BUY + GWT): does the BUY variable-density
          package reproduce the classic Henry saltwater-intrusion wedge (the
          0.5-isochlor shape)? Reports the toe penetration vs the published wedge.
        - ``sfr_stream_depletion`` (GWF-SFR + WEL): does an SFR-coupled well near a
          stream reproduce the Glover (1954) transient stream-depletion curve - the
          fraction of the pumping rate captured from the stream vs time? Uses a
          pump/no-pump superposition; reports the depletion fraction vs Glover erfc.
        - ``mvr_routing`` (GWF-MVR): does the Mover package transfer rejected UZF
          infiltration and DRN groundwater discharge into SFR reaches within one
          coupled timestep, conserving mass exactly? Reports the routed volumes and
          the conservation delta.

    Params:
        case: which validation case to run (default ``maw_crossaquifer``).
        direction: PRT tracking direction shown (``prt_capture_zone`` only;
            ``"backward"`` = capture zone from the well, ``"forward"`` = pathlines
            from the inflow). Both metrics are always computed; this selects the
            headline framing. Ignored by other cases.
        n_particles: PRT backward release-ring size (``prt_capture_zone`` only,
            default 40). Ignored by other cases.

    Returns:
        On success ``ModflowValidationResult`` (case, package, computed vs
        reference value, delta, relative_error, validated, per-case metrics, the
        emitted chart title, and the loud synthetic-benchmark note). On failure
        ``{"status":"error", ...}`` with a typed ``MODFLOW_*`` code.
    """
    try:
        result = await run_package_validation(
            str(case), direction=str(direction), n_particles=int(n_particles)
        )
        return result
    except asyncio.CancelledError:
        raise
    except ModflowValidationError as exc:
        logger.warning("modflow_package_validation failed: %s", getattr(exc, "error_code", "?"))
        return {
            "status": "error",
            "error_code": getattr(exc, "error_code", "MODFLOW_VALIDATION_ERROR"),
            "error_message": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("modflow_package_validation unexpected failure")
        return {
            "status": "error",
            "error_code": "MODFLOW_VALIDATION_INTERNAL_ERROR",
            "error_message": str(exc),
        }
