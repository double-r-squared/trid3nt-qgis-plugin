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

STILL-OVER-AFTER-STEP-A BUG (2x reproduced, trid3nt-local/logs/agent.log,
session 01KX8GCZKNBAFEJ9SY1C8VNVND-adjacent runs): ``proactive compaction
model=qwen3:8b-16k num_ctx=16384 budget=11264 before_est=64485
after_est=17066 dropped=7 hardened=0 folded=False`` -- still ~6k tokens over
budget, yet steps (b) (harden) and (c) (fold) never fired, and the turn then
clipped (``prompt_tokens=16384``) and aborted. Root cause: steps (b) and (c)
were BOTH gated on ``if working and ...`` -- once step (a) exhausted every
row it is allowed to drop (either because that emptied ``working``
completely, or because the only rows left carry a ``function_call`` /
``function_response`` Part alongside a giant narration ``text`` Part --
``adapter.build_contents_from_history``'s ``parts_blob`` full-fidelity path
can legitimately reconstruct a model turn as ONE ``Content`` with both a
text preamble and a tool call, and ``_is_droppable_row`` correctly refuses
to drop that row), the excess tokens end up living entirely in rows step
(b)'s old function-response-only hardening never touches, or in the
PROTECTED tail (the case-state note / terminal user message,
``_protected_tail_len``). An empty ``working`` list is falsy in Python, so
both ``if working`` checks silently no-op instead of running -- the ladder
returned still-over-budget with no further mitigation and no signal
anything was wrong. Fixed by: (1) step (b) now also caps any oversized
``text`` Part directly (not just ``function_response`` Parts), so a mixed
narration+function_call row is no longer immune; (2) a new step (d) that,
only when the ladder is still over target with nothing left in ``working``,
applies that same text cap to the PROTECTED tail (the one case a narration
row's TEXT LENGTH is not "structurally protected" the way its
existence/position is -- see ``compact_contents``) and logs a WARNING
naming which protected block was oversized; (3) a defensive per-row
``normalize_contents_row_sizes`` pass, run unconditionally at the top of
``compact_contents``, caps every row's text to
``CONTENTS_NORMALIZE_CHAR_CAP_DEFAULT`` so a single runaway row (the
177KB-message shape from the live log) never survives this function even on
a turn that happens to sit under budget overall.

POST-OPEN-14 ACCEPTANCE RERUN FOLLOW-UPS (3 bugs, sessions
01KXAGEJAAPWDH0YSEGYQK5QVG / 01KXAJ1WKWDC0XS7VW4RY6CVF6):

  BUG 1 -- a ``ContextWindowExceededError`` abort could persist NEITHER the
    typed failure card NOR any acknowledgement in the chat transcript itself
    (server.py's except-block ordering; see server.py ``_stream_gemini_reply``
    / ``_dispatch_gemini_and_persist``). Fixed by persisting first, and by
    appending ``CONTEXT_WINDOW_ABORT_NOTE`` to the turn's already-persisted
    partial narration so the reader sees the abort verdict right after the
    unverified streamed text, not only in a transient error envelope a
    dead/detached socket may never deliver.

  BUG 2 -- the fabrication backstop (item 4 below) was only wired into the
    normal zero-tool-call terminal branch, so an abort mid-fabrication
    persisted an unqualified false claim. ``build_context_window_abort_note``
    folds the same regex check into the abort path.

  BUG 3 -- no ``max_tokens`` cap meant a clipped/looping generation could run
    for ~22 minutes before the reactive clip guard (item 3) got a chance to
    react (it only inspects usage AFTER the stream ends). Fixed by
    ``openai_max_output_tokens`` (env ``GRACE2_OPENAI_MAX_TOKENS``, default
    4096), sent as ``max_tokens`` on every request in
    ``openai_adapter.stream_openai`` and COUPLED to ``reserve_output_tokens``
    so the proactive budget can never drift from the real request cap.
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

#: The ``max_tokens`` cap sent on every LOCAL ``chat.completions`` request
#: (``openai_adapter.stream_openai``) -- BUG 3, post-OPEN-14 acceptance
#: rerun: an uncapped clipped-prompt turn streamed 16k-26k tokens of looped
#: "Computing hillshade..." narration for ~22 minutes before the reactive
#: clip guard could react at stream end (it only inspects usage AFTER the
#: stream finishes). Also the single source of truth for
#: ``reserve_output_tokens()`` below -- the proactive budget must reserve
#: exactly what the request is allowed to generate, never a different
#: number, or the two silently drift apart.
OPENAI_MAX_TOKENS_DEFAULT = 4096

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

