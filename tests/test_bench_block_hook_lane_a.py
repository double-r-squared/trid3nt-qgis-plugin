"""BENCH pre-dispatch block hook (LANE A, task 1).

The routing-sweep experiments framework arms a session-scoped tool-block config
via the ``session-config`` path (``bench_tool_block``). When armed (bench mode
only -- absent = normal operation, ZERO dispatch overhead), the dispatch site
decides a model-picked tool's fate BEFORE invoking the fn:

  * a NON-MEMBER pick (outside allow / always_allowed / block_at_invocation) ->
    typed BENCH_BLOCKED_WRONG_PICK function-response, fn NOT run, turn ENDS.
  * a member pick in the block tier -> typed BENCH_BLOCKED_CORRECT after arg
    validation, fn NOT run (turn continues; the bench grades + ends it).
  * an always-allowed (mechanism) tool -> executes normally.

Covered here: the pure decision/parse logic, then the live dispatch loop --
armed blocks BOTH classes without executing the fn, always-allowed passes
through, and an UNARMED session is byte-identical (the fn runs).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server import data as agent_tools
from trid3nt_server.adapters.adapter import ModelSettings
from trid3nt_server.gates.tool_gating import (
    BENCH_BLOCKED_CORRECT,
    BENCH_BLOCKED_WRONG_PICK,
    BenchBlockConfig,
    BenchBlockedError,
    bench_block_decision,
    parse_bench_block_config,
)
from trid3nt_server.data import RegisteredTool
from trid3nt_server.emission.uri_registry import reset_uri_registries_for_tests
from trid3nt_contracts import new_ulid
from trid3nt_contracts.tool_registry import AtomicToolMetadata


# ---------------------------------------------------------------------------
# Pure logic: parse + decision.
# ---------------------------------------------------------------------------


def test_parse_absent_key_returns_none():
    # No bench_tool_block key -> None (leave whatever is armed untouched).
    assert parse_bench_block_config({"mode": "auto"}) is None
    assert parse_bench_block_config({}) is None


def test_parse_null_key_disarms():
    # An explicit null/false value -> None (disarm signal, distinct from absent
    # -- the caller only inspects the key when present).
    assert parse_bench_block_config({"bench_tool_block": None}) is None
    assert parse_bench_block_config({"bench_tool_block": False}) is None


def test_parse_dict_builds_config():
    cfg = parse_bench_block_config(
        {
            "bench_tool_block": {
                "allow": ["fetch_a", "fetch_b"],
                "always_allowed": ["search_tools", "web_fetch"],
                "block_at_invocation": ["run_expensive"],
            }
        }
    )
    assert isinstance(cfg, BenchBlockConfig)
    assert cfg.allow == frozenset({"fetch_a", "fetch_b"})
    assert cfg.always_allowed == frozenset({"search_tools", "web_fetch"})
    assert cfg.block_at_invocation == frozenset({"run_expensive"})


def test_parse_tolerates_junk_entries():
    cfg = parse_bench_block_config(
        {"bench_tool_block": {"allow": ["ok", 3, None, ""], "always_allowed": "nope"}}
    )
    assert cfg.allow == frozenset({"ok"})
    assert cfg.always_allowed == frozenset()
    assert cfg.block_at_invocation == frozenset()


def _cfg() -> BenchBlockConfig:
    return BenchBlockConfig(
        allow=frozenset({"fetch_a", "run_expensive"}),
        always_allowed=frozenset({"search_tools"}),
        block_at_invocation=frozenset({"run_expensive"}),
    )


def test_decision_always_allowed_executes():
    assert bench_block_decision(_cfg(), "search_tools") is None


def test_decision_member_run_tier_executes():
    assert bench_block_decision(_cfg(), "fetch_a") is None


def test_decision_block_tier_is_correct_blocked():
    assert bench_block_decision(_cfg(), "run_expensive") == "correct_blocked"


def test_decision_non_member_is_wrong_pick():
    assert bench_block_decision(_cfg(), "fetch_something_else") == "wrong_pick"


def test_decision_none_config_executes():
    # Unarmed (non-config) -> always execute.
    assert bench_block_decision(None, "anything") is None


def test_block_tier_beats_allow_absence():
    # A tool in block_at_invocation but (defensively) NOT in allow is still a
    # correct-block, never a wrong pick.
    cfg = BenchBlockConfig(block_at_invocation=frozenset({"x"}))
    assert bench_block_decision(cfg, "x") == "correct_blocked"


def test_bench_blocked_error_carries_typed_code():
    e = BenchBlockedError("wrong_pick", "fetch_x")
    assert e.error_code == BENCH_BLOCKED_WRONG_PICK
    assert e.retryable is False
    assert e.blocked_class == "wrong_pick"
    e2 = BenchBlockedError("correct_blocked", "run_y")
    assert e2.error_code == BENCH_BLOCKED_CORRECT


# ---------------------------------------------------------------------------
# Live dispatch loop.
# ---------------------------------------------------------------------------


@dataclass
class _FakeSocket:
    sent: list = field(default_factory=list)

    async def send(self, msg: str) -> None:
        try:
            self.sent.append(json.loads(msg))
        except (json.JSONDecodeError, TypeError):
            self.sent.append(msg)


def _fc_chunk(name: str, args: dict, call_id: str):
    return {"tool_call": {"name": name, "args": args, "call_id": call_id}}


def _text_chunk(text: str):
    return {"text": text}


def _settings() -> ModelSettings:
    return ModelSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )


_CALLS: list[dict] = []
_STUB_NAME = "_bench_stub_tool"


@pytest.fixture()
def _stub_tool():
    """A launch-counting stub registered in TOOL_REGISTRY for the loop tests."""
    original = agent_tools.TOOL_REGISTRY.get(_STUB_NAME)
    _CALLS.clear()
    reset_uri_registries_for_tests()

    def _fn(**kwargs):
        _CALLS.append(dict(kwargs))
        return {"status": "ok", "ran": True}

    meta = AtomicToolMetadata(
        name=_STUB_NAME, ttl_class="live-no-cache", cacheable=False
    )
    agent_tools.TOOL_REGISTRY[_STUB_NAME] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield
    finally:
        if original is not None:
            agent_tools.TOOL_REGISTRY[_STUB_NAME] = original
        else:
            agent_tools.TOOL_REGISTRY.pop(_STUB_NAME, None)
        reset_uri_registries_for_tests()


async def _drive_two_rounds(state, fake_llm) -> _FakeSocket:
    """Round 1 calls the stub tool; round 2 (if reached) calls it AGAIN.

    Two rounds let a test distinguish "turn ended after the block" (round 2
    never streams; the stub is never called) from "turn continued".
    """
    fake_llm.script([
        _fc_chunk(_STUB_NAME, {"query": "x"}, "c1"),
        _fc_chunk(_STUB_NAME, {"query": "x"}, "c2"),
        _text_chunk("Done."),
    ])

    sock = _FakeSocket()
    with patch.object(agent_server, "build_tool_declarations", return_value=[]):
        await agent_server._stream_model_reply(
            sock, state, _settings(), "please do the thing", "research"
        )
    state._rounds = len(fake_llm.calls)  # type: ignore[attr-defined]
    return sock


def _tool_io_responses(sock: _FakeSocket) -> list[str]:
    """The function_response strings from every tool-io envelope sent."""
    out = []
    for env in sock.sent:
        if isinstance(env, dict) and env.get("type") == "tool-io":
            fr = (env.get("payload") or {}).get("function_response")
            if isinstance(fr, str):
                out.append(fr)
    return out


@pytest.mark.asyncio
async def test_unarmed_executes_the_tool(_stub_tool, fake_llm):
    """UNARMED (config None) -> byte-identical: the fn runs (twice, both rounds)."""
    state = agent_server.SessionState(session_id=new_ulid())
    assert state.bench_block_config is None
    await _drive_two_rounds(state, fake_llm)
    assert len(_CALLS) == 2, "unarmed dispatch must invoke the tool every round"


@pytest.mark.asyncio
async def test_armed_wrong_pick_blocks_and_ends_turn(_stub_tool, fake_llm):
    """A non-member pick -> BENCH_BLOCKED_WRONG_PICK, fn never runs, turn ends."""
    state = agent_server.SessionState(session_id=new_ulid())
    state.bench_block_config = BenchBlockConfig(
        allow=frozenset({"some_other_tool"}),
        always_allowed=frozenset({"search_tools"}),
        block_at_invocation=frozenset(),
    )
    sock = await _drive_two_rounds(state, fake_llm)
    assert _CALLS == [], "wrong-pick must NOT execute the tool fn"
    responses = _tool_io_responses(sock)
    assert any(BENCH_BLOCKED_WRONG_PICK in r for r in responses), responses
    # Turn-ending note: round 2 never streamed (the turn ended after the block).
    assert state._rounds == 1, "wrong-pick must END the turn after round 1"


@pytest.mark.asyncio
async def test_armed_correct_blocked_validates_but_does_not_run(_stub_tool, fake_llm):
    """A block-tier member pick -> BENCH_BLOCKED_CORRECT, fn never runs."""
    state = agent_server.SessionState(session_id=new_ulid())
    state.bench_block_config = BenchBlockConfig(
        allow=frozenset({_STUB_NAME}),
        always_allowed=frozenset({"search_tools"}),
        block_at_invocation=frozenset({_STUB_NAME}),
    )
    sock = await _drive_two_rounds(state, fake_llm)
    assert _CALLS == [], "correct-blocked must NOT execute the tool fn"
    responses = _tool_io_responses(sock)
    assert any(BENCH_BLOCKED_CORRECT in r for r in responses), responses


@pytest.mark.asyncio
async def test_armed_always_allowed_executes(_stub_tool, fake_llm):
    """A tool in always_allowed rides through and executes normally."""
    state = agent_server.SessionState(session_id=new_ulid())
    state.bench_block_config = BenchBlockConfig(
        allow=frozenset({"unrelated"}),
        always_allowed=frozenset({_STUB_NAME}),
        block_at_invocation=frozenset(),
    )
    await _drive_two_rounds(state, fake_llm)
    assert len(_CALLS) == 2, "always-allowed tool must execute every round"
