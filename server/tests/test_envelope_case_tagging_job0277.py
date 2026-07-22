"""job-0277: envelope case-tagging (proposed A.1 amendment).

Every envelope emitted inside a turn carries ``Envelope.case_id`` = the
turn's pinned Case, via a per-task ContextVar bound by the dispatch
wrappers. The web routes tagged envelopes to the OWNING Case's stream —
killing the "still-running turn paints into the newest stream" display
limit documented in job-0269.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from grace2_agent import server
from grace2_agent.pipeline_emitter import (
    PipelineEmitter,
    bind_turn_case,
    current_turn_case,
)
from grace2_contracts.common import new_ulid


@pytest.fixture(autouse=True)
def _clean_session_registry():
    server._SESSION_ACTIVE_CASE.clear()
    yield
    server._SESSION_ACTIVE_CASE.clear()


@pytest.mark.asyncio
async def test_new_envelope_stamps_bound_case() -> None:
    async def turn(case_id: str) -> dict:
        bind_turn_case(case_id)
        return json.loads(server._new_envelope("agent-message", new_ulid(), {}))

    case = new_ulid()
    env = await asyncio.create_task(turn(case))
    assert env["case_id"] == case


@pytest.mark.asyncio
async def test_new_envelope_untagged_outside_turn() -> None:
    env = json.loads(server._new_envelope("case-list", new_ulid(), {}))
    assert env["case_id"] is None


@pytest.mark.asyncio
async def test_concurrent_turns_tag_independently() -> None:
    """ContextVar isolation: two concurrent turns never cross-tag."""
    release = asyncio.Event()

    async def turn(case_id: str) -> str:
        bind_turn_case(case_id)
        await release.wait()  # both turns alive simultaneously
        return json.loads(
            server._new_envelope("pipeline-state", new_ulid(), {})
        )["case_id"]

    a, b = new_ulid(), new_ulid()
    ta = asyncio.create_task(turn(a))
    tb = asyncio.create_task(turn(b))
    await asyncio.sleep(0.02)
    release.set()
    assert await ta == a
    assert await tb == b
    assert current_turn_case() is None  # bindings never leak to the parent


@pytest.mark.asyncio
async def test_emitter_send_stamps_bound_case() -> None:
    sent: list[str] = []

    async def sink(raw: str) -> None:
        sent.append(raw)

    emitter = PipelineEmitter(session_id=new_ulid(), sink=sink)

    async def turn(case_id: str) -> None:
        bind_turn_case(case_id)
        emitter.start_pipeline()
        emitter.add_step(name="x", tool_name="geocode_location")
        await emitter._emit_pipeline_state()

    case = new_ulid()
    await asyncio.create_task(turn(case))
    assert sent, "emitter sent nothing"
    env = json.loads(sent[-1])
    assert env["type"] == "pipeline-state"
    assert env["case_id"] == case


@pytest.mark.asyncio
async def test_dispatch_wrapper_binds_turn_case(monkeypatch) -> None:
    """The real Gemini dispatch wrapper binds the pin for the whole turn."""

    class FakeWS:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, text: str) -> None:
            self.sent.append(text)

    observed: list[str | None] = []

    async def fake_stream(websocket, st, settings, user_text, research_mode, bedrock_model=None, **_kwargs):
        observed.append(current_turn_case())

    monkeypatch.setattr(server, "_stream_gemini_reply", fake_stream)
    state = server.SessionState(session_id=new_ulid())
    case = new_ulid()
    state.active_case_id = case
    state.current_turn_case_id = case
    await asyncio.create_task(
        server._dispatch_gemini_and_persist(FakeWS(), state, None, "hi", "off")
    )
    assert observed == [case]
