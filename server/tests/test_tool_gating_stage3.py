"""Stage 3 (ADR 0017/0018) -- per-turn TOP-K TOOL GATING for the openai path.

The routing bench's own recommendation: the openai adapter was sending ALL
~190 tool schemas per round. ``tool_gating.gate_tool_registry`` trims the
per-turn registry to the retrieval top-k plus the always-include floors:

  * the META floor (hot set + catalog_search/fetch + web_fetch),
  * every tool already used this case-session (dispatched + explicit),
  * any tool the user NAMED in the message.

Scoped to ``MODEL_PROVIDER=openai`` -- the scripted/bedrock/vertex tool lists
are byte-unchanged. ``TRID3NT_TOOL_GATING_TOPK=0`` disables; a cold/empty
ranking FAILS OPEN to the full registry.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

import trid3nt_server.main as agent_main
from trid3nt_server import server as agent_server
from trid3nt_server.adapter import GeminiSettings, TextDeltaEvent
from trid3nt_server.categories import HOT_SET_TOOLS
from trid3nt_server.tool_gating import (
    META_TOOL_FLOOR,
    TOOL_GATING_TOPK_DEFAULT,
    gate_tool_registry,
    gating_topk,
    named_tools_in_text,
)
from trid3nt_server.tools import TOOL_REGISTRY
from trid3nt_contracts import new_ulid

# The gating tests rank/keep REAL registry names -- make sure the full
# registry (incl. catalog tools, which register via the startup import path)
# is populated regardless of test ordering.
agent_main._import_tools_registry()


# ---------------------------------------------------------------------------
# Unit: env resolution
# ---------------------------------------------------------------------------


def test_gating_topk_default(monkeypatch):
    monkeypatch.delenv("TRID3NT_TOOL_GATING_TOPK", raising=False)
    assert gating_topk() == TOOL_GATING_TOPK_DEFAULT == 24


def test_gating_topk_env_override_and_zero_disables(monkeypatch):
    monkeypatch.setenv("TRID3NT_TOOL_GATING_TOPK", "10")
    assert gating_topk() == 10
    monkeypatch.setenv("TRID3NT_TOOL_GATING_TOPK", "0")
    assert gating_topk() == 0  # 0 == gate disabled (all tools)
    monkeypatch.setenv("TRID3NT_TOOL_GATING_TOPK", "garbage")
    assert gating_topk() == TOOL_GATING_TOPK_DEFAULT
    monkeypatch.setenv("TRID3NT_TOOL_GATING_TOPK", "-5")
    assert gating_topk() == TOOL_GATING_TOPK_DEFAULT


# ---------------------------------------------------------------------------
# Unit: named-tool matching (alias/anchor match)
# ---------------------------------------------------------------------------


def test_named_tools_exact_name():
    names = {"fetch_dem", "fetch_nexrad_reflectivity", "publish_layer"}
    got = named_tools_in_text(
        "please use fetch_nexrad_reflectivity over Kansas", names
    )
    assert got == {"fetch_nexrad_reflectivity"}


def test_named_tools_spaced_form():
    names = {"fetch_dem", "fetch_nexrad_reflectivity"}
    got = named_tools_in_text(
        "use the fetch nexrad reflectivity tool over Kansas", names
    )
    assert got == {"fetch_nexrad_reflectivity"}


def test_named_tools_no_false_positive_on_substring():
    # "dem" alone must not match fetch_dem (whole-word / whole-phrase only).
    assert named_tools_in_text("show me a dem of the area", {"fetch_dem"}) == set()
    assert named_tools_in_text("", {"fetch_dem"}) == set()
    assert named_tools_in_text(None, {"fetch_dem"}) == set()


# ---------------------------------------------------------------------------
# Unit: the gate itself
# ---------------------------------------------------------------------------


def _ranked(n: int = 30) -> list[tuple[str, float]]:
    """A ranked list of n real registry names (deterministic order)."""
    names = sorted(TOOL_REGISTRY)[:n]
    return [(name, 0.05 - i * 0.001) for i, name in enumerate(names)]


def test_gate_keeps_topk_plus_meta_floor():
    ranked = _ranked(30)
    gated = gate_tool_registry("some request", dict(TOOL_REGISTRY), ranked, 24)
    assert gated is not None
    # top-24 of the ranking present
    for name, _ in ranked[:24]:
        assert name in gated
    # meta floor always present (registered members)
    for name in META_TOOL_FLOOR & set(TOOL_REGISTRY):
        assert name in gated, f"meta-floor tool {name} was gated out"
    # HOT_SET is a subset of the meta floor
    assert HOT_SET_TOOLS <= META_TOOL_FLOOR
    # and it actually shrank
    assert len(gated) < len(TOOL_REGISTRY)


def test_gate_always_includes_used_tools():
    ranked = _ranked(24)
    used = {"fetch_usgs_earthquakes", "compute_ndvi"}
    assert used <= set(TOOL_REGISTRY)
    assert not (used & {n for n, _ in ranked[:24]})  # not already in top-k
    gated = gate_tool_registry(
        "unrelated request", dict(TOOL_REGISTRY), ranked, 24, used_tools=used
    )
    assert gated is not None
    for name in used:
        assert name in gated, f"already-used tool {name} was hidden mid-task"


def test_gate_always_includes_named_tool():
    ranked = _ranked(24)
    target = "fetch_nexrad_reflectivity"
    assert target in TOOL_REGISTRY
    assert target not in {n for n, _ in ranked[:24]}
    gated = gate_tool_registry(
        f"run {target} for me", dict(TOOL_REGISTRY), ranked, 24
    )
    assert gated is not None
    assert target in gated


def test_gate_fails_open_on_empty_ranking():
    # Cold index / no match -> ranked=[] -> None (caller keeps full registry).
    assert gate_tool_registry("x", dict(TOOL_REGISTRY), [], 24) is None


def test_gate_disabled_at_k_zero():
    assert gate_tool_registry("x", dict(TOOL_REGISTRY), _ranked(), 0) is None


# ---------------------------------------------------------------------------
# Integration: provider scoping through _stream_gemini_reply
# ---------------------------------------------------------------------------


@dataclass
class _FakeSocket:
    sent: list = field(default_factory=list)

    async def send(self, msg: str) -> None:
        try:
            self.sent.append(json.loads(msg))
        except (json.JSONDecodeError, TypeError):
            self.sent.append(msg)


def _settings() -> GeminiSettings:
    return GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )


async def _fake_stream(*_a, **_k):
    yield TextDeltaEvent(delta="done")


async def _drive_turn_and_capture_registry(monkeypatch) -> dict:
    """Run one no-tool turn and capture the registry handed to
    build_tool_declarations."""
    from trid3nt_server.tools.discovery import tool_retrieval as tr

    monkeypatch.setattr(
        tr, "retrieve_ranked_tools", lambda text, k=25: _ranked(30)[: max(k, 2)]
    )

    captured: dict = {}

    def _capture_decls(registry):
        captured["registry"] = dict(registry)
        return []

    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    with patch.object(agent_server, "build_client", return_value=MagicMock()), \
         patch.object(agent_server, "build_tool_declarations", _capture_decls), \
         patch.object(agent_server, "stream_events_with_contents", _fake_stream):
        await agent_server._stream_gemini_reply(
            sock, state, _settings(), "fetch something for Boulder", "research"
        )
    return captured


@pytest.mark.asyncio
async def test_openai_provider_turn_is_gated(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.delenv("TRID3NT_TOOL_GATING_TOPK", raising=False)
    captured = await _drive_turn_and_capture_registry(monkeypatch)
    registry = captured["registry"]
    assert len(registry) < len(TOOL_REGISTRY), (
        f"openai turn was NOT gated: {len(registry)} == full registry"
    )
    # floors survive the gate
    for name in META_TOOL_FLOOR & set(TOOL_REGISTRY):
        assert name in registry


@pytest.mark.asyncio
async def test_openai_provider_gate_disabled_by_env(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("TRID3NT_TOOL_GATING_TOPK", "0")
    captured = await _drive_turn_and_capture_registry(monkeypatch)
    assert len(captured["registry"]) == len(TOOL_REGISTRY)


@pytest.mark.asyncio
async def test_scripted_provider_turn_is_never_gated(monkeypatch):
    # bedrock/scripted/vertex paths byte-unchanged: full registry always.
    monkeypatch.setenv("MODEL_PROVIDER", "scripted")
    from trid3nt_server.scripted_adapter import set_script

    set_script([{"text": "ok"}])
    try:
        captured = await _drive_turn_and_capture_registry(monkeypatch)
    finally:
        set_script(None)
    assert len(captured["registry"]) == len(TOOL_REGISTRY)


@pytest.mark.asyncio
async def test_openai_gate_fails_open_on_cold_index(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.delenv("TRID3NT_TOOL_GATING_TOPK", raising=False)
    from trid3nt_server.tools.discovery import tool_retrieval as tr

    monkeypatch.setattr(tr, "retrieve_ranked_tools", lambda text, k=25: [])

    captured: dict = {}

    def _capture_decls(registry):
        captured["registry"] = dict(registry)
        return []

    sock = _FakeSocket()
    state = agent_server.SessionState(session_id=new_ulid())
    with patch.object(agent_server, "build_client", return_value=MagicMock()), \
         patch.object(agent_server, "build_tool_declarations", _capture_decls), \
         patch.object(agent_server, "stream_events_with_contents", _fake_stream):
        await agent_server._stream_gemini_reply(
            sock, state, _settings(), "fetch something", "research"
        )
    assert len(captured["registry"]) == len(TOOL_REGISTRY)
