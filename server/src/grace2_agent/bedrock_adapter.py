"""Bedrock Converse adapter (sprint-14-aws job-0286) — the agent's AWS brain.

GRACE was built on Vertex AI / Gemini via ``adapter.py``. The AWS migration
swaps the model provider to **Amazon Bedrock** (Claude Sonnet 4.6 by default)
WITHOUT touching the multi-turn loop, the 57-tool catalog, the envelope
emission, or the web client. The seam is deliberately narrow:

  * This module accepts the SAME inputs ``adapter.stream_events_with_contents``
    accepts — a ``list[genai_types.Content]`` history + a list of
    ``genai_types.FunctionDeclaration`` tool specs + a system prompt — and
    converts them to the Bedrock Converse shapes at the boundary.
  * It yields the SAME ``StreamEvent`` union (``TextDeltaEvent`` /
    ``FunctionCallEvent`` / ``UsageMetadataEvent``) the Gemini path yields, so
    ``server.py``'s dispatch loop, ``categories.validate_function_call``, the
    PipelineEmitter, and the cache-status telemetry all work unchanged.

Provider selection is ``MODEL_PROVIDER`` (``vertex`` default; ``bedrock`` to
engage this path). ``adapter.stream_events_with_contents`` branches here when
the flag is ``bedrock`` — see that function. The Gemini ``cached_content``
fast-path does not apply (Bedrock has its own ``cachePoint`` prompt-caching;
deferred to a follow-up) — ``cached_content_name`` is ignored here.

Keeping the genai types as the internal lingua franca means the migration is
reversible and the Gemini path stays bit-for-bit intact while Bedrock is
proven. A later job can drop the genai dependency entirely once Bedrock parity
is verified end-to-end.
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
    FunctionCallEvent,
    StreamEvent,
    TextDeltaEvent,
    UsageMetadataEvent,
)

logger = logging.getLogger("grace2_agent.bedrock_adapter")

# Cross-region inference profile for Claude Sonnet 4.6 (confirmed accessible in
# the target account). Override via ``BEDROCK_MODEL_ID``. The ``us.`` prefix is
# the inference-profile id required for on-demand throughput on Claude 4.x.
BEDROCK_DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-6"

# ---------------------------------------------------------------------------
# Model registry — single source of truth for selectable Bedrock models.
#
# ``supportsPromptCache=True`` means cachePoint markers are safe to include
# in the Converse request.  Any model that does NOT support the cachePoint
# extension (e.g. DeepSeek-R1) MUST be listed here with False — sending
# cachePoint to an unsupporting model causes a Bedrock validation error.
# ---------------------------------------------------------------------------

#: Metadata for one selectable agent model.  Only the fields the server
#: needs at dispatch time (id + cache capability); the richer set (label,
#: accentColor, provider) lives on the web side in ``modelRegistry.ts``.
#:
#: ONLY models PROVEN (probed live 2026-06-17 in account 226996537797/us-west-2)
#: to be invokable AND to support the Converse ``toolConfig`` the agent loop
#: needs are listed.  MUST stay in sync with web ``modelRegistry.ts``.
#:   - ``us.anthropic.claude-haiku-4-5-20251001-v1:0``: valid id but ACCESS NOT
#:     ENABLED; add here + in the web registry once Bedrock model access is
#:     granted (it is the strongest cheap+agentic Anthropic option).
#:   - ``us.deepseek.r1-v1:0``: REJECTS toolConfig on Bedrock -> cannot drive
#:     the tool loop; intentionally OMITTED (the malformed short-form
#:     ``us.anthropic.claude-haiku-4-5`` that previously shipped here was the
#:     root cause of the "provided model identifier is invalid" error).
SELECTABLE_MODELS: list[dict[str, Any]] = [
    {
        "id": "us.anthropic.claude-sonnet-4-6",
        "label": "Claude Sonnet 4.6",
        "provider": "Anthropic",
        "supportsPromptCache": True,
    },
    {
        # Access enabled + verified 2026-06-17. Anthropic -> cachePoint OK.
        "id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "label": "Claude Haiku 4.5",
        "provider": "Anthropic",
        "supportsPromptCache": True,
    },
    {
        # Nova/DeepSeek reject cachePoint — supportsPromptCache False; the
        # Anthropic-only model_supports_cache() gate enforces this server-side.
        "id": "us.amazon.nova-pro-v1:0",
        "label": "Amazon Nova Pro",
        "provider": "Amazon",
        "supportsPromptCache": False,
    },
    {
        "id": "us.amazon.nova-lite-v1:0",
        "label": "Amazon Nova Lite",
        "provider": "Amazon",
        "supportsPromptCache": False,
    },
    {
        # User-pickable only (NO auto-routing). Opus 4.5 verified invokable WITH
        # toolConfig in 226996537797/us-west-2 on 2026-06-24. Anthropic ->
        # cachePoint OK. Default stays Sonnet; a user must deliberately select
        # this, so prod cost is never silently bumped.
        "id": "us.anthropic.claude-opus-4-5-20251101-v1:0",
        "label": "Claude Opus 4.5",
        "provider": "Anthropic",
        "supportsPromptCache": True,
    },
]

#: Fast-lookup set of the ids the in-chat selector may legitimately send. The
#: server validates an inbound ``model_id`` against this before using it (see
#: ``resolve_selected_model``) so a stale / removed / unsupported id can never
#: reach ConverseStream and throw a ValidationException.
SELECTABLE_MODEL_IDS: frozenset[str] = frozenset(m["id"] for m in SELECTABLE_MODELS)

def model_supports_cache(model_id: str) -> bool:
    """Return True only when ``model_id`` is an Anthropic Claude model.

    On Bedrock the ``cachePoint`` block is an ANTHROPIC-family feature. Amazon
    Nova and DeepSeek-R1 REJECT a request that carries cachePoint in the system
    block or toolConfig — proven live by NATE's "extraneous key [cachePoint] is
    not permitted, #/toolConfig/tools/93" error when he selected Nova Pro. So
    this is an ALLOWLIST (Anthropic only), NOT the earlier "unknown -> assume
    supported" default that wrongly enabled cachePoint for Nova and broke every
    non-Sonnet model. Match on provider substring so future Claude profile ids
    (haiku-4-5, opus, etc.) are covered without an edit.
    """
    mid = model_id.lower()
    return "anthropic" in mid or "claude" in mid


def resolve_selected_model(requested: str | None) -> tuple[str | None, str | None]:
    """Validate a user-requested model id against the selectable allowlist.

    Returns ``(effective_model_id, notice)`` where:
      - ``effective_model_id`` is ``requested`` when it is a known-good
        selectable id, else ``None`` (meaning "use the server default", so the
        caller falls back to ``bedrock_model_id()``).
      - ``notice`` is ``None`` on the happy path, or a short, user-facing
        sentence explaining the fall-back when ``requested`` is non-empty but
        not selectable.  The server surfaces this honestly instead of letting an
        invalid id reach ConverseStream (which throws a raw ValidationException).

    ``requested is None`` is the normal "no explicit choice" case and returns
    ``(None, None)`` — silent default, no notice.

    MODEL_PROVIDER=openai (the TRID3NT local build -- F2, live-feedback
    2026-07-08): the selectable set is whatever the local runtime serves (the
    web lists it live via the agent's ``/api/local-models`` endpoint), NOT the
    Bedrock allowlist, so the id passes through verbatim. Safety still holds:
    ``openai_adapter.openai_model`` ignores a Bedrock-shaped id (falls back to
    ``GRACE2_OPENAI_MODEL``), and a model the runtime does not have raises the
    runtime's own honest error rather than a fabricated success. The legacy
    ``"local-default"`` placeholder id (the pre-F2 web registry entry, possibly
    persisted in localStorage) maps to ``None`` — "use the server default".
    The cloud (bedrock) validation path below is byte-identical.
    """
    if requested is None:
        return None, None
    if model_provider() == "openai":
        if requested == "local-default":
            return None, None
        return requested, None
    if requested in SELECTABLE_MODEL_IDS:
        return requested, None
    return (
        None,
        (
            f"The requested model '{requested}' is not available, so this turn "
            "is running on the default model."
        ),
    )

# Match the Gemini per-request config (adapter.py:GenerateContentConfig).
_DEFAULT_TEMPERATURE = 0.7
_DEFAULT_MAX_TOKENS = 8192


def model_provider() -> str:
    """Resolve the active model provider (``bedrock`` default).

    GCP/Vertex is decommissioned: the agent runs on Amazon Bedrock. The
    ``MODEL_PROVIDER`` seam is retained — only the default flips from ``vertex``
    to ``bedrock`` — so an explicit override is still honored. Read at call time
    so an ECS / systemd env injection takes effect without re-import.
    """
    return (os.environ.get("MODEL_PROVIDER") or "bedrock").strip().lower()


def bedrock_model_id() -> str:
    return os.environ.get("BEDROCK_MODEL_ID", BEDROCK_DEFAULT_MODEL)


def _prompt_cache_enabled() -> bool:
    """Bedrock prompt caching (``cachePoint``) ON by default; env off-switch.

    The sprint-14 Gemini->Bedrock swap DEFERRED prompt caching, so every turn
    re-sent the full static system prompt + 94-tool catalog UNCACHED — the #1
    Bedrock cost driver (the Gemini path had cachedContent ~90% discount). We
    restore it with ``cachePoint`` markers. Gated by ``BEDROCK_PROMPT_CACHE`` so
    ops can disable without a redeploy if a model ever rejects cachePoint blocks.
    """
    return (
        os.environ.get("BEDROCK_PROMPT_CACHE", "1").strip().lower()
        not in {"0", "false", "no", "off"}
    )


def _build_converse_kwargs(
    contents: Any,
    tool_declarations: Any,
    system_prompt: str | None,
    model: str | None,
) -> dict[str, Any]:
    """Build the boto3 ``converse_stream`` kwargs (pure — unit-testable).

    Inserts Bedrock ``cachePoint`` markers (when enabled AND supported by the
    model) at the END of the system block AND the tool list.  Caching is
    PREFIX-based: for Anthropic models the cacheable prefix order is
    tools -> system -> messages, so the tool-catalog cachePoint caches the
    large static 94-tool block independently, and the system cachePoint
    additionally caches the system prefix when it is stable.  A miss is a
    normal uncached call (no correctness risk).

    cachePoint GATING (prod-critical): cachePoint is only safe for models that
    support it.  ``DeepSeek-R1`` (``us.deepseek.r1-v1:0``) does NOT support
    cachePoint — Bedrock returns a validation error if cachePoint blocks are
    included in a request to that model.  The gate is a two-condition AND:

        ``_prompt_cache_enabled()``    — global env off-switch (ops safety valve)
        ``model_supports_cache(id)``   — per-model capability check

    Both must be True for any cachePoint block to be added.
    """
    model_id = model or bedrock_model_id()
    _system_unused, messages = contents_to_bedrock_messages(contents)
    # Normalize messages to start with role=='user' (Bedrock requirement).
    messages = _ensure_messages_start_with_user(messages)
    system_blocks: list[dict[str, Any]] = (
        [{"text": system_prompt}] if system_prompt else []
    )
    tools = tool_declarations_to_bedrock_tools(tool_declarations)
    # Two-condition cache gate: global env switch AND per-model capability.
    cache = _prompt_cache_enabled() and model_supports_cache(model_id)

    if system_blocks and cache:
        system_blocks = [*system_blocks, {"cachePoint": {"type": "default"}}]

    kwargs: dict[str, Any] = {
        "modelId": model_id,
        "messages": messages,
        "inferenceConfig": {
            "temperature": _DEFAULT_TEMPERATURE,
            "maxTokens": _DEFAULT_MAX_TOKENS,
        },
    }
    if system_blocks:
        kwargs["system"] = system_blocks
    if tools:
        tool_list = (
            [*tools, {"cachePoint": {"type": "default"}}] if cache else tools
        )
        kwargs["toolConfig"] = {"tools": tool_list, "toolChoice": {"auto": {}}}

    # Log LLM input preview (system text length + message array shape).
    if os.environ.get("GRACE2_LOG_LLM_INPUT", "").lower() in {"1", "true", "yes", "on"}:
        system_text_len = sum(
            len(block.get("text", "")) for block in (kwargs.get("system") or [])
        )
        msg_shape = [
            {"role": m.get("role"), "content_types": [list(c.keys()) for c in m.get("content", [])]}
            for m in messages
        ]
        logger.info(
            f"LLM input preview: system_text_len={system_text_len}, messages={msg_shape}"
        )

    return kwargs


# --------------------------------------------------------------------------- #
# Client-side timeout bounds for the Converse call (live-down hardening).
#
# THE BUG THIS FIXES (2026-06-20, live agent DOWN): a hung Bedrock Converse
# call (NATE switched to Haiku then Nova; the call hung) blocked its
# ``asyncio.to_thread`` executor worker INDEFINITELY. With no client-side
# timeout, ``converse_stream`` / EventStream iteration never returns and never
# raises, so the producer thread is stuck forever, the consumer's
# ``await queue.get()`` never completes, the turn task never finishes ->
# ``inflight_turn_count()`` stays > 0 -> ``is_busy()`` is pinned True (the
# auto-stop gate refuses to sleep) AND the loop is effectively wedged on that
# turn, so NO model (even Sonnet) could respond and ``/api/health`` went
# unresponsive behind the blocked loop.
#
# THE FIX: attach a botocore ``Config`` with a bounded ``read_timeout`` /
# ``connect_timeout`` so a hung call RAISES ``ReadTimeoutError`` /
# ``ConnectTimeoutError`` instead of hanging forever. The producer's
# ``except BaseException`` then puts that exception on the queue, ``stream_*``
# re-raises it, and the server turn loop's ``except Exception`` handler
# surfaces an honest ``LLM_UNAVAILABLE`` error envelope AND lets the turn
# TERMINATE (so the live-turn entry's task completes -> ``inflight_turn_count``
# drops -> ``is_busy`` clears). This bounds ONLY the LLM Converse call -- it is
# NOT a turn-wide or solve-wide timeout. The minutes-long ``run_solver`` /
# ``wait_for_completion`` solve path runs through Batch (a SEPARATE boto3
# client built elsewhere) and is intentionally NOT bounded here.
#
# Both are env-overridable (ops safety valve) but default to values comfortably
# longer than a healthy Converse-stream first-byte + steady token flow yet far
# shorter than the ~indefinite hang that took the box down.
# --------------------------------------------------------------------------- #

#: Per-socket read timeout (seconds) for the bedrock-runtime client. A streamed
#: Converse turn keeps the socket active with token deltas, so this bounds the
#: gap between bytes -- a healthy stream resets it on every chunk; a wedged call
#: trips it. 60s is generous for first-byte + inter-chunk latency.
_BEDROCK_READ_TIMEOUT_DEFAULT = 60.0

#: TCP connect timeout (seconds) -- bounds DNS + handshake to the endpoint.
_BEDROCK_CONNECT_TIMEOUT_DEFAULT = 10.0

#: Max boto3 attempts (1 original + retries) for transient errors. ``standard``
#: mode retries throttling / 5xx but NOT a read-timeout of a streaming call.
_BEDROCK_MAX_ATTEMPTS_DEFAULT = 2


def _env_float(name: str, default: float) -> float:
    """Read a float from env ``name``; fall back to ``default`` on missing/bad."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def _env_int(name: str, default: int) -> int:
    """Read an int from env ``name``; fall back to ``default`` on missing/bad."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return val if val >= 1 else default


def _bedrock_timeout_config():
    """Build the botocore ``Config`` that bounds the Converse call.

    Returns a ``botocore.config.Config`` carrying ``read_timeout`` /
    ``connect_timeout`` (so a hung call RAISES instead of hanging the executor
    thread forever -- the core live-down fix) plus a small retry policy. Env
    overrides: ``BEDROCK_READ_TIMEOUT_S`` / ``BEDROCK_CONNECT_TIMEOUT_S`` /
    ``BEDROCK_MAX_ATTEMPTS``. Kept as its own function so the tests can assert
    the timeout values without standing up a real client.
    """
    from botocore.config import Config  # local import: optional for Vertex path

    return Config(
        read_timeout=_env_float(
            "BEDROCK_READ_TIMEOUT_S", _BEDROCK_READ_TIMEOUT_DEFAULT
        ),
        connect_timeout=_env_float(
            "BEDROCK_CONNECT_TIMEOUT_S", _BEDROCK_CONNECT_TIMEOUT_DEFAULT
        ),
        retries={
            "max_attempts": _env_int(
                "BEDROCK_MAX_ATTEMPTS", _BEDROCK_MAX_ATTEMPTS_DEFAULT
            ),
            "mode": "standard",
        },
    )


def _bedrock_client():
    """Build a ``bedrock-runtime`` client. boto3 resolves creds + region from
    the standard chain (env / ~/.aws / instance role). ``AWS_REGION`` wins.

    The client carries a ``Config`` with a bounded ``read_timeout`` /
    ``connect_timeout`` (see ``_bedrock_timeout_config``) so a hung Converse
    call RAISES rather than blocking its executor thread forever -- WITHOUT this
    the agent loop wedges on a stuck model turn (the 2026-06-20 live-down). The
    bound is on the LLM call ONLY; the Batch solve path uses its own client."""
    import boto3  # local import: keeps boto3 optional for the Vertex path

    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or "us-west-2"
    )
    return boto3.client(
        "bedrock-runtime",
        region_name=region,
        config=_bedrock_timeout_config(),
    )


# --------------------------------------------------------------------------- #
# Tool-spec conversion: genai FunctionDeclaration -> Bedrock toolConfig
# --------------------------------------------------------------------------- #

# genai Schema ``type`` is an uppercase enum (STRING/OBJECT/...); JSON Schema
# (what Bedrock's inputSchema.json wants) is lowercase.
_TYPE_MAP = {
    "STRING": "string",
    "NUMBER": "number",
    "INTEGER": "integer",
    "BOOLEAN": "boolean",
    "ARRAY": "array",
    "OBJECT": "object",
    "TYPE_UNSPECIFIED": "string",
}


def _ensure_messages_start_with_user(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize messages to always start with a role=='user' message.

    Bedrock's ConverseStream requires the first message to have role 'user'.
    This function strips any leading non-user messages (assistant/tool/etc)
    and, if the result is empty or still doesn't start with 'user', prepends
    a minimal synthetic user message "(context)" to satisfy Bedrock's requirement.

    Args:
        messages: A list of message dicts with 'role' and 'content' keys.

    Returns:
        The normalized messages list, guaranteed to start with role 'user'.
    """
    if not messages:
        return [{"role": "user", "content": [{"text": "(context)"}]}]

    # Strip leading non-user messages.
    idx = 0
    while idx < len(messages) and messages[idx].get("role") != "user":
        idx += 1

    # If we stripped all messages, prepend a synthetic one and return the rest.
    if idx >= len(messages):
        # All messages were non-user; return synthetic + everything.
        return [{"role": "user", "content": [{"text": "(context)"}]}] + messages

    # We found a user message at position idx; return from there onward.
    return messages[idx:]


def _genai_schema_to_json_schema(node: Any) -> dict[str, Any]:
    """Recursively convert a genai-dumped Schema dict to JSON Schema."""
    if not isinstance(node, dict):
        return {"type": "string"}
    out: dict[str, Any] = {}
    raw_type = node.get("type")
    if raw_type is not None:
        t = raw_type.value if hasattr(raw_type, "value") else str(raw_type)
        out["type"] = _TYPE_MAP.get(t.upper(), t.lower())
    if node.get("description"):
        out["description"] = node["description"]
    if node.get("enum"):
        out["enum"] = list(node["enum"])
    if node.get("format"):
        out["format"] = node["format"]
    props = node.get("properties")
    if isinstance(props, dict):
        out["properties"] = {
            k: _genai_schema_to_json_schema(v) for k, v in props.items()
        }
    items = node.get("items")
    if items is not None:
        out["items"] = _genai_schema_to_json_schema(items)
    if node.get("required"):
        out["required"] = list(node["required"])
    # Bedrock requires object schemas to at least declare type=object.
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    return out


def tool_declarations_to_bedrock_tools(
    tool_declarations: list[genai_types.FunctionDeclaration] | None,
) -> list[dict[str, Any]]:
    """Convert genai FunctionDeclarations to Bedrock ``tools[]`` (toolSpec)."""
    tools: list[dict[str, Any]] = []
    for decl in tool_declarations or []:
        dumped = decl.model_dump(mode="json", exclude_none=True)
        params = dumped.get("parameters")
        if params:
            schema = _genai_schema_to_json_schema(params)
        else:
            schema = {"type": "object", "properties": {}}
        if schema.get("type") != "object":
            schema = {"type": "object", "properties": {}}
        tools.append(
            {
                "toolSpec": {
                    "name": dumped["name"],
                    "description": (dumped.get("description") or dumped["name"])[
                        :1000
                    ],
                    "inputSchema": {"json": schema},
                }
            }
        )
    return tools


# --------------------------------------------------------------------------- #
# History conversion: genai Content[] -> Bedrock messages[] + system[]
# --------------------------------------------------------------------------- #


def _coalesce(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive same-role messages — Bedrock rejects two assistant (or
    two user) messages in a row, but the codebase emits one Content per part
    (text turn + function_call turn are both ``model``)."""
    merged: list[dict[str, Any]] = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"].extend(m["content"])
        else:
            merged.append({"role": m["role"], "content": list(m["content"])})
    return merged


def contents_to_bedrock_messages(
    contents: list[genai_types.Content],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert genai ``contents`` to ``(system_blocks, messages)``.

    genai roles ``user``/``model`` map to Bedrock ``user``/``assistant``.
    A function_call Part -> ``toolUse``; a function_response Part ->
    ``toolResult``. toolUse/toolResult ids must match across the pair; when
    the source call_id is None (legacy Gemini history) we synthesize a stable
    id and pair by arrival order.
    """
    system_blocks: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    pending_ids: deque[str] = deque()
    counter = 0

    def _next_id() -> str:
        nonlocal counter
        counter += 1
        return f"tooluse_{counter}"

    for content in contents:
        role = getattr(content, "role", "user") or "user"
        bedrock_role = "assistant" if role == "model" else "user"
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
                        "toolUse": {
                            "toolUseId": tid,
                            "name": fc.name,
                            "input": dict(getattr(fc, "args", None) or {}),
                        }
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
                        "toolResult": {
                            "toolUseId": tid,
                            "content": [{"json": resp}],
                        }
                    }
                )
            elif text:
                if bedrock_role == "user" or role == "user":
                    blocks.append({"text": text})
                else:
                    blocks.append({"text": text})
        if blocks:
            messages.append({"role": bedrock_role, "content": blocks})

    return system_blocks, _coalesce(messages)


