"""A decline is not an error, and a timeout is not a decline.

Each of the three gates - the payload-size warning, the ``run_pyqgis`` code
approval and the run confirmation - answers a cancel with ITS OWN wire code, a
pipeline step marked cancelled rather than failed, and a plain declined result
for the model; the circuit breaker leaves the declined tool's budget alone. A
card nobody answers is a different outcome at all three of those seams."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.tool_registry import AtomicToolMetadata
from trid3nt_contracts.ws import ErrorPayload, PayloadConfirmationEnvelopePayload

from trid3nt_server.adapters.adapter import summarize_tool_result
from trid3nt_server.gates.circuit_breaker import ToolCircuitBreaker
from trid3nt_server.gates.pending import _PENDING_CONFIRMATIONS
from trid3nt_server.server import SessionState, _invoke_tool_via_emitter
from trid3nt_server.server.errors import (
    CodeExecConfirmationCancelledError,
    GateConfirmationTimeoutError,
    PayloadWarningCancelledError,
    SolverConfirmationCancelledError,
    UserDeclinedError,
)
from trid3nt_server.tools import TOOL_REGISTRY, RegisteredTool


#: The three declines, each with the tool whose gate raises it and the code it
#: must carry, so every assertion below runs against all three.
DECLINES = [
    ("payload_warning_gate_tool", PayloadWarningCancelledError, "PAYLOAD_WARNING_CANCELLED"),
    ("run_pyqgis", CodeExecConfirmationCancelledError, "CODE_EXEC_CANCELLED"),
    ("fetch_dem", SolverConfirmationCancelledError, "SOLVER_CONFIRMATION_CANCELLED"),
]

#: Params that reach each tool's gate without touching the network: the gate
#: answers before any tool body runs.
PARAMS: dict[str, dict] = {
    "payload_warning_gate_tool": {"mb": 50.0},
    "run_pyqgis": {"code": "result = 1 + 1", "rationale": "proof"},
    "fetch_dem": {"bbox": [-114.48, 42.55, -114.46, 42.57]},
}


def estimate_payload_mb(**kwargs: Any) -> float:
    """Dummy estimator resolved by name off this module; reads ``mb``."""
    return float(kwargs.get("mb", 0.0))


def _dummy_tool(**kwargs: Any) -> dict:
    return {"received": dict(kwargs)}


@pytest.fixture(autouse=True)
def _cap_gate_waits(monkeypatch) -> None:
    monkeypatch.setenv("TRID3NT_GATE_WAIT_CAP_S", "5")


@pytest.fixture(autouse=True)
def _register_the_payload_warning_tool() -> None:
    """Add the one dummy beside the real registry; the other two gates are the
    real ``run_pyqgis`` and ``fetch_dem`` entries."""
    name = "payload_warning_gate_tool"
    TOOL_REGISTRY[name] = RegisteredTool(
        metadata=AtomicToolMetadata(
            name=name,
            ttl_class="dynamic-1h",
            source_class="dummy",
            cacheable=True,
            payload_mb_estimator_name="estimate_payload_mb",
        ),
        fn=_dummy_tool,
        module=__name__,
    )
    try:
        yield
    finally:
        TOOL_REGISTRY.pop(name, None)


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        self.sent.append(json.loads(raw) if isinstance(raw, str) else raw)


async def _cancel_the_card() -> None:
    """Answer the one pending gate card with a cancel, once it registers."""
    for _ in range(400):
        if _PENDING_CONFIRMATIONS:
            break
        await asyncio.sleep(0.005)
    wid = next(iter(_PENDING_CONFIRMATIONS))
    _PENDING_CONFIRMATIONS[wid][1].set_result(
        PayloadConfirmationEnvelopePayload(warning_id=wid, decision="cancel")
    )


async def _decline(tool_name: str) -> tuple[_FakeWS, BaseException]:
    """Dispatch ``tool_name`` and cancel its gate card; return the wire + error."""
    ws, state = _FakeWS(), SessionState(session_id=new_ulid())
    canceller = asyncio.create_task(_cancel_the_card())
    try:
        with pytest.raises(UserDeclinedError) as excinfo:
            await _invoke_tool_via_emitter(ws, state, tool_name, dict(PARAMS[tool_name]))
    finally:
        await canceller
    return ws, excinfo.value


async def _expire(tool_name: str) -> tuple[_FakeWS, BaseException]:
    """Dispatch ``tool_name`` and answer NOTHING; return the wire + error."""
    ws, state = _FakeWS(), SessionState(session_id=new_ulid())
    with pytest.raises(GateConfirmationTimeoutError) as excinfo:
        await _invoke_tool_via_emitter(ws, state, tool_name, dict(PARAMS[tool_name]))
    assert not _PENDING_CONFIRMATIONS, "an expired card left its future registered"
    return ws, excinfo.value


def _expired(tool_name: str, monkeypatch) -> tuple[_FakeWS, BaseException]:
    """``_expire`` with both deadline knobs tightened - the code-approval card
    reads its own, the other two the shared cap."""
    monkeypatch.setenv("TRID3NT_GATE_WAIT_CAP_S", "0.2")
    monkeypatch.setenv("TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S", "0.2")
    return asyncio.run(_expire(tool_name))


@pytest.mark.parametrize("tool_name,exc_type,code", DECLINES)
def test_a_decline_reaches_the_wire_with_its_own_code(
    tool_name: str, exc_type: type, code: str
) -> None:
    """The gate's error envelope carries the gate's OWN code, never a generic
    cancellation and never INTERNAL_ERROR."""
    ws, exc = asyncio.run(_decline(tool_name))
    assert isinstance(exc, exc_type) and exc.error_code == code
    errors = [e for e in ws.sent if e["type"] == "error"]
    assert errors, f"{tool_name} declined without an error envelope"
    assert errors[-1]["payload"]["error_code"] == code
    # The wire's code list is CLOSED, so the envelope only validates because the
    # code is a member of it.
    assert ErrorPayload(error_code=code, message="x").error_code == code


@pytest.mark.parametrize("tool_name,exc_type,code", DECLINES)
def test_a_declined_card_marks_its_step_cancelled(
    tool_name: str, exc_type: type, code: str
) -> None:
    """The tool still gets a step, and that step is cancelled - not failed, and
    not missing."""
    ws, _exc = asyncio.run(_decline(tool_name))
    states = [e for e in ws.sent if e["type"] == "pipeline-state"]
    assert states, f"{tool_name} declined without minting a step"
    steps = states[-1]["payload"]["steps"]
    mine = [s for s in steps if s["tool_name"] == tool_name]
    assert mine, steps
    assert mine[-1]["state"] == "cancelled", mine[-1]
    assert not any(s["state"] == "failed" for s in steps), steps


@pytest.mark.parametrize("tool_name,exc_type,code", DECLINES)
def test_the_model_reads_a_declined_result_not_an_error(
    tool_name: str, exc_type: type, code: str
) -> None:
    """The model's tool result says the user declined, names the card and what it
    asked, and tells the model not to apologise for a failure."""
    _ws, exc = asyncio.run(_decline(tool_name))
    summary = summarize_tool_result(tool_name, None, error=exc)
    assert summary["status"] == "declined"
    assert summary["error_code"] == code
    assert summary["retryable"] is False
    assert "declined" in summary["message"]
    assert "not an error" in summary["message"]
    # The card is named, so the model can say WHICH decision it is acknowledging.
    assert exc.decline_note in summary["message"]
    # Nothing here reads as a tool that broke.
    assert summary["status"] != "error" and "error_type" not in summary


def test_a_decline_does_not_consume_the_tools_retry_budget() -> None:
    """The breaker counts tool faults. A user's decline is not one, so a tool
    declined past the threshold is still dispatchable."""
    breaker = ToolCircuitBreaker(threshold=2, cooldown_s=600.0)
    for _ in range(5):
        breaker.record_failure("fetch_dem", SolverConfirmationCancelledError("fetch_dem"))
    assert breaker.is_tripped("fetch_dem") is False


@pytest.mark.parametrize("tool_name,exc_type,code", DECLINES)
def test_an_unanswered_card_carries_the_timeout_code_not_the_decline_code(
    tool_name: str, exc_type: type, code: str, monkeypatch
) -> None:
    """Nobody answered, so the wire and the raised error both say
    CONFIRMATION_TIMEOUT - never the gate's cancel code."""
    ws, exc = _expired(tool_name, monkeypatch)
    assert isinstance(exc, GateConfirmationTimeoutError)
    assert not isinstance(exc, UserDeclinedError)
    assert exc.error_code == "CONFIRMATION_TIMEOUT"
    errors = [e for e in ws.sent if e["type"] == "error"]
    assert errors, f"{tool_name} expired without an error envelope"
    assert errors[-1]["payload"]["error_code"] == "CONFIRMATION_TIMEOUT"
    assert not any(e["payload"]["error_code"] == code for e in errors)
    assert ErrorPayload(error_code="CONFIRMATION_TIMEOUT", message="x")


