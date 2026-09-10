# `adapters/` - the LLM providers

One adapter per provider, all converting at their own boundary from the shared
IR: `google.genai.types` is the internal history and tool-declaration shape, and
a provider adapter is the only place a provider's own nouns are allowed to
appear. Every adapter yields the same streamed events, so the turn loop never
branches on which model is answering.

## Files

| file | what it is |
| --- | --- |
| `__init__.py` | The exposed adapters; `openai` is the default. |
| `adapter.py` | The IR containment and provider-dispatch seam - what a provider adapter is required to be. |
| `anthropic_adapter.py` | The Anthropic Messages adapter, over the official SDK. |
| `model_discovery.py` | Provider model listings and per-provider context-window resolvers; unresolved returns `None`, never a guess. |
| `model_selection.py` | Which provider and which model id, resolved at call time and independent of any one adapter. |
| `openai_adapter.py` | The OpenAI-compatible adapter, for any endpoint speaking that wire. |
| `scripted_adapter.py` | The zero-cost deterministic stand-in that replays a recorded cassette. |
| `tool_schema.py` | genai `FunctionDeclaration` -> the JSON Schema a provider API takes. |
