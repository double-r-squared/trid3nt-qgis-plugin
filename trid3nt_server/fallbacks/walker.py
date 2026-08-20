"""The ONE walker that executes every declared fallback ladder.

Rungs are tried in order; each attempt either serves the request, serves PART of
it (:class:`LadderGap`), or fails outright. The walker records which rung served
and the coverage share it painted, fires the loudness gate before any
degradation, and raises :class:`LadderRefused` when the terminal REFUSE rung is
reached. Guarantees live here so no seam re-implements them.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from trid3nt_contracts.common import FallbackActivation, render_fallback_line

from .ladder import DEGRADATION_CLASSES, Ladder, Rung

logger = logging.getLogger("trid3nt_server.fallbacks.walker")

__all__ = [
    "LadderGap",
    "LadderRefused",
    "RungRecord",
    "Activation",
    "walk_ladder",
]

#: Coverage below this much of the request counts as complete: a rung that
#: painted all but a rounding error of the AOI has no gap to fill.
_COMPLETE_EPS = 1e-6


class LadderGap(Exception):
    """A rung served only PART of the request; the rest needs the next rung.

    Raised by a seam that can measure its own coverage (a mosaic knows the
    fraction of the AOI its tiles cover). ``covered_fraction`` is cumulative over
    the request, not over the rung. A seam raising this must NOT have filled the
    gap with anything -- the whole point is that the walker decides what fills it.
    """

    def __init__(self, message: str, *, covered_fraction: float, gap_note: str) -> None:
        super().__init__(message)
        self.covered_fraction = max(0.0, min(1.0, float(covered_fraction)))
        self.gap_note = gap_note


class LadderRefused(Exception):
    """The terminal REFUSE rung: no permitted rung could serve the request."""

    def __init__(self, message: str, *, error_code: str, activation: "Activation") -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = False
        self.activation = activation


@dataclass(frozen=True)
class RungRecord:
    rung: str
    consequence: str
    coverage: float
    note: str | None = None
    #: The gate said no. Kept on the contract even at coverage 0.0 -- a decline
    #: that left no trace is indistinguishable from a rung nobody offered.
    declined: bool = False


@dataclass
class Activation:
    """What actually served a request, rung by rung."""

    capability: str
    records: list[RungRecord] = field(default_factory=list)
    #: The capability's coverage check was exempted by this request's own params,
    #: so no rung's share was measured. Nothing is stamped: a 1.0 coverage claim
    #: nobody verified is an affirmatively false row.
    coverage_unverified: bool = False

    @property
    def degraded(self) -> bool:
        return any(
            r.consequence in DEGRADATION_CLASSES and r.coverage > 0.0
            for r in self.records
        )

    def to_contract(self) -> list[FallbackActivation]:
        if self.coverage_unverified:
            return []
        return [
            FallbackActivation(
                capability=self.capability,
                rung=r.rung,
                consequence=r.consequence,  # type: ignore[arg-type]
                coverage=r.coverage,
                note=r.note,
            )
            for r in self.records
            if r.consequence != "refuse" and (r.coverage > 0.0 or r.declined)
        ]

    def narration(self) -> str | None:
        return render_fallback_line(self.to_contract())

    def coverage_summary(self) -> str:
        return " / ".join(
            f"{r.coverage * 100:.0f}% {r.rung}"
            for r in self.records
            if r.coverage > 0.0
        )


def _resolve_call(dotted: str) -> Callable[..., Any]:
    module_name, _, attr = dotted.partition(":")
    if not attr:
        raise ValueError(f"rung call {dotted!r} must be 'module:function'")
    return getattr(importlib.import_module(module_name), attr)


def _invoke(
    rung: Rung,
    params: dict[str, Any],
    attempt: Callable[[Rung, dict[str, Any]], Any],
) -> Any:
    if rung.call:
        return _resolve_call(rung.call)(**params)
    if rung.source:
        from trid3nt_server.data import TOOL_REGISTRY

        entry = TOOL_REGISTRY.get(rung.source)
        if entry is None:
            raise LookupError(f"rung {rung.name!r} names unregistered source {rung.source!r}")
        return entry.fn(**params)
    return attempt(rung, params)


def walk_ladder(
    ladder: Ladder,
    *,
    params: Mapping[str, Any],
    attempt: Callable[[Rung, dict[str, Any]], Any],
    allow: Sequence[str] = (),
    gate_mode: str | None = None,
    gate: Callable[..., bool] | None = None,
) -> tuple[Any, Activation]:
    """Walk ``ladder`` for one request; return ``(result, activation)``.

    ``allow`` names the alternative rungs this call site tolerates, in order --
    an empty ``allow`` is the universal default of primary-or-REFUSE. ``attempt``
    invokes a rung that declares no source/call of its own (the primary, and any
    rung expressed as an override of the primary request). ``gate`` is the
    loudness gate; the default one rides the pending-confirmation spine.

    On REFUSE the PRIMARY's typed error is what surfaces (later rung failures
    chain through ``__cause__``), so no rung can launder the primary's
    ``error_code`` / ``retryable`` into its own; a decline and an unmeasurable
    gap each get their own typed refusal.
    """
    if gate is None:
        from trid3nt_server.gates.fallback import confirm_fallback as gate

    base = dict(params)
    plan: list[Rung] = []
    user = ladder.user_rung
    if user is not None and base.get(str(user.supplies_param)) is not None:
        plan.append(user)
    plan.append(ladder.primary_rung)
    for name in allow:
        rung = ladder.alternative(name)
        if rung is None:
            raise ValueError(
                f"ladder {ladder.capability!r} declares no alternative rung "
                f"{name!r}; declared: {[r.name for r in ladder.alternatives]}"
            )
        plan.append(rung)

    activation = Activation(capability=ladder.capability)
    unverified = any(bool(base.get(p)) for p in ladder.coverage_exempt_params)
    covered = 0.0
    last_exc: BaseException | None = None
    primary_exc: BaseException | None = None
    primary_attempted = False
    gap_note: str | None = None

    for rung in plan:
        if rung.consequence in DEGRADATION_CLASSES:
            permitted = gate(
                capability=ladder.capability,
                rung=rung,
                gate_mode=gate_mode,
                covered_fraction=covered,
                gap_note=gap_note,
            )
            if not permitted:
                activation.records.append(
                    RungRecord(rung.name, rung.consequence, 0.0,
                               "declined at the fallback gate", declined=True)
                )
                continue
        if rung is ladder.primary_rung:
            primary_attempted = True
        try:
            result = _invoke(rung, {**base, **dict(rung.params)}, attempt)
        except LadderGap as gap:
            share = max(0.0, gap.covered_fraction - covered)
            activation.records.append(
                RungRecord(rung.name, rung.consequence, share, gap.gap_note)
            )
            covered = max(covered, gap.covered_fraction)
            gap_note = gap.gap_note
            last_exc = gap
            if rung is ladder.primary_rung:
                primary_exc = gap
            if covered >= 1.0 - _COMPLETE_EPS:
                # A gap that closed itself is a seam bug, not a degradation.
                raise
            continue
        except Exception as exc:  # noqa: BLE001 -- a failed rung descends the ladder
            activation.records.append(
                RungRecord(rung.name, rung.consequence, 0.0,
                           f"{type(exc).__name__}: {exc}")
            )
            last_exc = exc
            if rung is ladder.primary_rung:
                primary_exc = exc
            continue
        activation.records.append(
            RungRecord(rung.name, rung.consequence, max(0.0, 1.0 - covered),
                       rung.describes)
        )
        if rung is ladder.primary_rung and unverified:
            activation.coverage_unverified = True
        if activation.degraded:
            logger.warning(
                "fallback ladder %s: %s", ladder.capability, activation.narration()
            )
        return result, activation

    # The terminal REFUSE rung.
    tried = ", ".join(f"{r.rung} ({r.note})" for r in activation.records)
    if not primary_attempted or last_exc is None:
        raise LadderRefused(
            f"{ladder.refuse_error_code}: the {ladder.capability} ladder reached "
            "REFUSE without attempting its primary rung -- a walker invariant "
            f"was violated. Rungs tried: {tried or 'none'}.",
            error_code=ladder.refuse_error_code,
            activation=activation,
        )

    declined = [r.rung for r in activation.records if r.declined]
    if declined:
        # Re-raising the gap error verbatim here would instruct the user to
        # permit the very rung they just declined.
        raise LadderRefused(
            f"{ladder.refuse_error_code}: declined at the fallback gate. "
            + (f"{gap_note}. " if gap_note else "")
            + f"The {', '.join(declined)} rung(s) of the {ladder.capability} "
            "fallback ladder were DECLINED, so nothing filled the gap and the "
            f"request refuses rather than degrading. Rungs tried: {tried}.",
            error_code=ladder.refuse_error_code,
            activation=activation,
        ) from last_exc

    if gap_note is not None and not isinstance(last_exc, LadderGap):
        # A gap WAS recorded and the rung meant to fill it failed for its own
        # unrelated reason, so the refusal has to name both.
        raise LadderRefused(
            f"{ladder.refuse_error_code}: {gap_note}, and no permitted rung of the "
            f"{ladder.capability} fallback ladder could fill it. Rungs tried: {tried}. "
            f"Declared alternatives: {[r.name for r in ladder.alternatives]}.",
            error_code=ladder.refuse_error_code,
            activation=activation,
        ) from last_exc

    # No gap was recorded (or the gap error IS the last failure): the PRIMARY's
    # own typed error is the truth about this request -- a bad input, an
    # unreachable upstream, or the gap error that already names what is missing.
    # It propagates VERBATIM so the raw call surface behaves exactly as it did
    # before a ladder existed, and a later rung's unrelated failure can never
    # substitute its error_code / retryable for the primary's.
    surfaced = last_exc if isinstance(last_exc, LadderGap) else (primary_exc or last_exc)
    setattr(surfaced, "fallback_activation", activation)
    if not isinstance(surfaced, (LadderGap, LadderRefused)) and (
        getattr(surfaced, "error_code", None) is None
    ):
        # An UNTYPED failure never escapes the walker bare: callers dispatch on
        # error_code, and a raw exception reads to them as an internal fault.
        raise LadderRefused(
            f"{ladder.refuse_error_code}: the {ladder.capability} primary rung "
            f"failed with an untyped {type(surfaced).__name__}: {surfaced}. "
            f"Rungs tried: {tried}.",
            error_code=ladder.refuse_error_code,
            activation=activation,
        ) from surfaced
    if surfaced is not last_exc:
        raise surfaced from last_exc  # type: ignore[misc]
    raise surfaced  # type: ignore[misc]