# --------------------------------------------------------------------------- #
# Inline <thinking> stripper (narration thinking-strip)
# --------------------------------------------------------------------------- #
#
# The tags we suppress are ``<thinking>`` / ``</thinking>`` (case-insensitive,
# whitespace-tolerant — see ``_ThinkingStripper._match_tag``).  The amount of
# trailing text held back across a delta boundary is NOT a fixed length: the
# matcher recognises a partial tag from any prefix and holds exactly that
# prefix, so an arbitrarily-padded tag (``< thinking >``) is still buffered
# correctly until disambiguated.


class _ThinkingStripper:
    """Streaming state machine that removes inline ``<thinking>...</thinking>``.

    Amazon Nova writes its chain-of-thought as literal ``<thinking>`` tags
    INSIDE the normal text content block, so it arrives under
    ``delta['text']`` and would otherwise stream straight to chat as visible
    narration.  Claude, by contrast, emits reasoning in a SEPARATE
    ``reasoningContent`` block (dropped explicitly in the producer), so this
    stripper is a no-op for Claude — but it is robust per-model, not a Nova
    special-case.

    The tags arrive SPLIT across deltas (a per-delta ``re.sub`` would fail to
    match a tag straddling a chunk boundary), so this maintains state across
    ``feed`` calls:

      * ``_in_think`` — currently inside a ``<thinking>`` span (suppress).
      * ``_buf`` — a small TAIL of trailing text we have not yet emitted
        because it could be the start of a partial ``<thinking>`` /
        ``</thinking>`` tag spanning into the next delta.

    Matching is case-insensitive and tolerates whitespace immediately after
    ``<thinking``/``</thinking`` and before the closing ``>`` (e.g.
    ``< thinking >``); ATTRIBUTES are also tolerated
    (``<thinking foo="bar">``) — any span up to the first ``>`` after the body
    is swallowed — see ``_match_tag``.  ``feed`` returns the text that is safe
    to emit NOW; ``flush`` returns whatever post-thinking narration remains at
    end-of-stream (an unclosed ``<thinking>`` span is dropped rather than
    leaked as raw tags, but a buffered partial that is NOT actually a
    thinking-tag prefix — e.g. a lone trailing ``<`` of genuine narration — is
    emitted, and real text after a closed span is never swallowed).
    """

    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    @staticmethod
    def _match_tag(text: str, pos: int, *, closing: bool) -> int | None:
        """Try to match a ``<thinking>`` (open) / ``</thinking>`` (close) at *pos*.

        Whitespace is tolerated after the leading ``<``, after the closing
        slash (``</ thinking>``), and before the trailing ``>``; matching is
        case-insensitive.  ATTRIBUTES are tolerated on the OPEN tag
        (``<thinking foo="bar">``): once the body ``thinking`` is followed by
        whitespace, any span up to the first ``>`` is consumed as an attribute
        list and the whole tag is suppressed.  Returns the index just past
        ``>`` on a full match, ``-1`` when the text from *pos* could still be a
        PARTIAL of this tag (caller must hold and wait for more), or ``None``
        when it definitively is not this tag.
        """
        n = len(text)
        if pos >= n or text[pos] != "<":
            return None
        i = pos + 1
        # optional whitespace after '<'
        while i < n and text[i].isspace():
            i += 1
        if closing:
            if i >= n:
                return -1  # '<' (+ws) only so far — could still become '</...'
            if text[i] != "/":
                return None
            i += 1
            # optional whitespace after '/'
            while i < n and text[i].isspace():
                i += 1
        # match the literal body "thinking" (case-insensitive)
        body = "thinking"
        j = 0
        while i < n and j < len(body) and text[i].lower() == body[j]:
            i += 1
            j += 1
        if j < len(body):
            # ran out of text mid-body -> partial if we consumed everything
            return -1 if i >= n else None
        # After the body, the tag closes either immediately at '>', or after a
        # whitespace-introduced attribute span (e.g. ``<thinking foo="bar">``).
        # A non-whitespace, non-'>' char immediately after the body means this
        # is a DIFFERENT tag (e.g. ``<thinkingx>``) — not a thinking tag.
        if i < n and not text[i].isspace() and text[i] != ">":
            return None
        # consume the (possibly attribute-bearing) remainder up to the first '>'
        while i < n and text[i] != ">":
            i += 1
        if i >= n:
            return -1  # body matched but '>' not yet arrived -> partial
        return i + 1  # text[i] == '>'

    def _scan_for_tag(self, text: str, pos: int) -> tuple[str | None, int]:
        """Return ``(kind, end)`` for a tag at *pos*.

        ``kind`` is ``"open"`` / ``"close"`` on a full match (``end`` is the
        index past ``>``), ``"partial"`` when the text could still grow into a
        tag (``end`` == ``pos``), or ``(None, pos)`` when no tag starts here.
        """
        open_end = self._match_tag(text, pos, closing=False)
        if open_end is not None and open_end >= 0:
            return "open", open_end
        close_end = self._match_tag(text, pos, closing=True)
        if close_end is not None and close_end >= 0:
            return "close", close_end
        if open_end == -1 or close_end == -1:
            return "partial", pos
        return None, pos

    def feed(self, text: str) -> str:
        """Consume a text delta, returning the text safe to emit now."""
        if not text:
            return ""
        work = self._buf + text
        self._buf = ""
        out: list[str] = []
        i = 0
        n = len(work)
        while i < n:
            ch = work[i]
            if ch == "<":
                kind, end = self._scan_for_tag(work, i)
                if kind == "open":
                    self._in_think = True
                    i = end
                    continue
                if kind == "close":
                    self._in_think = False
                    i = end
                    continue
                if kind == "partial":
                    # Could be the start of a real tag spanning into the next
                    # delta — hold the remainder and wait for more text.
                    self._buf = work[i:]
                    i = n
                    break
                # Not a tag — a literal '<'. Emit it (if outside thinking).
                if not self._in_think:
                    out.append(ch)
                i += 1
                continue
            # plain char: find the next '<' and emit/suppress the run.
            nxt = work.find("<", i)
            if nxt == -1:
                segment = work[i:]
                i = n
            else:
                segment = work[i:nxt]
                i = nxt
            if not self._in_think:
                out.append(segment)
        return "".join(out)

    @staticmethod
    def _buf_is_thinking_prefix(buf: str) -> bool:
        """Is *buf* a genuine PREFIX of a ``<thinking>``/``</thinking>`` tag?

        ``_buf`` is only ever set from ``feed``'s ``partial`` branch, so it
        always begins with ``<`` and could-in-principle still grow into a tag.
        But "could grow" includes the trivial case of a LONE ``<`` (optionally
        followed by whitespace) which has not yet committed to anything — that
        is far more likely to be genuine narration (a trailing ``<`` of real
        text) than the start of a thinking tag.  This returns ``True`` only
        when *buf* has progressed PAST the bare ``<`` (+ optional whitespace)
        into actual tag content — a ``/`` (closing) or the first letter of the
        case-insensitive body ``thinking``.  Such a prefix is dropped at EOS
        (an unfinished/unclosed thinking tag); everything else is emitted.
        """
        n = len(buf)
        if n == 0 or buf[0] != "<":
            return False
        i = 1
        while i < n and buf[i].isspace():
            i += 1
        if i >= n:
            return False  # '<' (+ whitespace) only — not committed to a tag
        ch = buf[i]
        if ch == "/":
            return True  # '</...' — a closing-tag prefix
        return ch.lower() == "t"  # first body char of "thinking"

    def flush(self) -> str:
        """End-of-stream flush.

        Returns whatever narration is safe to emit at end-of-stream:

          * If a ``<thinking>`` span is still OPEN, the trailing suppressed
            content was thinking — emit nothing (the span never closed).
          * ``self._buf`` is set ONLY in ``feed``'s ``partial`` branch, so a
            non-empty buffer is a held ``<...`` that never disambiguated.  If it
            is a genuine thinking-tag prefix (``"<thin"``, ``"</th"``, ...) it
            is an unfinished tag and is DROPPED (no raw partial-tag leak).  If
            it is merely a lone trailing ``<`` (+ optional whitespace) of real
            narration — ``feed(["done<"]) -> "done"`` then ``flush() -> "<"`` —
            it is genuine text and is EMITTED rather than lost.

        Real post-thinking narration is emitted by ``feed`` the moment the span
        CLOSES, so it is never swallowed here.
        """
        buf = self._buf
        self._buf = ""
        if self._in_think:
            # Unclosed thinking span — buffered partial is suppressed content.
            return ""
        if self._buf_is_thinking_prefix(buf):
            return ""  # unfinished thinking tag — drop rather than leak
        return buf  # genuine trailing narration (e.g. a lone '<')


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #


