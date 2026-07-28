"""ENGINE-DOOR gating (engine-door refactor, docs/specs/engine-door-refactor.md).

Two reused seams, no parallel gate:

(a) EXCLUSION - a ``tier="template"`` tool is EXCLUDED from the default
    retrieval pool: it never enters the discover index ``tool_names``, so
    ``retrieve_visible_tools`` / ``retrieve_ranked_tools`` can never surface it,
    and the FAIL-OPEN floor also filters it out. A ``tier="door"`` tool is NOT
    excluded (doors compete in per-turn retrieval).

(b) EXPANSION - calling an engine door widens the turn's visible gate with the
    door's ``templates[].tool_name`` list, reusing the discovery-expands-gate
    seam, but under the larger ``_DOOR_EXPAND_CAP`` (a door lists a closed
    curated set, so select-then-call is never truncated at the +8 discovery cap).

(c) NON-TEMPLATE behaviour is unchanged - a general tool still indexes and
    retrieves; the door itself ranks like any pool member.

Offline: registry is mutated in-process; the index is rebuilt from the live
registry; no network. Templates are registered/removed per test.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from trid3nt_server import server as agent_server
from trid3nt_server.agent import tools as agent_tools
from trid3nt_server.agent.adapters.adapter import ModelSettings
from trid3nt_server.agent.categories import HOT_SET_TOOLS
from trid3nt_server.agent.tools import RegisteredTool
from trid3nt_server.agent.tools.search import search_tools as st_pkg
from trid3nt_server.agent.tools.search.search_tools import search_tools as st
from trid3nt_server.agent.tools.search import tool_retrieval as tr
from trid3nt_server.emission.uri_registry import reset_uri_registries_for_tests
from trid3nt_contracts import new_ulid
from trid3nt_contracts.tool_registry import AtomicToolMetadata


def _template_meta(name: str) -> AtomicToolMetadata:
    return AtomicToolMetadata(
        name=name,
        ttl_class="live-no-cache",
        cacheable=False,
        source_class="workflow_dispatch",
        engine="modflow",
        tier="template",
    )


def _register_template(name: str) -> None:
    def _fn(well_location_latlon, pumping_rate_m3d, duration_days=365):
        """A registered MODFLOW template (excluded from the default pool)."""
        return {}

    agent_tools.TOOL_REGISTRY[name] = RegisteredTool(
        metadata=_template_meta(name), fn=_fn, module=__name__
    )


@pytest.fixture()
def one_template():
    """Register ONE modflow template; rebuild the index; clean up."""
    name = "modflow_capture_zone"
    existed = agent_tools.TOOL_REGISTRY.get(name)
    _register_template(name)
    st._reset_index_for_tests()
    st._get_index()  # sets search_tools._INDEX from the live registry
    try:
        yield name
    finally:
        if existed is not None:
            agent_tools.TOOL_REGISTRY[name] = existed
        else:
            agent_tools.TOOL_REGISTRY.pop(name, None)
        st._reset_index_for_tests()


# ---------------------------------------------------------------------------
# (a) EXCLUSION.
# ---------------------------------------------------------------------------


def test_template_absent_from_index_tool_names(one_template):
    idx = st._get_index()
    assert one_template not in idx.tool_names, (
        "a tier=template tool must be EXCLUDED from the discover index"
    )
    # (c) the door and a general tool are present (not excluded).
    assert "run_modflow" in idx.tool_names, "the door (tier=door) must stay in the pool"
    assert "fetch_dem" in idx.tool_names, "a general tool must still be indexed"


def test_template_never_surfaces_in_retrieval(one_template):
    st._get_index()  # warm
    vis = tr.retrieve_visible_tools("capture zone for a pumping well", None, k=30)
    assert one_template not in vis, "retrieve_visible_tools must never surface a template"
    ranked = dict(tr.retrieve_ranked_tools("capture zone for a pumping well", k=60))
    assert one_template not in ranked, "retrieve_ranked_tools must never surface a template"


def test_door_ranks_in_retrieval(one_template):
    st._get_index()  # warm
    ranked = dict(tr.retrieve_ranked_tools("model a groundwater contamination plume", k=60))
    assert "run_modflow" in ranked, "the door must compete in per-turn retrieval"


def test_fail_open_floor_excludes_templates_keeps_door(one_template):
    floor = tr._full_registry_floor(set())
    assert one_template not in floor, "fail-open floor must not re-leak a template"
    assert "run_modflow" in floor, "fail-open floor must keep the door"
    assert "fetch_dem" in floor, "fail-open floor must keep general tools"
    # A template ALREADY surfaced (in the Case's accrued set) is preserved.
    floor2 = tr._full_registry_floor({one_template})
    assert one_template in floor2, "an already-surfaced template must be preserved"


# ---------------------------------------------------------------------------
# (b) EXPANSION - pure helpers.
# ---------------------------------------------------------------------------


def test_engine_door_recognized_as_gate_expander():
    assert "run_modflow" in agent_server._engine_door_tool_names()
    gx = agent_server._gate_expander_tool_names()
    assert "run_modflow" in gx, "a door must be a gate-expander"
    assert "search_tools" in gx, "the tool-search tool stays a gate-expander"


def test_extract_names_from_templates_list():
    door_env = {
        "engine": "modflow",
        "kind": "engine_door",
        "templates": [
            {"tool_name": "modflow_capture_zone"},
            {"tool_name": "modflow_contaminant_plume"},
            {"tool_name": "modflow_capture_zone"},  # dup -> dropped
            {"score": 1},  # no name -> skipped
        ],
    }
    assert agent_server._tool_names_from_search_result(door_env) == [
        "modflow_capture_zone",
        "modflow_contaminant_plume",
    ]


def test_results_preferred_then_templates_fallback():
    # results wins when present + non-empty.
    both = {"results": [{"tool_name": "a"}], "templates": [{"tool_name": "b"}]}
    assert agent_server._tool_names_from_search_result(both) == ["a"]
    # empty results falls back to templates.
    fb = {"results": [], "templates": [{"tool_name": "b"}]}
    assert agent_server._tool_names_from_search_result(fb) == ["b"]


def test_door_cap_exceeds_discovery_cap():
    assert agent_server._DOOR_EXPAND_CAP >= 24
    assert agent_server._DOOR_EXPAND_CAP > agent_server._DISCOVERY_EXPAND_CAP


# ---------------------------------------------------------------------------
# (b) EXPANSION - live loop (door call surfaces its templates past the +8 cap).
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


def _settings() -> ModelSettings:
    return ModelSettings(
        model="gemini-2.5-pro", project="t", location="us-central1", use_vertex=True
    )


@pytest.fixture()
def eleven_templates():
    """Register 11 modflow templates (> the +8 discovery cap) + clean up."""
    names = [f"modflow_tmpl_{i:02d}" for i in range(11)]
    existed = {n: agent_tools.TOOL_REGISTRY.get(n) for n in names}
    reset_uri_registries_for_tests()
    for n in names:
        _register_template(n)
    st._reset_index_for_tests()
    try:
        yield names
    finally:
        for n in names:
            if existed[n] is not None:
                agent_tools.TOOL_REGISTRY[n] = existed[n]
            else:
                agent_tools.TOOL_REGISTRY.pop(n, None)
        st._reset_index_for_tests()
        reset_uri_registries_for_tests()


async def _drive_door_call(state, monkeypatch, decl_registries):
    """Enforce mode trims the gate to {hot set + run_modflow}; round 1 calls the
    REAL run_modflow door; capture the registry keys handed to
    build_tool_declarations each build so the rebuild is observable."""
    monkeypatch.setenv("TRID3NT_TOOL_RETRIEVAL", "enforce")
    visible = set(HOT_SET_TOOLS) | {"run_modflow"}
    monkeypatch.setattr(
        "trid3nt_server.agent.tools.search.tool_retrieval.retrieve_visible_tools",
        lambda *_a, **_k: set(visible),
    )

    rounds = {"n": 0}

    def _script(**kwargs):
        rounds["n"] += 1
        if rounds["n"] == 1:
            return iter([_fc_chunk("run_modflow", {}, "c1")])
        return iter([_text_chunk("Here are the groundwater templates.")])

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
        await agent_server._stream_model_reply(
            sock, state, _settings(), "model a groundwater contamination plume", "research"
        )
    return sock


@pytest.mark.asyncio
async def test_door_call_surfaces_all_templates_past_discovery_cap(
    eleven_templates, monkeypatch, caplog
):
    names = eleven_templates
    assert len(names) > agent_server._DISCOVERY_EXPAND_CAP, "need > +8 templates"
    decl_registries: list[set] = []
    state = agent_server.SessionState(session_id=new_ulid())
    with caplog.at_level(logging.INFO, logger="trid3nt_server.server"):
        await _drive_door_call(state, monkeypatch, decl_registries)

    assert len(decl_registries) >= 2, "expected a rebuild after the door round"
    pre_loop = decl_registries[0]
    rebuilt = decl_registries[-1]

    # (a) templates started OUT of the gate (excluded from the pool).
    assert not (set(names) & pre_loop), "templates must start OUT of the trimmed gate"
    # (b) ALL 11 templates were unioned in by the door -> the door cap, not +8.
    added = set(names) & rebuilt
    assert added == set(names), (
        f"door must surface ALL {len(names)} templates (past the +8 discovery cap); "
        f"got {len(added)}: {sorted(added)}"
    )
    assert pre_loop <= rebuilt, "the gate only grows (union, never shrink)"
    assert any(
        "door-expand: +" in r.message for r in caplog.records
    ), "the door expansion must be logged under the door label"