#: Step (b)/(d) narration cap: any ``text`` Part longer than this is
#: truncated (ellipsis-marked) once the ladder is still over target after
#: dropping (step a) and tool-result hardening -- covers (b) a row that
#: mixes narration text with a ``function_call``/``function_response`` Part
#: (never droppable, per ``_is_droppable_row``, and never touched by the old
#: function-response-only harden) and (d) the PROTECTED tail as a last
#: resort. Deliberately much smaller than ``CONTENTS_NORMALIZE_CHAR_CAP_DEFAULT``
#: -- this only fires once the turn is already proven over budget.
NARRATION_ROW_HARDEN_CHARS_DEFAULT = 2000

#: Defensive per-row TEXT cap applied unconditionally, to every row, at the
#: very top of ``compact_contents`` (the ladder's "normalization pass") --
#: guards against a single history row carrying 100KB+ into a future turn's
#: prompt even when the OVERALL total happens to sit under budget this turn
#: (the live-log failure mode: 177KB single-row agent narration messages).
#: NOT a persistence-side change -- persistence is untouched; this only
#: shapes what ``compact_contents`` hands back for the CURRENT turn.
CONTENTS_NORMALIZE_CHAR_CAP_DEFAULT = 8000

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


def openai_max_output_tokens() -> int:
    """``GRACE2_OPENAI_MAX_TOKENS`` -- the ``max_tokens`` cap passed on every
    LOCAL ``chat.completions`` request (BUG 3). Verified live against Ollama's
    OpenAI-compat endpoint (2026-07-12, llama3.2:3b): ``max_tokens`` maps to
    ``num_predict`` and the completion truncates at exactly that count
    (``finish_reason="length"``, ``usage.completion_tokens == max_tokens``),
    under both streaming and non-streaming requests.
    """
    return _env_int("GRACE2_OPENAI_MAX_TOKENS", OPENAI_MAX_TOKENS_DEFAULT, minimum=1)


def reserve_output_tokens() -> int:
    """Tokens reserved for the model's own reply -- COUPLED to
    ``openai_max_output_tokens()`` (BUG 3 fix, single source of truth): the
    adapter now caps generation at that many tokens on every request, so the
    proactive budget must reserve exactly that many, never a separately
    configured number that could drift out of sync with the real cap."""
    return openai_max_output_tokens()


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


def narration_row_harden_chars() -> int:
    return _env_int(
        "GRACE2_CONTEXT_NARRATION_HARDEN_CHARS",
        NARRATION_ROW_HARDEN_CHARS_DEFAULT,
        minimum=100,
    )


