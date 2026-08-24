"""Engine template ``swmm_node_hydraulics_comparison``.

A THIN composer over the shared mechanism-comparison runner. Three schematic
node-hydraulics comparisons selected by ``scenario``: ``outlet_family`` (transverse
weir vs V-notch weir vs circular orifice vs rating-curve outlet draining one
storage node), ``flow_diversion`` (a junction split main-vs-relief as inflow
rises), ``surcharge_ponding`` (an undersized pipe with ALLOW_PONDING off vs on).
Every number is a real parsed solver output; the decks are schematic stubs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from trid3nt_contracts.swmm_contracts import SWMMComparisonResult
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.mesh.swmm_deck_runner import SWMMDeckError
from trid3nt_server.mesh.swmm_mechanism_compare import build_node_hydraulics
from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.swmm._template_card import TemplateCard
from trid3nt_server.workflows.swmm.mechanism_compare.mechanism_compare import (
    run_mechanism_comparison,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.swmm.node_hydraulics_comparison"
)

__all__ = ["swmm_node_hydraulics_comparison"]

TEMPLATE_CARD = TemplateCard(
    question=(
        "at a node, how does discharge differ across outlet structures (weir / "
        "orifice / rating curve), how does a junction split flow to a relief pipe, "
        "or how does allowing surface ponding change an undersized network?"
    ),
    required_inputs=[],
    knobs="scenario=outlet_family|flow_diversion|surcharge_ponding",
)

_METADATA = AtomicToolMetadata(
    name="swmm_node_hydraulics_comparison",
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
async def swmm_node_hydraulics_comparison(
    scenario: Literal["outlet_family", "flow_diversion", "surcharge_ponding"] = "outlet_family",
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> SWMMComparisonResult | dict[str, Any]:
    """Compare node hydraulics: outlet-structure family, flow diversion, or
    surcharge/ponding, overlaying the discharge / depth series.

    Use this when: the user asks how a transverse weir vs V-notch weir vs circular
    orifice vs rating-curve outlet discharge at the same node (scenario=
    outlet_family); how flow SPLITS between a main pipe and a relief/bypass pipe as
    inflow rises (scenario=flow_diversion); or WHERE/how much water surcharges and
    ponds in an undersized network with ALLOW_PONDING off vs on
    (scenario=surcharge_ponding).

    Fidelity: SWMM dynamic-wave routing on a small SYNTHETIC pond/junction stub,
    solved headless with the continuity honesty gate. Schematic (NOT a
    georeferenced AOI) - the overlay chart + typed peak/flood numbers are the
    product; no map layer. Off-scope: a real AOI mesh flood -> swmm_urban_flood.

    Params:
        scenario: which node-hydraulics comparison to run. input_mode: reserved.

    Returns:
        On success ``SWMMComparisonResult``; on failure ``{"status":"error",...}``
        with a typed ``SWMM_DECK_*`` code.
    """
    try:
        build = build_node_hydraulics(scenario)
        result = await run_mechanism_comparison(build)
        logger.info(
            "swmm_node_hydraulics_comparison complete scenario=%s headline=%s",
            scenario, result.headline,
        )
        return result
    except asyncio.CancelledError:
        raise
    except SWMMDeckError as exc:
        logger.warning("swmm_node_hydraulics_comparison failed: %s", exc.error_code)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("swmm_node_hydraulics_comparison unexpected failure")
        return {"status": "error", "error_code": "SWMM_COMPARE_INTERNAL_ERROR",
                "error_message": str(exc)}
