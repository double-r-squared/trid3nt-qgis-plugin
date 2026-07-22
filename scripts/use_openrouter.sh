#!/usr/bin/env bash
# use_openrouter.sh -- point the TRID3NT agent at OpenRouter (or back to local
# ollama) by editing .env.local in place, then restart the agent.
#
# Usage:
#   scripts/use_openrouter.sh <OPENROUTER_API_KEY> [model] [num_ctx]
#   scripts/use_openrouter.sh --local          # revert to local ollama (qwen)
#
#   model    default: deepseek/deepseek-chat  (a TOOL-CAPABLE model; the agent
#            is tool-heavy, so pick a model that supports function-calling -
#            deepseek/deepseek-chat, meta-llama/llama-3.3-70b-instruct,
#            qwen/qwen-2.5-72b-instruct, mistralai/*. Free variants add ":free"
#            and are rate-limited.)
#   num_ctx  default: 65536 (OpenRouter has no /api/show; set it or the clip
#            guard false-trips at the 16384 default).
#
# The key is written ONLY to .env.local (already gitignored). A one-time backup
# is saved to .env.local.bak. The key is never printed in full or logged.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/.env.local"
LOCAL_MODEL="qwen3:8b-24k"
LOCAL_BASE="http://127.0.0.1:11434/v1"

if [ ! -f "$ENV_FILE" ]; then
  echo "[use_openrouter] ERROR: $ENV_FILE not found" >&2
  exit 1
fi

# set_kv KEY VALUE -- replace the KEY=... line in place, or append if absent.
set_kv() {
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    # Use a non-/ delimiter since values contain slashes/colons.
    sed "s|^${key}=.*|${key}=${val}|" "$ENV_FILE" > "$tmp"
  else
    cp "$ENV_FILE" "$tmp"
    printf '%s=%s\n' "$key" "$val" >> "$tmp"
  fi
  mv "$tmp" "$ENV_FILE"
}

# One-time backup so --local (or manual restore) is always possible.
[ -f "$ENV_FILE.bak" ] || cp "$ENV_FILE" "$ENV_FILE.bak"

if [ "${1:-}" = "--local" ]; then
  set_kv TRID3NT_OPENAI_BASE_URL "$LOCAL_BASE"
  set_kv TRID3NT_OPENAI_API_KEY  "not-needed"
  set_kv TRID3NT_OPENAI_MODEL    "$LOCAL_MODEL"
  set_kv TRID3NT_OPENAI_NUM_CTX  "24576"
  echo "[use_openrouter] reverted to LOCAL ollama ($LOCAL_MODEL)"
else
  KEY="${1:-}"
  MODEL="${2:-nvidia/nemotron-3-super-120b-a12b:free}"
  NUM_CTX="${3:-32768}"
  if [ -z "$KEY" ]; then
    echo "Usage: $0 <OPENROUTER_API_KEY> [model] [num_ctx]" >&2
    echo "       $0 --local" >&2
    exit 2
  fi
  case "$KEY" in
    sk-or-*) : ;;
    *) echo "[use_openrouter] WARNING: key does not start with 'sk-or-' - continuing anyway" >&2 ;;
  esac
  set_kv MODEL_PROVIDER          "openai"
  set_kv TRID3NT_OPENAI_BASE_URL  "https://openrouter.ai/api/v1"
  set_kv TRID3NT_OPENAI_API_KEY   "$KEY"
  set_kv TRID3NT_OPENAI_MODEL     "$MODEL"
  set_kv TRID3NT_OPENAI_NUM_CTX   "$NUM_CTX"
  echo "[use_openrouter] set OpenRouter: model=$MODEL num_ctx=$NUM_CTX key=${KEY:0:8}...(${#KEY} chars)"
fi

echo "[use_openrouter] restarting agent..."
bash "$REPO_ROOT/scripts/start_agent.sh" 2>&1 | grep -E "PID=|MODEL_PROVIDER" || true
echo "[use_openrouter] done. Backup of the previous env is at $ENV_FILE.bak"
echo "[use_openrouter] revert anytime with: $0 --local"
