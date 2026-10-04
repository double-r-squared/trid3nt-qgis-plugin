"""Provider and model selection, independent of any one provider adapter.

Both the provider name and a client-supplied model id are resolved at call
time; nothing here is cached at import.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: Display and telemetry label when the active provider resolves none of its own.
DEFAULT_MODEL_LABEL = "gemini-2.5-pro"


@dataclass(frozen=True)
class ModelSettings:
    """Resolved model configuration: the display and telemetry id used when the
    active provider resolves no model of its own."""

    model: str


def load_settings() -> ModelSettings:
    """Resolve model settings from the environment.
    ``TRID3NT_GEMINI_MODEL`` sets only the display and telemetry label; the
    active provider resolves the real model it calls."""
    return ModelSettings(
        model=os.environ.get("TRID3NT_GEMINI_MODEL", DEFAULT_MODEL_LABEL),
    )


def model_provider() -> str:
    """Active model provider from ``MODEL_PROVIDER``; ``openai`` by default."""
    return (os.environ.get("MODEL_PROVIDER") or "openai").strip().lower()


def resolve_selected_model(requested: str | None) -> tuple[str | None, str | None]:
    """Validate a requested model id -> ``(effective_id, notice)``.
    An effective id of ``None`` means the server default; any other id passes
    through verbatim, and ``notice`` is set only on a fall-back."""
    # ``"local-default"`` is the client's placeholder for "no explicit choice":
    # not a model id, and it must never reach a provider API.
    if requested is None or requested == "local-default":
        return None, None
    return requested, None
