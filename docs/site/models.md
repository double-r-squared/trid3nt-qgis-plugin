# TRID3NT Local -- Models

The LLM is pluggable through the OpenAI-compatible seam (`MODEL_PROVIDER=openai` +
`GRACE2_OPENAI_BASE_URL`). This page records what has actually been measured locally: which
models can drive the ~176-tool agent, why the default is what it is, and what the routing
benchmarks say.

Reference box for all numbers below: consumer desktop with an NVIDIA RTX 2060 SUPER (8 GB
VRAM), models served by Ollama with Q4 quantization.

---

## The local model matrix

| Model | Size (Q4) | Verdict | Detail |
|-------|-----------|---------|--------|
| `llama3.2:3b` (+ `3b-32k`) | 2.0 GB | **Narrates, cannot call** | Fine for plain conversational responses; never emitted a valid composer tool call in the original proof run (the MODFLOW e2e had to fall back to direct tool invocation). Do not use it to drive tools. |
| `qwen3.5:9b` (+ `9b-8k`) | 6.6 GB | **Tools, but slow** | Produces structured tool calls, but the 6.6 GB weights plus KV cache spill past the 8 GB VRAM budget, so layers offload to CPU and turns crawl. Usable only if you have more VRAM. |
| `qwen3:8b-16k` | 5.2 GB | **DEFAULT -- tools + fast** | Structured tool calls proven through the full agent stack (every engine's LLM-driven e2e called the right composer on turn 1 with 0 nudges). Fits in VRAM with a 16k context. |

The one-line reliability matrix from the commit that set the default:
`3b = no tools, 9b = tools-but-slow, 8b = tools+fast`.

### Why `qwen3:8b-16k` (the 16k part)

`qwen3:8b-16k` is not a Docker Hub model -- it is a locally-created Ollama variant:

```
FROM qwen3:8b
PARAMETER num_ctx 16384
```

Ollama serves models with `num_ctx 4096` by default, which is too small for the agent's system
prompt + tool declarations + case context; 16384 gives comfortable headroom (and affords
extras like retrieval-trimmed tool schemas and future lesson injection) while keeping the KV
cache small enough to stay resident on an 8 GB card. The base model's native window is 40960,
so there is room to go higher if you have the VRAM.

### The `/no_think` requirement

Qwen3-family models default to **thinking mode**: all tokens stream to the reasoning channel,
the OpenAI-compat content deltas arrive empty, and the turn renders no text in the chat.
`.env.local` ships `GRACE2_OPENAI_EXTRA_SYSTEM=/no_think`, which the adapter appends to the
system prompt to disable thinking. Keep it set for any Qwen3 model (it is a generic
system-suffix seam -- harmless for models that ignore it).

---

## Tool retrieval (top-K)

An 8B model cannot reliably pick the right tool out of a 176-tool catalog (measured below), so
the local build runs the retrieval layer in **enforce** mode: each turn, the user text is
ranked against the tool corpus (BM25 + name-substring + local dense embeddings, fused with
RRF, reusing `discover_dataset`'s cached index) and only the top-K tools (plus a hot-set floor,
union-ed monotonically per Case) are declared to the model.

- `GRACE2_TOOL_RETRIEVAL=enforce`, `GRACE2_TOOL_RETRIEVAL_K=8` locally (code default K=25).
- **Fail-open**: a cold index, an error, or an empty ranking shows the full registry -- the
  layer can never hide every tool.
- The index is warmed at startup (`asyncio.to_thread`); until the warm completes, retrieval
  logs `discover index COLD; FAIL-OPEN to full registry` and the model faces all 176 tools --
  which measurably hurts routing (below).

## Benchmark history

### 15-prompt breadth bench (cold vs warm index)

Same 15 prompts (fetchers, terrain chains, 5 solver composers, 1 no-tool control) on
`qwen3:8b-16k` + `/no_think`:

| Run | Selection accuracy | Failure clusters |
|-----|--------------------|------------------|
| BEFORE (cold index -> fail-open, all 176 tools in context) | **35.7%** (5/14) | `web_fetch` attractor (3 fetch prompts collapsed onto the generic web tool); no-call cluster (3 prose answers with zero dispatch); near-neighbor confusion (hillshade vs colored-relief, SFINCS vs SWMM, tsunami vs saltwater-intrusion) |
| AFTER (warm index, true top-K enforcement) | **57.1%** (8/14) | attractor + no-call clusters eliminated; residual misses were chain-depth vs the 4-minute cap, not selection |

Args were valid 15/15 in both runs, and the no-tool control never fired a tool -- **selection,
not schema-filling, is the bottleneck**. Distinctively-named composers (MODFLOW sustainable
yield, SWMM, OpenQuake PSHA) routed exactly even cold.

### Pass-3 per-tool routing sweep

One docstring-derived prompt per registered tool (174 scored), case-seeded (a DEM layer
pre-loaded so layer-consuming tools have a real target), K=8:

| Outcome | Count | Meaning |
|---------|-------|---------|
| HIT | 45 | expected tool fired |
| MISS | 73 | a different tool fired first (most common wrong first call: `geocode_location`) |
| NO_CALL | 55 | prose answer, no dispatch |
| ERROR | 1 | WS connection error |

Full table: `docs/reports/tool-routing-report.md` in the `trid3nt-local` repo.

### The retrieval-vs-model split (the verdict)

Every failed prompt was replayed through `retrieve_visible_tools` (K=8) and classified:

- **RETRIEVAL-MISS** (expected tool absent from the shortlist -- the model never saw it): **0**
- **MODEL-MISS** (tool was on the menu; the model chose otherwise): **127** (all scored failures)

At K=8 the retrieval layer put the expected tool in front of the model every single time.
The residual routing gap is a **model-capability limit of the 8B class**, not a retrieval
tuning problem -- raising K, re-weighting the corpus, or adding retrieval features will not
close it. The lever is a stronger model. Full split:
`docs/reports/tool-routing-failure-split.md`.

---

## Trying bigger models

The seam makes experiments cheap:

1. `ollama pull <model>`; if it is tool-capable, create a `num_ctx` variant (16384 minimum
   recommended) exactly as for qwen3.
2. Set `GRACE2_OPENAI_MODEL` in `.env.local` and restart the agent.
3. Watch VRAM: on an 8 GB card, ~5 GB of Q4 weights + a 16k KV cache is about the ceiling
   (this is precisely why qwen3.5:9b at 6.6 GB failed the "fast" bar). Spill to CPU shows up
   as multi-minute turns, not errors.
4. Re-run the harnesses to get numbers, not vibes: `scripts/tool_routing_bench.py`
   (15-prompt bench) and `scripts/tool_routing_sweep.py` (per-tool sweep, resumable), then
   `scripts/routing_failure_split.py` for the retrieval-vs-model split.
5. Escape hatch: point `GRACE2_OPENAI_BASE_URL` + `GRACE2_OPENAI_API_KEY` at any cloud
   OpenAI-compatible API when a local model is not cutting it. Same agent, same tools.
