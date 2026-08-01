#!/usr/bin/env bash
# Local ollama forensic run, STRATIFIED PARTIAL sample. Args: <tag> <model> <N> <arms...>
# N phrasings per source group (spans all 14 sources) + 2N controls. Production
# local-serving config for qwen3-family (/no_think). Env overrides on THIS
# subprocess only; server config untouched. temp 0, max_tokens 4096, N=1 trial.
set -u
cd /home/nate/Documents/trid3nt-local
TAG="$1"; MODEL="$2"; N="$3"; shift 3; ARMS="$*"
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
echo "=== $TAG start $(date -u +%FT%TZ) model=$MODEL stratified=$N arms=$ARMS ===" >> "$LOG"
for A in $ARMS; do
  echo "=== $TAG ARM $A $(date -u +%FT%TZ) ===" >> "$LOG"
  $PY experiments/catalog_surfacing/run_forensic.py --arm $A --tag "$TAG" --stratified "$N" >> "$LOG" 2>&1
  echo "=== $TAG ARM $A done rc=$? $(date -u +%FT%TZ) ===" >> "$LOG"
done
echo "=== $TAG ALL DONE $(date -u +%FT%TZ) ===" >> "$LOG"
