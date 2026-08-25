"""Anthropic Messages API adapter (official ``anthropic`` SDK).

``MODEL_PROVIDER=anthropic`` selects this path. It accepts the SAME inputs the
sibling adapters accept -- a ``list[genai_types.Content]`` history, a list of
``genai_types.FunctionDeclaration`` tool specs and a system prompt -- converts
them to Messages API shapes at the boundary, and yields the SAME
``StreamEvent`` union the server turn loop consumes.

Config env (read at call time so an env injection needs no re-import):

  MODEL_PROVIDER=anthropic     selects this adapter at the dispatch seam
  ANTHROPIC_API_KEY            the API key (resolved by the SDK itself)
  TRID3NT_ANTHROPIC_MODEL      model id (default ``claude-sonnet-5``)

API constraints this file encodes (they are 400s, not preferences):

  * ``thinking`` is adaptive -- ``{"type": "adaptive"}``; ``budget_tokens`` is
    removed on this model family.
  * Sampling params (``temperature`` / ``top_p`` / ``top_k``) are removed on
    this model family -- sending any of them is a 400, so none is sent.
  * No assistant prefill: the request never ends on an assistant turn.
  * Streaming is used for every call so a long tool-planning turn cannot trip
    the SDK request timeout.

Prompt caching is MANDATORY here (cost discipline carries across every model
swap). The cacheable prefix renders ``tools`` -> ``system`` -> ``messages``, so
the breakpoints sit at the end of the tool catalog and the end of the system
block -- the two large stable spans -- and every volatile per-turn content sits
after them. ``usage.cache_read_input_tokens`` on each turn proves the hit; it
is logged at INFO on every turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from google.genai import types as genai_types

from .adapter import (
    CompactionCompleteEvent,
    CompactionStartEvent,
    FunctionCallEvent,
    StreamEvent,
    TextDeltaEvent,
    UpstreamProviderError,
    UsageMetadataEvent,
    provider_backoff_wait,
    provider_retries,
)
from .bedrock_adapter import _genai_schema_to_json_schema
from trid3nt_server.gates.context_budget import (
    ContextWindowExceededError,
    discover_context_window,
    estimate_tokens,
    estimate_tokens_for_contents,
    estimate_tokens_for_tools,
    looks_like_context_overflow_error,
    plan_turn,
)

logger = logging.getLogger("trid3nt_server.adapters.anthropic_adapter")

#: Default model id. Exact string -- this family carries no date suffix.
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-5"

#: Output ceiling per round. Adaptive thinking tokens are drawn from the same
#: budget, so this sits above the 8k the Bedrock path uses.
_DEFAULT_MAX_TOKENS = 16000

_PROVIDER_LABEL = "Anthropic API"

#: Cache breakpoint marker. Max 4 per request; this file places 2.
_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}

#: This API offers an EXACT token counter (``messages.count_tokens``), which is
#: strictly better than our chars/4 heuristic -- but it costs a round trip, so
#: paying it on every turn would tax the common case to settle a question that
#: is not close. We consult it only once the cheap estimate reaches this
#: fraction of the discovered window, i.e. exactly when the trim decision is
#: marginal and being wrong is expensive.
_COUNT_TOKENS_CONSULT_RATIO = 0.7


async def _exact_prompt_tokens(client: Any, kwargs: dict[str, Any]) -> int | None:
    """The provider's OWN count of this request's input tokens, or None.

    Counts the whole prompt -- messages, system block and tool schemas -- which
    is precisely the number ``plan_turn`` wants as ``wire_tokens``. Best-effort:
    the counter is an optimization over the heuristic, never a hard dependency,
    so any fault degrades to the estimate rather than failing the turn.
    """
    try:
        payload: dict[str, Any] = {
            "model": kwargs["model"],
            "messages": kwargs["messages"],
        }
        if kwargs.get("system") is not None:
            payload["system"] = kwargs["system"]
        if kwargs.get("tools") is not None:
            payload["tools"] = kwargs["tools"]
        resp = await client.messages.count_tokens(**payload)
        tokens = getattr(resp, "input_tokens", None)
        return int(tokens) if isinstance(tokens, int) and tokens > 0 else None
    except Exception:  # noqa: BLE001 -- fall back to the heuristic
        logger.debug("anthropic count_tokens unavailable; using the estimate", exc_info=True)
        return None


def anthropic_api_key() -> str:
    """Return ``ANTHROPIC_API_KEY``; raise honestly when it is unset.

    The SDK resolves the key itself, but an unset key otherwise surfaces as an
    SDK construction error with no pointer to the fix.
    """
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "MODEL_PROVIDER=anthropic requires ANTHROPIC_API_KEY to be set. "
            "Add it to .env.local and restart the agent."
        )
    return key


def anthropic_model(session_model: str | None = None) -> str:
    """Resolve the model id to send.

    A per-turn selection from the client wins when it names a Claude model; an
    id shaped for another provider (a stale Bedrock inference-profile id, an
    Ollama tag) is ignored in favour of ``TRID3NT_ANTHROPIC_MODEL`` / the
    default, since sending it would be a 404 from the Messages API.
    """
    if session_model and session_model.strip().startswith("claude-"):
        return session_model.strip()
    configured = os.environ.get("TRID3NT_ANTHROPIC_MODEL", "").strip()
    return configured or ANTHROPIC_DEFAULT_MODEL


# --------------------------------------------------------------------------- #
# Tool-spec conversion: genai FunctionDeclaration -> Messages API tools[]
# --------------------------------------------------------------------------- #


def tool_declarations_to_anthropic_tools(
    tool_declarations: list[genai_types.FunctionDeclaration] | None,
) -> list[dict[str, Any]]:
    """Convert genai FunctionDeclarations to Messages API ``tools[]``.

    Descriptions pass through in full -- unlike the Bedrock toolSpec, this API
    imposes no per-description length cap, so the registry's LLM-facing
    docstrings reach the model whole.
    """
    tools: list[dict[str, Any]] = []
    for decl in tool_declarations or []:
        dumped = decl.model_dump(mode="json", exclude_none=True)
        params = dumped.get("parameters")
        schema = (
            _genai_schema_to_json_schema(params)
            if params
            else {"type": "object", "properties": {}}
        )
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        tools.append(
            {
                "name": dumped["name"],
                "description": dumped.get("description") or dumped["name"],
                "input_schema": schema,
            }
        )
    return tools


# --------------------------------------------------------------------------- #
# History conversion: genai Content[] -> Messages API messages[]
# --------------------------------------------------------------------------- #


def _coalesce(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive same-role messages.

    Tool results must ride the user message that immediately follows the
    assistant ``tool_use`` turn, and the codebase emits one Content per part,
    so the run of function_response Contents has to fold into one message.
    """
    merged: list[dict[str, Any]] = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"].extend(m["content"])
        else:
            merged.append({"role": m["role"], "content": list(m["content"])})
    return merged


