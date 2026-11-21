"""The input review the SDK asks before a run, and the one registry of what answers it.

The SDK never asks a person itself: a host registers the gate that does (``register_gate``). With
none registered, nothing asks: ``auto`` proceeds with every input labeled, and a ``user_gated``
review refuses ``NO_SESSION`` naming the run, as it does with no live session to present on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from trid3nt_contracts.common import ORIGIN_LABELS, Origin, SyntheticInput
from trid3nt_contracts.payload_warning import ParamSheet
from trid3nt.settings import setting

__all__ = [
    "DrawOutcome",
    "GateCard",
    "InputGateMode",
    "ReviewOutcome",
    "UserDeclinedError",
    "gate_draw_input",
    "gate_input_review",
    "no_session",
    "register_gate",
    "render_input_review_lines",
    "resolve_input_gate_mode",
]

#: Run-mode lever for every gated thing. ``auto``
#: proceeds immediately with labeled inputs; ``user_gated`` pauses for review.
InputGateMode = Literal["auto", "user_gated"]


def resolve_input_gate_mode(forced: InputGateMode | None = None) -> InputGateMode:
    """The mode in force: ``forced`` where a gate must always ask, else ``auto`` where the person's AUTO setting is on.

    The setting is the person's alone - no call and no run states it - so a ready run launches on an act the person chose to skip."""
    return forced or ("auto" if setting("AUTO") else "user_gated")


def _entry_field(e: Any, name: str) -> Any:
    return e.get(name) if isinstance(e, dict) else getattr(e, name, None)


def render_input_review_lines(entries: Any) -> list[str]:
    """One compact line per resolved input: ``param = value [origin, source]``.

    ``source`` names the fetcher or dataset only where there is one."""
    lines: list[str] = []
    for e in entries or []:
        value = _entry_field(e, "value")
        units = _entry_field(e, "units")
        source = _entry_field(e, "real_source_if_any")
        val_txt = "?" if value is None else f"{value}"
        if units:
            val_txt = f"{val_txt} {units}"
        tag = ORIGIN_LABELS[Origin(_entry_field(e, "basis"))]
        src = f", {source}" if source else ""
        lines.append(f"{_entry_field(e, 'param')} = {val_txt} [{tag}{src}]")
    return lines

@dataclass(frozen=True)
class GateCard:
    """What a gate's card says this round: the body lines, the sheet, the args.

    A caller with a thing of its own under review builds this each round, so what
    the person reads is measured now rather than when the gate opened."""

    lines: list[str]
    param_sheet: "ParamSheet | None" = None
    tool_args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReviewOutcome:
    """The result of an input-review gate.

    ``proceed`` means run with ``params`` and stamp ``entries`` into the result;
    ``cancelled`` means the template returns a typed cancel error and does NOT solve."""

    proceed: bool
    entries: list[SyntheticInput]
    params: dict[str, Any]
    cancelled: bool = False
    cancel_reason: str | None = None
    #: Which cancel this is - ``no_session``, ``timeout``, ``declined`` or
    #: ``not_approved`` - so a caller can raise its own typed error by it.
    cancel_code: str | None = None
    mode: InputGateMode = "auto"
    rounds_used: int = 0


def no_session(tool_name: str, forced: InputGateMode | None = None) -> str | None:
    """What a user-gated run with no live session refuses on, or ``None`` where it may launch: a workflow says it at its fill, the gate where a card would have opened."""
    from trid3nt.render.emission import current_emitter

    if resolve_input_gate_mode(forced) == "auto" or (current_emitter() is not None and _ASK):
        return None
    return (
        f"NO_SESSION: {tool_name} did not run - it waits for a person "
        "to proceed on its review card and no session is open. Open a "
        "session to review and launch it, or turn AUTO on in "
        "Settings to launch it at once.")

@dataclass(frozen=True, slots=True)
class DrawOutcome:
    """What the canvas gave back: a value, or a reason there is none."""

    value: Any = None
    reason: str | None = None

    @property
    def drawn(self) -> bool:
        return self.value is not None


class UserDeclinedError(RuntimeError):
    """The user answered a gate card with cancel, so the tool never ran.

    ``declined`` is the marker the emitter, the summarizer and the circuit breaker read
    without importing this module; ``decline_note`` is the words the model relays."""

    declined: bool = True
    retryable: bool = False

    def __init__(self, message: str, decline_note: str) -> None:
        super().__init__(message)
        self.decline_note = decline_note


#: What answers the review and the draw, keyed by ask; empty until a host registers its gate.
_ASK: dict[str, Callable[..., Awaitable[Any]]] = {}


def register_gate(*, review: Callable[..., Awaitable[ReviewOutcome]],
                  draw: Callable[..., Awaitable[DrawOutcome]]) -> None:
    """Install the gate every review and draw asks; a later registration replaces the earlier one."""
    _ASK.update(review=review, draw=draw)


async def gate_input_review(*, tool_name: str, entries: list[SyntheticInput],
                            params: dict[str, Any], mode: InputGateMode | None = None,
                            **card: Any) -> ReviewOutcome:
    """Present what is under review before it runs, and ask, through the registered gate.

    The gate takes ``max_rounds``, ``ttl_seconds``, ``param_sheet``, ``present``, ``apply_revision``,
    ``blocked`` and ``emitter`` as stated. With none registered nothing asks (the module's stance)."""
    ask = _ASK.get("review")
    if ask is not None:
        return await ask(tool_name=tool_name, entries=entries, params=params, mode=mode, **card)
    if resolve_input_gate_mode(mode) == "auto":
        return ReviewOutcome(proceed=True, entries=list(entries), params=dict(params), mode="auto")
    return ReviewOutcome(
        proceed=False, entries=list(entries), params=dict(params), cancelled=True,
        mode="user_gated", cancel_code="no_session", cancel_reason=no_session(tool_name, mode))


async def gate_draw_input(*, tool_name: str, param: str, geometry: str, prompt: str,
                          purpose: str | None = None, ttl_seconds: int | None = None) -> DrawOutcome:
    """Ask the registered gate for ONE param's value drawn on the canvas; with none, nothing is drawn."""
    ask = _ASK.get("draw")
    if ask is None:
        return DrawOutcome(reason="there is no live map session to draw on")
    return await ask(tool_name=tool_name, param=param, geometry=geometry, prompt=prompt,
                     purpose=purpose, ttl_seconds=ttl_seconds)
