# ADR 0301 -- the Anthropic Messages API adapter (MODEL_PROVIDER=anthropic)

Status: LANDED (adapter + dispatch branch + 21 offline tests). Live smoke NOT
run -- no credential on this box (see "What is unproven" below).
Date: 2026-08-22.

## Context

The daemon's LLM seam had three live providers: `bedrock` (the cloud default,
now decommissioned infrastructure), `openai` (any OpenAI-compatible endpoint --
the local Ollama build and OpenRouter), and `scripted` (the deterministic test
double). The offline build has been driving a 32k-context free model through
the openai path, and that context window overflows constantly: the tool catalog
plus system prompt alone consume most of it, so `context_budget` compaction
fires on nearly every turn and long sessions die on
`CONTEXT_WINDOW_EXCEEDED`.

A first-party Anthropic path removes that pressure (1M context) and is the
provider whose prompt-caching semantics the cost-discipline guardrail was
written around.

## Decision

`trid3nt_server/adapters/anthropic_adapter.py` -- a sibling adapter behind the
same seam every other provider sits behind. It takes the shared inputs
(`list[genai_types.Content]` history, `list[FunctionDeclaration]` tool specs, a
system prompt) and yields the shared `StreamEvent` union, so the turn loop, the
emitter, the gates, and the plugin are untouched. `MODEL_PROVIDER=anthropic`
selects it at `adapter.stream_events_with_contents`.

Model: `claude-sonnet-5` by default, `TRID3NT_ANTHROPIC_MODEL` to override.
Credential: `ANTHROPIC_API_KEY`, resolved by the SDK; the adapter checks
presence first so an unset key is an honest named error rather than an SDK
construction failure.

### API facts this file encodes

These are 400s, not preferences, and they differ from what older Claude code
looks like:

- `thinking={"type": "adaptive"}`. `budget_tokens` is REMOVED on this model
  family -- sending it is a 400.
- No sampling parameters. `temperature` / `top_p` / `top_k` are removed on this
  family; the Bedrock path's `temperature=0.7` has NO analogue here and sending
  it is a 400. This is why `_build_message_kwargs` carries no inference config.
- No assistant prefill -- the request never ends on an assistant turn
  (`_ensure_messages_start_with_user` plus the loop's own shape guarantee it).
- Streaming on every call (`messages.stream` + `get_final_message()`), so a
  long tool-planning round cannot trip the SDK request timeout.
- Tool inputs are read as parsed JSON (`json.loads` when the SDK hands back a
  string) -- never string-matched. This family varies its JSON escaping.
- `stop_reason == "refusal"` is a 200, not an exception. It is narrated
  honestly with its category rather than surfacing as an empty turn.

### Prompt caching (mandatory)

The cacheable prefix renders `tools` -> `system` -> `messages`. Two
`cache_control: {"type": "ephemeral"}` breakpoints are placed: one on the LAST
tool of the catalog, one on the system block. Everything volatile (the
conversation) follows them. That is 2 of the 4 allowed breakpoints; the other
two are deliberately unspent, since a breakpoint inside the message history
would be invalidated by the very turn that added it.

`usage.cache_read_input_tokens` is the proof, and it is logged at INFO on every
turn (`anthropic usage model=... cache_read=... cache_write=...`) and carried
onto the shared `UsageMetadataEvent` as `cached_content_token_count` /
`cache_hit`, which is what the existing cache-status envelope and the tool-call
telemetry already read.

### Thinking blocks are not replayed

Adaptive thinking is on, `display` is left at its default (omitted), so no
reasoning text reaches chat and none is yielded as a `ThinkingDeltaEvent`. The
IR is genai `Content` objects, which carry no thinking blocks, so historical
assistant turns are replayed without them. That is legal: thinking blocks from
previous assistant turns are ignored by the API. The constraint that DOES bite
-- a final assistant message must start with a thinking block -- only applies to
prefill, which this path never does.

### Upstream-provider discipline

Same shape as the two existing adapters, sharing `provider_retries()` /
`provider_backoff_wait()` (`TRID3NT_PROVIDER_RETRIES` /
`TRID3NT_PROVIDER_BACKOFF_S`). Error classes are tested most-specific-first:
`BadRequestError` / `AuthenticationError` / `PermissionDeniedError` /
`NotFoundError` / `UnprocessableEntityError` fail fast; `RateLimitError` (429,
honoring `retry-after`), any `APIStatusError` with status >= 500, and
`APIConnectionError` (which subsumes `APITimeoutError`) retry with the
provider's error logged VERBATIM. Exhaustion raises the typed
`UpstreamProviderError(provider="Anthropic API", ...)`. A MID-stream transient
failure is classified the same way but never replayed -- tokens already flowed.
`classify_provider_error_class` gained the matching branch so the per-turn
telemetry records `error_class="upstream_provider"`, never `internal`.

## Consequences

- The Bedrock allowlist does not apply: `resolve_selected_model` passes a
  requested id through under this provider (the Messages API validates the id
  itself with a 404), mirroring what it already does for `openai`.
- Tool descriptions are NOT truncated here. The Bedrock toolSpec caps
  descriptions at 1000 chars; this API does not, so the registry's LLM-facing
  docstrings reach the model whole -- better routing, at the cost of a larger
  (but cached) prefix.
- `anthropic>=1,<2` joins the core dependency list, dormant unless the provider
  is selected -- the same posture `openai` has.

## What is unproven

No `ANTHROPIC_API_KEY` and no `ant` CLI profile exist on this box, so the live
smoke (one chat turn + one tool-call turn through the daemon, with
`cache_read_input_tokens > 0` on the second turn) has NOT been run. The adapter
is proven only against a mocked client. The default provider in `.env.local` is
therefore UNCHANGED. Switching it is:

    MODEL_PROVIDER=anthropic
    ANTHROPIC_API_KEY=sk-ant-...
    TRID3NT_ANTHROPIC_MODEL=claude-sonnet-5

followed by `make agent` restart, `scripts/ws_smoke.py`, and a second turn whose
usage line shows a non-zero `cache_read`.
