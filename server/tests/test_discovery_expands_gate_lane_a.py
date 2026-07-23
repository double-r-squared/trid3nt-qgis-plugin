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
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server import tools as agent_tools
from trid3nt_server.adapter import GeminiSettings
from trid3nt_server.categories import HOT_SET_TOOLS
from trid3nt_server.tools import RegisteredTool
from trid3nt_server.uri_registry import reset_uri_registries_for_tests
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
    fn_call = MagicMock()
    fn_call.name = name
    fn_call.id = call_id
    fn_call.args = args
    part = MagicMock()
    part.function_call = fn_call
    part.text = None
    content = MagicMock()
    content.parts = [part]
    cand = MagicMock()
    cand.content = content
    chunk = MagicMock()
    chunk.candidates = [cand]
    chunk.text = None
    return chunk


def _text_chunk(text: str):
    part = MagicMock()
    part.function_call = None
    part.text = text
    content = MagicMock()
    content.parts = [part]
    cand = MagicMock()
    cand.content = content
    chunk = MagicMock()
    chunk.candidates = [cand]
    chunk.text = text
    return chunk


def _settings() -> GeminiSettings:
    return GeminiSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )


def _discoverable_names(n: int) -> list[str]:
    """``n`` real registered tool names that are NOT in the hot-set floor.

    These are the candidates a search returns -- they must be OUTSIDE the
    trimmed visible set so the union actually adds them.
    """
    out = [
        name
        for name in sorted(agent_tools.TOOL_REGISTRY)
        if name not in HOT_SET_TOOLS and name != "search_tools"
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


async def _drive_with_trimmed_gate(state, monkeypatch, decl_registries):
    """Drive: enforce mode trims the gate to {hot set + search_tools}; round 1
    calls search_tools; capture the registry keys passed to
    ``build_tool_declarations`` on each build so the rebuild is observable."""
    # Enforce mode so _retrieval_registry is a TRIMMED subset (else every tool
    # is already visible and the union is a no-op).
    monkeypatch.setenv("TRID3NT_TOOL_RETRIEVAL", "enforce")
    visible = set(HOT_SET_TOOLS) | {"search_tools"}
    monkeypatch.setattr(
        "trid3nt_server.tools.discovery.tool_retrieval.retrieve_visible_tools",
        lambda *_a, **_k: set(visible),
    )

    rounds = {"n": 0}

    def _script(**kwargs):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return iter([_fc_chunk("search_tools", {"query": "flood"}, "c1")])
        return iter([_text_chunk("Here are some options.")])

    def _capture_decls(registry):
        decl_registries.append(set(registry.keys()))
        return []

    sock = _FakeSocket()
    with patch.object(agent_server, "build_client", return_value=MagicMock()), patch.object(
        agent_server, "build_tool_declarations", side_effect=_capture_decls
    ):
        agent_server.build_client.return_value.models.generate_content_stream.side_effect = (
            lambda **kw: _script(**kw)
        )
        await agent_server._stream_gemini_reply(
            sock, state, _settings(), "find me flood tools", "research"
        )
    return sock


@pytest.mark.asyncio
async def test_discovery_expand_fires_and_caps_at_8(_stub_search, monkeypatch, caplog):
    hits = _stub_search
    decl_registries: list[set] = []
    state = agent_server.SessionState(session_id=new_ulid())
    with caplog.at_level(logging.INFO, logger="trid3nt_server.server"):
        await _drive_with_trimmed_gate(state, monkeypatch, decl_registries)

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
async def test_discovery_expand_noop_when_gate_untrimmed(_stub_search, monkeypatch):
    """With the FULL registry visible (no enforce), the discovered tools are
    already present -> no rebuild, no cap consumed (a clean no-op)."""
    monkeypatch.delenv("TRID3NT_TOOL_RETRIEVAL", raising=False)
    decl_registries: list[set] = []

    rounds = {"n": 0}

    def _script(**kwargs):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return iter([_fc_chunk("search_tools", {"query": "flood"}, "c1")])
        return iter([_text_chunk("done")])

    def _capture_decls(registry):
        decl_registries.append(set(registry.keys()))
        return []

    state = agent_server.SessionState(session_id=new_ulid())
    sock = _FakeSocket()
    with patch.object(agent_server, "build_client", return_value=MagicMock()), patch.object(
        agent_server, "build_tool_declarations", side_effect=_capture_decls
    ):
        agent_server.build_client.return_value.models.generate_content_stream.side_effect = (
            lambda **kw: _script(**kw)
        )
        await agent_server._stream_gemini_reply(
            sock, state, _settings(), "find me flood tools", "research"
        )
    # Full registry already contains the discovered tools -> the only build is
    # the pre-loop one (no dirty rebuild).
    assert len(decl_registries) == 1, "untrimmed gate must not rebuild declarations"
