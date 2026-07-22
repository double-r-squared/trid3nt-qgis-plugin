"""Bedrock prompt-caching (cachePoint) restored — the AWS bill fix (2026-06-17).

The sprint-14 Gemini->Bedrock swap DEFERRED prompt caching, so every agent turn
re-sent the full static system prompt + 94-tool catalog UNCACHED — the #1 Bedrock
cost driver. These assert ``_build_converse_kwargs`` inserts cachePoint markers
(default ON) on the system block + the tool list, and that the off-switch works.
"""

from __future__ import annotations

import pytest

from grace2_agent import bedrock_adapter as ba


_SYS = "You are TRID3NT, a hazard-modeling agent. " * 50  # large static system prompt

# A converted Bedrock tool spec (what tool_declarations_to_bedrock_tools returns).
_BEDROCK_TOOL = {"toolSpec": {"name": "fetch_dem", "inputSchema": {"json": {}}}}


@pytest.fixture(autouse=True)
def _stub_converters(monkeypatch):
    """Isolate the NEW cachePoint logic from the pre-existing genai->Bedrock
    converters (which expect contract objects, not raw dicts)."""
    monkeypatch.setattr(ba, "contents_to_bedrock_messages", lambda c: ([], []))
    monkeypatch.setattr(ba, "tool_declarations_to_bedrock_tools", lambda t: [dict(_BEDROCK_TOOL)] if t else [])


def _tools():
    return ["<one declaration>"]  # truthy; converter is stubbed to emit _BEDROCK_TOOL


def _last_is_cachepoint(items) -> bool:
    return isinstance(items[-1], dict) and "cachePoint" in items[-1]


def test_cachepoint_on_system_and_tools_by_default(monkeypatch):
    monkeypatch.delenv("BEDROCK_PROMPT_CACHE", raising=False)  # default = ON
    kw = ba._build_converse_kwargs([], _tools(), _SYS, None)

    # system block ends with a cachePoint (caches the static system prompt)
    assert "system" in kw
    assert _last_is_cachepoint(kw["system"])
    assert kw["system"][0] == {"text": _SYS}

    # tool list ends with a cachePoint (caches the static tool catalog)
    tools = kw["toolConfig"]["tools"]
    assert _last_is_cachepoint(tools)
    # the real tool spec still precedes the cachePoint
    assert any("toolSpec" in t for t in tools if isinstance(t, dict))


def test_cachepoint_disabled_by_env(monkeypatch):
    monkeypatch.setenv("BEDROCK_PROMPT_CACHE", "0")
    kw = ba._build_converse_kwargs([], _tools(), _SYS, None)
    assert not _last_is_cachepoint(kw["system"])
    assert not _last_is_cachepoint(kw["toolConfig"]["tools"])
    # and the tools/system content is otherwise intact
    assert kw["system"] == [{"text": _SYS}]


def test_no_system_no_empty_cachepoint(monkeypatch):
    monkeypatch.delenv("BEDROCK_PROMPT_CACHE", raising=False)
    kw = ba._build_converse_kwargs([], [], None, None)
    # no system prompt -> no system key (never emit a lone cachePoint)
    assert "system" not in kw
    # no tools -> no toolConfig
    assert "toolConfig" not in kw


@pytest.mark.parametrize("flag,expect", [("1", True), ("true", True), ("off", False), ("no", False)])
def test_env_flag_parsing(monkeypatch, flag, expect):
    monkeypatch.setenv("BEDROCK_PROMPT_CACHE", flag)
    kw = ba._build_converse_kwargs([], _tools(), _SYS, None)
    assert _last_is_cachepoint(kw["toolConfig"]["tools"]) is expect
