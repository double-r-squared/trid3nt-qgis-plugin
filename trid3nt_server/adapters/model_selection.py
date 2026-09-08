"""Provider and model selection, independent of any one provider adapter.

``MODEL_PROVIDER`` picks the adapter in the dispatch seam
(``adapter.stream_events_with_contents``). A per-turn model id arrives from the
client and is validated here before it can reach a provider API.
"""

from __future__ import annotations

import os


def model_provider() -> str:
    """Resolve the active model provider (``openai`` default).

    Read at call time so an env injection takes effect without re-import.
    """
    return (os.environ.get("MODEL_PROVIDER") or "openai").strip().lower()


def resolve_selected_model(requested: str | None) -> tuple[str | None, str | None]:
    """Validate a user-requested model id against the active provider.

    Returns ``(effective_model_id, notice)`` where ``effective_model_id`` is
    ``requested`` when the provider can serve it and ``None`` means "use the
    server default"; ``notice`` is a short user-facing sentence when a
    fall-back happened, else ``None``.

    The id passes through verbatim: the provider validates it itself (a wrong
    id is the provider's own honest error), and each adapter's model resolver
    ignores an id shaped for another provider. The legacy ``"local-default"``
    placeholder means "use the server default".
    """
    if requested is None or requested == "local-default":
        return None, None
    return requested, None
