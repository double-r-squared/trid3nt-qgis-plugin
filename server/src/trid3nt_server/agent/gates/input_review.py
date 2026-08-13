"""Two-mode INPUT_REQUIRED review gate -- the shared helper templates call (ADR 0107).

NATE's flagship review-before-run feature has TWO run modes:

  * ``auto`` (session default): the run proceeds immediately. Every non-user
    input is still LOUDLY LABELED via the 0106 ``synthetic_inputs`` machinery
    (already carried on the result envelope) -- this helper is a no-op pass
    through, it does not pause.
  * ``user_gated``: AFTER the template has RESOLVED its inputs (fetched values,
    prompt-interpreted values, demo defaults) and BEFORE the solver is
    dispatched, the resolved input set is presented for review. The user
    approves (``proceed``) or adjusts a value (``provide values`` == the
    ``narrow_scope`` action carrying ``revised_args``); on approval the run
    stamps EXACTLY the reviewed entries into its result so what-was-approved ==
    what-ran. A ``provide values`` reply re-resolves + re-presents, bounded to
    ``max_rounds`` rounds then an honest cancel.

It rides the EXISTING #154 pause/resume spine -- no new WS event, no new
confirmation envelope: the review is a ``tool-payload-warning`` carrying the
resolved provenance (rendered into ``recommendation`` so the plugin's existing
card shows it with NO new UI, plus the structured ``synthetic_inputs`` field for
narration), and ``server``'s ``_PENDING_CONFIRMATIONS`` block-and-wait +
``tool-payload-confirmation`` resume path handle it unchanged. The mode lever is
shared with the mesh preview gate (ADR 0099): ``user_gated`` also turns the mesh
preview gate ON for regular grids.

An in-tool gate cannot import ``server`` at module load (circular), so the helper
reaches the spine through ``current_emitter()`` (the sink + session id) and the
leaf ``agent.gates.pending`` registry. With NO live emitter (a direct-call /
offline run with no session) the gate FAILS OPEN -- it proceeds with the resolved
inputs labeled, never blocking a headless run.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from trid3nt_contracts import new_ulid
from trid3nt_contracts.common import SyntheticInput
from trid3nt_contracts.payload_warning import PayloadWarningEnvelopePayload

logger = logging.getLogger("trid3nt_server.agent.gates.input_review")

__all__ = [
    "InputGateMode",
    "ReviewOutcome",
    "resolve_input_gate_mode",
    "render_input_review_lines",
    "gate_input_review",
]

#: Run-mode lever (shared vocabulary with the mesh preview gate). ``auto``
#: proceeds immediately with labeled inputs; ``user_gated`` pauses for review.
InputGateMode = Literal["auto", "user_gated"]

#: Session-level default when a run does not pass an explicit ``input_mode``.
#: Env override so a whole session can opt into review-before-run; unset ==
#: ``auto`` (the shipped behavior -- runs are not blocked by default).
_INPUT_GATE_MODE_ENV = "TRID3NT_INPUT_GATE_MODE"

#: Max review rounds before an honest cancel (NATE: "bounded: 3 rounds then
#: honest cancel"). One round == one presentation; a ``provide values`` reply
#: consumes a round and re-presents, so the user gets up to this many looks.
_DEFAULT_MAX_ROUNDS = 3

#: Gate wait cap (seconds) mirroring the solver-confirm gate TTL.
_DEFAULT_TTL_SECONDS = 300


def resolve_input_gate_mode(mode: str | None) -> InputGateMode:
    """Resolve the effective run mode: explicit param wins, else session default.

    A per-run ``input_mode`` (``"auto"`` / ``"user_gated"``) overrides; anything
    else (None, unrecognized) falls to the ``TRID3NT_INPUT_GATE_MODE`` env
    default, itself defaulting to ``auto`` so runs are never blocked unless the
    user opted in.
    """
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

    Concise-chat norm: a table-like block, one input per line. ``basis`` is
    spelled human-readably (``fetched`` -> ``site-derived``, ``default_demo`` ->
    ``demo default``); the ``source`` clause names the fetcher/dataset when the
    value is fetched/derived.
    """
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


def _build_review_envelope(
    *,
    tool_name: str,
    entries: list[SyntheticInput],
    round_idx: int,
    max_rounds: int,
    ttl_seconds: int,
) -> PayloadWarningEnvelopePayload:
    """Build the input-review ``tool-payload-warning`` (rides the #154 spine).

    The provenance is rendered into ``recommendation`` (so the plugin's existing
    card surfaces the table with NO new UI) AND carried structured on
    ``synthetic_inputs`` (for the narration seam + future rich rendering). The
    ``narrow_scope`` option is the "provide values" action -- a reply with
    ``revised_args`` re-resolves the run.
    """
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
    )


def _apply_revision(
    entries: list[SyntheticInput],
    params: dict[str, Any],
    revised_args: dict[str, Any] | None,
) -> tuple[list[SyntheticInput], dict[str, Any]]:
    """Merge a ``provide values`` revision into the params + provenance entries.

    For each revised key: update ``params[key]`` and, if a provenance entry names
    it, rebuild that entry with ``basis="user"`` (a user-revised value is
    user-provenance) preserving its units; an unknown key becomes a new
    user-basis entry. Returns the merged ``(entries, params)``.
    """
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

    ``proceed`` True -> run with ``params`` and stamp ``entries`` into the
    result. ``cancelled`` True -> the user declined (or the rounds/TTL ran out);
    the template returns a typed cancel error and does NOT solve.
    """

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
) -> ReviewOutcome:
    """Present resolved inputs for review before solver dispatch (ADR 0107).

    ``mode``: the per-run lever (``auto`` / ``user_gated`` / None -> session
    default). ``entries``: the resolved ``SyntheticInput`` provenance. ``params``:
    the run params the review may revise. ``reresolve``: optional callback that,
    given revised params, returns freshly resolved ``(entries, params)`` -- used
    when a ``provide values`` reply should re-run fetchers (e.g. a revised dam
    name -> a new NID lookup). When absent, a revision updates the affected
    entries to user-basis without re-fetching.

    Returns a :class:`ReviewOutcome`. In ``auto`` mode (or with no live session)
    it returns ``proceed=True`` immediately with the inputs unchanged.
    """
    resolved_mode = resolve_input_gate_mode(mode)
    if resolved_mode == "auto":
        return ReviewOutcome(proceed=True, entries=list(entries),
                             params=dict(params), mode="auto")

    # user_gated: needs a live session to pause on. current_emitter() is bound at
    # turn entry; a headless direct-call has none -> fail OPEN (labeled, no block).
    from trid3nt_server.emission.pipeline_emitter import current_emitter
    from trid3nt_server.agent.gates.pending import (
        _register_pending_confirmation,
        _pop_pending_confirmation,
    )

    emitter = current_emitter()
    if emitter is None:
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
