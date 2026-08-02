# 0072 - Model bench: the arm3 NO_CALL empties are MODEL behavior, not provider artifacts (nemotron hypothesis REFUTED); local 8-9B ollama is not good enough

Context: ADR 0050/0055-era catalog-surfacing arm3 (stratified-pools) recorded 28
NO_CALL empties out of 104 source asks and a NO_ADVANCE verdict on the
nvidia/nemotron-3-super-120b-a12b:free model served via OpenRouter. NATE
hypothesized (2026-08-01) that those empties were UPSTREAM/PROVIDER transport
artifacts -- OpenRouter free-tier queueing, silent 200s, truncated/empty
completions -- rather than genuine model behavior, and asked whether the local
ollama models (qwen3:8b-24k, qwen3.5-lowvram:9b-24k) clear the signed bars and
whether the empties disappear locally. The signed methodology + 123 phrasings are
frozen; a model swap is a new RUN of the same design (deterministic grading:
fired/selected names vs the acceptable set + router.validate_params; upstream
failures graded OUTSIDE the accuracy denominator).

Method: a forensic re-runner (experiments/catalog_surfacing/run_forensic.py, a
results-side COPY of the signed run.py; server code untouched) reuses run.py's
grading verbatim and swaps ONLY the model turn -- a direct OpenAI-compatible
streaming call reusing the adapter's own wire builders (byte-identical request:
same messages incl. SYSTEM_PROMPT + baked tool-discipline line, same tools, temp
0, max_tokens 4096, same model) that additionally captures per call: HTTP status,
finish_reason, content length, reasoning length, tool-call count, retry count,
provider error body. Every NO_CALL is classified {provider_error,
empty_200_completion (silent vs reasoned), reasoning_truncated (finish_reason
length), model_declined}. Scorer: experiments/catalog_surfacing/score_bench.py.
Deliverables under experiments/catalog_surfacing/results/model_bench_2026-08-01/.

Decision (2026-08-01):

1. **NATE's provider-artifact hypothesis is REFUTED for nemotron.** Full arm3
   re-run (104 source asks): 31 NO_CALLs, of which provider_error=0,
   silent_empty=0 (a 200 with ZERO tokens), http-status != 200 = 0, retries = 0.
   Every NO_CALL is model-side: 12 reasoned_empty (the model streamed 1k-15k chars
   of reasoning on `delta.reasoning` then emitted no content and no tool call,
   finish_reason=stop), 11 model_declined (produced prose, chose not to call a
   tool), 8 reasoning_truncated (finish_reason=length -- the 4096 max_tokens cap
   was hit mid-reasoning before a tool call landed). provider_transport=0 vs
   model_config=31. There is NO evidence of OpenRouter queueing / silent 200 /
   429 / 5xx in the forensics.

2. **The signed empties are non-deterministic model behavior, not a stable
   provider signature.** Of the 31 signed arm3 NO_CALL ids, 17 NOW fire a tool on
   the re-run and only 14 are still NO_CALL (grading unchanged). A reasoning model
   at temp 0 through OpenRouter is not run-to-run deterministic; the specific
   empties are sampling variance, not a reproducible transport fault. Selection
   accuracy reproduced within 1 point (re-run 56.73 vs signed 57.69), first-attempt
   and one-retry param validity both 100.0 -- so the signed arm3 measurement itself
   is sound.

3. **The 60.6 arm0 baseline is NOT provider-inflated -- it reproduced HIGHER.** The
   arm0 re-run (104 sources) scored selection 66.35 (vs signed 60.58), first-attempt
   79.71, one-retry 98.55, with 17 NO_CALLs (16 model_declined + 1 reasoned_empty, 0
   provider-transport). So the baseline carries ~+/-6 pt run-to-run noise on a
   non-deterministic reasoning model even at temp 0, and it is if anything
   under-stated, not inflated: NO_CALLs are graded misses that DEPRESS accuracy and
   they are model-side. The one CONFIG lever is max_tokens=4096, too low for a model
   that emits >10k chars of reasoning per turn (it caused the 8 arm3
   reasoning_truncated cases). Raising TRID3NT_OPENAI_MAX_TOKENS for reasoning-model
   runs would recover truncated tool calls -- an infra/config fix orthogonal to the
   surfacing-arm decision; re-measure before crediting.

4. **Local 8-9B ollama models are NOT good enough and the NO_CALL does NOT
   disappear locally (PARTIAL sample).** On this 8GB-VRAM host the 9GB models run
   ~69% CPU-offloaded at ~10 tok/s; a single tool-call turn is minutes, so the
   signed 104-ask arms are intractable -- reported as a STRATIFIED PARTIAL (first
   N phrasings per source, all 14 sources, /no_think per the production
   start_agent.sh local-serving default). Local qwen3 still routes to the reasoning
   channel and produces frequent reasoned_empty NO_CALLs plus wrong-tool selections
   (it even names plausible-but-wrong tools). Measured: qwen3:8b-24k selected the
   correct source in 0 of 6, qwen3.5-lowvram:9b-24k in 1 of 12 -- far below every
   signed bar. The NO_CALL is therefore a general reasoning/small-model behavior,
   present locally too (reasoned_empty) -- which independently corroborates (1): it
   is not OpenRouter-specific.

Consequence: The registry-shrink go/no-go is UNCHANGED by this bench -- arm3's
NO_ADVANCE stands, and it was never a provider artifact to be waved away. The
actionable follow-up is a CONFIG one: raise TRID3NT_OPENAI_MAX_TOKENS for
reasoning-model runs so the 8 truncated tool calls are not cut off (re-measure
before crediting it). Local 8-9B models on 8GB VRAM are neither fast enough nor
accurate enough to serve as the tool-calling driver; they are not a drop-in for
the cloud model on this hardware. The forensic runner + score_bench + the
per-model records are retained as the template for any future model swap.
