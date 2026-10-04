"""``resolve_selected_model`` validates the per-turn model id the client sends."""

from __future__ import annotations

from trid3nt_server.adapters import model_selection as ms


def test_resolve_none_is_silent_default(monkeypatch):
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    assert ms.resolve_selected_model(None) == (None, None)


def test_resolve_openai_provider_passes_local_id_verbatim(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    assert ms.resolve_selected_model("qwen3:8b-16k") == ("qwen3:8b-16k", None)


def test_resolve_openai_provider_local_default_placeholder_maps_to_default(
    monkeypatch,
):
    """The legacy 'local-default' web placeholder = 'use the server default'."""
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    assert ms.resolve_selected_model("local-default") == (None, None)


def test_resolve_openai_provider_none_still_silent(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    assert ms.resolve_selected_model(None) == (None, None)


def test_resolve_openai_provider_foreign_id_passes_through_to_adapter_guard(
    monkeypatch,
):
    """An id shaped for another provider passes through ``resolve``.

    ``openai_adapter.openai_model`` ignores it and falls back to the configured
    model, so the guard lives at the adapter boundary."""
    monkeypatch.setenv("MODEL_PROVIDER", "openai")
    got, notice = ms.resolve_selected_model("us.anthropic.claude-sonnet-4-6")
    assert got == "us.anthropic.claude-sonnet-4-6"
    assert notice is None
