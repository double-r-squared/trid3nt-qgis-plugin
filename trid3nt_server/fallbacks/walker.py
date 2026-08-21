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
    "LADDER_ERROR_CODE",
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


#: The error_code of a wrap that is NOT a coverage refusal: an infra fault
#: raised somewhere under a rung (cache, transport, param validation). It must
#: never wear the capability's coverage-gap code -- a composer excepting on that
#: code would read a transient fault as a terminal data gap.
LADDER_ERROR_CODE = "FALLBACK_LADDER_ERROR"


class LadderRefused(Exception):
    """The terminal REFUSE rung: no permitted rung could serve the request."""

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        activation: "Activation",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable
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
    #: What an unverified serve says instead of a number. Rides the narration so
    #: an exempted request is still VISIBLE to the model, never silent.
    unverified_note: str | None = None

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
        return render_fallback_line(self.to_contract()) or self.unverified_note

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


def _measured_coverage(result: Any) -> dict[str, float] | None:
    """The MEASURED per-rung share a result reports, or None.

    The seam a capability uses to hand the walker what actually painted:
    ``rung_coverage`` maps rung name -> fraction of the request that rung's
    source painted. Without it the walker only knows its own promise arithmetic
    (1.0 minus what an earlier rung claimed), which is not evidence.
    """
    raw = getattr(result, "rung_coverage", None)
    if not isinstance(raw, Mapping) or not raw:
        return None
    out: dict[str, float] = {}
    for name, value in raw.items():
        try:
            out[str(name)] = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return None
    return out or None


def _reconcile_to_paint(
    ladder: Ladder, activation: Activation, measured: Mapping[str, float]
) -> None:
    """Replace promise-derived shares with the measured paint, in place.

    A row that survives a reconcile reports what the source PAINTED. Rungs the
    capability measured but the walker never recorded (the request laid the
    rung's source down without descending to it) are appended, so the raster's
    real composition is on the contract.
    """
    seen: set[str] = set()
    rebuilt: list[RungRecord] = []
    for record in activation.records:
        share = measured.get(record.rung)
        if share is None or record.declined:
            rebuilt.append(record)
            continue
        seen.add(record.rung)
        rebuilt.append(
            RungRecord(record.rung, record.consequence, share, record.note)
        )
    for rung in (ladder.primary_rung, *ladder.alternatives):
        share = measured.get(rung.name)
        if rung.name in seen or not share:
            continue
        rebuilt.append(RungRecord(rung.name, rung.consequence, share, rung.describes))
    activation.records = rebuilt


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
    ``error_code`` / ``retryable`` into its own. A decline gets the ladder's own
    refusal ONLY when a gap was actually recorded; an untyped failure gets
    ``LADDER_ERROR_CODE``, never the capability's coverage code.

    Coverage rows report MEASURED paint whenever the result reports it
    (``rung_coverage``); a request that turned the capability's own coverage
    check off through ``coverage_exempt_params`` stamps NO number and carries a
    visible unverified note instead.
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
    # ONLY the CALLER's own params exempt: a param a RUNG injects is the ladder
    # exercising its own declared alternative, and that attempt is accounted for
    # like any other (from measured paint, below).
    exempting = [p for p in ladder.coverage_exempt_params if base.get(p)]
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
        measured = None if exempting else _measured_coverage(result)
        if measured is not None:
            _reconcile_to_paint(ladder, activation, measured)
        if exempting:
            # The request itself turned the coverage check off, so no share was
            # measured -- for ANY serving rung, not just the primary. A number
            # here would be a claim nobody stood behind; the note is what the
            # model sees instead.
            activation.coverage_unverified = True
            activation.unverified_note = (
                f"{ladder.capability}: served with {', '.join(exempting)} set, which "
                "exempts the capability's coverage check -- the per-rung share of "
                "this result is UNMEASURED (the result's own labeled warning names "
                "what went into it)."
            )
            logger.warning("fallback ladder %s: %s", ladder.capability,
                           activation.unverified_note)
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
    if declined and gap_note is not None:
        # A DECLINE only explains the refusal when there was a GAP to fill. Re-
        # raising the gap error verbatim here would instruct the user to permit
        # the very rung they just declined.
        raise LadderRefused(
            f"{ladder.refuse_error_code}: declined at the fallback gate. "
            f"{gap_note}. The {', '.join(declined)} rung(s) of the "
            f"{ladder.capability} fallback ladder were DECLINED, so nothing "
            "filled the gap and the request refuses rather than degrading. "
            f"Rungs tried: {tried}.",
            error_code=ladder.refuse_error_code,
            activation=activation,
        ) from last_exc
    if declined:
        # No gap was ever recorded: the primary failed for its OWN reason (an
        # unreachable upstream, a bad input) and the gate question was moot. The
        # primary's typed error -- code and retryability intact -- is the truth
        # about this request; the decline rides the activation, not the code.
        logger.info(
            "fallback ladder %s: %s declined, but the primary recorded no gap -- "
            "surfacing the primary's own error", ladder.capability,
            ", ".join(declined),
        )

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
    surfaced = primary_exc if primary_exc is not None else last_exc
    setattr(surfaced, "fallback_activation", activation)
    if isinstance(surfaced, LadderGap) and getattr(surfaced, "error_code", None) is None:
        # A gap the capability raised WITHOUT its own typed code is still a
        # coverage refusal -- it just has no vocabulary of its own, so it wears
        # the ladder's terminal code rather than escaping untyped.
        raise LadderRefused(
            f"{ladder.refuse_error_code}: {surfaced.gap_note}, and no permitted "
            f"rung of the {ladder.capability} fallback ladder could fill it. "
            f"Rungs tried: {tried}.",
            error_code=ladder.refuse_error_code,
            activation=activation,
        ) from surfaced
    if getattr(surfaced, "error_code", None) is None and not isinstance(
        surfaced, LadderRefused
    ):
        # An UNTYPED failure never escapes the walker bare: callers dispatch on
        # error_code, and a raw exception reads to them as an internal fault.
        # It is NOT dressed as a coverage refusal either -- most untyped
        # failures under a rung are infra (cache, transport, validation), and
        # the composer that excepts on the capability's gap code would read a
        # transient fault as a terminal data gap. The original retryability
        # rides through so a retry loop still works.
        raise LadderRefused(
            f"{LADDER_ERROR_CODE}: the {ladder.capability} {'primary' if surfaced is primary_exc else 'fallback'} "
            f"rung failed with an untyped {type(surfaced).__name__}: {surfaced}. "
            f"This is NOT a {ladder.refuse_error_code} -- nothing measured a "
            f"coverage gap. Rungs tried: {tried}.",
            error_code=LADDER_ERROR_CODE,
            activation=activation,
            retryable=bool(getattr(surfaced, "retryable", False)),
        ) from surfaced
    if surfaced is not last_exc:
        raise surfaced from last_exc  # type: ignore[misc]
    raise surfaced  # type: ignore[misc]
