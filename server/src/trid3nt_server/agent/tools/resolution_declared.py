"""Declared-resolution enforcement (ADR 0225, NATE's clamp ruling).

The schema (:class:`trid3nt_contracts.tool_registry.ResolutionSpec`) is the
machine-readable DECLARATION of the resolutions a tool can actually run. This module
is the BEHAVIOUR that rides it: a request outside the declared range is QUOTED BACK
(the range + native hint + optional measured cost) and raised as a typed error --
never silently snapped to an undeclared value. It replaces the labeled-snap that ADR
0223 added at the clamp sites with the full ruling ("out-of-range asks get the
declared range quoted back, never a silent coercion").

Two-layer truth (established architecture): a DATA-native bound lives with the fetcher
(``constraint_source="data"``), a SOLVER bound lives with the template
(``constraint_source="solver"``); :meth:`ResolutionSpec.quote_back` composes both into
one card. Tools call :func:`enforce_resolution` at the top of their resolution
handling; a fetcher/template that autoscales WITHIN the declared range keeps its
labeled-derived note (coarsening for tractability is a labeled degrade, not a silent
snap -- only the out-of-DECLARED-range case is the hard error).
"""
from __future__ import annotations

from trid3nt_contracts.errors import ToolInputError
from trid3nt_contracts.tool_registry import ResolutionSpec

__all__ = [
    "ResolutionOutOfRangeError",
    "enforce_resolution",
    "resolution_review_note",
]


class ResolutionOutOfRangeError(ValueError):
    """A resolution request outside a tool's DECLARED range (ADR 0225).

    Carries the :class:`ResolutionSpec` and the requested value, and a wire-typed
    :class:`ToolInputError` (``code="INVALID_ARG"``) whose message is the quote-back
    card. A template may let this propagate (the dispatch boundary serializes the
    ToolInputError) or catch it and re-raise its own typed error type carrying
    ``str(exc)`` -- the message is already the full quote-back so no rewording is
    needed. ``.error_code = "INVALID_ARG"`` lets the hecras/schism-style emitters that
    branch on ``.error_code`` re-wrap uniformly.
    """

    def __init__(
        self,
        spec: ResolutionSpec,
        requested: float,
        *,
        measured: str | None = None,
    ) -> None:
        self.spec = spec
        self.requested = requested
        self.tool_input_error = ToolInputError(
            code="INVALID_ARG",
            message=spec.quote_back(requested, measured=measured),
        )
        self.error_code = "INVALID_ARG"
        super().__init__(self.tool_input_error.message)


def enforce_resolution(
    spec: ResolutionSpec,
    requested: float | None,
    *,
    measured: str | None = None,
) -> None:
    """Raise :class:`ResolutionOutOfRangeError` when ``requested`` is out of range.

    ``requested=None`` (the native / autoscaled default) is always in-range by
    construction -- there is nothing to enforce. A value inside the declared window /
    option set returns silently. ``measured`` is an optional one-line cost string
    (e.g. ``"~180 MB measured"``) folded into the quote-back card so the user sees the
    range AND the price of their ask in one place (the two-layer + payload composition).
    """
    if requested is None:
        return
    if not spec.contains(float(requested)):
        raise ResolutionOutOfRangeError(spec, float(requested), measured=measured)


def resolution_review_note(
    spec: ResolutionSpec, requested: float | None, effective: float | None
) -> str | None:
    """A labeled note for the ``resolution_m`` review entry when a value was DERIVED.

    Returns ``None`` when the effective value equals the request (basis ``user``, no
    note needed). When they differ -- an in-range request autoscaled for tractability,
    or a native default resolved from the AOI -- returns a one-line note that names the
    declared range so the derivation is visible (never silent). Out-of-range is handled
    upstream by :func:`enforce_resolution`; this is only for the WITHIN-range
    derivations that remain legitimate labeled degrades.
    """
    if requested is None:
        if effective is None:
            return None
        return (
            f"native default resolved to {effective:g} {spec.unit} for this AOI "
            f"(declared range {spec.range_phrase()})"
        )
    if effective is None or abs(float(effective) - float(requested)) <= 1e-9:
        return None
    return (
        f"autoscaled from {float(requested):g} to {float(effective):g} {spec.unit} "
        f"for this AOI within the declared {spec.range_phrase()}"
    )
