"""Tests for ``code_exec_request`` + the server confirm-gate + the 0232 findings
fixes (job-0233, sprint-13 Stage 2).

Coverage (kickoff scope §4):
  - confirm-gate fails closed: the tool body refuses to run without confirmation
    AND the server-side gate blocks dispatch until approval / denies on cancel.
  - approved path runs the local sandbox end-to-end (benign numpy) -> status=ok.
  - blocked-egress script returns status="blocked" honestly.
  - timeout path -> status="timeout".
  - FINDING-1: oversized JSON-native string result -> truncated=true, valid JSON.
  - FINDING-2: a huge stdout never corrupts the parsed envelope (parse-then-bound).
  - function_response summary shape: compact, full payload stripped, no cost.

No network. No Gemini. Pure local-subprocess sandbox + in-process gate logic.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grace2_contracts import new_ulid
from grace2_contracts.payload_warning import PayloadConfirmationEnvelopePayload
from grace2_contracts.sandbox_contracts import CodeExecResultPayload

from grace2_agent.sandbox_runner import run_sandbox_local
from grace2_agent.tools.code_exec_tool import (
    CODE_EXEC_RESULT_KEY,
    CodeExecConfirmationRequired,
    build_code_exec_result_payload,
    code_exec_request,
    is_code_exec_result,
    summarize_code_exec_for_llm,
)


@pytest.fixture(autouse=True)
def _force_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs the local-subprocess sandbox + a short cap for the timeout
    scenario (keeps the suite fast; the executor's SIGALRM honors the env)."""
    monkeypatch.setenv("GRACE2_SANDBOX_LOCAL", "1")
    monkeypatch.setenv("MPLBACKEND", "Agg")
    monkeypatch.setenv("GRACE2_SANDBOX_TIMEOUT", "5")


# --------------------------------------------------------------------------- #
# Confirm-gate: tool body fails closed without approval
# --------------------------------------------------------------------------- #


def test_tool_body_refuses_without_confirmation() -> None:
    """``code_exec_request`` must not run the sandbox without ``confirmed=True``."""
    with pytest.raises(CodeExecConfirmationRequired) as exc:
        code_exec_request("result = 2 + 2")
    assert exc.value.error_code == "CODE_EXEC_CONFIRMATION_REQUIRED"
    assert exc.value.retryable is False


# --------------------------------------------------------------------------- #
# Confirm-gate: server-side gate blocks / approves (reuses payload-warning seam)
# --------------------------------------------------------------------------- #


