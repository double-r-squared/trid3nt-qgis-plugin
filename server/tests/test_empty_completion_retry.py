"""OPEN-16 empty-completion retry tests (live 2026-07-19).

The local qwen3 model (MODEL_PROVIDER=openai) occasionally returns a round with
ZERO tool calls AND ZERO non-whitespace text -- the empty-completion shape (log:
"gemini loop terminal ... text_chunks=0"). Before this fix the loop logged
terminal and BROKE, so the user's request (e.g. compute_hillshade) silently died.

The fix (server.py, ``_stream_gemini_reply`` loop): on the LOCAL path only, an
empty round RETRIES with a corrective user-role nudge appended to ``contents``,
BOUNDED by ``_EMPTY_COMPLETION_RETRY_CAP`` so an always-empty model can never
loop forever. Bedrock / vertex (production narration) is byte-unchanged.

These drive the real loop with a scripted ``stream_events_with_contents`` (no
live model, mirroring test_multi_turn_loop.py's fake-chunk approach) so the
per-round event stream -- including a genuinely empty round -- is fully
controlled. Cases:

  (a) empty round -> RETRIED with a nudge appended -> next round calls a tool
      -> turn completes normally.
  (b) CAP+1 consecutive empty rounds stop at the cap and end the turn (no
      infinite loop; exactly CAP nudges appended).
  (c) a normal text answer (non-empty, no tool) still terminates in ONE round.
  (d) the non-openai (vertex/bedrock) provider path never retries.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from unittest.mock import MagicMock, patch

from trid3nt_server.adapter import (
    FunctionCallEvent,
    GeminiSettings,
    TextDeltaEvent,
)
from trid3nt_contracts import new_ulid


@dataclass
class _FakeSocket:
    """Minimal WebSocket shim that records every ``send`` payload."""

    sent: list[str] = field(default_factory=list)

    async def send(self, msg: str) -> None:  # noqa: D401 -- protocol shim
        self.sent.append(msg)


def _text_round(text: str):
    """One round that streams a single text delta (non-empty narration)."""
    return [TextDeltaEvent(delta=text)]


def _tool_round(name: str, args: dict, call_id: str):
    """One round that streams a single function_call (a tool request)."""
    return [FunctionCallEvent(name=name, call_id=call_id, args=args)]


def _empty_round():
    """One round that streams NOTHING -- the qwen3 empty-completion shape."""
    return []


def _install_scripted_stream(agent_server, rounds):
    """Patch server.stream_events_with_contents with a scripted per-round fake.

    ``rounds`` is a list of event-lists (one per model call). Each invocation
    pops the next round and yields its events. Every call snapshots the text of
    the ``user``-role Content parts in ``contents`` so a test can assert the
    corrective nudge was (or was not) appended between rounds.

    Returns ``(contents_user_texts_per_call, model_call_count)`` recorders.
    """
    round_iter = iter(rounds)
    user_texts_per_call: list[list[str]] = []
    model_calls: list[int] = []

    async def _fake_stream(client, model, contents, **kwargs):
        model_calls.append(1)
        # Snapshot the user-role text parts visible on THIS round's request.
        snap: list[str] = []
        for c in contents:
            if getattr(c, "role", None) != "user":
                continue
            for p in getattr(c, "parts", []) or []:
                if getattr(p, "text", None):
                    snap.append(p.text)
        user_texts_per_call.append(snap)
        try:
            events = next(round_iter)
        except StopIteration:
            events = []
        for ev in events:
            yield ev

    return (
        user_texts_per_call,
        model_calls,
        patch.object(agent_server, "stream_events_with_contents", _fake_stream),
    )


def _drive(provider: str, rounds, monkeypatch, dispatch_side_effect=None):
    """Run one ``_stream_gemini_reply`` turn under ``provider`` with ``rounds``.

    Returns ``(user_texts_per_call, model_calls, dispatch_log, sock)``.
    """
    from trid3nt_server import server as agent_server
    from trid3nt_server.server import SessionState

    monkeypatch.setenv("MODEL_PROVIDER", provider)

    dispatch_log: list[tuple[str, dict]] = []

    async def _fake_invoke(_ws, _state, name, args):
        dispatch_log.append((name, args))
        if dispatch_side_effect is not None:
            return dispatch_side_effect(name, args)
        return {"status": "ok"}

    user_texts_per_call, model_calls, stream_patch = _install_scripted_stream(
        agent_server, rounds
    )

    sock = _FakeSocket()
    state = SessionState(session_id=new_ulid())
    settings = GeminiSettings(
        model="gpt-oss", project="t", location="us-central1", use_vertex=True
    )

    async def _run():
        with patch.object(agent_server, "build_client", return_value=MagicMock()), \
             patch.object(
                 agent_server, "_invoke_tool_via_emitter", side_effect=_fake_invoke
             ), \
             patch.object(agent_server, "build_tool_declarations", return_value=[]), \
             stream_patch:
            await agent_server._stream_gemini_reply(
                sock, state, settings, "compute a hillshade here", "research"
            )

    asyncio.run(_run())
    return user_texts_per_call, model_calls, dispatch_log, sock


def _nudge_seen(user_texts_per_call) -> int:
    """Count how many rounds saw the empty-completion nudge in their contents."""
    from trid3nt_server.server import _EMPTY_COMPLETION_NUDGE

    return sum(
        1 for snap in user_texts_per_call if _EMPTY_COMPLETION_NUDGE in snap
    )


# --------------------------------------------------------------------------- #
# (a) empty round -> retried with a nudge -> next round's tool call completes.
# --------------------------------------------------------------------------- #
def test_empty_round_is_retried_then_tool_completes(monkeypatch):
    rounds = [
        _empty_round(),  # round 1: qwen3 emits nothing -> must RETRY
        _tool_round("geocode_location", {"query": "here"}, "call-geo"),  # round 2
        _text_round("Hillshade computed."),  # round 3: terminal narration
    ]
    user_texts, model_calls, dispatch_log, _sock = _drive(
        "openai", rounds, monkeypatch,
        dispatch_side_effect=lambda n, a: {"bbox": [0, 0, 1, 1]},
    )

    # The turn did NOT die on the empty round: the tool ran and 3 rounds fired.
    assert dispatch_log == [("geocode_location", {"query": "here"})]
    assert len(model_calls) == 3, f"expected 3 model calls, got {len(model_calls)}"

    # The corrective nudge was appended to contents BEFORE the retried round 2:
    # round 1 (the empty round) saw NO nudge; round 2 (the retry) DID.
    from trid3nt_server.server import _EMPTY_COMPLETION_NUDGE

    assert _nudge_seen(user_texts) >= 1, "nudge was not appended on retry"
    assert _EMPTY_COMPLETION_NUDGE not in user_texts[0]
    assert _EMPTY_COMPLETION_NUDGE in user_texts[1]


# --------------------------------------------------------------------------- #
# (b) CAP+1 consecutive empty rounds stop at the cap -- no infinite loop.
# --------------------------------------------------------------------------- #
def test_empty_rounds_stop_at_cap(monkeypatch):
    from trid3nt_server.server import _EMPTY_COMPLETION_RETRY_CAP

    # More empty rounds than the loop can ever consume: the cap must stop it.
    rounds = [_empty_round() for _ in range(_EMPTY_COMPLETION_RETRY_CAP + 5)]
    user_texts, model_calls, dispatch_log, _sock = _drive(
        "openai", rounds, monkeypatch
    )

    # CAP retries + the final (capped) empty round = CAP+1 model calls, then
    # the turn ends. Never the full step cap, never infinite.
    assert len(model_calls) == _EMPTY_COMPLETION_RETRY_CAP + 1, (
        f"expected {_EMPTY_COMPLETION_RETRY_CAP + 1} model calls "
        f"(CAP retries + terminal), got {len(model_calls)}"
    )
    # Exactly CAP nudges were appended (one per retry).
    assert _nudge_seen(user_texts) == _EMPTY_COMPLETION_RETRY_CAP
    # No tool ever ran (every round was empty).
    assert dispatch_log == []


# --------------------------------------------------------------------------- #
# (c) a normal (non-empty) text answer terminates in ONE round -- no retry.
# --------------------------------------------------------------------------- #
def test_normal_text_answer_no_spurious_retry(monkeypatch):
    rounds = [_text_round("Here is your answer.")]
    user_texts, model_calls, dispatch_log, _sock = _drive(
        "openai", rounds, monkeypatch
    )

    assert len(model_calls) == 1, "a normal text answer must not be retried"
    assert _nudge_seen(user_texts) == 0
    assert dispatch_log == []


# --------------------------------------------------------------------------- #
# (d) the non-openai provider path NEVER retries an empty round.
# --------------------------------------------------------------------------- #
def test_non_openai_provider_never_retries(monkeypatch):
    # Two empty rounds queued, but the vertex path must break on the FIRST one.
    rounds = [_empty_round(), _empty_round()]
    user_texts, model_calls, dispatch_log, _sock = _drive(
        "vertex", rounds, monkeypatch
    )

    assert len(model_calls) == 1, (
        "vertex (production narration) must break on an empty round, not retry"
    )
    assert _nudge_seen(user_texts) == 0
    assert dispatch_log == []
