"""Engine template ``swmm_subcatchment_runoff_comparison``.

A THIN composer over the shared mechanism-comparison runner. Runs ONE synthetic
subcatchment under ONE design storm across a knob and overlays the runoff
hydrographs: ``compare="infiltration_method"`` (Horton vs Green-Ampt vs Curve
Number) or ``compare="development_intensity"`` (pre- vs post-development
imperviousness). Every number is a real parsed solver output; the deck is a
schematic stub (not a georeferenced site), so the product is the chart + typed
scalars.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from trid3nt_contracts.swmm_contracts import SWMMComparisonResult
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.mesh.swmm_deck_runner import SWMMDeckError
from trid3nt_server.mesh.swmm_mechanism_compare import build_subcatchment_runoff
from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.swmm._template_card import TemplateCard
from trid3nt_server.workflows.swmm.mechanism_compare.mechanism_compare import (
    run_mechanism_comparison,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.swmm.subcatchment_runoff_comparison"
)

__all__ = ["swmm_subcatchment_runoff_comparison"]

TEMPLATE_CARD = TemplateCard(
    question=(
        "on one subcatchment + storm, how much does the infiltration method (Horton "
        "vs Green-Ampt vs Curve Number) or pre-vs-post development imperviousness "
        "change peak runoff and volume?"
    ),
    required_inputs=[],
    knobs="compare=infiltration_method|development_intensity",
)

_METADATA = AtomicToolMetadata(
    name="swmm_subcatchment_runoff_comparison",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="swmm",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=True,
    destructive_hint=False,
    idempotent_hint=False,
)
async def swmm_subcatchment_runoff_comparison(
    compare: Literal["infiltration_method", "development_intensity"] = "infiltration_method",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> SWMMComparisonResult | dict[str, Any]:
    """Compare single-subcatchment runoff across an infiltration-method or a
    development-intensity knob, overlaying the hydrographs.

    Use this when: the user asks how the infiltration LOSS METHOD (Horton vs
    Green-Ampt vs Curve Number) or how INCREASED IMPERVIOUSNESS (pre- vs
    post-development) changes peak runoff / volume / timing on a subcatchment.

    Fidelity: SWMM runoff on a small SYNTHETIC subcatchment + one design storm,
    solved headless with the continuity honesty gate. The deck is schematic (NOT a
    georeferenced AOI) - the runoff-hydrograph overlay + typed peak/volume numbers
    are the product; no map layer. Off-scope: a real AOI mesh flood ->
    swmm_urban_flood.

    Params:
        compare: "infiltration_method" (Horton | Green-Ampt | Curve Number, same
            pervious subcatchment + intense storm) or "development_intensity"
            (5 percent pasture vs 75 percent developed). input_mode: reserved.

    Returns:
        On success ``SWMMComparisonResult`` (per-variant peak/volume/continuity +
        the overlay chart title). On failure ``{"status":"error",...}`` with a
        typed ``SWMM_DECK_*`` code.
    """
    try:
        build = build_subcatchment_runoff(compare)
        result = await run_mechanism_comparison(build)
        logger.info(
            "swmm_subcatchment_runoff_comparison complete compare=%s headline=%s",
            compare, result.headline,
        )
        return result
    except asyncio.CancelledError:
        raise
    except SWMMDeckError as exc:
        logger.warning("swmm_subcatchment_runoff_comparison failed: %s", exc.error_code)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("swmm_subcatchment_runoff_comparison unexpected failure")
        return {"status": "error", "error_code": "SWMM_COMPARE_INTERNAL_ERROR",
                "error_message": str(exc)}
