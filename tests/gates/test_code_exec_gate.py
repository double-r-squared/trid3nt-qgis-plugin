"""The code-exec approval gate in front of ``run_pyqgis``: fail-closed.

The dispatch site strips a model-supplied approval before the gate; the gate
emits the card with the verbatim code, injects the approval only on a
``proceed`` reply, refuses a cancel, and a card nobody answers expires into a
typed error with its pending entry dropped."""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from trid3nt_contracts import new_ulid
from trid3nt_contracts.payload_warning import PayloadConfirmationEnvelopePayload

from trid3nt_server.gates import confirm
from trid3nt_server.gates.pending import _PENDING_CONFIRMATIONS
from trid3nt_server.server import config
from trid3nt_server.server.dispatch import emitter as dispatch
from trid3nt_server.server.errors import CodeExecApprovalTimeoutError, CodeExecConfirmationCancelledError


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


class _FakeState:
    def __init__(self) -> None:
        self.session_id = new_ulid()


async def _reply_when_registered(decision: str) -> None:
    for _ in range(200):
        if _PENDING_CONFIRMATIONS:
            break
        await asyncio.sleep(0.005)
    cx_id = next(iter(_PENDING_CONFIRMATIONS))
    _PENDING_CONFIRMATIONS[cx_id][1].set_result(
        PayloadConfirmationEnvelopePayload(warning_id=cx_id, decision=decision)
    )


@pytest.mark.asyncio
async def test_approve_injects_confirmed_and_the_card_carries_the_code() -> None:
    ws, state = _FakeWS(), _FakeState()
    params = {"code": "result = 1 + 1", "rationale": "add"}
    replier = asyncio.create_task(_reply_when_registered("proceed"))
    should_run, effective = await confirm._gate_on_code_exec(ws, state, params)  # type: ignore[arg-type]
    await replier
    assert should_run is True
    assert effective["confirmed"] is True and effective["code_exec_id"]
    req = next(e for e in ws.sent if e.get("type") == "code-exec-request")
    assert req["payload"]["python_code"] == "result = 1 + 1"
    assert req["payload"]["rationale"] == "add"
    assert req["payload"]["code_exec_id"] == effective["code_exec_id"]
    assert "layer_refs" not in req["payload"]


@pytest.mark.asyncio
async def test_cancel_blocks_dispatch_and_leaks_nothing() -> None:
    ws, state = _FakeWS(), _FakeState()
    replier = asyncio.create_task(_reply_when_registered("cancel"))
    should_run, effective = await confirm._gate_on_code_exec(ws, state, {"code": "result = 1"})  # type: ignore[arg-type]
    await replier
    assert should_run is False
    assert "confirmed" not in effective and effective["code_exec_id"]
    err = next(e for e in ws.sent if e.get("type") == "error")
    assert err["payload"]["error_code"] == "USER_INPUT_CANCELLED"
    assert not _PENDING_CONFIRMATIONS
    exc = CodeExecConfirmationCancelledError("01TESTCXID")
    assert exc.error_code == "CODE_EXEC_CANCELLED" and exc.retryable is False


@pytest.mark.asyncio
async def test_an_unanswered_card_expires_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S", "0.2")
    ws, state = _FakeWS(), _FakeState()
    with pytest.raises(CodeExecApprovalTimeoutError) as excinfo:
        await confirm._gate_on_code_exec(ws, state, {"code": "result = 1"})  # type: ignore[arg-type]
    assert excinfo.value.error_code == "CODE_EXEC_APPROVAL_TIMEOUT"
    assert excinfo.value.retryable is False
    assert any(e.get("type") == "code-exec-request" for e in ws.sent)
    err = next(e for e in ws.sent if e.get("type") == "error")
    assert err["payload"]["error_code"] == "CONFIRMATION_TIMEOUT"
    assert not _PENDING_CONFIRMATIONS


@pytest.mark.asyncio
async def test_a_cancelled_wait_drops_its_entry() -> None:
    ws, state = _FakeWS(), _FakeState()
    task = asyncio.create_task(confirm._gate_on_code_exec(ws, state, {"code": "result = 1"}))  # type: ignore[arg-type]
    for _ in range(200):
        if _PENDING_CONFIRMATIONS:
            break
        await asyncio.sleep(0.005)
    assert _PENDING_CONFIRMATIONS
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not _PENDING_CONFIRMATIONS


def test_approval_window_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S", raising=False)
    assert config._code_exec_approval_timeout_s() == 180.0
    monkeypatch.setenv("TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S", "42.5")
    assert config._code_exec_approval_timeout_s() == 42.5
    for bad in ("abc", "0", "-5"):
        monkeypatch.setenv("TRID3NT_CODE_EXEC_APPROVAL_TIMEOUT_S", bad)
        assert config._code_exec_approval_timeout_s() == 180.0


def test_dispatch_strips_a_model_supplied_approval() -> None:
    src = inspect.getsource(dispatch._invoke_tool_via_emitter)
    assert 'if tool_name == "run_pyqgis":' in src
    assert 'params.pop("confirmed", None)' in src
    assert 'params.pop("code_exec_id", None)' in src
