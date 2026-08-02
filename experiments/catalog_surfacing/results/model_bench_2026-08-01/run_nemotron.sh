#!/usr/bin/env bash
# Nemotron forensic re-run: signed env (.env.local -> OpenRouter nemotron) + temp 0.
# Both arms (0 baseline + 3 stratified). Forensic instrumentation via run_forensic.py.
set -u
cd /home/nate/Documents/trid3nt-local
set -a; . ./.env.local; set +a
export TRID3NT_OPENAI_TEMPERATURE=0
PY=venvs/agent/bin/python
D=experiments/catalog_surfacing/results/model_bench_2026-08-01
LOG=$D/nemotron_run.log
echo "=== nemotron start $(date -u +%FT%TZ) model=$TRID3NT_OPENAI_MODEL base=$TRID3NT_OPENAI_BASE_URL ===" >> "$LOG"
for A in 3 0; do
  echo "=== nemotron ARM $A $(date -u +%FT%TZ) ===" >> "$LOG"
  $PY experiments/catalog_surfacing/run_forensic.py --arm $A --tag nemotron >> "$LOG" 2>&1
  echo "=== nemotron ARM $A done rc=$? $(date -u +%FT%TZ) ===" >> "$LOG"
done
echo "=== nemotron ALL DONE $(date -u +%FT%TZ) ===" >> "$LOG"
