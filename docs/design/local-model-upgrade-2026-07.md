# Local model upgrade research -- better tool-calling brains for TRID3NT Local (2026-07)

Status: research only (nothing pulled, nothing benchmarked). Follow-up to the pass-3 routing
sweep verdict: all 127 scored failures were MODEL-MISS at K=8 -- the retrieval layer put the
right tool on the 8-tool menu every single time and `qwen3:8b-16k` picked wrong or declined
(`docs/reports/tool-routing-failure-split.md`). The lever is model quality at three things:
choosing from a short menu, argument fidelity, and multi-step chaining (geocode-then-fetch
chains stall today). This doc ranks the candidates that can actually run on this box.

## 1. Hardware truth (measured on this box, 2026-07-07)

- GPU: NVIDIA GeForce RTX 2060 SUPER, **8192 MiB** VRAM (`nvidia-smi --query-gpu=name,memory.total`).
- System RAM: **15 GiB total, ~10 GiB available** (`free -g`; 5-6 GiB in use, 15 GiB swap).
  NOT the 64 GB sometimes assumed -- partial-CPU-offload headroom is ~8-9 GiB, which rules
  out "just spill a 14B to RAM" as a comfortable plan.
- Serving: Ollama, OpenAI-compatible endpoint (`TRID3NT_OPENAI_BASE_URL=http://127.0.0.1:11434/v1`),
  current model `qwen3:8b-16k` (local `num_ctx 16384` variant of `qwen3:8b`, 5.2 GB Q4),
  `TRID3NT_OPENAI_EXTRA_SYSTEM=/no_think`, `TRID3NT_TOOL_RETRIEVAL=enforce`, K=8 over 178 tools.

### KV-cache math (fp16 K+V; bytes/token = 2 x layers x kv_heads x head_dim x 2)

| Model | KV geometry | KV/token | KV @16k | KV @32k | Q4 weights | 16k total fits 8 GB? |
|---|---|---|---|---|---|---|
| Qwen3-8B | 36L x 8KV x 128 | 144 KiB | 2.25 GiB | 4.5 GiB | 5.2 GB | yes, barely (proven live) |
| Qwen3-4B | 36L x 8KV x 128 | 144 KiB | 2.25 GiB | 4.5 GiB | 2.6 GB | yes, 32k also plausible |
| Llama-3.1-8B (watt-tool/ToolACE base) | 32L x 8KV x 128 | 128 KiB | 2.0 GiB | 4.0 GiB | 4.9 GB | yes, tight |
| Qwen3-14B | 40L x 8KV x 128 | 160 KiB | 2.5 GiB | 5.0 GiB | ~9.3 GB | **no** -- weights alone exceed VRAM |
| Qwen3.5-9B (default tag) | unpublished (multimodal) | ~144 KiB est. | ~2.2 GiB est. | ~4.5 GiB est. | 6.6 GB | **no** (measured: spills, crawls) |
| Qwen3.5-9B lowvram variant | same est. | ~144 KiB est. | ~2.2 GiB est. | -- | 4.8 GB | plausibly yes (the experiment) |
| Qwen3.5-4B | unpublished | assume Qwen3-4B class | ~2.25 GiB | ~4.5 GiB | 3.4 GB | yes, with headroom |
| Granite 4.1 8B | unverified | unverified | -- | -- | 5.3 GB | probably (qwen3:8b class) |

Two levers stretch any of these: `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`
halves the KV figures above with negligible quality loss (Turing supports flash attention in
llama.cpp), so e.g. Qwen3-8B @16k drops from 2.25 GiB to ~1.1 GiB of cache. That is the
cheapest VRAM we are not currently spending.

Context past 16k: only the small-weights candidates (Qwen3.5-4B, Qwen3-4B class, Granite 4.1
3B) can afford 24-32k fully resident; for the 5+ GB-weight models, 16k with q8_0 KV is the
practical ceiling. Community guidance for the 8 GB tier agrees: keep context modest, a 32k
window on a 7-8B Q4 model overflows the card (localllm.in VRAM guide).

The measured cost of getting this wrong: 8B Q4 at 16k ran 40.6 tok/s fully resident vs
8.6 tok/s with partial offload (localllm.in) -- a ~5x cliff, and exactly why `qwen3.5:9b`
(6.6 GB weights) failed the "fast" bar in `docs/site/models.md`.

### K=8 -> 12?

