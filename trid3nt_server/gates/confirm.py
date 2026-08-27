"""The GateSpec confirm engine + the shared gate-wait seam + the user-decision emit-wait gates (payload, code-exec, solver-confirm, credential, region, spatial)."""

from __future__ import annotations

import asyncio
import os
import logging
from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.gate_spec import GateSpec
from trid3nt_contracts.payload_warning import PayloadConfirmationEnvelopePayload, PayloadWarningEnvelopePayload
from trid3nt_contracts.region_choice import RegionChoiceProvidedEnvelopePayload
from trid3nt_contracts.sandbox_contracts import CodeExecRequestPayload
from trid3nt_contracts.secrets import CredentialProvidedEnvelopePayload
from trid3nt_contracts.ws import SpatialInputResponsePayload
from trid3nt_server.credentials.credential_registry import CredentialProvider, generic_provider_for_tool, is_credential_error, is_credential_shaped_error, provider_for_tool
from trid3nt_server.credentials.resolver import resolve_credential
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.gates.cards import _build_credential_request_payload, _build_region_choice_request_payload, _build_spatial_input_request_payload, _gate_memory_key, _get_hard_cap_mb, _get_warning_threshold_mb, _resolve_payload_estimator, _spatial_response_to_result
from trid3nt_server.gates.cards.estimate import call_provider
from trid3nt_server.gates.pending import _pop_pending_confirmation, _register_pending_confirmation
from trid3nt_server.server.config import CODE_EXEC_CONFIRM_TIMEOUT_SECONDS, _code_exec_approval_timeout_s
from trid3nt_server.server.errors import CodeExecApprovalTimeoutError, SpatialInputInvalidResponseError
from trid3nt_server.server.interactions import _pop_pending_credential, _register_pending_credential
from trid3nt_server.server.session.state import SessionState
from trid3nt_server.server.spatial import _pop_pending_region_choice, _pop_pending_spatial_input, _register_pending_region_choice, _register_pending_spatial_input
from trid3nt_server.server.turn.wire import _new_envelope, _send_error, _session_safe_send
from typing import Any
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")

# ---------------------------------------------------------------------------
# Routing-layer typed exceptions.
#
# These live here rather than in a shared exceptions module because they are
# raised exclusively inside ``_invoke_tool_via_emitter`` -- the server-side
# routing layer. They follow the same typed-exception contract as the tool-level
# typed exceptions (``WDPAError``, ``HRSLError``, etc.): ``error_code`` is a
# SCREAMING_SNAKE_CASE string and ``retryable`` is False for both (the LLM
# cannot retry its way out of a missing tool registration; it must revise its
# function-call decision).
#
# ``summarize_tool_result`` in ``adapter.py`` harvests ``error_code`` +
# ``retryable`` from any exception that carries them, so these propagate as
# a full structured error envelope to the model -- the same shape as any
# ``fetch_*`` / ``compute_*`` typed exception.
# ---------------------------------------------------------------------------


# Confirm-gate membership is DERIVED from tool metadata (ADR 0273, the
# gate-collapse): a tool declares a ``GateSpec`` on its ``AtomicToolMetadata``
# (``gate_spec``) and that PRESENCE is the one membership signal -- the
# hand-wired SOLVER_CONFIRM_TOOLS / FETCH_CONFIRM_TOOLS name-set literals are
# gone. ``kind`` on the spec ('solver' | 'fetch') splits the two lanes: a solver
# strips a model-supplied ``confirmed`` before gating and injects it only on an
# explicit proceed; a fetch does not (fetchers ignore it). The named sets survive
# ONLY as registry-derived views (see ``__getattr__`` below) for callers that
# still read ``server.SOLVER_CONFIRM_TOOLS`` -- the source of truth is the specs.


def _gate_spec_for(tool_name: str) -> "GateSpec | None":
    """The declared :class:`GateSpec` for ``tool_name``, or ``None`` if un-gated.

    The ONE membership check the dispatch site + the gate engine read: a tool is
    confirm-gated iff its registered metadata carries a ``gate_spec``. Replaces
    the ``tool_name in SOLVER_CONFIRM_TOOLS | FETCH_CONFIRM_TOOLS`` literal
    membership test with a metadata lookup.
    """
    entry = TOOL_REGISTRY.get(tool_name)
    if entry is None:
        return None
    return entry.metadata.gate_spec

def _confirm_tools_by_kind(kind: str) -> "frozenset[str]":
    """Registry-derived confirm-gate membership for one ``kind`` ('solver'|'fetch').

    Backs the derived ``SOLVER_CONFIRM_TOOLS`` / ``FETCH_CONFIRM_TOOLS`` views.
    Computed on read (not at import) so it reflects the fully-populated registry
    regardless of workflow-module import order.
    """
    return frozenset(
        name
        for name, entry in TOOL_REGISTRY.items()
        if entry.metadata.gate_spec is not None
        and entry.metadata.gate_spec.kind == kind
    )

# User-decision gates must NOT expire in the TRID3NT local build: the user
# OWNS the machine and the LLM, so a gate card should wait for them
# indefinitely. "Effectively unbounded" = 24h -- long enough that no human
# session ever hits it, finite so an abandoned process still unwinds its
# futures.
_LOCAL_GATE_TIMEOUT_SECONDS: int = 24 * 3600

def _gate_wait_cap_s() -> "float | None":
    """Optional hard ceiling (seconds) applied to EVERY gate wait window.

    Test seam (``TRID3NT_GATE_WAIT_CAP_S``): headless suites exercise the
    gate-park machinery with no client to answer the card, so the 24h F6
    local-lane lift (and even the cloud 300s defaults) would hang the run.
    When this env is set to a positive value, ``_gate_wait_timeout`` returns
    ``min(configured, cap)`` for every gate -- the F6 24h override included --
    so the wait deterministically hits the honest timeout path. UNSET (the
    production case) leaves every wait byte-identical; malformed / non-positive
    values are ignored (treated as unset). Read LIVE so a per-test env flip is
    honored.
    """
    raw = os.environ.get("TRID3NT_GATE_WAIT_CAP_S")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None

