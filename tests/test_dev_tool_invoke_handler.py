"""``!run`` direct tool invocation -- server handler tests (ADR 0114).

``_handle_dev_tool_invoke`` runs the named registry closure OUTSIDE the LLM
loop through the SAME ``_dispatch_tool_and_persist`` -> ``_invoke_tool_via_emitter``
seam a ``/invoke`` directive uses. These pin the handler's own contract:

  * wire-shape validation (name / args) -> typed TOOL_PARAMS_INVALID;
  * an unknown tool routes through the shared TOOL_NOT_FOUND envelope;
  * a valid invocation drives the shared emission pipeline (tool-io +
    pipeline-state + turn-complete on the wire) and respects the sync-tool
    off-load rule;
  * the payload-warning gate composes on the !run path (the gate seam is
    invoked before dispatch).

The prepared-turn scaffolding (case rebind / sync / auto-create / user-row
persist) is owned + tested by ``_prepare_user_turn``; it is stubbed here so the
handler test stays hermetic (no persistence).
"""

from __future__ import annotations

import json
import threading

import pytest

from trid3nt_server import server
from trid3nt_server import tools as agent_tools
from trid3nt_server.tools import RegisteredTool
from trid3nt_contracts.common import new_ulid
from trid3nt_contracts.tool_registry import AtomicToolMetadata


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, text: str) -> None:
        self.sent.append(json.loads(text))


_PROBE_NAME = "compute_run_probe"


