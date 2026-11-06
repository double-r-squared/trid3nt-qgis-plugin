"""``set_config`` - change one changeable setting, for the user or for the next run, after the person approves it on a card.

Find a setting and its range with describe_keywords(module="settings", query=...); this tool states the value. ``scope="user"`` writes the person's settings file and holds until reset; ``scope="run"`` binds the session's next run only. ``reset=True`` drops the value so the default stands. A value outside the entry's range refuses before any card opens, and the card shows the name, the value in force, the new value, the scope and what the setting decides.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.payload_warning import ParamOption, ParamSheet, ParamSheetRow
from trid3nt_contracts.tool_registry import AtomicToolMetadata

from trid3nt_server.inputs.gate.errors import UserDeclinedError
from trid3nt_server.inputs.gate.input_review import GateCard, gate_input_review
from trid3nt_server.inputs.user_input import UserInputError
from trid3nt_server.settings import (
    entry, resolve, session_run, state_session_run, typed, user_values, write_user)
from trid3nt_server.tools import register_tool
from trid3nt_server.workflows.runtime.module import SlotRefused

__all__ = ["SettingDeclinedError", "set_config"]

_SCOPES = ("user", "run")


class SettingDeclinedError(UserDeclinedError):
    """The person cancelled the card a setting change was shown on."""

    error_code: str = "USER_INPUT_CANCELLED"

    def __init__(self, name: str) -> None:
        super().__init__(
            f"the change to setting {name} was cancelled on its card; nothing changed.",
            f"The user cancelled the card for setting {name}. Nothing changed.")


def _row(name: str, shown: Any, slot: Any, scope: str, was: tuple[Any, str],
         refusal: str | None) -> ParamSheetRow:
    value, level = was
    return ParamSheetRow(
        name=name, value=shown, units=slot.unit or None, desc=slot.desc[:512],
        bounds=slot.whole_bounds, note=refusal or f"in force: {value} ({level}); sets for {scope}",
        source_badge=(refusal or slot.source)[:200], group=slot.rubrique[0] if slot.rubrique else "",
        options=[ParamOption(value=c, label=str(c)) for c in slot.choices]
        if slot.choices and not isinstance(slot.choices, Mapping) else None)


@register_tool(
    AtomicToolMetadata(name="set_config", ttl_class="live-no-cache"),
    read_only_hint=False,
    open_world_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
)
async def set_config(name: str, value: float | int | str | bool | None = None,
                     scope: str = "user", reset: bool = False) -> dict[str, Any]:
    """Change one setting - for the user, or for the session's next run - once the person approves it on a card.

    ROUTING: the user asks to change a changeable judgment the system carries (how far a hole is filled, how large a merge may be,
    how long a card waits). Find the name and range first: `describe_keywords(module="settings", query="fill distance")`.
    Never states a physics value: a setting is a limit or a method, and a value outside its range refuses.

    Args:
        name: The setting's name, as describe_keywords lists it (FILL_MAX_DISTANCE).
        value: The new value; omit with reset=True.
        scope: "user" writes the person's settings file and holds until reset; "run" binds the session's next run only.
        reset: True drops the value at that scope so the next level down (the default for "user") stands.

    Returns the setting, its previous and new value at that scope, and where it was written."""
    scope = str(scope or "").strip().lower()
    if scope not in _SCOPES:
        raise SlotRefused(f"scope is one of {', '.join(_SCOPES)}; got {scope!r}.")
    slot = entry(name)
    ident = slot.identifier
    if not reset and value is None:
        raise SlotRefused(f"{ident} needs a value, or reset=True to drop it.")
    new = None if reset else typed(slot, value)
    was = resolve(ident)
    picked: dict[str, Any] = {"value": new}
    refused: dict[str, str] = {}

    def blocked() -> str | None:
        return f"{ident} (refused)" if refused else None

    async def card() -> GateCard:
        shown = picked["value"] if picked["value"] is not None else slot.engine_default
        title = (f"Reset {ident} for {scope}" if reset else f"Set {ident} for {scope}")
        row = _row(ident, shown, slot, scope, was, refused.get("note"))
        return GateCard(
            lines=[f"{ident}: {was[0]} ({was[1]}) -> {shown if not reset else 'default ' + str(slot.engine_default)} for {scope}",
                   slot.desc],
            param_sheet=ParamSheet(workflow="set_config", rows=[row], title=title))

    async def revise(revised: Mapping[str, Any]) -> None:
        if ident not in revised:
            return
        try:
            picked["value"] = typed(slot, revised[ident])
            refused.clear()
        except SlotRefused as exc:
            refused["note"] = str(exc)

    outcome = await gate_input_review(
        tool_name="set_config", mode="user_gated",
        entries=[SyntheticInput(param=ident, value=new, basis="user")], params={},
        present=card, apply_revision=revise, blocked=blocked)
    if not outcome.proceed:
        if outcome.cancel_code == "declined":
            raise SettingDeclinedError(ident)
        raise UserInputError(outcome.cancel_reason or "set_config did not run.",
                             code="USER_INPUT_CANCELLED")
    final = None if reset else picked["value"]
    if scope == "user":
        previous = user_values().get(ident)
        where = str(await asyncio.to_thread(write_user, ident, final))
    else:
        from trid3nt_server.render.pipeline_emitter import current_emitter

        session = current_emitter().session_id
        previous = session_run(session).get(ident)
        state_session_run(session, ident, final)
        where = f"session {session}, its next run"
    return {"name": ident, "scope": scope, "previous": previous, "value": final,
            "in_force": resolve(ident)[0], "written_to": where}