def _gate_wait_timeout(default_seconds: float) -> float:
    """Effective ``asyncio.wait_for`` timeout for a user-decision gate future.

    24h: confirmation / resolution / credential / region-choice / spatial-input
    gates must never time out on a user who stepped away, and the solve is on
    their own machine waiting for them. ``default_seconds`` is what the CARD
    advertises and is left alone - the wire envelope (``ttl_seconds`` etc.) is
    not rewritten, so the client contract is untouched.

    Test cap (``TRID3NT_GATE_WAIT_CAP_S``, see ``_gate_wait_cap_s``): when set,
    the resolved wait is floored to ``min(effective, cap)`` so headless suites
    never hang on an unanswerable card. Unset -> production behavior unchanged.
    """
    effective = float(_LOCAL_GATE_TIMEOUT_SECONDS)
    cap = _gate_wait_cap_s()
    if cap is not None:
        return min(effective, cap)
    return effective

async def _gate_on_confirm(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
    gate_spec: GateSpec,
    _warning_id_out: dict[str, str] | None = None,
) -> tuple[bool, dict]:
    """Generic confirm gate driven by a tool's declared ``GateSpec`` (ADR 0273).

    The ONE gate engine for every solver/fetch confirm card -- the per-engine
    ``if/elif`` card-building chain, the seven per-engine locals, and the
    per-engine decision-tail branches are gone. Membership is the caller's
    ``gate_spec`` presence check (:func:`_gate_spec_for`); the card is built by the
    spec's declared ESTIMATE provider (exported from the tool's own module); the
    decision is applied by the spec's declared PIN provider (the SAME per-engine
    tail arithmetic, relocated to the engine). Both are pure functions named by
    dotted path and imported lazily (:func:`call_provider`, which awaits async
    providers -- the TELEMAC mesh preview / SWMM real-cap re-probe).

    Fail-open semantics preserved EXACTLY: an estimate-provider failure falls
    through to dispatch (``True``) so the tool raises its own typed param error;
    an estimate whose envelope is ``None`` (the fetch_landcover no-coarsening
    skip) dispatches as-is; a headless timeout / explicit cancel fails closed.

    ``_warning_id_out``: optional out-param -- stashed the moment a REAL gate is
    emitted, so the turn-memory wrapper knows a decision is worth memoizing. It
    stays unset on every fail-open early return (no gate emitted there).
    """
    # Build the confirm card via the tool's declared estimate provider. Any
    # failure fails OPEN -- the gate must never mask a parameter problem behind a
    # confusing confirm card (the composer then raises its own typed error).
    try:
        estimate = await call_provider(
            gate_spec.estimate_provider,
            params,
            tool_name=tool_name,
            # A provider that paints a layer before its card reads the emitter;
            # every other one ignores it. getattr so a minimal/headless state
            # without an emitter still gates.
            emitter=getattr(state, "emitter", None),
        )
    except Exception:  # noqa: BLE001 -- never mask param errors with a gate
        logger.warning(
            "confirm gate could not build the card for %s; falling through so "
            "the tool raises its typed error",
            tool_name,
            exc_info=True,
        )
        return True, params

    if estimate.envelope is None:
        # Estimate provider signalled NO gate needed (fetch_landcover
        # no-coarsening skip): dispatch as-is.
        return True, params

    envelope = estimate.envelope
    warning_id = envelope.warning_id
    if _warning_id_out is not None:
        # A real gate is about to be sent -- the caller may memoize whatever
        # decision comes back (proceed/narrow_scope only; a cancel raises before
        # the caller's write site is reached).
        _warning_id_out["warning_id"] = warning_id
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_confirmation(state.session_id, warning_id, fut)

    await _session_safe_send(websocket, state.session_id,
        _new_envelope("tool-payload-warning", state.session_id, envelope)
    )
    logger.info(
        "confirm gate emitted session=%s tool=%s warning_id=%s kind=%s",
        state.session_id,
        tool_name,
        warning_id,
        gate_spec.kind,
    )

    try:
        decision_payload: PayloadConfirmationEnvelopePayload = await asyncio.wait_for(
            fut, timeout=_gate_wait_timeout(CODE_EXEC_CONFIRM_TIMEOUT_SECONDS)
        )
    except asyncio.TimeoutError:
        logger.warning(
            "confirm gate timeout session=%s tool=%s warning_id=%s",
            state.session_id,
            tool_name,
            warning_id,
        )
        await _send_error(
            websocket,
            state.session_id,
            "CONFIRMATION_TIMEOUT",
            f"{tool_name} parameter-confirmation gate timed out; "
            "the solver did not run",
        )
        return False, params
    finally:
        _pop_pending_confirmation(warning_id)

    logger.info(
        "confirm decision session=%s tool=%s warning_id=%s decision=%s",
        state.session_id,
        tool_name,
        warning_id,
        decision_payload.decision,
    )

    if decision_payload.decision == "cancel":
        # Explicit cancel: fail-closed (no run).
        await _send_error(
            websocket,
            state.session_id,
            "USER_INPUT_CANCELLED",
            f"{tool_name} declined by user "
            f"(decision={decision_payload.decision!r}); the solver did not run",
        )
        return False, params

    # proceed / narrow_scope. The declared PIN provider (when present) owns the
    # approved-params DELTA -- the engine's own tail arithmetic (fetch floor-clamp,
    # flood dual-lever, TELEMAC seed-decouple, SWMM real-cap re-probe). A pin
    # provider returning None fails closed (a narrow_scope to a card that never
    # advertised an override). A gate with NO pin provider is a plain
    # proceed/cancel: a narrow_scope fails closed; a proceed injects ``confirmed``
    # for a solver (a fetch reads nothing).
    if gate_spec.pin_provider is not None:
        delta = await call_provider(
            gate_spec.pin_provider,
            decision_payload.decision,
            decision_payload.revised_args or {},
            params,
            estimate.tail_state,
        )
        if delta is None:
            await _send_error(
                websocket,
                state.session_id,
                "USER_INPUT_CANCELLED",
                f"{tool_name} declined by user "
                f"(decision={decision_payload.decision!r}); the solver did not run",
            )
            return False, params
        return True, {**params, **delta}

    if decision_payload.decision == "narrow_scope":
        # A lever-less gate never advertised narrow_scope -> fail-closed.
        await _send_error(
            websocket,
            state.session_id,
            "USER_INPUT_CANCELLED",
            f"{tool_name} declined by user "
            f"(decision={decision_payload.decision!r}); the solver did not run",
        )
        return False, params

    approved = dict(params)
    if gate_spec.kind == "solver":
        approved["confirmed"] = True
    return True, approved

