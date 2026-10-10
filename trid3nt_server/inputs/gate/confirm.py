"""The confirm engine and the user-decision gates that park a turn on a card.

Each blocks on a future the inbound confirmation handler resolves; a wait that runs out
resolves the parked call with a typed error rather than hanging it.
"""

from __future__ import annotations

import asyncio
import os
import logging
from trid3nt_contracts import new_ulid, now_utc
from trid3nt_contracts.gate_spec import GateSpec
from trid3nt_contracts.payload_warning import PayloadConfirmationEnvelopePayload, PayloadWarningEnvelopePayload
from trid3nt_contracts.processing_contracts import CodeExecRequestPayload
from trid3nt_contracts.ws import SpatialInputResponsePayload
from trid3nt_server.model.credentials.resolver import MissingCredentialError, credential_for_tool, resolve_credential
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_server.inputs.gate.cards import _build_spatial_input_request_payload, _gate_memory_key, _get_hard_cap_mb, _get_warning_threshold_mb, _resolve_payload_estimator, _spatial_response_to_result
from trid3nt_server.inputs.gate.cards.estimate import call_provider
from trid3nt_server.inputs.gate.pending import _PENDING_CONFIRMATIONS, _PENDING_SPATIAL_INPUTS
from trid3nt_server.inputs.gate.spatial_input_tool import SPATIAL_INPUT_SENTINEL_KEY
from trid3nt_server.server.config import _env_float
from trid3nt_server.inputs.gate.errors import GateConfirmationTimeoutError, SpatialInputInvalidResponseError
from trid3nt_server.server.session.state import SessionState
from trid3nt_server.server.turn.wire import _new_envelope, _send_error, _session_safe_send
from typing import Any
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("trid3nt_server.server")

# The decision window (seconds) the payload and solver-confirm gates share.
CODE_EXEC_CONFIRM_TIMEOUT_SECONDS: int = int(
    os.environ.get("TRID3NT_CODE_EXEC_CONFIRM_TIMEOUT", "300")
)


def _code_exec_approval_timeout_s() -> float:
    """The code-exec gate's approval window (``TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S``, default 180).

    An expired card raises ``CodeExecApprovalTimeoutError`` so the turn completes honestly."""
    return _env_float("TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S", 180.0)

# Gate membership is DERIVED: a tool's ``GateSpec`` on its ``AtomicToolMetadata`` is the
# one signal, and ``kind`` ('solver' | 'fetch') splits the two lanes: a solver strips a
# model-supplied ``confirmed`` before gating and injects it only on an explicit proceed.


def _gate_spec_for(tool_name: str) -> "GateSpec | None":
    """The declared :class:`GateSpec` for ``tool_name``, or ``None`` if un-gated."""
    entry = TOOL_REGISTRY.get(tool_name)
    if entry is None:
        return None
    return entry.metadata.gate_spec

def _confirm_tools_by_kind(kind: str) -> "frozenset[str]":
    """Registry-derived confirm-gate membership for one ``kind``, computed on read so import order cannot leave it partial."""
    return frozenset(
        name
        for name, entry in TOOL_REGISTRY.items()
        if entry.metadata.gate_spec is not None
        and entry.metadata.gate_spec.kind == kind
    )

# User-decision gates must NOT expire: the user owns the machine, so a gate waits
# indefinitely; 24h is the finite stand-in, so an abandoned process still unwinds its futures.
_LOCAL_GATE_TIMEOUT_SECONDS: int = 24 * 3600