class _FakeWS:
    """Minimal websocket capturing sent envelopes."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


class _FakeState:
    def __init__(self) -> None:
        self.session_id = new_ulid()


@pytest.mark.asyncio
async def test_server_gate_approve_injects_confirmed() -> None:
    """The server gate emits ``code-exec-request`` and, on a ``proceed`` reply,
    returns ``(True, params + confirmed=True + code_exec_id)``."""
    from grace2_agent import server

    ws = _FakeWS()
    state = _FakeState()
    params = {"python_code": "result = 1 + 1", "layer_refs": {}, "rationale": "add"}

    async def _approve_soon() -> None:
        # Wait for the gate to register its future, then complete it.
        for _ in range(200):
            if server._PENDING_CONFIRMATIONS:
                break
            await asyncio.sleep(0.005)
        cx_id = next(iter(server._PENDING_CONFIRMATIONS))
        fut = server._PENDING_CONFIRMATIONS[cx_id][1]
        fut.set_result(
            PayloadConfirmationEnvelopePayload(warning_id=cx_id, decision="proceed")
        )

    approver = asyncio.create_task(_approve_soon())
    should_run, effective = await server._gate_on_code_exec(ws, state, params)  # type: ignore[arg-type]
    await approver

    assert should_run is True
    assert effective["confirmed"] is True
    assert effective["code_exec_id"]
    # The request card was emitted with the verbatim code.
    req = next(e for e in ws.sent if e.get("type") == "code-exec-request")
    assert req["payload"]["python_code"] == "result = 1 + 1"
    assert req["payload"]["code_exec_id"] == effective["code_exec_id"]


@pytest.mark.asyncio
async def test_server_gate_cancel_blocks_dispatch() -> None:
    """A ``cancel`` reply makes the gate return ``(False, params)`` — fail-closed."""
    from grace2_agent import server

    ws = _FakeWS()
    state = _FakeState()
    params = {"python_code": "result = 1 + 1", "layer_refs": {}}

    async def _cancel_soon() -> None:
        for _ in range(200):
            if server._PENDING_CONFIRMATIONS:
                break
            await asyncio.sleep(0.005)
        cx_id = next(iter(server._PENDING_CONFIRMATIONS))
        server._PENDING_CONFIRMATIONS[cx_id][1].set_result(
            PayloadConfirmationEnvelopePayload(warning_id=cx_id, decision="cancel")
        )

    canceller = asyncio.create_task(_cancel_soon())
    should_run, _ = await server._gate_on_code_exec(ws, state, params)  # type: ignore[arg-type]
    await canceller

    assert should_run is False
    # An error envelope was emitted explaining the decline.
    err = next(e for e in ws.sent if e.get("type") == "error")
    assert err["payload"]["error_code"] == "USER_INPUT_CANCELLED"
    # Registry cleaned up on the deny path -- nothing leaks.
    assert not server._PENDING_CONFIRMATIONS
    # The call site resolves a deny as the typed, non-retryable cancel error
    # (the adapter harvests error_code/retryable into the function_response).
    exc = server.CodeExecConfirmationCancelledError("01TESTCXID")
    assert exc.error_code == "CODE_EXEC_CANCELLED"
    assert exc.retryable is False


# --------------------------------------------------------------------------- #
# Approval timeout (live-feedback 2026-07-22): unanswered card -> typed error,
# turn completes, registry cleaned up. The QGIS plugin had no handler for the
# code-exec-request envelope, so the F6 24h local gate wait hung the turn.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_server_gate_timeout_raises_typed_and_cleans_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No confirmation within GRACE2_CODE_EXEC_APPROVAL_TIMEOUT_S -> the gate
    raises ``CodeExecApprovalTimeoutError`` (typed, non-retryable) instead of
    parking forever, emits a contract-valid CONFIRMATION_TIMEOUT ws error, and
    pops the pending-confirmation registry entry."""
    from grace2_agent import server

    monkeypatch.setenv("GRACE2_CODE_EXEC_APPROVAL_TIMEOUT_S", "0.2")
    ws = _FakeWS()
    state = _FakeState()
    params = {"python_code": "result = 1 + 1", "layer_refs": {}}

    with pytest.raises(server.CodeExecApprovalTimeoutError) as excinfo:
        await server._gate_on_code_exec(ws, state, params)  # type: ignore[arg-type]

    exc = excinfo.value
    # Typed function-response surface: the adapter harvests these attrs.
    assert exc.error_code == "CODE_EXEC_APPROVAL_TIMEOUT"
    assert exc.retryable is False
    assert "not answered" in str(exc)
    # The request card WAS emitted before the wait.
    assert any(e.get("type") == "code-exec-request" for e in ws.sent)
    # WS surface: error_code is the closed A.6 ErrorCode Literal, so the wire
    # code stays the contract-valid CONFIRMATION_TIMEOUT.
    err = next(e for e in ws.sent if e.get("type") == "error")
    assert err["payload"]["error_code"] == "CONFIRMATION_TIMEOUT"
    assert "not answered" in err["payload"]["message"]
    # Registry cleaned up on timeout -- nothing leaks.
    assert not server._PENDING_CONFIRMATIONS


@pytest.mark.asyncio
async def test_server_gate_cleanup_on_task_cancel() -> None:
    """Cancelling the gate task (session close / turn cancel) pops the pending
    registry entry via the finally -- no leaked futures."""
    from grace2_agent import server

    ws = _FakeWS()
    state = _FakeState()
    params = {"python_code": "result = 1 + 1", "layer_refs": {}}

    task = asyncio.create_task(
        server._gate_on_code_exec(ws, state, params)  # type: ignore[arg-type]
    )
    # Wait for the gate to register its pending future.
    for _ in range(200):
        if server._PENDING_CONFIRMATIONS:
            break
        await asyncio.sleep(0.005)
    assert server._PENDING_CONFIRMATIONS

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not server._PENDING_CONFIRMATIONS


