"""Engine template ``swmm_lid_raingarden_wq`` - run a CITED published SWMM deck
demonstrating rain-garden LID + buildup/washoff water quality.

A THIN composer over the shared published-deck runner
(``workflows/swmm/deck_runner``). Binds the cited openswmm.org example "A Very
Simple Two-Subcatchment Water Quality Model With and Without Rain Gardens"
(Robert Dickinson): paired subcatchments, one WITH a rain-garden LID control and
one WITHOUT, so the runoff + pollutant reduction from the LID is the built-in
expected-outcome check. Fetched at runtime from the pinned public source; solved
VERBATIM; the runoff hydrographs are charted.

Demonstration-honesty: the deck is the cited example's schematic network, NOT a
user AOI (no georeferenced map). Every narrated number is a real parsed output.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts.swmm_contracts import SWMMDeckRunResult
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.mesh.swmm_deck_runner import SWMMDeckError
from trid3nt_server.data import register_tool
from trid3nt_server.workflows.swmm._template_card import TemplateCard
from trid3nt_server.workflows.swmm.deck_runner.deck_runner import model_published_deck

logger = logging.getLogger(
    "trid3nt_server.workflows.swmm.deck_lid_wq.deck_lid_wq"
)

__all__ = ["swmm_lid_raingarden_wq"]

_DECK_ID = "lid_raingarden_wq"

TEMPLATE_CARD = TemplateCard(
    question=(
        "run the cited published SWMM rain-garden example (two subcatchments, with "
        "and without a bioretention LID control) and show the runoff + pollutant "
        "reduction the rain garden achieves"
    ),
    required_inputs=[],  # the deck is a fixed published example - no user input
    knobs="rain_scale (multiply the published design storm)",
)

_METADATA = AtomicToolMetadata(
    name="swmm_lid_raingarden_wq",
    ttl_class="live-no-cache",
    source_class="workflow_dispatch",
    cacheable=False,
    engine="swmm",
    tier="template",
)


@register_tool(
    _METADATA,
    read_only_hint=False,
    open_world_hint=True,  # fetches the cited deck from the public source at runtime
    destructive_hint=False,
    idempotent_hint=False,
)
async def swmm_lid_raingarden_wq(
    rain_scale: float = 1.0,
    input_mode: str | None = None,
    **_extra_ignored: Any,
) -> SWMMDeckRunResult | dict[str, Any]:
    """Run the CITED published rain-garden LID + water-quality SWMM example.

    Fidelity: SWMM dynamic-wave + LID bioretention + buildup/washoff on a PUBLISHED
    EXAMPLE deck (Robert Dickinson, openswmm.org), run VERBATIM. This is a
    demonstration of the cited example's own network - NOT a user site. The deck's
    coordinates are schematic, so there is NO georeferenced map; the product is the
    runoff hydrograph chart + the typed with/without-rain-garden reduction numbers.
    Off-scope: a real municipal network -> swmm_network_import; a DEM-synthesized
    overland flood -> swmm_urban_flood.

    Use this when: the user wants to see / demonstrate how a rain garden (bioretention
    LID) reduces runoff and pollutant washoff, or asks for the published two-
    subcatchment with-and-without-rain-garden SWMM water-quality example.

    Params:
        rain_scale: multiply the published design storm's hyetograph by this factor
            (1.0 = the example's own storm, unchanged). Same profile, scaled depth.
        input_mode: reserved lever; the labeled demonstration note is
            always surfaced (the deck is a fixed published example).

    Returns:
        On success: ``SWMMDeckRunResult`` carrying ``continuity_error_pct`` /
        ``n_subcatchments`` / ``headline.subcatchment_peak_runoff`` /
        ``headline.lid_reduces_runoff`` / ``chart_titles`` / ``demonstration_note``
        (narrate these typed fields only). On failure: ``{"status":"error",...}``
        with a typed ``SWMM_DECK_*`` code (e.g. the cited source was unreachable).
    """
    try:
        result = await model_published_deck(
            deck_id=_DECK_ID, rain_scale=float(rain_scale), input_mode=input_mode
        )
        logger.info(
            "swmm_lid_raingarden_wq complete continuity=%+.3f%% headline=%s",
            result.continuity_error_pct, result.headline,
        )
        return result
    except asyncio.CancelledError:
        raise
    except SWMMDeckError as exc:
        logger.warning("swmm_lid_raingarden_wq failed: %s (%s)", exc.error_code, exc)
        return {"status": "error", "error_code": exc.error_code, "error_message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("swmm_lid_raingarden_wq unexpected failure")
        return {
            "status": "error",
            "error_code": "SWMM_DECK_INTERNAL_ERROR",
            "error_message": str(exc),
        }