# Test seam: a headless suite exercises the gate-park machinery with no client to answer
# the card, so the local-lane 24h wait would hang the run. With ``TRID3NT_GATE_WAIT_CAP_S``
# set, every gate waits ``min(configured, cap)`` and reaches the honest timeout path.
def _gate_wait_cap_s() -> "float | None":
    """Optional hard ceiling (seconds) applied to EVERY gate wait window.

    Read LIVE; unset, malformed or non-positive leave every wait unchanged."""
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

    ``default_seconds`` is what the CARD advertises and is not rewritten here."""
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
    """The ONE gate engine, driven by a tool's declared ``GateSpec``.

    Fail-OPEN on an estimate fault or a ``None`` envelope; fail-CLOSED on an explicit
    cancel, and on a deadline nobody answered, which RAISES."""
    # Build the confirm card via the tool's declared ESTIMATE provider, a pure function
    # named by dotted path and imported lazily. Any failure fails OPEN: the gate must
    # never mask a parameter problem behind a confusing card.
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
        # Estimate provider signalled NO gate needed (fetch_landcover already at
        # its native grid): dispatch as-is.
        return True, params

    envelope = estimate.envelope
    warning_id = envelope.warning_id
    if _warning_id_out is not None:
        # A real gate is about to be sent, so the caller may memoize whatever decision
        # comes back (proceed/narrow_scope only; a cancel raises before the caller's
        # write site is reached). Unset on every fail-open return above.
        _warning_id_out["warning_id"] = warning_id
    logger.info(
        "confirm gate emitted session=%s tool=%s warning_id=%s kind=%s",
        state.session_id,
        tool_name,
        warning_id,
        gate_spec.kind,
    )

    wait_s = _gate_wait_timeout(CODE_EXEC_CONFIRM_TIMEOUT_SECONDS)
    try:
        decision_payload: PayloadConfirmationEnvelopePayload = (
            await _PENDING_CONFIRMATIONS.park(
                state.session_id, warning_id,
                lambda: _session_safe_send(websocket, state.session_id, _new_envelope(
                    "tool-payload-warning", state.session_id, envelope)),
                wait_s))
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
        # Nobody answered, which is nobody's decision: the typed timeout keeps
        # the call fail-closed while reading to the model as the card expiring,
        # never as the user declining it.
        raise GateConfirmationTimeoutError(
            "parameter-confirmation card", tool_name, wait_s
        ) from None

    logger.info(
        "confirm decision session=%s tool=%s warning_id=%s decision=%s",
        state.session_id,
        tool_name,
        warning_id,
        decision_payload.decision,
    )

    if decision_payload.decision == "cancel":
        # Explicit cancel: fail-closed (no run). The wire carries the gate's OWN
        # code, so a client tells a declined card from any other cancellation.
        await _send_error(
            websocket,
            state.session_id,
            "SOLVER_CONFIRMATION_CANCELLED",
            f"{tool_name} declined by user "
            f"(decision={decision_payload.decision!r}); the solver did not run",
        )
        return False, params

    # proceed / narrow_scope. The declared PIN provider owns the approved-params DELTA;
    # one returning None fails closed (a narrow_scope on a card that offered no override).
    # With NO pin provider a narrow_scope fails closed and a proceed injects ``confirmed``
    # for a solver.
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
                "SOLVER_CONFIRMATION_CANCELLED",
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
            "SOLVER_CONFIRMATION_CANCELLED",
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
    """Resolve the tool's ``GateSpec`` and run the engine; an un-gated tool fails open."""
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

    A remembered decision replays its DELTA with no new card; another tool or bbox gates normally."""
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
        # A real gate was sent and answered proceed/narrow_scope (a cancel returns
        # should_run=False above and is never memoized, so a corrected retry after a
        # cancel still gates fresh). Only the DELTA the gate applied is remembered, so a
        # later retry keeps its own corrected non-bbox args.
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



async def _maybe_gate_on_payload_warning(
    websocket: ServerConnection,
    state: SessionState,
    tool_name: str,
    params: dict,
) -> tuple[bool, dict]:
    """Run the payload-warning gate before dispatching ``tool_name``.

    A gate FAULT never raises: it logs and falls through, the gate being a UX nudge. A
    card nobody answers raises the typed timeout."""
    # Returns (should_dispatch, effective_params): (True, params) when no warning is
    # needed or the user proceeds; (True, revised_args) on narrow_scope; (False, params)
    # on cancel, where the caller surfaces a typed decline to chat. An audit entry is
    # appended to ``state.payload_warning_audit_log`` on emission AND decision.
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
        # Offloaded: a sampled estimator may read the network to MEASURE a small
        # native window, and it must stay off the event loop so it cannot stall
        # the WS keepalive.
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
    # An OPTIONAL ``<estimator>_detail`` companion returns a one-line human string
    # carrying the measured-vs-analytic kind plus a concrete coarsening suggestion; it is
    # appended to the recommendation. Best-effort.
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
    wait_s = _gate_wait_timeout(warning_payload.ttl_seconds)
    try:
        decision_payload: PayloadConfirmationEnvelopePayload = (
            await _PENDING_CONFIRMATIONS.park(
                state.session_id, warning_id,
                lambda: _session_safe_send(websocket, state.session_id, _new_envelope(
                    "tool-payload-warning", state.session_id, warning_payload)),
                wait_s))
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
        # The audit row says timeout and so does the model's narration: a card
        # that expired is not the user cancelling the fetch.
        raise GateConfirmationTimeoutError(
            "payload-size warning card", tool_name, wait_s
        ) from None

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
            "PAYLOAD_WARNING_CANCELLED",
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
    """Confirm gate for ``run_pyqgis`` -- MANDATORY, fail-closed.

    The user approves the EXACT code first; ``narrow_scope`` is not offered and is a cancel,
    and a card nobody answers raises the typed timeout."""
    # Returns (should_dispatch, effective_params): on approval, params plus
    # ``confirmed`` and the ``code_exec_id`` the request card carried, so the request
    # and result cards correlate. On cancel the caller raises a typed non-retryable error.
    python_code = params.get("code")
    if not isinstance(python_code, str) or not python_code.strip():
        # No code to confirm -- let the tool body raise its own params error.
        return True, params

    code_exec_id = new_ulid()
    rationale = params.get("rationale")
    request_payload = CodeExecRequestPayload(
        code_exec_id=code_exec_id,
        python_code=python_code,
        rationale=rationale[:512] if isinstance(rationale, str) else None,
    )

    logger.info(
        "code-exec-request emitted session=%s code_exec_id=%s code_len=%d",
        state.session_id,
        code_exec_id,
        len(python_code),
    )

    # This wait deliberately bypasses the local-lane 24h override: an
    # unanswerable approval card must resolve the parked tool call with a typed
    # error so the turn COMPLETES instead of hanging on it.
    approval_timeout_s = _code_exec_approval_timeout_s()
    try:
        decision_payload: PayloadConfirmationEnvelopePayload = (
            await _PENDING_CONFIRMATIONS.park(
                state.session_id, code_exec_id,
                lambda: _session_safe_send(websocket, state.session_id, _new_envelope(
                    "code-exec-request", state.session_id, request_payload)),
                approval_timeout_s))
    except asyncio.TimeoutError:
        logger.warning(
            "code-exec confirm gate timeout session=%s code_exec_id=%s "
            "waited=%.0fs (approval card never answered)",
            state.session_id,
            code_exec_id,
            approval_timeout_s,
        )
        await _send_error(
            websocket,
            state.session_id,
            "CONFIRMATION_TIMEOUT",
            f"run_pyqgis {code_exec_id!r} approval card was not answered "
            f"within {approval_timeout_s:.0f}s; the code did not run",
        )
        # Typed resolution of the parked tool call: the turn COMPLETES, reading the card
        # as expired rather than as a refusal to run the code.
        raise GateConfirmationTimeoutError(
            "code-approval card", f"run_pyqgis {code_exec_id}", approval_timeout_s
        ) from None

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
            "CODE_EXEC_CANCELLED",
            f"run_pyqgis {code_exec_id!r} declined by user "
            f"(decision={decision_payload.decision!r}); the code did not run",
        )
        return False, {**params, "code_exec_id": code_exec_id}

    # Approved: inject the gate-cleared flags so the tool body dispatches with the
    # SAME code_exec_id the request card carried, so the result joins its card.
    approved = dict(params)
    approved["confirmed"] = True
    approved["code_exec_id"] = code_exec_id
    return True, approved

# The credential path: a keyed tool's key is resolved onto its ``secret_ref``
# before dispatch, and a keyed tool with no key REFUSES by name rather than
# dispatching a call that cannot succeed.


async def _inject_secret_ref(
    state: SessionState,
    tool_name: str,
    params: dict,
    case_id: str | None,
) -> dict:
    """Thread the resolved credential VALUE into a keyed tool's ``secret_ref``.

    A no-op for a public tool or an explicit ref; a keyed tool with no key raises the refusal naming the keys form."""
    # ``case_id`` is unused: the credential cache is session-scoped, not
    # per-Case.
    credential = credential_for_tool(tool_name)
    if credential is None:
        return params
    # Respect an explicit override already on params (dev/test path).
    if params.get("secret_ref") is not None:
        return params
    value = resolve_credential(state.session_id, tool_name)
    if not value:
        raise MissingCredentialError(credential)
    params = dict(params)
    # The raw value goes in as a plain ``str``, which every keyed fetcher accepts
    # verbatim: no file vault and no persistence read on this path.
    params["secret_ref"] = value
    logger.info(
        "secret_ref injected tool=%s credential=%s (session cache / env)",
        tool_name,
        credential.name,
    )
    return params

# The LLM-facing tool returns a sentinel the turn loop replaces with the parsed drawn
# geometry; the websocket pause/resume lives here, where the live socket and the session
# future registry are reachable.

async def _emit_spatial_input_and_wait(
    websocket: ServerConnection,
    state: SessionState,
    payload: "SpatialInputRequestPayload",
) -> "SpatialInputResponsePayload | None":
    """Emit a ``spatial-input-request`` and await ``spatial-input-response``.

    ``None`` on timeout; the caller turns that into a typed "nothing drawn"."""
    logger.info(
        "spatial-input-request emitted session=%s mode=%s request_id=%s",
        state.session_id,
        payload.mode,
        payload.request_id,
    )

    try:
        # Session-scoped, so a reply arriving on a sibling connection after a
        # double-mount or a reconnect still resolves it.
        response: SpatialInputResponsePayload = await _PENDING_SPATIAL_INPUTS.park(
            state.session_id, payload.request_id,
            lambda: _session_safe_send(websocket, state.session_id, _new_envelope(
                "spatial-input-request", state.session_id, payload)),
            _gate_wait_timeout(payload.default_timeout_seconds))
    except asyncio.TimeoutError:
        logger.info(
            "spatial-input-request timeout session=%s request_id=%s; "
            "no geometry drawn",
            state.session_id,
            payload.request_id,
        )
        return None
    except SpatialInputInvalidResponseError:
        # The user's reply ARRIVED but failed structural validation (e.g. a feature
        # carrying an unknown role). The inbound handler failed the future eagerly, so this
        # wakes in-band, not after the read TTL; re-raised for the typed error result.
        logger.info(
            "spatial-input-request invalid-response session=%s request_id=%s; "
            "resolving turn with typed error (not timeout path)",
            state.session_id,
            payload.request_id,
        )
        raise

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

    Never raises: no client, an invalid reply, a timeout and a cancellation all become a typed result."""
    if state.emitter is None:
        # No interactive surface bound (e.g. headless eval). Honest typed error.
        return {
            "status": "error",
            "error_code": "SPATIAL_INPUT_NO_CLIENT",
            "error_message": (
                "No interactive map client is connected, so the user cannot "
                "draw. Proceed without a drawn AOI, or ask the user to "
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
        # A malformed drawn FeatureCollection degrades to a TYPED error result, never a
        # silent success and never a hung turn that reads as a timeout.
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
                f"Ask the user to redraw; do not fabricate an AOI."
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
                "received. Do not fabricate an AOI."
            ),
        }
    return _spatial_response_to_result(response)


def __getattr__(name: str):
    if name == "SOLVER_CONFIRM_TOOLS":
        return _confirm_tools_by_kind("solver")
    if name == "FETCH_CONFIRM_TOOLS":
        return _confirm_tools_by_kind("fetch")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