async def stream_bedrock(
    contents: list[genai_types.Content],
    tool_declarations: list[genai_types.FunctionDeclaration] | None = None,
    system_prompt: str | None = None,
    model: str | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream one Bedrock Converse turn, yielding the GRACE ``StreamEvent`` union.

    Mirrors ``adapter.stream_events_with_contents``: one call == one model
    round. The dispatch loop in ``server.py`` appends function_call +
    function_response Contents and re-calls until no tool calls remain.

    boto3's ``converse_stream`` is synchronous and returns an EventStream; we
    run it in an executor thread feeding an ``asyncio.Queue`` — exactly the
    producer/consumer pattern the Gemini path uses — so cancellation and
    back-pressure behave identically.
    """
    loop = asyncio.get_running_loop()
    # Bedrock prompt-caching restored here (job — bill fix): caches the static
    # system prompt + 94-tool catalog across turns via cachePoint markers.
    kwargs = _build_converse_kwargs(contents, tool_declarations, system_prompt, model)

    queue: asyncio.Queue[StreamEvent | None | BaseException] = asyncio.Queue()

    def _producer() -> None:
        try:
            client = _bedrock_client()
            resp = client.converse_stream(**kwargs)
            # Per-contentBlock accumulation of streamed toolUse input JSON.
            tool_blocks: dict[int, dict[str, Any]] = {}
            # Per-turn inline <thinking> stripper: Amazon Nova writes its
            # chain-of-thought as literal <thinking>...</thinking> INSIDE the
            # normal text block (so it arrives under delta['text']); the tags
            # split across deltas, so a streaming state machine — not a
            # per-delta re.sub — is required to suppress them.  Claude routes
            # reasoning through a separate reasoningContent block (dropped
            # below), so this is a no-op for Claude.
            stripper = _ThinkingStripper()
            for event in resp["stream"]:
                if "contentBlockStart" in event:
                    start = event["contentBlockStart"]
                    idx = start.get("contentBlockIndex", 0)
                    tu = start.get("start", {}).get("toolUse")
                    if tu:
                        tool_blocks[idx] = {
                            "name": tu.get("name"),
                            "toolUseId": tu.get("toolUseId"),
                            "buf": "",
                        }
                elif "contentBlockDelta" in event:
                    d = event["contentBlockDelta"]
                    idx = d.get("contentBlockIndex", 0)
                    delta = d.get("delta", {})
                    if "reasoningContent" in delta:
                        # Claude emits chain-of-thought in a SEPARATE
                        # reasoningContent block (reasoningText / redactedContent
                        # / signature sub-keys).  This is model THINKING, not
                        # user-facing narration — drop it outright.  Documenting
                        # the branch explicitly (rather than letting it fall
                        # through) makes the intent unmistakable.
                        pass
                    elif "text" in delta and delta["text"]:
                        # Route through the inline <thinking> stripper: Nova
                        # writes its thinking as literal tags in this text
                        # stream, split across deltas.  Emit only the cleaned,
                        # outside-thinking text.
                        clean = stripper.feed(delta["text"])
                        if clean:
                            loop.call_soon_threadsafe(
                                queue.put_nowait, TextDeltaEvent(delta=clean)
                            )
                    elif "toolUse" in delta and idx in tool_blocks:
                        tool_blocks[idx]["buf"] += delta["toolUse"].get("input", "")
                elif "contentBlockStop" in event:
                    idx = event["contentBlockStop"].get("contentBlockIndex", 0)
                    tb = tool_blocks.pop(idx, None)
                    if tb is not None:
                        try:
                            args = json.loads(tb["buf"]) if tb["buf"] else {}
                        except json.JSONDecodeError:
                            args = {}
                        loop.call_soon_threadsafe(
                            queue.put_nowait,
                            FunctionCallEvent(
                                name=tb["name"],
                                call_id=tb["toolUseId"],
                                args=args if isinstance(args, dict) else {},
                            ),
                        )
                elif "metadata" in event:
                    usage = event["metadata"].get("usage", {}) or {}
                    cached = usage.get("cacheReadInputTokens")
                    ev = UsageMetadataEvent(
                        cached_content_token_count=cached,
                        total_token_count=usage.get("totalTokens"),
                        prompt_token_count=usage.get("inputTokens"),
                        candidates_token_count=usage.get("outputTokens"),
                        cache_hit=bool(cached and cached > 0),
                    )
                    loop.call_soon_threadsafe(queue.put_nowait, ev)
            # Flush any buffered tail held back across delta boundaries. A
            # closed thinking span leaves only real post-thinking narration
            # here; a buffered partial that is genuine trailing text (a lone
            # '<') is emitted, while an unclosed <thinking> span or an
            # unfinished thinking-tag prefix is dropped (the stripper returns
            # "" so raw tags never leak to chat).
            trailing = stripper.flush()
            if trailing:
                loop.call_soon_threadsafe(
                    queue.put_nowait, TextDeltaEvent(delta=trailing)
                )
            loop.call_soon_threadsafe(queue.put_nowait, None)
        except BaseException as exc:  # noqa: BLE001 — surface to caller
            loop.call_soon_threadsafe(queue.put_nowait, exc)

    producer_task = loop.run_in_executor(None, _producer)
    try:
        while True:
            item = await queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    except asyncio.CancelledError:
        producer_task.cancel()
        raise