def _ensure_messages_start_with_user(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize to a first message with role ``user`` (an API requirement)."""
    if not messages:
        return [{"role": "user", "content": [{"type": "text", "text": "(context)"}]}]
    idx = 0
    while idx < len(messages) and messages[idx].get("role") != "user":
        idx += 1
    if idx >= len(messages):
        return [
            {"role": "user", "content": [{"type": "text", "text": "(context)"}]},
            *messages,
        ]
    return messages[idx:]


def contents_to_anthropic_messages(
    contents: list[genai_types.Content],
) -> list[dict[str, Any]]:
    """Convert genai ``contents`` to Messages API ``messages[]``.

    genai roles ``user``/``model`` map to ``user``/``assistant``. A
    function_call Part becomes a ``tool_use`` block, a function_response Part a
    ``tool_result`` block; the two ids must match, so a history whose call ids
    are absent gets synthesized ids paired by arrival order. Empty text is
    dropped -- an empty text block is a 400.
    """
    messages: list[dict[str, Any]] = []
    pending_ids: deque[str] = deque()
    counter = 0

    def _next_id() -> str:
        nonlocal counter
        counter += 1
        return f"toolu_{counter}"

    for content in contents:
        role = getattr(content, "role", "user") or "user"
        api_role = "assistant" if role == "model" else "user"
        blocks: list[dict[str, Any]] = []
        for part in getattr(content, "parts", None) or []:
            fc = getattr(part, "function_call", None)
            fr = getattr(part, "function_response", None)
            text = getattr(part, "text", None)
            if fc is not None and getattr(fc, "name", None):
                tid = getattr(fc, "id", None) or _next_id()
                pending_ids.append(tid)
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tid,
                        "name": fc.name,
                        "input": dict(getattr(fc, "args", None) or {}),
                    }
                )
            elif fr is not None and getattr(fr, "name", None):
                tid = getattr(fr, "id", None) or (
                    pending_ids.popleft() if pending_ids else _next_id()
                )
                resp = getattr(fr, "response", None)
                if not isinstance(resp, dict):
                    resp = {"result": resp}
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tid,
                        "content": json.dumps(resp, default=str),
                    }
                )
            elif text and text.strip():
                blocks.append({"type": "text", "text": text})
        if blocks:
            messages.append({"role": api_role, "content": blocks})

    return _ensure_messages_start_with_user(_coalesce(messages))


# --------------------------------------------------------------------------- #
# Request construction
# --------------------------------------------------------------------------- #


def _build_message_kwargs(
    contents: Any,
    tool_declarations: Any,
    system_prompt: str | None,
    model: str | None,
) -> dict[str, Any]:
    """Build the ``messages.stream`` kwargs (pure -- unit-testable).

    Cache breakpoints land on the LAST tool and the system block: the render
    order is tools -> system -> messages, so those two mark the end of the
    stable prefix and everything volatile (the conversation) follows them. A
    miss is a normal uncached call, never a correctness risk.
    """
    kwargs: dict[str, Any] = {
        "model": anthropic_model(model),
        "max_tokens": _DEFAULT_MAX_TOKENS,
        "messages": contents_to_anthropic_messages(contents),
        # Adaptive thinking: budget_tokens is removed on this model family.
        # Display defaults to omitted, so no reasoning text reaches chat.
        "thinking": {"type": "adaptive"},
    }

    tools = tool_declarations_to_anthropic_tools(tool_declarations)
    if tools:
        tools[-1] = {**tools[-1], "cache_control": dict(_CACHE_CONTROL)}
        kwargs["tools"] = tools
        kwargs["tool_choice"] = {"type": "auto"}

    if system_prompt:
        kwargs["system"] = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": dict(_CACHE_CONTROL),
            }
        ]

    return kwargs


# --------------------------------------------------------------------------- #
# Upstream-provider discipline
# --------------------------------------------------------------------------- #


def _is_transient_anthropic_error(exc: BaseException) -> bool:
    """True when ``exc`` is a TRANSIENT upstream failure worth retrying.

    Transient: 429, any status >= 500 (overloaded / service-unavailable /
    internal), and connection drops or request timeouts. Non-transient: 400 /
    401 / 403 / 404 / 422 -- genuine rejections of our request where a retry
    only hides the bug. Classes are tested most-specific-first.
    """
    try:
        import anthropic  # noqa: WPS433 -- dep dormant unless this provider is on
    except ImportError:
        return False

    if isinstance(
        exc,
        (
            anthropic.BadRequestError,
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
            anthropic.UnprocessableEntityError,
        ),
    ):
        return False
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        try:
            return int(getattr(exc, "status_code", 0) or 0) >= 500
        except (TypeError, ValueError):
            return False
    if isinstance(exc, anthropic.APIConnectionError):  # subsumes APITimeoutError
        return True
    return False


def _retry_after_seconds(exc: BaseException, attempt: int) -> float:
    """A 429's ``retry-after`` header when present, else the backoff schedule."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        raw = headers.get("retry-after")
        if raw:
            try:
                wait = float(raw)
                if wait > 0:
                    return min(wait, 60.0)
            except (TypeError, ValueError):
                pass
    return provider_backoff_wait(attempt)


def _log_usage(usage: Any, model_id: str) -> None:
    """Log the per-turn cache accounting -- the prompt-caching proof line."""
    logger.info(
        "anthropic usage model=%s input=%s output=%s cache_read=%s cache_write=%s",
        model_id,
        getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None),
        getattr(usage, "cache_read_input_tokens", None),
        getattr(usage, "cache_creation_input_tokens", None),
    )


