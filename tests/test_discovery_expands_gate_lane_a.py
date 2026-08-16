"""DISCOVERY-EXPANDS-GATE (LANE A, task 2).

Tool names the tool-search tool (``search_tools``, formerly ``discover_dataset``)
returns during a turn are unioned into the visible gate for SUBSEQUENT rounds --
capped at +8 per turn, logged. This lets the model discover its way past a
trimmed gate without re-dumping the whole catalog.

Covered: the pure result parser + registry-lookup name resolver, then the live
loop -- expand fires (discovered tools land in the next round's declarations),
the +8 cap holds, and the widening is logged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server.agent import tools as agent_tools
from trid3nt_server.agent.adapters.adapter import ModelSettings
from trid3nt_server.agent.tools.search.tool_retrieval import CORE_FLOOR
from trid3nt_server.agent.tools import RegisteredTool
from trid3nt_server.emission.uri_registry import reset_uri_registries_for_tests
from trid3nt_contracts import new_ulid
from trid3nt_contracts.tool_registry import AtomicToolMetadata


# ---------------------------------------------------------------------------
# Pure helpers.
# ---------------------------------------------------------------------------


def test_search_tool_name_resolves_by_registry_lookup():
    # Resolved off the discovery module's registration metadata, not hardcoded.
    names = agent_server._tool_search_tool_names()
    assert "search_tools" in names


def test_parse_search_result_extracts_ranked_names():
    result = {
        "results": [
            {"tool_name": "fetch_a", "score": 0.9},
            {"tool_name": "fetch_b", "score": 0.5},
            {"tool_name": "fetch_a", "score": 0.1},  # dup -> dropped
            {"score": 0.05},  # no name -> skipped
            "junk",  # non-dict -> skipped
        ]
    }
    assert agent_server._tool_names_from_search_result(result) == ["fetch_a", "fetch_b"]


def test_parse_search_result_tolerates_junk():
    assert agent_server._tool_names_from_search_result(None) == []
    assert agent_server._tool_names_from_search_result({"results": "nope"}) == []
    assert agent_server._tool_names_from_search_result({}) == []


# ---------------------------------------------------------------------------
# Live loop.
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


def _discoverable_names(n: int) -> list[str]:
    """``n`` real registered tool names that are NOT in the core floor.

    These are the candidates a search returns -- they must be OUTSIDE the
    trimmed visible set so the union actually adds them.
    """
    out = [
        name
        for name in sorted(agent_tools.TOOL_REGISTRY)
        if name not in CORE_FLOOR and name != "search_tools"
    ]
    return out[:n]


@pytest.fixture()
def _stub_search():
    """Shadow ``search_tools`` with a stub returning a fixed candidate list."""
    name = "search_tools"
    original = agent_tools.TOOL_REGISTRY.get(name)
    reset_uri_registries_for_tests()
    hits = _discoverable_names(10)  # more than the +8 cap
    assert len(hits) == 10, "need >=10 non-hot-set tools for the cap test"

    async def _fn(**_kwargs):
        return {"results": [{"tool_name": h, "score": 0.1} for h in hits]}

    meta = AtomicToolMetadata(name=name, ttl_class="live-no-cache", cacheable=False)
    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=meta, fn=_fn, module=__name__
    )
    try:
        yield hits
    finally:
        if original is not None:
            agent_tools.TOOL_REGISTRY[name] = original
        else:
            agent_tools.TOOL_REGISTRY.pop(name, None)
        reset_uri_registries_for_tests()


async def _drive_with_trimmed_gate(state, monkeypatch, decl_registries, fake_llm):
    """Drive: retrieval trims the gate to {core floor + search_tools}; round 1
    calls search_tools; capture the registry keys passed to
    ``build_tool_declarations`` on each build so the rebuild is observable."""
    # Trim the visible set so the union actually adds (else every tool is
    # already visible and the union is a no-op).
    visible = set(CORE_FLOOR) | {"search_tools"}
    monkeypatch.setattr(
        "trid3nt_server.agent.tools.search.tool_retrieval.retrieve_visible_tools",
        lambda *_a, **_k: set(visible),
    )

    fake_llm.script([
        _fc_chunk("search_tools", {"query": "flood"}, "c1"),
        _text_chunk("Here are some options."),
    ])

    def _capture_decls(registry):
        decl_registries.append(set(registry.keys()))
        return []

    sock = _FakeSocket()
    with patch.object(
        agent_server, "build_tool_declarations", side_effect=_capture_decls
    ):
        await agent_server._stream_model_reply(
            sock, state, _settings(), "find me flood tools", "research"
        )
    return sock


@pytest.mark.asyncio
async def test_discovery_expand_fires_and_caps_at_8(_stub_search, monkeypatch, caplog, fake_llm):
    hits = _stub_search
    decl_registries: list[set] = []
    state = agent_server.SessionState(session_id=new_ulid())
    with caplog.at_level(logging.INFO, logger="trid3nt_server.server"):
        await _drive_with_trimmed_gate(state, monkeypatch, decl_registries, fake_llm)

    # First build (pre-loop) is the trimmed gate; a later build is the rebuild.
    assert len(decl_registries) >= 2, "expected a rebuild after the search round"
    pre_loop = decl_registries[0]
    rebuilt = decl_registries[-1]

    # None of the discovered tools were visible before the search.
    assert not (set(hits) & pre_loop), "discovered tools must start OUT of the gate"
    # Exactly 8 (the cap) of the 10 discovered tools were unioned in.
    added = set(hits) & rebuilt
    assert len(added) == 8, f"expected the +8 cap, got {len(added)}: {sorted(added)}"
    # The rebuilt gate is a superset of the pre-loop gate (union, never shrink).
    assert pre_loop <= rebuilt

    assert any(
        "discovery-expand: +" in r.message for r in caplog.records
    ), "the expansion must be logged"


@pytest.mark.asyncio
async def test_discovery_expand_noop_when_gate_untrimmed(_stub_search, monkeypatch, fake_llm):
    """With the FULL registry visible (retrieval surfaces everything), the
    discovered tools are already present -> no rebuild, no cap consumed."""
    monkeypatch.setattr(
        "trid3nt_server.agent.tools.search.tool_retrieval.retrieve_visible_tools",
        lambda *_a, **_k: set(agent_tools.TOOL_REGISTRY),
    )
    decl_registries: list[set] = []

    fake_llm.script([
        _fc_chunk("search_tools", {"query": "flood"}, "c1"),
        _text_chunk("done"),
    ])

    def _capture_decls(registry):
        decl_registries.append(set(registry.keys()))
        return []

    state = agent_server.SessionState(session_id=new_ulid())
    sock = _FakeSocket()
    with patch.object(
        agent_server, "build_tool_declarations", side_effect=_capture_decls
    ):
        await agent_server._stream_model_reply(
            sock, state, _settings(), "find me flood tools", "research"
        )
    # Full registry already contains the discovered tools -> the only build is
    # the pre-loop one (no dirty rebuild).
    assert len(decl_registries) == 1, "untrimmed gate must not rebuild declarations"
