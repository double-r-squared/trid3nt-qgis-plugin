// TRID3NT LOCAL - "Show model thinking" preference (live-feedback 2026-07-08).
//
// The LOCAL build streams the model's reasoning-channel tokens to the chat as
// `agent-thinking-chunk` envelopes; this per-user flag decides whether the
// client ASKS for them (UserMessagePayload.show_thinking) and is only
// meaningful locally. On the CLOUD build the pref is hard-false regardless of
// storage - a cloud client must never send the key or grow local-only UI
// (deployment seam rule: every divergence gates on lib/deployment.ts).
//
// Persistence: localStorage "grace2_show_thinking". DEFAULT ON when unset -
// the value "0" means OFF, anything else / absent / unreadable means ON
// (mirrors the bbox-animation default-on convention).

import { isLocalDeployment } from "./deployment";

export const LS_SHOW_THINKING = "grace2_show_thinking";

/** Raw persisted flag (no deployment gating). Settings reads/writes this. */
export function readShowThinking(): boolean {
  try {
    return window.localStorage.getItem(LS_SHOW_THINKING) !== "0";
  } catch {
    // localStorage unavailable (privacy mode) -> the default: ON.
    return true;
  }
}

/** Persist the flag. "0" = off; "1" = on (any non-"0" value reads as on). */
export function writeShowThinking(enabled: boolean): void {
  try {
    window.localStorage.setItem(LS_SHOW_THINKING, enabled ? "1" : "0");
  } catch {
    // ignore - the toggle simply won't persist.
  }
}

/**
 * The effective per-turn preference Chat.tsx sends: FALSE on the cloud build
 * (always - the key is then omitted from the outgoing user-message entirely),
 * otherwise the persisted flag with its default-ON semantics.
 */
export function showThinkingPref(): boolean {
  if (!isLocalDeployment()) return false;
  return readShowThinking();
}