def _usage_event(usage: Any) -> UsageMetadataEvent:
    """Map ``message.usage`` onto the shared UsageMetadataEvent."""
    def _int(name: str) -> int | None:
        val = getattr(usage, name, None)
        return val if isinstance(val, int) else None

    cache_read = _int("cache_read_input_tokens")
    cache_write = _int("cache_creation_input_tokens")
    prompt_tokens = _int("input_tokens")
    output_tokens = _int("output_tokens")
    parts = [v for v in (prompt_tokens, cache_read, cache_write, output_tokens) if v]
    return UsageMetadataEvent(
        cached_content_token_count=cache_read,
        total_token_count=sum(parts) if parts else None,
        prompt_token_count=prompt_tokens,
        candidates_token_count=output_tokens,
        cache_hit=bool(cache_read),
    )


def _function_call_events(message: Any) -> list[FunctionCallEvent]:
    """Harvest ``tool_use`` blocks off the final message.

    Tool inputs are read as parsed JSON (the SDK accumulates the streamed
    partial JSON); a string payload is parsed with ``json.loads`` -- never
    matched as text.
    """
    events: list[FunctionCallEvent] = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) != "tool_use":
            continue
        raw = getattr(block, "input", None)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                raw = {}
        events.append(
            FunctionCallEvent(
                name=getattr(block, "name", "") or "",
                call_id=getattr(block, "id", None),
                args=raw if isinstance(raw, dict) else {},
            )
        )
    return [ev for ev in events if ev.name]


