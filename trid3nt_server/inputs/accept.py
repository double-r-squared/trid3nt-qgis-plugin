"""THE ACCEPT RULE of one input of a registered tool, and the values it would take.

An input named for a slot accepts what that slot's ingestion reads; an input
whose annotation enumerates its values accepts one of them; a typed input
accepts what parses as its type. What a dropdown lists is every case layer and
every choice that same rule accepts, so nothing is written per tool.
"""

from __future__ import annotations

import ast
import inspect
import json
from typing import Any, Literal, Mapping, Sequence, Union, get_args, get_origin, get_type_hints

from .slots import SLOTS, ingest_slot
from .user_input import UserInputError

__all__ = ["InputRefused", "accept", "choices_of", "inputs_of", "offers"]

#: What rides a call beside its inputs and is never an input to supply.
_CARRIED = ("input_mode",)


class InputRefused(UserInputError):
    """One input's value refused by its own accept rule, by name."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="INPUT_REFUSED")


def inputs_of(fn: Any) -> list[str]:
    """The inputs a caller supplies to ``fn``, in its signature's order."""
    return [name for name, prm in inspect.signature(fn).parameters.items()
            if not name.startswith("_") and name not in _CARRIED
            and prm.kind not in (prm.VAR_POSITIONAL, prm.VAR_KEYWORD)]


def _annotation(fn: Any, name: str) -> Any:
    """The resolved annotation with ``None`` unwrapped; ``Any`` when it does not resolve."""
    try:
        found = get_type_hints(fn).get(name, Any)
    except Exception:  # noqa: BLE001 - an unresolvable annotation states nothing
        return Any
    if get_origin(found) is Union or type(found).__name__ == "UnionType":
        members = [a for a in get_args(found) if a is not type(None)]
        literal = next((a for a in members if get_origin(a) is Literal), None)
        return literal or (members[0] if len(members) == 1 else found)
    return found


def choices_of(fn: Any, name: str) -> tuple[Any, ...] | None:
    """The values an ENUMERATED input takes, as its annotation states them."""
    found = _annotation(fn, name)
    return tuple(get_args(found)) if get_origin(found) is Literal else None


def _layer(value: Any, layers: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    key = value.get("layer") if isinstance(value, Mapping) else value
    return next((row for row in layers if isinstance(key, str)
                 and key in (row.get("layer_id"), row.get("uri"))), None)


def accept(fn: Any, name: str, value: Any,
           layers: Sequence[Mapping[str, Any]] = ()) -> Any:
    """``value`` as ``fn`` takes it under ``name``, or :class:`InputRefused` naming why.

    A case layer, by id or by ``{"layer": id}``, is taken as its uri. Runs the
    slot's ingestion, so call it off the loop."""
    layer = _layer(value, layers)
    if layer is not None:
        value = layer.get("uri")
    choices = choices_of(fn, name)
    if choices is not None:
        if value not in choices:
            raise InputRefused(f"{name} takes one of {list(choices)}; got {value!r}.")
        return value
    slot = SLOTS.get(name)
    if slot is not None and slot.ingest is not None:
        try:
            ingest_slot(name, value, label=name)
        except Exception as exc:  # noqa: BLE001 - the ingestion's refusal is the verdict
            raise InputRefused(
                f"{name} does not take {layer.get('name') if layer else value!r}: "
                f"{exc}") from exc
        return value
    return _typed(name, _annotation(fn, name), value)


def _typed(name: str, annotation: Any, value: Any) -> Any:
    """A typed value from the text a field carries; anything else as it came."""
    if not isinstance(value, str):
        return value
    kind = get_origin(annotation) or annotation
    try:
        if kind in (int, float):
            return kind(value.strip())
        if kind in (list, tuple, dict):
            return _literal(value.strip())
    except (ValueError, SyntaxError) as exc:
        raise InputRefused(
            f"{name} takes a {getattr(kind, '__name__', kind)}; {value!r} does "
            "not read as one.") from exc
    return value


def _literal(text: str) -> Any:
    """JSON first, then a Python literal: a field carries either spelling."""
    try:
        return json.loads(text)
    except ValueError:
        return ast.literal_eval(text)


def offers(fn: Any, name: str,
           layers: Sequence[Mapping[str, Any]]) -> list[tuple[Any, str]] | None:
    """``(value, label)`` for EXACTLY what ``name`` would accept, or ``None`` for
    an input whose value is typed rather than picked. Runs the slot's ingestion
    over each layer, so call it off the loop."""
    choices = choices_of(fn, name)
    if choices is not None:
        return [(choice, str(choice)) for choice in choices]
    slot = SLOTS.get(name)
    if slot is None or slot.ingest is None:
        return None
    taken = []
    for layer in layers:
        try:
            accept(fn, name, layer.get("layer_id"), layers)
        except InputRefused:
            continue
        taken.append((str(layer.get("layer_id")),
                      str(layer.get("name") or layer.get("layer_id"))))
    return taken
