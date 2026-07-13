"""OpenAI-compatible LLM provider adapter (offline/local build -- GAP 1).

``MODEL_PROVIDER=openai`` selects this path. It accepts the SAME inputs that
``bedrock_adapter.stream_bedrock`` accepts -- a ``list[genai_types.Content]``
history + a list of ``genai_types.FunctionDeclaration`` tool specs + a system
prompt -- converts them to OpenAI chat-completions wire shapes at the boundary,
and yields the SAME ``StreamEvent`` union (``TextDeltaEvent`` /
``FunctionCallEvent`` / ``UsageMetadataEvent`` / ``CompactionStartEvent`` /
``CompactionCompleteEvent``) that the server.py dispatch loop consumes.

This single provider covers any OpenAI-compatible endpoint:
  - Local: Ollama (http://localhost:11434/v1), vLLM, llama.cpp server, LM Studio
  - Cloud: OpenAI, Groq, DeepSeek, OpenRouter, Anthropic (messages-compat API)

Config env vars (all read at call time so an ECS/systemd env injection works
without re-import):

  GRACE2_OPENAI_BASE_URL  (REQUIRED when MODEL_PROVIDER=openai; no default)
  GRACE2_OPENAI_API_KEY   (default "not-needed" -- local endpoints ignore it)
  GRACE2_OPENAI_MODEL     (the default model; a per-turn selection from the
                           web model selector overrides it -- see openai_model)
  MODEL_PROVIDER=openai   (selects this adapter from the dispatch seam)

Design notes:

  1. genai Content[] -> OpenAI messages[]
     - ``user`` role -> ``"user"``; ``model`` role -> ``"assistant"``
     - assistant function_call Part -> ``"assistant"`` message with ``tool_calls``
       list; ``tool_call_id`` is minted deterministically (``"call_{counter}"``
       per turn if the genai id is absent).
     - function_response Part -> ``"tool"`` message with matching ``tool_call_id``
       and JSON-serialised content; ids are resolved by pairing arrivals in order
       (mirroring the bedrock_adapter queue strategy).
     - Consecutive same-role messages are coalesced to satisfy the OpenAI API
       requirement that roles alternate (or at minimum that tool-result sequences
       form a legal run).

  2. FunctionDeclaration[] -> OpenAI tools[]
     - ``_genai_schema_to_json_schema`` converts genai uppercase enum types to
       lowercase JSON Schema (mirrors bedrock_adapter._genai_schema_to_json_schema).
     - The same sanitisation pass is applied: empty parameters -> object with
       ``{}``, non-object top-level schema -> wrapped in object.

  3. Streaming
     - ``stream_options={"include_usage": True}`` is sent so the final chunk
       carries usage metadata (usage is tolerated absent for providers that do
       not support it).
     - ``max_tokens`` (``context_budget.openai_max_output_tokens``, env
       ``GRACE2_OPENAI_MAX_TOKENS``, default 4096) caps every request so a
       clipped/looping round cannot run away for minutes before the reactive
       clip guard (below) gets a chance to react at stream end (BUG 3,
       post-OPEN-14 acceptance rerun).
     - Tool-call argument deltas are accumulated per index (``delta.tool_calls``
       index field) across chunks; on ``finish_reason=="tool_calls"`` the
       accumulated JSON is parsed and ``FunctionCallEvent``s are emitted.
     - Text deltas -> ``TextDeltaEvent`` as they arrive.
     - Usage on the final chunk -> ``UsageMetadataEvent`` (best-effort).

  4. Bedrock-style id compatibility
     - When the session's selected model id looks like a Bedrock inference-profile
       id (contains ``anthropic.`` / ``us.`` / ``:0``), GRACE2_OPENAI_MODEL
       overrides it and a one-shot warning is logged.

The ``openai`` package (``openai>=1.40``) is a hard dependency only when this
adapter is active; the import lives inside the streaming function so the rest of
the agent starts cleanly on environments where the package is not installed.
"""