def _refusal_notice(message: Any) -> str | None:
    """An honest sentence when the model declined, else None."""
    if getattr(message, "stop_reason", None) != "refusal":
        return None
    details = getattr(message, "stop_details", None)
    category = getattr(details, "category", None)
    suffix = f" (category: {category})" if category else ""
    return f"The model declined to answer this request{suffix}."


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #


async def stream_anthropic(
    contents: list[genai_types.Content],
    tool_declarations: list[genai_types.FunctionDeclaration] | None = None,
    system_prompt: str | None = None,
    model: str | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream one Messages API turn, yielding the ``StreamEvent`` union.

    Mirrors ``bedrock_adapter.stream_bedrock``: one call == one model round.
    The turn loop appends function_call + function_response Contents and
    re-calls until no tool calls remain.

    Request-time transient failures (429 / 5xx / timeouts) retry with the
    shared exponential backoff, logging the provider's verbatim error; on
    exhaustion the typed ``UpstreamProviderError`` ends the turn with an honest
    provider-unavailable narration. A MID-STREAM transient failure is
    classified the same way but never replayed -- tokens already flowed.
    """
    try:
        import anthropic
        from anthropic import AsyncAnthropic
    except ImportError as exc:
        raise RuntimeError(
            "MODEL_PROVIDER=anthropic requires the 'anthropic' package. "
            "Install it with: pip install anthropic"
        ) from exc

    anthropic_api_key()  # fail loudly and early when the key is unset
    client = AsyncAnthropic()
    model_id = anthropic_model(model)

    # CLIENT-SIDE history management. The window is discovered from the Models
    # API (``max_input_tokens``), and the trim strategy is the SHARED one in
    # context_budget -- this adapter only rebuilds kwargs from the planned
    # contents. Because the plan rewrites ONLY the conversation, and the cache
    # breakpoints sit on ``tools``/``system`` (which render before messages),
    # trimming can never invalidate the cached prefix.
    window = await discover_context_window("anthropic", model_id)
    working_contents = list(contents)
    tools_preview = tool_declarations_to_anthropic_tools(tool_declarations)
    tool_tokens = estimate_tokens_for_tools(tools_preview)
    sys_tokens = estimate_tokens(system_prompt) if system_prompt else 0

    kwargs = _build_message_kwargs(working_contents, tool_declarations, system_prompt, model)

    # Only when the cheap estimate says the decision is MARGINAL do we spend a
    # round trip on the provider's exact counter (see the ratio's docstring).
    heuristic = estimate_tokens_for_contents(working_contents) + tool_tokens + sys_tokens
    exact: int | None = None
    if heuristic >= window.tokens * _COUNT_TOKENS_CONSULT_RATIO:
        exact = await _exact_prompt_tokens(client, kwargs)
        if exact is not None:
            logger.info(
                "context-budget: anthropic exact prompt tokens=%d (heuristic said %d)",
                exact,
                heuristic,
            )

    plan = plan_turn(
        working_contents,
        window=window,
        tool_tokens=tool_tokens,
        system_tokens=sys_tokens,
        wire_tokens=exact,
        output_reserve=_DEFAULT_MAX_TOKENS,
        phase="proactive",
    )
    if plan.compacted:
        working_contents = plan.contents
        yield CompactionStartEvent()
        yield CompactionCompleteEvent(
            before_tokens=plan.before_tokens, after_tokens=plan.after_tokens
        )
        kwargs = _build_message_kwargs(
            working_contents, tool_declarations, system_prompt, model
        )

    max_retries = provider_retries()
    streamed_any = False
    last_exc: BaseException | None = None
    overflow_retried = False

    for attempt in range(max_retries + 1):
        try:
            async with client.messages.stream(**kwargs) as stream:
                async for event in stream:
                    if getattr(event, "type", None) == "text":
                        streamed_any = True
                        text = getattr(event, "text", "")
                        if text:
                            yield TextDeltaEvent(delta=text)
                final = await stream.get_final_message()
            streamed_any = True

            _log_usage(getattr(final, "usage", None), model_id)
            notice = _refusal_notice(final)
            if notice:
                logger.warning("anthropic refusal (model=%s): %s", model_id, notice)
                yield TextDeltaEvent(delta=notice)
            for call_event in _function_call_events(final):
                yield call_event
            usage = getattr(final, "usage", None)
            if usage is not None:
                yield _usage_event(usage)
            return
        except anthropic.APIError as exc:
            # CONTEXT OVERFLOW is the one 400 worth retrying: the request is
            # well-formed, it just did not fit. Standing upstream-provider
            # rule -- log the provider's message VERBATIM, then trim HARDER
            # (reactive ratio) and resend exactly once. A second overflow is
            # the honest typed CONTEXT_WINDOW_EXCEEDED envelope, never the
            # generic provider-unavailable bucket.
            if (
                looks_like_context_overflow_error(exc)
                and not streamed_any
            ):
                last_exc = exc
                if overflow_retried:
                    logger.error(
                        "anthropic context overflow persisted after one recompaction "
                        "(model=%s, window=%d from %s); provider error verbatim: %s",
                        model_id,
                        window.tokens,
                        window.source,
                        exc,
                    )
                    raise ContextWindowExceededError(window.tokens) from exc
                overflow_retried = True
                logger.warning(
                    "anthropic context overflow (model=%s, window=%d from %s) -- "
                    "recompacting and retrying once; provider error verbatim: %s",
                    model_id,
                    window.tokens,
                    window.source,
                    exc,
                )
                retry_plan = plan_turn(
                    working_contents,
                    window=window,
                    tool_tokens=tool_tokens,
                    system_tokens=sys_tokens,
                    output_reserve=_DEFAULT_MAX_TOKENS,
                    phase="reactive",
                )
                working_contents = retry_plan.contents
                kwargs = _build_message_kwargs(
                    working_contents, tool_declarations, system_prompt, model
                )
                yield CompactionStartEvent()
                yield CompactionCompleteEvent(
                    before_tokens=retry_plan.before_tokens,
                    after_tokens=retry_plan.after_tokens,
                )
                continue
            if not _is_transient_anthropic_error(exc):
                raise
            if streamed_any:
                # Tokens already flowed -- classify honestly, never replay.
                logger.error(
                    "anthropic mid-stream transient upstream failure (model=%s); "
                    "provider error verbatim: %s",
                    model_id,
                    exc,
                )
                raise UpstreamProviderError(
                    provider=_PROVIDER_LABEL, detail=str(exc), attempts=attempt + 1
                ) from exc
            last_exc = exc
            if attempt >= max_retries:
                break
            wait = _retry_after_seconds(exc, attempt)
            logger.warning(
                "anthropic transient upstream error (attempt %d/%d) - sleeping "
                "%.0fs then retrying (model=%s); provider error verbatim: %s",
                attempt + 1,
                max_retries,
                wait,
                model_id,
                exc,
            )
            await asyncio.sleep(wait)

    assert last_exc is not None
    if looks_like_context_overflow_error(last_exc):
        # The retry budget ran out while the prompt still did not fit: that is
        # a context-window failure, not an unavailable provider.
        logger.error(
            "anthropic context overflow unresolved (model=%s, window=%d from %s); "
            "last provider error verbatim: %s",
            model_id,
            window.tokens,
            window.source,
            last_exc,
        )
        raise ContextWindowExceededError(window.tokens) from last_exc
    logger.error(
        "anthropic upstream provider unavailable after %d attempt(s) (model=%s); "
        "last provider error verbatim: %s",
        max_retries + 1,
        model_id,
        last_exc,
    )
    raise UpstreamProviderError(
        provider=_PROVIDER_LABEL, detail=str(last_exc), attempts=max_retries + 1
    ) from last_exc


__all__ = [
    "ANTHROPIC_DEFAULT_MODEL",
    "anthropic_api_key",
    "anthropic_model",
    "contents_to_anthropic_messages",
    "stream_anthropic",
    "tool_declarations_to_anthropic_tools",
]
