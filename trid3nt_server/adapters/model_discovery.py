"""Provider model listings and per-provider CONTEXT-WINDOW resolvers.

Provider nouns are quarantined here; a resolver returns ``None``, never a
guess, when the provider publishes no such fact.
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
        from .model_selection import model_provider

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
    The native API lives one level above the OpenAI-compat base; falls back to
    the Ollama default host when ``TRID3NT_OPENAI_BASE_URL`` is unset."""
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
    """PURE: an OpenRouter ``GET /models`` body -> ``[{"id","label"}]``, the
    FREE and TOOL-CAPABLE models only.
    A malformed row is skipped, never fatal: one bad entry cannot blank the list."""
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
        # FREE = ``pricing.prompt`` and ``pricing.completion`` both "0", or an
        # id ending in ``:free``.
        pricing = m.get("pricing")
        prompt_free = completion_free = False
        if isinstance(pricing, dict):
            prompt_free = pricing.get("prompt") == "0"
            completion_free = pricing.get("completion") == "0"
        is_free = (prompt_free and completion_free) or mid.endswith(":free")
        if not is_free:
            continue
        # TOOL-CAPABLE = ``"tools"`` in ``supported_parameters``. A row MISSING
        # that field is kept OUT: the agent is tool_choice=auto every round, and
        # a model that cannot honor tools narrates a fake answer.
        supported = m.get("supported_parameters")
        if not isinstance(supported, list) or "tools" not in supported:
            continue
        label = mid if mid.endswith(":free") else f"{mid} (free)"
        out.append({"id": mid, "label": label})
    return out


def _fetch_openrouter_models(base_url: str) -> bytes:
    """SYNC (httpx): free + tool-capable OpenRouter models, in the Ollama shape
    ``{"models":[{"id","label"}], "default": ...}``.
    An upstream fault raises ``_LocalModelsUpstreamError``, never an empty list."""
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
        # The configured provider key rides a ``Bearer`` header; NEVER logged.
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
    """SYNC (httpx; the caller wraps it in ``asyncio.to_thread``): the JSON body.
    An ``openrouter.ai`` base lists OpenRouter's free tool-capable models, any
    other lists the installed Ollama models; an upstream fault raises."""
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


# Per-provider CONTEXT-WINDOW resolvers
#
# Each returns the model's INPUT-token capacity, or None when the provider
# exposes no such fact. None means "undiscoverable", NEVER "assume a default"
# -- the caller owns the fallback and the warning that goes with it.


def is_openrouter_base_url(base_url: str | None) -> bool:
    """True when an OpenAI-compatible base URL points at OpenRouter."""
    return bool(base_url) and _base_url_host(base_url or "").endswith("openrouter.ai")


def parse_openrouter_context_length(raw: Any, model_name: str) -> int | None:
    """PURE: an OpenRouter ``GET /models`` body -> ``context_length`` for
    ``model_name``, or ``None`` when the id is absent or the field is unusable."""
    data = raw.get("data") if isinstance(raw, dict) else None
    if not isinstance(data, list):
        return None
    for row in data:
        if not isinstance(row, dict) or row.get("id") != model_name:
            continue
        # ``top_provider.context_length`` mirrors the top-level field and can
        # be SMALLER when the routed upstream serves a shorter window, so the
        # MINIMUM of the two is what a request actually has to fit inside.
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
    Unfiltered, so a paid or non-tool model still gets an honest window;
    best-effort, and any network or parse fault returns ``None``."""
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
    That field IS the context window on this API; an older surface or a trimmed
    proxied object can legitimately omit it, which returns ``None``."""
    value = getattr(model_obj, "max_input_tokens", None)
    if value is None and isinstance(model_obj, dict):
        value = model_obj.get("max_input_tokens")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


async def anthropic_max_input_tokens(model_id: str) -> int | None:
    """Anthropic ``GET /v1/models/{id}`` -> ``max_input_tokens``.
    Best-effort: a missing SDK, an auth fault, an unknown id or an object
    without the field all return ``None``."""
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


class ProviderConfigBadRequest(Exception):
    """POST /api/provider-config body was malformed. SECURITY: the message
    NEVER echoes the request body or the api_key -- only field-shape complaints
    (a malformed body could itself be a mistyped key)."""


class ProviderConfigIncoherent(ProviderConfigBadRequest):
    """base_url and model name DIFFERENT providers, so the push is refused with
    the env left exactly as it was. The message may name the base URL HOST and
    the model id, never the full URL or the api_key."""


#: Ollama's fixed listen port. The ONLY signal that an OpenAI-compatible
#: endpoint is Ollama rather than vLLM / llama.cpp / LM Studio, which also serve
#: on loopback but accept HuggingFace-style ``vendor/model`` ids -- so a bare
#: localhost host must NEVER be read as Ollama.
_OLLAMA_PORT = 11434

#: Live /api/tags probe budget. The dock's post_provider_config timeout is 5s;
#: this must stay far under it so a slow endpoint never stalls Save.
_OLLAMA_PROBE_TIMEOUT_S = 1.5


