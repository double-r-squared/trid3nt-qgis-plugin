#!/usr/bin/env bash
# Resume arms 1 + 2 (arm 0 already checkpointed) + score. Idempotent: each arm
# resumes from its checkpointed jsonl. Live drive -> sources .env.local; temp 0.
set -u
cd /home/nate/Documents/trid3nt-local
set -a; . ./.env.local; set +a
export TRID3NT_OPENAI_TEMPERATURE=0
PY=venvs/agent/bin/python
LOG=experiments/catalog_surfacing/results/run_rest.log
echo "=== start $(date -u +%FT%TZ) ===" >> "$LOG"
for A in 1 2; do
  echo "=== ARM $A $(date -u +%FT%TZ) ===" >> "$LOG"
  $PY experiments/catalog_surfacing/run.py --arm $A >> "$LOG" 2>&1
  echo "=== ARM $A done rc=$? $(date -u +%FT%TZ) ===" >> "$LOG"
done
echo "=== SCORE $(date -u +%FT%TZ) ===" >> "$LOG"
$PY experiments/catalog_surfacing/score.py >> "$LOG" 2>&1
echo "=== ALL DONE $(date -u +%FT%TZ) ===" >> "$LOG"
