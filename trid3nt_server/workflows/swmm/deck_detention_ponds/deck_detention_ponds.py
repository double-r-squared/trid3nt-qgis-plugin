"""Engine template ``swmm_wwtp_detention_ponds`` - run a CITED published SWMM deck
demonstrating stage-storage detention ponds + weir/orifice storage routing.

A THIN composer over the shared published-deck runner. Binds the cited
openswmm.org example "UV Plant with Detention Ponds" (Rob James): storage
(detention pond) nodes draining through outlet weirs - a storage-routing
demonstration. Fetched at runtime; solved VERBATIM; the pond stage-recession
series are charted.

Demonstration-honesty: the deck is the cited example's schematic network, NOT a
user AOI (no georeferenced map). This example publishes NO numeric results - it is
a storage-routing / complex-outlet stress test, not a calibration target; the
runner labels it so.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.swmm_contracts import SWMMDeckRunResult
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.mesh.swmm_deck_runner import SWMMDeckError
from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.swmm._template_card import TemplateCard
from trid3nt_server.workflows.swmm.deck_runner.deck_runner import model_published_deck

logger = logging.getLogger(
    "trid3nt_server.workflows.swmm.deck_detention_ponds.deck_detention_ponds"
)

__all__ = ["swmm_wwtp_detention_ponds"]

_DECK_ID = "wwtp_detention_ponds"

TEMPLATE_CARD = TemplateCard(
    question=(
        "run the cited published SWMM detention-pond example and show how the "
        "storage ponds drain through their outlet weirs (stage-storage routing)"
    ),
    required_inputs=[],
    knobs="(none - fixed published storage-routing example)",
)

_METADATA = AtomicToolMetadata(
    name="swmm_wwtp_detention_ponds",
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
async def swmm_wwtp_detention_ponds(
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> SWMMDeckRunResult | dict[str, Any]:
    """Run the CITED published detention-pond storage-routing SWMM example.

    Fidelity: SWMM dynamic-wave STORAGE-node routing on a PUBLISHED EXAMPLE deck
    (Rob James, openswmm.org "UV Plant with Detention Ponds"), run VERBATIM. A
    demonstration of the cited example's own network - NOT a user site; the deck's
    coordinates are schematic, so there is NO georeferenced map. The example
    publishes no numeric results (a storage-routing stress test), so the runner
    surfaces the pond stage-recession chart + typed stage numbers, honestly labeled
    a demonstration. Off-scope: a real municipal network -> swmm_network_import.

    Use this when: the user wants to see / demonstrate detention-pond (storage-node)
    routing through outlet weirs, or asks for the published UV-plant detention-pond
    SWMM example.

    Params:
        input_mode: reserved lever; the labeled demonstration note is
            always surfaced.

    Returns:
        On success: ``SWMMDeckRunResult`` carrying ``continuity_error_pct`` /
        ``n_nodes`` / ``n_links`` / ``headline.pond_stage`` (start/end/peak per
        pond) / ``chart_titles`` / ``demonstration_note``. On failure:
        ``{"status":"error",...}`` with a typed ``SWMM_DECK_*`` code.
    """
    try:
        result = await model_published_deck(deck_id=_DECK_ID, input_mode=input_mode)
        logger.info(
            "swmm_wwtp_detention_ponds complete continuity=%+.3f%% headline=%s",
            result.continuity_error_pct, result.headline,
        )
        return result
    except asyncio.CancelledError:
        raise
    except SWMMDeckError as exc:
        logger.warning("swmm_wwtp_detention_ponds failed: %s (%s)", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("swmm_wwtp_detention_ponds unexpected failure")
        return {
            "status": "error",
            "error_code": "SWMM_DECK_INTERNAL_ERROR",
            "error_message": str(exc),
        }
