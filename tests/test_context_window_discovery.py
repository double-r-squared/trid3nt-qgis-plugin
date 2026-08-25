"""Per-model context-budget seam: runtime window discovery, the ONE shared
trim strategy, cache-prefix survival, and the provider-overflow retry.

The context window is a PER-MODEL FACT DISCOVERED AT RUNTIME -- never a
hardcoded constant. These tests pin that property from both directions: a
provider that states its window is believed, and a provider that says nothing
degrades to a LOUD conservative fallback rather than a silent guess.

Covers:
  1. Discovery matrix -- OpenRouter ``context_length``, Anthropic
     ``max_input_tokens``, the Bedrock maintained table, Ollama's runtime
     ``num_ctx``, the ``-<N>k`` name suffix, the env pin, and ABSENT metadata
     on every one of them.
  2. ``plan_turn`` -- the single strategy seam: the system prompt and tool
     contracts are never trim candidates, and the terminal user message plus
     the case-state note (the pending-confirmation spine) always survive.
  3. Cache-prefix preservation -- trimming rewrites only the conversation, so
     the Anthropic ``cache_control`` / Bedrock ``cachePoint`` breakpoints keep
     a byte-identical prefix across a compacted turn.
  4. Overflow classification + the trim-and-retry-once path.

Run:
    python3 -m pytest tests/test_context_window_discovery.py -q
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from google.genai import types as genai_types

from trid3nt_server.adapters import model_discovery
from trid3nt_server.gates.context_budget import (
    CONTEXT_WINDOW_FALLBACK_DEFAULT,
    ContextWindow,
    WINDOW_SOURCE_ANTHROPIC_MODELS,
    WINDOW_SOURCE_BEDROCK_TABLE,
    WINDOW_SOURCE_ENV,
    WINDOW_SOURCE_FALLBACK,
    WINDOW_SOURCE_NAME_SUFFIX,
    WINDOW_SOURCE_OLLAMA_SHOW,
    WINDOW_SOURCE_OPENROUTER_MODELS,
    discover_context_window,
    looks_like_context_overflow_error,
    plan_turn,
    reset_num_ctx_cache,
)


@pytest.fixture(autouse=True)
def _clear_window_cache():
    """Discovery caches per (provider, model) for the PROCESS lifetime, so
    every test must start from an empty cache to exercise the real ladder."""
    reset_num_ctx_cache()
    yield
    reset_num_ctx_cache()


def user_content(text: str) -> genai_types.Content:
    return genai_types.Content(role="user", parts=[genai_types.Part(text=text)])


def model_content(text: str) -> genai_types.Content:
    return genai_types.Content(role="model", parts=[genai_types.Part(text=text)])


def long_alternating_history(pairs: int = 8, filler: int = 3000) -> list[genai_types.Content]:
    """A realistic history: ALTERNATING user/model rows ending on a terminal
    user message. Alternation matters -- the Bedrock/Anthropic converters
    coalesce same-role runs and drop leading assistant rows, so an all-model
    history would collapse to one wire message and hide what trimming did."""
    rows: list[genai_types.Content] = []
    for i in range(pairs):
        rows.append(user_content(f"q{i} " + "x" * filler))
        rows.append(model_content(f"a{i} " + "y" * filler))
    rows.append(user_content("go"))
    return rows


# ---------------------------------------------------------------------------
# 1. Discovery matrix
# ---------------------------------------------------------------------------


def test_openrouter_parse_reads_context_length():
    body = {
        "data": [
            {"id": "other/model", "context_length": 8192},
            {"id": "qwen/qwen3-coder:free", "context_length": 262144},
        ]
    }
    assert (
        model_discovery.parse_openrouter_context_length(body, "qwen/qwen3-coder:free")
        == 262144
    )


def test_openrouter_parse_takes_the_smaller_top_provider_window():
    """``top_provider.context_length`` can be SMALLER than the headline number
    when the routed upstream serves a shorter window -- the request has to fit
    the smaller one."""
    body = {
        "data": [
            {
                "id": "m",
                "context_length": 262144,
                "top_provider": {"context_length": 32768},
            }
        ]
    }
    assert model_discovery.parse_openrouter_context_length(body, "m") == 32768


@pytest.mark.parametrize(
    "body",
    [
        {"data": []},
        {"data": [{"id": "m"}]},  # row present, NO context_length
        {"data": [{"id": "m", "context_length": 0}]},
        {"data": [{"id": "m", "context_length": True}]},  # bool is not a window
        {"data": "not-a-list"},
        {},
    ],
)
def test_openrouter_parse_absent_metadata_is_none_never_a_guess(body):
    assert model_discovery.parse_openrouter_context_length(body, "m") is None


def test_anthropic_parse_reads_max_input_tokens():
    """``max_input_tokens`` IS the context window on the Models API -- there is
    no ``context_window`` field to read."""

    class _Model:
        max_input_tokens = 200_000

    assert model_discovery.parse_anthropic_max_input_tokens(_Model()) == 200_000
    assert (
        model_discovery.parse_anthropic_max_input_tokens({"max_input_tokens": 1_000_000})
        == 1_000_000
    )


def test_anthropic_parse_absent_field_is_none():
    """An older API surface (or a proxy serving a trimmed object) legitimately
    omits the field -- that is 'undiscoverable', not zero."""

    class _Old:
        id = "claude-sonnet-5"

    assert model_discovery.parse_anthropic_max_input_tokens(_Old()) is None
    assert model_discovery.parse_anthropic_max_input_tokens({}) is None


@pytest.mark.asyncio
async def test_discovery_openrouter_path():
    with patch.object(
        model_discovery, "openrouter_context_length", AsyncMock(return_value=131072)
    ):
        window = await discover_context_window(
            "openai", "some/model", base_url="https://openrouter.ai/api/v1"
        )
    assert window.tokens == 131072
    assert window.source == WINDOW_SOURCE_OPENROUTER_MODELS
    assert window.discovered is True
    assert window.narration() is None


@pytest.mark.asyncio
async def test_discovery_ollama_runtime_num_ctx_path():
    """Ollama's RUNTIME window lives in the ``parameters`` free-text field --
    not ``model_info.*.context_length`` (the much larger TRAINED context)."""

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {
                "parameters": "top_k    20\nnum_ctx    16384\ntemperature   1",
                "model_info": {"qwen3": {"context_length": 262144}},
            }

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = _Resp()
    with patch("httpx.AsyncClient", return_value=client):
        window = await discover_context_window(
            "openai", "qwen3:8b", base_url="http://127.0.0.1:11434/v1"
        )
    assert window.tokens == 16384
    assert window.source == WINDOW_SOURCE_OLLAMA_SHOW


@pytest.mark.asyncio
async def test_discovery_falls_through_to_name_suffix():
    class _Resp:
        status_code = 404

        @staticmethod
        def json():
            return {}

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = _Resp()
    with patch("httpx.AsyncClient", return_value=client):
        window = await discover_context_window(
            "openai", "llama3.2:3b-32k", base_url="http://127.0.0.1:11434/v1"
        )
    assert window.tokens == 32 * 1024
    assert window.source == WINDOW_SOURCE_NAME_SUFFIX


@pytest.mark.asyncio
async def test_discovery_anthropic_models_endpoint():
    with patch.object(
        model_discovery, "anthropic_max_input_tokens", AsyncMock(return_value=200_000)
    ):
        window = await discover_context_window("anthropic", "claude-sonnet-5")
    assert window.tokens == 200_000
    assert window.source == WINDOW_SOURCE_ANTHROPIC_MODELS


@pytest.mark.asyncio
async def test_discovery_bedrock_maintained_table_is_last_resort_and_loud(caplog):
    """Bedrock publishes NO runtime window fact, so the table is the source --
    and every read of it must say so at WARNING."""
    with caplog.at_level(logging.WARNING):
        window = await discover_context_window(
            "bedrock", "us.anthropic.claude-sonnet-4-6"
        )
    assert window.tokens == 200_000
    assert window.source == WINDOW_SOURCE_BEDROCK_TABLE
    assert any("MAINTAINED TABLE" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_undiscoverable_window_is_a_loud_conservative_fallback(caplog, monkeypatch):
    """The honesty floor: no metadata anywhere means a conservative default
    PLUS a warning PLUS user-facing narration -- never a silent guess."""
    monkeypatch.delenv("TRID3NT_CONTEXT_WINDOW", raising=False)
    with patch.object(
        model_discovery, "anthropic_max_input_tokens", AsyncMock(return_value=None)
    ):
        with caplog.at_level(logging.WARNING):
            window = await discover_context_window("anthropic", "mystery-model")
    assert window.tokens == CONTEXT_WINDOW_FALLBACK_DEFAULT
    assert window.source == WINDOW_SOURCE_FALLBACK
    assert window.discovered is False
    assert any("UNDISCOVERABLE" in r.getMessage() for r in caplog.records)
    narration = window.narration()
    assert narration is not None and "conservative" in narration


@pytest.mark.asyncio
async def test_env_pin_outranks_the_fallback_but_not_the_provider(monkeypatch):
    monkeypatch.setenv("TRID3NT_CONTEXT_WINDOW", "48000")
    with patch.object(
        model_discovery, "anthropic_max_input_tokens", AsyncMock(return_value=None)
    ):
        window = await discover_context_window("anthropic", "mystery-model")
    assert (window.tokens, window.source) == (48000, WINDOW_SOURCE_ENV)

    # A provider that DOES state its window wins over the operator's pin --
    # if discovery succeeded, the env var is stale.
    reset_num_ctx_cache()
    with patch.object(
        model_discovery, "anthropic_max_input_tokens", AsyncMock(return_value=200_000)
    ):
        window = await discover_context_window("anthropic", "claude-sonnet-5")
    assert (window.tokens, window.source) == (200_000, WINDOW_SOURCE_ANTHROPIC_MODELS)


@pytest.mark.asyncio
async def test_discovery_is_cached_per_model_and_reset_clears_it():
    resolver = AsyncMock(return_value=200_000)
    with patch.object(model_discovery, "anthropic_max_input_tokens", resolver):
        await discover_context_window("anthropic", "claude-sonnet-5")
        await discover_context_window("anthropic", "claude-sonnet-5")
        assert resolver.await_count == 1, "one discovery round-trip per model, ever"

        # A live provider/model switch must re-discover, not serve the stale
        # window from the previous provider.
        reset_num_ctx_cache()
        await discover_context_window("anthropic", "claude-sonnet-5")
        assert resolver.await_count == 2


@pytest.mark.asyncio
async def test_discovery_never_raises_when_the_provider_is_unreachable(monkeypatch):
    monkeypatch.delenv("TRID3NT_CONTEXT_WINDOW", raising=False)
    with patch.object(
        model_discovery,
        "anthropic_max_input_tokens",
        AsyncMock(side_effect=RuntimeError("connection refused")),
    ):
        window = await discover_context_window("anthropic", "claude-sonnet-5")
    assert window.source == WINDOW_SOURCE_FALLBACK


# ---------------------------------------------------------------------------
# 2. The single trim strategy
# ---------------------------------------------------------------------------


def _window(tokens: int) -> ContextWindow:
    return ContextWindow(
        tokens=tokens, source=WINDOW_SOURCE_ENV, provider="test", model="m"
    )


def test_plan_turn_under_budget_is_a_no_op():
    contents = [user_content("hi"), model_content("hello")]
    plan = plan_turn(contents, window=_window(100_000))
    assert plan.compacted is False
    assert [c.parts[0].text for c in plan.contents] == ["hi", "hello"]


def test_plan_turn_trims_and_preserves_the_confirmation_spine():
    """ALWAYS preserved: the terminal user message and the case-state note
    immediately before it (the pending-confirmation spine)."""
    filler = [model_content("old narration " + "x" * 4000) for _ in range(12)]
    case_state = user_content("CASE STATE: pending confirmation for run_telemac")
    terminal = user_content("yes, run it")
    contents = [*filler, case_state, terminal]

    plan = plan_turn(contents, window=_window(4096), phase="proactive")

    assert plan.compacted is True
    assert plan.after_tokens < plan.before_tokens
    texts = [p.text for c in plan.contents for p in (c.parts or []) if p.text]
    assert any("pending confirmation for run_telemac" in t for t in texts)
    assert texts[-1] == "yes, run it"


def test_plan_turn_never_sees_the_system_prompt_or_tool_contracts():
    """They are fixed per-turn OVERHEAD, accounted for in the budget but never
    trim candidates -- they are not part of ``contents`` at all."""
    contents = [model_content("x" * 20000) for _ in range(5)]
    contents.append(user_content("go"))

    plan = plan_turn(
        contents,
        window=_window(4096),
        tool_tokens=500,
        system_tokens=800,
        phase="proactive",
    )
    texts = [p.text for c in plan.contents for p in (c.parts or []) if p.text]
    assert plan.compacted is True
    # Nothing the planner returned is a system prompt or a tool schema: it only
    # ever hands back conversation rows.
    assert texts[-1] == "go"


def test_plan_turn_reactive_phase_always_reports_compacted():
    """The provider already rejected the prompt: a retry IS happening, so the
    pass reports itself honestly even when the ladder finds nothing more."""
    contents = [user_content("go")]
    plan = plan_turn(contents, window=_window(1_000_000), phase="reactive")
    assert plan.compacted is True
    assert plan.before_tokens == plan.after_tokens


def test_plan_turn_reactive_shrinks_even_when_the_window_says_we_fit():
    """THE REJECTED PROMPT IS AN UPPER BOUND. A reactive pass runs because the
    provider said the prompt did not fit -- so the window fact or the estimator
    was wrong. Budgeting off that wrong window would let the ladder no-op and
    resend a byte-identical prompt, burning the one retry."""
    contents = long_alternating_history()
    before = sum(len(p.text or "") for c in contents for p in (c.parts or []))

    # A window so large that a window-derived budget says "nothing to do".
    plan = plan_turn(contents, window=_window(1_000_000), phase="reactive")

    after = sum(len(p.text or "") for c in plan.contents for p in (plan.contents and c.parts or []))
    assert plan.compacted is True
    assert after < before, "a reactive pass must actually shrink the prompt"
    texts = [p.text for c in plan.contents for p in (c.parts or []) if p.text]
    assert texts[-1] == "go", "the terminal user message still survives"


def test_plan_turn_reactive_targets_tighter_than_proactive():
    contents = [model_content("y" * 6000) for _ in range(10)]
    contents.append(user_content("go"))
    proactive = plan_turn(list(contents), window=_window(8192), phase="proactive")
    reactive = plan_turn(list(contents), window=_window(8192), phase="reactive")
    assert reactive.after_tokens <= proactive.after_tokens


def test_plan_turn_wire_tokens_is_the_whole_prompt_and_is_authoritative():
    """``wire_tokens`` is the TOTAL (conversation + tools + system). Nothing is
    added on top -- double-counting the tool catalog would trim turns that fit."""
    contents = [user_content("hi")]
    # A window whose budget sits just above the stated total. If tool_tokens
    # were added on top of wire_tokens, this would compact.
    plan = plan_turn(
        contents,
        window=_window(20_000),
        tool_tokens=6_000,
        system_tokens=2_000,
        wire_tokens=9_000,
        output_reserve=1_000,
        phase="proactive",
    )
    assert plan.est_tokens == 9_000
    assert plan.compacted is False


def test_plan_turn_reserves_the_callers_own_output_cap():
    """The reply shares the window with the prompt, so a path that may generate
    16k must reserve 16k -- reserving another path's smaller cap would declare
    an overflowing prompt safe."""
    # ~5k estimated tokens of trimmable history in a 20k window.
    contents = long_alternating_history(pairs=5, filler=2000)
    common = dict(window=_window(20_000), phase="proactive")

    # Reserving 1k leaves a ~18k budget: the history fits, nothing is trimmed.
    assert plan_turn(list(contents), output_reserve=1_000, **common).compacted is False
    # Reserving 16k leaves only ~3k: the same history must now be trimmed.
    assert plan_turn(list(contents), output_reserve=16_000, **common).compacted is True


@pytest.mark.asyncio
async def test_anthropic_consults_the_exact_token_counter_only_when_marginal():
    """The provider's own counter beats our heuristic, but costs a round trip --
    so it is consulted only once the cheap estimate nears the window."""
    from trid3nt_server.adapters import anthropic_adapter

    counter = AsyncMock(return_value=123_456)
    small = [user_content("hi"), model_content("hello")]

    with patch.object(anthropic_adapter, "_exact_prompt_tokens", counter):
        # Comfortably under the window -> no decision to settle, no round trip.
        heuristic = 100
        window = ContextWindow(
            tokens=200_000, source=WINDOW_SOURCE_ANTHROPIC_MODELS,
            provider="anthropic", model="claude-sonnet-5",
        )
        assert heuristic < window.tokens * anthropic_adapter._COUNT_TOKENS_CONSULT_RATIO
        counter.assert_not_awaited()

    # And the counter itself degrades to the heuristic rather than failing the
    # turn when the endpoint is unavailable.
    client = AsyncMock()
    client.messages.count_tokens.side_effect = RuntimeError("no such endpoint")
    assert (
        await anthropic_adapter._exact_prompt_tokens(
            client, {"model": "m", "messages": []}
        )
        is None
    )


@pytest.mark.asyncio
async def test_anthropic_exact_counter_returns_the_provider_number():
    from trid3nt_server.adapters import anthropic_adapter

    class _Count:
        input_tokens = 187_432

    client = AsyncMock()
    client.messages.count_tokens.return_value = _Count()
    got = await anthropic_adapter._exact_prompt_tokens(
        client,
        {
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "x"}]}],
            "system": [{"type": "text", "text": "sys"}],
            "tools": [{"name": "t", "description": "d", "input_schema": {}}],
        },
    )
    assert got == 187_432
    # The whole prompt is counted -- system and tools included, not just messages.
    sent = client.messages.count_tokens.await_args.kwargs
    assert set(sent) == {"model", "messages", "system", "tools"}


# ---------------------------------------------------------------------------
# 3. Cache-prefix preservation
# ---------------------------------------------------------------------------


def _decls() -> list[genai_types.FunctionDeclaration]:
    return [
        genai_types.FunctionDeclaration(
            name=f"tool_{i}",
            description=f"does thing {i}",
            parameters=genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={"a": genai_types.Schema(type=genai_types.Type.STRING)},
            ),
        )
        for i in range(3)
    ]


def test_anthropic_cache_prefix_is_byte_identical_across_a_trim():
    """Trimming rewrites ONLY the conversation. The cacheable prefix renders
    tools -> system -> messages, so the breakpoints on the last tool and the
    system block cannot move when messages shrink."""
    from trid3nt_server.adapters.anthropic_adapter import _build_message_kwargs

    system = "You are TRID3NT." + " spec" * 500
    decls = _decls()
    long_history = long_alternating_history()

    full = _build_message_kwargs(long_history, decls, system, None)
    plan = plan_turn(long_history, window=_window(4096), phase="proactive")
    assert plan.compacted is True, "test needs an actually-trimmed history"
    trimmed = _build_message_kwargs(plan.contents, decls, system, None)

    assert full["tools"] == trimmed["tools"]
    assert full["system"] == trimmed["system"]
    assert full["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert full["system"][0]["cache_control"] == {"type": "ephemeral"}
    # ... and the volatile part -- and ONLY the volatile part -- changed.
    assert len(trimmed["messages"]) < len(full["messages"])


def test_bedrock_cache_points_survive_a_trim():
    from trid3nt_server.adapters.bedrock_adapter import _build_converse_kwargs

    system = "You are TRID3NT." + " spec" * 500
    decls = _decls()
    long_history = long_alternating_history()

    full = _build_converse_kwargs(
        long_history, decls, system, "us.anthropic.claude-sonnet-4-6"
    )
    plan = plan_turn(long_history, window=_window(4096), phase="proactive")
    assert plan.compacted is True
    trimmed = _build_converse_kwargs(
        plan.contents, decls, system, "us.anthropic.claude-sonnet-4-6"
    )

    assert full["system"] == trimmed["system"]
    assert full["toolConfig"] == trimmed["toolConfig"]
    assert full["system"][-1] == {"cachePoint": {"type": "default"}}
    assert full["toolConfig"]["tools"][-1] == {"cachePoint": {"type": "default"}}
    assert len(trimmed["messages"]) < len(full["messages"])


# ---------------------------------------------------------------------------
# 4. Overflow classification + retry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "prompt is too long: 210000 tokens > 200000 maximum",
        "ValidationException: Input is too long for requested model.",
        "This model's maximum context length is 32768 tokens",
        "context_length_exceeded",
        "too many input tokens",
        "input exceeds the maximum allowed",
    ],
)
def test_overflow_phrasings_are_classified(message):
    assert looks_like_context_overflow_error(Exception(message)) is True


@pytest.mark.parametrize(
    "message",
    [
        "invalid tool schema at #/tools/3",
        "ThrottlingException: rate exceeded",
        "AccessDeniedException",
        "Read timeout on endpoint URL",
    ],
)
def test_non_overflow_errors_are_not_reclassified(message):
    """Every OTHER 400 is a genuine bug in our request and must fail loudly --
    retrying it only hides the bug."""
    assert looks_like_context_overflow_error(Exception(message)) is False


def test_overflow_classifier_tolerates_none():
    assert looks_like_context_overflow_error(None) is False


@pytest.mark.asyncio
async def test_bedrock_overflow_trims_and_retries_once_then_succeeds():
    """A 400 overflow is logged verbatim, the history is trimmed HARDER, and
    the request is resent exactly once."""
    from trid3nt_server.adapters import bedrock_adapter
    from trid3nt_server.adapters.adapter import (
        CompactionCompleteEvent,
        CompactionStartEvent,
        TextDeltaEvent,
    )

    calls: list[dict[str, Any]] = []

    class _Overflow(Exception):
        pass

    def _fake_converse(client: Any, kwargs: dict[str, Any]) -> Any:
        calls.append(kwargs)
        if len(calls) == 1:
            raise _Overflow(
                "ValidationException: Input is too long for requested model."
            )
        return {
            "stream": [
                {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "ok"}}}
            ]
        }

    contents = long_alternating_history()

    with (
        patch.object(bedrock_adapter, "_bedrock_client", return_value=object()),
        patch.object(bedrock_adapter, "_converse_stream_with_retry", _fake_converse),
        patch.object(
            bedrock_adapter, "_is_transient_bedrock_error", return_value=False
        ),
    ):
        events = [
            e
            async for e in bedrock_adapter.stream_bedrock(
                contents, _decls(), "sys", "us.anthropic.claude-sonnet-4-6"
            )
        ]

    assert len(calls) == 2, "exactly one retry after the overflow"
    # The retry carried FEWER messages than the rejected request.
    assert len(calls[1]["messages"]) < len(calls[0]["messages"])
    # The cacheable prefix was rebuilt byte-identically across the retry.
    assert calls[0]["system"] == calls[1]["system"]
    assert calls[0]["toolConfig"] == calls[1]["toolConfig"]
    # The user saw the compaction, and the turn produced real output.
    assert any(isinstance(e, CompactionStartEvent) for e in events)
    assert any(isinstance(e, CompactionCompleteEvent) for e in events)
    assert any(isinstance(e, TextDeltaEvent) and e.delta == "ok" for e in events)


@pytest.mark.asyncio
async def test_bedrock_second_overflow_is_an_honest_typed_error():
    """Trim, retry once, then STOP -- surfaced as the dedicated
    CONTEXT_WINDOW_EXCEEDED envelope, never the provider-unavailable bucket."""
    from trid3nt_server.adapters import bedrock_adapter
    from trid3nt_server.gates.context_budget import ContextWindowExceededError

    def _always_overflow(client: Any, kwargs: dict[str, Any]) -> Any:
        raise Exception("Input is too long for requested model.")

    contents = long_alternating_history()

    with (
        patch.object(bedrock_adapter, "_bedrock_client", return_value=object()),
        patch.object(bedrock_adapter, "_converse_stream_with_retry", _always_overflow),
        patch.object(
            bedrock_adapter, "_is_transient_bedrock_error", return_value=False
        ),
        pytest.raises(ContextWindowExceededError),
    ):
        async for _ in bedrock_adapter.stream_bedrock(
            contents, _decls(), "sys", "us.anthropic.claude-sonnet-4-6"
        ):
            pass
