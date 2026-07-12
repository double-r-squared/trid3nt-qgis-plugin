"""Context-budget compaction + overflow guard for the LOCAL model path
(OPEN-14).

PROVEN FAILURE THIS FIXES (2x reproduced, session 01KX8GCZKNBAFEJ9SY1C8VNVND,
trid3nt-local/logs/agent.log): a turn's prompt hit exactly ``num_ctx=16384``;
Ollama silently clipped it; the model lost its tool contract and narrated a
fabricated success with zero tool calls.

Covers:
  1. Token estimator (chars/4).
  2. Compaction ladder: drop -> harden -> fold, hysteresis targets, the
     case-state-note / current-user-message survival guarantee.
  3. num_ctx discovery: /api/show parsing, ``-<N>k`` suffix fallback, env
     fallback, process-lifetime cache.
  4. Reactive clip guard: ``is_prompt_clipped`` + ``ContextWindowExceededError``.
  5. Fabrication backstop regex: positive (the real fabricated sentence
     shape) and negative (plain answers, capability statements) cases.

Run:
    cd services/agent && .venv/bin/python -m pytest tests/test_context_budget.py -q
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import types as genai_types

from grace2_agent.context_budget import (
    CompactionResult,
    CONTEXT_WINDOW_ABORT_NOTE,
    ContextWindowExceededError,
    FABRICATION_CAVEAT,
    _parse_num_ctx_from_show_response,
    _reset_num_ctx_cache_for_tests,
    build_context_window_abort_note,
    compact_contents,
    compute_budget_tokens,
    discover_num_ctx,
    estimate_tokens,
    estimate_tokens_for_contents,
    estimate_tokens_for_messages,
    estimate_tokens_for_tools,
    is_prompt_clipped,
    looks_like_fabricated_action_claim,
    num_ctx_env_fallback,
    num_ctx_from_suffix,
    openai_max_output_tokens,
    proactive_target_ratio,
    reactive_target_ratio,
    reserve_output_tokens,
    safety_tokens,
)


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_openai_adapter.py)
# ---------------------------------------------------------------------------


def user_content(text: str) -> genai_types.Content:
    return genai_types.Content(role="user", parts=[genai_types.Part(text=text)])


def model_content(text: str) -> genai_types.Content:
    return genai_types.Content(role="model", parts=[genai_types.Part(text=text)])


def fc_content(name: str, args: dict[str, Any], call_id: str = "c1") -> genai_types.Content:
    fc = genai_types.FunctionCall(name=name, args=args, id=call_id)
    return genai_types.Content(role="model", parts=[genai_types.Part(function_call=fc)])


def fr_content(name: str, response: dict[str, Any], call_id: str = "c1") -> genai_types.Content:
    fr = genai_types.FunctionResponse(name=name, response=response, id=call_id)
    return genai_types.Content(role="user", parts=[genai_types.Part(function_response=fr)])


def case_state_note_content(text: str = "These layers are ALREADY produced...") -> genai_types.Content:
    """A stand-in for the row server.py appends as the case-state note --
    just an ordinary role=user text Content structurally."""
    return genai_types.Content(role="user", parts=[genai_types.Part(text=text)])


# ---------------------------------------------------------------------------
# 1. Token estimator
# ---------------------------------------------------------------------------


class TestEstimator:
    def test_estimate_tokens_ceil_chars_over_4(self):
        assert estimate_tokens("") == 0
        assert estimate_tokens(None) == 0
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("abcde") == 2  # ceil(5/4) == 2
        assert estimate_tokens("x" * 400) == 100

    def test_estimate_tokens_for_contents_sums_rows(self):
        contents = [user_content("hello"), model_content("world")]
        total = estimate_tokens_for_contents(contents)
        assert total == estimate_tokens_for_contents([contents[0]]) + estimate_tokens_for_contents(
            [contents[1]]
        )
        assert total > 0

    def test_estimate_tokens_for_messages_and_tools(self):
        messages = [{"role": "user", "content": "x" * 40}]
        assert estimate_tokens_for_messages(messages) > 0
        assert estimate_tokens_for_messages([]) == 0
        tools = [{"type": "function", "function": {"name": "fetch_dem"}}]
        assert estimate_tokens_for_tools(tools) > 0
        assert estimate_tokens_for_tools(None) == 0
        assert estimate_tokens_for_tools([]) == 0


# ---------------------------------------------------------------------------
# Budget arithmetic
# ---------------------------------------------------------------------------


class TestBudget:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("GRACE2_OPENAI_MAX_TOKENS", raising=False)
        monkeypatch.delenv("GRACE2_CONTEXT_SAFETY_TOKENS", raising=False)
        assert reserve_output_tokens() == 4096
        assert safety_tokens() == 1024
        assert compute_budget_tokens(16384) == 16384 - 4096 - 1024

    def test_budget_floors_at_256_for_a_tiny_num_ctx(self, monkeypatch):
        monkeypatch.delenv("GRACE2_OPENAI_MAX_TOKENS", raising=False)
        monkeypatch.delenv("GRACE2_CONTEXT_SAFETY_TOKENS", raising=False)
        assert compute_budget_tokens(100) == 256

    def test_openai_max_output_tokens_default_and_override(self, monkeypatch):
        monkeypatch.delenv("GRACE2_OPENAI_MAX_TOKENS", raising=False)
        assert openai_max_output_tokens() == 4096
        monkeypatch.setenv("GRACE2_OPENAI_MAX_TOKENS", "1024")
        assert openai_max_output_tokens() == 1024
        # Garbage falls back to the safe default (same _env_int contract as
        # every other context-budget env knob).
        monkeypatch.setenv("GRACE2_OPENAI_MAX_TOKENS", "not-a-number")
        assert openai_max_output_tokens() == 4096

    def test_reserve_output_tokens_is_coupled_to_max_output_tokens(self, monkeypatch):
        """BUG 3 (post-OPEN-14 acceptance rerun): the proactive budget's
        output reserve must be the SAME number as the max_tokens cap sent on
        the wire -- a single source of truth, not two independently
        configured knobs that can drift apart."""
        monkeypatch.delenv("GRACE2_OPENAI_MAX_TOKENS", raising=False)
        assert reserve_output_tokens() == openai_max_output_tokens() == 4096
        monkeypatch.setenv("GRACE2_OPENAI_MAX_TOKENS", "777")
        assert reserve_output_tokens() == openai_max_output_tokens() == 777

    def test_ratio_defaults_and_overrides(self, monkeypatch):
        monkeypatch.delenv("GRACE2_CONTEXT_PROACTIVE_RATIO", raising=False)
        monkeypatch.delenv("GRACE2_CONTEXT_REACTIVE_RATIO", raising=False)
        assert proactive_target_ratio() == 0.75
        assert reactive_target_ratio() == 0.60
        monkeypatch.setenv("GRACE2_CONTEXT_PROACTIVE_RATIO", "0.5")
        assert proactive_target_ratio() == 0.5
        # Out-of-range / garbage falls back to the safe default.
        monkeypatch.setenv("GRACE2_CONTEXT_PROACTIVE_RATIO", "5")
        assert proactive_target_ratio() == 0.75
        monkeypatch.setenv("GRACE2_CONTEXT_PROACTIVE_RATIO", "not-a-number")
        assert proactive_target_ratio() == 0.75


# ---------------------------------------------------------------------------
# 2. Compaction ladder
# ---------------------------------------------------------------------------


class TestCompactionLadder:
    def test_noop_when_under_budget(self):
        contents = [user_content("hi"), user_content("there")]
        result = compact_contents(contents, budget_tokens=10_000, target_ratio=0.75)
        assert result.changed is False
        assert result.dropped == 0
        assert result.hardened == 0
        assert result.folded is False
        assert result.contents == contents

    def test_drop_oldest_first(self):
        # Many small rows + a protected tail. Budget forces some drops but not
        # a full fold (small target margin above the tail's own cost).
        rows = [user_content(f"turn {i} " + "x" * 40) for i in range(20)]
        tail = [user_content("case state note"), user_content("current question")]
        contents = rows + tail
        # Budget generous enough that dropping the oldest few rows alone
        # clears the (ratio-scaled) target -- never reaches hardening/folding.
        tail_tokens = estimate_tokens_for_contents(tail)
        budget = tail_tokens + estimate_tokens_for_contents(rows[-3:]) + 50
        result = compact_contents(contents, budget_tokens=budget, target_ratio=1.0)
        assert result.changed is True
        assert result.dropped > 0
        assert result.hardened == 0
        assert result.folded is False
        # Oldest rows are the ones gone; the tail always survives verbatim.
        assert result.contents[-2:] == tail
        assert rows[0] not in result.contents

    def test_harden_long_tool_result_rows(self):
        big_response = {"data": "y" * 2000}
        rows = [
            user_content("please fetch the DEM"),
            fc_content("fetch_dem", {"bbox": [0, 0, 1, 1]}),
            fr_content("fetch_dem", big_response),
        ]
        tail = [user_content("case state note"), user_content("current question")]
        contents = rows + tail
        # Budget too small for drop-only to clear it (drop removes the small
        # rows first; the huge tool-result row remains and must be hardened).
        budget = estimate_tokens_for_contents(tail) + 120
        result = compact_contents(contents, budget_tokens=budget, target_ratio=1.0)
        assert result.hardened >= 1
        # The hardened tool-result row shrank drastically.
        hardened_row = next(
            c
            for c in result.contents
            if getattr(c.parts[0], "function_response", None) is not None
        )
        resp = hardened_row.parts[0].function_response.response
        assert resp.get("truncated") is True
        assert len(resp["summary"]) <= 200
        # Tail untouched.
        assert result.contents[-2:] == tail

    def test_fold_remaining_rows_into_one_digest(self):
        # Tool call/response PAIRS are exempt from step (a) DROP (dropping one
        # side without the other would orphan the pairing -- see
        # ``_is_droppable_row``), so many of them survive drop AND, even
        # hardened, still overflow a tiny budget -- the only remaining lever
        # is fold.
        rows: list[genai_types.Content] = []
        for i in range(8):
            rows.append(fc_content(f"fetch_layer_{i}", {"i": i}, call_id=f"c{i}"))
            rows.append(fr_content(f"fetch_layer_{i}", {"data": "y" * 300}, call_id=f"c{i}"))
        tail = [case_state_note_content(), user_content("current question")]
        contents = rows + tail
        budget = estimate_tokens_for_contents(tail) + 30
        result = compact_contents(contents, budget_tokens=budget, target_ratio=1.0)
        assert result.dropped == 0  # nothing droppable -- all rows are tool call/response
        assert result.hardened >= 1  # step (b) ran first
        assert result.folded is True
        # working region collapsed to exactly one digest row + the protected tail.
        assert len(result.contents) == 1 + len(tail)
        digest = result.contents[0]
        assert digest.role == "user"
        assert digest.parts[0].text.startswith("Earlier in this case:")
        assert "fetch_layer_0" in digest.parts[0].text
        assert result.contents[-2:] == tail
        assert result.contents[-2:] == tail

    def test_hysteresis_targets_a_fraction_of_budget_not_100_percent(self):
        rows = [user_content(f"turn {i} " + "x" * 100) for i in range(30)]
        tail = [user_content("note"), user_content("question")]
        contents = rows + tail
        budget = 2000
        result_75 = compact_contents(contents, budget_tokens=budget, target_ratio=0.75)
        result_60 = compact_contents(contents, budget_tokens=budget, target_ratio=0.60)
        assert result_75.after_tokens <= budget * 0.75 + 1
        assert result_60.after_tokens <= budget * 0.60 + 1
        # A lower ratio (reactive) compacts at least as aggressively.
        assert result_60.after_tokens <= result_75.after_tokens

    def test_case_state_note_and_current_user_message_always_survive(self):
        """HARD RULE: never drop the case-state note or the current user
        message, even under an extreme squeeze that forces a full fold."""
        rows = [user_content(f"turn {i}") for i in range(5)]
        note = case_state_note_content("These layers are ALREADY produced: flood-depth RESULT")
        current_question = user_content("what is the peak depth now?")
        contents = rows + [note, current_question]
        # Absurdly tight budget -- forces drop + harden + fold all the way.
        result = compact_contents(contents, budget_tokens=1, target_ratio=1.0)
        assert result.contents[-1] is current_question
        assert result.contents[-2] is note

    def test_single_row_turn_is_never_touched(self):
        """A brand-new Case (just the user's first message) has nothing to
        compact -- the protected tail covers 100% of ``contents``."""
        contents = [user_content("hello")]
        result = compact_contents(contents, budget_tokens=1, target_ratio=1.0)
        assert result.contents == contents
        assert result.dropped == 0
        assert result.folded is False


# ---------------------------------------------------------------------------
# 3. num_ctx discovery
# ---------------------------------------------------------------------------


class TestNumCtxDiscovery:
    def setup_method(self):
        _reset_num_ctx_cache_for_tests()

    def teardown_method(self):
        _reset_num_ctx_cache_for_tests()

    def test_parse_num_ctx_from_show_response_parameters_field(self):
        # Verified live shape (2026-07-11) against qwen3.5-lowvram:9b-16k.
        payload = {
            "parameters": (
                "top_k                          20\n"
                "top_p                          0.95\n"
                "num_ctx                        16384\n"
                "presence_penalty               1.5\n"
                "temperature                    1"
            ),
        }
        assert _parse_num_ctx_from_show_response(payload) == 16384

    def test_parse_num_ctx_returns_none_when_no_override_baked(self):
        # A model with no PARAMETER num_ctx override (verified live: qwen3:8b,
        # llama3.2:3b, qwen3.5:9b base variants) has no num_ctx line at all.
        payload = {
            "parameters": (
                "stop                           \"<|im_start|>\"\n"
                "temperature                    0.6\n"
                "top_k                          20"
            ),
        }
        assert _parse_num_ctx_from_show_response(payload) is None

    def test_parse_num_ctx_ignores_model_info_context_length(self):
        # model_info.*.context_length is the architecture MAX (e.g. 262144),
        # never the runtime window -- must not be read as num_ctx.
        payload = {
            "parameters": "top_k    20",
            "model_info": {"qwen35.context_length": 262144},
        }
        assert _parse_num_ctx_from_show_response(payload) is None

    def test_parse_num_ctx_handles_malformed_payload(self):
        assert _parse_num_ctx_from_show_response({}) is None
        assert _parse_num_ctx_from_show_response(None) is None
        assert _parse_num_ctx_from_show_response({"parameters": ""}) is None
        assert _parse_num_ctx_from_show_response({"parameters": 12345}) is None

    def test_suffix_fallback(self):
        assert num_ctx_from_suffix("qwen3:8b-16k") == 16384
        assert num_ctx_from_suffix("llama3.2:3b-32k") == 32768
        assert num_ctx_from_suffix("qwen3.5-lowvram:9b-16k") == 16384
        assert num_ctx_from_suffix("qwen3.5:9b-8k") == 8192
        assert num_ctx_from_suffix("qwen3:8b") is None  # no suffix
        assert num_ctx_from_suffix("") is None
        assert num_ctx_from_suffix(None) is None

    def test_env_fallback_default_and_override(self, monkeypatch):
        monkeypatch.delenv("GRACE2_OPENAI_NUM_CTX", raising=False)
        assert num_ctx_env_fallback() == 16384
        monkeypatch.setenv("GRACE2_OPENAI_NUM_CTX", "8192")
        assert num_ctx_env_fallback() == 8192
        monkeypatch.setenv("GRACE2_OPENAI_NUM_CTX", "garbage")
        assert num_ctx_env_fallback() == 16384

    @pytest.mark.asyncio
    async def test_discover_num_ctx_uses_api_show_when_reachable(self, monkeypatch):
        monkeypatch.delenv("GRACE2_OPENAI_NUM_CTX", raising=False)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"parameters": "num_ctx    16384"}

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await discover_num_ctx("http://localhost:11434/v1", "qwen3.5-lowvram:9b-16k")
        assert result == 16384
        # Hits the Ollama-native root, not the /v1 OpenAI-compat mount.
        mock_client.post.assert_awaited_once()
        called_url = mock_client.post.await_args.args[0]
        assert called_url == "http://localhost:11434/api/show"

    @pytest.mark.asyncio
    async def test_discover_num_ctx_falls_back_to_suffix_on_404(self, monkeypatch):
        monkeypatch.delenv("GRACE2_OPENAI_NUM_CTX", raising=False)
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await discover_num_ctx("http://localhost:11434/v1", "qwen3:8b-16k")
        assert result == 16384  # from the -16k suffix, not the env default

    @pytest.mark.asyncio
    async def test_discover_num_ctx_falls_back_to_env_when_no_suffix_and_network_fails(
        self, monkeypatch
    ):
        monkeypatch.setenv("GRACE2_OPENAI_NUM_CTX", "4096")
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=ConnectionError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await discover_num_ctx("http://localhost:11434/v1", "qwen3:8b")
        assert result == 4096

    @pytest.mark.asyncio
    async def test_discover_num_ctx_caches_per_model_for_process_lifetime(self, monkeypatch):
        monkeypatch.delenv("GRACE2_OPENAI_NUM_CTX", raising=False)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"parameters": "num_ctx    16384"}
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client) as ctor:
            r1 = await discover_num_ctx("http://localhost:11434/v1", "qwen3:8b-16k")
            r2 = await discover_num_ctx("http://localhost:11434/v1", "qwen3:8b-16k")
        assert r1 == r2 == 16384
        # Only ONE network round-trip -- the second call hit the cache.
        assert ctor.call_count == 1


# ---------------------------------------------------------------------------
# 4. Reactive clip guard
# ---------------------------------------------------------------------------


class TestClipGuard:
    def test_is_prompt_clipped(self):
        assert is_prompt_clipped(16384, 16384) is True  # the reproduced shape
        assert is_prompt_clipped(16385, 16384) is True
        assert is_prompt_clipped(16000, 16384) is False
        assert is_prompt_clipped(None, 16384) is False
        assert is_prompt_clipped(100, 0) is False

    def test_context_window_exceeded_error_message(self):
        exc = ContextWindowExceededError(16384)
        msg = str(exc)
        assert "context window" in msg
        assert "16k" in msg
        assert "new case" in msg.lower() or "larger-context model" in msg.lower()
        assert exc.num_ctx == 16384

    def test_context_window_exceeded_error_rounds_small_num_ctx(self):
        # Never renders "0k" for a small/misconfigured window.
        exc = ContextWindowExceededError(100)
        assert "1k" in str(exc) or "0k" not in str(exc)


# ---------------------------------------------------------------------------
# 5. Fabrication backstop regex
# ---------------------------------------------------------------------------


class TestFabricationBackstop:
    def test_positive_real_fabricated_sentence_shape(self):
        # The actual shape from the incident narration.
        text = (
            "I have computed the hillshade from the DEM and published the "
            "resulting map to the Case."
        )
        assert looks_like_fabricated_action_claim(text) is True

    def test_positive_variants(self):
        assert looks_like_fabricated_action_claim(
            "I've created a new flood layer for you."
        ) is True
        assert looks_like_fabricated_action_claim(
            "The DEM has been fetched and a raster generated."
        ) is True
        assert looks_like_fabricated_action_claim(
            "Done -- I generated the plume model and updated the map."
        ) is True

    def test_negative_plain_qa(self):
        assert looks_like_fabricated_action_claim("The capital of Texas is Austin.") is False
        assert looks_like_fabricated_action_claim("") is False
        assert looks_like_fabricated_action_claim(None) is False

    def test_negative_capability_statement_present_tense(self):
        # "compute" (present/base form), not "computed" -- a capability
        # claim, not a claim of a FINISHED action.
        assert looks_like_fabricated_action_claim(
            "I can compute a hillshade if you give me a DEM."
        ) is False

    def test_negative_in_progress_statement(self):
        assert looks_like_fabricated_action_claim("Fetching the DEM now...") is False

    def test_negative_verb_without_geospatial_object(self):
        assert looks_like_fabricated_action_claim(
            "I published papers on this topic before."
        ) is False

    def test_fabrication_caveat_text_is_honest_and_stable(self):
        assert "no tools were executed" in FABRICATION_CAVEAT
        assert "not verified" in FABRICATION_CAVEAT


# ---------------------------------------------------------------------------
# BUG 1 / BUG 2 (post-OPEN-14 acceptance rerun): the abort-note builder wired
# into server.py's ``except ContextWindowExceededError`` handler.
# ---------------------------------------------------------------------------


class TestContextWindowAbortNote:
    def test_plain_abort_note_has_no_fabrication_caveat(self):
        note = build_context_window_abort_note(fabricated_claim=False)
        assert note == CONTEXT_WINDOW_ABORT_NOTE
        assert FABRICATION_CAVEAT not in note
        assert "context window" in note
        assert "unverified" in note
        assert "new case" in note.lower() or "larger-context model" in note.lower()

    def test_fabricated_claim_leads_with_the_caveat(self):
        """BUG 2: on the abort path, a zero-tool-call turn whose partial text
        claims a completed action must see the fabrication caveat BEFORE the
        context-window explanation, not after (or not at all)."""
        note = build_context_window_abort_note(fabricated_claim=True)
        assert FABRICATION_CAVEAT in note
        assert CONTEXT_WINDOW_ABORT_NOTE in note
        assert note.index(FABRICATION_CAVEAT) < note.index(CONTEXT_WINDOW_ABORT_NOTE)
