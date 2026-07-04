# TRID3NT Local

Offline / local-first build of TRID3NT (GRACE-2): the AI workbench for
multi-hazard geospatial modeling, running entirely on your own machine.

- One local server + the same web UI as the cloud app (browser opens localhost)
- Pluggable LLM via any OpenAI-compatible endpoint: local (Ollama, vLLM,
  llama.cpp, LM Studio) or cloud (OpenAI, Groq, DeepSeek, OpenRouter, ...)
- Simulations run locally: MODFLOW 6 first, SFINCS next; more engines follow
- File-based persistence + local tile rendering -- no cloud account required

Status: pre-alpha scaffold. Design doc lands in `docs/design/`.

## Layout (planned)

```
server/     local agent server (WS + HTTP, LLM provider seam, local solver exec)
web/        the SPA (same UI as cloud, localhost backend)
workers/    engine runners (mf6 subprocess, SFINCS docker)
docs/       design + user docs
```
