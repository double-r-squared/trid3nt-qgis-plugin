"""Declarative confirm-gate metadata: the DECLARATION a gated tool carries.

Serializable and server-free - the estimate and pin providers are named by
DOTTED IMPORT PATH rather than as callables, so engine knowledge stays in the
engine and this layer stays a pure shape.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import ContractModel

__all__ = ["GateKind", "GateSpec", "LeverSpec"]


#: What the gate guards. ``"solver"``: a model-supplied ``confirmed`` is stripped before gating and re-injected only
#: on an explicit proceed. ``"fetch"``: ``confirmed`` is not in play, only the resolution lever is pinned.
GateKind = Literal["solver", "fetch"]


class LeverSpec(ContractModel):
    """One user-overridable lever a confirm card offers.
    A DECLARATION only - the engine-specific pinning arithmetic stays in the
    tool's pin provider, so rendering and enforcement can be uniform."""

    name: str = Field(min_length=1)
    #: The approved-params key the pin writes; a ``narrow_scope`` override is routed to the pin provider under it.
    param: str = Field(min_length=1)
    unit: str = "m"
    #: A discrete value ladder, offered as chips; exclusive with the range window.
    rungs: tuple[float, ...] | None = None
    #: A continuous override window, finest / coarsest inclusive, that the pin provider clamps into; None leaves a side unbounded.
    range_min: float | None = None
    range_max: float | None = None
    #: True pins the suggested value on a plain ``proceed``, so the run matches the card; False leaves the lever inert until an explicit ``narrow_scope``.
    pin_on_proceed: bool = True

    @model_validator(mode="after")
    def _validate_lever(self) -> LeverSpec:
        """A lever declares a discrete ladder xor a continuous window, or neither (a free-edit lever); with both bounds, ``min <= max``."""
        has_window = self.range_min is not None or self.range_max is not None
        if self.rungs is not None and has_window:
            raise ValueError(
                "LeverSpec: declare a discrete rungs ladder OR a continuous "
                "range_min/range_max window, not both."
            )
        if (
            self.range_min is not None
            and self.range_max is not None
            and self.range_min > self.range_max
        ):
            raise ValueError(
                f"LeverSpec: range_min {self.range_min} > range_max {self.range_max} "
                f"for param {self.param!r}."
            )
        return self


class GateSpec(ContractModel):
    """A tool's DECLARED confirm gate.
    Presence of this on a tool's registration metadata is the ONE membership
    signal the gate engine reads - there is no name set to join."""

    kind: GateKind
    #: Dotted path to a pure estimate builder ``(params) -> CardEstimate`` in the tool's own module (a coroutine is awaited); an envelope of None means no gate needed.
    estimate_provider: str = Field(min_length=1)
    #: Dotted path to a pure decision tail ``(decision, revised_args, params, tail_state) -> dict`` returning the approved-params delta; None for a lever-less gate.
    pin_provider: str | None = None
    levers: tuple[LeverSpec, ...] = ()
    title: str = ""
    rationale: str = ""

    @model_validator(mode="after")
    def _validate_gate(self) -> GateSpec:
        """A gate with levers must name a pin provider: a lever with nothing to honour ``narrow_scope`` is a dead knob."""
        if self.levers and self.pin_provider is None:
            raise ValueError(
                "GateSpec: a gate declaring levers must name a pin_provider to honour a "
                "narrow_scope override (a lever with no pin is a dead knob)."
            )
        return self
