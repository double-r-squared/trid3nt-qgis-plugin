#!/usr/bin/env bash
# Full catalog-surfacing run: 3 arms (own process each) + deterministic scoring.
# Live drive -> sources .env.local; temperature pinned 0; N=1.
set -u
cd /home/nate/Documents/trid3nt-local
set -a; . ./.env.local; set +a
export TRID3NT_OPENAI_TEMPERATURE=0
PY=venvs/agent/bin/python
LOG=experiments/catalog_surfacing/results/run_all.log
echo "=== start $(date -u +%FT%TZ) provider=$MODEL_PROVIDER model=$TRID3NT_OPENAI_MODEL temp=$TRID3NT_OPENAI_TEMPERATURE ===" > "$LOG"
for A in 0 1 2; do
  echo "=== ARM $A $(date -u +%FT%TZ) ===" >> "$LOG"
  $PY experiments/catalog_surfacing/run.py --arm $A >> "$LOG" 2>&1
  echo "=== ARM $A done rc=$? $(date -u +%FT%TZ) ===" >> "$LOG"
done
echo "=== SCORE $(date -u +%FT%TZ) ===" >> "$LOG"
$PY experiments/catalog_surfacing/score.py >> "$LOG" 2>&1
echo "=== ALL DONE $(date -u +%FT%TZ) ===" >> "$LOG"