async def _gate_on_solver_confirm(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
    _warning_id_out: dict[str, str] | None = None,
) -> tuple[bool, dict]:
    """Thin compat entrypoint: resolve the tool's ``GateSpec`` + run the engine.

    Kept as the stable name the gate-behavior suite drives directly. Resolves
    membership from metadata (:func:`_gate_spec_for`) and delegates to the
    generic :func:`_gate_on_confirm`; an un-gated tool fails open (the dispatch
    site never routes one here).
    """
    gate_spec = _gate_spec_for(tool_name)
    if gate_spec is None:
        return True, params
    return await _gate_on_confirm(
        websocket, state, tool_name, params, gate_spec,
        _warning_id_out=_warning_id_out,
    )

async def _gate_with_turn_memory(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
) -> tuple[bool, dict]:
    """``_gate_on_solver_confirm`` wrapped with per-turn decision memory.

    Checks ``state.gate_decisions_this_turn`` (keyed by
    :func:`_gate_memory_key`) BEFORE calling the real gate. A remembered
    "proceed" / "narrow_scope" decision from earlier in the SAME turn is
    auto-applied (its recorded param DELTA is merged onto the current call's
    params) and no new gate is emitted. A "cancel" is never recorded (the
    real gate raises via ``should_run=False`` before this wrapper's write
    site), so a corrected retry after a cancel still gates fresh. A
    DIFFERENT tool or a DIFFERENT bbox in the same turn always gates
    normally (different key -> memory miss).
    """
    gate_key = _gate_memory_key(tool_name, params)
    remembered = state.gate_decisions_this_turn.get(gate_key)
    if remembered is not None:
        merged = {**params, **remembered["overrides"]}
        logger.info(
            "solver-confirm gate auto-applied from turn memory "
            "session=%s tool=%s warning_id_prior=%s",
            state.session_id,
            tool_name,
            remembered["warning_id"],
        )
        return True, merged

    pre_gate_params = dict(params)
    warning_id_box: dict[str, str] = {}
    should_run, approved = await _gate_on_solver_confirm(
        websocket, state, tool_name, params, _warning_id_out=warning_id_box
    )
    if not should_run:
        return False, approved

    prior_warning_id = warning_id_box.get("warning_id")
    if prior_warning_id is not None:
        # A real gate was sent and answered proceed/narrow_scope (a cancel
        # returns should_run=False above and is never memoized, so a
        # corrected retry after a cancel still gates fresh). Remember only
        # the DELTA the gate applied to params - not the whole approved
        # dict - so a later retry keeps ITS OWN corrected non-bbox args
        # (e.g. a fixed `dataset`) and only inherits what the gate itself
        # decided (e.g. `resolution_m`).
        overrides = {
            k: v
            for k, v in approved.items()
            if k not in pre_gate_params or pre_gate_params[k] != v
        }
        state.gate_decisions_this_turn[gate_key] = {
            "overrides": overrides,
            "warning_id": prior_warning_id,
        }
    return True, approved

# --------------------------------------------------------------------------- #
# Payload-warning gate.
# --------------------------------------------------------------------------- #