from __future__ import annotations

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
    ThinkingDeltaEvent,
    UsageMetadataEvent,
)
from .context_budget import (
    ContextWindowExceededError,
    compact_contents,
    compute_budget_tokens,
    discover_num_ctx,
    estimate_tokens,
    estimate_tokens_for_messages,
    estimate_tokens_for_tools,
    is_prompt_clipped,
    openai_max_output_tokens,
    proactive_target_ratio,
    reactive_target_ratio,
)

logger = logging.getLogger("grace2_agent.openai_adapter")

#: Baked local-model tool-discipline system line (2026-07-13, OPEN-17 class:
#: a 0-event fetch was followed by a publish_layer call carrying an invented
#: placeholder handle). Appended to EVERY openai-path system prompt in
#: ``contents_to_openai_messages`` - see the call-site comment for why the
#: start_agent.sh GRACE2_OPENAI_EXTRA_SYSTEM default is not enough.
_TOOL_DISCIPLINE_SYSTEM = (
    "Fetch and composer tools publish their own layers - only call "
    "publish_layer when you have a handle returned by a previous tool "
    "result, passed verbatim. If a fetch returns no data, say so and stop."
)

# Logged once per process if the session model id looks like a Bedrock id.
_BEDROCK_ID_WARN_DONE = False

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_BEDROCK_ID_PATTERNS = (
    "anthropic.",
    "us.anthropic",
    "us.amazon",
    "us.deepseek",
    ":0",
)


def _looks_like_bedrock_id(model_id: str) -> bool:
    return any(p in model_id for p in _BEDROCK_ID_PATTERNS)


def openai_base_url() -> str:
    """Return GRACE2_OPENAI_BASE_URL; raise clearly if unset."""
    url = os.environ.get("GRACE2_OPENAI_BASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "MODEL_PROVIDER=openai requires GRACE2_OPENAI_BASE_URL to be set "
            "(e.g. http://localhost:11434/v1 for Ollama). "
            "Set it in the environment and restart the agent."
        )
    return url


def openai_api_key() -> str:
    return os.environ.get("GRACE2_OPENAI_API_KEY", "not-needed")


def openai_model(session_model: str | None = None) -> str:
    """Resolve the OpenAI model name to send.

    Precedence (F2, live-feedback 2026-07-08: local hot-swap):
      1. session_model if it does NOT look like a Bedrock inference-profile id
         -- the per-turn selection from the web model selector (which, in the
         local build, lists the REAL installed Ollama models via the agent's
         /api/local-models endpoint). This must OVERRIDE the env default so
         picking a model in the UI actually changes the serving model.
      2. GRACE2_OPENAI_MODEL env var (the configured default)
      3. Raise if nothing is configured

    A Bedrock-shaped session id (stale localStorage from a cloud session) is
    ignored with a one-shot warning and falls through to the env default --
    same guard as before, just no longer masked by the env-always-wins rule.
    This function is only reached when MODEL_PROVIDER=openai, so the cloud
    (Bedrock) path is untouched by the precedence flip.
    """
    global _BEDROCK_ID_WARN_DONE
    configured = os.environ.get("GRACE2_OPENAI_MODEL", "").strip()
    if session_model:
        if _looks_like_bedrock_id(session_model):
            if not _BEDROCK_ID_WARN_DONE:
                logger.warning(
                    "openai_adapter: session model %r looks like a Bedrock id; "
                    "ignoring it for the OpenAI path. Set GRACE2_OPENAI_MODEL "
                    "to the local/OpenAI model name (e.g. llama3.2:3b).",
                    session_model,
                )
                _BEDROCK_ID_WARN_DONE = True
        else:
            return session_model
    if configured:
        return configured
    raise RuntimeError(
        "MODEL_PROVIDER=openai requires GRACE2_OPENAI_MODEL to be set "
        "(e.g. 'llama3.2:3b' for Ollama or 'gpt-4o' for OpenAI). "
        "Set it in the environment and restart the agent."
    )


# ---------------------------------------------------------------------------
# Schema conversion: genai FunctionDeclaration -> OpenAI tools[]
# (mirrors bedrock_adapter._genai_schema_to_json_schema / tool_declarations_to_bedrock_tools)
# ---------------------------------------------------------------------------

