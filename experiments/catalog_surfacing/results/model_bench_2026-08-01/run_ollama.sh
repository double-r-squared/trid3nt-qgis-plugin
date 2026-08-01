#!/usr/bin/env bash
# Local ollama forensic run (PARTIAL sample). Args: <tag> <ollama_model> <limit> <arms...>
# Reaches ollama via the openai-compatible adapter pointed at the LOCAL endpoint
# (env overrides on THIS subprocess only; server config files untouched). Uses the
# production local-serving config for qwen3-family: /no_think in EXTRA_SYSTEM
# (start_agent.sh default) so the reasoning channel does not consume the whole
# max_tokens budget. temp 0, N=1.
set -u
cd /home/nate/Documents/trid3nt-local
TAG="$1"; MODEL="$2"; LIMIT="$3"; shift 3; ARMS="$*"
export MODEL_PROVIDER=openai
export TRID3NT_OPENAI_BASE_URL=http://localhost:11434/v1
export TRID3NT_OPENAI_MODEL="$MODEL"
export TRID3NT_OPENAI_API_KEY=not-needed
export TRID3NT_OPENAI_TEMPERATURE=0
export TRID3NT_OPENAI_NUM_CTX=24576
export TRID3NT_OPENAI_EXTRA_SYSTEM="/no_think"
PY=venvs/agent/bin/python
D=experiments/catalog_surfacing/results/model_bench_2026-08-01
LOG=$D/${TAG}_run.log
echo "=== $TAG start $(date -u +%FT%TZ) model=$MODEL limit=$LIMIT arms=$ARMS ===" >> "$LOG"
for A in $ARMS; do
  echo "=== $TAG ARM $A $(date -u +%FT%TZ) ===" >> "$LOG"
  $PY experiments/catalog_surfacing/run_forensic.py --arm $A --tag "$TAG" --limit "$LIMIT" >> "$LOG" 2>&1
  echo "=== $TAG ARM $A done rc=$? $(date -u +%FT%TZ) ===" >> "$LOG"
done
echo "=== $TAG ALL DONE $(date -u +%FT%TZ) ===" >> "$LOG"
