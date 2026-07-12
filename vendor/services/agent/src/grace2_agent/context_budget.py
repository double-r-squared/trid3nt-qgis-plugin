"""Context-budget compaction + overflow guard for the LOCAL model path
(OPEN-14).

PROVEN FAILURE THIS FIXES (2x reproduced, session 01KX8GCZKNBAFEJ9SY1C8VNVND,
trid3nt-local/logs/agent.log): a turn's serialized prompt hit EXACTLY
``num_ctx`` (the "gemini usage" log line read ``prompt=16384`` for a
``qwen3.5-lowvram:9b-16k`` model, whose configured window is 16384). Ollama
SILENTLY clips an over-long prompt to fit the window rather than erroring --
the model never saw its own tool contract, emitted ZERO tool calls, and
narrated a fabricated success ("I have computed the hillshade... and
published") as if the (non-existent) work had happened.

Four independent pieces, all LOCAL (``MODEL_PROVIDER=openai``) only -- the
Bedrock path is untouched:

  1. NUM_CTX DISCOVERY (``discover_num_ctx``) -- per-model, queries Ollama's
     native ``/api/show`` and parses the RUNTIME-configured window out of the
     ``parameters`` free-text field (NOT ``model_info.*.context_length``,
     which is the architecture's max TRAINED context -- a different, much
     larger number that would silently defeat this whole guard). Falls back
     to a ``-<N>k`` name suffix, then ``GRACE2_OPENAI_NUM_CTX`` (env, default
     16384). Cached per model name for the process lifetime.

  2. PROACTIVE BUDGET + COMPACTION LADDER (``compact_contents``) -- estimates
     tokens as ``ceil(chars / 4)`` and, when over budget
     (``num_ctx - output_reserve - safety_margin``), compacts with
     HYSTERESIS (targets a FRACTION of budget so a borderline turn does not
     re-trigger compaction every round): drop oldest rows -> harden long
     tool-result rows -> fold the remaining oldest rows into one digest row.
     The terminal user message and the case-state note immediately before it
     (always the last <= 2 rows of ``contents`` per the
     ``build_contents_from_history`` / server.py ``turn_history_for_contents``
     contract) are NEVER touched.

  3. REACTIVE CLIP GUARD (``is_prompt_clipped`` / ``ContextWindowExceededError``)
     -- ``openai_adapter.stream_openai`` checks the ACTUAL reported
     ``usage.prompt_tokens`` against ``num_ctx`` after every round; a value
     ``>= num_ctx`` proves the send was clipped. One harder recompaction +
     retry is attempted; a second clip raises the typed error, which
     server.py surfaces as an honest ``CONTEXT_WINDOW_EXCEEDED`` envelope
     (not the generic ``LLM_UNAVAILABLE`` bucket).

  4. FABRICATION BACKSTOP (``looks_like_fabricated_action_claim``) -- cheap,
     conservative regex over the closing narration of a turn that issued
     ZERO tool calls: only fires when a completed-action verb (computed,
     published, created, fetched, ...) pairs with a geospatial output noun
     (layer, map, hillshade, dataset, ...) in the same sentence. Ordinary Q&A
     answers and any turn that actually dispatched a tool never trigger it --
     the structural (zero-tool-call) gate is the caller's job
     (``server.py``), this module only judges the TEXT.

All four pieces are individually unit-testable without a live Ollama or
network access; ``discover_num_ctx`` is the only piece that makes a network
call, and it degrades gracefully (best-effort) through its fallback chain on
any fault.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Any

from google.genai import types as genai_types

logger = logging.getLogger("grace2_agent.context_budget")

# ---------------------------------------------------------------------------
# Config (env-overridable, read at call time -- mirrors runaway_guard.py)
# ---------------------------------------------------------------------------

#: Final fallback when neither /api/show discovery nor a ``-<N>k`` name
#: suffix resolves a model's context window (``GRACE2_OPENAI_NUM_CTX``).
NUM_CTX_FALLBACK_DEFAULT = 16384

#: Tokens reserved for the model's OWN reply. The adapter does not currently
#: request an explicit ``max_tokens`` cap (verified: no such kwarg is sent in
#: ``openai_adapter.stream_openai``), so this is a conservative headroom
#: estimate, not a value read back from the request.
RESERVE_OUTPUT_TOKENS_DEFAULT = 2048

#: Extra fixed headroom below the raw arithmetic budget (tokenizer estimate
#: error, chat-template overhead not visible to the char/4 estimator, etc).
SAFETY_TOKENS_DEFAULT = 1024

#: Proactive-ladder hysteresis target: compact down to this FRACTION of the
#: budget (not all the way to 100%) so a turn sitting right at the edge does
#: not re-trigger compaction on every subsequent turn.
PROACTIVE_TARGET_RATIO_DEFAULT = 0.75

#: Reactive (post-clip) hysteresis target: compact harder than the proactive
#: pass, since a clip already happened once this turn.
REACTIVE_TARGET_RATIO_DEFAULT = 0.60

#: A tool-result row longer than this (serialized JSON chars) is re-summarized
#: down to roughly this many chars by the hardening step.
TOOL_RESULT_HARDEN_CHARS_DEFAULT = 200

#: The token estimator: ``ceil(total_chars / CHARS_PER_TOKEN)``.
CHARS_PER_TOKEN = 4


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return val if val >= minimum else default


def _env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return val if minimum <= val <= maximum else default


def num_ctx_env_fallback() -> int:
    """``GRACE2_OPENAI_NUM_CTX`` -- the final fallback when discovery and the
    name-suffix parse both come up empty."""
    return _env_int("GRACE2_OPENAI_NUM_CTX", NUM_CTX_FALLBACK_DEFAULT, minimum=512)


def reserve_output_tokens() -> int:
    return _env_int(
        "GRACE2_CONTEXT_RESERVE_OUTPUT_TOKENS", RESERVE_OUTPUT_TOKENS_DEFAULT, minimum=0
    )


def safety_tokens() -> int:
    return _env_int("GRACE2_CONTEXT_SAFETY_TOKENS", SAFETY_TOKENS_DEFAULT, minimum=0)


def proactive_target_ratio() -> float:
    return _env_float("GRACE2_CONTEXT_PROACTIVE_RATIO", PROACTIVE_TARGET_RATIO_DEFAULT)


def reactive_target_ratio() -> float:
    return _env_float("GRACE2_CONTEXT_REACTIVE_RATIO", REACTIVE_TARGET_RATIO_DEFAULT)


def tool_result_harden_chars() -> int:
    return _env_int(
        "GRACE2_CONTEXT_TOOL_RESULT_HARDEN_CHARS",
        TOOL_RESULT_HARDEN_CHARS_DEFAULT,
        minimum=20,
    )


def compute_budget_tokens(num_ctx: int) -> int:
    """``budget = num_ctx - output reserve - safety margin``, floored so a
    tiny/misconfigured ``num_ctx`` never produces a negative or degenerate
    budget."""
    budget = num_ctx - reserve_output_tokens() - safety_tokens()
    return max(budget, 256)


# ---------------------------------------------------------------------------
# Token estimator (item 2): ceil(chars / 4) over the serialized payload.
# ---------------------------------------------------------------------------


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _content_text_repr(content: genai_types.Content) -> str:
    """Serialize one history row for token estimation."""
    try:
        dumped = content.model_dump(mode="json", exclude_none=True)
        return json.dumps(dumped, default=str)
    except Exception:  # noqa: BLE001 -- estimator must never raise
        return str(content)


def estimate_tokens_for_contents(contents: list[genai_types.Content]) -> int:
    return sum(estimate_tokens(_content_text_repr(c)) for c in contents)


def estimate_tokens_for_messages(messages: list[dict[str, Any]]) -> int:
    """Estimate over the ACTUAL OpenAI wire ``messages[]`` (post-conversion) --
    the closest available proxy to what Ollama really receives."""
    if not messages:
        return 0
    return estimate_tokens(json.dumps(messages, default=str))


def estimate_tokens_for_tools(tools: list[dict[str, Any]] | None) -> int:
    if not tools:
        return 0
    return estimate_tokens(json.dumps(tools, default=str))


# ---------------------------------------------------------------------------
# 1. NUM_CTX DISCOVERY
# ---------------------------------------------------------------------------

_NUM_CTX_CACHE: dict[str, int] = {}

_SUFFIX_RE = re.compile(r"-(\d+)k$", re.IGNORECASE)

# Matches a "num_ctx <int>" line inside Ollama /api/show's ``parameters``
# free-text field, e.g. "top_k    20\nnum_ctx    16384\ntemperature   1".
_PARAM_NUM_CTX_RE = re.compile(r"^\s*num_ctx\s+(\d+)\s*$", re.IGNORECASE | re.MULTILINE)


def _ollama_root(base_url: str | None) -> str:
    """Strip a trailing OpenAI-compat ``/v1`` mount to reach Ollama's native
    API root (``GRACE2_OPENAI_BASE_URL`` is typically
    ``http://host:11434/v1``; ``/api/show`` lives at the bare root)."""
    root = (base_url or "").rstrip("/")
    if root.lower().endswith("/v1"):
        root = root[: -len("/v1")]
    return root


def num_ctx_from_suffix(model_name: str | None) -> int | None:
    """Parse a trailing ``-<N>k`` context-size suffix off a model name
    (e.g. ``qwen3:8b-16k`` -> 16384, ``llama3.2:3b-32k`` -> 32768). Verified
    live against the box's Modelfile ``PARAMETER num_ctx`` values: the ``k``
    suffix means ``N * 1024``, not ``N * 1000``."""
    if not model_name:
        return None
    m = _SUFFIX_RE.search(model_name.strip())
    if not m:
        return None
    try:
        k = int(m.group(1))
    except ValueError:
        return None
    return k * 1024 if k > 0 else None


def _parse_num_ctx_from_show_response(payload: dict[str, Any]) -> int | None:
    """Extract the runtime-configured ``num_ctx`` from an Ollama
    ``POST /api/show`` response body.

    Verified live shape (2026-07-11, models on box -- qwen3.5-lowvram:9b-16k,
    qwen3:8b-16k, llama3.2:3b-32k, and their un-suffixed base variants): the
    RUNTIME override baked via ``PARAMETER num_ctx <n>`` in the Modelfile
    shows up as a line inside the top-level ``parameters`` free-text field,
    e.g.::

        {"parameters": "top_k    20\\ntop_p    0.95\\nnum_ctx    16384\\n..."}

    ``model_info.<family>.context_length`` is a DIFFERENT, much larger number
    (the architecture's max TRAINED context -- 262144 for qwen3.5, 40960 for
    qwen3 -- regardless of the runtime window) and must NOT be read here; a
    model with no baked ``-16k``-style override has NO ``num_ctx`` line in
    ``parameters`` at all, and this returns ``None`` so the caller falls
    through to the name-suffix / env fallback.
    """
    if not isinstance(payload, dict):
        return None
    params_text = payload.get("parameters")
    if not isinstance(params_text, str) or not params_text.strip():
        return None
    m = _PARAM_NUM_CTX_RE.search(params_text)
    if not m:
        return None
    try:
        val = int(m.group(1))
    except ValueError:
        return None
    return val if val > 0 else None


async def discover_num_ctx(base_url: str | None, model_name: str) -> int:
    """Resolve the effective ``num_ctx`` for ``model_name`` (cached for the
    process lifetime -- one ``/api/show`` round-trip per model name, ever).

    Precedence:
      1. Ollama-native ``POST {root}/api/show`` (see
         ``_parse_num_ctx_from_show_response``).
      2. A ``-<N>k`` suffix on ``model_name``.
      3. ``GRACE2_OPENAI_NUM_CTX`` env var (default 16384).

    Any network/parse fault at step 1 is swallowed -- discovery is
    best-effort, never a hard dependency of the model call.
    """
    cached = _NUM_CTX_CACHE.get(model_name)
    if cached is not None:
        return cached

    discovered: int | None = None
    root = _ollama_root(base_url)
    if root:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.post(f"{root}/api/show", json={"model": model_name})
            if resp.status_code == 200:
                discovered = _parse_num_ctx_from_show_response(resp.json())
        except Exception:  # noqa: BLE001 -- discovery is best-effort
            logger.debug(
                "context-budget: /api/show discovery failed for %r", model_name, exc_info=True
            )

    if discovered is None:
        discovered = num_ctx_from_suffix(model_name)
    if discovered is None:
        discovered = num_ctx_env_fallback()

    _NUM_CTX_CACHE[model_name] = discovered
    return discovered


def _reset_num_ctx_cache_for_tests() -> None:
    """Test-only: clear the process-lifetime discovery cache between cases."""
    _NUM_CTX_CACHE.clear()


# ---------------------------------------------------------------------------
# 2. PROACTIVE BUDGET + COMPACTION LADDER
# ---------------------------------------------------------------------------


@dataclass
class CompactionResult:
    contents: list[genai_types.Content]
    changed: bool
    dropped: int
    hardened: int
    folded: bool
    before_tokens: int
    after_tokens: int


def _protected_tail_len(contents: list[genai_types.Content]) -> int:
    """Rows that must NEVER be dropped/hardened/folded: the terminal user
    message, and (when present) the case-state note immediately before it.

    Both are always the LAST <= 2 rows of ``contents`` per the
    ``adapter.build_contents_from_history`` / server.py
    ``turn_history_for_contents`` contract (the case-state note, when built,
    is appended as the final row of ``turn_history_for_contents`` BEFORE
    ``build_contents_from_history`` appends the new ``user_text`` as the
    terminal row). Protecting the tail structurally -- rather than
    text-sniffing for the note's exact wording -- is correct even on a turn
    where no note was built (it then just protects one extra real history
    row, which is harmless)."""
    return min(2, len(contents))


def _is_droppable_row(content: genai_types.Content) -> bool:
    """True for a plain narration row (text only -- no ``function_call`` /
    ``function_response`` Part). Step (a) only drops THESE, oldest first --
    a tool call and its matching response live in separate ``Content`` rows
    (see ``adapter.build_function_call_content`` /
    ``build_function_response_content``), and dropping one side without the
    other would leave an orphaned tool_call/tool_result pairing once
    ``contents_to_openai_messages`` converts it to the OpenAI wire format
    (an API-breaking shape). Tool rows are exempt from DROP and handled by
    the HARDEN step (b) instead -- shrunk, never deleted outright, until the
    FOLD step (c) (which only ever sees whatever step (a) could not remove)."""
    for part in getattr(content, "parts", None) or []:
        if getattr(part, "function_call", None) is not None:
            return False
        if getattr(part, "function_response", None) is not None:
            return False
    return True


def _harden_function_response_part(
    part: genai_types.Part, max_chars: int
) -> tuple[genai_types.Part, bool]:
    fr = getattr(part, "function_response", None)
    if fr is None:
        return part, False
    resp = getattr(fr, "response", None)
    serialized = json.dumps(resp, default=str) if resp is not None else ""
    if len(serialized) <= max_chars:
        return part, False
    hardened_resp = {"summary": serialized[:max_chars], "truncated": True}
    new_fr = genai_types.FunctionResponse(
        name=getattr(fr, "name", None),
        id=getattr(fr, "id", None),
        response=hardened_resp,
    )
    return genai_types.Part(function_response=new_fr), True


def _harden_content(
    content: genai_types.Content, max_chars: int
) -> tuple[genai_types.Content, bool]:
    """Re-summarize any long ``function_response`` Part in ``content`` down to
    ``max_chars``. Text / function_call Parts are left untouched (only tool
    RESULTS get hardened, per spec -- the call itself is small)."""
    parts = list(getattr(content, "parts", None) or [])
    changed = False
    new_parts: list[genai_types.Part] = []
    for part in parts:
        new_part, part_changed = _harden_function_response_part(part, max_chars)
        new_parts.append(new_part)
        changed = changed or part_changed
    if not changed:
        return content, False
    return genai_types.Content(role=content.role, parts=new_parts), True


def _digest_line_for_content(content: genai_types.Content) -> str | None:
    role = getattr(content, "role", "user") or "user"
    for part in getattr(content, "parts", None) or []:
        fc = getattr(part, "function_call", None)
        fr = getattr(part, "function_response", None)
        text = getattr(part, "text", None)
        if fc is not None and getattr(fc, "name", None):
            return f"called {fc.name}"
        if fr is not None and getattr(fr, "name", None):
            return f"{fr.name} completed"
        if text:
            snippet = " ".join(text.strip().split())[:80]
            if snippet:
                return f"{'asked' if role == 'user' else 'answered'}: {snippet}"
    return None


def _build_digest_row(contents: list[genai_types.Content]) -> genai_types.Content:
    """Extractive digest row (no LLM call in v1): one line per surviving row
    (user asks / model answers / tool calls+outcomes), folded into a single
    ``user``-role Content so it reads as durable context, not a live turn."""
    lines = [ln for ln in (_digest_line_for_content(c) for c in contents) if ln]
    body = "\n".join(f"- {ln}" for ln in lines) if lines else "(no further detail)"
    text = "Earlier in this case: " + body
    return genai_types.Content(role="user", parts=[genai_types.Part(text=text)])


def compact_contents(
    contents: list[genai_types.Content],
    *,
    budget_tokens: int,
    target_ratio: float,
    harden_chars: int | None = None,
) -> CompactionResult:
    """Run the 3-step compaction ladder against ``contents`` until the
    estimated token count is at/under ``budget_tokens * target_ratio``
    (HYSTERESIS -- see module docstring). No-ops (returns ``changed=False``)
    when already under target.

    Ladder, each step checked before the next fires:
      (a) drop the OLDEST unprotected row, one at a time;
      (b) harden long tool-result rows, oldest first;
      (c) fold ALL remaining unprotected rows into one digest row.

    The terminal user message and (when present) the case-state note are
    NEVER touched by any step (``_protected_tail_len``).
    """
    if harden_chars is None:
        harden_chars = tool_result_harden_chars()
    target = max(int(budget_tokens * target_ratio), 1)

    protect_n = _protected_tail_len(contents)
    protected = list(contents[len(contents) - protect_n :]) if protect_n else []
    working = list(contents[: len(contents) - protect_n]) if protect_n else list(contents)

    before_tokens = estimate_tokens_for_contents(contents)

    def _current_tokens() -> int:
        return estimate_tokens_for_contents(working) + estimate_tokens_for_contents(protected)

    if _current_tokens() <= target:
        return CompactionResult(
            contents=working + protected,
            changed=False,
            dropped=0,
            hardened=0,
            folded=False,
            before_tokens=before_tokens,
            after_tokens=before_tokens,
        )

    # (a) drop the oldest DROPPABLE (plain narration) row, repeatedly, until
    # under target or no droppable row remains. Tool call/response rows are
    # never touched here -- see ``_is_droppable_row``.
    dropped = 0
    while _current_tokens() > target:
        idx = next((i for i, c in enumerate(working) if _is_droppable_row(c)), None)
        if idx is None:
            break
        working.pop(idx)
        dropped += 1

    # (b) harden long tool-result rows, oldest first.
    hardened = 0
    if working and _current_tokens() > target:
        for i in range(len(working)):
            if _current_tokens() <= target:
                break
            new_content, changed = _harden_content(working[i], harden_chars)
            if changed:
                working[i] = new_content
                hardened += 1

    # (c) fold everything remaining into one extractive digest row.
    folded = False
    if working and _current_tokens() > target:
        working = [_build_digest_row(working)]
        folded = True

    after_tokens = _current_tokens()
    return CompactionResult(
        contents=working + protected,
        changed=dropped > 0 or hardened > 0 or folded,
        dropped=dropped,
        hardened=hardened,
        folded=folded,
        before_tokens=before_tokens,
        after_tokens=after_tokens,
    )


# ---------------------------------------------------------------------------
# Narration seams (cheap: extra TextDeltaEvent, no new envelope types).
# ---------------------------------------------------------------------------

PROACTIVE_COMPACTION_NOTE = (
    "[Note: earlier turns in this conversation were summarized to fit this "
    "model's context window.]\n\n"
)

CLIP_RETRY_NOTE = (
    "\n\n[Note: that reply did not fit this model's context window and may "
    "be incomplete or inaccurate -- retrying with a shorter conversation...]\n\n"
)


# ---------------------------------------------------------------------------
# 3. REACTIVE CLIP GUARD
# ---------------------------------------------------------------------------


class ContextWindowExceededError(RuntimeError):
    """Raised (LOCAL/OpenAI path only) when a model round's ACTUAL reported
    usage proves the prompt was clipped by ``num_ctx`` even after one
    recompaction + retry.

    Caught by server.py's per-turn exception handler and surfaced as a
    dedicated ``CONTEXT_WINDOW_EXCEEDED`` typed error envelope (not the
    generic ``LLM_UNAVAILABLE`` bucket) -- honesty floor: tell the user
    exactly why the turn stopped and what to do about it.
    """

    def __init__(self, num_ctx: int):
        self.num_ctx = num_ctx
        k = max(num_ctx // 1024, 1)
        super().__init__(
            "The conversation no longer fits this model's context window "
            f"({k}k). Start a new case or switch to a larger-context model."
        )


def is_prompt_clipped(prompt_tokens: int | None, num_ctx: int) -> bool:
    """True iff the model's reported ``usage.prompt_tokens`` reached (or
    exceeded) ``num_ctx`` -- the tell-tale sign Ollama silently truncated the
    prompt to fit the window (the 2x-reproduced incident: the "gemini usage"
    log line read exactly ``prompt=16384`` for a 16384-``num_ctx`` model)."""
    if prompt_tokens is None or num_ctx <= 0:
        return False
    return prompt_tokens >= num_ctx


# ---------------------------------------------------------------------------
# 4. FABRICATION BACKSTOP
# ---------------------------------------------------------------------------

# Completed-action verbs (past tense only -- "I can compute a hillshade" /
# "fetching the DEM now" are capability/in-progress statements, not claims of
# a finished action, and must NOT trigger this).
_ACTION_VERBS = (
    "computed", "published", "created", "fetched", "generated", "produced",
    "built", "rendered", "completed", "ran", "retrieved", "downloaded",
    "uploaded", "exported", "updated",
)

# Geospatial/output nouns -- requiring one of these near the verb keeps the
# backstop from firing on ordinary non-geospatial sentences ("I published
# papers on this topic before").
_ACTION_OBJECTS = (
    "layer", "layers", "map", "raster", "hillshade", "dem", "dataset",
    "datasets", "result", "results", "output", "model", "scenario", "flood",
    "plume", "contour", "mesh", "analysis", "shapefile", "geojson", "tile",
    "tiles",
)

# Same-sentence proximity: verb and object within ~60 non-sentence-ending
# characters of each other, in EITHER order (".", "!", "?" break the window
# so a verb in one sentence never pairs with an object in the next).
_VERB_ALT = "|".join(_ACTION_VERBS)
_OBJ_ALT = "|".join(_ACTION_OBJECTS)
_GAP = r"(?:(?![.!?]).){0,60}?"
_FABRICATION_RE = re.compile(
    rf"\b(?:{_VERB_ALT})\b{_GAP}\b(?:{_OBJ_ALT})\b"
    rf"|\b(?:{_OBJ_ALT})\b{_GAP}\b(?:{_VERB_ALT})\b",
    re.IGNORECASE,
)

FABRICATION_CAVEAT = (
    "Note: no tools were executed this turn - the statements above were not "
    "verified by any action."
)


def looks_like_fabricated_action_claim(text: str | None) -> bool:
    """True when ``text`` claims a completed geospatial action (a completed-
    action verb paired with a layer/map/result-ish noun in the same
    sentence).

    Conservative by construction: callers MUST additionally gate this on the
    STRUCTURAL condition (zero tool calls fired the whole turn) -- this
    function only judges the TEXT, never the tool-call history, so it will
    happily match text from a turn that legitimately dispatched tools; it is
    the caller's job to only consult this function when that turn did not.
    """
    if not text:
        return False
    return bool(_FABRICATION_RE.search(text))


__all__ = [
    "CompactionResult",
    "ContextWindowExceededError",
    "FABRICATION_CAVEAT",
    "CLIP_RETRY_NOTE",
    "PROACTIVE_COMPACTION_NOTE",
    "compact_contents",
    "compute_budget_tokens",
    "discover_num_ctx",
    "estimate_tokens",
    "estimate_tokens_for_contents",
    "estimate_tokens_for_messages",
    "estimate_tokens_for_tools",
    "is_prompt_clipped",
    "looks_like_fabricated_action_claim",
    "num_ctx_env_fallback",
    "num_ctx_from_suffix",
    "proactive_target_ratio",
    "reactive_target_ratio",
    "reserve_output_tokens",
    "safety_tokens",
    "tool_result_harden_chars",
]
