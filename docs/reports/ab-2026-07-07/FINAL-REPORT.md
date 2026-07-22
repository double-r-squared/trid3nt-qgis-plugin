# Local LLM A/B + usability report - 2026-07-07/08

## Question

Can we improve the local model's tool routing via (a) a lessons
self-improvement loop, (b) a bigger quantized model - and what coverage does
a user actually experience?

## Instrument

- Routing sweep: one docstring-derived cold prompt per registered tool,
  driven through the live agent WS; HIT = the expected tool was called.
  Harshest possible cut: no conversation context, exact-tool target.
- Failure splitter: replays every failed prompt through the tool-retrieval
  layer (K=8) - attributes each miss to RETRIEVAL (tool not visible) vs
  MODEL (visible but not chosen).
- Usability sweep: 2-turn protocol; turn 1 = cold prompt, turn 2 = directed
  "use the <tool> tool" follow-up in the same case. USABLE = reached in
  <= 2 turns. This approximates real use (the tool catalog is user-visible).

## Results (apples-to-apples on the 174 shared tools)

| cell                          | cold HIT rate |
|-------------------------------|---------------|
| qwen3:8b-16k, lessons dark    | 45/174 (25.9%) |
| qwen3.5-lowvram:9b-24k + lessons | 39/174 (22.4%) |
| qwen3:8b-16k + lessons (gated)   | 41/174 (23.6%) |

- Retrieval misses across ALL cells: ZERO. Every failure is a model miss.
- Flip analysis (dark vs lessons-gated): 54 tools flipped HIT<->MISS
  (29 lost, 25 gained). Run-to-run variance (~31% flip rate) dwarfs every
  treatment effect (~2pp). NO treatment differs from noise.

### Usability (qwen3:8b-16k + gated lessons)

| outcome    | count |
|------------|-------|
| USABLE turn 1 | 102 |
| USABLE turn 2 | 55  |
| UNUSABLE      | 22  |
| ERROR         | 1   |

**Usable coverage: 157/180 (87%).** Cold routing ~24-26%; with one
clarifying turn the local 8B reaches 87% of the catalog.

The 22 unusable tools cluster into: layer-transform tools the model
misroutes to siblings even when named (compute_hillshade, compute_ndvi,
change detection, UHI, IDF), niche fetchers it paraphrases into the wrong
sibling (copernicus_dem -> fetch_dem, sentinel2_truecolor -> landsat), and
key-gated tools whose credential error the model treats as failure
(airnow, era5). List in usability-8b-final.jsonl.

## Verdicts

1. **The model is the binding constraint.** Zero retrieval misses in every
   cell; K-tuning and RAG work are done. A materially better local model
   needs more VRAM than this 8GB box offers (the 9B quant fit at 24k ctx,
   100% GPU, but routed WORSE than the 8B).
2. **Lessons loop: neutral on cold routing** (within noise), retained ON:
   the relevance gates prevent harm, and its real product value is
   user-thumbs-down corrections in live use, which this benchmark cannot
   measure.
3. **Usable coverage 87% is the honest user-facing number.** Cold-prompt
   accuracy understates the product because real use is interactive.
4. **Config going forward: qwen3:8b-16k + TRID3NT_LESSONS=on (gated).**

## Incidents worth remembering

- qwen3.5's chat template renders ~1.2k tokens larger than qwen3's; at
  num_ctx 16384 Ollama silently truncated every agent prompt at exactly
  16384 and the tool schemas fell off the end -> 0/10 NO_CALL. Silent
  context truncation is the #1 silent killer (second occurrence in this
  project). Fixed with a 24k variant (7.2GB, still 100% GPU).
- The first "lessons-on" sweep was accidentally DARK: the flag was armed in
  .env.local but the serving agent predated it. Verify treatment env in the
  serving process, not the config file.
- The original lessons injection (raw-BM25 floor 0.1) injected top-2 on
  ~every turn, mostly location-boilerplate matches - plausibly harmful for
  a small model. Fixed: distinctive-token overlap gate + relative floor
  (GRACE-2 a1ce516).

## Artifacts

All in docs/reports/ab-2026-07-07/: baseline-dark-qwen3-8b.jsonl,
8b-lessons-gated.jsonl (+split), model-swap-9b-lessons.jsonl (+split),
usability-8b-final.jsonl, tool-usability-report.md, sweep logs.
