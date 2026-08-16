"""Runtime confirm-card estimate + provider resolution (the gate-collapse engine side).

The RUNTIME half of the ADR 0273 gate-collapse contract (the declaration half is
``trid3nt_contracts.gate_spec``): a :class:`CardEstimate` is what a tool's declared
``estimate_provider`` returns -- the built confirm card (a
``PayloadWarningEnvelopePayload``) plus the opaque per-engine ``tail_state`` the pin
provider reads on a proceed / narrow_scope decision. It carries a live envelope, so it
lives server-side, not in the serializable contract layer (mirroring the
ResolutionSpec / ResolvedResolution split).

The generic gate engine imports the estimate + pin providers by the dotted paths the
:class:`~trid3nt_contracts.gate_spec.GateSpec` names, so engine knowledge stays in the
engine module (the gate_input_review precedent) and the server dispatch is one uniform
registry-driven call, not a per-engine ``if/elif`` chain.
"""
from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = ["CardEstimate", "resolve_provider", "call_provider"]


@dataclass(frozen=True)
class CardEstimate:
    """A built confirm card + the tail state its pin provider reads.

    ``envelope`` is the ``PayloadWarningEnvelopePayload`` the gate emits on the wire
    (the plugin-rendered ``tool-payload-warning`` card). Its ``granularity`` /
    ``time_scale`` blocks already carry the generic surfaces a card shows -- estimated
    cells / nodes, est runtime, the ladder rungs, preview stats -- so an engine fills in
    what applies and leaves the rest ``None``. ``envelope=None`` is the "no gate needed"
    signal (the fetch_landcover no-coarsening skip): the engine dispatches as-is.

    ``tail_state`` is the OPAQUE per-engine state the declared pin provider consumes to
    compute the approved-params delta on a decision (the SWMM autoscale result + DEM path
    for the real-cap re-probe, the TELEMAC preview stats, the fetch suggestion, the flood
    autoscale + resolved cadence). Empty for a plain proceed/cancel gate (no levers).
    """

    envelope: Any | None
    tail_state: dict[str, Any] = field(default_factory=dict)


def resolve_provider(dotted: str) -> Callable[..., Any]:
    """Import a ``module.path:attr`` (or ``module.path.attr``) provider reference.

    The :class:`~trid3nt_contracts.gate_spec.GateSpec` names its estimate / pin providers
    by dotted path so the contract carries no server import; the engine resolves them
    lazily off the tool's own module here. Accepts both the ``:`` (module:attr) and the
    dotted (module.attr) forms.
    """
    if ":" in dotted:
        module_path, attr = dotted.split(":", 1)
    else:
        module_path, attr = dotted.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, attr)


async def call_provider(dotted: str, *args: Any, **kwargs: Any) -> Any:
    """Call a dotted provider, awaiting it when it is a coroutine (async providers).

    The TELEMAC mesh-preview estimate provider runs an async mesh worker and the SWMM
    real-cap pin provider offloads a DEM re-probe, so providers may be coroutines; a
    plain proceed/cancel builder is sync. This normalizes the call so the gate engine
    treats both uniformly. Unknown kwargs a sync provider does not accept (e.g. an
    ``emitter`` only TELEMAC reads) are filtered against the callable's signature so
    every provider gets a uniform call.
    """
    fn = resolve_provider(dotted)
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
