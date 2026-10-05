"""The card a direct run opens for the inputs its call left out or that refused.

It is the input review gate's own card: one row per open input, each dropdown
listing exactly what that input's accept rule takes. A pick is accepted or
refused by name on the redrawn card, and the card's proceed is the launch.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.payload_warning import ParamOption, ParamSheet, ParamSheetRow

from trid3nt_server.inputs.accept import InputRefused, accept, inputs_of, offers
from trid3nt_server.inputs.gate.errors import UserDeclinedError
from trid3nt_server.inputs.user_input import UserInputError

__all__ = ["RunInputsDeclinedError", "open_inputs", "review_run_inputs"]

#: Rounds a card is redrawn on picks before the run is refused unlaunched.
_ROUNDS = 6


class RunInputsDeclinedError(UserDeclinedError):
    """The person cancelled the inputs card a direct run opened."""

    error_code: str = "USER_INPUT_CANCELLED"

    def __init__(self, tool_name: str) -> None:
        super().__init__(
            f"{tool_name} was cancelled on its inputs card; it did not run.",
            f"The user cancelled the inputs card for {tool_name}. It did not run.")


async def open_inputs(fn: Any, args: Mapping[str, Any],
                      layers: Sequence[Mapping[str, Any]]
                      ) -> tuple[dict[str, Any], dict[str, str]]:
    """``(taken, refused)``: the call's stated inputs through their accept rule,
    each either taken as the tool reads it or refused with its reason."""
    taken, refused = {}, {}
    for name, value in args.items():
        if name not in inputs_of(fn):
            taken[name] = value
            continue
        try:
            taken[name] = await asyncio.to_thread(accept, fn, name, value, layers)
        except InputRefused as exc:
            refused[name] = str(exc)
    return taken, refused


async def review_run_inputs(tool_name: str, fn: Any, args: Mapping[str, Any], *,
                            layers: Sequence[Mapping[str, Any]],
                            emitter: Any) -> dict[str, Any]:
    """The args a direct run launches with -> after its card, where one opens.

    A call stating every input acceptably, or stating ``input_mode='auto'``,
    opens none; otherwise the card lists the inputs left out or refused."""
    from trid3nt_server.inputs.gate.input_review import gate_input_review

    taken, refused = await open_inputs(fn, args, layers)
    if str(args.get("input_mode") or "").lower() == "auto" and not refused:
        return taken
    rows = [name for name in inputs_of(fn) if name not in taken]
    if not rows:
        return taken
    picked: dict[str, Any] = {}
    lists = {name: await asyncio.to_thread(offers, fn, name, layers)
             for name in rows}

    async def card() -> Any:
        from trid3nt_server.inputs.gate.input_review import GateCard

        drawn = [_row(name, picked.get(name, args.get(name)), lists[name],
                      refused.get(name)) for name in rows]
        return GateCard(
            lines=[f"{row.name} = {'' if row.value is None else row.value}"
                   + (f" (refused: {row.note})" if row.note else "")
                   for row in drawn],
            param_sheet=ParamSheet(workflow=tool_name, rows=drawn,
                                   title=f"Supply the inputs of {tool_name}"))

    async def revise(revised: Mapping[str, Any]) -> None:
        for name, value in revised.items():
            if name not in rows:
                continue
            picked[name] = value
            try:
                taken[name] = await asyncio.to_thread(accept, fn, name, value,
                                                      layers)
                refused.pop(name, None)
            except InputRefused as exc:
                taken.pop(name, None)
                refused[name] = str(exc)

    outcome = await gate_input_review(
        tool_name=tool_name, mode="user_gated",
        entries=[SyntheticInput(param=name, value=None, basis="user")
                 for name in rows],
        params={}, present=card, apply_revision=revise, max_rounds=_ROUNDS,
        emitter=emitter)
    if outcome.proceed:
        return taken
    if outcome.cancel_code == "declined":
        raise RunInputsDeclinedError(tool_name)
    raise UserInputError(outcome.cancel_reason or f"{tool_name} did not run.",
                         code="USER_INPUT_CANCELLED")


def _row(name: str, value: Any, listed: list[tuple[Any, str]] | None,
         refusal: str | None) -> ParamSheetRow:
    """One open input as the card draws it: its dropdown where it is picked."""
    shown = value if isinstance(value, (int, float, str, bool, list)) \
        or value is None else str(value)
    return ParamSheetRow(
        name=name, value=shown, door="user", basis="user",
        note=refusal, source_badge=(refusal or "")[:200],
        options=None if listed is None else [
            ParamOption(value=option, label=label) for option, label in listed])