_TYPE_MAP = {
    "STRING": "string",
    "NUMBER": "number",
    "INTEGER": "integer",
    "BOOLEAN": "boolean",
    "ARRAY": "array",
    "OBJECT": "object",
    "TYPE_UNSPECIFIED": "string",
}

# ---------------------------------------------------------------------------
# Tool-schema slimming (LOCAL path only -- 2026-07-12 context-window fix)
# ---------------------------------------------------------------------------
#
# MEASURED FAILURE THIS FIXES: on qwen3:8b-16k (num_ctx=16384) the tool
# schemas + system prompt alone were ~15.7k tokens on the wire, so a 2-prompt
# session hit an honest CONTEXT_WINDOW_EXCEEDED abort while the actual
# conversation content was only ~6k tokens (live log 2026-07-12: whole-prompt
# est 22107 vs budget 11264). The registry's tool/param descriptions are
# written for large cloud models; the local wire caps them at schema-BUILD
# time instead. ONLY description strings are ever touched -- name, type,
# enum, required, properties, items structure pass through untouched (guard
# rail), so the truncation can never break the JSON schema contract.
#
# GRACE2_OPENAI_TOOL_DESC_CAP (default 600) caps each TOOL description;
# GRACE2_OPENAI_PARAM_DESC_CAP (default 200) caps each PARAMETER (and nested
# schema) description. Setting GRACE2_OPENAI_TOOL_DESC_CAP=0 disables ALL
# slimming (the single kill switch -- restores the legacy [:1000] behavior);
# GRACE2_OPENAI_PARAM_DESC_CAP=0 disables only the param-level cap.
# Truncation is word-boundary with a trailing "..." marker. bedrock_adapter
# and adapter.py are deliberately NOT touched -- cloud models keep the full
# descriptions.

TOOL_DESC_CAP_DEFAULT = 600
PARAM_DESC_CAP_DEFAULT = 200

_TRUNC_MARKER = "..."

# One INFO stats line per process (startup/first-build), not per round.
_SCHEMA_STATS_LOGGED = False


def _desc_cap_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return val if val >= 0 else default


def tool_desc_cap() -> int:
    """``GRACE2_OPENAI_TOOL_DESC_CAP`` -- per-tool description cap in chars
    (default 600). 0 disables all tool-schema description slimming."""
    return _desc_cap_env("GRACE2_OPENAI_TOOL_DESC_CAP", TOOL_DESC_CAP_DEFAULT)


def param_desc_cap() -> int:
    """``GRACE2_OPENAI_PARAM_DESC_CAP`` -- per-parameter description cap in
    chars (default 200). 0 disables the param-level cap only."""
    return _desc_cap_env("GRACE2_OPENAI_PARAM_DESC_CAP", PARAM_DESC_CAP_DEFAULT)


def _truncate_word_boundary(text: str, cap: int) -> str:
    """Truncate ``text`` to at most ``cap`` chars, cutting at a word boundary
    and appending a trailing ``...`` marker. Text that already fits is
    returned unchanged (identity -- no marker)."""
    if cap <= 0 or len(text) <= cap:
        return text
    room = max(cap - len(_TRUNC_MARKER), 1)
    cut = text[:room]
    space = cut.rfind(" ")
    # Back up to the last space so we never cut mid-word; but if the text has
    # no usable space in the back half (e.g. one giant token), hard-cut
    # rather than throwing away most of the budget.
    if space > room // 2:
        cut = cut[:space]
    return cut.rstrip() + _TRUNC_MARKER


