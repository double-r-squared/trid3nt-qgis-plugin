"""Two-mode INPUT_REQUIRED review gate -- the shared helper templates call.

``auto`` proceeds with every non-user input labeled; ``user_gated`` presents them for
approval, bounded to ``max_rounds`` rounds then an honest cancel.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.payload_warning import ParamSheet, PayloadWarningEnvelopePayload

logger = logging.getLogger("trid3nt_server.gates.input_review")

__all__ = [
    "InputGateMode",
    "ReviewOutcome",
    "resolve_input_gate_mode",
    "render_input_review_lines",
    "physics_refusal_reason",
    "gate_input_review",
]

#: Run-mode lever (shared vocabulary with the mesh preview gate). ``auto``
#: proceeds immediately with labeled inputs; ``user_gated`` pauses for review.
InputGateMode = Literal["auto", "user_gated"]

#: Session-level default when a run does not pass an explicit ``input_mode``.
#: Env override so a whole session can opt into review-before-run; unset is
#: ``auto``, so runs are not blocked by default.
_INPUT_GATE_MODE_ENV = "TRID3NT_INPUT_GATE_MODE"

#: Max review rounds before an honest cancel. One round == one presentation; a
#: ``provide values`` reply consumes a round and re-presents, so the user gets
#: up to this many looks.
_DEFAULT_MAX_ROUNDS = 3

#: Gate wait cap (seconds); running out is a typed cancel, never a silent run.
_DEFAULT_TTL_SECONDS = 300


def resolve_input_gate_mode(mode: str | None) -> InputGateMode:
    """Resolve the effective run mode: explicit param wins, else session default.

    Anything unrecognized falls to the env default, itself ``auto``."""
    if mode is not None:
        m = str(mode).strip().lower()
        if m in ("auto", "user_gated"):
            return m  # type: ignore[return-value]
    env = (os.environ.get(_INPUT_GATE_MODE_ENV) or "auto").strip().lower()
    return "user_gated" if env == "user_gated" else "auto"


def _entry_field(e: Any, name: str) -> Any:
    return e.get(name) if isinstance(e, dict) else getattr(e, name, None)


def render_input_review_lines(entries: Any) -> list[str]:
    """One compact line per resolved input: ``param = value [basis, source]``.

    ``basis`` is spelled human-readably; ``source`` names the fetcher or dataset
    only where there is one."""
    basis_label = {
        "fetched": "site-derived",
        "derived": "derived",
        "user": "user-supplied",
        "prompt_interpreted": "from prompt",
        "default_demo": "demo default",
    }
    lines: list[str] = []
    for e in entries or []:
        param = _entry_field(e, "param")
        value = _entry_field(e, "value")
        units = _entry_field(e, "units")
        basis = _entry_field(e, "basis")
        source = _entry_field(e, "real_source_if_any")
        val_txt = "?" if value is None else f"{value}"
        if units:
            val_txt = f"{val_txt} {units}"
        tag = basis_label.get(str(basis), str(basis))
        src = f", {source}" if source else ""
        lines.append(f"{param} = {val_txt} [{tag}{src}]")
    return lines


def _physics_demo_entries(entries: Any) -> list[Any]:
    """The entries that must REFUSE in auto: ``consequence="physics"`` demo defaults.

    Scenario, numerical and aoi demo defaults are the user's question or a solver
    knob, not a world invention, and are excluded."""
    out = []
    for e in entries or []:
        if (_entry_field(e, "basis") == "default_demo"
                and _entry_field(e, "consequence") == "physics"):
            out.append(e)
    return out


def physics_refusal_reason(tool_name: str, entries: Any, *,
                           no_session: bool = False,
                           no_review_surface: bool = False) -> str | None:
    """The typed ``*_PHYSICS_INPUT_REQUIRED`` refusal text, or None if none refuse.

    ``no_session`` (nothing live to present on) and ``no_review_surface`` (a
    workflow with no card and no self-reviewing step) each pick their remedy."""
    refusing = _physics_demo_entries(entries)
    if not refusing:
        return None
    needs = []
    for e in refusing:
        param = _entry_field(e, "param")
        note = _entry_field(e, "note")
        need = f" ({note})" if note else ""
        needs.append(f"{param}{need}")
    if no_review_surface:
        where = "cannot run: nothing in this workflow reviews these values"
        remedy = ("Supply real values, ensure a fetcher can resolve them, or give "
                  "the workflow a review surface (a form gate, or a step that "
                  "reviews its own inputs).")
    elif no_session:
        where = "cannot run without a live session to approve these on"
        remedy = "Supply real values, or ensure a fetcher can resolve them."
    else:
        where = "cannot run in auto mode"
        remedy = ("Supply real values, ensure a fetcher can resolve them, or re-run "
                  "in user_gated mode to approve the demo defaults explicitly.")
    return (
        f"PHYSICS_INPUT_REQUIRED: {tool_name} {where} -- these "
        "physics-consequential inputs have no real data source and fell back to "
        "invented demo defaults, which would silently ruin the simulation (law 9): "
        + "; ".join(needs) + ". " + remedy
    )


def _build_review_envelope(
    *,
    tool_name: str,
    entries: list[SyntheticInput],
    round_idx: int,
    max_rounds: int,
    ttl_seconds: int,
    param_sheet: "ParamSheet | None" = None,
) -> PayloadWarningEnvelopePayload:
    """Build the input-review ``tool-payload-warning``.

    ``param_sheet`` is the resolved sheet as an EDIT SURFACE; ``narrow_scope`` is
    the "provide values" action, and its reply carries ``revised_args``."""
    # The provenance is carried twice on purpose: rendered into
    # ``recommendation`` so a client with no rich renderer still surfaces the
    # table, and structured on ``synthetic_inputs`` for the narration seam.
    lines = render_input_review_lines(entries)
    header = (
        f"Review the resolved inputs for {tool_name} before it runs "
        f"(round {round_idx}/{max_rounds}):"
    )
    body = "\n".join(f"- {ln}" for ln in lines)
    footer = (
        "Reply 'proceed' to run as-is, 'provide values' to adjust an input, or "
        "'cancel'."
    )
    recommendation = f"{header}\n{body}\n{footer}"
    # Trim the body (never the header/footer) if the table overruns the cap.
    if len(recommendation) > 512:
        budget = 512 - len(header) - len(footer) - 2
        recommendation = f"{header}\n{body[: max(0, budget)]}\n{footer}"[:512]
    return PayloadWarningEnvelopePayload(
        warning_id=new_ulid(),
        tool_name=tool_name,
        tool_args={},
        estimated_mb=0.0,
        threshold_mb=0.0,
        recommendation=recommendation,
        options=["proceed", "narrow_scope", "cancel"],
        ttl_seconds=int(ttl_seconds),
        synthetic_inputs=list(entries),
        param_sheet=param_sheet,
    )


def _apply_revision(
    entries: list[SyntheticInput],
    params: dict[str, Any],
    revised_args: dict[str, Any] | None,
) -> tuple[list[SyntheticInput], dict[str, Any]]:
    """Merge a ``provide values`` revision into the params + provenance entries.

    A revised value is re-stamped ``basis="user"`` with its units preserved; an
    unknown key becomes a new user-basis entry."""
    revised = revised_args or {}
    if not revised:
        return entries, params
    merged_params = dict(params)
    by_param = {str(_entry_field(e, "param")): e for e in entries}
    merged_entries = list(entries)
    for key, val in revised.items():
        merged_params[key] = val
        existing = by_param.get(str(key))
        if existing is not None:
            units = _entry_field(existing, "units")
            new_entry = SyntheticInput(
                param=str(key), value=val, units=units, basis="user",
                note="user-revised at review",
            )
            merged_entries = [
                new_entry if str(_entry_field(e, "param")) == str(key) else e
                for e in merged_entries
            ]
        else:
            merged_entries.append(
                SyntheticInput(param=str(key), value=val, basis="user",
                               note="user-supplied at review")
            )
    return merged_entries, merged_params


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
    mode: InputGateMode = "auto"
    rounds_used: int = 0


async def gate_input_review(
    *,
    tool_name: str,
    mode: str | None,
    entries: list[SyntheticInput],
    params: dict[str, Any],
    reresolve: Callable[[dict[str, Any]], Awaitable[
        tuple[list[SyntheticInput], dict[str, Any]]]] | None = None,
    max_rounds: int = _DEFAULT_MAX_ROUNDS,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    param_sheet: "ParamSheet | None" = None,
) -> ReviewOutcome:
    """Present resolved inputs for review before solver dispatch.

    In ``auto``, and with no live session, the inputs proceed unchanged UNLESS a
    physics-consequential demo default is present, which REFUSES."""
    resolved_mode = resolve_input_gate_mode(mode)
    physics_refusal = physics_refusal_reason(tool_name, entries)
    if resolved_mode == "auto":
        if physics_refusal is not None:
            logger.info(
                "input-review gate REFUSE (auto) tool=%s -- physics demo default(s) "
                "with no real source", tool_name,
            )
            return ReviewOutcome(
                proceed=False, entries=list(entries), params=dict(params),
                cancelled=True, cancel_reason=physics_refusal, mode="auto",
            )
        return ReviewOutcome(proceed=True, entries=list(entries),
                             params=dict(params), mode="auto")

    # user_gated: needs a live session to pause on. current_emitter() is bound at
    # turn entry; a headless direct-call has none -> fail OPEN (labeled, no block).
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.gates.pending import (
        _register_pending_confirmation,
        _pop_pending_confirmation,
    )

    emitter = current_emitter()
    if emitter is None:
        # No live session to present the demo defaults for approval. A physics
        # demo default still REFUSES -- the value would silently ruin the run and
        # there is nobody to approve it; everything else fails open, labeled.
        if physics_refusal is not None:
            logger.info(
                "input-review gate REFUSE (user_gated, no emitter) tool=%s -- "
                "physics demo default(s) with no real source", tool_name,
            )
            return ReviewOutcome(
                proceed=False, entries=list(entries), params=dict(params),
                cancelled=True, mode="user_gated",
                cancel_reason=physics_refusal_reason(tool_name, entries,
                                                     no_session=True),
            )
        logger.info(
            "input-review gate: user_gated requested for %s but no live session "
            "(direct-call/offline) -- proceeding with labeled inputs (fail-open)",
            tool_name,
        )
        return ReviewOutcome(proceed=True, entries=list(entries),
                             params=dict(params), mode="user_gated")

    cur_entries = list(entries)
    cur_params = dict(params)
    for round_idx in range(1, max_rounds + 1):
        envelope = _build_review_envelope(
            tool_name=tool_name, entries=cur_entries, round_idx=round_idx,
            max_rounds=max_rounds, ttl_seconds=ttl_seconds,
            param_sheet=param_sheet,
        )
        warning_id = envelope.warning_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        _register_pending_confirmation(emitter.session_id, warning_id, fut)
        await emitter.send_envelope("tool-payload-warning", envelope)
        logger.info(
            "input-review gate emitted session=%s tool=%s warning_id=%s "
            "round=%d/%d entries=%d",
            emitter.session_id, tool_name, warning_id, round_idx, max_rounds,
            len(cur_entries),
        )
        try:
            decision = await asyncio.wait_for(fut, timeout=float(ttl_seconds))
        except asyncio.TimeoutError:
            logger.warning(
                "input-review gate timeout session=%s tool=%s warning_id=%s",
                emitter.session_id, tool_name, warning_id,
            )
            return ReviewOutcome(
                proceed=False, entries=cur_entries, params=cur_params,
                cancelled=True, cancel_reason="review timed out; the solver did "
                "not run", mode="user_gated", rounds_used=round_idx,
            )
        finally:
            _pop_pending_confirmation(warning_id)

        if decision.decision == "proceed":
            logger.info(
                "input-review gate proceed session=%s tool=%s round=%d",
                emitter.session_id, tool_name, round_idx,
            )
            return ReviewOutcome(proceed=True, entries=cur_entries,
                                 params=cur_params, mode="user_gated",
                                 rounds_used=round_idx)
        if decision.decision == "cancel":
            return ReviewOutcome(
                proceed=False, entries=cur_entries, params=cur_params,
                cancelled=True, cancel_reason="declined by user at input review",
                mode="user_gated", rounds_used=round_idx,
            )
        # narrow_scope == "provide values": merge the revision, optionally
        # re-resolve, then re-present (unless this was the last round).
        cur_entries, cur_params = _apply_revision(
            cur_entries, cur_params, decision.revised_args
        )
        if param_sheet is not None:
            # The form card showed the WHOLE sheet, so submitting it IS the
            # approval and the gate proceeds instead of re-presenting. Without a
            # sheet the text card keeps its adjust-and-re-present rounds, where a
            # revision the user could not see in full deserves another look.
            logger.info(
                "input-review gate submit-with-edits session=%s tool=%s revised=%s",
                emitter.session_id, tool_name,
                sorted(decision.revised_args or {}),
            )
            return ReviewOutcome(proceed=True, entries=cur_entries,
                                 params=cur_params, mode="user_gated",
                                 rounds_used=round_idx)
        # Without a reresolve callback a revision only re-stamps the affected
        # entries to user basis; with one, revised params re-run their fetchers
        # (a revised dam name reaching a new NID lookup, say).
        if reresolve is not None:
            try:
                cur_entries, cur_params = await reresolve(cur_params)
            except Exception:  # noqa: BLE001 -- a re-resolve fault must not orphan
                logger.warning(
                    "input-review gate reresolve failed session=%s tool=%s "
                    "-- keeping the merged revision",
                    emitter.session_id, tool_name, exc_info=True,
                )
        if round_idx == max_rounds:
            return ReviewOutcome(
                proceed=False, entries=cur_entries, params=cur_params,
                cancelled=True, cancel_reason=(
                    f"input review not approved after {max_rounds} rounds; the "
                    "solver did not run"
                ), mode="user_gated", rounds_used=round_idx,
            )
    # Unreachable (loop always returns), but keep a definite outcome.
    return ReviewOutcome(proceed=False, entries=cur_entries, params=cur_params,
                         cancelled=True, cancel_reason="input review not approved",
                         mode="user_gated", rounds_used=max_rounds)