async def _maybe_gate_on_payload_warning(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
) -> tuple[bool, dict]:
    """Run the payload-warning gate before dispatching ``tool_name``.

    Returns ``(should_dispatch, effective_params)``:

    - ``(True, params)`` -- no warning needed (no estimator, estimate below
      threshold) OR user picked ``proceed``. Dispatch with ``params``.
    - ``(True, revised_args)`` -- user picked ``narrow_scope``. Dispatch with
      the user's revised args.
    - ``(False, params)`` -- user picked ``cancel`` OR the gate timed out.
      Skip the dispatch; the caller surfaces a typed failure to chat.

    Audit-log entries are appended to ``state.payload_warning_audit_log``
    on both emission AND decision. Never raises -- a gate failure logs +
    falls through to dispatch (the gate is a UX nudge, not a hard
    invariant; a broken estimator should not break the tool).
    """
    entry = TOOL_REGISTRY.get(tool_name)
    if entry is None:
        return True, params
    estimator_name = entry.metadata.payload_mb_estimator_name
    if not estimator_name:
        return True, params
    estimator_fn = _resolve_payload_estimator(tool_name, estimator_name)
    if estimator_fn is None:
        return True, params
    try:
        # Offloaded: a sampled estimator (resolution doctrine R-B) may read the
        # network to MEASURE a small native window; keep it off the event loop so it
        # cannot stall the WS keepalive (no-sync-blocking norm).
        estimated_mb = float(await asyncio.to_thread(estimator_fn, **params))
    except Exception:  # noqa: BLE001 -- never let the gate kill a tool
        logger.exception(
            "payload-warning: estimator raised tool=%s name=%s; skipping gate",
            tool_name,
            estimator_name,
        )
        return True, params

    threshold_mb = _get_warning_threshold_mb()
    hard_cap_mb = _get_hard_cap_mb()
    if estimated_mb < threshold_mb:
        return True, params

    over_hard_cap = estimated_mb > hard_cap_mb
    options = (
        ["cancel", "narrow_scope"]
        if over_hard_cap
        else ["proceed", "cancel", "narrow_scope"]
    )
    recommendation = (
        f"Estimated payload {estimated_mb:.1f} MB exceeds the "
        f"{'hard cap' if over_hard_cap else 'warning threshold'} "
        f"({hard_cap_mb if over_hard_cap else threshold_mb:.0f} MB). "
        "Consider narrowing bbox or other scope parameters."
    )
    # Resolution doctrine R-B: an OPTIONAL ``<estimator>_detail`` companion returns a
    # one-line human string carrying the MEASURED-vs-analytic kind + a concrete
    # coarsening suggestion ("native ~2.4 GB measured; suggested coarsening 199 m ~0.4
    # MB; proceed native / coarsen / cancel"). Appended to the recommendation so the
    # card quotes real numbers -- no new envelope field / WS event. Best-effort.
    detail_fn = _resolve_payload_estimator(tool_name, f"{estimator_name}_detail")
    if detail_fn is not None:
        try:
            detail = await asyncio.to_thread(detail_fn, **params)
        except Exception:  # noqa: BLE001 -- detail is a nicety, never fatal
            detail = None
        if detail:
            recommendation = f"{recommendation} {detail}"[:512]

    warning_id = new_ulid()
    warning_payload = PayloadWarningEnvelopePayload(
        warning_id=warning_id,
        tool_name=tool_name,
        tool_args=params,
        estimated_mb=estimated_mb,
        threshold_mb=hard_cap_mb if over_hard_cap else threshold_mb,
        recommendation=recommendation,
        options=options,
    )

    # Audit-log the emission.
    audit_entry: dict = {
        "warning_id": warning_id,
        "tool_name": tool_name,
        "estimated_mb": estimated_mb,
        "threshold_mb": warning_payload.threshold_mb,
        "options": list(options),
        "emitted_at": now_utc().isoformat(),
        "decision": None,
    }
    state.payload_warning_audit_log.append(audit_entry)

    # Create the future the inbound handler will complete.
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_confirmation(state.session_id, warning_id, fut)

    await _session_safe_send(websocket, state.session_id,
        _new_envelope("tool-payload-warning", state.session_id, warning_payload)
    )
    logger.info(
        "payload-warning emitted session=%s tool=%s warning_id=%s estimated_mb=%.2f over_hard_cap=%s",
        state.session_id,
        tool_name,
        warning_id,
        estimated_mb,
        over_hard_cap,
    )

    # Await the confirmation (TTL on the envelope is advisory; we honour it
    # with an asyncio timeout so the dispatch coroutine doesn't hang forever).
    try:
        decision_payload: PayloadConfirmationEnvelopePayload = await asyncio.wait_for(
            fut, timeout=_gate_wait_timeout(warning_payload.ttl_seconds)
        )
    except asyncio.TimeoutError:
        audit_entry["decision"] = "timeout"
        logger.warning(
            "payload-warning timeout session=%s tool=%s warning_id=%s",
            state.session_id,
            tool_name,
            warning_id,
        )
        await _send_error(
            websocket,
            state.session_id,
            "CONFIRMATION_TIMEOUT",
            f"tool {tool_name!r} payload-warning gate timed out",
        )
        return False, params
    finally:
        _pop_pending_confirmation(warning_id)

    audit_entry["decision"] = decision_payload.decision
    audit_entry["decided_at"] = now_utc().isoformat()
    logger.info(
        "payload-warning decision session=%s tool=%s warning_id=%s decision=%s",
        state.session_id,
        tool_name,
        warning_id,
        decision_payload.decision,
    )

    if decision_payload.decision == "cancel":
        await _send_error(
            websocket,
            state.session_id,
            "USER_INPUT_CANCELLED",
            f"tool {tool_name!r} cancelled by user at payload-warning gate "
            f"(estimated {estimated_mb:.1f} MB)",
        )
        return False, params
    if decision_payload.decision == "proceed":
        if over_hard_cap:
            # Defense in depth: the warning envelope omitted ``proceed`` so a
            # well-behaved client can't pick it. Refuse if it does anyway.
            await _send_error(
                websocket,
                state.session_id,
                "TOOL_PARAMS_INVALID",
                f"tool {tool_name!r} exceeds hard cap "
                f"({estimated_mb:.1f} > {hard_cap_mb:.0f} MB); "
                "'proceed' is not an allowed response",
            )
            return False, params
        return True, params
    # narrow_scope
    revised = decision_payload.revised_args or {}
    return True, revised

