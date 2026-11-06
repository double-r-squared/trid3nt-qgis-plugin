"""The card a keyed source asks its key on.

One masked row named by the credential. The client stores the typed key in its own credential store and pushes it over
``secret-add``; the reply carries the decision and no value, so a proceed is launched only once a key is held that differs
from the rejected one.
"""

from __future__ import annotations

from typing import Any

from trid3nt_contracts.payload_warning import ParamSheet, ParamSheetRow

from trid3nt_server.inputs.gate.errors import UserDeclinedError
from trid3nt_server.tools.fetchers._fetch_common import KeyAskError, KeyRejectedError

__all__ = ["KeyDeclinedError", "ask_for_key"]


class KeyDeclinedError(UserDeclinedError):
    """The person cancelled the card that asked for a source's key."""

    error_code: str = "USER_INPUT_CANCELLED"

    def __init__(self, asked: KeyAskError) -> None:
        super().__init__(
            f"{asked.source} was not run: its {asked.label} key card was cancelled.",
            f"The user declined to give a {asked.label} key on the card, so {asked.source} did not run.")


async def ask_for_key(tool_name: str, asked: KeyAskError, *, emitter: Any) -> bool:
    """``True`` once the person has stored a key to retry with; ``False`` when no card could be answered (no session, expired, rounds spent); a cancel raises."""
    from trid3nt_server.inputs.gate.input_review import GateCard, gate_input_review
    from trid3nt_server.model.credentials.resolver import stored_value

    rejected = isinstance(asked, KeyRejectedError)
    before = stored_value(asked.credential) if rejected else None

    def blocked() -> str | None:
        now = stored_value(asked.credential)
        return None if now and now != before else f"{asked.label} key (required)"

    async def card() -> GateCard:
        note = ("The provider rejected the stored key." if rejected else f"{asked.source} takes a key.")
        if asked.link:
            note += f" The provider points to {asked.link}."
        row = ParamSheetRow(
            name=asked.credential, value=None, note=note, source_badge=asked.label[:200],
            desc="Kept in QGIS's credential store; it does not pass through the chat.",
            secret="rejected" if rejected else "required")
        title = f"{asked.label} key {'was rejected' if rejected else 'is needed'}"
        return GateCard(lines=[f"{asked.credential}: {note}"],
                        param_sheet=ParamSheet(workflow=tool_name, rows=[row], title=title))

    outcome = await gate_input_review(
        tool_name=tool_name, mode="user_gated", entries=[], params={},
        present=card, blocked=blocked, emitter=emitter)
    if outcome.proceed:
        return True
    if outcome.cancel_code == "declined":
        raise KeyDeclinedError(asked)
    return False
