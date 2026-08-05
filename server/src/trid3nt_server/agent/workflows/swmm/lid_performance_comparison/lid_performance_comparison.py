"""Engine template ``swmm_lid_performance_comparison`` (ADR 0151).

A THIN composer over the shared mechanism-comparison runner. Runs one synthetic
subcatchment + design storm with vs without a LID control and overlays the runoff
hydrographs, selected by ``lid_type``: ``green_roof`` (roof detention),
``vegetative_swale`` (conveyance attenuation), or ``rainbarrel_vs_disconnect``
(rain barrel vs simple rooftop disconnection, both vs baseline). Every number is a
real parsed solver output; the deck is a schematic stub.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from trid3nt_contracts.swmm_contracts import SWMMComparisonResult
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.mesh.swmm_deck_runner import SWMMDeckError
from trid3nt_server.agent.mesh.swmm_mechanism_compare import build_lid_performance
from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.swmm._template_card import TemplateCard
from trid3nt_server.agent.workflows.swmm.mechanism_compare.mechanism_compare import (
    run_mechanism_comparison,
)

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.swmm.lid_performance_comparison"
)

__all__ = ["swmm_lid_performance_comparison"]

TEMPLATE_CARD = TemplateCard(
    question=(
        "how much less and how much slower does runoff leave a subcatchment with a "
        "green roof, a vegetative swale, or a rain barrel vs rooftop disconnection, "
        "compared to no LID?"
    ),
    required_inputs=[],
    knobs="lid_type=green_roof|vegetative_swale|rainbarrel_vs_disconnect",
)

_METADATA = AtomicToolMetadata(
    name="swmm_lid_performance_comparison",
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
async def swmm_lid_performance_comparison(
    lid_type: Literal["green_roof", "vegetative_swale", "rainbarrel_vs_disconnect"] = "green_roof",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> SWMMComparisonResult | dict[str, Any]:
    """Compare runoff with vs without a LID (green roof / vegetative swale / rain
    barrel vs rooftop disconnection), overlaying the hydrographs.

    Use this when: the user asks how a GREEN ROOF, a VEGETATIVE SWALE, a RAIN
    BARREL, or simple ROOFTOP DISCONNECTION reduces / delays runoff on a
    subcatchment (low-impact development / green-infrastructure detention).

    Fidelity: SWMM runoff + native EPA LID controls (green roof = Surface+Soil+
    DrainMat; rain barrel = Surface+Storage; rooftop disconnect / swale =
    Surface-based) on a small SYNTHETIC subcatchment + one design storm, solved
    headless with the continuity honesty gate. Schematic (NOT a georeferenced AOI)
    - the with/without runoff overlay + reduction numbers are the product; no map
    layer. Off-scope: a real AOI mesh flood -> swmm_urban_flood.

    Params:
        lid_type: "green_roof", "vegetative_swale", or "rainbarrel_vs_disconnect"
            (baseline vs rain barrel vs rooftop disconnection). input_mode: reserved.

    Returns:
        On success ``SWMMComparisonResult`` (per-variant peak/volume + the runoff
        overlay chart). On failure ``{"status":"error",...}`` with a typed
        ``SWMM_DECK_*`` code.
    """
    try:
        build = build_lid_performance(lid_type)
        result = await run_mechanism_comparison(build)
        logger.info(
            "swmm_lid_performance_comparison complete lid_type=%s headline=%s",
            lid_type, result.headline,
        )
        return result
    except asyncio.CancelledError:
        raise
    except SWMMDeckError as exc:
        logger.warning("swmm_lid_performance_comparison failed: %s", exc.error_code)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("swmm_lid_performance_comparison unexpected failure")
        return {"status": "error", "error_code": "SWMM_COMPARE_INTERNAL_ERROR",
                "error_message": str(exc)}
