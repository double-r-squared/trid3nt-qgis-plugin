"""Shared MODFLOW archetype input-review helpers (ADR 0223).

The MODFLOW archetype family (capture_zone, river_seepage, contaminant_plume,
saltwater_intrusion, managed_recharge, mine_dewatering, regional_water_budget,
wetland_hydroperiod, sustainable_yield, asr, ...) historically carried the
demo-aquifer provenance as PROSE caveat strings (``demo_aquifer_caveat``,
``aquifer_k_source``) in the result ``summary`` dict. That is truthful but not
machine-readable and never ran through the ``gate_input_review`` review surface,
so a session in ``user_gated`` mode could not review/override the demo defaults.

This module promotes that prose to STRUCTURED ``SyntheticInput`` review entries and
routes them through ``gate_input_review`` (ADR 0107), matching the surge / hecras /
swmm templates. The caveat strings become the entry ``note``s -- the prose is kept
on the summary (backward compatible) AND mirrored as structured provenance stamped
onto the returned layer's ``synthetic_inputs``.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_contracts.common import InputBasis, SyntheticInput

from trid3nt_server.agent.gates.input_review import ReviewOutcome, gate_input_review

logger = logging.getLogger("trid3nt_server.agent.workflows.modflow._input_review")

__all__ = [
    "aquifer_k_basis",
    "aquifer_k_review_entry",
    "gate_and_stamp_modflow_inputs",
    "review_modflow_entries",
]


def aquifer_k_basis(k_source: str) -> tuple[InputBasis, str | None]:
    """Map an archetype ``k_source`` token to a structured (basis, real_source).

    ``user_supplied`` -> user; ``soil_pedotransfer`` -> derived (SoilGrids texture
    via the Saxton-Rawls pedotransfer function); anything else (``demo_default``,
    ``demo``, None) -> a labelled demo default.
    """
    token = (k_source or "").strip().lower()
    if token == "user_supplied":
        return "user", None
    if token == "soil_pedotransfer":
        return "derived", "SoilGrids texture via the Saxton-Rawls (2006) pedotransfer function"
    return "default_demo", None


def aquifer_k_review_entry(
    *,
    k_source: str,
    k_ms: float | None,
    porosity: float | None,
    note: str,
) -> SyntheticInput:
    """Build the structured aquifer-conductivity provenance entry.

    ``note`` is the existing prose caveat (kept verbatim so the narration is
    unchanged); ``basis`` + ``real_source_if_any`` make it machine-readable.
    """
    basis, real_source = aquifer_k_basis(k_source)
    return SyntheticInput(
        param="aquifer_k_ms",
        value=(round(float(k_ms), 8) if k_ms is not None else None),
        units="m/s",
        basis=basis,
        real_source_if_any=real_source,
        note=note,
    )


async def review_modflow_entries(
    *,
    tool_name: str,
    entries: list[SyntheticInput],
    params: dict[str, Any] | None = None,
    input_mode: str | None = None,
) -> ReviewOutcome:
    """Run ``gate_input_review`` over ``entries`` and return the outcome.

    For composers that return MULTIPLE layers (e.g. multi-species plumes): the
    caller stamps ``outcome.entries`` onto each layer's ``synthetic_inputs`` and
    short-circuits on ``outcome.cancelled``. In ``auto`` mode this is a
    pass-through that returns the entries unchanged.
    """
    return await gate_input_review(
        tool_name=tool_name, mode=input_mode, entries=entries, params=params or {},
    )


async def gate_and_stamp_modflow_inputs(
    *,
    tool_name: str,
    layer: Any,
    entries: list[SyntheticInput],
    params: dict[str, Any] | None = None,
    input_mode: str | None = None,
) -> tuple[Any, ReviewOutcome]:
    """Run ``gate_input_review`` over ``entries`` and stamp them onto ``layer``.

    In ``auto`` mode (or with no live session) the gate is a pass-through: it
    returns the entries unchanged and this stamps them onto
    ``layer.synthetic_inputs`` (via ``model_copy``) so the demo-default provenance
    is machine-readable on the envelope. In ``user_gated`` mode it pauses for
    review before the layer is finalized. Returns ``(layer, review)``; the caller
    checks ``review.cancelled`` to short-circuit with a typed cancel error.
    """
    review = await gate_input_review(
        tool_name=tool_name, mode=input_mode, entries=entries, params=params or {},
    )
    if review.cancelled:
        return layer, review
    try:
        stamped = layer.model_copy(update={"synthetic_inputs": list(review.entries)})
    except Exception as exc:  # noqa: BLE001 -- never break the run on a stamp failure
        logger.warning(
            "%s: could not stamp synthetic_inputs onto the layer (%s); "
            "provenance stays on the summary prose", tool_name, exc)
        stamped = layer
    return stamped, review
