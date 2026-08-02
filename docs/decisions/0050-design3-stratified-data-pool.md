# 0050 - Design 3: stratified data pool (auto-trigger composed declaration) - NO_ADVANCE

Context: ADR 0049 left the registry-shrink UNDECIDED. Designs 1 (card-carried) and
2 (discovery-expands-declaration) both collapsed on selection (0.0% / 0.96% vs 60.6%
baseline) for ONE empirical reason: the stack's weak default free model
(`nvidia/nemotron-3-super-120b:free`) almost never INITIATES a discovery hop when
semantically-adjacent ambient sibling tools are declared. Design 3
(`docs/specs/stratified-pools.md`, SIGNED NATE 2026-07-30, incl. the controls-gate
amendment) removes that dependency: discovery is HARNESS-SIDE. Every turn a
source-stratum retrieval pass (a separate index scoped to ONLY the 14 spec-served
sources) selects matching sources; when the top source clears an absolute
relevance gate the harness DECLARES one composed generic fetcher whose `source`
enum is the matched candidates in rank order, with full source cards riding in
context. The model never searches; it just picks a `source` from the narrowed enum
and forms `params`. Router validation + one-retry are graded exactly as arms 0-2.

Decision:
- BUILD the Design 3 lane, identity-gated behind the reversible `TRID3NT_CATALOG_ARM=3`
  flag (default off). DEFAULT config is byte-identical (registry 190, ambient
  declarable 170, `fetch_from_catalog` signature + docstring unchanged; offline suite
  == the 9-failure baseline). Mechanisms:
  * `registration.catalog_arm()` recognizes "3"; the 14 specs register `tier="catalog"`
    (pool-excluded, still indexed) as in arms 1/2.
  * NEW `fetchers/_router/stratified.py`: a source-only retrieval index
    (`_build_index` over the 14-source snapshot -- per-pool BM25 IDF sharpening is
    expected), the same BM25+dense+name/RRF ranker; an ABSOLUTE dense-cosine
    activation gate (rank-normalized RRF saturates in a 14-doc pool and cannot say
    whether the pool is relevant); a threshold + dense-heavy escalation pass; the
    composed `FunctionDeclaration` (source enum in rank order + free-form params);
    and the plaintext cards-context renderer.
  * `fetch_from_catalog` grows its `source`-passthrough branch under arm 3 (like arm 1)
    -> `_fetch_from_catalog_via_spec` -> `router.route` (router validation as the sole gate).
  * NO model-facing search tool is declared under arm 3 (`search_data_catalog` /
    `search_tools` are not in the arm-3 surface).
- RUN model-in-the-loop through the production dispatch seam (stack default adapter,
  temp 0, N=1), deterministic grading; model-free reachability precondition first.
  Amended controls gate: a control divergence invalidates ONLY on catalog/data-surface
  LEAKAGE; NO_CALL / unrelated-tool jitter is re-run N=3 with majority grading and
  reported, not invalidating.
- VERDICT: **NO_ADVANCE.** Arm 3 selection 57.69% < arm0 60.58% (the pre-registered
  selection bar), so the registry-shrink is NOT rolled out on this run. Keep the arm-3
  mechanisms as reversible, default-off scaffolding for a capable-model re-run.

Consequence:
- The MECHANISM is sound and, on its own axes, BEATS baseline:
  * Model-free reachability 1.0 (104/104) -- the sharpened source stratum finds the
    correct source every time (> the 0.9904 full-index recall).
  * First-attempt param validity 80.95% -> **100.0%** and one-retry 100% -> the enum +
    full cards make the model form valid `params` EVERY time it selects a source. This
    clears the first-attempt bar (>= 75.95) decisively -- the design's central
    router-side-validation shift is a WIN, not a risk.
  * Controls: **zero leakage** (16/19 identical to arm0; the 3 divergences are all
    NO_CALL empties). N=3 majority resolved each as non-leakage jitter: control#0 ->
    None (empty), control#1 -> fetch_fema_nfhl_zones (correct core tool), control#4 ->
    geocode_location (non-surface). Surface stability held: the stratified declaration
    did NOT corrupt unrelated routing. (Honest residual: 1 of control#4's 3 trials fired
    fetch_gridmet -- a lone data-surface distraction on a flood-model ask, minority,
    reported.)
- The ONLY thing holding selection below baseline is the SAME weak-free-model artifact
  ADR 0049 named: 28/104 source asks returned an EMPTY completion (no tool call, no
  text) = NO_CALL, each graded a selection miss. Beat-baseline hypothesis: FALSE on this
  model (57.69 <= 60.58) -- but the gap is entirely NO_CALL noise, not wrong picks: when
  the model calls, it picks the harness-narrowed source and validates params at 100%.
- Rollout implication: the registry-shrink go/no-go stays UNDECIDED pending a capable
  model. Design 3 is the strongest candidate -- it is the only arm that decouples
  selection from the model's willingness to initiate discovery, it preserves surface
  stability (NATE's governing principle), and it delivers the -14 ambient shrink with a
  PERMANENT O(1) data surface (one composed fetcher regardless of source count). Re-run
  arm 3 on a capable adapter before deciding; if selection clears 60.58 with NO_CALL
  noise removed, roll out the stratified data pool (phase 1 of docs/specs/stratified-pools.md)
  and delete per-source registration. Supersede this ADR with that re-run's verdict; do
  not rewrite it.
