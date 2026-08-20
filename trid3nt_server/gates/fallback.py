"""The ONE fallback gate: the loudness floor over the pending-confirm spine.

The floor, by consequence class:

  * ``same_data`` -- another mirror of the SAME dataset. Walks silently; still
    recorded on the activation.
  * ``cross_dataset`` -- a different dataset, method or resolution. Narrates
    loudly always; PAUSES for approval in ``user_gated`` mode.
  * ``synthetic`` -- a value with no real data source. ALWAYS pauses, and its
    labeled default is REFUSE (law 9: a physics-consequential invention never
    runs unapproved).

Declining is not a run-cancel: the walker treats a declined rung as one it may
not take and descends to the next, ending at the ladder's typed REFUSE. AUTO and
headless runs apply the labeled default, so a canary never hangs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from trid3nt_contracts import new_ulid
from trid3nt_contracts.payload_warning import PayloadWarningEnvelopePayload

logger = logging.getLogger("trid3nt_server.gates.fallback")

__all__ = ["gate_fires", "labeled_default", "confirm_fallback"]

#: Gate wait cap (seconds), matching the input-review / solver-confirm gates.
_TTL_SECONDS = 300


def gate_fires(consequence: str, gate_mode: str | None) -> bool:
    """Whether descending to a rung of ``consequence`` needs user approval."""
    if consequence == "synthetic":
        return True
    if consequence == "cross_dataset":
        from trid3nt_server.gates.input_review import resolve_input_gate_mode

        return resolve_input_gate_mode(gate_mode) == "user_gated"
    return False


def labeled_default(consequence: str) -> bool:
    """The default applied when there is nobody to ask: proceed, or refuse."""
    return consequence != "synthetic"


def _recommendation(
    capability: str, rung: Any, covered_fraction: float, gap_note: str | None
) -> str:
    served = (
        f"The declared source served {covered_fraction * 100:.0f}% of this request. "
        if covered_fraction > 0.0
        else ""
    )
    gap = f"{gap_note} " if gap_note else ""
    default = (
        "refuse (this alternative has no real data source)"
        if rung.consequence == "synthetic"
        else "proceed on the alternative, loudly labeled"
    )
    return (
        f"{capability}: {served}{gap}Approve the fallback rung "
        f"'{rung.name}' [{rung.consequence}]? {rung.describes} "
        f"Declining does not cancel the run -- it refuses the substitution and "
        f"the tool returns its typed error. Default if unanswered: {default}."
    )[:512]


def confirm_fallback(
    *,
    capability: str,
    rung: Any,
    gate_mode: str | None = None,
    covered_fraction: float = 0.0,
    gap_note: str | None = None,
) -> bool:
    """Ask (or apply the labeled default) before descending to ``rung``.

    Returns True when the walker may take the rung. Callable from a worker thread
    (the fetch path is off-loaded): the coroutine is driven onto the emitter's
    bound loop, which is free while the composer is parked on the thread. On the
    loop thread itself a blocking wait would deadlock, so the labeled default
    applies -- never a hang.
    """
    if not gate_fires(rung.consequence, gate_mode):
        if rung.consequence == "cross_dataset":
            logger.warning(
                "fallback %s -> rung %s [cross_dataset] in auto mode: %s",
                capability, rung.name, rung.describes,
            )
        return True

    default = labeled_default(rung.consequence)
    from trid3nt_server.emission.pipeline_emitter import current_emitter

    emitter = current_emitter()
    loop = getattr(emitter, "_bound_loop", None) if emitter is not None else None
    on_loop = True
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        on_loop = False
    if emitter is None or loop is None or not loop.is_running() or on_loop:
        logger.info(
            "fallback gate %s -> rung %s: no channel to ask on; applying the "
            "labeled default (%s)",
            capability, rung.name, "proceed" if default else "refuse",
        )
        return default

    envelope = PayloadWarningEnvelopePayload(
        warning_id=new_ulid(),
        tool_name=capability,
        tool_args={"fallback_rung": rung.name, "consequence": rung.consequence},
        estimated_mb=0.0,
        threshold_mb=0.0,
        recommendation=_recommendation(capability, rung, covered_fraction, gap_note),
        options=["proceed", "cancel"],
        ttl_seconds=_TTL_SECONDS,
    )
    fut = asyncio.run_coroutine_threadsafe(
        _present_and_wait(emitter, envelope), loop
    )
    try:
        return bool(fut.result(timeout=_TTL_SECONDS + 30))
    except Exception as exc:  # noqa: BLE001 -- a gate fault applies the default
        logger.warning(
            "fallback gate %s -> rung %s faulted (%s); applying the labeled "
            "default (%s)",
            capability, rung.name, exc, "proceed" if default else "refuse",
        )
        return default


async def _present_and_wait(
    emitter: Any, envelope: PayloadWarningEnvelopePayload
) -> bool:
    from trid3nt_server.gates.pending import (
        _pop_pending_confirmation,
        _register_pending_confirmation,
    )

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_confirmation(emitter.session_id, envelope.warning_id, fut)
    await emitter.send_envelope("tool-payload-warning", envelope)
    try:
        # An unanswered gate raises out to the caller's labeled default rather
        # than reading as a decline (a timeout is nobody answering, not a "no").
        decision = await asyncio.wait_for(fut, timeout=float(envelope.ttl_seconds))
    finally:
        _pop_pending_confirmation(envelope.warning_id)
    return getattr(decision, "decision", None) == "proceed"
