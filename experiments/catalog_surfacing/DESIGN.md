# Experiment: catalog surfacing -- the registry-shrink decision (DESIGN, awaiting NATE sign-off)

ASCII only. DESIGN ONLY -- do NOT run until NATE signs
`experiments/catalog_surfacing/inputs/phrasings.json`.

## Question

14 data sources are spec-served: each `fetchers/**/source.yaml` is synthesized
into an individually-registered virtual tool (`_router/registration.py`,
`tier="general"`), so each occupies one slot in the ambient declarable pool that
the model sees every turn. NATE's goal: the registry should SHRINK as generic
surfaces absorb functionality -- spec-served sources should stop being
individually-declared tools and melt into the catalog pattern. This experiment
decides WHICH surfacing design does that WITHOUT degrading how well the model
picks the right source and forms valid arguments.

Both candidate designs preserve FULL per-source context -- the synthesized
signature + the verbatim twin docstring (`spec.docstring`) + the typed param
schema (`spec.params`) + gates/caveats/fallback travel intact either way. The
only thing that moves is WHERE that context is surfaced and WHERE enforcement
lives.

## The three arms

### Arm 0 -- BASELINE (today)
The 14 spec-served sources are ambient, individually-declared `tier="general"`
tools (exactly `register_specs_from_tree()` at import). The model sees each
source's synthesized `FunctionDeclaration` (docstring truncated to Bedrock's
~1000-char tool-description limit; typed inputSchema fully enforced by the
provider). Selection = the model fires the per-source tool directly. Params are
validated twice: provider schema (`promoted_signature` -> `from_callable`
inputSchema) THEN `router.validate_params` at dispatch.

What the model sees per step:
1. Turn opens with the per-source tools already declarable (subject to the
   tool-retrieval visible-set gate). Model emits `function_call fetch_gridmet(bbox=..., variable=..., start_date=..., end_date=...)`.
2. Dispatch runs `router.validate_params`; a typed `*_INPUT` error feeds back as
   a `function_response` (the standing retry norm), model retries.

### Arm 1 -- DESIGN 1 (card-carried; the simplicity target)
Ambient surface for data = `search_data_catalog` + `fetch_from_catalog` ONLY.
The 14 per-source tools are NOT in the ambient declarable pool. A
`search_data_catalog` hit returns the source CARD: `name`, the FULL untruncated
docstring (NOT clipped at the 1000-char provider tool limit), the typed param
schema (from `spec.params`), gates, caveats, fallback. The model then calls
`fetch_from_catalog(source=<name>, params={...})`; OUR pydantic/router layer
(`router.validate_params`) validates and typed errors feed the retry loop.
Enforcement MOVES from provider schema validation to router validation.

What the model sees per step (the honest 2-hop flow):
1. Turn opens; data ambient surface is just the 2 generic tools. Model emits
   `function_call search_data_catalog(topic="fuel moisture fire danger")`.
2. Result = ranked list of source CARDS (full docstring + param schema + gates).
   The card is the model's ONLY view of per-source detail; it is NOT a provider
   `FunctionDeclaration`, so there is no provider-side inputSchema on the fetch.
3. Model emits `function_call fetch_from_catalog(source="fetch_gridmet", params={bbox:..., variable:..., start_date:..., end_date:...})`.
4. `fetch_from_catalog` resolves the spec by `source`, runs
   `router.validate_params(spec, params)`; a typed error feeds back, model
   retries the fetch with corrected `params` (still 1 hop -- the card is already
   in context).

