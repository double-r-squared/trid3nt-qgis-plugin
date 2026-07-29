# Experiment: fetcher-fold routing parity (DESIGN - awaiting NATE sign-off)

## Hypothesis

Catalog-spec-routed data sources are retrieved at least as well as their
hand-written fetcher tools for the same user phrasings - i.e. moving a
source's discoverability surface from (registered tool + corpus.yaml) to
(router spec + the same corpus phrasings) does NOT degrade routing. If this
holds on the pilot set, the fold's central risk (NATE's original
hesitation) is disproven on paper before any fetcher is cut.

## Method (model-free, deterministic - no LLM anywhere)

1. BASELINE ARM: current tree. For each input phrasing, run the production
   retrieval path (retrieve_ranked_tools / retrieve_visible_tools, exactly
   as the retrieval_probe bench does) and record the ranked list.
2. FOLD ARM: the pilot branch state - same phrasings, the 5 pilot sources
   served by router specs (their hand-written twins deregistered from the
   default pool for the arm, twins' corpus carried into the specs verbatim).
3. GRADING (deterministic, catalog-validated): per phrasing an ACCEPTABLE
   SET of expected surface names (the source's router-surface name and/or
   the consumption pair), validated against the live registry at load -
   exactly the routing-sweep grading rules. Metrics: hit@5, hit@8, top1,
   nDCG@5, MRR@5 - computed per arm by the same scorer as retrieval_probe.
4. REPETITION: retrieval is deterministic per tree state, so variance comes
   from the INPUT SAMPLE, not reruns - each pilot source gets >=8 phrasings
   (its existing corpus phrasings verbatim + paraphrases + 2 vague-class),
   and the NON-pilot control set (12 canonical phrasings for untouched
   tools) must be IDENTICAL across arms (any control drift = instrumentation
   bug, run invalid).

## Pass criteria (pre-registered)

- Per pilot source: fold-arm hit@8 >= baseline hit@8, and nDCG@5 within
  0.05 of baseline (or better).
- Aggregate: fold-arm MRR@5 >= baseline - 0.03.
- Controls: byte-identical rankings across arms.
- ANY pilot source failing -> that source's fold blocks; 2+ failing ->
  the architecture iterates before any migration (spec-doc quality, corpus
  carriage, or surfacing mechanism) and the experiment re-runs.

## Inputs (constructed AFTER the phase-1 audit picks pilots)

experiments/fetcher_fold_routing/inputs/phrasings.json - per pilot source:
{source, acceptable:[names], phrasings:[...]} + the control block. Assembled
from the pilot picks + their live corpus files; PRESENTED TO NATE with the
pilot classification for sign-off BEFORE any run (standing methodology
rule). Resource profile: model-free, in-process, no API calls - cheap; one
run per arm.

## Deliverable

experiments/fetcher_fold_routing/results/ - per-arm ranked lists (JSONL),
scored comparison table, VERDICT.md (SUPPORTED / REFUTED / MIXED with the
per-source table). The verdict paper-trails the go/no-go for phase 2.
