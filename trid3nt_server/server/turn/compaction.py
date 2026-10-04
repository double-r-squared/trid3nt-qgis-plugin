"""The compaction card: one durable tool card for one context-compaction pass,
persisted running at mint and upserted to its terminal state with the final label.
"""

from __future__ import annotations

import logging
from typing import Any

from trid3nt_server.model.guards.context_budget import COMPACTING_LABEL, compaction_complete_label

logger = logging.getLogger("trid3nt_server.server.turn.compaction")

# Compaction is one atomic local pass, not a dispatch and a solve, so it takes a
# ``role="tool"`` card and never a compute one - there is no dispatched run to
# bind. It starts running and is later renamed and completed, riding the same
# wire shape as every other tool card rather than a new envelope type.


async def mint_compaction_card(*, emitter: Any) -> str | None:
    """Mint the durable running compaction card and return its step id.
    ``None`` on any failure: the card is an observability affordance, never a
    gate on the compaction it describes.
    """
    if emitter is None:
        return None
    try:
        step_id = await emitter.add_durable_step(
            name=COMPACTING_LABEL, tool_name="context:compact"
        )
        # Persist the running card NOW, so a reconnect mid-pass replays the
        # spinning card instead of dropping it; the terminal write upserts the
        # SAME row.
        await emitter.persist_running_compute_card(step_id)
        return step_id
    except Exception as exc:  # noqa: BLE001 -- observability, never break the turn
        logger.warning("mint_compaction_card failed (non-fatal): %s", exc)
        return None


async def complete_compaction_card(
    *,
    emitter: Any,
    step_id: str | None,
    before_tokens: int,
    after_tokens: int,
) -> None:
    """Drive the minted compaction card to its terminal state.
    A no-op when the mint failed or was never called; an emit or persist failure
    is swallowed rather than raised into the turn.
    """
    if emitter is None or step_id is None:
        return
    try:
        # Renamed BEFORE the completion, so the live terminal emission and the
        # persisted upsert both carry the final text rather than the stale
        # running label.
        emitter.rename_step(
            step_id, name=compaction_complete_label(before_tokens, after_tokens)
        )
        await emitter.mark_complete(step_id)
        # Upserts the SAME row persisted running at mint, so the card survives a
        # reopen with its final renamed label and state.
        await emitter.persist_terminal_compute_card(step_id)
    except Exception as exc:  # noqa: BLE001 -- observability, never break the turn
        logger.warning("complete_compaction_card failed (non-fatal): %s", exc)