def test_approval_timeout_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default 180s; env override honored; malformed / non-positive -> default."""
    from grace2_agent import server

    monkeypatch.delenv("GRACE2_CODE_EXEC_APPROVAL_TIMEOUT_S", raising=False)
    assert server._code_exec_approval_timeout_s() == 180.0

    monkeypatch.setenv("GRACE2_CODE_EXEC_APPROVAL_TIMEOUT_S", "42.5")
    assert server._code_exec_approval_timeout_s() == 42.5

    monkeypatch.setenv("GRACE2_CODE_EXEC_APPROVAL_TIMEOUT_S", "abc")
    assert server._code_exec_approval_timeout_s() == 180.0

    monkeypatch.setenv("GRACE2_CODE_EXEC_APPROVAL_TIMEOUT_S", "0")
    assert server._code_exec_approval_timeout_s() == 180.0

    monkeypatch.setenv("GRACE2_CODE_EXEC_APPROVAL_TIMEOUT_S", "-5")
    assert server._code_exec_approval_timeout_s() == 180.0


# --------------------------------------------------------------------------- #
# Approved path: benign numpy end-to-end through the local sandbox
# --------------------------------------------------------------------------- #


def test_approved_benign_numpy_runs_end_to_end() -> None:
    out = code_exec_request(
        "import numpy as np\nresult = float(np.mean([10, 20, 30, 40]))",
        confirmed=True,
        code_exec_id=new_ulid(),
    )
    assert out["status"] == "ok"
    assert out["result"]["kind"] == "json"
    assert out["result"]["value"] == 25.0
    assert is_code_exec_result(out)
    payload = out[CODE_EXEC_RESULT_KEY]
    assert payload["envelope_type"] == "code-exec-result"
    assert payload["status"] == "ok"
    # The full payload validates against the wire contract.
    CodeExecResultPayload.model_validate(payload)


# --------------------------------------------------------------------------- #
# Blocked egress: honest status="blocked"
# --------------------------------------------------------------------------- #


def test_approved_blocked_egress_reports_blocked() -> None:
    code = (
        "import urllib.request\n"
        "result = urllib.request.urlopen('http://example.com', timeout=5).status\n"
    )
    out = code_exec_request(code, confirmed=True, code_exec_id=new_ulid())
    assert out["status"] == "blocked", out
    # The honest reason is surfaced (never dressed up as ok).
    assert "block" in (out["stderr_tail"] or "").lower()
    assert out[CODE_EXEC_RESULT_KEY]["status"] == "blocked"


# --------------------------------------------------------------------------- #
# Timeout: honest status="timeout"
# --------------------------------------------------------------------------- #


def test_approved_timeout_reports_timeout() -> None:
    out = code_exec_request(
        "while True:\n    pass\n", confirmed=True, code_exec_id=new_ulid()
    )
    assert out["status"] == "timeout", out
    assert out[CODE_EXEC_RESULT_KEY]["status"] == "timeout"
    # duration reflects the cap was consumed.
    assert out["duration_s"] >= 0


# --------------------------------------------------------------------------- #
# FINDING-1: oversized JSON-native string result -> truncated, valid JSON
# --------------------------------------------------------------------------- #


def test_finding1_oversized_string_result_truncated_honestly() -> None:
    out = code_exec_request(
        'result = "x" * 9_000_000', confirmed=True, code_exec_id=new_ulid()
    )
    assert out["status"] == "ok"
    assert out["result"]["truncated"] is True
    assert "original_bytes" in out["result"]
    # The whole tool result round-trips as valid JSON (never a corrupt slice).
    assert json.loads(json.dumps(out))["status"] == "ok"
    # The bounded string stays under the 2 MiB cap (+ small marker slack).
    assert len(out["result"]["value"]) < 2_200_000
    # And the honesty flag bubbles to the wire payload.
    assert out[CODE_EXEC_RESULT_KEY]["truncated"] is True


def test_finding1_oversized_container_result_too_large_descriptor() -> None:
    out = code_exec_request(
        "result = list(range(2_000_000))", confirmed=True, code_exec_id=new_ulid()
    )
    assert out["status"] == "ok"
    assert out["result"]["kind"] == "too_large"
    assert out["result"]["truncated"] is True
    assert json.loads(json.dumps(out))["result"]["kind"] == "too_large"


# --------------------------------------------------------------------------- #
# FINDING-2: huge stdout never corrupts the parsed envelope (parse-then-bound)
# --------------------------------------------------------------------------- #


def test_finding2_huge_stdout_envelope_stays_valid() -> None:
    # Print far more than MAX_OUTPUT_CHARS; the executor caps stdout, the host
    # runner parses the FULL line then bounds it — the result must be intact.
    code = 'print("A" * 5_000_000)\nresult = 42\n'
    env = run_sandbox_local(code)
    assert env["status"] == "ok", env
    assert env["result"] == {"kind": "json", "value": 42}
    assert env["stdout_truncated"] is True
    # The envelope round-trips as valid JSON (the FINDING-2 fix: no blind slice).
    assert json.loads(json.dumps(env))["result"]["value"] == 42


def test_finding2_host_side_bound_envelope_marks_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Directly exercise the host-side parse-then-bound on an oversized field."""
    from grace2_agent import sandbox_runner as sr

    monkeypatch.setattr(sr, "MAX_ENVELOPE_FIELD_CHARS", 100)
    env = {
        "status": "ok",
        "stdout": "Z" * 5000,
        "stderr": "",
        "result": {"kind": "json", "value": 7},
        "error": None,
    }
    bounded = sr._bound_envelope(dict(env))
    assert bounded["stdout_truncated"] is True
    assert len(bounded["stdout"]) < 5000
    assert "truncated" in bounded["stdout"]
    # result is untouched and the dict is valid JSON.
    assert bounded["result"] == {"kind": "json", "value": 7}
    assert json.loads(json.dumps(bounded))["result"]["value"] == 7