@pytest.mark.parametrize("tool_name,exc_type,code", DECLINES)
def test_an_unanswered_card_fails_its_step_where_a_decline_cancels_it(
    tool_name: str, exc_type: type, code: str, monkeypatch
) -> None:
    """The tool still gets a step; an expired card FAILS it, which is the state a
    decline never uses."""
    ws, _exc = _expired(tool_name, monkeypatch)
    states = [e for e in ws.sent if e["type"] == "pipeline-state"]
    assert states, f"{tool_name} expired without minting a step"
    steps = states[-1]["payload"]["steps"]
    mine = [s for s in steps if s["tool_name"] == tool_name]
    assert mine, steps
    assert mine[-1]["state"] == "failed", mine[-1]
    assert not any(s["state"] == "cancelled" for s in steps), steps


@pytest.mark.parametrize("tool_name,exc_type,code", DECLINES)
def test_the_model_reads_an_unanswered_card_not_a_decline(
    tool_name: str, exc_type: type, code: str, monkeypatch
) -> None:
    """The model's tool result says the card went unanswered and never says the
    user declined, so a deadline is not narrated as a decision."""
    _ws, exc = _expired(tool_name, monkeypatch)
    summary = summarize_tool_result(tool_name, None, error=exc)
    assert summary["status"] == "error"
    assert summary["error_code"] == "CONFIRMATION_TIMEOUT"
    assert summary["retryable"] is False
    assert "unanswered" in summary["message"]
    assert "declin" not in summary["message"].lower()
    # And the decline's own narration is the one it never borrows.
    declined = summarize_tool_result(tool_name, None, error=exc_type(tool_name))
    assert summary["message"] != declined["message"]
    assert declined["status"] == "declined"
