"""Declarative confirm-gate metadata: the DECLARATION a gated tool carries.

Serializable and server-free - the estimate and pin providers are named by
DOTTED IMPORT PATH rather than as callables, so engine knowledge stays in the
engine and this layer stays a pure shape.
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import GraceModel

__all__ = ["GateKind", "GateSpec", "LeverSpec"]


#: What KIND of consequence the gate guards. ``"solver"`` - a consequential engine
#: run: a model-supplied ``confirmed`` is STRIPPED before gating and re-injected
#: only on an explicit proceed. ``"fetch"`` - a heavy raster download or merge:
#: ``confirmed`` is not in play at all, only the resolution lever is pinned.
GateKind = Literal["solver", "fetch"]


class LeverSpec(GraceModel):
    """One user-overridable lever a confirm card offers.
    A DECLARATION only - the engine-specific pinning arithmetic stays in the
    tool's pin provider, so rendering and enforcement can be uniform."""

    #: Human label for the lever. Card copy only.
    name: str = Field(min_length=1)
    #: The approved-params key the pin WRITES. A ``narrow_scope`` override's
    #: revised value is routed to the pin provider under this key.
    param: str = Field(min_length=1)
    #: Value unit - ``"m"`` for a resolution lever, ``"min"`` for a cadence,
    #: ``"hr"`` for a window.
    unit: str = "m"
    #: A DISCRETE value ladder, offered as chips. Mutually exclusive with the
    #: range window; ``None`` when the lever is a continuous free edit.
    rungs: tuple[float, ...] | None = None
    #: A continuous override window, FINEST / COARSEST inclusive, that the pin
    #: provider clamps a chosen value into. ``None`` leaves that side unbounded.
    range_min: float | None = None
    range_max: float | None = None
    #: True pins the SUGGESTED value the card showed on a plain ``proceed``, so
    #: the run matches the card the user saw. False leaves the lever inert until
    #: an explicit ``narrow_scope`` override.
    pin_on_proceed: bool = True

    @model_validator(mode="after")
    def _validate_lever(self) -> LeverSpec:
        """A lever declares a discrete ladder XOR a continuous window, or neither.
        Neither is legitimate - a free-edit lever whose bounds the deck itself
        enforces. With both range bounds set, ``min <= max``."""
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


class GateSpec(GraceModel):
    """A tool's DECLARED confirm gate.
    Presence of this on a tool's registration metadata is the ONE membership
    signal the gate engine reads - there is no name set to join."""

    kind: GateKind
    #: Dotted import path to a PURE builder ``(params: dict) -> CardEstimate``
    #: exported from the tool's OWN module; a coroutine is awaited. An estimate
    #: whose envelope is ``None`` means "no gate needed, dispatch as-is".
    estimate_provider: str = Field(min_length=1)
    #: Dotted import path to a PURE decision tail
    #: ``(decision, revised_args, params, tail_state) -> dict`` returning the
    #: approved-params DELTA to merge. ``None`` for a lever-less gate, where the
    #: generic tail injects ``confirmed`` for a solver and nothing for a fetch.
    pin_provider: str | None = None
    #: The declared levers. Empty for a plain proceed/cancel gate.
    levers: tuple[LeverSpec, ...] = ()
    #: Card copy naming the gate. The user-facing caption is still the
    #: recommendation the estimate provider bakes into the envelope; these two
    #: are metadata for the audit surface.
    title: str = ""
    rationale: str = ""

    @model_validator(mode="after")
    def _validate_gate(self) -> GateSpec:
        """A gate WITH levers must name a pin provider to enforce them.
        A lever offered with nothing to honour a ``narrow_scope`` override is a
        dead knob, so the inconsistency is refused at declaration time."""
        if self.levers and self.pin_provider is None:
            raise ValueError(
                "GateSpec: a gate declaring levers must name a pin_provider to honour a "
                "narrow_scope override (a lever with no pin is a dead knob)."
            )
        return self
