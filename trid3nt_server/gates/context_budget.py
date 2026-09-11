"""Context-budget: per-model window discovery and client-side history management.

The window is a PER-MODEL FACT DISCOVERED AT RUNTIME, and one that could not be
discovered narrates as an assumption. The trim STRATEGY lives here, once.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from dataclasses import dataclass
from typing import Any

from trid3nt_contracts.message import Message, Part, ToolResponse

from trid3nt_server.adapters.model_discovery import _ollama_root

logger = logging.getLogger("trid3nt_server.gates.context_budget")

# Config (env-overridable, read at call time)

#: Final fallback when neither /api/show discovery nor a ``-<N>k`` name
#: suffix resolves a model's context window (``TRID3NT_OPENAI_NUM_CTX``).
NUM_CTX_FALLBACK_DEFAULT = 16384

#: The ``max_tokens`` cap sent on every LOCAL ``chat.completions`` request. An
#: uncapped clipped-prompt turn can stream tens of thousands of tokens of looped
#: narration before the reactive clip guard reacts, since that guard only
#: inspects usage AFTER the stream finishes. Also the single source of truth for
#: ``reserve_output_tokens()`` below: the proactive budget must reserve exactly
#: what the request may generate, or the two silently drift apart.
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

#: Step (b)/(d) narration cap: any ``text`` Part longer than this is truncated
#: (ellipsis-marked) once the ladder is still over target after dropping and
#: tool-result hardening. It covers (b) a row mixing narration text with a
#: ``function_call``/``function_response`` Part, which is never droppable, and
#: (d) the PROTECTED tail as a last resort. Deliberately much smaller than
#: ``CONTENTS_NORMALIZE_CHAR_CAP_DEFAULT``: it fires only once the turn is
#: already proven over budget.
NARRATION_ROW_HARDEN_CHARS_DEFAULT = 2000

#: Defensive per-row TEXT cap applied unconditionally, to every row, at the very
#: top of ``compact_contents``. It guards against a single history row carrying
#: 100KB+ into a future turn's prompt even when the OVERALL total sits under
#: budget this turn. NOT a persistence-side change: persistence is untouched,
#: and this only shapes what ``compact_contents`` hands back for THIS turn.
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
    """``TRID3NT_OPENAI_NUM_CTX`` -- the final fallback when discovery and the
    name-suffix parse both come up empty."""
    return _env_int("TRID3NT_OPENAI_NUM_CTX", NUM_CTX_FALLBACK_DEFAULT, minimum=512)


def openai_max_output_tokens() -> int:
    """``TRID3NT_OPENAI_MAX_TOKENS`` -- the cap on every LOCAL request.

    It maps to ``num_predict``, and the completion truncates at exactly that
    count under both streaming and non-streaming requests."""
    return _env_int("TRID3NT_OPENAI_MAX_TOKENS", OPENAI_MAX_TOKENS_DEFAULT, minimum=1)


def reserve_output_tokens() -> int:
    """Tokens reserved for the model's reply, COUPLED to the request's own cap.

    A separately configured number would drift out of sync with the real cap."""
    return openai_max_output_tokens()


def safety_tokens() -> int:
    return _env_int("TRID3NT_CONTEXT_SAFETY_TOKENS", SAFETY_TOKENS_DEFAULT, minimum=0)


def proactive_target_ratio() -> float:
    return _env_float("TRID3NT_CONTEXT_PROACTIVE_RATIO", PROACTIVE_TARGET_RATIO_DEFAULT)


def reactive_target_ratio() -> float:
    return _env_float("TRID3NT_CONTEXT_REACTIVE_RATIO", REACTIVE_TARGET_RATIO_DEFAULT)


def tool_result_harden_chars() -> int:
    return _env_int(
        "TRID3NT_CONTEXT_TOOL_RESULT_HARDEN_CHARS",
        TOOL_RESULT_HARDEN_CHARS_DEFAULT,
        minimum=20,
    )


def narration_row_harden_chars() -> int:
    return _env_int(
        "TRID3NT_CONTEXT_NARRATION_HARDEN_CHARS",
        NARRATION_ROW_HARDEN_CHARS_DEFAULT,
        minimum=100,
    )


def contents_normalize_char_cap() -> int:
    return _env_int(
        "TRID3NT_CONTEXT_NORMALIZE_CHAR_CAP",
        CONTENTS_NORMALIZE_CHAR_CAP_DEFAULT,
        minimum=500,
    )


def compute_budget_tokens(num_ctx: int) -> int:
    """``budget = num_ctx - output reserve - safety margin``, floored so a
    tiny/misconfigured ``num_ctx`` never produces a negative or degenerate
    budget."""
    budget = num_ctx - reserve_output_tokens() - safety_tokens()
    return max(budget, 256)




def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _content_text_repr(content: Message) -> str:
    """Serialize one history row for token estimation."""
    try:
        dumped = content.model_dump(mode="json", exclude_none=True)
        return json.dumps(dumped, default=str)
    except Exception:  # noqa: BLE001 -- estimator must never raise
        return str(content)


def estimate_tokens_for_contents(contents: list[Message]) -> int:
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



_SUFFIX_RE = re.compile(r"-(\d+)k$", re.IGNORECASE)

# Matches a "num_ctx <int>" line inside Ollama /api/show's ``parameters``
# free-text field, e.g. "top_k    20\nnum_ctx    16384\ntemperature   1".
_PARAM_NUM_CTX_RE = re.compile(r"^\s*num_ctx\s+(\d+)\s*$", re.IGNORECASE | re.MULTILINE)


def num_ctx_from_suffix(model_name: str | None) -> int | None:
    """Parse a trailing ``-<N>k`` context-size suffix off a model name.

    The ``k`` suffix means ``N * 1024``, not ``N * 1000``."""
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
    """Extract the runtime ``num_ctx`` from an Ollama ``/api/show`` response body.

    ``None`` when the model has no baked override, and the caller falls through."""
    if not isinstance(payload, dict):
        return None
    # The RUNTIME override baked via ``PARAMETER num_ctx <n>`` shows up as a
    # line inside the top-level ``parameters`` free-text field. Its neighbour
    # ``model_info.<family>.context_length`` is a DIFFERENT, much larger number
    # -- the architecture's max TRAINED context, regardless of the runtime
    # window -- and must NOT be read here.
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
    """The OpenAI/local path's ``num_ctx`` as a plain int, losing its source.

    A thin wrapper over :func:`discover_context_window`, so the two cannot drift."""
    window = await discover_context_window("openai", model_name, base_url=base_url)
    return window.tokens


def reset_num_ctx_cache() -> None:
    """Clear the process-lifetime context-window discovery cache.

    A live provider/model switch must not reuse a window discovered from the
    old provider, so a same-name model re-discovers on the next turn."""
    _WINDOW_CACHE.clear()


def _reset_num_ctx_cache_for_tests() -> None:
    """Test-only alias of :func:`reset_num_ctx_cache`."""
    reset_num_ctx_cache()


# 1b. PROVIDER-AGNOSTIC CONTEXT-WINDOW DISCOVERY
#
# The context window is a PER-MODEL FACT DISCOVERED AT RUNTIME. It is never
# hardcoded, and a value we could not discover is never passed off as one we
# did -- ``ContextWindow.source`` records where every number came from, and an
# undiscovered window logs a WARNING and carries narration for the user.

#: Where a resolved window came from. Carried on every ``ContextWindow`` so a
#: log line (or a test) can tell a provider-stated fact from a fallback.
WINDOW_SOURCE_OLLAMA_SHOW = "ollama:/api/show"
WINDOW_SOURCE_OPENROUTER_MODELS = "openrouter:/models.context_length"
WINDOW_SOURCE_ANTHROPIC_MODELS = "anthropic:/v1/models.max_input_tokens"
WINDOW_SOURCE_NAME_SUFFIX = "model-name-suffix"
WINDOW_SOURCE_ENV = "env:TRID3NT_CONTEXT_WINDOW"
WINDOW_SOURCE_FALLBACK = "conservative-default"

#: The sources that represent a genuine provider-stated (or operator-stated)
#: fact. Anything outside this set is a fallback and narrates as one.
_DISCOVERED_SOURCES = frozenset(
    {
        WINDOW_SOURCE_OLLAMA_SHOW,
        WINDOW_SOURCE_OPENROUTER_MODELS,
        WINDOW_SOURCE_ANTHROPIC_MODELS,
        WINDOW_SOURCE_NAME_SUFFIX,
        WINDOW_SOURCE_ENV,
    }
)

#: Conservative window used when NOTHING could be discovered. Deliberately
#: small: under-stating a window costs an early compaction, over-stating it
#: costs a hard provider overflow. Operator override: ``TRID3NT_CONTEXT_WINDOW``.
CONTEXT_WINDOW_FALLBACK_DEFAULT = 16384


def context_window_env_override() -> int | None:
    """Operator-pinned window (``TRID3NT_CONTEXT_WINDOW``), or None when unset.

    Outranks the conservative default, but NEVER a fact the provider reported."""
    raw = os.environ.get("TRID3NT_CONTEXT_WINDOW", "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "context-window: TRID3NT_CONTEXT_WINDOW=%r is not an integer -- ignoring", raw
        )
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class ContextWindow:
    """A model's resolved input-token capacity PLUS where that number came from.

    Never construct it with a made-up number and a discovered-looking source."""

    tokens: int
    source: str
    provider: str
    model: str

    @property
    def discovered(self) -> bool:
        """True when ``tokens`` is a provider- or operator-stated fact."""
        return self.source in _DISCOVERED_SOURCES

    def narration(self) -> str | None:
        """User-facing honesty note, or None when the window is a real fact.

        An undiscoverable window is a stated assumption, never a silent guess."""
        if self.discovered:
            return None
        return (
            f"Could not determine the context window for {self.model!r} from "
            f"{self.provider}; assuming a conservative {self.tokens} tokens. "
            "Set TRID3NT_CONTEXT_WINDOW to pin the real value."
        )


#: Discovered windows, keyed ``(provider, model)`` for the process lifetime --
#: one discovery round-trip per model, ever. Cleared by ``reset_num_ctx_cache``
#: so a LIVE provider/model switch re-discovers instead of serving a stale
#: window from the previous provider.
_WINDOW_CACHE: dict[tuple[str, str], ContextWindow] = {}


async def _resolve_window_tokens(
    provider: str, model_name: str, base_url: str | None
) -> tuple[int, str] | None:
    """Per-provider discovery ladder -> ``(tokens, source)``, or None.

    Each branch asks the provider for its OWN metadata and gives up honestly."""
    from trid3nt_server.adapters import model_discovery

    # openai-compatible: OpenRouter's ``/models.context_length`` when the base
    # URL is OpenRouter, else Ollama's native ``/api/show`` runtime ``num_ctx``,
    # then a ``-<N>k`` name suffix.
    # anthropic: the Models API ``max_input_tokens`` field.
    # bedrock: the maintained table, because Bedrock publishes no runtime fact.
    if provider == "openai":
        if model_discovery.is_openrouter_base_url(base_url):
            tokens = await model_discovery.openrouter_context_length(
                (base_url or "").rstrip("/"), model_name
            )
            if tokens:
                return tokens, WINDOW_SOURCE_OPENROUTER_MODELS
        else:
            root = _ollama_root(base_url)
            if root:
                try:
                    import httpx

                    async with httpx.AsyncClient(timeout=3.0) as client:
                        resp = await client.post(
                            f"{root}/api/show", json={"model": model_name}
                        )
                    if resp.status_code == 200:
                        tokens = _parse_num_ctx_from_show_response(resp.json())
                        if tokens:
                            return tokens, WINDOW_SOURCE_OLLAMA_SHOW
                except Exception:  # noqa: BLE001 -- discovery is best-effort
                    logger.debug(
                        "context-window: /api/show discovery failed for %r",
                        model_name,
                        exc_info=True,
                    )
        suffix = num_ctx_from_suffix(model_name)
        if suffix:
            return suffix, WINDOW_SOURCE_NAME_SUFFIX
        # Provider-specific operator pin, honored before the generic one so
        # existing local-path deployments keep their configured window.
        if os.environ.get("TRID3NT_OPENAI_NUM_CTX", "").strip():
            return num_ctx_env_fallback(), WINDOW_SOURCE_ENV
        return None

    if provider == "anthropic":
        tokens = await model_discovery.anthropic_max_input_tokens(model_name)
        if tokens:
            return tokens, WINDOW_SOURCE_ANTHROPIC_MODELS
        return None

    return None


async def discover_context_window(
    provider: str, model_name: str, *, base_url: str | None = None
) -> ContextWindow:
    """Resolve ``model_name``'s context window for ``provider``, cached.

    Provider metadata, then the env pin, then a conservative default that says so."""
    key = (provider, model_name)
    cached = _WINDOW_CACHE.get(key)
    if cached is not None:
        return cached

    resolved: tuple[int, str] | None = None
    try:
        resolved = await _resolve_window_tokens(provider, model_name, base_url)
    except Exception:  # noqa: BLE001 -- discovery is best-effort
        logger.debug(
            "context-window: discovery raised for provider=%s model=%r",
            provider,
            model_name,
            exc_info=True,
        )

    if resolved is None:
        override = context_window_env_override()
        if override is not None:
            resolved = (override, WINDOW_SOURCE_ENV)
        else:
            resolved = (CONTEXT_WINDOW_FALLBACK_DEFAULT, WINDOW_SOURCE_FALLBACK)
            logger.warning(
                "context-window: UNDISCOVERABLE for provider=%s model=%r -- assuming a "
                "conservative %d tokens. Set TRID3NT_CONTEXT_WINDOW to pin the real "
                "value.",
                provider,
                model_name,
                resolved[0],
            )

    window = ContextWindow(
        tokens=resolved[0], source=resolved[1], provider=provider, model=model_name
    )
    logger.info(
        "context-window: provider=%s model=%s tokens=%d source=%s",
        provider,
        model_name,
        window.tokens,
        window.source,
    )
    _WINDOW_CACHE[key] = window
    return window




@dataclass
class CompactionResult:
    contents: list[Message]
    changed: bool
    dropped: int
    hardened: int
    folded: bool
    before_tokens: int
    after_tokens: int


def _protected_tail_len(contents: list[Message]) -> int:
    """The rows that must NEVER be dropped, hardened or folded.

    Always the LAST <= 2: the case-state note and the terminal user message."""
    # Protecting the tail STRUCTURALLY, rather than text-sniffing for the note's
    # exact wording, is correct even on a turn where no note was built: it then
    # protects one extra real history row, which is harmless.
    return min(2, len(contents))


def _is_droppable_row(content: Message) -> bool:
    """True for a plain narration row: text only, no call or response Part.

    Step (a) drops only these; a tool row is hardened by (b), never deleted."""
    # A tool call and its matching response live in SEPARATE rows, so dropping
    # one side without the other leaves an orphaned tool_call/tool_result
    # pairing once the rows are converted to the OpenAI wire format -- an
    # API-breaking shape.
    for part in content.parts:
        if part.call is not None or part.response is not None:
            return False
    return True


def _harden_function_response_part(
    part: Part, max_chars: int
) -> tuple[Part, bool]:
    fr = part.response
    if fr is None:
        return part, False
    serialized = json.dumps(fr.result, default=str) if fr.result is not None else ""
    if len(serialized) <= max_chars:
        return part, False
    hardened = ToolResponse(
        name=fr.name,
        id=fr.id,
        result={"summary": serialized[:max_chars], "truncated": True},
    )
    return Part(response=hardened), True


def _harden_content(
    content: Message, max_chars: int
) -> tuple[Message, bool]:
    """Re-summarize any long ``function_response`` Part down to ``max_chars``.

    Only tool RESULTS are hardened; text and call Parts are left untouched."""
    changed = False
    new_parts: list[Part] = []
    for part in content.parts:
        new_part, part_changed = _harden_function_response_part(part, max_chars)
        new_parts.append(new_part)
        changed = changed or part_changed
    if not changed:
        return content, False
    return Message(role=content.role, parts=new_parts), True


def _cap_text_parts(
    content: Message, max_chars: int
) -> tuple[Message, bool]:
    """Truncate any oversized ``text`` Part to ``max_chars``, ellipsis-marked.

    What shrinks a giant AGENT NARRATION row, alone or beside a call Part."""
    changed = False
    new_parts: list[Part] = []
    for part in content.parts:
        text = part.text
        if text is not None and len(text) > max_chars:
            new_parts.append(Part(text=text[:max_chars] + " ...[truncated]"))
            changed = True
        else:
            new_parts.append(part)
    if not changed:
        return content, False
    return Message(role=content.role, parts=new_parts), True


def normalize_contents_row_sizes(
    contents: list[Message], max_chars: int | None = None
) -> list[Message]:
    """Defensive per-row TEXT cap, run whether or not the turn is over budget.

    A row needing no change keeps its object identity, untouched verbatim."""
    if max_chars is None:
        max_chars = contents_normalize_char_cap()
    out: list[Message] = []
    for c in contents:
        capped, changed = _cap_text_parts(c, max_chars)
        out.append(capped if changed else c)
    return out


def _protected_row_labels(n: int) -> list[str]:
    """Human-readable names for the protected tail, in order, for the step (d) log.

    The last <= 2 rows are [case-state note, terminal user message]."""
    if n == 2:
        return ["case-state note (protected)", "terminal user message (protected)"]
    if n == 1:
        return ["terminal user message (protected)"]
    return [f"protected row {i}" for i in range(n)]


def _digest_line_for_content(content: Message) -> str | None:
    for part in content.parts:
        if part.call is not None and part.call.name:
            return f"called {part.call.name}"
        if part.response is not None and part.response.name:
            return f"{part.response.name} completed"
        if part.text:
            snippet = " ".join(part.text.strip().split())[:80]
            if snippet:
                return f"{'asked' if content.role == 'user' else 'answered'}: {snippet}"
    return None


def _build_digest_row(contents: list[Message]) -> Message:
    """One extractive line per surviving row, folded into a single row.

    The ``user`` role is what makes it read as durable context, not a live turn."""
    lines = [ln for ln in (_digest_line_for_content(c) for c in contents) if ln]
    body = "\n".join(f"- {ln}" for ln in lines) if lines else "(no further detail)"
    text = "Earlier in this case: " + body
    return Message(role="user", parts=[Part(text=text)])


def compact_contents(
    contents: list[Message],
    *,
    budget_tokens: int,
    target_ratio: float,
    harden_chars: int | None = None,
    narration_chars: int | None = None,
) -> CompactionResult:
    """Run the ladder until the estimate is at or under target, else no-op.

    The target is a FRACTION of budget, so a turn sitting at the edge does not
    re-trigger compaction on every round."""
    if harden_chars is None:
        harden_chars = tool_result_harden_chars()
    if narration_chars is None:
        narration_chars = narration_row_harden_chars()
    target = max(int(budget_tokens * target_ratio), 1)

    # Cap every row's text BEFORE any budget math, so a single runaway row
    # never survives this function even on a turn that ends up under budget.
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

    # (b, continued) cap any remaining oversized narration text directly. A row
    # carrying a function_call/function_response Part alongside its text is one
    # (a) must not drop and the response-only harden above cannot shrink,
    # because its bulk is in a ``text`` Part rather than in the response.
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

    # (d) The excess can live entirely in the PROTECTED tail: (a) drains
    # ``working`` (or it started empty) while the tail alone still exceeds
    # target, and both (b) and (c) then no-op on their ``if working`` guard. A
    # narration row is never STRUCTURALLY protected from a defensive text cap
    # the way it is from DROP and FOLD, so cap any oversized text Part left in
    # ``protected`` and log loudly naming which block was too big. This is the
    # ONLY circumstance under which a protected row is ever mutated.
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


#
# The adapter yields typed compaction start / complete events, and the dispatch
# loop mints and completes a durable pipeline card from them using the two
# labels below -- the running-tool-card treatment, animated live and persisted
# so it survives a Case reopen. No new envelope type: the card rides the
# EXISTING wire shape every atomic-tool card already uses.

#: The running card's label, from the instant compaction starts until it
#: completes (mint time only -- never shown again once renamed).
COMPACTING_LABEL = "Compacting conversation..."


def compaction_complete_label(before_tokens: int, after_tokens: int) -> str:
    """Terminal card label for a finished compaction pass.

    Counts round to the nearest thousand, a nonzero one floored at 1k."""

    def _k(n: int) -> str:
        if n <= 0:
            return "0k"
        return f"{max(1, round(n / 1000))}k"

    return f"Conversation compacted ({_k(before_tokens)} -> {_k(after_tokens)} tokens)"


#: Appended to the persisted partial-reply text when a turn aborts on
#: ``ContextWindowExceededError``. The streamed narration up to that point is
#: ALREADY persisted and the reader has no other signal it was cut short, so
#: the abort verdict must land right after it in the SAME chat row, not only in
#: the transient error envelope a dead socket may never deliver.
CONTEXT_WINDOW_ABORT_NOTE = (
    "\n\n[This reply exceeded the model's context window and was aborted - "
    "the statements above are unverified. Start a new case or switch to a "
    "larger-context model.]"
)


def build_context_window_abort_note(*, fabricated_claim: bool) -> str:
    """The text appended to a persisted partial reply on a context-window abort.

    ``fabricated_claim`` puts the caveat FIRST, so the reader sees "no tools
    were executed" ahead of the context-window explanation."""
    if fabricated_claim:
        return f"\n\n{FABRICATION_CAVEAT}{CONTEXT_WINDOW_ABORT_NOTE}"
    return CONTEXT_WINDOW_ABORT_NOTE




