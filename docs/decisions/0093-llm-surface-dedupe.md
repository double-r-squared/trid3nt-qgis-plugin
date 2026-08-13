# ADR 0093 -- LLM fed-surface dedupe: door envelope-shape boilerplate

Status: accepted (2026-08-03)
Follows: ADR 0057 (mass docstring refresh -- the retrieval-verification gate this
wave reuses). NATE directive (2026-08-03, from the docstring mass-refresh review):
"make sure that we aren't feeding the LLM duplicate information."

## Context

Census of every surface fed to the LLM at runtime -- the assembled system prompt
(`adapter.SYSTEM_PROMPT`, ~30.5k chars sent per turn), the 186 registered tool
FunctionDeclaration descriptions (docstring, Bedrock-truncated at 1000 chars), the
catalog snippets (`list_tools_in_category`), and the engine-door runtime envelopes.
corpus.yaml queries are retrieval-index-only (never fed to the model) -- OUT of scope.

Mechanical findings (hash/sentence/shingle match across all fed surfaces):

- **The docstring corpus is already deduplicated** -- ADR 0057's refresh did the
  work. Cross-docstring verbatim repetition is ~2.4k chars and is dominated by
  LEGITIMATE per-tool contract: each fetcher naming its own cache class (FR-DC-6 /
  FR-CE-8 lines), its own tier/key/`supports_global_query` flag, its own typed
  honest-empty error. Per NATE doctrine that is NOT duplication (analogous to every
  fetcher naming its own typed error codes).
- **Class (b) (docstrings paraphrasing system-prompt doctrine) is essentially
  absent.** Probe phrases (honesty-floor, bbox/AOI auto-fill, reuse-before-rerun,
  retry-on-arg-error) hit only per-tool CONTRACT text (a tool's own honest-empty
  path / its own cache key), never re-stamped cross-tool doctrine.
- **The one genuine, zero-loss docstring duplication** is the engine-door
  return-envelope enumeration. All 9 doors (`run_sfincs` ... `run_pelicun`) ended
  their function docstring with a BYTE-IDENTICAL paragraph enumerating the return
  keys: "Returns a read-only concierge envelope: `engine`, `kind`, `templates`
  (each `tool_name` / `question` / `required_inputs` / `knobs`), `fidelity_brief`,
  `mismatch_redirect`, `next_action`." This restates the paragraph two lines above
  ("returns the available `X_*` templates, each with its one-line question,
  required inputs, and knobs") AND is exactly the envelope the model RECEIVES the
  instant it calls the door -- fed twice, once statically, once at runtime.

Two duplication classes were characterized and DEFERRED as architectural (scope-6
STOP), NOT hand-edited:

- **System-prompt internal redundancy.** The 30.5k-char prompt states some rules
  2-4x (reuse-before-rerun, flood routing, named-tool dispatch). Much of this is
  INTENTIONAL emphasis-duplication documented in-prompt as necessary after a live
  regression ("This rule exists because the live agent IGNORED the softer steer
  below and re-ran..."), and it is heavily pinned by `test_system_prompt.py`.
  Collapsing it risks the exact behaviors it encodes, for a cached (per-turn-free
  after turn 1) surface. STOP with named residual -- a separate decision for NATE.
- **System-prompt door-routing vs door docstrings.** The prompt's flood /
  groundwater routing blocks teach door SELECTION; the door docstrings teach the
  same. But only `run_sfincs` is in the always-visible floor -- the other 8 doors
  surface via retrieval, so the door docstring is NOT in context until AFTER the
  prompt's routing block has already selected it. The two serve DIFFERENT decision
  moments; not redundant. Left intact.

## Decision

Strip the return-envelope enumeration paragraph from all 9 engine-door function
docstrings. Zero information loss: the runtime envelope still returns all six keys
(proven live), and the paragraph two lines up already names what the door returns.
Nothing is hoisted to the system prompt -- the trimmed information already lives at
runtime, so there is nowhere cheaper to move it TO.

Names, params, registry membership, and the door runtime envelopes are byte-identical
-- ONLY the description text shrank.

## Consequence

- Fed-surface delta (Bedrock <=1000-char descriptions):
  - 9 doors: 8608 -> 6794 chars (-1814, ~-454 tok).
  - `run_sfincs` (only door in the always-visible HOT_SET floor): 1005 -> 797 chars
    -- -208 chars off EVERY turn's fixed tool surface.
  - Full 186-tool catalog dump: 184222 -> 182408 chars (-1814, ~-454 tok).
  - System prompt: UNCHANGED (30556 chars) -- no hoist.
- Registry: 186, unchanged. Declarations build 186/186. Daemon imports clean.
- Retrieval (model-free `retrieve_visible_tools(q, None, 8)` over the full 1550-pair
  corpus, before vs after): baseline 163 expected-not-in-top8 misses -> 164. The
  single new miss is `fetch_usace_nsi` (a NON-door tool, never edited) on one query
  ("structure values at risk in this 100-year floodplain") -- a global BM25
  corpus-statistics renormalization tail effect from shortening 9 documents; the
  displacing tool `fetch_fema_nfhl_zones` is itself apt for a 100-year floodplain,
  and `fetch_usace_nsi` still surfaces for its other 7 corpus phrasings. No routing
  signal was cut from any door or from usace_nsi. Judged within-noise; not restored
  (re-inflating 1.8k chars of boilerplate to chase one stochastic boundary slot is a
  net loss).
- Live smoke (offline in-process agent-loop drive, reflecting the edits): natural
  prompts surface `run_sfincs` (flood) and `run_pelicun` (damage); their slimmed
  FunctionDeclarations are valid; the door envelopes STILL carry all six trimmed
  fields; the named templates (`sfincs_flood`, `pelicun_damage_assessment`) are
  callable. Round-trip green.

Supersedes nothing. The as-we-go rule (ADR 0057.2) now also covers this class: a new
engine door inherits the trimmed docstring shape (routing block + mismatch note; NO
return-key enumeration -- the envelope is the contract).
