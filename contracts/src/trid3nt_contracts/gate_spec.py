"""Declarative confirm-gate metadata (the gate-collapse contract, ADR 0273).

NATE's design call: the per-engine solver/fetch confirm gates are built from tool
METADATA, not hand-wired name sets + per-engine ``if/elif`` branches. This module is
the machine-readable DECLARATION a gated tool carries on its
:class:`trid3nt_contracts.tool_registry.AtomicToolMetadata`; the server gate engine
reads it uniformly (membership = spec presence, no name set) exactly the way
:class:`ResolutionSpec` drives the resolution machinery.

Two collaborators, mirroring the ResolutionSpec / ResolvedResolution split:

* the DECLARATION lives here (serializable, no server imports): :class:`GateSpec` +
  :class:`LeverSpec`. It names WHICH pure functions build the card and pin the
  decision, and DECLARES the levers the card offers.
* the RUNTIME estimate (the built card + the opaque per-engine tail state the pin
  provider reads) lives server-side as ``agent.gates.cards.estimate.CardEstimate`` --
  it carries a live ``PayloadWarningEnvelopePayload``, so it cannot live in the
  contract layer.

The estimate/pin providers are named by DOTTED IMPORT PATH (a string), not a callable,
so the contract stays a pure serializable shape and the engine imports the provider
lazily off the tool's own module (engine knowledge stays in the engine, the
gate_input_review precedent).
"""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import GraceModel

__all__ = ["GateKind", "GateSpec", "LeverSpec"]


#: What KIND of consequence the gate guards. ``"solver"`` - a consequential engine run
#: (Invariant 9); the engine STRIPS a model-supplied ``confirmed`` before gating and the
#: pin provider injects it only on an explicit proceed. ``"fetch"`` - a heavy raster
#: download/merge; ``confirmed`` is NOT in play (fetchers do not read it), only the
#: resolution lever is pinned.
GateKind = Literal["solver", "fetch"]


class LeverSpec(GraceModel):
    """One user-overridable lever a confirm card offers (declared, not hand-wired).

    The card renderer (the existing ``GranularitySuggestion`` / ``TimeScaleSuggestion``
    blocks the plugin already draws) and the decision tail read this declaration
    uniformly. The ENGINE-specific pinning arithmetic (a real-cap re-probe, a floor
    clamp, a seed-pair decouple) stays in the tool's pin provider; this is only the
    machine-readable DECLARATION of the lever so membership/rendering/enforcement are
    uniform.

    Fields:

    - ``name`` - human label for the lever (e.g. ``"grid resolution"``, ``"animation
      cadence"``). Card copy only.
    - ``param`` - the approved-params key the pin writes (e.g. ``"grid_resolution_m"``,
      ``"output_interval_min"``, ``"target_resolution_m"``, ``"resolution_m"``,
      ``"mesh_resolution_m"``). The generic tail routes a ``narrow_scope`` override's
      ``revised_args[param]`` through the engine's pin provider under this key.
    - ``unit`` - value unit (``"m"`` default; ``"min"`` for a cadence lever, ``"hr"`` for
      a window lever).
    - ``rungs`` - a DISCRETE suggested-value ladder (mutually exclusive with a
      ``range_min``/``range_max`` window); the chips the card offers. ``None`` when the
      lever is a continuous free-edit window.
    - ``range_min`` / ``range_max`` - a continuous override window (FINEST / COARSEST
      inclusive); the pin provider clamps a chosen value into it. ``None`` on either side
      declares that side unbounded.
    - ``pin_on_proceed`` - when True (default) a plain ``proceed`` pins the SUGGESTED
      value the card showed into the approved params (so the run matches the card the
      user saw); when False the lever only takes effect on an explicit ``narrow_scope``
      override.
    """

    name: str = Field(min_length=1)
    param: str = Field(min_length=1)
    unit: str = "m"
    rungs: tuple[float, ...] | None = None
    range_min: float | None = None
    range_max: float | None = None
    pin_on_proceed: bool = True

    @model_validator(mode="after")
    def _validate_lever(self) -> LeverSpec:
        """A lever declares a discrete ladder XOR a continuous window (or neither).

        ``rungs`` and a ``range_min``/``range_max`` window are mutually exclusive - pick
        one form. Neither is a legitimate declaration (a free-edit lever with no declared
        bounds, e.g. a cadence in minutes floored by the deck itself). When both range
        bounds are set, ``min <= max``.
        """
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
    """A tool's DECLARED confirm gate (the gate-collapse carrier, ADR 0273).

    Presence of this on a tool's :class:`AtomicToolMetadata` is the ONE membership
    signal the server gate engine reads - the ``SOLVER_CONFIRM_TOOLS`` /
    ``FETCH_CONFIRM_TOOLS`` name sets die. It names the pure functions that build the
    card and pin the decision (by dotted import path, so the contract carries no server
    import) and declares the levers the card offers.

    Fields:

    - ``kind`` - ``"solver"`` or ``"fetch"`` (see :data:`GateKind`).
    - ``estimate_provider`` - dotted import path to a PURE builder
      ``(params: dict) -> CardEstimate`` (may be a coroutine; async providers are awaited)
      exported from the tool's OWN module. It builds the confirm card (envelope) and the
      opaque tail state the pin provider reads. Returning a CardEstimate whose envelope is
      ``None`` signals "no gate needed, dispatch as-is" (the fetch_landcover
      no-coarsening skip).
    - ``pin_provider`` - dotted import path to a PURE decision-tail function
      ``(decision, revised_args, params, tail_state) -> dict`` (may be a coroutine)
      returning the approved-params DELTA to merge on a ``proceed`` / ``narrow_scope``.
      ``None`` for a plain proceed/cancel gate with no levers (the generic tail then just
      injects ``confirmed=True`` for a solver, nothing for a fetch).
    - ``levers`` - the declared :class:`LeverSpec` list (empty for a plain proceed/cancel
      gate). Card rendering + the generic ``narrow_scope`` routing read these.
    - ``title`` / ``rationale`` - optional card-copy fields naming the gate + why it
      exists (the prose the provider bakes into the envelope recommendation still owns the
      user-facing caption; these are metadata for the docstring / audit surface).
    """

    kind: GateKind
    estimate_provider: str = Field(min_length=1)
    pin_provider: str | None = None
    levers: tuple[LeverSpec, ...] = ()
    title: str = ""
    rationale: str = ""

    @model_validator(mode="after")
    def _validate_gate(self) -> GateSpec:
        """A gate WITH levers must name a pin provider to enforce them.

        A lever the card offers but no ``pin_provider`` to honour a ``narrow_scope``
        override would be a dead knob; forbid the inconsistency at declaration time. A
        lever-less gate (plain proceed/cancel) needs no pin provider (the generic tail
        injects ``confirmed`` for a solver).
        """
        if self.levers and self.pin_provider is None:
            raise ValueError(
                "GateSpec: a gate declaring levers must name a pin_provider to honour a "
                "narrow_scope override (a lever with no pin is a dead knob)."
            )
        return self