# --------------------------------------------------------------------------- #
# function_response summary shape — compact, no full payload, no cost
# --------------------------------------------------------------------------- #


def test_summary_shape_is_compact_and_stripped() -> None:
    payload = CodeExecResultPayload(
        code_exec_id=new_ulid(),
        status="ok",
        stdout_tail="hello\n",
        stderr_tail="",
        result={"kind": "json", "value": 3.14},
        truncated=False,
        duration_s=0.02,
    )
    summary = summarize_code_exec_for_llm(payload)
    assert summary["status"] == "ok"
    assert summary["result"] == {"kind": "json", "value": 3.14}
    assert "duration_s" in summary
    # No cost field anywhere (Invariant 9).
    for key in summary:
        assert "cost" not in key.lower()
        assert "price" not in key.lower()
        assert "dollar" not in key.lower()


def test_summary_carries_full_payload_under_private_key() -> None:
    payload = CodeExecResultPayload(
        code_exec_id="01J0000000000000000000CXEC",
        status="error",
        stdout_tail="",
        stderr_tail="Traceback...",
        result=None,
        truncated=False,
        duration_s=0.01,
    )
    summary = summarize_code_exec_for_llm(payload)
    summary[CODE_EXEC_RESULT_KEY] = payload.model_dump(mode="json")
    assert is_code_exec_result(summary)
    # The error reason is surfaced for honest narration.
    assert "Traceback" in summary["stderr_tail"]


def test_adapter_strips_full_payload_from_function_response() -> None:
    """``summarize_tool_result`` must strip ``_code_exec_result`` so Gemini sees
    only the compact summary, not the larger wire payload."""
    from grace2_agent.adapter import summarize_tool_result

    payload = CodeExecResultPayload(
        code_exec_id=new_ulid(),
        status="ok",
        stdout_tail="x" * 10000,
        stderr_tail="",
        result={"kind": "json", "value": 1},
        truncated=False,
        duration_s=0.01,
    )
    tool_result = summarize_code_exec_for_llm(payload)
    tool_result[CODE_EXEC_RESULT_KEY] = payload.model_dump(mode="json")
    fr = summarize_tool_result("code_exec_request", tool_result)
    encoded = json.dumps(fr)
    assert CODE_EXEC_RESULT_KEY not in encoded
    assert fr["status"] == "ok"


# --------------------------------------------------------------------------- #
# build_code_exec_result_payload mapping
# --------------------------------------------------------------------------- #


def test_build_payload_derives_truncated_from_result_marker() -> None:
    env = {
        "status": "ok",
        "stdout": "",
        "stderr": "",
        "result": {"kind": "json", "value": "x", "truncated": True, "original_bytes": 9_000_000},
        "stdout_truncated": False,
        "stderr_truncated": False,
        "wallclock_cap_seconds": 60,
    }
    payload = build_code_exec_result_payload("01J0000000000000000000CXEC", env)
    assert payload.truncated is True
    assert payload.status == "ok"


def test_dispatch_strips_llm_supplied_confirmed_for_code_exec() -> None:
    """Invariant 9 (job-0301): the dispatch site STRIPS a model-supplied
    confirmed/code_exec_id for code_exec_request BEFORE gating, so a model that
    passes confirmed=True cannot self-approve and skip the user gate. The prior
    `and not params.get("confirmed")` condition allowed exactly that bypass
    (the params are not underscore-hidden from the model's tool schema)."""
    import inspect

    import grace2_agent.server as server_mod

    src = inspect.getsource(server_mod._invoke_tool_via_emitter)
    assert 'if tool_name == "code_exec_request":' in src
    assert 'params.pop("confirmed", None)' in src
    assert 'params.pop("code_exec_id", None)' in src
    # The bypass-prone guard must be gone.
    assert 'code_exec_request" and not params.get("confirmed")' not in src
