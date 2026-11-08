# adapters/ -- LLM provider adapters

`trid3nt_server/model/adapters/` is the ONLY place
provider nouns appear. It presents one contents/declarations/system-prompt
surface the turn engine drives regardless of backend.

## What lives here

- `adapter.py` -- the shared surface: `SYSTEM_PROMPT`,
  `build_contents_from_history`, `build_tool_declarations`,
  `summarize_tool_result`, `UsageMetadataEvent`, error classification. Builds
  turns from the `trid3nt_contracts.message` IR. A tool's result reaches the
  model whole when it fits the TOOL_RESULT_CHARS setting and summarized past
  it; a refusal - raised, or returned as `{"status": "error"}` by a template's
  fill - is an error envelope, its text cut only past that same budget.
- `model_selection.py` -- provider + per-turn model resolution, owned by no
  one provider: `model_provider()` reads `MODEL_PROVIDER` at call time, and
  `resolve_selected_model()` validates a client-supplied model id before it
  can reach a provider API.
- `anthropic_adapter.py` -- the first-party Anthropic Messages API path
  (`stream_anthropic`, `anthropic_model`, `anthropic_api_key`), selected by
  `MODEL_PROVIDER=anthropic`. Claude Sonnet 5 by default
  (`TRID3NT_ANTHROPIC_MODEL`), adaptive thinking, no sampling params, two
  `cache_control` breakpoints on the tool catalog + system block.
- `openai_adapter.py` -- OpenRouter + local-model (Ollama) path
  (`stream_openai`, `FunctionCallEvent`, `openai_api_key`).
- `model_discovery.py` -- the provider model-LIST surface behind
  `/api/local-models`: the installed-Ollama and OpenRouter free/tool-capable
  listings + the Ollama API-root/tags URL derivation (`_fetch_local_models`,
  `_filter_openrouter_models`, `_fetch_openrouter_models`, `_ollama_root`,
  `_ollama_tags_url`, `_local_models_route_enabled`). ALSO the per-provider
  CONTEXT-WINDOW resolvers (`openrouter_context_length`,
  `anthropic_max_input_tokens`, and their pure parsers), each returning `None`
  rather than a guess when the provider states nothing. The provider nouns
  (`openrouter.ai`, Ollama, `model_provider() == "openai"`) that used to sit in
  the settings door + `model/guards/context_budget` are quarantined here.
- `scripted_adapter.py` -- deterministic test double.

## Composition

The message IR is ours: `Message`, `Part`, `ToolCall`, `ToolResponse` and
`ToolDeclaration` in `trid3nt_contracts.message` carry a turn, and each adapter
converts them to its own wire shape at its own boundary, so no provider SDK
type crosses the seam. A declaration carries JSON Schema, which is the argument
shape both provider APIs take.

`server/turn/stream.py` drives these via the shared `adapter.py` surface. The
pluggable-LLM story (cloud API or local model) is a provider swap behind this
seam. Provider model-discovery folded into `model_discovery.py`: the
`protocol/http/settings` route handlers and `model/guards/context_budget` import it
instead of defining provider logic themselves.

## Context budget

The context window is a PER-MODEL FACT DISCOVERED AT RUNTIME, never hardcoded.
`model/guards/context_budget.discover_context_window` resolves it per
`(provider, model)` through the resolvers above, then an operator pin, then a
conservative default with a warning; `ContextWindow.source` records which.

History management is CLIENT-SIDE and the strategy lives in ONE place --
`context_budget.plan_turn`. Every adapter calls it, then only translates the
planned `contents` into its wire shape and emits the compaction events. An
adapter passes its OWN `max_tokens` as `output_reserve` (the reply shares the
window) and may supply an authoritative total via `wire_tokens` (the OpenAI
path measures the real payload; the Anthropic path consults `count_tokens` once
the estimate nears the window).

## Tools offered by confidence

A turn is offered the core floor, the tools the model has CALLED this Case, and what the message matches. The message is
split by rule (no model call) at "then", "and then", "after that", ";" and an "and" that joins two action phrases; each
step is ranked on its own, and a step's matches are its best tool plus every tool within `MATCH_MARGIN` of it by
embedding similarity (all-MiniLM-L6-v2, normalized cosine, on the CPU so it works while the local model holds the GPU).
Below `MIN_MATCH_SIMILARITY` a step adds nothing and the model has `search_tools`. The steps' matches are taken one per
step in turn and capped at `TOOL_RETRIEVAL_K`, a ceiling and never a fill. RRF still orders; the cut is on similarity.

The dense channel embeds a document as rows - a head (name and description) and one per corpus phrasing - and a document's
similarity is its best row's, because the encoder reads 256 tokens and a docstring fills them before the phrasings begin.

AUTO offers the matched set to the model; ASK shows the same set on the tool-candidates card. A tool offered and never
called drops next turn; a called one stays (`SessionState.called_tools`). `find_sources(query=...)` returns the sources
within `SOURCE_MARGIN` of its top row on the same similarity. A cold index or a fault fails open to the full registry.

## The data door - find_sources, then fetch

No data source is a tool the model is told of or ranks. The prompt's tool list
(`build_tool_section`), the declarable registry, the fail-open dump and the
retrieval index all skip every spec-served fetcher (`registered_spec_names()`);
what the model sees is two tools on the always-visible floor: `find_sources`
(`need` a class, or `query` free text, at a `place` and `window`) and
`fetch(source, args)`. Every source's phrasings index as documents named
`find_sources`, so a message about its data ranks the door and spends one slot.

`find_sources` rows key `source`; a query row is `unranked` (a source with no
coverage row has no facts to weigh) and carries `about`, `params` and the `ask`
to hand to `fetch`. A call to `fetch` is translated at the dispatch entry
(`_invoke_tool_via_emitter`) and in the turn loop into a call of the source it
names, so gates, cards, the cache, the reuse guard, the circuit breaker and the
emit-on-fetch silence are the source's own. An argument the source does not take,
or a source that does not exist, is refused by name with the nearest. All
registered fetchers stay callable by name (`!run`, templates, composers).

A template input is described in its schema as well as its docstring: the unit,
range or shape first, then the first sentence of what it means, generated from the
declaration (`Input.schema_description`) and cut to INPUT_DESC_CHARS.

## Settings and limits

What a person may reasonably change about a turn is the `model` family of
settings (`docs/design/settings.md`): MAX_TURN_STEPS, MAX_TURN_SECONDS,
TEMPERATURE (sent where the model accepts one), TOOL_RESULT_CHARS, and the
small-context wire's TOOL_DESC_CHARS / PARAM_DESC_CHARS, and INPUT_DESC_CHARS for the
description each template input carries. The internal limits are
one constant each where they are used: the provider retry count and backoff
(`adapter.py`), the rate-limit wait cap (`openai_adapter.py`), the circuit
breaker's threshold and cooldown, the loop watchdog's repeat count and the
context budget's reserves and ratios (`model/guards/`). The served model's
window stays an environment fact (`TRID3NT_OPENAI_NUM_CTX`,
`TRID3NT_CONTEXT_WINDOW`).

## Invariants / extension points

- Provider vocabulary is quarantined here -- no provider nouns leak into
  `server/`, `data/`, or `workflows/`.
- Prompt-caching carries across any model swap (cost-discipline). Trimming is
  cache-safe by construction: it rewrites only the conversation, and every
  provider's breakpoints sit on tools/system, which render BEFORE messages.
- A new provider drops in as a sibling adapter behind the shared surface: add
  its window resolver to `model_discovery`, wire it into
  `_resolve_window_tokens`, and call `plan_turn` -- never a second trim policy.