class ContextWindowExceededError(RuntimeError):
    """Raised when a round's reported usage proves the prompt was clipped.

    Surfaced as its own typed envelope, never the provider-unavailable bucket."""

    def __init__(self, num_ctx: int):
        self.num_ctx = num_ctx
        k = max(num_ctx // 1024, 1)
        super().__init__(
            "The conversation no longer fits this model's context window "
            f"({k}k). Start a new case or switch to a larger-context model."
        )


def is_prompt_clipped(prompt_tokens: int | None, num_ctx: int) -> bool:
    """True iff reported ``usage.prompt_tokens`` reached or exceeded ``num_ctx``.

    The tell-tale sign the provider silently truncated the prompt to fit."""
    if prompt_tokens is None or num_ctx <= 0:
        return False
    return prompt_tokens >= num_ctx



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
    """True when ``text`` claims a completed geospatial action; TEXT only.

    It matches a turn that legitimately dispatched tools just as happily, so the
    caller MUST additionally gate on the turn having fired ZERO tool calls."""
    if not text:
        return False
    return bool(_FABRICATION_RE.search(text))


# THE SHARED BUDGET SEAM (one strategy, every provider)
#
# TWO FAILURE MODES, one seam. Ollama silently CLIPS an over-long prompt rather
# than erroring, so the model never sees its own tool contract, can emit ZERO
# tool calls, and can narrate a fabricated success as if the work had happened.
# Hosted providers instead REJECT it with a 400. Either way the fix is the same
# and it is ours: history management is CLIENT-SIDE, trimming BEFORE the
# provider has to react. What to trim, when, and what is untouchable lives HERE,
# once -- adapters only translate the planned ``contents`` into their own wire
# shape and emit the compaction events. Provider-side compaction is an opt-in
# EXTRA layered on top, never a replacement for this.
#
# CACHE-PREFIX SAFETY: the plan only ever rewrites ``contents`` -- the
# conversation. Prompt-cache breakpoints on every provider sit on the TOOL
# catalog and the SYSTEM block, which render BEFORE messages (tools -> system
# -> messages), so trimming the conversation cannot move or invalidate them.
# Adapters therefore MUST run the plan BEFORE building request kwargs, so the
# cached prefix is rebuilt byte-identically each turn.


@dataclass
class TurnPlan:
    """The decision for one model round: what to send, and what it cost."""

    contents: list[Message]
    compacted: bool
    before_tokens: int
    after_tokens: int
    window: ContextWindow
    est_tokens: int


def plan_turn(
    contents: list[Message],
    *,
    window: ContextWindow,
    tool_tokens: int = 0,
    system_tokens: int = 0,
    wire_tokens: int | None = None,
    target_ratio: float | None = None,
    output_reserve: int | None = None,
    phase: str = "proactive",
) -> TurnPlan:
    """Decide whether this turn's history must be compacted, and do it.

    THE single client-side history-management entry point, for every provider;
    the system prompt and tool contracts are never candidates."""
    # ``phase`` is "proactive" (a pre-send estimate) or "reactive" (the provider
    # has already said we overflowed); reactive takes the tighter target ratio
    # so a retry actually gains headroom.
    if target_ratio is None:
        target_ratio = (
            reactive_target_ratio() if phase != "proactive" else proactive_target_ratio()
        )
    # The reply has to FIT IN THE SAME WINDOW as the prompt, so the budget must
    # reserve exactly what THIS request is allowed to generate. Defaults to the
    # OpenAI-path cap; the Anthropic and Bedrock adapters pass their own
    # ``max_tokens``, which are far larger -- reserving the wrong one would let
    # a long reply overflow a prompt we had declared safe.
    reserve = output_reserve if output_reserve is not None else reserve_output_tokens()
    budget = max(window.tokens - reserve - safety_tokens(), 256)
    # Budget available to the CONTENT rows: tool schemas and the system prompt
    # are fixed overhead the ladder cannot touch.
    content_budget = max(budget - tool_tokens - system_tokens, 256)

    # ``wire_tokens``, when given, is the COMPLETE prompt count (conversation +
    # tools + system) -- measured off the real wire payload, or returned by the
    # provider's own token counter. It is AUTHORITATIVE: nothing is added on top
    # of it. Absent one, fall back to the stated chars/4 heuristic per piece.
    if wire_tokens is not None:
        est_tokens = wire_tokens
        conversation_tokens = max(wire_tokens - tool_tokens - system_tokens, 0)
    else:
        conversation_tokens = estimate_tokens_for_contents(contents)
        est_tokens = conversation_tokens + tool_tokens + system_tokens

    if phase != "proactive":
        # THE REJECTED PROMPT IS ITSELF AN UPPER BOUND. A reactive pass runs
        # because the provider said the prompt did not fit -- which means our
        # window fact, our estimator, or both were wrong. Budgeting off the
        # (evidently wrong) window can leave the ladder believing there is
        # nothing to do, and we would resend a byte-identical prompt and burn
        # the one retry. Clamping to what we just sent forces real shrinkage:
        # compact_contents targets ``budget_tokens * target_ratio``, so this
        # guarantees the retry is strictly smaller than the rejected request.
        actual_content_tokens = estimate_tokens_for_contents(contents)
        content_budget = max(min(content_budget, actual_content_tokens), 256)

    logger.info(
        "context-budget: pre-send provider=%s model=%s window=%d source=%s budget=%d "
        "est_total=%d convo=%d tools=%d sys=%d phase=%s",
        window.provider,
        window.model,
        window.tokens,
        window.source,
        budget,
        est_tokens,
        conversation_tokens,
        tool_tokens,
        system_tokens,
        phase,
    )

    # Proactive: only pay for the ladder when the estimate says we must.
    # Reactive: the provider has ALREADY rejected this prompt, so run it
    # unconditionally -- our estimate was wrong by definition.
    if phase == "proactive" and est_tokens <= budget:
        return TurnPlan(
            contents=list(contents),
            compacted=False,
            before_tokens=est_tokens,
            after_tokens=est_tokens,
            window=window,
            est_tokens=est_tokens,
        )

    result = compact_contents(
        contents, budget_tokens=content_budget, target_ratio=target_ratio
    )
    if result.changed:
        logger.info(
            "context-budget: %s compaction provider=%s model=%s window=%d before=%d "
            "after=%d dropped=%d hardened=%d folded=%s",
            phase,
            window.provider,
            window.model,
            window.tokens,
            result.before_tokens,
            result.after_tokens,
            result.dropped,
            result.hardened,
            result.folded,
        )
    return TurnPlan(
        contents=result.contents,
        # A reactive pass reports itself as compacted even when the ladder found
        # nothing further to shrink: a retry IS happening, and the honest
        # before==after counts say so rather than hiding the pass.
        compacted=result.changed or phase != "proactive",
        before_tokens=result.before_tokens,
        after_tokens=result.after_tokens,
        window=window,
        est_tokens=est_tokens,
    )


#: Provider phrasings for "your prompt does not fit". Each is a 400-class
#: REJECTION (never a transient fault), so it must not be routed into the
#: transient-retry ladder -- the fix is to trim and resend, not to wait.
#: Anthropic: "prompt is too long: 210000 tokens > 200000 maximum".
#: Bedrock: ValidationException "Input is too long for requested model".
#: OpenAI-compatible: "context_length_exceeded" / "maximum context length".
_CONTEXT_OVERFLOW_RE = re.compile(
    r"context[_ ]length[_ ]exceeded"
    r"|prompt is too long"
    r"|input is too long"
    r"|too many (?:input )?tokens"
    r"|maximum context length"
    r"|exceeds the maximum",
    re.IGNORECASE,
)


def looks_like_context_overflow_error(exc: BaseException | None) -> bool:
    """True when a provider error says the PROMPT DID NOT FIT.

    Separates the one 400 worth retrying from every other 400, which is our own
    bug and must fail loudly; the caller logs the message VERBATIM either way."""
    if exc is None:
        return False
    return bool(_CONTEXT_OVERFLOW_RE.search(str(exc)))


__all__ = [
    "CompactionResult",
    "ContextWindow",
    "ContextWindowExceededError",
    "CONTEXT_WINDOW_FALLBACK_DEFAULT",
    "TurnPlan",
    "context_window_env_override",
    "discover_context_window",
    "looks_like_context_overflow_error",
    "plan_turn",
    "WINDOW_SOURCE_ANTHROPIC_MODELS",
    "WINDOW_SOURCE_ENV",
    "WINDOW_SOURCE_FALLBACK",
    "WINDOW_SOURCE_NAME_SUFFIX",
    "WINDOW_SOURCE_OLLAMA_SHOW",
    "WINDOW_SOURCE_OPENROUTER_MODELS",
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
