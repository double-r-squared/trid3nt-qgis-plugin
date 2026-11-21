"""The runtime half of the confirm-card contract: estimates and provider lookup.

A :class:`CardEstimate` carries a LIVE envelope, so it lives server-side; providers are
named by dotted path, so the contract carries no server import.
"""
from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = ["CardEstimate", "resolve_provider", "call_provider"]


@dataclass(frozen=True)
class CardEstimate:
    """A built confirm card plus the tail state its pin provider reads.

    ``envelope=None`` is the "no gate needed" signal: the engine dispatches as-is."""

    #: The card the gate emits on the wire. An engine fills in the generic
    #: surfaces that apply to it and leaves the rest ``None``.
    envelope: Any | None
    #: OPAQUE per-engine state the pin provider consumes to compute the
    #: approved-params delta. Empty for a lever-less proceed/cancel gate.
    tail_state: dict[str, Any] = field(default_factory=dict)


def resolve_provider(dotted: str) -> Callable[..., Any]:
    """Import a ``module.path:attr`` (or ``module.path.attr``) provider reference.

    Both the ``:`` and the fully-dotted spelling resolve the same way."""
    if ":" in dotted:
        module_path, attr = dotted.split(":", 1)
    else:
        module_path, attr = dotted.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, attr)


async def call_provider(dotted: str, *args: Any, **kwargs: Any) -> Any:
    """Call a dotted provider, awaiting it when it is a coroutine.

    A provider that needs a worker or an offloaded probe is async; a plain
    proceed/cancel builder is sync, and the gate engine sees one shape."""
    fn = resolve_provider(dotted)
    # A kwarg a given provider does not declare is filtered out against its own
    # signature, so every provider can be called uniformly.
    try:
        sig = inspect.signature(fn)
        accepts_kw = {
            k: v
            for k, v in kwargs.items()
            if k in sig.parameters
            or any(
                p.kind is inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
        }
    except (TypeError, ValueError):
        accepts_kw = kwargs
    result = fn(*args, **accepts_kw)
    if inspect.isawaitable(result):
        return await result
    return result
