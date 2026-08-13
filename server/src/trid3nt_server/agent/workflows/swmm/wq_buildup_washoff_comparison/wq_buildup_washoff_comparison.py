"""Engine template ``swmm_wq_buildup_washoff_comparison`` (ADR 0151).

A THIN composer over the shared mechanism-comparison runner. Runs one synthetic
subcatchment + design storm and overlays the TSS pollutographs across a
water-quality knob: ``compare="normalization"`` (buildup normalized per AREA vs
per CURB LENGTH) or ``compare="washoff"`` (exponential buildup-driven washoff vs a
flat event-mean concentration). Every number is a real parsed solver output; the
deck is a schematic stub.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from trid3nt_contracts.swmm_contracts import SWMMComparisonResult
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.mesh.swmm_deck_runner import SWMMDeckError
from trid3nt_server.agent.mesh.swmm_mechanism_compare import build_wq_buildup_washoff
from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.swmm._template_card import TemplateCard
from trid3nt_server.agent.workflows.swmm.mechanism_compare.mechanism_compare import (
    run_mechanism_comparison,
)

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.swmm.wq_buildup_washoff_comparison"
)

__all__ = ["swmm_wq_buildup_washoff_comparison"]

TEMPLATE_CARD = TemplateCard(
    question=(
        "how much does normalizing pollutant buildup by curb length instead of area "
        "change predicted TSS, and how does exponential washoff compare to an "
        "event-mean-concentration washoff for the same storm?"
    ),
    required_inputs=[],
    knobs="compare=normalization|washoff",
)

_METADATA = AtomicToolMetadata(
    name="swmm_wq_buildup_washoff_comparison",
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
async def swmm_wq_buildup_washoff_comparison(
    compare: Literal["normalization", "washoff"] = "normalization",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> SWMMComparisonResult | dict[str, Any]:
    """Compare TSS pollutographs across a buildup-normalization or a washoff-method
    knob, overlaying the concentration series.

    Use this when: the user asks how normalizing pollutant BUILDUP by CURB LENGTH
    (vs by AREA) changes predicted TSS load (compare=normalization), or how the
    exponential (buildup-driven, first-flush) WASHOFF method compares to a flat
    EVENT-MEAN CONCENTRATION washoff (compare=washoff), for the same storm.

    Fidelity: SWMM runoff-quality buildup/washoff on a small SYNTHETIC subcatchment
    + one design storm, solved headless with the continuity honesty gate. Schematic
    (NOT a georeferenced AOI) - the TSS pollutograph overlay + peak-concentration
    numbers are the product; no map layer. The buildup coefficient is a shared demo
    constant, so the normalization difference reflects the per-area vs per-curb unit
    basis (the documented recalibration pitfall).

    Params:
        compare: "normalization" (AREA vs CURB buildup) or "washoff" (EXP vs EMC).
            input_mode: reserved (labeled demonstration note always surfaced).

    Returns:
        On success ``SWMMComparisonResult`` (per-variant peak TSS + the pollutograph
        overlay chart). On failure ``{"status":"error",...}`` with a typed
        ``SWMM_DECK_*`` code.
    """
    try:
        build = build_wq_buildup_washoff(compare)
        result = await run_mechanism_comparison(build)
        logger.info(
            "swmm_wq_buildup_washoff_comparison complete compare=%s headline=%s",
            compare, result.headline,
        )
        return result
    except asyncio.CancelledError:
        raise
    except SWMMDeckError as exc:
        logger.warning("swmm_wq_buildup_washoff_comparison failed: %s", exc.error_code)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("swmm_wq_buildup_washoff_comparison unexpected failure")
        return {"status": "error", "error_code": "SWMM_COMPARE_INTERNAL_ERROR",
                "error_message": str(exc)}
