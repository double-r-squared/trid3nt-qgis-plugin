# Stratified retrieval pools (NATE-APPROVED + SIGNED, 2026-07-30)

Principle (NATE): pool-ify the UNBOUNDED populations; bounded surfaces
with working mechanisms stay as they are. "The registry is a library;
the declared set is a per-turn portfolio assembled by stratified
retrieval."

## Scope

| Population | Size | Mechanism |
|---|---|---|
| Data sources (spec-served fetchers) | unbounded (grows per fold) | POOL -> composed generic-fetcher declaration |
| DuckDB spatial functions | 285, unbounded upstream | POOL -> context cards riding spatial_query |
| QGIS Processing algorithms | large | POOL -> context cards riding qgis_process |
| Engine templates | small (~20), bounded | EXCLUDED - doors stay (NATE 2026-07-30: "templates have a smaller number, and if our existing implementation works we don't need to change it up") |

## Mechanics

1. PARALLEL PER-STRATUM RETRIEVAL: the same BM25 + dense + name/RRF
   machinery, run over separate indexes (core tools vs each pool).
   No fused leaderboard across strata.
2. QUOTA-MERGED DECLARED SET: core tools fill their reserved share of
   the per-turn declared slots; each active pool fills its own small
   quota. An unbounded pool can never crowd the core surface, and
   vice versa.
3. DECLARATION VS CONTEXT delivery per pool: data sources surface as
   ONE generic fetcher (source enum in schema + full source cards in
   context - enums escape the Bedrock 1000-char docstring limit);
   spatial functions and QGIS algorithms surface as CONTEXT CARDS
   accompanying their executor tool (they are vocabulary, not tools).
4. TRIGGERED, NEVER SURFACED: discovery is harness-side. The pool
   pass runs every turn (cheap, same index machinery); an escalation
   pass (lower threshold, dense-heavy) fires automatically when the
   top pool score is weak on a pool-shaped ask. Threshold, not an
   intent classifier. The explicit search tools (search_data_catalog,
   search_spatial_functions, list_qgis_algorithms) demote to internal
   trigger implementations / browse fallbacks.

## Rationale (evidence-backed)

1. The catalog-surfacing experiment (ADR 0049): the model will not
   INITIATE discovery (0-1 pct across 208 arm drives) but harness
   retrieval finds the right source at 0.99 model-free. Discovery
   belongs to the harness.
2. Noise scaling: separate pools + quotas let source/function/algo
   populations grow without ever widening the model's menu.
3. Redundancy-intersect (NATE): composite tools consume fetchers
   internally (model_debris_flow -> mtbs; exposure_summary ->
   population/buildings); in a fused surface they compete with their
   own ingredients for slots. Lanes make composite-vs-raw a
   structural choice, not a ranking accident. Internal consumption
   already rides the registry-callable seam and needs no declaration.

## Rollout phases (each identity-gated, flag-first)

1. DATA POOL - experiment Design 3 (amended DESIGN.md, needs NATE
   sign-off) proves the composed-declaration one-shot; then rollout.
2. SPATIAL-FUNCTION POOL - context-card injection for spatial_query.
3. QGIS-ALGORITHM POOL - same pattern for qgis_process.
Engine doors/templates: no change at any phase.
