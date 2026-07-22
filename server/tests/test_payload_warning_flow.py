"""Tests for the agent-side payload-warning gate (job-0127).

Exercises ``_maybe_gate_on_payload_warning`` + ``_invoke_tool_via_emitter``
end-to-end with a ``MockWebSocket`` collecting wire envelopes:

- small payload (1 MB) → no warning, direct dispatch
- medium payload (50 MB) → warning emitted, agent pauses
- confirmation:proceed → dispatch fires after the confirmation
- confirmation:cancel → dispatch skipped, USER_INPUT_CANCELLED error returned
- confirmation:narrow_scope → revised_args used in the dispatch
- hard cap (>250 MB) → warning omits ``proceed`` from options
- audit log captures every decision

The gate calls into ``TOOL_REGISTRY`` so we register a couple of dummy
tools per-test inside the autouse fixture that snapshots / restores the
real registry. The tools' ``__module__`` is overridden so the estimator
lookup resolves to the test module itself (where the estimator is defined
at module scope below).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from trid3nt_server import server
from trid3nt_server.server import (
    SessionState,
    _invoke_tool_via_emitter,
    _maybe_gate_on_payload_warning,
)
from trid3nt_server.tools import (
    TOOL_REGISTRY,
    RegisteredTool,
    clear_registry_for_tests,
)
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.payload_warning import (
    PayloadConfirmationEnvelopePayload,
)
from trid3nt_contracts.tool_registry import AtomicToolMetadata


# --------------------------------------------------------------------------- #
# Module-level estimator + tool body — referenced by NAME (the metadata
# carries the identifier "estimate_payload_mb" + "estimate_payload_mb_huge").
# --------------------------------------------------------------------------- #


def estimate_payload_mb(**kwargs: Any) -> float:
    """Dummy estimator: read ``mb`` from kwargs."""
    return float(kwargs.get("mb", 0.0))


def estimate_payload_mb_huge(**kwargs: Any) -> float:
    """Always return a value past the hard cap (300 MB > 250 default)."""
    return 300.0


def _dummy_tool(**kwargs: Any) -> dict:
    """Identity-style tool: echoes its kwargs as the result."""
    return {"received": dict(kwargs)}


# --------------------------------------------------------------------------- #
# MockWebSocket — collects wire envelopes for assertion.
# --------------------------------------------------------------------------- #


class MockWebSocket:
    """Collects every envelope ``send`` would have written to the wire."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: Any) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            self.sent.append(json.loads(raw))
        else:
            self.sent.append(raw)


# --------------------------------------------------------------------------- #
# Registry snapshot fixture (shared with sibling Wave 1.5 tests).
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _snapshot_and_restore_registry() -> None:
    snapshot = dict(TOOL_REGISTRY)
    clear_registry_for_tests()
    try:
        yield
    finally:
        clear_registry_for_tests()
        TOOL_REGISTRY.update(snapshot)


def _register_dummy(
    name: str,
    *,
    estimator_name: str | None = "estimate_payload_mb",
) -> None:
    """Insert a ``RegisteredTool`` whose module resolves to THIS test module.

    The gate resolves estimators by importing ``RegisteredTool.module`` and
    looking up the named attribute. By pointing at this module we get the
    ``estimate_payload_mb`` defined at the top of the file.
    """
    meta = AtomicToolMetadata(
        name=name,
        ttl_class="dynamic-1h",
        source_class="dummy",
        cacheable=True,
        payload_mb_estimator_name=estimator_name,
    )
    TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=_dummy_tool, module=__name__
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_no_estimator_means_no_gate() -> None:
    """A tool without ``payload_mb_estimator_name`` skips the gate entirely."""
    _register_dummy("no_estimator_tool", estimator_name=None)
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    should, params = asyncio.run(
        _maybe_gate_on_payload_warning(ws, state, "no_estimator_tool", {"mb": 9999.0})
    )
    assert should is True
    assert params == {"mb": 9999.0}
    assert ws.sent == []
    assert state.payload_warning_audit_log == []


def test_small_payload_skips_gate() -> None:
    """1 MB estimate is well below the 25 MB default; no warning emitted."""
    _register_dummy("small_payload_tool")
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    should, params = asyncio.run(
        _maybe_gate_on_payload_warning(ws, state, "small_payload_tool", {"mb": 1.0})
    )
    assert should is True
    assert params == {"mb": 1.0}
    assert ws.sent == []
    assert state.payload_warning_audit_log == []