async def _gate_on_code_exec(
    websocket: ServerConnection,
    state: SessionState,
    params: dict,
) -> tuple[bool, dict]:
    """Confirm gate for ``code_exec_request`` -- MANDATORY, fail-closed.

    Running arbitrary Python is a consequential action; the user MUST approve
    the exact code before the sandbox runs. This gate emits a
    ``code-exec-request`` confirm card and blocks on the SAME
    ``pending_payload_warnings`` future seam the payload-warning gate uses
    (the ``code_exec_id`` is the correlation key, carried back as the
    ``tool-payload-confirmation.warning_id``).

    Returns ``(should_dispatch, effective_params)``:

    - ``(True, params + {confirmed: True, code_exec_id})`` -- user approved
      (``decision="proceed"``). The tool body runs the sandbox.
    - ``(False, params)`` -- user chose ``cancel``. The caller raises
      :class:`CodeExecConfirmationCancelledError` so the model sees a typed,
      non-retryable error and narrates the decline honestly.

    Raises :class:`CodeExecApprovalTimeoutError` when NO confirmation answers
    the card within ``_code_exec_approval_timeout_s()`` (default 180s, env
    ``TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S``). This wait deliberately bypasses
    the 24h local-lane ``_gate_wait_timeout`` override: an unanswerable card
    must resolve the parked tool call with a typed error so the turn
    completes instead of hanging. The pending-confirmation registry entry is
    popped in the ``finally`` below on EVERY exit -- approve, deny, timeout,
    and task cancellation (session close / turn cancel) -- so nothing leaks.

    ``narrow_scope`` is NOT offered for code-exec (you don't "narrow" a code
    snippet -- you cancel and the agent rewrites it); a ``narrow_scope``
    reply is treated as a cancel (fail-closed).
    """
    python_code = params.get("python_code")
    if not isinstance(python_code, str) or not python_code.strip():
        # No code to confirm -- let the tool body raise its own params error.
        return True, params

    code_exec_id = new_ulid()
    request_payload = CodeExecRequestPayload(
        code_exec_id=code_exec_id,
        python_code=python_code,
        layer_refs=params.get("layer_refs") or {},
        rationale=params.get("rationale"),
    )

    # Create the future the inbound ``tool-payload-confirmation`` handler completes
    # (keyed on code_exec_id == warning_id). Same seam as the payload-warning gate.
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_confirmation(state.session_id, code_exec_id, fut)

    await _session_safe_send(websocket, state.session_id,
        _new_envelope("code-exec-request", state.session_id, request_payload)
    )
    logger.info(
        "code-exec-request emitted session=%s code_exec_id=%s code_len=%d n_layers=%d",
        state.session_id,
        code_exec_id,
        len(python_code),
        len(request_payload.layer_refs),
    )

    approval_timeout_s = _code_exec_approval_timeout_s()
    try:
        decision_payload: PayloadConfirmationEnvelopePayload = await asyncio.wait_for(
            fut, timeout=approval_timeout_s
        )
    except asyncio.TimeoutError:
        logger.warning(
            "code-exec confirm gate timeout session=%s code_exec_id=%s "
            "waited=%.0fs (approval card never answered)",
            state.session_id,
            code_exec_id,
            approval_timeout_s,
        )
        # WS envelope: ``error_code`` is the closed A.6 ``ErrorCode`` Literal
        # (contracts are read-only), so the wire code stays the contract-valid
        # CONFIRMATION_TIMEOUT; the DISTINCT typed code below
        # (CODE_EXEC_APPROVAL_TIMEOUT) rides the function_response surface,
        # which is free-form.
        await _send_error(
            websocket,
            state.session_id,
            "CONFIRMATION_TIMEOUT",
            f"code_exec_request {code_exec_id!r} approval card was not answered "
            f"within {approval_timeout_s:.0f}s; the sandbox did not run",
        )
        # Typed resolution of the parked tool call: propagates to the tool
        # dispatch except-handler -> summarize_tool_result(error=...) -> a
        # structured function_response the LLM narrates -- the turn COMPLETES.
        raise CodeExecApprovalTimeoutError(code_exec_id, approval_timeout_s)
    finally:
        # Runs on approve, deny, timeout, AND CancelledError (session close /
        # turn cancel) -- the registry never leaks a dead future.
        _pop_pending_confirmation(code_exec_id)

    logger.info(
        "code-exec confirm decision session=%s code_exec_id=%s decision=%s",
        state.session_id,
        code_exec_id,
        decision_payload.decision,
    )

    if decision_payload.decision != "proceed":
        # cancel OR narrow_scope (the latter is meaningless for code; fail-closed).
        await _send_error(
            websocket,
            state.session_id,
            "USER_INPUT_CANCELLED",
            f"code_exec_request {code_exec_id!r} declined by user "
            f"(decision={decision_payload.decision!r}); the sandbox did not run",
        )
        return False, params

    # Approved: inject the gate-cleared flags so the tool body dispatches with the
    # SAME code_exec_id the request card carried (so request/result cards correlate).
    approved = dict(params)
    approved["confirmed"] = True
    approved["code_exec_id"] = code_exec_id
    return True, approved

# --------------------------------------------------------------------------- #
# Credential pipeline (job VAULT-READ): secret_ref injection + auth-error ->
# credential-request -> retry.
# --------------------------------------------------------------------------- #


async def _inject_secret_ref(
    state: SessionState,
    tool_name: str,
    params: dict,
    case_id: str | None,
) -> dict:
    """Thread the resolved credential VALUE into a keyed tool's ``secret_ref``.

    Resolution runs through ``credentials.resolver`` (in-memory session cache the
    plugin pushed over the ``secret-add`` seam -> env fallback). The raw value is
    injected as a ``str`` ``secret_ref``, which every keyed fetcher's
    ``_materialize_secret`` accepts verbatim -- no file vault, no Persistence
    read on the path.

    No-op for non-keyed tools, when the caller already supplied an explicit
    ``secret_ref`` / key kwarg, or when neither source has a value (the fetcher's
    own env fallback then runs, and absent a key raises its typed auth error,
    which the credential-request flow acts on). ``case_id`` is unused: the
    session cache is session-scoped, not per-Case.
    """
    if provider_for_tool(tool_name) is None:
        return params
    # Respect an explicit override already on params (dev/test path).
    if params.get("secret_ref") is not None:
        return params
    value = resolve_credential(state.session_id, tool_name)
    if not value:
        return params
    params = dict(params)
    params["secret_ref"] = value
    logger.info(
        "secret_ref injected tool=%s provider=%s (session cache / env)",
        tool_name,
        provider_for_tool(tool_name).provider_id,
    )
    return params

