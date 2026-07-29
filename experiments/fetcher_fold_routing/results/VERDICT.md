# VERDICT -- fetcher-fold routing parity

**SUPPORTED**

Model-free, deterministic (no LLM). Production retrieval seam: `search_tools._get_index` warm + `tool_retrieval.retrieve_ranked_tools` rank, scored by the retrieval_probe `ndcg_at_k`/`mrr_at_k` + hit@k membership semantics. One run per arm; retrieval proven deterministic across processes (baseline rep1 vs rep2 byte-identical, all 71 records).

## Control identity check (FIRST-CLASS, DESIGN sec 4)

- Controls: 12 untouched-tool phrasings.
- Byte-identical rankings across arms (ordered (name, score)): **True**.
- No instrumentation drift -> scoring is VALID.

## Per-pilot-source results

Criteria per source: fold hit@8 >= baseline hit@8 AND fold nDCG@5 >= baseline nDCG@5 - 0.05.

| source | n | hit@8 base->fold | hit@5 base->fold | top1 base->fold | nDCG@5 base->fold (delta) | MRR@5 base->fold | byte-ident | PASS |
|---|---|---|---|---|---|---|---|---|
| fetch_gridmet | 12 | 9->9 | 9->9 | 5->5 | 0.6103->0.6103 (+0.0) | 0.5625->0.5625 | True | PASS |
| fetch_hifld_critical_infrastructure | 12 | 12->12 | 12->12 | 9->9 | 0.8968->0.8968 (+0.0) | 0.8611->0.8611 | True | PASS |
| fetch_noaa_coops_tides | 11 | 11->11 | 11->11 | 11->11 | 1.0->1.0 (+0.0) | 1.0->1.0 | True | PASS |
| fetch_esri_landcover_10m | 12 | 12->12 | 12->12 | 10->10 | 0.9109->0.9109 (+0.0) | 0.8819->0.8819 | True | PASS |
| fetch_census_acs | 12 | 10->10 | 10->10 | 9->9 | 0.8026->0.8026 (+0.0) | 0.7917->0.7917 | True | PASS |

## Aggregate (pilot phrasings; controls excluded as the identity set)

- n = 59 pilot phrasings.
- hit@8: 54/59 -> 54/59
- hit@5: 54/59 -> 54/59
- top1: 44/59 -> 44/59
- nDCG@5 mean: 0.8415 -> 0.8415
- MRR@5 mean: 0.8164 -> 0.8164 (delta +0.0)
- Criterion (fold MRR@5 >= baseline - 0.03): 0.8164 >= 0.7864 -> **True**

All 71 records (informational): MRR@5 0.838 -> 0.838; nDCG@5 0.8612 -> 0.8612; hit@8 66/71 -> 66/71.

## Decision

All 5 pilot sources meet the per-source criteria, the aggregate MRR@5 criterion holds, and controls are byte-identical. The fold's central routing risk is disproven on the pilot set -> phase-2 migration is unblocked on routing grounds (paper trail only; no fetcher is cut by this experiment).