def test_medium_payload_emits_warning_and_pauses() -> None:
    """50 MB estimate crosses the warning threshold but is below the hard cap."""
    _register_dummy("medium_payload_tool")
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())

    async def _run() -> tuple[bool, dict]:
        gate_task = asyncio.create_task(
            _maybe_gate_on_payload_warning(
                ws, state, "medium_payload_tool", {"mb": 50.0}
            )
        )
        # Let the gate emit the warning + register its future.
        await asyncio.sleep(0)
        # The warning envelope was sent.
        assert any(e["type"] == "tool-payload-warning" for e in ws.sent)
        warning_env = next(
            e for e in ws.sent if e["type"] == "tool-payload-warning"
        )
        payload = warning_env["payload"]
        assert payload["tool_name"] == "medium_payload_tool"
        assert payload["estimated_mb"] == 50.0
        assert payload["threshold_mb"] == 25.0
        assert "proceed" in payload["options"]
        assert "cancel" in payload["options"]
        assert "narrow_scope" in payload["options"]
        # Confirm with proceed.
        wid = payload["warning_id"]
        fut = server._PENDING_CONFIRMATIONS[wid][1]
        fut.set_result(
            PayloadConfirmationEnvelopePayload(
                warning_id=wid, decision="proceed"
            )
        )
        return await gate_task

    should, params = asyncio.run(_run())
    assert should is True
    assert params == {"mb": 50.0}
    # Audit log captured emission + decision.
    assert len(state.payload_warning_audit_log) == 1
    entry = state.payload_warning_audit_log[0]
    assert entry["tool_name"] == "medium_payload_tool"
    assert entry["estimated_mb"] == 50.0
    assert entry["decision"] == "proceed"


def test_cancel_decision_skips_dispatch() -> None:
    """``decision=cancel`` → gate returns ``(False, ...)`` + error frame."""
    _register_dummy("cancel_tool")
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())

    async def _run() -> tuple[bool, dict]:
        gate_task = asyncio.create_task(
            _maybe_gate_on_payload_warning(
                ws, state, "cancel_tool", {"mb": 50.0}
            )
        )
        await asyncio.sleep(0)
        warning_env = next(
            e for e in ws.sent if e["type"] == "tool-payload-warning"
        )
        wid = warning_env["payload"]["warning_id"]
        fut = server._PENDING_CONFIRMATIONS[wid][1]
        fut.set_result(
            PayloadConfirmationEnvelopePayload(
                warning_id=wid, decision="cancel"
            )
        )
        return await gate_task

    should, _params = asyncio.run(_run())
    assert should is False
    # The gate sent an error envelope with USER_INPUT_CANCELLED.
    errors = [e for e in ws.sent if e["type"] == "error"]
    assert len(errors) == 1
    assert errors[0]["payload"]["error_code"] == "USER_INPUT_CANCELLED"
    assert "cancel_tool" in errors[0]["payload"]["message"]
    assert state.payload_warning_audit_log[0]["decision"] == "cancel"


def test_narrow_scope_decision_uses_revised_args() -> None:
    """``decision=narrow_scope`` → returned params are the revised dict."""
    _register_dummy("narrow_tool")
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())

    async def _run() -> tuple[bool, dict]:
        gate_task = asyncio.create_task(
            _maybe_gate_on_payload_warning(
                ws, state, "narrow_tool", {"mb": 50.0, "extra": "drop"}
            )
        )
        await asyncio.sleep(0)
        warning_env = next(
            e for e in ws.sent if e["type"] == "tool-payload-warning"
        )
        wid = warning_env["payload"]["warning_id"]
        fut = server._PENDING_CONFIRMATIONS[wid][1]
        fut.set_result(
            PayloadConfirmationEnvelopePayload(
                warning_id=wid,
                decision="narrow_scope",
                revised_args={"mb": 5.0},
            )
        )
        return await gate_task

    should, params = asyncio.run(_run())
    assert should is True
    assert params == {"mb": 5.0}
    assert state.payload_warning_audit_log[0]["decision"] == "narrow_scope"


def test_hard_cap_warning_omits_proceed_option() -> None:
    """At >250 MB the warning options exclude ``proceed`` (cancel / narrow only)."""
    # Use the huge estimator so the gate sees 300 MB regardless of args.
    _register_dummy("hard_cap_tool", estimator_name="estimate_payload_mb_huge")
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())

    async def _run() -> None:
        gate_task = asyncio.create_task(
            _maybe_gate_on_payload_warning(
                ws, state, "hard_cap_tool", {}
            )
        )
        await asyncio.sleep(0)
        warning_env = next(
            e for e in ws.sent if e["type"] == "tool-payload-warning"
        )
        payload = warning_env["payload"]
        assert payload["estimated_mb"] == 300.0
        assert "proceed" not in payload["options"]
        assert "cancel" in payload["options"]
        assert "narrow_scope" in payload["options"]
        # Cancel to clean up.
        wid = payload["warning_id"]
        fut = server._PENDING_CONFIRMATIONS[wid][1]
        fut.set_result(
            PayloadConfirmationEnvelopePayload(
                warning_id=wid, decision="cancel"
            )
        )
        await gate_task

    asyncio.run(_run())