async def _maybe_handle_credential_error(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
    error: BaseException,
    case_id: str | None,
) -> dict | None:
    """Handle a keyed-tool credential error: prompt + await + re-resolve.

    Returns:
    - ``dict`` (retry params with a freshly-resolved ``secret_ref``) when the
      user supplied a key (``credential-provided`` with ``provided=True``) --
      the caller retries the tool ONCE.
    - ``None`` when the error is NOT credential-shaped, the tool already
      prompted this turn (one-prompt-per-tool-per-turn guard), or the user
      declined / the gate timed out. The caller then re-raises the original
      error so it flows through the normal typed-error surface and
      the LLM narrates the failure honestly.

    Two paths:
    1. REGISTERED tool (``provider_for_tool`` resolves): emit the real
       per-provider card (real ``signup_url`` from the registry -- the ONLY
       source of real URLs) and, on provided=True, re-resolve the credential
       (the plugin pushed the value into the session cache over ``secret-add``)
       so the retry reads the freshly-supplied key.
    2. UNREGISTERED tool with a credential-SHAPED error: emit a NAME-ONLY
       generic card (credential name derived from
       the tool, ``signup_url=None``, just the secret-entry form) so the user
       still gets a card and the agent NEVER narrates a fabricated URL. On
       provided=True we retry once with the original params (the tool reads its
       own key path); there is no per-Case ``secret_ref`` to inject for an
       unregistered provider.
    """
    provider = provider_for_tool(tool_name)
    is_registered_credential = (
        provider is not None and is_credential_error(tool_name, error)
    )
    is_generic_credential = (
        provider is None and is_credential_shaped_error(tool_name, error)
    )
    if not is_registered_credential and not is_generic_credential:
        return None

    # One prompt per tool per turn -- don't loop forever on a still-bad key.
    if tool_name in state.credential_prompted_tools:
        logger.info(
            "credential-request suppressed (already prompted this turn) tool=%s",
            tool_name,
        )
        return None

    if is_generic_credential:
        # NAME-ONLY card for a tool with no registered
        # provider. ``generic_provider_for_tool`` derives a human credential
        # name and pins ``signup_url=None`` (NO fabricated URL). The emit is
        # best-effort: if the generic ``provider_id`` is not yet a valid wire
        # ``ProviderID`` (schema-owned Literal), ``_emit_credential_request_and_wait``
        # → ``_build_credential_request_payload`` returns None and we surface
        # the original typed error instead -- we still NEVER invent a URL.
        generic_provider = generic_provider_for_tool(tool_name)
        state.credential_prompted_tools.add(tool_name)
        logger.info(
            "credential-request (generic name-only) tool=%s label=%r "
            "signup_url=None — no registered provider",
            tool_name,
            generic_provider.label,
        )
        provided = await _emit_credential_request_and_wait(
            websocket, state, tool_name, generic_provider, error
        )
        if provided is None or not provided.provided:
            return None
        # Unregistered provider: no per-Case secret_ref to inject. Retry once
        # with the original params (minus any stale inline key) so the tool can
        # pick up a key from its own resolution path.
        return {
            k: v for k, v in params.items()
            if k not in ("secret_ref", "map_key", "api_key")
        }

    # REGISTERED path: real per-provider card with a real signup_url.
    assert provider is not None  # narrowed by is_registered_credential
    state.credential_prompted_tools.add(tool_name)

    provided = await _emit_credential_request_and_wait(
        websocket, state, tool_name, provider, error
    )
    if provided is None or not provided.provided:
        # Declined / timed out: surface the original typed error.
        return None

    # Key pushed to the session cache: re-resolve the secret_ref so the retry
    # reads the NEW key. Strip any stale secret_ref/map_key from params first.
    retry_params = {
        k: v for k, v in params.items()
        if k not in ("secret_ref", "map_key", "api_key")
    }
    retry_params = await _inject_secret_ref(
        state, tool_name, retry_params, case_id
    )
    return retry_params

async def _emit_credential_request_and_wait(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    provider: CredentialProvider,
    error: BaseException,
) -> "CredentialProvidedEnvelopePayload | None":
    """Emit a ``credential-request`` envelope and await ``credential-provided``.

    Blocks on a future keyed by the minted ``request_id`` (registered in the
    session-scoped ``_PENDING_CREDENTIALS`` registry so a reply on a sibling
    connection still resolves it). Returns the ``CredentialProvidedEnvelopePayload``
    on reply, or ``None`` on timeout (the gate gets the same 300s read-decision
    TTL as the payload-warning / code-exec gates -- fail-open to the original
    typed error so the turn is not hung).
    """
    request_id = new_ulid()
    # Prefer the tool's typed-error message (honest, specific) over the
    # registry default; both name that a key is needed (no silent dead-end).
    err_detail = str(error).strip()
    message = provider.default_message
    if err_detail:
        message = f"{provider.default_message} ({err_detail[:400]})"

    # Build the envelope scoped to the REAL provider (every registered
    # provider_id is now a valid ``ProviderID`` Literal member). If validation
    # fails for an unregistered provider, ``_build_credential_request_payload``
    # returns ``None`` -- we abandon the prompt rather than mis-scope the
    # secret-add (which would save the key where the retry can't re-resolve it).
    # The caller then surfaces the original typed error (honest narration).
    payload = _build_credential_request_payload(
        request_id=request_id,
        provider=provider,
        tool_name=tool_name,
        message=message,
    )
    if payload is None:
        return None

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_credential(state.session_id, request_id, fut)

    await _session_safe_send(websocket, state.session_id,
        _new_envelope("credential-request", state.session_id, payload)
    )
    logger.info(
        "credential-request emitted session=%s tool=%s provider=%s request_id=%s",
        state.session_id,
        tool_name,
        provider.provider_id,
        request_id,
    )

    try:
        provided: CredentialProvidedEnvelopePayload = await asyncio.wait_for(
            fut, timeout=_gate_wait_timeout(CODE_EXEC_CONFIRM_TIMEOUT_SECONDS)
        )
    except asyncio.TimeoutError:
        logger.warning(
            "credential-request timeout session=%s tool=%s request_id=%s",
            state.session_id,
            tool_name,
            request_id,
        )
        return None
    finally:
        _pop_pending_credential(request_id)

    logger.info(
        "credential-provided received session=%s tool=%s request_id=%s provided=%s",
        state.session_id,
        tool_name,
        request_id,
        provided.provided,
    )
    return provided