Four extra schemas at ~150-200 tokens each is +600-800 prompt tokens -- negligible against a
16k window (<5%) and ~0.1 GiB of extra KV. Capacity was never the blocker; the pass-3 split
proved the expected tool is already inside K=8, so raising K adds only distractors for the
current model. With a stronger chooser, K=12 becomes a cheap robustness margin for real user
phrasings (where retrieval will rank worse than on docstring-derived prompts -- see caveats).
Plan: hold K=8 for the A/B, then run a K=12 arm of the 15-prompt bench on the winner.

## 2. The field (as of mid-2026)

**Qwen3.5** (released Feb 2026, Apache 2.0): open sizes 0.8B / 2B / 4B / 9B (plus 27B+),
multimodal, native tool calling, 256K context window (Ollama library: `qwen3.5`; tag sizes
1.0 / 2.7 / 3.4 / 6.6 GB). Community consensus puts the Qwen3 family as "the most stable
series for tool calling among models that run locally," and `qwen3.5:9b` as the strongest
agent model of the 8 GB tier -- when it fits. The 122B scored 72.2 on BFCL-V4 (beating
GPT-5-mini's 55.5); no published BFCL for 4B/9B, but the generational jump is large
(Terminal-Bench 2.0: 52.5 vs 22.5 for Qwen3-era). We already proved `qwen3.5:9b` emits
correct structured tool calls through the full agent stack; it failed only on speed because
the 6.6 GB default tag spills. A community requant, `reecdev/qwen3.5-lowvram:9b` (4.8 GB,
"~1.2 GB less VRAM, near-zero quality loss," 14 tok/s on an RTX 3050), targets exactly this
gap -- the 2060 SUPER has roughly twice the 3050's memory bandwidth, so ~25-30 tok/s class
fully resident is a reasonable expectation. Known wart: an open Ollama issue where
`qwen3.5:9b` sometimes prints the tool call as text instead of executing it
(ollama/ollama#14745) -- watch for it in the sweep's NO_CALL column.

**Qwen3.6**: open weights exist but start at 27B (17 GB) -- out of reach for this box.

**Purpose-built tool-callers** (all Llama-3.1-8B-Instruct fine-tunes unless noted):
- **watt-tool-8B**: claims SOTA on BFCL; pullable directly as `hengwen/watt-tool-8B` (4.9 GB Q4).
- **ToolACE-2-8B** (Team-ACE): highest BFCL-v3 scores among 8B-scale models, "rivaling GPT-4"
  on function calling; Hugging Face only -- needs a GGUF import, no first-party Ollama tag.
- **Hammer2.1-7b** (MadeAgents, Qwen2.5-coder base, function-masking training): best-at-scale
  on BFCL-v3 among function-calling-enhanced models, notably robust to renamed/perturbed
  schemas; HF only, GGUF import.
- **Llama-3-Groq-8B-Tool-Use**: 89.06% BFCL (v1-era number, mid-2024 model) -- the cleanest
  published score, but two generations old; superseded by the above.
The shared risk: these are BFCL specialists. Our failure mode is menu-selection among 8
near-neighbor geospatial tools plus multi-turn chaining with 200-2000-token tool results --
adjacent to, but not the same as, BFCL's schema-adherence focus. And a single-purpose caller
still has to narrate results to the user afterward, which Llama-3.1-8B does adequately but
not better than Qwen3-era models.

**Granite 4.1** (IBM, Jan 2026 era, Apache 2.0): 3B / 8B / 30B, explicitly trained for
tool-calling, structured JSON, and agentic RAG; `granite4.1:8b` is 5.3 GB, 128K context,
first-party Ollama tag. No published BFCL for the 8B. The enterprise-tool-use focus makes it
the best non-Qwen dark horse with a clean license.

**Phi-4-mini** (3.8B, MIT): official function-calling support on Ollama, surprisingly solid
for its size, but community reports say schema adherence gets brittle when chaining calls
with verbose tool definitions -- and 8 schemas x 150-200 tokens is exactly our shape. A
downgrade in headroom from 8B for the thing we care most about. Pass.

**Ministral 8B / Mistral small**: Ministral-8B has native function calling and 128K context,
but the weights are under the Mistral Research License (non-commercial without a paid
license) and only community Ollama tags exist (`nchapman/ministral-8b-instruct-2410`).
Mistral Small 3.x (24B) is the family's real tool-caller and does not fit. Pass on license +
size grounds.

**Nemotron-3-nano:4b** (NVIDIA, 2.8 GB, tool calling, 256K): reasoning-trace-first by design
(answers arrive after a thinking channel), which is the exact behavior `/no_think` exists to
suppress; NVIDIA Open Model License. Keep on the bench, below the Qwen 4B-class options.

**Qwen3-14B and up**: weights alone (~9.3 GB Q4) exceed the card; partial offload lands in
the 4-11 tok/s band on 8 GB cards, and this box's 15 GiB RAM makes it worse. A 174-prompt
sweep at that speed blows the 240 s per-turn cap constantly. Not viable on this hardware.

## 3. Ranked shortlist

1. **Qwen3.5 9B, squeezed to fit** (`reecdev/qwen3.5-lowvram:9b`, 4.8 GB, + q8_0 KV, 16k) --
   the strongest brain already proven to emit correct structured calls on this stack; its
   only recorded failure was VRAM spill, and 4.8 GB weights + ~1.1 GiB quantized KV fits.
2. **Qwen3.5 4B** (`qwen3.5:4b`, 3.4 GB, Apache 2.0) -- same generation as (1) with huge
   headroom (32k context and K=12 both trivially affordable); the generational jump over
   Qwen3-8B may outweigh the 8B->4B size drop.
3. **watt-tool-8B** (`hengwen/watt-tool-8B`, 4.9 GB) -- purpose-built BFCL-SOTA tool-caller,
   one `ollama pull` away; the cheapest test of "does a function-calling specialist fix our
   menu-selection misses."
4. **ToolACE-2-8B** (GGUF import) -- best published 8B-scale BFCL-v3; only worth the import
   friction if watt-tool moves the needle but not enough.
5. **Granite 4.1 8B** (`granite4.1:8b`, 5.3 GB, Apache 2.0) -- newest tool-use-trained 8B
   with a first-party tag and clean license; the fallback if the Qwen3.5 family
   disappoints on structured-call reliability under Ollama.

## 4. Recommendation

**Next experiment: Qwen3.5 9B lowvram at 16k, K=8 (unchanged), with quantized KV cache.**

- `ollama pull reecdev/qwen3.5-lowvram:9b`, then a local variant exactly like the qwen3 one:
  `FROM reecdev/qwen3.5-lowvram:9b` + `PARAMETER num_ctx 16384` -> `qwen3.5:9b-lowvram-16k`.
- Serve with `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0` (halves KV; the
  single cheapest fit lever and it also retroactively helps the current default).
- Keep `TRID3NT_OPENAI_EXTRA_SYSTEM=/no_think` (Qwen3.5 is also thinking-mode-default; verify
  on a smoke prompt that content deltas are non-empty before starting the sweep).
- Keep K=8 so the A/B against pass 3 is clean.
- Go/no-go gate before committing to the full sweep: run 10 prompts; if `nvidia-smi` shows
  spill or median turn latency exceeds ~120 s, abort.

**Fallback if it does not fit or still crawls: `qwen3.5:4b` (3.4 GB), same settings.** It is
guaranteed resident, and if it merely matches qwen3:8b's 45/174 it still wins on speed and
context headroom; if the generational gains are real it beats it. (Third arm, only if both
Qwen3.5 arms disappoint on selection: `hengwen/watt-tool-8B`.)

## 5. Benchmark plan (the A/B)

Continuity target: pass-3 sweep (HIT 45 / MISS 73 / NO_CALL 55 / ERROR 1 over 174) and the
15-prompt bench history (35.7% cold -> 57.1% warm on qwen3:8b-16k).

1. Preserve the baseline: `cp docs/reports/tool-routing-results.jsonl docs/reports/tool-routing-results-qwen3-8b-pass3.jsonl`,
   then reset the live file (the sweep is resumable via that jsonl; a stale copy would skip
   every prompt). Same for the report md if regenerating in place.
2. Pull + create the 16k variant (above); flip `TRID3NT_OPENAI_MODEL=qwen3.5:9b-lowvram-16k`
   in `.env.local`; restart the agent via `scripts/start_agent.sh`.
3. Wait for the retrieval index warm log (`discover index` warm) -- the 35.7% BEFORE run
   already proved a cold-index fail-open poisons results.
4. Smoke: one chat turn ("where is Ybor City?") to confirm non-empty content + a structured
   tool call; watch `nvidia-smi` for full residency. Apply the 10-prompt go/no-go gate.
5. Full sweep: `python scripts/tool_routing_sweep.py` (174 prompts, sequential, resumable).
6. Split: `python scripts/routing_failure_split.py` -- compare HIT/MISS/NO_CALL and confirm
   the MODEL-MISS/RETRIEVAL-MISS split stays 127/0-shaped (it should: retrieval is unchanged).
7. Continuity bench: `python scripts/tool_routing_bench.py` (same 15 prompts) -> a third
   point on the 35.7 -> 57.1 -> X line.
8. If X clearly wins: a K=12 arm (`TRID3NT_TOOL_RETRIEVAL_K=12`) of the 15-prompt bench first
   (cheap), full sweep only if the bench does not regress.
9. Write pass-4 report + failure split; update `docs/site/models.md` matrix.

Wall time: pass-3 turns averaged ~80-90 s with a 240 s cap, so a full 174-prompt sweep is
**~4-6 h** if the model stays resident (budget an evening; it is resumable). The 15-prompt
bench is ~25-40 min. The fallback arm doubles it. Do not skip the go/no-go gate: a spilled
model turns the sweep into a 12 h+ timeout parade.

## 6. Honest caveats

- **Docstring-derived prompts flatter retrieval.** Sweep prompts are generated from the same
  registry descriptions that BM25/dense retrieval indexes, so the 127/0 MODEL-MISS verdict is
  a best-case for the retrieval layer. Real user phrasing will reintroduce RETRIEVAL-miss;
  a model upgrade fixes only the model half.
- **The geocode attractor is partly a scoring artifact.** Prompts contain place names; a
  chain-competent model may legitimately geocode first, then call the expected tool -- the
  sweep scores first-call only. Worth adding a secondary "expected tool within 3 calls"
  metric before declaring a stronger model worse at chains.
- **8 GB is a cliff, not a slope.** 40.6 -> 8.6 tok/s measured for the same 8B model at 16k
  when offload begins; with only ~10 GiB of free system RAM here, offload is doubly bad.
  Every candidate above was chosen to stay resident; verify with `nvidia-smi`, not vibes.
- **Qwen3.5 unknowns**: KV geometry unpublished (estimates above are Qwen3-class
  assumptions); the multimodal vision tower adds tag weight we do not use; the lowvram
  requant is community-made (quality claim unverified); and ollama/ollama#14745 documents
  intermittent printed-not-executed tool calls on `qwen3.5:9b`.
- **BFCL is not our benchmark.** Specialist fine-tunes (watt-tool, ToolACE, Hammer) win on
  schema adherence and abstain-detection; our misses are near-neighbor menu selection among
  178 geospatial tools plus multi-turn digestion of 200-2000-token results. Expect BFCL rank
  and our sweep rank to correlate loosely at best.
- **Licenses**: Qwen3.5 + Granite 4.1 are Apache 2.0; watt-tool/ToolACE inherit the Llama 3.1
  Community License; Ministral is research-only (excluded for that reason); Nemotron is under
  the NVIDIA Open Model License.

## Sources

- https://ollama.com/library/qwen3.5
- https://ollama.com/library/qwen3.6
- https://ollama.com/reecdev/qwen3.5-lowvram
- https://summarizemeeting.com/en/news/ollama-qwen-3-5-local-tool-calling-multimodal
- https://techie007.substack.com/p/qwen-35-the-complete-guide-benchmarks
- https://github.com/ollama/ollama/issues/14745
- https://gorilla.cs.berkeley.edu/leaderboard.html
- https://llm-stats.com/benchmarks/bfcl
- https://localaimaster.com/blog/best-ollama-models-tool-calling
- https://localllm.in/blog/best-local-llms-8gb-vram-2025
- https://localllm.in/blog/ollama-vram-requirements-for-local-llms
- https://ollama.com/hengwen/watt-tool-8B
- https://huggingface.co/Team-ACE/ToolACE-2-Llama-3.1-8B
- https://huggingface.co/MadeAgents/Hammer2.1-7b
- https://github.com/MadeAgents/Hammer
- https://ollama.com/library/granite4.1
- https://ollama.com/library/nemotron-3-nano
- https://techcommunity.microsoft.com/blog/educatordeveloperblog/building-ai-agents-on-edge-devices-using-ollama--phi-4-mini-function-calling/4391029
- https://ollama.com/nchapman/ministral-8b-instruct-2410:8b
- https://docs.mistral.ai/capabilities/function_calling
- https://docs.ollama.com/capabilities/tool-calling
- https://www.webscraft.org/blog/yaku-model-ollama-obrati-dlya-agenta-z-tool-calling-porivnyannya-i-benchmarki?lang=en