def test_hard_cap_rejects_proceed_decision() -> None:
    """Defense-in-depth: if a misbehaving client returns ``proceed`` past the
    hard cap, the gate refuses with TOOL_PARAMS_INVALID."""
    _register_dummy("rude_client_tool", estimator_name="estimate_payload_mb_huge")
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())

    async def _run() -> tuple[bool, dict]:
        gate_task = asyncio.create_task(
            _maybe_gate_on_payload_warning(
                ws, state, "rude_client_tool", {}
            )
        )
        await asyncio.sleep(0)
        warning_env = next(
            e for e in ws.sent if e["type"] == "tool-payload-warning"
        )
        wid = warning_env["payload"]["warning_id"]
        fut = server._PENDING_CONFIRMATIONS[wid][1]
        fut.set_result(
            PayloadConfirmationEnvelopePayload(
                warning_id=wid, decision="proceed"
            )
        )
        return await gate_task

    should, _ = asyncio.run(_run())
    assert should is False
    errors = [e for e in ws.sent if e["type"] == "error"]
    assert any(
        e["payload"]["error_code"] == "TOOL_PARAMS_INVALID" for e in errors
    )


def test_audit_log_records_every_event() -> None:
    """Two consecutive gated dispatches produce two audit log entries."""
    _register_dummy("audit_tool_a")
    _register_dummy("audit_tool_b")
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())

    async def _gate_and_confirm(tool: str, decision: str, **revised: Any) -> None:
        kwargs: dict[str, Any] = {"warning_id": "", "decision": decision}
        gate_task = asyncio.create_task(
            _maybe_gate_on_payload_warning(ws, state, tool, {"mb": 40.0})
        )
        await asyncio.sleep(0)
        warning_env = next(
            e for e in ws.sent
            if e["type"] == "tool-payload-warning"
            and e["payload"]["tool_name"] == tool
        )
        wid = warning_env["payload"]["warning_id"]
        kwargs["warning_id"] = wid
        if decision == "narrow_scope":
            kwargs["revised_args"] = revised or {}
        fut = server._PENDING_CONFIRMATIONS[wid][1]
        fut.set_result(PayloadConfirmationEnvelopePayload(**kwargs))
        await gate_task

    async def _run() -> None:
        await _gate_and_confirm("audit_tool_a", "proceed")
        await _gate_and_confirm("audit_tool_b", "cancel")

    asyncio.run(_run())
    assert len(state.payload_warning_audit_log) == 2
    decisions = {e["tool_name"]: e["decision"] for e in state.payload_warning_audit_log}
    assert decisions == {"audit_tool_a": "proceed", "audit_tool_b": "cancel"}


def test_estimator_exception_falls_through_to_dispatch() -> None:
    """An estimator that raises must not break the tool — gate skips."""
    def _broken_estimator(**kwargs: Any) -> float:
        raise RuntimeError("boom")

    # Inject the broken estimator into THIS module's namespace.
    globals()["estimate_payload_mb_broken"] = _broken_estimator
    try:
        _register_dummy("broken_tool", estimator_name="estimate_payload_mb_broken")
        ws = MockWebSocket()
        state = SessionState(session_id=new_ulid())
        should, params = asyncio.run(
            _maybe_gate_on_payload_warning(ws, state, "broken_tool", {"mb": 1000.0})
        )
        assert should is True
        assert params == {"mb": 1000.0}
        assert ws.sent == []
    finally:
        globals().pop("estimate_payload_mb_broken", None)


def test_missing_estimator_callable_falls_through_to_dispatch() -> None:
    """An estimator name that doesn't resolve to a callable: gate skips."""
    _register_dummy("misnamed_tool", estimator_name="nonexistent_estimator_xyz")
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    should, params = asyncio.run(
        _maybe_gate_on_payload_warning(ws, state, "misnamed_tool", {"mb": 1000.0})
    )
    assert should is True
    assert params == {"mb": 1000.0}
    assert ws.sent == []