def contents_normalize_char_cap() -> int:
    return _env_int(
        "GRACE2_CONTEXT_NORMALIZE_CHAR_CAP",
        CONTENTS_NORMALIZE_CHAR_CAP_DEFAULT,
        minimum=500,
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


def _cap_text_parts(
    content: genai_types.Content, max_chars: int
) -> tuple[genai_types.Content, bool]:
    """Truncate any oversized ``text`` Part in ``content`` to ``max_chars``
    (ellipsis-marked). ``function_call`` / ``function_response`` Parts are
    left untouched -- those have their own dedicated hardening
    (``_harden_function_response_part``). This is what actually shrinks a
    giant AGENT NARRATION row, whether it lives alone or alongside a
    ``function_call`` Part in the same row (the mixed-row shape the old
    function-response-only harden could never reach -- see module
    docstring, STILL-OVER-AFTER-STEP-A BUG)."""
    parts = list(getattr(content, "parts", None) or [])
    changed = False
    new_parts: list[genai_types.Part] = []
    for part in parts:
        text = getattr(part, "text", None)
        if text is not None and len(text) > max_chars:
            new_parts.append(genai_types.Part(text=text[:max_chars] + " ...[truncated]"))
            changed = True
        else:
            new_parts.append(part)
    if not changed:
        return content, False
    return genai_types.Content(role=content.role, parts=new_parts), True


def normalize_contents_row_sizes(
    contents: list[genai_types.Content], max_chars: int | None = None
) -> list[genai_types.Content]:
    """Defensive per-row TEXT cap (module docstring, SECONDARY fix) -- run
    unconditionally at the top of ``compact_contents`` regardless of whether
    the turn is over budget, so a single runaway narration row (the
    177KB-message failure mode) never survives this function even on a turn
    whose OVERALL total happens to sit under budget. Rows that need no
    change keep their original object identity (callers -- notably the
    existing test suite -- rely on protected-tail rows being untouched
    verbatim when nothing was actually oversized)."""
    if max_chars is None:
        max_chars = contents_normalize_char_cap()
    out: list[genai_types.Content] = []
    for c in contents:
        capped, changed = _cap_text_parts(c, max_chars)
        out.append(capped if changed else c)
    return out


def _protected_row_labels(n: int) -> list[str]:
    """Human-readable names for the protected tail, in order, for the step
    (d) WARNING log (``_protected_tail_len`` contract: last <=2 rows are
    [case-state note, terminal user message])."""
    if n == 2:
        return ["case-state note (protected)", "terminal user message (protected)"]
    if n == 1:
        return ["terminal user message (protected)"]
    return [f"protected row {i}" for i in range(n)]


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
    narration_chars: int | None = None,
) -> CompactionResult:
    """Run the compaction ladder against ``contents`` until the estimated
    token count is at/under ``budget_tokens * target_ratio`` (HYSTERESIS --
    see module docstring). No-ops (returns ``changed=False``) when already
    under target.

    Ladder, each step checked before the next fires:
      (a) drop the OLDEST unprotected DROPPABLE (plain-narration, no
          function_call/function_response Part) row, one at a time;
      (b) harden long tool-result rows, oldest first, THEN cap any
          remaining oversized narration ``text`` Part directly -- covers a
          row that mixes narration text with a function_call/function_response
          Part, which (a) correctly refuses to drop and the old
          function-response-only harden could never shrink (STILL-OVER-
          AFTER-STEP-A BUG, module docstring);
      (c) fold ALL remaining unprotected rows into one digest row;
      (d) ONLY when still over target with nothing left in ``working`` --
          i.e. the excess lives entirely in the PROTECTED tail -- cap any
          oversized narration text there too and log a WARNING naming which
          protected block was too big. This is the ONLY circumstance under
          which a protected row is ever mutated: its POSITION/EXISTENCE stays
          structurally protected (never dropped, never folded away), but a
          narration row's TEXT LENGTH is not exempt from this defensive cap.

    Guarantee: after this returns, either ``after_tokens <= target``, or the
    excess is structurally irreducible (protected content alone, even after
    step (d)'s cap, still exceeds target) -- in which case a WARNING is
    logged naming the shortfall rather than failing silently.
    """
    if harden_chars is None:
        harden_chars = tool_result_harden_chars()
    if narration_chars is None:
        narration_chars = narration_row_harden_chars()
    target = max(int(budget_tokens * target_ratio), 1)

    # Defensive normalization pass (SECONDARY fix, module docstring): cap
    # every row's text BEFORE any budget math, so a single runaway row never
    # survives this function even on a turn that ends up under budget
    # overall.
    contents = normalize_contents_row_sizes(contents)

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

    # (b, continued) cap any remaining oversized narration text directly --
    # the BUG FIX: a row step (a) could not drop (it carries a
    # function_call/function_response Part alongside its text) and step
    # (b)'s function-response-only harden could not shrink (its bulk is in a
    # ``text`` Part, not the response) previously rode through untouched.
    if working and _current_tokens() > target:
        for i in range(len(working)):
            if _current_tokens() <= target:
                break
            new_content, changed = _cap_text_parts(working[i], narration_chars)
            if changed:
                working[i] = new_content
                hardened += 1

    # (c) fold everything remaining into one extractive digest row.
    folded = False
    if working and _current_tokens() > target:
        working = [_build_digest_row(working)]
        folded = True

    # (d) BUG FIX: previously, whenever (a) fully drained ``working`` (or it
    # started empty) while the PROTECTED tail alone still exceeded target,
    # both (b) and (c) silently no-op'd on ``if working and ...`` (an empty
    # list is falsy) -- the 2x-reproduced live failure (module docstring,
    # STILL-OVER-AFTER-STEP-A BUG). A narration row is never *structurally*
    # protected from a defensive text cap the way it is from DROP/FOLD, so:
    # cap any oversized text Part remaining in ``protected`` and log loudly
    # naming which block was too big.
    if _current_tokens() > target:
        labels = _protected_row_labels(len(protected))
        any_capped = False
        for i in range(len(protected)):
            row_tokens_before = estimate_tokens(_content_text_repr(protected[i]))
            new_content, changed = _cap_text_parts(protected[i], narration_chars)
            if changed:
                any_capped = True
                label = labels[i] if i < len(labels) else f"protected row {i}"
                logger.warning(
                    "context-budget: protected content alone exceeds target "
                    "(target=%d tokens) -- %s carried ~%d tokens; truncating "
                    "its narration text to %d chars (the ONLY case "
                    "compact_contents ever mutates a protected row)",
                    target, label, row_tokens_before, narration_chars,
                )
                protected[i] = new_content
                hardened += 1
            if _current_tokens() <= target:
                break
        if _current_tokens() > target:
            logger.warning(
                "context-budget: compaction still %d tokens over target "
                "(target=%d) after the full ladder%s -- remaining content is "
                "structurally irreducible (protected function_call/"
                "function_response Parts, or the cap still leaves it over); "
                "the turn will proceed over budget",
                _current_tokens() - target, target,
                " (protected-row text was truncated)" if any_capped else "",
            )

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
# Compaction UX (Part A) -- durable pipeline-card labels.
# ---------------------------------------------------------------------------
#
# SUPERSEDES the OPEN-14 narration seam (a bare ``TextDeltaEvent`` note glued
# onto the model's own reply -- ``PROACTIVE_COMPACTION_NOTE`` /
# ``CLIP_RETRY_NOTE``, removed here). ``openai_adapter.stream_openai`` now
# yields typed ``adapter.CompactionStartEvent`` / ``CompactionCompleteEvent``
# instead; ``server.py``'s dispatch loop mints/completes a durable pipeline
# card through ``pipeline_emitter.mint_compaction_card`` /
# ``complete_compaction_card`` (the F10 running-tool-card treatment, animated
# live, persisted so it survives a Case reopen) using the two labels below.
# No new envelope type: the card rides the EXISTING ``PipelineStep`` /
# ``ToolCardRecord`` wire shape every atomic-tool card already uses.