def _cap_schema_descriptions(schema: Any, cap: int) -> None:
    """Recursively truncate every ``description`` string inside a converted
    JSON Schema, in place. ONLY ``description`` values are modified --
    type/enum/format/required and the properties/items STRUCTURE are never
    touched (guard rail: the cap can only ever shorten prose, never break
    the schema contract)."""
    if cap <= 0 or not isinstance(schema, dict):
        return
    desc = schema.get("description")
    if isinstance(desc, str):
        schema["description"] = _truncate_word_boundary(desc, cap)
    props = schema.get("properties")
    if isinstance(props, dict):
        for sub in props.values():
            _cap_schema_descriptions(sub, cap)
    _cap_schema_descriptions(schema.get("items"), cap)


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
    # Ensure object schemas declare type.
    if out.get("type") == "object" and "properties" not in out:
        out["properties"] = {}
    return out


def tool_declarations_to_openai_tools(
    tool_declarations: list[genai_types.FunctionDeclaration] | None,
) -> list[dict[str, Any]]:
    """Convert genai FunctionDeclarations to OpenAI ``tools[]`` (function type).

    Applies the LOCAL-wire description caps (``tool_desc_cap`` /
    ``param_desc_cap``, see the slimming block above) at build time, and logs
    ONE INFO line (first build per process) with the total serialized size
    before and after capping so the win is measurable in the agent log."""
    global _SCHEMA_STATS_LOGGED
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
                "type": "function",
                "function": {
                    "name": dumped["name"],
                    "description": (dumped.get("description") or dumped["name"])[:1000],
                    "parameters": schema,
                },
            }
        )

    t_cap = tool_desc_cap()
    want_stats = bool(tools) and not _SCHEMA_STATS_LOGGED
    before_chars = len(json.dumps(tools, default=str)) if want_stats else 0

    if t_cap > 0:
        p_cap = param_desc_cap()
        for tool in tools:
            fn = tool["function"]
            fn["description"] = _truncate_word_boundary(fn["description"], t_cap)
            _cap_schema_descriptions(fn["parameters"], p_cap)

    if want_stats:
        after_chars = len(json.dumps(tools, default=str))
        logger.info(
            "tool-schema slimming: %d tools, before=%d chars (~%d tokens est) "
            "after=%d chars (~%d tokens est) tool_desc_cap=%d param_desc_cap=%d",
            len(tools),
            before_chars,
            (before_chars + 3) // 4,
            after_chars,
            (after_chars + 3) // 4,
            t_cap,
            param_desc_cap() if t_cap > 0 else 0,
        )
        _SCHEMA_STATS_LOGGED = True
    return tools


# ---------------------------------------------------------------------------
# History conversion: genai Content[] -> OpenAI messages[]
# ---------------------------------------------------------------------------


