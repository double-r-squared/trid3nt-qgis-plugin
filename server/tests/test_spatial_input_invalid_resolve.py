"""FR-WC-16 untagged-barrier mismatch — SERVER half (the critical correctness fix).

An inbound ``spatial-input-response`` whose drawn ``FeatureCollection`` fails
structural validation (e.g. a ``role=="barrier"`` feature missing
``barrier_type``) raises a pydantic ``ValidationError`` in the WS handler.

BEFORE the fix: the handler sent ``TOOL_PARAMS_INVALID`` and ``continue``d
WITHOUT resolving the pending ``request_spatial_input`` future, so the paused
turn hung until ``default_timeout_seconds`` (~300s) then degraded to
``SPATIAL_INPUT_TIMEOUT`` — a confusing, slow, wrong terminal state.

AFTER the fix: the handler ALSO fails the pending future eagerly via
``_fail_pending_spatial_input``, so the awaiting tool wakes IN-BAND with a typed
``SPATIAL_INPUT_BAD_BARRIER_TYPE`` error PROMPTLY — never via the timeout path.

These tests prove the future is resolved promptly (asserting the wall-clock
budget is FAR under the read TTL) and that the honesty floor holds (a typed
error result, never a silent success).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from pydantic import ValidationError

from trid3nt_server import server
from trid3nt_server.server import (
    SessionState,
    SpatialInputInvalidResponseError,
    _emit_spatial_input_and_wait,
    _fail_pending_spatial_input,
    _handle_request_spatial_input,
    _spatial_response_to_result,
)
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.ws import (
    SpatialInputRequestPayload,
    SpatialInputResponsePayload,
)


# --------------------------------------------------------------------------- #
# A "real" untagged-barrier reply: the exact malformed shape the client can
# send — a role=='barrier' LineString with NO barrier_type. This is what makes
# SpatialInputResponsePayload.model_validate raise in the WS handler.
# --------------------------------------------------------------------------- #


def _untagged_barrier_payload_dict(request_id: str) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "geometry_type": "vector_draw",
        "features": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"role": "barrier"},  # <-- NO barrier_type
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[-85.305, 35.045], [-85.305, 35.055]],
                    },
                }
            ],
        },
    }


class _MockWebSocket:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, raw: Any) -> None:
        self.sent.append(raw)


# --------------------------------------------------------------------------- #
# Sanity: the untagged barrier really DOES raise in model_validate (so the WS
# handler's except-ValidationError branch is the one that fires).
# --------------------------------------------------------------------------- #


def test_untagged_barrier_fails_model_validate():
    pd = _untagged_barrier_payload_dict(new_ulid())
    with pytest.raises(ValidationError) as ei:
        SpatialInputResponsePayload.model_validate(pd)
    assert "barrier_type" in str(ei.value)


# --------------------------------------------------------------------------- #
# THE FIX: an invalid response for a PENDING request resolves the awaiting
# future PROMPTLY with a typed error — NOT via the ~300s timeout path.
# --------------------------------------------------------------------------- #


def test_invalid_response_resolves_pending_future_promptly_not_via_timeout():
    """The awaiting ``_emit_spatial_input_and_wait`` wakes IN-BAND with a typed
    error the instant the invalid response is failed — wall-clock budget FAR
    under the 300s read TTL (proving we did NOT drain default_timeout_seconds)."""

    async def _run() -> tuple[SpatialInputInvalidResponseError, float]:
        ws = _MockWebSocket()
        state = SessionState(session_id=new_ulid())
        req_id = new_ulid()
        payload = SpatialInputRequestPayload(
            request_id=req_id,
            mode="vector_draw",
            title="Draw the flood walls",
            description="Outline the AOI and place walls / flap gates.",
            # A LONG TTL: if the fix regressed and we fell through to the
            # timeout path, this test would take ~300s. We assert it does NOT.
            default_timeout_seconds=300,
        )
        handler = asyncio.create_task(
            _emit_spatial_input_and_wait(ws, state, payload)
        )
        # Let the request emit + the pending future register.
        for _ in range(500):
            await asyncio.sleep(0)
            if server._PENDING_SPATIAL_INPUTS.get(req_id) is not None:
                break
        assert server._PENDING_SPATIAL_INPUTS.get(req_id) is not None, (
            "the pending spatial-input future must be registered"
        )

        # Simulate exactly what the WS handler now does on a malformed reply:
        # model_validate raises, we extract request_id from the raw payload,
        # then FAIL the pending future eagerly.
        pd = _untagged_barrier_payload_dict(req_id)
        try:
            SpatialInputResponsePayload.model_validate(pd)
            pytest.fail("untagged barrier should have failed validation")
        except ValidationError as ve:
            err_msg = ve.errors()[0]["msg"]

        t0 = time.monotonic()
        failed = _fail_pending_spatial_input(
            state.session_id,
            req_id,
            "SPATIAL_INPUT_BAD_BARRIER_TYPE",
            err_msg,
        )
        assert failed is True, "a live pending future must be failed"

        # The awaiting coroutine must now raise the typed error PROMPTLY.
        with pytest.raises(SpatialInputInvalidResponseError) as ei:
            await asyncio.wait_for(handler, timeout=5.0)
        elapsed = time.monotonic() - t0
        return ei.value, elapsed

    exc, elapsed = asyncio.run(_run())
    assert exc.error_code == "SPATIAL_INPUT_BAD_BARRIER_TYPE"
    assert "barrier_type" in exc.error_message
    # PROMPT: nowhere near the 300s timeout TTL. Generous CI budget of 5s; the
    # real resolve is sub-millisecond. The point is "NOT the ~300s timeout path".
    assert elapsed < 5.0, (
        f"future resolved in {elapsed:.3f}s — must be prompt, not the "
        f"~300s timeout path"
    )
    # And the registry is cleaned up (no leak).
    assert not server._PENDING_SPATIAL_INPUTS


def test_handle_request_spatial_input_returns_typed_error_on_invalid_reply():
    """End-to-end through the tool entry point: a pending request_spatial_input
    turn, fed an invalid reply, returns the TYPED error result the LLM reads
    (honesty floor — never a silent success / fabricated barriers), and does so
    PROMPTLY (not after default_timeout_seconds)."""

    async def _run() -> tuple[dict[str, Any], float]:
        ws = _MockWebSocket()
        state = SessionState(session_id=new_ulid())
        # _handle_request_spatial_input requires a bound emitter (else it short
        # -circuits to SPATIAL_INPUT_NO_CLIENT). A truthy sentinel is enough —
        # the emit path uses the websocket.send, not the emitter, for the prompt.
        state.emitter = object()  # type: ignore[assignment]

        handler = asyncio.create_task(
            _handle_request_spatial_input(
                ws,
                state,
                {
                    "mode": "vector_draw",
                    "title": "Draw the flood walls",
                    "description": "Outline the AOI and the barriers.",
                    "default_timeout_seconds": 300,  # long TTL — must NOT be hit
                },
            )
        )

        # Wait for the request to emit + the pending future to register, then
        # capture the server-minted request_id from the registry.
        req_id = None
        for _ in range(500):
            await asyncio.sleep(0)
            keys = list(server._PENDING_SPATIAL_INPUTS.keys())
            if keys:
                req_id = keys[0]
                break
        assert req_id is not None, "the pending spatial-input future must register"

        pd = _untagged_barrier_payload_dict(req_id)
        try:
            SpatialInputResponsePayload.model_validate(pd)
            pytest.fail("untagged barrier should have failed validation")
        except ValidationError as ve:
            err_msg = ve.errors()[0]["msg"]

        t0 = time.monotonic()
        assert _fail_pending_spatial_input(
            state.session_id, req_id, "SPATIAL_INPUT_BAD_BARRIER_TYPE", err_msg
        )
        result = await asyncio.wait_for(handler, timeout=5.0)
        elapsed = time.monotonic() - t0
        return result, elapsed

    result, elapsed = asyncio.run(_run())
    assert result["status"] == "error"
    assert result["error_code"] == "SPATIAL_INPUT_BAD_BARRIER_TYPE"
    # Honesty floor: NO fabricated barriers / AOI on an error result.
    assert "barriers" not in result
    assert "aoi_bbox" not in result
    # PROMPT, not the timeout path.
    assert elapsed < 5.0, f"resolved in {elapsed:.3f}s — must not be the TTL path"


# --------------------------------------------------------------------------- #
# Safety: a malformed reply that carries NO resolvable request_id must NOT
# crash and must NOT resolve any other session's future (the WS handler then
# only notifies the user — there is nothing to resolve).
# --------------------------------------------------------------------------- #


def test_fail_unknown_request_id_is_safe_noop():
    state = SessionState(session_id=new_ulid())
    # Nothing pending -> False, no crash.
    assert (
        _fail_pending_spatial_input(
            state.session_id, new_ulid(), "SPATIAL_INPUT_BAD_BARRIER_TYPE", "x"
        )
        is False
    )


def test_fail_cross_session_refused():
    """A non-owner session cannot fail another session's pending future."""

    async def _run() -> tuple[bool, bool]:
        owner = new_ulid()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        req_id = new_ulid()
        server._register_pending_spatial_input(owner, req_id, fut)
        try:
            refused = _fail_pending_spatial_input(
                "some-other-session", req_id, "SPATIAL_INPUT_BAD_BARRIER_TYPE", "x"
            )
            accepted = _fail_pending_spatial_input(
                owner, req_id, "SPATIAL_INPUT_BAD_BARRIER_TYPE", "x"
            )
            # Drain the now-failed future's exception so it is not flagged as
            # never-retrieved by the event loop.
            if accepted:
                with pytest.raises(SpatialInputInvalidResponseError):
                    fut.result()
            return refused, accepted
        finally:
            server._pop_pending_spatial_input(req_id)

    refused, accepted = asyncio.run(_run())
    assert refused is False, "cross-session fail must be refused"
    assert accepted is True, "owner-session fail must resolve the future"


def test_invalid_reply_with_no_request_id_extracts_none_safely():
    """The WS handler extracts request_id defensively from the raw payload; a
    payload with no/garbage request_id yields None -> nothing to fail (notify
    only). Mirror that extraction here and assert it is the safe no-op path."""
    # Garbage request_id (not a non-empty str) -> handler treats as None.
    for rid in (None, "", 123, {"x": 1}):
        payload_dict = {"geometry_type": "vector_draw"}
        if rid is not None:
            payload_dict["request_id"] = rid  # type: ignore[assignment]
        req_id = None
        if isinstance(payload_dict, dict):
            got = payload_dict.get("request_id")
            if isinstance(got, str) and got:
                req_id = got
        assert req_id is None, f"non-str/empty request_id {rid!r} must read None"