async def _emit_region_choice_and_wait(
    websocket: ServerConnection,
    state: SessionState,
    payload: "RegionChoiceRequestEnvelopePayload",
) -> "RegionChoiceProvidedEnvelopePayload | None":
    """Emit a ``region-choice-request`` and await ``region-choice-provided``.

    Blocks on a future keyed by ``payload.request_id`` (registered in the
    session-scoped ``_PENDING_REGION_CHOICES`` registry so a reply on a sibling
    connection still resolves it). Returns the ``RegionChoiceProvidedEnvelopePayload``
    on reply, or ``None`` on timeout (the gate gets the same read-decision TTL
    as the credential / payload-warning / code-exec gates -- fail-open to the
    whole-state default so the turn is never hung).
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_region_choice(state.session_id, payload.request_id, fut)

    await _session_safe_send(websocket, state.session_id,
        _new_envelope("region-choice-request", state.session_id, payload)
    )
    logger.info(
        "region-choice-request emitted session=%s state=%s candidates=%d request_id=%s",
        state.session_id,
        payload.state_code,
        len(payload.candidates),
        payload.request_id,
    )

    try:
        provided: RegionChoiceProvidedEnvelopePayload = await asyncio.wait_for(
            fut, timeout=_gate_wait_timeout(CODE_EXEC_CONFIRM_TIMEOUT_SECONDS)
        )
    except asyncio.TimeoutError:
        logger.info(
            "region-choice-request timeout session=%s request_id=%s; "
            "using whole-state default",
            state.session_id,
            payload.request_id,
        )
        return None
    finally:
        _pop_pending_region_choice(payload.request_id)

    logger.info(
        "region-choice-provided received session=%s request_id=%s choice=%s",
        state.session_id,
        payload.request_id,
        provided.choice,
    )
    return provided

async def _maybe_handle_region_choice(
    websocket: ServerConnection,
    state: SessionState,
    geocode_result: dict,
) -> None:
    """If ``geocode_result`` is a state-snap, offer + apply a narrower region.

    No-op unless the geocode came back as a state-bbox-fallback
    (``source == "state-bbox-fallback"``). When it did, this:

    1. Builds the candidate sub-regions (default: counties of the state) and
       emits a ``region-choice-request`` (whole-state default + candidates +
       an honest prompt).
    2. PAUSES the turn awaiting ``region-choice-provided`` (fail-open: a
       headless client / timeout keeps the whole-state bbox).
    3. On ``choice == "region"`` MUTATES ``geocode_result`` in place to the
       picked region's bbox (re-resolved by ``selected_region_id`` against
       the candidate set -- authoritative over a client-sent bbox; falls
       back to ``selected_bbox`` only when the id is unknown) and stamps
       narrowing provenance so downstream tools + the function_response
       the model reads use the narrowed extent. On ``choice == "whole_state"``
       leaves the state bbox unchanged.

    Best-effort: any failure leaves the whole-state bbox intact -- the
    narrowing is a UX nicety layered ON TOP of an already-correct result, so
    it must never break the turn. Never raises.
    """
    if geocode_result.get("source") != "state-bbox-fallback":
        return
    if state.emitter is None:
        # No interactive surface bound; keep the whole-state default.
        return
    try:
        request_id = new_ulid()
        payload = _build_region_choice_request_payload(
            request_id=request_id, geocode_result=geocode_result
        )
        if payload is None:
            return
        provided = await _emit_region_choice_and_wait(websocket, state, payload)
        if provided is None or provided.choice == "whole_state":
            # Declined / timed out / explicit whole-state -- keep the state bbox.
            geocode_result["region_choice"] = "whole_state"
            return
        # choice == "region": resolve the picked candidate. Prefer re-resolving
        # by region_id against the candidate set (a tampered client bbox cannot
        # redirect the workflow); fall back to the echoed bbox only if unknown.
        chosen = None
        if provided.selected_region_id:
            chosen = next(
                (
                    c
                    for c in payload.candidates
                    if c.region_id == provided.selected_region_id
                ),
                None,
            )
        new_bbox: tuple[float, float, float, float] | None = None
        chosen_name: str | None = None
        if chosen is not None:
            new_bbox = chosen.bbox
            chosen_name = chosen.name
        elif provided.selected_bbox is not None:
            new_bbox = provided.selected_bbox
        if new_bbox is None:
            # The client said "region" but supplied neither a known id nor a
            # bbox -- keep the state default rather than guess.
            geocode_result["region_choice"] = "whole_state"
            return
        # Mutate the geocode result IN PLACE so the immediate zoom-to AND the
        # function_response the model reads (and any downstream bbox consumer) use
        # the narrowed extent.
        geocode_result["bbox"] = list(new_bbox)
        # The result is no longer a whole-state snap -- drop the fallback source
        # so a downstream re-trigger does not re-offer the picker, and record
        # honest provenance of the narrowing.
        geocode_result["source"] = "region-choice-narrowed"
        geocode_result["region_choice"] = "region"
        geocode_result["selected_region_id"] = provided.selected_region_id
        if chosen_name:
            geocode_result["name"] = chosen_name
            geocode_result["region_name"] = chosen_name
        # Recompute a rough centroid for the narrowed bbox so map snaps + any
        # centroid consumer stay consistent with the new extent.
        geocode_result["longitude"] = (new_bbox[0] + new_bbox[2]) / 2.0
        geocode_result["latitude"] = (new_bbox[1] + new_bbox[3]) / 2.0
        logger.info(
            "region-choice: narrowed to region_id=%s name=%r bbox=%s",
            provided.selected_region_id,
            chosen_name,
            new_bbox,
        )
    except Exception:  # noqa: BLE001 -- narrowing is a best-effort UX layer
        logger.warning(
            "region-choice handling failed; keeping whole-state bbox",
            exc_info=True,
        )

# --------------------------------------------------------------------------- #
# request_spatial_input -- pause the turn, await the drawn geometry.
# --------------------------------------------------------------------------- #
#
# Mirrors the region-choice pause/resume seam (``_emit_region_choice_and_wait``).
# The LLM-facing ``request_spatial_input`` tool (tools/spatial_input_tool.py)
# returns a sentinel result that this interception in the turn loop replaces with
# the parsed, role-split drawn geometry -- so the tool surface stays catalog-clean
# while the actual websocket pause/resume lives here (where the live socket +
# session future registry are reachable). The drawn barriers FeatureCollection
# round-trips straight into ``swmm_urban_flood(barriers=...)``.

# Sentinel result the ``request_spatial_input`` catalog tool returns; the turn
# loop detects it and replaces it with the real drawn-geometry result.
SPATIAL_INPUT_SENTINEL_KEY = "_request_spatial_input"

async def _emit_spatial_input_and_wait(
    websocket: ServerConnection,
    state: SessionState,
    payload: "SpatialInputRequestPayload",
) -> "SpatialInputResponsePayload | None":
    """Emit a ``spatial-input-request`` and await ``spatial-input-response``.

    Blocks on a future keyed by ``payload.request_id`` (registered in the
    session-scoped ``_PENDING_SPATIAL_INPUTS`` registry so a reply on a sibling
    connection still resolves it -- StrictMode double-mount / reconnect). Returns
    the ``SpatialInputResponsePayload`` on reply, or ``None`` on timeout (the gate
    gets the same read-decision TTL as the credential / region-choice gates --
    fail-open to a typed "no geometry drawn" result, never a hung turn).
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _register_pending_spatial_input(state.session_id, payload.request_id, fut)

    await _session_safe_send(websocket, state.session_id,
        _new_envelope("spatial-input-request", state.session_id, payload)
    )
    logger.info(
        "spatial-input-request emitted session=%s mode=%s request_id=%s",
        state.session_id,
        payload.mode,
        payload.request_id,
    )

    try:
        response: SpatialInputResponsePayload = await asyncio.wait_for(
            fut, timeout=_gate_wait_timeout(payload.default_timeout_seconds)
        )
    except asyncio.TimeoutError:
        logger.info(
            "spatial-input-request timeout session=%s request_id=%s; "
            "no geometry drawn",
            state.session_id,
            payload.request_id,
        )
        return None
    except SpatialInputInvalidResponseError:
        # The user's reply ARRIVED but failed structural validation (e.g. a
        # barrier missing barrier_type). The inbound handler failed the future
        # eagerly so we wake here IN-BAND -- NOT after the read TTL. Re-raise so
        # _handle_request_spatial_input surfaces the typed error result.
        logger.info(
            "spatial-input-request invalid-response session=%s request_id=%s; "
            "resolving turn with typed error (not timeout path)",
            state.session_id,
            payload.request_id,
        )
        raise
    finally:
        _pop_pending_spatial_input(payload.request_id)

    logger.info(
        "spatial-input-response received session=%s request_id=%s "
        "cancelled=%s geometry_type=%s",
        state.session_id,
        payload.request_id,
        response.cancelled,
        response.geometry_type,
    )
    return response

