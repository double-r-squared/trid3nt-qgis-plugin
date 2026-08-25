# 0311 - Per-model context budget: runtime window discovery + one client-side trim seam

Context: the context window was only ever a LOCAL concern. `gates/context_budget`
existed to stop Ollama silently clipping an over-long prompt (the model loses its
tool contract, fires zero tools, and narrates a fabricated success), and it was
scoped that way in its own docstring: "all LOCAL (`MODEL_PROVIDER=openai`) only --
the Bedrock path is untouched". Two facts made that scoping wrong. Pointing
`TRID3NT_OPENAI_MODEL` at a local model overflowed 32k because the OpenRouter and
Ollama paths shared one 16k env fallback and no discovery. And the Anthropic and
Bedrock paths had NO budget management at all -- they simply sent whatever history
had accumulated and let the provider 400.

Decision: the context window is a PER-MODEL FACT DISCOVERED AT RUNTIME, never
hardcoded, and history management is CLIENT-SIDE in ONE place shared by every
adapter.

## Discovery (`context_budget.discover_context_window`)

Provider-agnostic, cached per `(provider, model)` for the process lifetime, and
cleared by `reset_num_ctx_cache()` so a live provider switch re-discovers instead
of serving the previous provider's window. Per-provider resolvers stay quarantined
in `adapters/model_discovery` and each returns `None` -- never a guess -- when the
provider does not state a window:

| Provider | Source of the window fact |
| --- | --- |
| openai-compatible (OpenRouter) | `GET /models` -> `context_length`, taking the MIN of the row and `top_provider.context_length` (the routed upstream can serve a shorter window than the headline) |
| openai-compatible (Ollama) | `POST /api/show` -> the runtime `num_ctx` inside the `parameters` free-text field. Deliberately NOT `model_info.*.context_length`, which is the much larger max TRAINED context and would silently defeat the guard |
| openai-compatible (fallback) | a `-<N>k` name suffix, then `TRID3NT_OPENAI_NUM_CTX` |
| anthropic | Models API `max_input_tokens`. That field IS the context window here; there is no `context_window` field |
| bedrock | a MAINTAINED TABLE, and the only one on the roster. Bedrock publishes no runtime fact -- Converse never reports it and `get_foundation_model` returns modalities but no token capacity -- so every read logs a WARNING naming the number as hand-kept |

Then `TRID3NT_CONTEXT_WINDOW` (operator pin), then a conservative 16384 default
with a WARNING. Every resolved window carries a `source`, so a provider-stated
fact is always distinguishable from a fallback, and an undiscovered window
narrates the assumption (`ContextWindow.narration()`) rather than passing for
truth. A provider that DOES state its window outranks the env pin: if discovery
succeeded, the env var is stale.

Consequence: `discover_num_ctx` became a thin wrapper over the shared ladder
rather than a parallel implementation, and its now-dead `_NUM_CTX_CACHE` was
removed (the provider-config route test now asserts against the live cache).

## The one trim seam (`context_budget.plan_turn`)

All three live adapters -- OpenAI-compatible, Anthropic, Bedrock -- call it and do
nothing else strategic: they translate the planned `contents` into their own wire
shape and emit `CompactionStartEvent` / `CompactionCompleteEvent`. What gets
dropped, when, and what is untouchable is decided once.

Always preserved: the system prompt and tool contracts (never candidates -- they
are not in `contents` at all) plus the terminal user message and the case-state
note carrying the pending-confirmation spine (`compact_contents`'s protected
tail).

Two corrections the generalization forced, both real bugs rather than cosmetics:

- **The output reserve must be the CALLER's cap.** `compute_budget_tokens`
  reserved `openai_max_output_tokens()` (4096) unconditionally, but Anthropic
  requests `max_tokens=16000` and Bedrock 8192. The reply shares the window with
  the prompt, so reserving another path's smaller cap would declare an
  overflowing prompt safe. `plan_turn` now takes `output_reserve` and each
  adapter passes its own.
- **The rejected prompt is itself an upper bound.** A reactive pass runs *because*
  the provider said the prompt did not fit, which means the window fact or the
  estimator was wrong. Budgeting the retry off that same (evidently wrong) window
  let the ladder conclude there was nothing to do -- we would resend a
  byte-identical prompt and burn the one retry. The reactive budget is now
  clamped to what was just sent, guaranteeing the retry is strictly smaller.

## Prompt-cache preservation

Cache-safe BY CONSTRUCTION, not by careful placement: the plan rewrites only the
conversation, and every provider's cache breakpoints sit on the tool catalog and
the system block, which render BEFORE messages (`tools` -> `system` ->
`messages`). Trimming the tail therefore cannot move or invalidate the cached
prefix. Adapters run the plan BEFORE building request kwargs so the prefix is
rebuilt byte-identically. Pinned by tests asserting `tools`/`system` (Anthropic
`cache_control`) and `system`/`toolConfig` (Bedrock `cachePoint`) are equal across
a trimmed turn, and that the overflow RETRY resends an identical prefix.

## Token counting

The stated heuristic stays `ceil(chars / 4)`. Anthropic offers an exact counter
(`messages.count_tokens`), which is strictly better but costs a round trip, so it
is consulted only once the cheap estimate reaches 70% of the window -- exactly
when the decision is marginal and being wrong is expensive, and never on the
common comfortable turn. It degrades to the heuristic on any fault. `wire_tokens`
is now defined as the AUTHORITATIVE TOTAL prompt (conversation + tools + system);
nothing is added on top, so an exact count cannot be double-counted against the
tool catalog.

## Upstream-provider honesty

`looks_like_context_overflow_error` separates the one 400 worth retrying ("prompt
is too long" / "Input is too long" / `context_length_exceeded`) from every other
400, which is a genuine bug in our request and must fail loudly. On overflow the
provider's message is logged VERBATIM, the history is trimmed harder, and the
request is resent exactly ONCE; a second overflow raises the typed
`ContextWindowExceededError` -> the dedicated `CONTEXT_WINDOW_EXCEEDED` envelope,
never the generic provider-unavailable bucket. Only retried before any event has
flowed -- a replay after tokens streamed would duplicate output.

Consequence: `CompactionStartEvent`'s "never emitted by the Bedrock path" contract
is retired; it is now emitted by every live provider, and the server dispatch loop
already handled it provider-agnostically.

## Not done, deliberately

Provider-side compaction (the Anthropic `compact-2026-01-12` beta) stays
unimplemented. Client-side management is the requirement and the portable answer
-- generic hosts do nothing for you -- and provider-side compaction is an opt-in
extra layered on top, not a replacement. Adding it would mean persisting
compaction blocks back into history on one provider only; that is its own wave
when there is a reason to want it.