Cost note: Arm 1 is inherently 2-hop (search then fetch) where baseline is
1-hop. The selection metric counts the FETCH hop's `source` arg; the search hop
is a precondition (graded separately as "card surfaced the correct source in
top-k", below).

### Arm 2 -- DESIGN 2 (discovery-expands-declaration)
The 14 sources STAY registered but LEAVE the ambient declarable pool via the
tier mechanism (exactly like engine templates: `tier="template"`-style
exclusion in `_default_declarable_registry` / the tool-retrieval fail-open
pool). A search hit DECLARES the matched source's synthesized per-source tool
FOR THE TURN, reusing the existing gate-expander seam
(`_gate_expander_tool_names` + the door-expand block in `_stream_model_reply`,
server.py ~L4345-4388: a search/door result's `results[].tool_name` /
`templates[].tool_name` are unioned into `_retrieval_registry` +
`state.allowed_tool_set`). POST-DISCOVERY STATE IS IDENTICAL TO TODAY:
the declared per-source tool carries the same provider `FunctionDeclaration`
(full inputSchema), so provider-level schema enforcement is KEPT.

What the model sees per step:
1. Turn opens; the 14 sources are NOT declarable. Model emits
   `function_call search_data_catalog(topic="fuel moisture fire danger")` (or
   `search_tools`; see build prereqs).
2. The search result names the matched source(s); the server expansion declares
   `fetch_gridmet` (its full provider `FunctionDeclaration`) for subsequent
   rounds and adds it to the allowed set.
3. Model emits `function_call fetch_gridmet(bbox=..., variable=...)` -- IDENTICAL
   to baseline from here (provider schema + `router.validate_params`, typed-error
   retry).

## Method (model-in-the-loop execution, deterministic grading)

Unlike the model-free `fetcher_fold_routing` parity run, the HEADLINE metrics
here (selection accuracy, param fidelity, one-retry) require the model to
actually select and form args, so the RUNS are model-in-the-loop through the
production dispatch seam (the same harness the bench uses). Grading stays
deterministic per NATE doctrine: compare fired/selected NAMES to the
catalog-validated acceptable set, and check `validate_params` pass/fail -- never
LLM-judged prose.

- One fixed model + fixed decoding config, PINNED and recorded in
  `results/<arm>_meta.json` (same model+config across all 3 arms -- the arm is
  the only variable). Each arm runs in its OWN process so registry / index /
  env state cannot leak across arms.
- Each of the 123 input records (see `inputs/phrasings.json`) is run once per
  arm as a single-turn task seeded with a canvas AOI (place-name or AOI bbox
  from the phrasing; NO literal bbox coords in the prompt text). Determinism of
  grading does not require determinism of the model; to bound sampling noise the
  harness MAY run N>=1 trials per record at temperature 0 and report the modal
  fired name -- N recorded in meta. (NATE sets N at sign-off; default N=1,
  temperature 0.)
- MODEL-FREE PRECONDITION GATE (reuses the signed `fetcher_fold_routing`
  machinery verbatim): before any model run, for every source phrasing confirm
  the source is REACHABLE by the arm's discovery surface --
  * Arm 1: `search_data_catalog(topic)` returns the correct source card in its
    top-k (card-surfacing recall).
  * Arm 2: `retrieve_ranked_tools(phrasing)` / the search expander ranks the
    correct source name into the expandable top-k (declaration recall).
  * Arm 0: the source is in the ambient visible set for the phrasing.
  A source that FAILS its precondition cannot be selected in that arm; the gate
  result is reported alongside selection so a selection miss is attributable to
  discovery vs the model's pick. This gate is deterministic and model-free.

## Grading (deterministic, catalog-validated)

Per input record, per arm:

**(a) Source-selection accuracy.** The correct source name is selected.
  - Arm 0: the per-source tool fired == `acceptable[0]`.
  - Arm 1: the `source` argument passed to `fetch_from_catalog` == `acceptable[0]`.
  - Arm 2: the per-source tool declared-by-expansion AND fired == `acceptable[0]`.
  Scored as the mean over each source's phrasings and aggregate over all 14.
  Acceptable sets are validated against the LIVE `TOOL_REGISTRY` at load (the
  `fetcher_fold_routing` validation rule).

**(b) Param fidelity.** The formed args validate against the source spec.
  - FIRST-ATTEMPT validity: the model's first tool call args pass
    `router.validate_params(spec, args)` with no error. (Arm 0/2 ALSO clear the
    provider inputSchema by construction; Arm 1 has no provider inputSchema on
    the fetch, so router validation is the sole gate -- this is the design's
    central shift, measured head-on.)
  - ONE-RETRY validity: after a first-attempt typed error is fed back as a
    `function_response`, the SECOND call passes `router.validate_params`. The
    retry norm is part of the contract, so BOTH numbers are reported (a design
    that misses first-attempt but recovers within one retry is materially
    different from one that does not recover).
  Denominators: param fidelity is scored ONLY over records where selection was
  correct (you cannot grade args for a source the model did not pick). Records
  that hit an UPSTREAM_PROVIDER failure (429/5xx/timeout from the model
  provider) are graded UPSTREAM_FAILURE and excluded from both denominators
  (standing upstream-error rule).

**(c) Controls route identically across arms.** The 19 control (non-catalog)
  prompts MUST fire the SAME acceptable tool in ALL THREE arms. Any cross-arm
  divergence on a control = the surfacing change leaked into unrelated routing =
  instrumentation/architecture bug -> the run is INVALID (do not score the
  source metrics), fix and re-run. This is the analogue of the
  `fetcher_fold_routing` byte-identical control check, lifted to the
  model-in-the-loop setting (identity of the FIRED name across arms, not of a
  ranked list).

**(d) Advancement thresholds (pre-registered).** An arm ADVANCES only if ALL:
  - selection accuracy (aggregate) >= baseline arm's selection accuracy, AND
  - first-attempt param validity >= baseline - 5 points, AND
  - one-retry param validity >= baseline one-retry param validity, AND
  - controls pass the (c) identity check.
  TIE-BREAK: if both Design 1 and Design 2 advance and their metrics are within
  the sampling noise band (overlapping at the reported N-trial spread; NATE
  fixes the band at sign-off, default: aggregate selection within 2 points AND
  one-retry validity within 2 points), the SIMPLER design wins -> Design 1
  (fewer moving parts: 2 generic tools, no per-turn declaration wiring), per the
  simplicity norm.
  PER-SOURCE guard (mirrors `fetcher_fold_routing`): any single source whose
  selection accuracy drops >10 points below baseline in the winning arm is
  flagged; 2+ such sources block the rollout until the card/expansion quality
  for those sources is iterated and the arm re-runs.

## What each arm requires BUILT before it can run (scoped honestly)

### Arm 0 -- BASELINE: nothing to build.
Today's tree. `register_specs_from_tree()` at import already gives the 14
ambient per-source tools. The harness just runs the phrasings through the
production dispatch.

### Arm 1 -- DESIGN 1: two builds (both new code).
1. **Card schema in search results.** `search_data_catalog` today searches the
   YAML `CatalogEntry` catalog (`public_data_source_catalog.yaml`) via
   `catalog_common.load_catalog`, NOT the 14 router specs. Build: a card
   projection over the promoted spec registry
   (`registration._SPEC_REGISTRY` / `registered_spec_names()`) returning, per
   matched source: `name`, `spec.docstring` (FULL, untruncated), the typed
   param schema derived from `spec.params` (name/type/required/default/enum
   values/min-max), `spec.gates`, `spec.caveats`, `spec.fallback`. Ranking can
   reuse the existing `_score_entry` heuristic over docstring + `spec.corpus`,
   OR route through the same BM25/dense index `search_tools` uses (preferred --
   keeps ranking parity with baseline). Scope: MEDIUM -- one new card dataclass
   + a search path over the spec registry; no new external I/O.
2. **`fetch_from_catalog` source passthrough.** Add a `source` param (the spec
   name) branch: resolve the `SourceSpec` from `_SPEC_REGISTRY`, then
   `router.route(spec, params)` (which internally runs `validate_params` ->
   typed `router_input_error` on bad args, exactly the retry contract). Today
   `fetch_from_catalog(entry_id, params)` only dispatches YAML entries by
   `entry_id` through the generic OGC/HTTPS ladder; the new branch is additive
   (keep `entry_id` for the YAML catalog). Scope: SMALL -- a resolve-and-route
   branch; the router + `validate_params` already exist.
3. **Pool exclusion of the 14 (experiment-only for the arm).** For the arm the
   14 per-source tools must NOT be ambient-declarable. Reuse the tier-exclusion
   mechanism (mark them pool-excluded) OR, since this is a design arm, gate
   their registration behind the arm flag. Scope: SMALL. (If Design 1 wins,
   rollout DELETES the per-source registration entirely -- see end-state.)

### Arm 2 -- DESIGN 2: one build, more wiring.
1. **Tier exclusion.** Mark the 14 specs' registration so they leave the
   ambient declarable pool -- the engine-template pattern
   (`_default_declarable_registry` already drops `tier="template"`; the
   tool-retrieval fail-open pool already drops `tier="template"`). One-line
   metadata change in `registration.register_spec` (a tier value) IF the
   exclusion semantics match; BUT note the divergence from templates: engine
   templates are ALSO pulled from the SEARCH index (surfaced only by their
   door), whereas Design 2 needs the 14 to STAY searchable so the search hit can
   find + expand them. So the correct build is a NEW tier (e.g. `"catalog"`)
   that is (i) EXCLUDED from the default declarable pool, (ii) INCLUDED in the
   search/retrieval index, (iii) recognized by the gate-expander so a search
   result naming it declares it for the turn. Scope: MEDIUM -- new tier
   semantics threaded through `_default_declarable_registry`, the tool-retrieval
   pool filter (include-in-index / exclude-from-default), and the search tool's
   result shape.
2. **Expansion seam wiring.** A `search_data_catalog` (or `search_tools`) hit
   must return the matched source name(s) in a `results[].tool_name` shape so
   the EXISTING door-expand block (server.py ~L4355) declares them -- OR route
   catalog discovery through `search_tools`, which already emits
   `results[].tool_name` and is already a registered gate-expander. If
   `search_data_catalog` is the expander, add it to `_gate_expander_tool_names`
   and have it emit `results[].tool_name`. Scope: SMALL-MEDIUM on top of (1).
   Provider `FunctionDeclaration` (full inputSchema) is unchanged -- that is the
   arm's point.

## Registry / declaration delta per arm, and the end-state

Measurement (deterministic, at load; the harness records the integers in
`results/<arm>_meta.json`):
- AMBIENT DECLARABLE COUNT = `len(_default_declarable_registry())` -- the
  full `TOOL_REGISTRY` minus `tier="template"` (and, in the design arms, minus
  the tier-excluded catalog sources). This is the per-turn provider tool-list
  size floor before any retrieval/expansion.
- PER-TURN DECLARED COUNT = the size of the visible set the model actually sees
  for a phrasing (ambient floor intersected with the tool-retrieval visible
  gate, plus any expansion).

| Arm | 14 sources ambient? | Ambient declarable delta vs baseline | Per-turn declared for a catalog phrasing |
|-----|--------------------|--------------------------------------|------------------------------------------|
| 0 baseline | yes (tier=general) | 0 (reference) | ambient floor incl. all 14 (subject to retrieval gate) |
| 1 Design 1 | no (only 2 generic tools) | -14 (the 14 leave; the 2 generic tools already existed) | ambient floor - 14 + the card list is a RESULT payload, not a declaration |
| 2 Design 2 | no (tier=catalog, searchable) | -14 | ambient floor - 14, + 1-3 sources DECLARED on the search hit for the turn |

Reference: the signed `fetcher_fold_routing` file recorded
`registry_size_at_check=200`; the exact ambient-declarable integer for each arm
is printed by the harness at run (do not hardcode). Both designs deliver the
SAME headline shrink today: -14 ambient declarations. They differ in what
replaces per-source declaration -- a RESULT-payload card (Design 1) vs an
on-demand DECLARATION (Design 2, provider schema retained).

END-STATE if the winning arm rolls out to EVERY future fold wave:
- Design 1: the ambient data surface is PERMANENTLY 2 tools
  (`search_data_catalog` + `fetch_from_catalog`), independent of how many
  sources exist. Adding a source = adding a `source.yaml`; zero registry / zero
  ambient-declaration cost; enforcement is uniformly router-side. Registry stops
  growing per-source at the DECLARATION surface entirely -- O(1) ambient data
  footprint. `register_spec`'s per-source tool registration is DELETED; the spec
  registry feeds the card projection only.
- Design 2: the ambient data surface stays O(1) too (sources are tier-excluded),
  but each source remains a registered per-source tool declared on demand, so
  the registry itself does NOT shrink -- only the AMBIENT pool does. Provider
  schema enforcement is preserved for every source. Every future fold wave adds
  a `source.yaml` -> a `tier=catalog` registration; ambient cost stays flat,
  registry count keeps growing (one entry per source), discovery declares the
  matched few per turn.
- Simplicity ledger for the tie-break: Design 1 removes per-source
  registration + provider-schema plumbing (fewer entities, single enforcement
  locus); Design 2 keeps them and adds tier + expansion wiring. If metrics tie,
  Design 1 is the standing winner.

## Deliverables

`experiments/catalog_surfacing/results/` -- per-arm run records (fired names,
formed args, first-attempt + one-retry `validate_params` outcomes, precondition
gate results), the control identity check, the scored comparison table, and
`VERDICT.md` (ADVANCE Design 1 / ADVANCE Design 2 / NEITHER, with the per-source
table + the registry-delta integers). The verdict paper-trails the
registry-shrink go/no-go and which surfacing design every future fold wave
adopts.