async def _handle_request_spatial_input(
    websocket: ServerConnection,
    state: SessionState,
    call_args: dict[str, Any],
) -> dict[str, Any]:
    """Drive one ``request_spatial_input`` turn-pause and return the LLM result.

    Builds the request from the LLM args, emits it, PAUSES the turn awaiting the
    drawn geometry, then parses + role-splits the reply into the engine-ready
    result. Never raises -- every failure path (no client, validation, parse,
    timeout, cancellation) becomes a typed result the LLM narrates honestly. The
    ``role=="barrier"`` features become the ``barriers`` FeatureCollection that
    feeds ``swmm_urban_flood`` -> ``SWMMRunArgs.barriers`` -> the existing
    ``build_swmm_mesh`` wall=omit-conduit / flap_gate=one-way-orifice seam.
    """
    if state.emitter is None:
        # No interactive surface bound (e.g. headless eval). Honest typed error.
        return {
            "status": "error",
            "error_code": "SPATIAL_INPUT_NO_CLIENT",
            "error_message": (
                "No interactive map client is connected, so the user cannot "
                "draw. Proceed without drawn barriers / AOI, or ask the user to "
                "provide a bbox in text."
            ),
        }
    request_id = new_ulid()
    payload = _build_spatial_input_request_payload(
        request_id=request_id, call_args=call_args
    )
    if payload is None:
        return {
            "status": "error",
            "error_code": "SPATIAL_INPUT_PARAMS_INVALID",
            "error_message": (
                "Could not build a valid spatial-input request from the given "
                "mode/title/description."
            ),
        }
    try:
        response = await _emit_spatial_input_and_wait(websocket, state, payload)
    except asyncio.CancelledError:
        raise
    except SpatialInputInvalidResponseError as exc:
        # The reply arrived but was structurally invalid (honesty floor: a
        # malformed drawn FeatureCollection -- e.g. a barrier missing
        # barrier_type -- degrades to a TYPED error result, NOT a silent success
        # and NOT a hung turn that drains default_timeout_seconds then reads as
        # SPATIAL_INPUT_TIMEOUT). The user already saw TOOL_PARAMS_INVALID.
        logger.info(
            "spatial-input invalid-response session=%s request_id=%s code=%s",
            state.session_id,
            request_id,
            exc.error_code,
        )
        return {
            "status": "error",
            "error_code": exc.error_code,
            "error_message": (
                f"The drawn geometry could not be used: {exc.error_message}. "
                f"Ask the user to redraw; do not fabricate barriers or an AOI."
            ),
        }
    except Exception:  # noqa: BLE001 -- degrade to a typed result, never crash
        logger.warning(
            "spatial-input emit/wait failed session=%s request_id=%s",
            state.session_id,
            request_id,
            exc_info=True,
        )
        return {
            "status": "error",
            "error_code": "SPATIAL_INPUT_FAILED",
            "error_message": (
                "The spatial-input request failed unexpectedly; no geometry was "
                "received. Do not fabricate barriers or an AOI."
            ),
        }
    return _spatial_response_to_result(response)


def __getattr__(name: str):
    if name == "SOLVER_CONFIRM_TOOLS":
        return _confirm_tools_by_kind("solver")
    if name == "FETCH_CONFIRM_TOOLS":
        return _confirm_tools_by_kind("fetch")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
