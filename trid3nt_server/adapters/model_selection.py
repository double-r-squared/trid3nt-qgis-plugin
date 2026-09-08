"""Provider and model selection, independent of any one provider adapter.

``MODEL_PROVIDER`` picks the adapter in the dispatch seam
(``adapter.stream_events_with_contents``). A per-turn model id arrives from the
client and is validated here before it can reach a provider API, so a stale or
unsupported id becomes an honest fall-back to the server default rather than a
raw provider validation error.
"""

from __future__ import annotations

import os


def model_provider() -> str:
    """Resolve the active model provider (``bedrock`` default).

    Read at call time so an env injection takes effect without re-import.
    """
    return (os.environ.get("MODEL_PROVIDER") or "bedrock").strip().lower()


def resolve_selected_model(requested: str | None) -> tuple[str | None, str | None]:
    """Validate a user-requested model id against the active provider.

    Returns ``(effective_model_id, notice)`` where:
      - ``effective_model_id`` is ``requested`` when the provider can serve it,
        else ``None`` (meaning "use the server default").
      - ``notice`` is ``None`` on the happy path, or a short, user-facing
        sentence explaining the fall-back.

    ``requested is None`` is the normal "no explicit choice" case and returns
    ``(None, None)`` -- silent default, no notice.

    On the openai and anthropic paths the id passes through verbatim: the
    provider validates it itself (a wrong id is the provider's own honest
    error), and each adapter's model resolver ignores an id shaped for another
    provider. The legacy ``"local-default"`` placeholder means "use the server
    default". The bedrock path gates on its own allowlist instead, because an
    unlisted id reaches ConverseStream as a raw ValidationException.
    """
    if requested is None:
        return None, None
    if model_provider() == "anthropic":
        return (None, None) if requested == "local-default" else (requested, None)
    if model_provider() == "openai":
        if requested == "local-default":
            return None, None
        return requested, None
    from .bedrock_adapter import SELECTABLE_MODEL_IDS

    if requested in SELECTABLE_MODEL_IDS:
        return requested, None
    return (
        None,
        (
            f"The requested model '{requested}' is not available, so this turn "
            "is running on the default model."
        ),
    )