def _provider_family(base_url: str) -> str | None:
    """The provider family for an OpenAI-compatible base URL, or ``None`` when
    the endpoint has no fixed model-id convention; only families whose ids are
    UNAMBIGUOUS are named, and everything else is never gated."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(base_url)
        port = parts.port
    except ValueError:  # unparsable authority -> no identity, no gate
        return None
    if (parts.hostname or "").lower().endswith("openrouter.ai"):
        return "openrouter"
    if port == _OLLAMA_PORT:
        return "ollama"
    return None


def _ollama_serves_model(base_url: str, model: str) -> bool | None:
    """SYNC live probe of the installed-model list: True or False when the
    endpoint answers, ``None`` when it cannot be reached. ``None`` must never be
    read as incoherence - only a non-empty answer can prove a model absent."""
    import httpx

    root = _ollama_root(base_url)
    if not root:
        return None
    try:
        with httpx.Client(timeout=_OLLAMA_PROBE_TIMEOUT_S) as client:
            resp = client.get(f"{root}/api/tags")
            resp.raise_for_status()
            payload = resp.json()
    except Exception:  # noqa: BLE001 -- unreachable / slow / non-JSON -> unknown
        return None
    raw = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return None

    def _norm(name: str) -> str:
        return name[: -len(":latest")] if name.endswith(":latest") else name

    installed = {
        _norm(m["name"].strip())
        for m in raw
        if isinstance(m, dict)
        and isinstance(m.get("name"), str)
        and m["name"].strip()
    }
    if not installed:
        return None
    return _norm(model) in installed


def _check_provider_coherence(base_url: str, model: str) -> None:
    """Refuse when base_url and model belong to DIFFERENT providers. Called
    BEFORE any env mutation, on the RESOLVED pair rather than per field."""
    # A client saves fields independently, so a base-URL-only push can strand a
    # model id from the previous provider and leave the daemon dialling an
    # endpoint that does not serve it. Static identification is the gate; the
    # live probe runs only for the one shape static form cannot settle, and an
    # unreachable probe never rejects.
    family = _provider_family(base_url)
    if family is None or not model:
        return
    host = _base_url_host(base_url)
    if family == "openrouter" and "/" not in model:
        raise ProviderConfigIncoherent(
            f"provider mismatch: base_url host {host!r} is OpenRouter but model "
            f"{model!r} is not an OpenRouter id. OpenRouter ids are namespaced "
            "'vendor/model' (e.g. 'meta-llama/llama-3.3-70b-instruct:free'). "
            "Set base_url and model to the same provider."
        )
    if family == "ollama":
        if model.endswith(":free"):
            raise ProviderConfigIncoherent(
                f"provider mismatch: base_url host {host!r} is Ollama but model "
                f"{model!r} is an OpenRouter free-tier id. Expected an installed "
                "Ollama tag (e.g. 'qwen3:8b-24k'). Set base_url and model to "
                "the same provider."
            )
        if "/" in model and _ollama_serves_model(base_url, model) is False:
            raise ProviderConfigIncoherent(
                f"provider mismatch: base_url host {host!r} is Ollama but model "
                f"{model!r} is not installed there (it looks like another "
                "provider's namespaced id). Pull it with 'ollama pull' or set "
                "base_url and model to the same provider."
            )


def apply_provider_config(raw_body: bytes) -> bytes:
    """Update the provider env from the POST body and return
    ``{"ok", "model", "base_url_host"}``; the adapter reads the env per call, so
    a push takes effect on the NEXT turn with no restart."""
    # The RESOLVED pair - this body over the live env - must pass the coherence
    # check before anything is written, so a rejected push leaves the env
    # byte-identical rather than half-applied. SECURITY: the api_key is written
    # to the env but NEVER logged, echoed or raised; only the base URL HOST and
    # the effective model name leave this function.
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body.strip() else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Deliberately generic -- a decode error message can carry a fragment of
        # the raw body, which may be the mistyped key. Never surface it.
        raise ProviderConfigBadRequest("body must be valid JSON") from None
    if not isinstance(payload, dict):
        raise ProviderConfigBadRequest(
            'body must be a JSON object like {"base_url": "...", "model": "..."}'
        )
    field_env = {
        "base_url": "TRID3NT_OPENAI_BASE_URL",
        "api_key": "TRID3NT_OPENAI_API_KEY",
        "model": "TRID3NT_OPENAI_MODEL",
        "num_ctx": "TRID3NT_OPENAI_NUM_CTX",
    }
    updates: dict[str, str] = {}
    for field, env_name in field_env.items():
        if field in payload and payload[field] is not None:
            value = str(payload[field]).strip()
            if value:
                updates[env_name] = value
    # Gate on the pair that would be IN FORCE after this push -- a body carrying
    # only base_url still has to agree with the model already set.
    _check_provider_coherence(
        updates.get("TRID3NT_OPENAI_BASE_URL")
        or os.environ.get("TRID3NT_OPENAI_BASE_URL", "").strip(),
        updates.get("TRID3NT_OPENAI_MODEL")
        or os.environ.get("TRID3NT_OPENAI_MODEL", "").strip(),
    )
    os.environ.update(updates)
    # A same-name model must re-discover its num_ctx (the provider/num_ctx
    # switch invalidates the process-lifetime cache).
    try:
        from trid3nt_server.gates.context_budget import reset_num_ctx_cache

        reset_num_ctx_cache()
    except Exception:  # noqa: BLE001 -- cache reset is best-effort, never fatal
        pass
    effective_model = os.environ.get("TRID3NT_OPENAI_MODEL", "").strip() or None
    base_url = os.environ.get("TRID3NT_OPENAI_BASE_URL", "").strip()
    host = _base_url_host(base_url) if base_url else ""
    return json.dumps(
        {"ok": True, "model": effective_model, "base_url_host": host},
        separators=(",", ":"),
    ).encode("utf-8")
