"""One effective-model resolve serves the server's startup line and every turn:
the active provider's own resolution, else the request, else the provider."""
from __future__ import annotations

from trid3nt_server.server.turn.stream import _effective_model_id


def test_openai_takes_the_request_else_the_configured_model(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    monkeypatch.setenv("TRID3NT_OPENAI_MODEL", "llama3.2:3b")
    assert _effective_model_id(None) == "llama3.2:3b"
    assert _effective_model_id("gpt-4o") == "gpt-4o"


def test_anthropic_ignores_an_id_shaped_for_another_provider(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("TRID3NT_ANTHROPIC_MODEL", "claude-test")
    assert _effective_model_id("gpt-4o") == "claude-test"
    assert _effective_model_id("claude-other") == "claude-other"


def test_another_provider_names_the_request_or_itself(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "ollama")
    assert _effective_model_id(None) == "ollama"
    assert _effective_model_id("m") == "m"
