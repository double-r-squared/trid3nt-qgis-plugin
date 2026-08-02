# Model bench 2026-08-01 -- catalog-surfacing arms on nemotron + local ollama

Deterministic grading identical to score.py (selection = selected_name vs acceptable[0] over non-upstream source records; param validity over selection-correct records). N=1, temp 0, max_tokens 4096. Forensic re-run: per-call HTTP status / finish_reason / content length / reasoning length captured. NO_CALLs classified.

Signed arm0 nemotron baseline (prior, ADR 0050/0055): selection 60.58.

## Headline metrics

| Model | Arm | Sel acc | First-attempt | One-retry | Graded | Sel-correct | NO_CALL | NO_CALL rate | Wrong-src | Upstream excl |
|-------|-----|---------|---------------|-----------|--------|-------------|---------|--------------|-----------|---------------|
| nemotron | 0 | 66.35 | 79.71 | 98.55 | 104 | 69 | 17 | 16.35 | 18 | 0 |
| nemotron | 3 | 56.73 | 100.0 | 100.0 | 104 | 59 | 31 | 29.81 | 14 | 0 |
| qwen3-8b | 3 | 0.0 | 0.0 | 0.0 | 6 | 0 | 3 | 50.0 | 3 | 0 |
| qwen35-lowvram-9b | 3 | 8.33 | 0.0 | 0.0 | 12 | 1 | 3 | 21.43 | 8 | 2 |

## NO_CALL forensic classification

provider-transport artifacts = provider_error + silent_empty (200 w/ ZERO tokens). model/config = model_declined + reasoned_empty (reasoned then emitted nothing) + reasoning_truncated (max_tokens hit). Hypothesis CONFIRMED only if provider-transport is the MAJORITY.

### nemotron arm0 -- 17 NO_CALL(s)
- classes: {"empty_200_completion": 1, "model_declined": 16} (silent_empty=0, reasoned_empty=1)
- provider_transport=0 vs model_config=17 -> hypothesis REFUTED

### nemotron arm3 -- 31 NO_CALL(s)
- classes: {"empty_200_completion": 12, "model_declined": 11, "reasoning_truncated": 8} (silent_empty=0, reasoned_empty=12)
- provider_transport=0 vs model_config=31 -> hypothesis REFUTED

### qwen3-8b arm3 -- 3 NO_CALL(s)
- classes: {"empty_200_completion": 2, "model_declined": 1} (silent_empty=0, reasoned_empty=2)
- provider_transport=0 vs model_config=3 -> hypothesis REFUTED

### qwen35-lowvram-9b arm3 -- 3 NO_CALL(s)
- classes: {"model_declined": 2, "provider_error": 1} (silent_empty=0, reasoned_empty=0)
- provider_transport=1 vs model_config=2 -> hypothesis REFUTED

## Reproducibility of the SIGNED run's arm3 NO_CALL empties (nemotron re-run)

- signed arm3 NO_CALL ids: 31
- STILL NO_CALL on re-run: 14
- NOW fired a tool: 17
- other outcome: 0 | not in re-run: 0
- sample flips (id, outcome, fired): [('control#0', 'SELECTED', 'fetch_nws_event'), ('control#1', 'SELECTED', 'fetch_fema_nfhl_zones'), ('control#4', 'SELECTED', 'fetch_noaa_slr_scenarios'), ('fetch_cdc_svi#3', 'WRONG_SOURCE', 'fetch_openfema_disasters'), ('fetch_esri_landcover_10m#3', 'SELECTED', 'fetch_esri_landcover_10m'), ('fetch_gridmet#0', 'WRONG_SOURCE', 'code_exec_request'), ('fetch_gridmet#8', 'SELECTED', 'fetch_gridmet'), ('fetch_hifld_critical_infrastructure#11', 'WRONG_SOURCE', 'fetch_epa_frs_facilities'), ('fetch_hifld_critical_infrastructure#5', 'WRONG_SOURCE', 'geocode_location'), ('fetch_hifld_transmission_lines#0', 'SELECTED', 'fetch_hifld_transmission_lines'), ('fetch_nhdplus_nldi_navigate#0', 'WRONG_SOURCE', 'fetch_usgs_nwis_gauges'), ('fetch_nhdplus_nldi_navigate#3', 'SELECTED', 'fetch_nhdplus_nldi_navigate')]


## Sampling + run notes

- nemotron (nvidia/nemotron-3-super-120b-a12b:free via OpenRouter): FULL arms. arm3
  = 104 sources + 19 controls (123). arm0 = 104 sources complete + 11 of 19 controls
  (the last controls hit OpenRouter free-tier throttling and were stopped; ALL source
  metrics are over the complete 104 and are unaffected). temp 0, max_tokens 4096,
  EXTRA_SYSTEM unset (signed-equivalent env).
- qwen3-8b (qwen3:8b-24k) + qwen35-lowvram-9b (qwen3.5-lowvram:9b-24k): PARTIAL,
  STRATIFIED sample (first 1 phrasing per source, spans all 14 sources) -- the signed
  "first 40" prefix is intractable on this 8GB-VRAM host (9GB models run ~69-82%
  CPU-offloaded, minutes/call; a full 104-ask arm would take many hours) AND is
  dominated by the earliest source groups. Production local-serving config
  (/no_think per start_agent.sh default), temp 0, max_tokens 4096. qwen3-8b got 6
  records before its ollama request hung (keep-alive eviction under the long
  hard-source reasoning) and was stopped; qwen35 got the full 16 (2 early records
  are UPSTREAM_FAILURE from a model-load contention while qwen3:8b was still resident
  -- graded OUTSIDE the denominator per the standing rule).

## Bars cleared (signed criteria)

- Signed arm3 PASS bar = selection >= arm0 baseline AND first-attempt >= arm0 - 5 AND
  one-retry >= arm0. This run's arm0 baseline reproduced at 66.35 (HIGHER than the
  signed 60.58 -- the reasoning model is not run-to-run deterministic even at temp 0;
  the baseline carries ~+/-6 pt noise).
- nemotron arm3 selection 56.73 < arm0 66.35 -> does NOT clear (NO_ADVANCE stands).
- qwen3-8b (0.0) and qwen35-lowvram-9b (8.33) selection are far below every bar ->
  do NOT clear. First-attempt/one-retry are 0 because they almost never select the
  correct source (param validity is only graded over selection-correct records).
- No model+arm other than the arm0 baseline itself clears the selection bar.

## Recommendation

1. Registry-shrink flip: NOT unblocked. arm3 NO_ADVANCE holds and was never a
   provider artifact -- the empties are the reasoning model declining/emptying, not
   OpenRouter transport failures (0 provider-transport artifacts across ~250 nemotron
   asks; 0 http != 200; 0 retries needed on the graded set).
2. The 60.6 baseline was NOT provider-inflated. The re-run scored HIGHER (66.35), and
   NO_CALLs are graded misses that DEPRESS accuracy. The one real lever is CONFIG:
   raise TRID3NT_OPENAI_MAX_TOKENS for reasoning-model runs so the 8 arm3
   reasoning_truncated tool calls (finish_reason=length at 4096) are not cut off --
   re-measure before crediting.
3. Local 8-9B ollama on 8GB VRAM: NOT good enough (0/6 and 1/12 correct) and NOT
   faster -- do not treat as a drop-in tool-calling driver on this hardware. The
   NO_CALL does NOT disappear locally (present as reasoned_empty), independently
   confirming it is a general reasoning/small-model behavior, not OpenRouter-specific.
