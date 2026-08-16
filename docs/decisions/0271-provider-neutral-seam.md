# ADR 0271 - server-refactor wave 12: provider-neutral model-dispatch seam

Status: LANDED. Date: 2026-08-16.

## Context

NATE's catch (wave 10/11 corrected comment prose but left code speaking
provider nouns): the model-dispatch call site in `_core.py` still passed
`state.gemini_cache_name` and `bedrock_model=` into the streaming seam. The
rule: core code speaks ONE provider-neutral adapter vocabulary; only the
adapter modules (`bedrock_adapter`, `openai_adapter`, `scripted_adapter`,
`adapter`) know provider names. A field named `gemini_cache_name` or a param
named `bedrock_model` in `server/` is core code leaking the adapter's private
vocabulary across the seam.

This wave renames every provider-specific IDENTIFIER (variable / field / param
/ function / wire-value name) OUTSIDE the adapter modules to neutral names, and
moves the neutral -> provider translation to the adapter boundary. Pure renames
plus boundary re-naming; zero behavior change.

## Identifier inventory (before -> after)

| Before | After | Location | Kind |
| --- | --- | --- | --- |
| `SessionState.gemini_cache_name` | `SessionState.model_cache_ref` | `server/session.py` | session field (provider-side prompt-cache handle) |
| `bedrock_model` param | `model_id` | `server/_core.py` `_stream_model_reply` | function param |
| `bedrock_model` param | `model_id` | `server/_core.py` `_dispatch_model_turn_and_persist` | function param |
| `_turn_bedrock_model` | `_turn_model_id` | `server/_core.py` (per-turn model selection) | local var |
| `bedrock_model_id as _active_bedrock_model_id` | `... as _active_default_model_id` | `server/_core.py` `main`/startup log | import alias |
| `tool_name="gemini_generate"` | `tool_name="model_generate"` | `server/_core.py` (x4 pipeline-step sites) | telemetry/wire value |
| `"cache_name"` (cache-status payload key) | `"model_cache_ref"` | `server/turn.py` `_emit_cache_status` | wire envelope key |
| `stream_events_with_contents(cached_content_name=, bedrock_model=)` | `(model_cache_ref=, model_id=)` | `agent/adapters/adapter.py` seam entry | boundary params |
| `stream_events(cached_content_name=)` | `(model_cache_ref=)` | `agent/adapters/adapter.py` seam wrapper | boundary param |
| log strings "gemini function-call / usage / loop ..." | "model function-call / usage / loop ..." | `server/_core.py` | runtime log prose |

`_LLM_STEP_NAMES` in the plugin (`qgis-plugin/trid3nt/ui/dock.py`) already
carried `model_generate` alongside the provider-named `gemini_generate` /
`bedrock_generate` / `ollama_generate`; the server now emits only
`model_generate`, so the three dead provider-named aliases were dropped from
the plugin's tolerance set (rides plugin 0.3.16, per the kickoff).

## Adapter-boundary translation points

The neutral vocabulary stops at `agent/adapters/adapter.py`, the provider
router. `stream_events_with_contents` is the seam `_core` calls; its params are
now `model_id` (neutral model selection) and `model_cache_ref` (neutral
prompt-cache handle). Inside the router:

- `model_id` forwards to the concrete provider adapters as `model=` --
  already the neutral param name on `stream_bedrock` / `stream_openai` /
  `stream_scripted`. Those adapters (and only those) map it to provider-native
  request fields (`modelId` for Bedrock Converse, `model` for the OpenAI-
  compatible endpoint).
- `model_cache_ref` is the provider-side prompt-cache handle. It is inert on
  the live paths (Bedrock caches via its own `cachePoint` markers, reported
  through `UsageMetadataEvent`; there is no separate cached-content fast-path),
  so it is carried but unused there -- documented at the field and the seam.
- The `model_provider()` registry read and the `== "bedrock"` / `== "openai"`
  provider-tag comparisons that survive in `_core.py` (telemetry model-tag
  resolution, startup log) are the LEGITIMATE seam boundary: core asks the
  registry "which provider is active" to tag telemetry and pick which adapter's
  model-resolver to call. That is the adapter-selection contract, not leaked
  vocabulary, so those string comparisons stay.

