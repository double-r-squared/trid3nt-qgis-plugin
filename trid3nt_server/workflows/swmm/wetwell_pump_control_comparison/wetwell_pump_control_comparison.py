"""Engine template ``swmm_wetwell_pump_control_comparison``.

A THIN composer over the shared mechanism-comparison runner. One synthetic wet
well with three pumps under one inflow, comparing pump CONTROL schemes: a fixed
setpoint (all pumps on one band), a depth-staged duty/standby alternation, and a
multi-condition AND rule. Overlays the wet-well depth trace and reports per-scheme
cycling + run-fraction. Every number is a real parsed solver output; the deck is a
schematic stub.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.swmm_contracts import SWMMComparisonResult
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.mesh.swmm_deck_runner import SWMMDeckError
from trid3nt_server.mesh.swmm_mechanism_compare import build_pump_control
from trid3nt_server.data import register_tool
from trid3nt_server.workflows.swmm._template_card import TemplateCard
from trid3nt_server.workflows.swmm.mechanism_compare.mechanism_compare import (
    run_mechanism_comparison,
)

logger = logging.getLogger(
    "trid3nt_server.workflows.swmm.wetwell_pump_control_comparison"
)

__all__ = ["swmm_wetwell_pump_control_comparison"]

TEMPLATE_CARD = TemplateCard(
    question=(
        "for a lift station wet well with several pumps, how do the pump control "
        "rules (fixed setpoint vs depth-staged duty/standby vs multi-condition) "
        "change wet-well depth, pump run-time and cycling?"
    ),
    required_inputs=[],
    knobs="(none - compares the three control schemes)",
)

_METADATA = AtomicToolMetadata(
    name="swmm_wetwell_pump_control_comparison",
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
async def swmm_wetwell_pump_control_comparison(
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> SWMMComparisonResult | dict[str, Any]:
    """Compare lift-station pump CONTROL schemes on one wet well + inflow,
    overlaying wet-well depth and reporting cycling + run-fraction per scheme.

    Use this when: the user asks about a lift station / wet well pump curve, at
    what depths pumps turn ON and OFF, or how a depth-staged DUTY/STANDBY
    alternation of multiple pumps (vs a single fixed setpoint) or a MULTI-CONDITION
    (AND) control rule changes pump run-time and cycling.

    Fidelity: SWMM dynamic-wave routing of a small SYNTHETIC wet well + three
    depth-flow pump curves + CONTROLS rules, solved headless with the continuity
    honesty gate. Schematic (NOT a georeferenced AOI) - the wet-well depth overlay
    + per-scheme cycle/run-fraction numbers are the product; no map layer.

    Params:
        input_mode: reserved (labeled demonstration note always surfaced).

    Returns:
        On success ``SWMMComparisonResult`` (per-scheme depth peak + ``extra``
        cycles/run-fraction + the depth overlay chart). On failure
        ``{"status":"error",...}`` with a typed ``SWMM_DECK_*`` code.
    """
    try:
        build = build_pump_control()
        result = await run_mechanism_comparison(build)
        logger.info(
            "swmm_wetwell_pump_control_comparison complete headline=%s", result.headline,
        )
        return result
    except asyncio.CancelledError:
        raise
    except SWMMDeckError as exc:
        logger.warning("swmm_wetwell_pump_control_comparison failed: %s", exc.error_code)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("swmm_wetwell_pump_control_comparison unexpected failure")
        return {"status": "error", "error_code": "SWMM_COMPARE_INTERNAL_ERROR",
                "error_message": str(exc)}