def test_threshold_env_override() -> None:
    """``TRID3NT_PAYLOAD_WARNING_MB`` env var lowers the threshold below 25 MB."""
    import os
    _register_dummy("env_tool")
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())
    saved = os.environ.get("TRID3NT_PAYLOAD_WARNING_MB")
    os.environ["TRID3NT_PAYLOAD_WARNING_MB"] = "5"
    try:
        async def _run() -> None:
            gate_task = asyncio.create_task(
                _maybe_gate_on_payload_warning(ws, state, "env_tool", {"mb": 10.0})
            )
            await asyncio.sleep(0)
            warning_env = next(
                e for e in ws.sent if e["type"] == "tool-payload-warning"
            )
            assert warning_env["payload"]["estimated_mb"] == 10.0
            assert warning_env["payload"]["threshold_mb"] == 5.0
            wid = warning_env["payload"]["warning_id"]
            fut = server._PENDING_CONFIRMATIONS[wid][1]
            fut.set_result(
                PayloadConfirmationEnvelopePayload(
                    warning_id=wid, decision="cancel"
                )
            )
            await gate_task
        asyncio.run(_run())
    finally:
        if saved is None:
            os.environ.pop("TRID3NT_PAYLOAD_WARNING_MB", None)
        else:
            os.environ["TRID3NT_PAYLOAD_WARNING_MB"] = saved


# --------------------------------------------------------------------------- #
# Integration: _invoke_tool_via_emitter end-to-end with the gate.
# --------------------------------------------------------------------------- #


def test_invoke_tool_via_emitter_dispatches_after_proceed() -> None:
    """End-to-end: warning → proceed → tool runs and returns its result."""
    _register_dummy("integration_tool")
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())

    async def _run() -> Any:
        invoke_task = asyncio.create_task(
            _invoke_tool_via_emitter(
                ws, state, "integration_tool", {"mb": 50.0, "payload": "ok"}
            )
        )
        # Yield until the warning envelope is queued.
        for _ in range(50):
            await asyncio.sleep(0)
            if any(e["type"] == "tool-payload-warning" for e in ws.sent):
                break
        warning_env = next(
            e for e in ws.sent if e["type"] == "tool-payload-warning"
        )
        wid = warning_env["payload"]["warning_id"]
        fut = server._PENDING_CONFIRMATIONS[wid][1]
        fut.set_result(
            PayloadConfirmationEnvelopePayload(
                warning_id=wid, decision="proceed"
            )
        )
        return await invoke_task

    result = asyncio.run(_run())
    # Tool ran with the original args.
    assert result == {"received": {"mb": 50.0, "payload": "ok"}}
    # Audit log captured the decision.
    assert state.payload_warning_audit_log[0]["decision"] == "proceed"


def test_invoke_tool_via_emitter_skips_after_cancel() -> None:
    """End-to-end: warning → cancel → tool does NOT run, raises PayloadWarningCancelledError.

    B-rev: previously this returned None (opaque to Gemini); now it raises
    PayloadWarningCancelledError so the multi-turn loop feeds a structured
    error envelope back to Gemini (error_code=PAYLOAD_WARNING_CANCELLED,
    retryable=False).
    """
    from trid3nt_server.server import PayloadWarningCancelledError

    _register_dummy("integration_cancel_tool")
    ws = MockWebSocket()
    state = SessionState(session_id=new_ulid())

    raised: list[BaseException] = []

    async def _run() -> Any:
        invoke_task = asyncio.create_task(
            _invoke_tool_via_emitter(
                ws, state, "integration_cancel_tool", {"mb": 50.0}
            )
        )
        for _ in range(50):
            await asyncio.sleep(0)
            if any(e["type"] == "tool-payload-warning" for e in ws.sent):
                break
        warning_env = next(
            e for e in ws.sent if e["type"] == "tool-payload-warning"
        )
        wid = warning_env["payload"]["warning_id"]
        fut = server._PENDING_CONFIRMATIONS[wid][1]
        fut.set_result(
            PayloadConfirmationEnvelopePayload(
                warning_id=wid, decision="cancel"
            )
        )
        try:
            return await invoke_task
        except PayloadWarningCancelledError as exc:
            raised.append(exc)
            return None

    asyncio.run(_run())
    # B-rev: must have raised PayloadWarningCancelledError, not returned None.
    assert len(raised) == 1, "expected PayloadWarningCancelledError to be raised"
    assert raised[0].error_code == "PAYLOAD_WARNING_CANCELLED"
    assert raised[0].retryable is False
    assert "integration_cancel_tool" in str(raised[0])
    # No tool-call-start envelope: the emitter never opened a pipeline.
    assert not any(e["type"] == "tool-call-start" for e in ws.sent)
    # USER_INPUT_CANCELLED frame WAS emitted by the gate (pre-raise side effect).
    assert any(
        e["type"] == "error"
        and e["payload"]["error_code"] == "USER_INPUT_CANCELLED"
        for e in ws.sent
    )
