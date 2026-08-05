"""Engine template ``swmm_pump_pid_rtc`` - run a CITED published SWMM deck
demonstrating a PID real-time-control (RTC) rule on a pump (ADR 0128).

A THIN composer over the shared published-deck runner. Binds the cited
openswmm.org example "Example - PID Control for a Pump" (Robert Dickinson, on the
EXTRAN 3/4 composite model): a PID CONTROLS rule adjusts the pump SETTING to hold
the wet-well (upstream node) at a target depth under dry- + wet-weather inflow.
Fetched at runtime; solved VERBATIM; the wet-well depth (vs target) + pump flow
are charted.

Demonstration-honesty: the deck is the cited example's schematic network, NOT a
user AOI (no georeferenced map). Every narrated number is a real parsed output.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.swmm_contracts import SWMMDeckRunResult
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.agent.mesh.swmm_deck_runner import SWMMDeckError
from trid3nt_server.agent.tools import register_tool
from trid3nt_server.agent.workflows.swmm._template_card import TemplateCard
from trid3nt_server.agent.workflows.swmm.deck_runner.deck_runner import model_published_deck

logger = logging.getLogger(
    "trid3nt_server.agent.workflows.swmm.deck_pid_pump.deck_pid_pump"
)

__all__ = ["swmm_pump_pid_rtc"]

_DECK_ID = "pump_pid_rtc"

TEMPLATE_CARD = TemplateCard(
    question=(
        "run the cited published SWMM PID pump-control example and show how the "
        "real-time-control rule holds the wet-well at its target depth"
    ),
    required_inputs=[],
    knobs="(none - fixed published real-time-control example)",
)

_METADATA = AtomicToolMetadata(
    name="swmm_pump_pid_rtc",
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
async def swmm_pump_pid_rtc(
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> SWMMDeckRunResult | dict[str, Any]:
    """Run the CITED published PID pump real-time-control (RTC) SWMM example.

    Fidelity: SWMM dynamic-wave + a PID CONTROLS rule regulating a pump on a
    PUBLISHED EXAMPLE deck (Robert Dickinson, openswmm.org, EXTRAN 3/4 composite),
    run VERBATIM. A demonstration of the cited example's own network - NOT a user
    site; the deck's coordinates are schematic, so there is NO georeferenced map.
    The product is the wet-well-depth-vs-target control-tracking chart + the pump
    flow, honestly labeled a demonstration. Off-scope: a real municipal network ->
    swmm_network_import.

    Use this when: the user wants to see / demonstrate real-time control (RTC) or a
    PID rule regulating a pump / wet-well in SWMM, or asks for the published PID
    pump-control SWMM example.

    Params:
        input_mode: reserved ADR 0107 lever; the labeled demonstration note is
            always surfaced.

    Returns:
        On success: ``SWMMDeckRunResult`` carrying ``continuity_error_pct`` /
        ``headline.wet_well_node`` / ``headline.wet_well_depth`` (min/max) /
        ``headline.pid_target_depth`` / ``headline.pump_peak_flow`` /
        ``chart_titles`` / ``demonstration_note``. On failure:
        ``{"status":"error",...}`` with a typed ``SWMM_DECK_*`` code.
    """
    try:
        result = await model_published_deck(deck_id=_DECK_ID, input_mode=input_mode)
        logger.info(
            "swmm_pump_pid_rtc complete continuity=%+.3f%% headline=%s",
            result.continuity_error_pct, result.headline,
        )
        return result
    except asyncio.CancelledError:
        raise
    except SWMMDeckError as exc:
        logger.warning("swmm_pump_pid_rtc failed: %s (%s)", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("swmm_pump_pid_rtc unexpected failure")
        return {
            "status": "error",
            "error_code": "SWMM_DECK_INTERNAL_ERROR",
            "error_message": str(exc),
        }