@pytest.fixture(autouse=True)
def _register_probe():
    original = agent_tools.TOOL_REGISTRY.get(_PROBE_NAME)

    def _fn(**kw) -> dict:
        return {
            "ran_on_main": threading.current_thread() is threading.main_thread(),
            "echo": kw.get("echo"),
        }

    meta = AtomicToolMetadata(
        name=_PROBE_NAME, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[_PROBE_NAME] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield
    finally:
        if original is not None:
            agent_tools.TOOL_REGISTRY[_PROBE_NAME] = original
        else:
            agent_tools.TOOL_REGISTRY.pop(_PROBE_NAME, None)


@pytest.fixture(autouse=True)
def _stub_prepare(monkeypatch: pytest.MonkeyPatch):
    """Isolate the handler from persistence: the prepared-turn scaffolding is
    tested separately. Leaves ``current_turn_case_id`` None so the finally-
    persist branches skip (no persistence bound in the test)."""

    async def _noop_prepare(websocket, state, text, *, client_case_id=None):
        state.current_turn_case_id = None
        return None

    monkeypatch.setattr(server, "_prepare_user_turn", _noop_prepare)


async def _await_inflight(state: server.SessionState) -> None:
    for task in list(state.inflight_tasks.values()):
        await task


def _errors(ws: FakeWS) -> list[dict]:
    return [e for e in ws.sent if e.get("type") == "error"]


@pytest.mark.asyncio
async def test_missing_name_typed_error() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await server._handle_dev_tool_invoke(ws, state, {"args": {}})
    errs = _errors(ws)
    assert errs and errs[0]["payload"]["error_code"] == "TOOL_PARAMS_INVALID"
    # No task was created for an invalid envelope.
    assert not state.inflight_tasks


@pytest.mark.asyncio
async def test_bad_args_typed_error() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await server._handle_dev_tool_invoke(
        ws, state, {"name": _PROBE_NAME, "args": [1, 2, 3]}
    )
    errs = _errors(ws)
    assert errs and errs[0]["payload"]["error_code"] == "TOOL_PARAMS_INVALID"
    assert not state.inflight_tasks


@pytest.mark.asyncio
async def test_unknown_tool_routes_tool_not_found() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await server._handle_dev_tool_invoke(
        ws, state, {"name": "nonexistent_tool_xyz", "args": {}}
    )
    await _await_inflight(state)
    codes = [e["payload"].get("error_code") for e in _errors(ws)]
    assert "TOOL_NOT_FOUND" in codes


@pytest.mark.asyncio
async def test_valid_invoke_drives_shared_emission_pipeline() -> None:
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await server._handle_dev_tool_invoke(
        ws, state, {"name": _PROBE_NAME, "args": {"echo": 7},
                    "raw_text": "!run compute_run_probe(echo=7)"}
    )
    await _await_inflight(state)
    types = [e.get("type") for e in ws.sent]
    # The tool-io sidecar + pipeline-state frames + the end-of-turn idle marker
    # all rode the shared seam.
    assert "tool-io" in types
    assert "pipeline-state" in types
    assert "turn-complete" in types
    # The tool-io sidecar carried THIS tool's name.
    io = [e for e in ws.sent if e.get("type") == "tool-io"]
    assert any(e["payload"].get("tool_name") == _PROBE_NAME for e in io)


@pytest.mark.asyncio
async def test_offload_rule_respected_on_run_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arm the staged off-load; the probe's compute_ name matches the subset
    # predicate, so its sync body must run OFF the loop thread.
    monkeypatch.setattr(server, "_SYNC_OFFLOAD_MODE", "subset")
    loop_ident = threading.current_thread().ident
    captured: dict = {}

    def _fn(**kw) -> dict:
        captured["ident"] = threading.current_thread().ident
        return {"echo": kw.get("echo")}

    meta = AtomicToolMetadata(
        name=_PROBE_NAME, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[_PROBE_NAME] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await server._handle_dev_tool_invoke(
        ws, state, {"name": _PROBE_NAME, "args": {"echo": 1}}
    )
    await _await_inflight(state)
    assert captured["ident"] != loop_ident


@pytest.mark.asyncio
async def test_payload_warning_gate_composes_on_run_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    async def _spy_gate(websocket, state, tool_name, params):
        seen["tool_name"] = tool_name
        return True, params  # proceed

    monkeypatch.setattr(server, "_maybe_gate_on_payload_warning", _spy_gate)
    ws = FakeWS()
    state = server.SessionState(session_id=new_ulid())
    await server._handle_dev_tool_invoke(
        ws, state, {"name": _PROBE_NAME, "args": {"echo": 1}}
    )
    await _await_inflight(state)
    # The gate seam was invoked for the !run dispatch.
    assert seen.get("tool_name") == _PROBE_NAME


class _TypedToolError(RuntimeError):
    """A stand-in for a tool's typed exception (MeshGenerationError etc.)."""

    error_code = "TELEMAC_ROG_POUR_POINT_OFF_DEM"
    retryable = False


@pytest.mark.asyncio
async def test_typed_tool_error_reaches_client_as_error_envelope() -> None:
    """A typed tool exception on the ``!run`` no-awaiter path must surface as a
    structured ``error`` envelope -- not vanish as an "asyncio Task exception was
    never retrieved" while the client sees nothing (ADR 0198 honesty-floor fix).

    The A.6 ``ErrorCode`` Literal is a CLOSED set, so a tool's own out-of-enum
    code (``TELEMAC_ROG_POUR_POINT_OFF_DEM``) rides the wire as INTERNAL_ERROR
    with the specific code LEADING the message as a ``[MARKER]`` (house
    convention) -- honest + greppable, and it never raises inside _send_error
    (which would re-open the silence)."""

    def _raise(**_kw):
        raise _TypedToolError("pour point falls outside the DEM window")

    meta = AtomicToolMetadata(
        name="compute_run_typed_fail", ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY["compute_run_typed_fail"] = RegisteredTool(
        metadata=meta, fn=_raise, module=__name__
    )
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        await server._handle_dev_tool_invoke(
            ws, state, {"name": "compute_run_typed_fail", "args": {}}
        )
        await _await_inflight(state)
    finally:
        agent_tools.TOOL_REGISTRY.pop("compute_run_typed_fail", None)

    errs = _errors(ws)
    assert errs, (
        f"Expected an error envelope; got types {[e.get('type') for e in ws.sent]!r}"
    )
    payload = errs[0]["payload"]
    assert payload["error_code"] == "INTERNAL_ERROR"
    assert payload["retryable"] is False
    # the specific tool code is preserved in the message marker, and the detail.
    assert "[TELEMAC_ROG_POUR_POINT_OFF_DEM]" in payload["message"]
    assert "DEM window" in payload["message"]


@pytest.mark.asyncio
async def test_valid_enum_tool_error_passes_code_through() -> None:
    """When a tool's typed code IS already a valid A.6 ErrorCode it rides the
    wire verbatim (an upstream-provider LLM_UNAVAILABLE is NOT internalized)."""

    class _ValidCodeError(RuntimeError):
        error_code = "DEM_SOURCE_UNAVAILABLE"
        retryable = True

    def _raise(**_kw):
        raise _ValidCodeError("3DEP tiles unavailable for the AOI")

    meta = AtomicToolMetadata(
        name="compute_run_valid_code_fail", ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY["compute_run_valid_code_fail"] = RegisteredTool(
        metadata=meta, fn=_raise, module=__name__
    )
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        await server._handle_dev_tool_invoke(
            ws, state, {"name": "compute_run_valid_code_fail", "args": {}}
        )
        await _await_inflight(state)
    finally:
        agent_tools.TOOL_REGISTRY.pop("compute_run_valid_code_fail", None)

    errs = _errors(ws)
    assert errs
    payload = errs[0]["payload"]
    assert payload["error_code"] == "DEM_SOURCE_UNAVAILABLE"
    assert payload["retryable"] is True
    assert "3DEP" in payload["message"]


@pytest.mark.asyncio
async def test_untyped_tool_error_defaults_to_execution_failed() -> None:
    """An UNTYPED tool exception (no error_code attr) still reaches the client,
    the ``[TOOL_EXECUTION_FAILED]`` marker on INTERNAL_ERROR / non-retryable,
    rather than silently dying on the create_task path."""

    def _raise(**_kw):
        raise ValueError("boom -- something broke inside the tool")

    meta = AtomicToolMetadata(
        name="compute_run_untyped_fail", ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY["compute_run_untyped_fail"] = RegisteredTool(
        metadata=meta, fn=_raise, module=__name__
    )
    try:
        ws = FakeWS()
        state = server.SessionState(session_id=new_ulid())
        await server._handle_dev_tool_invoke(
            ws, state, {"name": "compute_run_untyped_fail", "args": {}}
        )
        await _await_inflight(state)
    finally:
        agent_tools.TOOL_REGISTRY.pop("compute_run_untyped_fail", None)

    errs = _errors(ws)
    assert errs
    payload = errs[0]["payload"]
    assert payload["error_code"] == "INTERNAL_ERROR"
    assert payload["retryable"] is False
    assert "[TOOL_EXECUTION_FAILED]" in payload["message"]
    assert "boom" in payload["message"]


def test_reconstruct_run_signature() -> None:
    assert server._reconstruct_run_signature("geocode_location", {}) == (
        "!run geocode_location"
    )
    sig = server._reconstruct_run_signature("fetch_dem", {"bbox": [1, 2]})
    assert sig.startswith("!run fetch_dem ")
    assert json.loads(sig[len("!run fetch_dem "):]) == {"bbox": [1, 2]}
