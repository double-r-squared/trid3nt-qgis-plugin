"""Provider model-discovery: the installed-Ollama and OpenRouter free/tool-capable
model listings, the Ollama API-root/tags URL derivation, and the per-provider
CONTEXT-WINDOW resolvers. Provider nouns are quarantined here, off the
protocol/gates surfaces that consume them.

The window resolvers below each answer ONE question -- "how many input tokens
does this model accept?" -- from that provider's own metadata, and return
``None`` (never a guess) when the provider does not say. The ladder that
consumes them, the cache, and the honest fallback all live in
``gates.context_budget.discover_context_window``; the strategy is NOT
per-adapter.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("trid3nt_server.adapters.model_discovery")


def _local_models_route_enabled() -> bool:
    """The /api/local-models route exists only for the OpenAI-compatible provider."""
    try:
        from .bedrock_adapter import model_provider

        return model_provider() == "openai"
    except Exception:  # noqa: BLE001 -- import fault -> route absent
        return False


def _ollama_root(base_url: str | None) -> str:
    """Strip a trailing OpenAI-compat ``/v1`` mount to reach Ollama's native API
    root (``TRID3NT_OPENAI_BASE_URL`` is typically ``http://host:11434/v1``; the
    native ``/api/*`` endpoints live at the bare root)."""
    root = (base_url or "").rstrip("/")
    if root.lower().endswith("/v1"):
        root = root[: -len("/v1")]
    return root


def _ollama_tags_url() -> str:
    """Derive the Ollama ``/api/tags`` URL from the agent's own LLM endpoint.

    ``TRID3NT_OPENAI_BASE_URL`` is the OpenAI-compatible base the adapter dials
    (e.g. ``http://127.0.0.1:11434/v1``); the native Ollama API lives one level
    up. Falls back to the Ollama default host when the env is unset.
    """
    base = _ollama_root(os.environ.get("TRID3NT_OPENAI_BASE_URL", "").strip())
    if not base:
        base = "http://127.0.0.1:11434"
    return f"{base}/api/tags"


class _LocalModelsUpstreamError(Exception):
    """Ollama /api/tags (or OpenRouter /models) unreachable or unusable."""


# OpenRouter ``GET /models`` is large (~300 entries) and rarely changes; cache the
# FILTERED result per base_url for a process TTL so a provider-change repopulate
# does not re-fetch every open. A restart (or a different base_url key) naturally
# bypasses staleness -- there is no explicit invalidation, by design.
_OPENROUTER_MODELS_TTL_S = 600.0
_OPENROUTER_MODELS_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}


def _base_url_host(base_url: str) -> str:
    """Lowercased host of an OpenAI-compatible base URL ("" when unparsable)."""
    from urllib.parse import urlsplit

    return (urlsplit(base_url).hostname or "").lower()


def _filter_openrouter_models(raw: Any) -> list[dict[str, str]]:
    """PURE: an OpenRouter ``GET /models`` body -> ``[{"id","label"}]`` of the
    FREE, TOOL-CAPABLE models only.

      FREE          = ``pricing.prompt == "0"`` AND ``pricing.completion == "0"``
                      OR the id ends with ``:free``.
      TOOL-CAPABLE  = ``"tools"`` present in ``supported_parameters``. A model
                      MISSING ``supported_parameters`` is kept OUT of the free
                      list (to be safe -- the agent is tool_choice=auto every
                      round; a model that cannot honor tools narrates a fake
                      answer, the design's top risk).

    Never raises on absent / oddly-typed keys -- a malformed row is skipped,
    never fatal, so one bad entry cannot blank the whole list.
    """
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for m in data:
        if not isinstance(m, dict):
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not mid.strip():
            continue
        mid = mid.strip()
        pricing = m.get("pricing")
        prompt_free = completion_free = False
        if isinstance(pricing, dict):
            prompt_free = pricing.get("prompt") == "0"
            completion_free = pricing.get("completion") == "0"
        is_free = (prompt_free and completion_free) or mid.endswith(":free")
        if not is_free:
            continue
        supported = m.get("supported_parameters")
        if not isinstance(supported, list) or "tools" not in supported:
            continue
        label = mid if mid.endswith(":free") else f"{mid} (free)"
        out.append({"id": mid, "label": label})
    return out


def _fetch_openrouter_models(base_url: str) -> bytes:
    """SYNC (httpx): OpenRouter free + tool-capable models in the SAME shape the
    Ollama branch returns -- ``{"models":[{"id","label"}], "default": ...}``.

    Sends the configured provider key as a ``Bearer`` header (reuses
    ``openai_adapter.openai_api_key()``); the key is NEVER logged. Result is
    cached per ``base_url`` with a process TTL (see ``_OPENROUTER_MODELS_CACHE``).
    Raises ``_LocalModelsUpstreamError`` on any upstream fault -> honest 502.
    """
    import time

    import httpx

    from .openai_adapter import openai_api_key

    now = time.monotonic()
    cached = _OPENROUTER_MODELS_CACHE.get(base_url)
    if cached is not None and (now - cached[0]) < _OPENROUTER_MODELS_TTL_S:
        models = cached[1]
    else:
        url = f"{base_url.rstrip('/')}/models"
        headers: dict[str, str] = {}
        key = openai_api_key()
        if key and key != "not-needed":
            headers["Authorization"] = f"Bearer {key}"
        try:
            with httpx.Client(timeout=8.0) as client:
                resp = client.get(url, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001 -- unreachable / 4xx / non-JSON
            # NB: the message carries only the URL (host + path), never the key.
            raise _LocalModelsUpstreamError(
                f"OpenRouter model list unreachable at {url}: {exc}"
            ) from exc
        models = _filter_openrouter_models(payload)
        _OPENROUTER_MODELS_CACHE[base_url] = (now, models)

    default = os.environ.get("TRID3NT_OPENAI_MODEL", "").strip() or None
    # Configured default first, so a client picking entry 0 gets the served
    # model. Build a NEW list -- never mutate the cached list in place.
    ordered = list(models)
    if default is not None:
        for i, m in enumerate(ordered):
            if m["id"] == default:
                ordered.insert(0, ordered.pop(i))
                break
    return json.dumps(
        {"models": ordered, "default": default}, separators=(",", ":")
    ).encode("utf-8")


def _fetch_local_models() -> bytes:
    """SYNC (httpx; caller wraps in ``asyncio.to_thread``): build the JSON body.

    Branches on the configured provider base URL: an ``openrouter.ai`` host
    lists the FREE + tool-capable OpenRouter models; any other base URL (local
    Ollama) lists the installed Ollama models. Raises
    ``_LocalModelsUpstreamError`` on any upstream fault so the handler emits an
    honest 502 -- never a fabricated empty success.
    """
    import httpx

    base = os.environ.get("TRID3NT_OPENAI_BASE_URL", "").strip()
    if base and _base_url_host(base).endswith("openrouter.ai"):
        return _fetch_openrouter_models(base)

    url = _ollama_tags_url()
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001 -- unreachable / non-JSON / 5xx
        raise _LocalModelsUpstreamError(
            f"local model runtime unreachable at {url}: {exc}"
        ) from exc

    raw_models = payload.get("models") if isinstance(payload, dict) else None
    models: list[dict[str, str]] = []
    if isinstance(raw_models, list):
        for m in raw_models:
            if not isinstance(m, dict):
                continue
            name = m.get("name") or m.get("model")
            if isinstance(name, str) and name.strip():
                name = name.strip()
                models.append({"id": name, "label": name})
    default = os.environ.get("TRID3NT_OPENAI_MODEL", "").strip() or None
    # Configured default first, so a client that picks entry 0 gets the model
    # the agent would serve anyway.
    if default is not None:
        for i, m in enumerate(models):
            if m["id"] == default:
                models.insert(0, models.pop(i))
                break
    return json.dumps(
        {"models": models, "default": default}, separators=(",", ":")
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Per-provider CONTEXT-WINDOW resolvers
#
# Each returns the model's INPUT-token capacity, or None when the provider
# exposes no such fact. None means "undiscoverable", NEVER "assume a default"
# -- the caller (gates.context_budget.discover_context_window) owns the
# fallback and the warning that goes with it.
# ---------------------------------------------------------------------------


def is_openrouter_base_url(base_url: str | None) -> bool:
    """True when an OpenAI-compatible base URL points at OpenRouter."""
    return bool(base_url) and _base_url_host(base_url or "").endswith("openrouter.ai")


def parse_openrouter_context_length(raw: Any, model_name: str) -> int | None:
    """PURE: an OpenRouter ``GET /models`` body -> ``context_length`` for
    ``model_name``, or None when the id is absent or the field is unusable.

    OpenRouter publishes ``context_length`` at the top level of each model row
    (and mirrors it under ``top_provider.context_length``, which can be SMALLER
    when the routed upstream provider serves a shorter window). We take the
    MINIMUM of the two present values -- the smaller number is the one a
    request actually has to fit inside.
    """
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data, list):
        return None
    for row in data:
        if not isinstance(row, dict) or row.get("id") != model_name:
            continue
        candidates: list[int] = []
        for value in (
            row.get("context_length"),
            (row.get("top_provider") or {}).get("context_length")
            if isinstance(row.get("top_provider"), dict)
            else None,
        ):
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value > 0:
                candidates.append(value)
        return min(candidates) if candidates else None
    return None


async def openrouter_context_length(base_url: str, model_name: str) -> int | None:
    """OpenRouter ``GET /models`` -> this model's ``context_length``.

    Unfiltered (unlike ``_fetch_openrouter_models``, which keeps only the free
    tool-capable subset for the picker) -- a paid or non-tool model still needs
    an honest window. Best-effort: any network / parse fault returns None.
    """
    import httpx

    from .openai_adapter import openai_api_key

    url = f"{base_url.rstrip('/')}/models"
    headers: dict[str, str] = {}
    key = openai_api_key()
    if key and key != "not-needed":
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
    except Exception:  # noqa: BLE001 -- discovery is best-effort
        # NB: never log the key -- only the URL.
        logger.debug("context-window: OpenRouter /models failed at %s", url, exc_info=True)
        return None
    return parse_openrouter_context_length(payload, model_name)


def parse_anthropic_max_input_tokens(model_obj: Any) -> int | None:
    """PURE: an Anthropic Models API model object -> ``max_input_tokens``.

    ``max_input_tokens`` IS the context window on this API (there is no
    ``context_window`` field). It was added to the model object in Mar 2026, so
    an older API surface -- or a partner platform that proxies a trimmed object
    -- can legitimately omit it; that returns None.
    """
    value = getattr(model_obj, "max_input_tokens", None)
    if value is None and isinstance(model_obj, dict):
        value = model_obj.get("max_input_tokens")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


async def anthropic_max_input_tokens(model_id: str) -> int | None:
    """Anthropic ``GET /v1/models/{id}`` -> ``max_input_tokens``.

    Best-effort: a missing SDK, an auth fault, an unknown id, or an object
    without the field all return None.
    """
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        return None
    try:
        client = AsyncAnthropic()
        model_obj = await client.models.retrieve(model_id)
    except Exception:  # noqa: BLE001 -- discovery is best-effort
        logger.debug("context-window: anthropic models.retrieve failed for %r", model_id, exc_info=True)
        return None
    return parse_anthropic_max_input_tokens(model_obj)