def _coalesce_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive messages of the same role (except tool messages).

    OpenAI requires that consecutive same-role assistant messages be merged.
    Tool (role=='tool') messages must NEVER be merged -- each carries its own
    tool_call_id and must remain separate.
    """
    merged: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "tool":
            merged.append(m)
            continue
        if (
            merged
            and merged[-1]["role"] == m["role"]
            and merged[-1]["role"] != "tool"
        ):
            # Merge: extend content list or concatenate text strings.
            prev = merged[-1]
            prev_content = prev.get("content")
            cur_content = m.get("content")
            if isinstance(prev_content, list) and isinstance(cur_content, list):
                prev_content.extend(cur_content)
            elif isinstance(prev_content, str) and isinstance(cur_content, str):
                prev["content"] = prev_content + "\n" + cur_content
            elif isinstance(prev_content, list) and isinstance(cur_content, str):
                prev_content.append({"type": "text", "text": cur_content})
            # Also merge tool_calls if both have them.
            if m.get("tool_calls"):
                if prev.get("tool_calls"):
                    prev["tool_calls"].extend(m["tool_calls"])
                else:
                    prev["tool_calls"] = m["tool_calls"]
        else:
            merged.append(dict(m))
    return merged


def contents_to_openai_messages(
    contents: list[genai_types.Content],
    system_prompt: str | None = None,
    show_thinking: bool = False,
) -> list[dict[str, Any]]:
    """Convert genai ``contents`` to OpenAI ``messages[]``.

    genai roles:
      ``user``  -> ``"user"``
      ``model`` -> ``"assistant"``
    function_call Part -> ``"assistant"`` message with ``tool_calls``
    function_response Part -> ``"tool"`` message with tool_call_id

    tool_call_id is harvested from fc.id; when absent (legacy Gemini history),
    a stable deterministic id ``"call_{counter}"`` is minted. function_response
    ids are resolved by pairing with the preceding function_call by arrival order
    (same FIFO queue strategy as bedrock_adapter.contents_to_bedrock_messages).

    ``show_thinking`` (NATE live-feedback 2026-07-08, local build): when True
    any ``/no_think`` directive inside GRACE2_OPENAI_EXTRA_SYSTEM is dropped
    for THIS round so the qwen3-family reasoning channel is generated (and
    streamed back as ``ThinkingDeltaEvent``s by ``stream_openai``). Other
    extra-system text is preserved.
    """
    messages: list[dict[str, Any]] = []
    # GRACE2_OPENAI_EXTRA_SYSTEM: optional text appended to the system prompt.
    # Primary use: "/no_think" for Qwen3-family models served by Ollama, whose
    # default thinking mode routes ALL tokens to the reasoning channel -- the
    # OpenAI-compat content deltas arrive empty and the turn renders no text.
    # Generic seam (any provider-specific system suffix), dormant unless set.
    extra_system = os.environ.get("GRACE2_OPENAI_EXTRA_SYSTEM", "").strip()
    if extra_system and show_thinking:
        # Thinking display ON for this turn: omit the /no_think suppressor
        # (keep any unrelated extra-system text verbatim).
        extra_system = extra_system.replace("/no_think", "").strip()
    if extra_system:
        system_prompt = f"{system_prompt}\n{extra_system}" if system_prompt else extra_system
    # 2026-07-13 (local small-model tool discipline, OPEN-17 class): baked
    # HERE - not only in start_agent.sh's GRACE2_OPENAI_EXTRA_SYSTEM default -
    # because a user .env.local that sets EXTRA_SYSTEM (e.g. a bare
    # "/no_think") silently SHADOWS that baked default (live-proven
    # 2026-07-13: the running agent's env carried only "/no_think"). The
    # openai path is the local build's path, so this stays local-only.
    system_prompt = (
        f"{system_prompt}\n{_TOOL_DISCIPLINE_SYSTEM}"
        if system_prompt
        else _TOOL_DISCIPLINE_SYSTEM
    )
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})

    pending_ids: deque[str] = deque()
    counter = 0

    def _next_id() -> str:
        nonlocal counter
        counter += 1
        return f"call_{counter}"

    for content in contents:
        role = getattr(content, "role", "user") or "user"
        oai_role = "assistant" if role == "model" else "user"
        parts = getattr(content, "parts", None) or []

        # Collect tool calls and text for assistant turns.
        tool_calls: list[dict[str, Any]] = []
        text_parts: list[str] = []
        tool_results: list[dict[str, Any]] = []

        for part in parts:
            fc = getattr(part, "function_call", None)
            fr = getattr(part, "function_response", None)
            text = getattr(part, "text", None)

            if fc is not None and getattr(fc, "name", None):
                tid = getattr(fc, "id", None) or _next_id()
                pending_ids.append(tid)
                args = dict(getattr(fc, "args", None) or {})
                tool_calls.append(
                    {
                        "id": tid,
                        "type": "function",
                        "function": {
                            "name": fc.name,
                            "arguments": json.dumps(args),
                        },
                    }
                )
            elif fr is not None and getattr(fr, "name", None):
                tid = getattr(fr, "id", None) or (
                    pending_ids.popleft() if pending_ids else _next_id()
                )
                resp = getattr(fr, "response", None)
                if not isinstance(resp, dict):
                    resp = {"result": resp}
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": tid,
                        "content": json.dumps(resp),
                    }
                )
            elif text:
                text_parts.append(text)

        # Build the message(s) for this content.
        if tool_results:
            # Tool result messages go as individual "tool" role messages.
            messages.extend(tool_results)
        elif tool_calls:
            # Assistant turn with tool calls (may also have text).
            msg: dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
            if text_parts:
                msg["content"] = "\n".join(text_parts)
            else:
                msg["content"] = None
            messages.append(msg)
        elif text_parts:
            messages.append({"role": oai_role, "content": "\n".join(text_parts)})

    return _coalesce_messages(messages)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


async def _stream_one_round(
    client: Any, kwargs: dict[str, Any]
) -> AsyncIterator[StreamEvent]:
    """Stream ONE ``chat.completions.create`` round, yielding the GRACE
    ``StreamEvent`` union. Split out of ``stream_openai`` (OPEN-14) so the
    caller can wrap it in the clip-guard retry loop without duplicating the
    chunk-accumulation logic.
    """
    # Per-index accumulator for fragmented tool-call argument deltas.
    # Structure: {index: {"id": str, "name": str, "args_buf": str}}
    tool_call_accumulators: dict[int, dict[str, Any]] = {}

    async with await client.chat.completions.create(**kwargs) as stream:  # type: ignore[attr-defined]
        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            for choice in choices:
                delta = getattr(choice, "delta", None)

                if delta is None:
                    continue

                # Text delta.
                text = getattr(delta, "content", None)
                if text:
                    yield TextDeltaEvent(delta=text)

                # Reasoning delta (NATE live-feedback 2026-07-08): Ollama's
                # OpenAI-compat stream carries qwen3-family thinking as
                # ``delta.reasoning`` (verified live); DeepSeek-style servers
                # use ``delta.reasoning_content``. The openai SDK's ChoiceDelta
                # tolerates extra fields, but read defensively via getattr +
                # the pydantic ``model_extra`` bag so an SDK that drops unknown
                # attrs still surfaces the channel. Always yielded when
                # present -- the server gates FORWARDING on the per-turn user
                # toggle, and with /no_think armed the channel is simply not
                # generated.
                reasoning = getattr(delta, "reasoning", None) or getattr(
                    delta, "reasoning_content", None
                )
                if not reasoning:
                    extra = getattr(delta, "model_extra", None)
                    if isinstance(extra, dict):
                        reasoning = extra.get("reasoning") or extra.get(
                            "reasoning_content"
                        )
                if reasoning and isinstance(reasoning, str):
                    yield ThinkingDeltaEvent(delta=reasoning)

                # Tool-call deltas: accumulate by index.
                tc_deltas = getattr(delta, "tool_calls", None) or []
                for tc_delta in tc_deltas:
                    idx = tc_delta.index
                    if idx not in tool_call_accumulators:
                        tool_call_accumulators[idx] = {
                            "id": "",
                            "name": "",
                            "args_buf": "",
                        }
                    acc = tool_call_accumulators[idx]
                    if tc_delta.id:
                        acc["id"] += tc_delta.id
                    fn = getattr(tc_delta, "function", None)
                    if fn:
                        if getattr(fn, "name", None):
                            acc["name"] += fn.name
                        if getattr(fn, "arguments", None):
                            acc["args_buf"] += fn.arguments

            # Usage on the last chunk (stream_options include_usage).
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                prompt_tokens = getattr(usage, "prompt_tokens", None)
                completion_tokens = getattr(usage, "completion_tokens", None)
                total_tokens = getattr(usage, "total_tokens", None)
                if any(v is not None for v in (prompt_tokens, completion_tokens, total_tokens)):
                    yield UsageMetadataEvent(
                        prompt_token_count=prompt_tokens,
                        candidates_token_count=completion_tokens,
                        total_token_count=total_tokens,
                        cached_content_token_count=None,
                        cache_hit=False,
                    )

    # After the stream, emit any accumulated tool calls.
    for _idx, acc in sorted(tool_call_accumulators.items()):
        if not acc["name"]:
            continue
        try:
            args = json.loads(acc["args_buf"]) if acc["args_buf"].strip() else {}
        except json.JSONDecodeError:
            args = {}
        yield FunctionCallEvent(
            name=acc["name"],
            call_id=acc["id"] or None,
            args=args if isinstance(args, dict) else {},
        )


async def stream_openai(
    contents: list[genai_types.Content],
    tool_declarations: list[genai_types.FunctionDeclaration] | None = None,
    system_prompt: str | None = None,
    model: str | None = None,
    show_thinking: bool = False,
) -> AsyncIterator[StreamEvent]:
    """Stream one OpenAI-compatible turn, yielding the GRACE ``StreamEvent`` union.

    Mirrors ``bedrock_adapter.stream_bedrock``: one call == one model round.
    The dispatch loop in ``server.py`` appends function_call + function_response
    Contents and re-calls until no tool calls remain.

    The ``openai`` package is imported inside this function so the rest of the
    agent starts cleanly on environments where the package is not installed
    (the dep is dormant unless MODEL_PROVIDER=openai is selected).

    OPEN-14 (context-budget compaction + overflow guard, LOCAL path only):
    before the request is sent, ``contents`` is proactively compacted if the
    estimated prompt would exceed the model's discovered ``num_ctx`` budget
    (see ``context_budget.compact_contents``). After the round completes, the
    ACTUAL reported ``usage.prompt_tokens`` is checked against ``num_ctx``; a
    value ``>= num_ctx`` proves Ollama silently clipped the prompt (the
    tell-tale shape of the 2x-reproduced incident this closes -- the model
    loses its tool contract and narrates a fabricated success). One harder
    recompaction + retry is attempted; a second clip raises
    ``ContextWindowExceededError`` (caught by server.py and surfaced as an
    honest typed envelope instead of a fabricated or generic failure).

    Compaction UX (Part A): every ``compact_contents`` call is bracketed by a
    ``CompactionStartEvent`` immediately followed by a
    ``CompactionCompleteEvent(before_tokens=..., after_tokens=...)`` -- NOT a
    ``TextDeltaEvent`` glued onto the model's own reply (the pre-Part-A
    ``PROACTIVE_COMPACTION_NOTE`` / ``CLIP_RETRY_NOTE`` narration seam,
    removed). ``server.py``'s dispatch loop turns that pair into a durable
    pipeline card (``pipeline_emitter.mint_compaction_card`` /
    ``complete_compaction_card``) instead. The PROACTIVE call site (below)
    gates the pair on ``result.changed`` (mirroring the original note's own
    gate -- an under-budget turn that never compacts must show no card at
    all). The REACTIVE clip-guard call site does NOT gate it: a detected clip
    always means a retry is happening, so the card always reports its honest
    before/after count even on a no-op pass (mirrors the original
    ``CLIP_RETRY_NOTE``, which was likewise unconditional on that path).
    """
    try:
        from openai import AsyncOpenAI
    except ImportError as exc:
        raise RuntimeError(
            "MODEL_PROVIDER=openai requires the 'openai' package. "
            "Install it with: pip install 'openai>=1.40'"
        ) from exc

    resolved_model = openai_model(model)
    base_url = openai_base_url()
    api_key = openai_api_key()

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    num_ctx = await discover_num_ctx(base_url, resolved_model)
    budget = compute_budget_tokens(num_ctx)

    tools = tool_declarations_to_openai_tools(tool_declarations)
    tool_tokens = estimate_tokens_for_tools(tools)
    # Budget available to the CONTENT rows (the part the ladder can shrink) --
    # tool schemas and the system prompt are fixed overhead per turn.
    sys_tokens = estimate_tokens(system_prompt) if system_prompt else 0
    content_budget = max(budget - tool_tokens - sys_tokens, 256)

    working_contents = list(contents)
    messages = contents_to_openai_messages(
        working_contents, system_prompt=system_prompt, show_thinking=show_thinking
    )

    # PROACTIVE BUDGET CHECK: estimate over the ACTUAL wire messages + tool
    # schemas (the closest available proxy to what Ollama really receives).
    msg_tokens = estimate_tokens_for_messages(messages)
    est_tokens = msg_tokens + tool_tokens
    # One honest pre-send line per model round (2026-07-12 context-window
    # fix): the compaction line below only fires when a turn is OVER budget,
    # which left healthy turns unmeasurable in the log.
    logger.info(
        "context-budget: pre-send model=%s num_ctx=%d budget=%d est_total=%d "
        "msgs=%d tools=%d sys=%d",
        resolved_model,
        num_ctx,
        budget,
        est_tokens,
        msg_tokens,
        tool_tokens,
        sys_tokens,
    )
    if est_tokens > budget:
        result = compact_contents(
            working_contents, budget_tokens=content_budget, target_ratio=proactive_target_ratio()
        )
        if result.changed:
            logger.info(
                "context-budget: proactive compaction model=%s num_ctx=%d budget=%d "
                "before_est=%d after_est=%d dropped=%d hardened=%d folded=%s",
                resolved_model,
                num_ctx,
                budget,
                est_tokens,
                result.after_tokens + tool_tokens + sys_tokens,
                result.dropped,
                result.hardened,
                result.folded,
            )
            working_contents = result.contents
            messages = contents_to_openai_messages(
                working_contents, system_prompt=system_prompt, show_thinking=show_thinking
            )
            # Compaction UX (Part A): typed events, not a narration note --
            # see the docstring above and context_budget.COMPACTING_LABEL /
            # compaction_complete_label.
            yield CompactionStartEvent()
            yield CompactionCompleteEvent(
                before_tokens=result.before_tokens, after_tokens=result.after_tokens
            )

    attempt = 0
    while True:
        attempt += 1
        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0.7,
            # BUG 3 (post-OPEN-14 acceptance rerun): cap generation so a
            # clipped/looping round cannot stream 16k-26k tokens of runaway
            # narration for ~22 minutes before the reactive clip guard below
            # gets a chance to react (it only inspects usage AFTER the round
            # ends). Verified live against Ollama's OpenAI-compat endpoint
            # (2026-07-12, llama3.2:3b): max_tokens maps to num_predict and
            # truncates the completion at exactly this count under streaming.
            # ``context_budget.reserve_output_tokens`` reserves this SAME
            # value (single source of truth) -- see openai_max_output_tokens.
            "max_tokens": openai_max_output_tokens(),
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        round_usage: UsageMetadataEvent | None = None
        async for event in _stream_one_round(client, kwargs):
            if isinstance(event, UsageMetadataEvent):
                round_usage = event
            yield event

        prompt_tokens = round_usage.prompt_token_count if round_usage is not None else None
        if not is_prompt_clipped(prompt_tokens, num_ctx):
            return

        # REACTIVE CLIP GUARD: the send WAS clipped -- the round we just
        # streamed is unreliable (this is exactly how the incident's
        # fabricated-success narration happened: zero tool calls, confident
        # prose, prompt clipped to num_ctx). Recompact HARDER and retry once.
        logger.info(
            "context-budget: clip detected model=%s attempt=%d prompt_tokens=%s num_ctx=%d",
            resolved_model,
            attempt,
            prompt_tokens,
            num_ctx,
        )
        if attempt >= 2:
            raise ContextWindowExceededError(num_ctx)

        result = compact_contents(
            working_contents, budget_tokens=content_budget, target_ratio=reactive_target_ratio()
        )
        working_contents = result.contents
        messages = contents_to_openai_messages(
            working_contents, system_prompt=system_prompt, show_thinking=show_thinking
        )
        # Compaction UX (Part A): same typed-event pair as the proactive
        # path above -- UNCONDITIONAL (unlike the proactive site's
        # ``if result.changed:`` gate), matching the pre-Part-A
        # ``CLIP_RETRY_NOTE`` this replaces: a detected clip always means a
        # retry is happening, whether or not this pass finds more to shrink
        # (an already-near-minimal history still reports its honest
        # before==after count -- not a fabrication, just a no-op pass).
        yield CompactionStartEvent()
        yield CompactionCompleteEvent(
            before_tokens=result.before_tokens, after_tokens=result.after_tokens
        )


__all__ = [
    "stream_openai",
    "tool_declarations_to_openai_tools",
    "contents_to_openai_messages",
    "openai_model",
    "openai_base_url",
    "openai_api_key",
    "tool_desc_cap",
    "param_desc_cap",
]
