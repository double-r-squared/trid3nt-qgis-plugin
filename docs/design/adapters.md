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
- `openai_adapter.py` -- OpenRouter + local-model (Ollama) path
  (`stream_openai`, `FunctionCallEvent`, `openai_api_key`).
- `model_discovery.py` -- the provider model-LIST surface behind
  `/api/local-models`: the installed-Ollama and OpenRouter free/tool-capable
  listings + the Ollama API-root/tags URL derivation (`_fetch_local_models`,
  `_filter_openrouter_models`, `_fetch_openrouter_models`, `_ollama_root`,
  `_ollama_tags_url`, `_local_models_route_enabled`). The provider nouns
  (`openrouter.ai`, Ollama, `model_provider() == "openai"`) that used to sit in
  the catalog HTTP module + `gates/context_budget` are quarantined here.
- `scripted_adapter.py` -- deterministic test double.

## Composition

`server/turn/stream.py` drives these via the shared `adapter.py` surface. The
pluggable-LLM story (cloud API or local model) is a provider swap behind this
seam. Provider model-discovery folded into `model_discovery.py` (ADR 0279): the
`protocol/catalog_http` route handler and `gates/context_budget` import it
instead of defining provider logic themselves.

## Invariants / extension points

- Provider vocabulary is quarantined here -- no provider nouns leak into
  `server/`, `data/`, or `workflows/`.
- Prompt-caching carries across any model swap (cost-discipline).
- A new provider drops in as a sibling adapter behind the shared surface.
