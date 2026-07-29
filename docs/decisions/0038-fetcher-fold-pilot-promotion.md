# 0038 - fetcher-fold pilot promotion (phase-2 wave 1, the first real cut)

Context: both parity gates PASSED for the 5 pilots -- replication 5/5 across the
full edge matrix (ADR 0037) and the routing experiment SUPPORTED byte-identical
(all 71 records identical across arms, `experiments/fetcher_fold_routing`). Per the
cull doctrine (`docs/specs/data-router-fold.md`) a fetcher dies ONLY after BOTH
gates hold; the pre-registered campaign therefore unlocks PILOT PROMOTION. The
env-gated experiment toggle (`TRID3NT_FETCHER_FOLD_ARM`, ADR 0036 sec 3) was an
experiment scaffold, not a shipping surface.

Decision (NATE, 2026-07-29):

1. The 5 pilot source specs (`fetch_gridmet`, `fetch_hifld_critical_infrastructure`,
   `fetch_noaa_coops_tides`, `fetch_esri_landcover_10m`, `fetch_census_acs`) are
   registered as THE tools UNDER THE TWIN NAMES at `tier="general"` (the default
   retrieval pool) at import time -- `register_specs_from_tree()` called once from
   `agent/tools/__init__.py`, NOT behind any env flag. Adding a source = adding a
   `source.yaml`.

2. The 5 hand-written twin modules are DELETED (`git rm fetch_X.py`). The per-tool
   folders keep exactly `source.yaml` (the spec) + `corpus.yaml` (the ONE retrieval
   corpus source, read by `_compose_corpus_from_tree` keyed by name -- no
   double-count; `spec.corpus` stays empty and is lifted from the sibling only for
   the router-internal fallback) + the empty package `__init__.py`.

3. The env-gated fold-arm substitution machinery RETIRED: the `__spec` alias, the
   `apply_fold_substitution_*` helpers, and the three pool-producer hooks
   (`server._default_declarable_registry`, `tool_retrieval`, `search_tools._build_index`)
   are removed. Promotion is the default; the promoted tools flow through the
   ordinary `tier != "template"` filter with zero special-casing.

4. INDISTINGUISHABILITY preserved at every consumer surface (the promotion invariant):
   - Docstring carried VERBATIM from the twin into `SourceSpec.docstring` (new field,
     dedented via `inspect.getdoc`) -> the sole source of the promoted tool's
     `FunctionDeclaration` description AND the BM25/dense retrieval-index document
     text. Proven: the index token stream is identical to the twin's and all 71
     routing records rank byte-identical to the recorded baseline (zero index shift).
   - Signature SYNTHESIZED from `spec.params` (`registration.promoted_signature`):
     required-first + defaulted + a `**_extra_ignored` absorber, so
     `FunctionDeclaration.from_callable` reproduces the twin inputSchema
     (properties + required) byte-for-byte and the dispatch `tool_arg_normalizer`
     (`inspect.signature` + `get_type_hints`) sees the twin's params.
   - Callable seam = `TOOL_REGISTRY[name].fn` (the router closure). The 3 nested
     consumers (`sfincs_forcing_autowire`, `compute_urban_heat_island`,
     `compute_sediment_yield`) re-point mechanically to registry-name resolution;
     envelope unchanged.
   - Payload gate: a per-spec synthetic `_router._promoted.<name>` module exposes the
     synthesized `estimate_payload_mb`, resolved by the `tool-payload-warning` seam
     exactly as a twin's module-level estimator was.

5. Test migration: the 5 twin test files (146 collected tests) test the twins'
   INTERNAL helpers (`_plan_tile_grid`, `_VARIABLES`, `_fetch_coops_tides_bytes`,
   `_resolve_variable`, ...) which no longer exist -> DELETED. The contract-level
   behavior that survives the fold (each name registered with the twin's
   signature/docstring/typed-errors, in the default pool) is re-expressed in
   `test_router_promotion.py` (20 tests) + the 4 retired fold-arm tests in
   `test_router_engine.py` migrated to promotion assertions. Twin-vs-router value /
   layer / caveat / error-path parity remains covered by the replication harness and
   the router unit suites.

Consequence: the fold's first fetchers are gone as code (~40.4k-line family; first
cut = 3,732 lines of twin fetcher Python + 2,453 lines of twin tests removed, ~725
added -> net ~-5,735 LOC). The registry stays 200 (daemon) with the 5 names
present, now spec-served; retrieval index unshifted; all 6 sources (incl. the
raw-code census request) live-proven with sane envelopes; the coastal
`sfincs_forcing_autowire` canary is status=ok through the promoted CO-OPS surface.
The clean-as-you-go substitution scaffold is retired; phase-2 family fan-out now
promotes directly (spec + `register_specs_from_tree`, twin `git rm`) once a source
clears both gates.
