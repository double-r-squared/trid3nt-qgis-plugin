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
- `scripted_adapter.py` -- deterministic test double.

## Composition

`server/turn.py` and `server/_core` drive these via the shared `adapter.py`
surface. The pluggable-LLM story (cloud API or local model) is a provider swap
behind this seam. Model-discovery HTTP routes still live in
`tool_catalog_http` / `gates/context_budget`; folding them into this folder is
deferred (ADR 0277).

## Invariants / extension points

- Provider vocabulary is quarantined here -- no provider nouns leak into
  `server/`, `data/`, or `workflows/`.
- Prompt-caching carries across any model swap (cost-discipline).
- A new provider drops in as a sibling adapter behind the shared surface.