#: The running card's label, from the instant compaction starts until it
#: completes (mint time only -- never shown again once renamed).
COMPACTING_LABEL = "Compacting conversation..."


def compaction_complete_label(before_tokens: int, after_tokens: int) -> str:
    """Terminal card label for a finished compaction pass.

    ``before_tokens`` / ``after_tokens`` are
    ``CompactionResult.before_tokens`` / ``.after_tokens`` -- rounded to the
    nearest thousand (a nonzero count floors at 1k so a small budget never
    misleadingly reads "0k"). Example: ``compaction_complete_label(12800,
    3900)`` -> ``"Conversation compacted (13k -> 4k tokens)"``.
    """

    def _k(n: int) -> str:
        if n <= 0:
            return "0k"
        return f"{max(1, round(n / 1000))}k"

    return f"Conversation compacted ({_k(before_tokens)} -> {_k(after_tokens)} tokens)"


#: Appended to the persisted partial-reply text (post-OPEN-14 acceptance
#: rerun, BUG 1) when a turn aborts on ``ContextWindowExceededError``: the
#: streamed narration up to that point is ALREADY persisted (the reader has
#: no other signal it was cut short), so the abort verdict must land right
#: after it in the SAME chat row, not only in the transient error envelope
#: (which a dead/detached socket may never deliver).
CONTEXT_WINDOW_ABORT_NOTE = (
    "\n\n[This reply exceeded the model's context window and was aborted - "
    "the statements above are unverified. Start a new case or switch to a "
    "larger-context model.]"
)


def build_context_window_abort_note(*, fabricated_claim: bool) -> str:
    """The text appended to a turn's persisted partial reply on a
    ``ContextWindowExceededError`` abort (BUG 1 + BUG 2).

    When ``fabricated_claim`` is True -- the aborting turn dispatched ZERO
    tool calls AND its partial narration matches
    ``looks_like_fabricated_action_claim`` (BUG 2: the same fabrication
    backstop wired into the normal zero-tool-call terminal branch was being
    skipped entirely on the abort path) -- the appended text LEADS with
    ``FABRICATION_CAVEAT`` so the reader sees "no tools were executed" before
    the context-window explanation, not after.
    """
    if fabricated_claim:
        return f"\n\n{FABRICATION_CAVEAT}{CONTEXT_WINDOW_ABORT_NOTE}"
    return CONTEXT_WINDOW_ABORT_NOTE


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
    "CONTEXT_WINDOW_ABORT_NOTE",
    "COMPACTING_LABEL",
    "FABRICATION_CAVEAT",
    "build_context_window_abort_note",
    "compact_contents",
    "compaction_complete_label",
    "compute_budget_tokens",
    "contents_normalize_char_cap",
    "discover_num_ctx",
    "estimate_tokens",
    "estimate_tokens_for_contents",
    "estimate_tokens_for_messages",
    "estimate_tokens_for_tools",
    "is_prompt_clipped",
    "looks_like_fabricated_action_claim",
    "narration_row_harden_chars",
    "normalize_contents_row_sizes",
    "num_ctx_env_fallback",
    "num_ctx_from_suffix",
    "openai_max_output_tokens",
    "proactive_target_ratio",
    "reactive_target_ratio",
    "reserve_output_tokens",
    "safety_tokens",
    "tool_result_harden_chars",
]
