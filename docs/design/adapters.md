# adapters/ -- LLM provider adapters

`trid3nt_server/adapters/` (was `agent/adapters/`, ADR 0277) is the ONLY place
provider nouns appear. It presents one contents/declarations/system-prompt
surface the turn engine drives regardless of backend.

## What lives here

- `adapter.py` -- the shared surface: `SYSTEM_PROMPT`,
  `build_contents_from_history`, `build_tool_declarations`,
  `MAX_TURN_ITERATIONS`, `UsageMetadataEvent`, error classification. Reuses
  `google.genai.types` as the Content/Part containment layer.
- `bedrock_adapter.py` -- the default cloud provider (`model_provider`).
- `anthropic_adapter.py` -- the first-party Anthropic Messages API path
  (`stream_anthropic`, `anthropic_model`, `anthropic_api_key`), selected by
  `MODEL_PROVIDER=anthropic`. Claude Sonnet 5 by default
  (`TRID3NT_ANTHROPIC_MODEL`), adaptive thinking, no sampling params, two
  `cache_control` breakpoints on the tool catalog + system block. ADR 0301.
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
  the catalog HTTP module + `gates/context_budget` are quarantined here.
- `scripted_adapter.py` -- deterministic test double.

## Composition

`server/turn/stream.py` drives these via the shared `adapter.py` surface. The
pluggable-LLM story (cloud API or local model) is a provider swap behind this
seam. Provider model-discovery folded into `model_discovery.py` (ADR 0279): the
`protocol/catalog_http` route handler and `gates/context_budget` import it
instead of defining provider logic themselves.

## Context budget (ADR 0311)

The context window is a PER-MODEL FACT DISCOVERED AT RUNTIME, never hardcoded.
`gates/context_budget.discover_context_window` resolves it per
`(provider, model)` through the resolvers above (Bedrock alone needs a
maintained table, and logs a WARNING saying so), then an operator pin, then a
conservative default with a warning; `ContextWindow.source` records which.

History management is CLIENT-SIDE and the strategy lives in ONE place --
`context_budget.plan_turn`. Every adapter calls it, then only translates the
planned `contents` into its wire shape and emits the compaction events. An
adapter passes its OWN `max_tokens` as `output_reserve` (the reply shares the
window) and may supply an authoritative total via `wire_tokens` (the OpenAI
path measures the real payload; the Anthropic path consults `count_tokens` once
the estimate nears the window).

## Invariants / extension points

- Provider vocabulary is quarantined here -- no provider nouns leak into
  `server/`, `data/`, or `workflows/`.
- Prompt-caching carries across any model swap (cost-discipline). Trimming is
  cache-safe by construction: it rewrites only the conversation, and every
  provider's breakpoints sit on tools/system, which render BEFORE messages.
- A new provider drops in as a sibling adapter behind the shared surface: add
  its window resolver to `model_discovery`, wire it into
  `_resolve_window_tokens`, and call `plan_turn` -- never a second trim policy.