## Wire / persisted verdicts

- `cache-status` envelope key `cache_name` -> `model_cache_ref`: the QGIS
  plugin does NOT read this key (grep of `qgis-plugin/`); it is an
  observability-only surface (explicitly "not a wire-API contract"). Renamed on
  both the emitter and its one test with no plugin-side change needed.
- `tool_name` pipeline-step value `gemini_generate` -> `model_generate`: the
  plugin DOES read this value (`_LLM_STEP_NAMES` step-hiding set). The plugin's
  set already contained `model_generate`, so the emitted value is already
  handled; coordinated on plugin 0.3.16.
- `SessionState.model_cache_ref` is NOT persisted (in-memory per-turn handle,
  reset to `None` each turn; absent from `persistence.py` / session
  serialization). No stale-key tolerance needed.

## Env-var notes

- `TRID3NT_GEMINI_MODEL` (read only in `adapter.py::load_settings` as the
  scripted/display fallback model label) is provider-named but lives INSIDE an
  adapter module and is NOT set in NATE's `.env.local` (which uses
  `MODEL_PROVIDER=openai` + `TRID3NT_OPENAI_MODEL`). Left as-is this wave; a
  neutral `TRID3NT_MODEL` alias is a candidate for a settings pass. No alias
  added, so nothing in `.env.local` breaks.
- `TRID3NT_OPENAI_*` are the OpenAI-compatible adapter's own config surface
  (adapter-owned, legitimately provider-named).

## Residual provider nouns OUTSIDE adapters (flagged, not renamed)

The renamed-identifier count for the model-DISPATCH seam is zero. The residual
provider nouns in non-adapter code are the model-DISCOVERY surface (a SEPARATE
seam from NATE's dispatch catch): the web/plugin model-selector `/models` HTTP
route and context-window probe genuinely encode per-provider endpoint quirks.

| Item | Location | Why not renamed |
| --- | --- | --- |
| `_ollama_tags_url`, `_filter_openrouter_models`, `_fetch_openrouter_models`, `openrouter.ai` host branch | `tool_catalog_http.py` (model-selector route) | Honest names for provider-specific list endpoints; relocating into `openai_adapter` is a structural move, not a rename, and no gate covers that route |
| `_ollama_root` (context-window probe) | `agent/gates/context_budget.py` | Same class: ollama-specific `/api/tags` context probe |
| `ModelSettings.project/location/use_vertex`, `load_settings` | `agent/adapters/adapter.py` | Inside an adapter module (out of the non-adapter scope) |
| Comment/log residue naming dead systems | scattered | Comments are wave 8-10 scope, not identifiers |

Recommendation: fold the model-discovery helpers into `openai_adapter` (which
already owns the OpenAI-compatible providers incl. ollama/openrouter) in a
follow-up job with its own model-selector route smoke.

## Gate evidence

- Offline slices (4) at baseline: a-e 1499 passed / 0 fail; f-o 6377 passed /
  4 fetch_resolution; p-r 2020 passed / 2 river_dye; s-z 1414 passed / 0 fail.
- contracts: 708 passed. Registry import: OK (252 tools).
- Daemon restart OK; `ws_smoke.py all_passed=True` (real model turn; the
  `cache-status` envelope emitted with `model_cache_ref`; step = `model_generate`
  / `llm_generation`).
- Flood canary `run_sfincs_direct.py`: status=ok, depth COG published, envelope
  complete (1 layer + 7 frames).
- Plugin suite: 390 pass; 2 pre-existing Qt-harness failures
  (`test_case_bbox_dock_behaviors`, tool_picker `test_harness_green`) verified
  identical with `dock.py` reverted -- unrelated to the `_LLM_STEP_NAMES` edit.

## Consequence

The model-dispatch seam now speaks one neutral vocabulary end to end:
`_core` -> `model_id` / `model_cache_ref` -> `adapter.py` router -> `model=` on
the concrete provider adapters, which alone translate to provider-native
fields. NATE's catch (`state.gemini_cache_name`, `bedrock_model=`) is resolved;
provider-noun identifiers on the dispatch path outside the adapters: zero. The
model-DISCOVERY route remains provider-aware and is flagged for a follow-up
relocation into `openai_adapter`.
